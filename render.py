import json
import subprocess
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
PLATFORMS_FILE = BASE / "configs" / "platforms.json"
BRANDS_DIR = BASE / "configs" / "brands"
BANNERS_DIR = BASE / "banners"

REF_W, REF_H = 1080, 1920
IMAGE_EXT = {"png", "jpg", "jpeg", "webp"}
VIDEO_EXT = {"mp4", "webm", "mkv"}
ANIM_EXT = {"gif"}
BANNER_EXT = IMAGE_EXT | VIDEO_EXT | ANIM_EXT

# Как бренд называет себя в интерфейсе бота
BRAND_LABELS = {
    "mostbet": "Mostbet",
    "playerok": "Playerok",
    "funpay": "FunPay",
    "mycsgo": "MyCSGO",
    "1win": "1Win",
}

PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "insta": "Instagram Reels",
    "shorts": "YouTube Shorts",
}

# Потоки ffmpeg. По умолчанию 1: на хостинге с маленькой памятью (Render free
# tier - 512 МБ) многопоточный x264 на вертикальном 1080x1920 упирается в OOM
# и контейнер перезапускается на середине рендера. Подними до 2+, если памяти
# хватает - рендер станет заметно быстрее.
FFMPEG_THREADS = 1

# Потолок выходного кадра. НЕ путать с потоками кодировщика: память под рендер
# ест декодирование входа плюс фильтры, и она растёт вместе с числом пикселей
# кадра. Замеры на этой машине (ffmpeg + колорокей + overlay, вертикаль):
#   1080x1920 @60fps, баннер 2000x1920 -> пик 362 МБ
#   1080x1920 @60fps, баннер 1440x600  -> пик 231 МБ
#   1440x2560 @60fps одним проходом     -> пик 488 МБ (впритык к 512)
#   1440x2560 @60fps двумя проходами    -> пик 242 + 393 МБ
# У Render free tier 512 МБ, поэтому крупный кадр проходит через
# build_prepass_cmd(), а на выход всегда идёт не выше MAX_OUT_*.
MAX_OUT_W, MAX_OUT_H = 1080, 1920

# Потолок входа. Всё между MAX_OUT_* и этим лимитом бот ужимает сам,
# отдельным проходом (см. build_prepass_cmd).
#
# Граница задана по ПЛОЩАДИ кадра, а не по отдельным сторонам: память под
# декодирование растёт с числом пикселей, а не с длиной стороны. Замеры
# препрохода + наложения (insta, худшая площадка, 59с):
#   1170x2532 = 2.96 Мп -> 344 МБ   (iPhone 11/12/13)
#   1284x2778 = 3.57 Мп -> 373 МБ   (iPhone Pro Max)
#   1440x2560 = 3.69 Мп -> 396 МБ
#   2160x3840 = 8.29 Мп -> 447 МБ на одном препроходе (запас 13%)
# 4 Мп - последняя точка, где запас ещё рабочий; 8 Мп оставляют 13% и
# того рискованнее.
MAX_IN_PIXELS = 4_000_000


def load_platforms():
    with open(PLATFORMS_FILE, encoding="utf-8") as f:
        return json.load(f)


BRANDS_FILE = BASE / "configs" / "brands.json"


def list_brands():
    """Активные бренды.

    Если есть configs/brands.json - берём список оттуда (можно оставить только
    Playerok, пока проверяем его). Иначе - все конфиги в configs/brands/.
    """
    if BRANDS_FILE.exists():
        with open(BRANDS_FILE, encoding="utf-8") as f:
            names = json.load(f).get("active", [])
        return [n for n in names if (BRANDS_DIR / f"{n}.json").exists()]
    if not BRANDS_DIR.exists():
        return []
    return sorted(p.stem for p in BRANDS_DIR.glob("*.json"))


def list_platforms():
    return [k for k in load_platforms() if not k.startswith("_")]


def load_brand(brand):
    path = BRANDS_DIR / f"{brand}.json"
    if not path.exists():
        raise FileNotFoundError(f"Нет конфига бренда: configs/brands/{brand}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_banner(brand, config, platform=None):
    """Ищем медиафайл баннера.

    Порядок приоритета:
      1. banners/<brand>/<platform>.<ext>  - отдельный файл под площадку
      2. banners/<brand>/<platform>.*       - то же, любое расширение
      3. поле "media" из конфига            - общий файл на все площадки
      4. первый попавшийся медиафайл в папке бренда
    """
    folder = BANNERS_DIR / brand
    if not folder.exists():
        return None

    media_in_folder = [
        f for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lstrip(".").lower() in BANNER_EXT
    ]

    if platform:
        exact = [f for f in media_in_folder if f.stem.lower() == platform.lower()]
        if exact:
            return exact[0]

    preferred = config.get("media")
    if preferred:
        candidate = folder / preferred
        if candidate.exists():
            return candidate
        if preferred in config.get("media_per_platform", {}):
            per = folder / config["media_per_platform"][preferred]
            if per.exists():
                return per

    return media_in_folder[0] if media_in_folder else None


def detect_key_color(path, sample_size=96, frames=3):
    """Определяет цвет хромакея по рамке кадра.

    Идея: фон хромакейного баннера занимает края и он однотонный. Берём несколько
    кадров, уменьшаем, смотрим доминирующий цвет пикселей рамки.

    Возвращает (r, g, b) или None, если фон нейтральный (серый/белый/чёрный) -
    в таком случае ключить нечего и контент лучше не трогать.
    """
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", str(path),
        "-vf", f"scale={sample_size}:{sample_size}:flags=area",
        "-frames:v", str(frames),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if not raw:
        return None

    frame_bytes = sample_size * sample_size * 3
    count = len(raw) // frame_bytes
    if count == 0:
        return None

    # Рамка шириной 4px по периметру - там гарантированно фон, а не контент.
    border: list[tuple[int, int, int]] = []
    t = 4
    for i in range(count):
        frame = raw[i * frame_bytes:(i + 1) * frame_bytes]
        for y in range(sample_size):
            row = y * sample_size * 3
            if y < t or y >= sample_size - t:
                for x in range(sample_size):
                    o = row + x * 3
                    border.append((frame[o], frame[o + 1], frame[o + 2]))
        for y in range(t, sample_size - t):
            row = y * sample_size * 3
            for x in list(range(t)) + list(range(sample_size - t, sample_size)):
                o = row + x * 3
                border.append((frame[o], frame[o + 1], frame[o + 2]))

    # Доминирующий цвет рамки.
    best = max(set(border), key=border.count)
    share = border.count(best) / len(border)

    # Фон должен быть однотонным: иначе это просто красивая картинка, а не хромакей.
    if share < 0.80:
        return None

    r, g, b = best
    # Насыщенность. Нейтральный фон (серый/белый/чёрный) ключить нельзя -
    # снесём половину контента.
    sat = (max(r, g, b) - min(r, g, b)) / 255
    if sat < 0.15:
        return None

    return (r, g, b)


def resolve_chroma(config, banner_path):
    """Возвращает параметры colorkey или None.

    В конфиге можно задать цвет руками:
        "chroma_key": {"color": "0x284402", "similarity": 0.10, "blend": 0.05}
    Если цвет не задан - определяем автоматически по рамке кадра.
    """
    ck = config.get("chroma_key")
    if not ck:
        return None

    color = ck.get("color")
    if color in (None, "auto"):
        detected = detect_key_color(banner_path)
        if not detected:
            return None
        r, g, b = detected
        color = f"0x{r:02X}{g:02X}{b:02X}"
    else:
        r = g = b = None

    return {
        "color": color,
        "similarity": float(ck.get("similarity", 0.10)),
        "blend": float(ck.get("blend", 0.05)),
        "rgb": (r, g, b),
        "auto": ck.get("color") in (None, "auto"),
    }



def probe(path):
    """Возвращает (w, h, duration) из ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe не нашёл видеопоток в {path}")
    s = streams[0]
    try:
        duration = float(data.get("format", {}).get("duration") or s.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return int(s["width"]), int(s["height"]), duration


def safe_box(platform, video_w, video_h):
    """Пересчитываем insets с 1080x1920 на фактический кадр и возвращаем
    (x0, y0, x1, y1) - область, которую интерфейс НЕ перекрывает."""
    cfg = load_platforms()[platform]
    ins = cfg["inset"]
    sx = video_w / REF_W
    sy = video_h / REF_H

    left = int(round(ins["left"] * sx))
    right = int(round(ins["right"] * sx))
    top = int(round(ins["top"] * sy))
    bottom = int(round(ins["bottom"] * sy))

    x0, y0 = left, top
    x1, y1 = video_w - right, video_h - bottom

    # На нестандартном кадре insets могут съесть весь safe box - поджимаем.
    if x1 - x0 < 64:
        x0, x1 = 0, video_w
    if y1 - y0 < 64:
        y0, y1 = 0, video_h
    return x0, y0, x1, y1


def plan_overlay(platform, video_w, video_h, banner_w, banner_h, config):
    """Считает финальный размер и координаты баннера внутри safe box.
    Пропорции баннера сохраняются, площадь не опускается ниже min_area_ratio."""
    x0, y0, x1, y1 = safe_box(platform, video_w, video_h)
    box_w, box_h = x1 - x0, y1 - y0

    aspect = banner_w / banner_h if banner_h else 1.0

    def fit(width):
        """Размер баннера под заданную ширину, вписанный в safe box с сохранением пропорций."""
        w = max(1, min(int(round(width)), box_w))
        h = max(1, int(round(w / aspect)))
        if h > box_h:
            h = box_h
            w = max(1, int(round(h * aspect)))
            if w > box_w:
                w = box_w
                h = max(1, int(round(w / aspect)))
        return w, h

    target_w, target_h = fit(box_w * float(config.get("width_ratio", 0.8)))

    # ТЗ: баннер занимает не менее 1/6 экрана. Если текущий размер мал - растягиваем
    # до предела safe box, но пропорции и границы не нарушаем.
    min_area = float(config.get("min_area_ratio", 0)) * video_w * video_h

    # Максимум, который вообще можно получить, не вылезая из safe box и не ломая пропорции.
    max_area_possible = fit(box_w)[0] * fit(box_w)[1]

    min_area_met = target_w * target_h >= min_area
    if not min_area_met:
        full_w, full_h = fit(box_w)
        if full_w * full_h > target_w * target_h:
            target_w, target_h = full_w, full_h
        min_area_met = target_w * target_h >= min_area

    gap = int(round(box_h * float(config.get("gap_ratio", 0.06))))

    if config.get("anchor") == "top":
        pos_x = x0 + (box_w - target_w) // 2
        pos_y = y0 + gap
    else:
        pos_x = x0 + (box_w - target_w) // 2
        pos_y = y1 - target_h - gap

    # Не даём баннеру вылезти за safe box.
    pos_x = max(x0, min(pos_x, x1 - target_w))
    pos_y = max(y0, min(pos_y, y1 - target_h))

    area = target_w * target_h
    return {
        "pos": (pos_x, pos_y),
        "size": (target_w, target_h),
        "safe_box": (x0, y0, x1, y1),
        "area_ratio": round(area / (video_w * video_h), 4),
        "min_area_ratio": round(min_area / (video_w * video_h), 4) if min_area else 0,
        "min_area_met": area >= min_area,
        "max_possible_area_ratio": round(max_area_possible / (video_w * video_h), 4),
    }


def plan_banner(platform, banner_path, config, video_w=REF_W, video_h=REF_H):
    """Считает план раскладки баннера, ничего не рендеря.

    Нужно, чтобы показать пользователю размер и предупредить про площадь
    ДО того, как он отправит видео в рендер на пару минут.

    video_w/video_h - размер ИСХОДНИКА. Если он выше потолка, раскладка
    считается для ужатого кадра - ровно так же, как это делает build_filter.
    """
    banner_path = Path(banner_path)
    banner_w, banner_h, _ = probe(banner_path)

    out_w, out_h = out_size(video_w, video_h)
    plan = plan_overlay(platform, out_w, out_h, banner_w, banner_h, config)
    plan["banner_size"] = (banner_w, banner_h)
    plan["src_size"] = (video_w, video_h)
    plan["out_size"] = (out_w, out_h)
    plan["downscaled"] = (out_w, out_h) != (video_w, video_h)
    return plan


def out_size(video_w, video_h):
    """Размер выходного кадра: не выше MAX_OUT_W x MAX_OUT_H, пропорции 9:16
    сохраняем. Исходник меньше потолка не растягиваем - апскейл ничего не даёт,
    а только жрёт память и место."""
    if video_w <= MAX_OUT_W and video_h <= MAX_OUT_H:
        return video_w, video_h
    scale = min(MAX_OUT_W / video_w, MAX_OUT_H / video_h)
    return max(2, int(round(video_w * scale)) // 2 * 2), max(2, int(round(video_h * scale)) // 2 * 2)


def build_filter(platform, video_w, video_h, banner_w, banner_h, config, chroma, duration):
    out_w, out_h = out_size(video_w, video_h)

    # Сначала ужимаем исходник до потолка, и уже на ужатом кадре считаем
    # раскладку баннера: safe box и координаты должны быть в координатах
    # выходного кадра, иначе баннер уедет.
    plan = plan_overlay(platform, out_w, out_h, banner_w, banner_h, config)
    pos_x, pos_y = plan["pos"]
    bw, bh = plan["size"]
    plan["src_size"] = (video_w, video_h)
    plan["out_size"] = (out_w, out_h)
    plan["downscaled"] = (out_w, out_h) != (video_w, video_h)

    chains = []
    if plan["downscaled"]:
        # scale ДО overlay: декодированный кадр 4K ужимается сразу, поэтому
        # колорокей и наложение работают уже с 1080x1920 и не держат в памяти
        # четыре мегапикселя на каждый буферизованный кадр.
        chains.append(
            f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2,setsar=1[src]"
        )
        vsrc = "src"
    else:
        vsrc = "0:v"

    chains.append(f"[1:v]scale={bw}:{bh}:force_original_aspect_ratio=decrease[banner_raw]")

    if chroma:
        chains.append(
            f"[banner_raw]colorkey={chroma['color']}:{chroma['similarity']}:{chroma['blend']}[banner_keyed]"
        )
    else:
        chains.append("[banner_raw]null[banner_keyed]")

    chains.append(
        f"[{vsrc}][banner_keyed]overlay={pos_x}:{pos_y}:"
        f"eof_action=repeat:shortest=0:format=auto[outv]"
    )
    return ";".join(chains), plan


def build_ffmpeg_cmd(input_vid, banner_vid, out_vid, platform, brand, duration=0.0):
    config = load_brand(brand)
    vw, vh, _ = probe(input_vid)
    bw, bh, _ = probe(banner_vid)
    ext = Path(banner_vid).suffix.lstrip(".").lower()
    chroma = resolve_chroma(config, banner_vid)

    # -hide_banner и -loglevel error: ffmpeg иначе печатает баннер и построчный
    # прогресс (для 59-секундного ролика это десятки килобайт), которые мы
    # затем держим в памяти процесса вместо того, чтобы просто отбросить.
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_vid)]

    # Зацикливаем баннер, чтобы он шёл всю длину ролика.
    if ext in ANIM_EXT:
        cmd += ["-ignore_loop", "0", "-i", str(banner_vid)]
    elif ext in VIDEO_EXT:
        cmd += ["-stream_loop", "-1", "-i", str(banner_vid)]
    else:
        cmd += ["-loop", "1", "-i", str(banner_vid)]

    filters, plan = build_filter(platform, vw, vh, bw, bh, config, chroma, duration)
    plan["chroma"] = chroma

    # Один поток и ultrafast держат кодировщик скромным, но основную память ест
    # декодирование исходника и буферы фильтров - они растут с числом пикселей.
    # Потолок выходного кадра задаёт out_size() и build_filter() (scale ДО
    # overlay), а не этот список: 2160x3840 на Render free tier с 512 МБ
    # выедал 733 МБ и контейнер убивало с OOM.
    cmd += [
        "-filter_complex", filters,
        "-map", "[outv]",
        "-map", "0:a?",
        "-threads", str(FFMPEG_THREADS),
        "-filter_threads", "1",
        "-filter_complex_threads", "1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "26",
        "-maxrate", "2500k",
        "-bufsize", "500k",
        "-pix_fmt", "yuv420p",
        # 1080x1920 = 8160 макроблоков. level 3.1 держит только 3600, поэтому
        # для вертикальных роликов нужен 4.1 - иначе x264 ругается на размер кадра.
        "-profile:v", "main",
        "-level", "4.1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "1",
        "-ar", "44100",
        "-movflags", "+faststart",
    ]
    if duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [str(out_vid)]
    return cmd, plan


def build_prepass_cmd(input_vid, out_vid, duration=0):
    """Проход ужатия: только декод исходника и scale, без баннера.

    Нужен для кадров выше MAX_OUT_*. Один ffmpeg с наложением на большом
    исходнике держит в памяти и декодированные кадры, и буферы колоркея с
    overlay одновременно. Замеры на 1440x2560 + insta (59с):
      один проход  -> 488 МБ (запас 5%, Render считал это OOM)
      два прохода  -> 242 МБ тут и 393 МБ тут (запас 23%)
    Разносим по разным процессам, и тяжёлые буферы не накладываются.
    """
    out_w, out_h = out_size(*probe(input_vid)[:2])
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(input_vid),
        # force_divisible_by обязателен: scale с force_original_aspect_ratio
        # сам округляет вторую сторону, и для нестандартных кадров iPhone
        # (1170x2532, 1260x2720, 1284x2778) получалась нечётная высота,
        # которую yuv420p отвергает с кодом -22.
        "-vf", f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:"
               f"force_divisible_by=2,setsar=1",
        "-threads", str(FFMPEG_THREADS),
        "-filter_threads", "1",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "26",
        "-maxrate", "2500k",
        "-bufsize", "500k",
        "-pix_fmt", "yuv420p",
        "-profile:v", "main",
        "-level", "4.1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "1",
        "-ar", "44100",
    ]
    if duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [str(out_vid)]
    return cmd


def _run_ffmpeg(cmd):
    # subprocess.run сам вычитывает stderr в фоне, поэтому ffmpeg не встанет
    # на записи в полный канал. В PIPE оставляем только ошибки - прогресс
    # отключён флагами выше.
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise RuntimeError(f"ffmpeg упал (код {proc.returncode}):\n{tail}")


def render(input_vid, banner_vid, out_vid, platform, brand):
    src_w, src_h, duration = probe(input_vid)

    # Крупный кадр ужимаем отдельным проходом: колорокей и overlay поверх
    # 2160x3840 в одном процессе не помещаются в 512 МБ Render.
    if src_w > MAX_OUT_W or src_h > MAX_OUT_H:
        with tempfile.TemporaryDirectory(prefix="prepass_") as tmp:
            mid = Path(tmp) / "mid.mp4"
            _run_ffmpeg(build_prepass_cmd(input_vid, mid, duration))
            cmd, plan = build_ffmpeg_cmd(mid, banner_vid, out_vid, platform, brand, duration)
            # Плану показываем исходник, а не промежуточный файл.
            plan["src_size"] = (src_w, src_h)
            plan["prepassed"] = True
            _run_ffmpeg(cmd)
        return plan

    cmd, plan = build_ffmpeg_cmd(input_vid, banner_vid, out_vid, platform, brand, duration)
    plan["prepassed"] = False
    _run_ffmpeg(cmd)
    return plan
