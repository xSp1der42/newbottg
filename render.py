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

# Где может стоять баннер: три положения по вертикали, все привязаны к краям
# кадра. Боковых нет намеренно - узкая колонка не набирает площадь из ТЗ.
# Прячем ли баннер под интерфейс площадки, решают insets в configs/platforms.json,
# сейчас они нулевые.
POSITIONS = ("top", "center", "bottom")
POSITION_LABELS = {
    "top": "⬆️ Сверху",
    "center": "🎯 По центру",
    "bottom": "⬇️ Снизу",
}
DEFAULT_POSITION = "bottom"

# --- Обрезка хромакея по контенту -------------------------------------------
# Кадр хромакеЙного баннера заметно больше того, что в нём видно: зелёный
# фон занимает большую часть площади (insta - 1%). Если масштабировать кадр
# целиком, контент остаётся мелким, а ТЗ по площади формально выполняется на
# пустом зелёном фоне. Поэтому ищем bbox непрозрачных пикселей и обрезаем
# кадр по нему - тогда размер в плане относится к тому, что реально видно.
#
# Порог альфы, по которому пиксель считается контентом (0-255). Держим
# мягче обычного порога колоркея: у баннеров есть растушёванный край, и на
# жёстком пороге он выпадает из bbox - тогда crop срезает светящуюся кромку.
CONTENT_ALPHA_MIN = 16
# Ищем bbox на уменьшенной копии: точность нужна только у самой границы, а
# 2000x1920 пикселей в Python считать незачем. Плюс convert_scan8-style
# масштаб дешёвый и одинаковый для картинок и видео.
CONTENT_SAMPLE = 256
# Запас вокруг найденного контента. Всё лишнее потом срезает colorkey, поэтому
# лучше перестраховаться и не подрезать края баннера. Запас держим и от
# квантизации замера, и от того, что баннер анимированный: контент между
# опорными кадрами может вылезти за объединение на несколько пикселей.
CONTENT_MARGIN_RATIO = 0.03
# Сколько кадров берём для замера. Баннеры анимированные: контент ездит и
# меняет размер, поэтому замер по нескольким кадрам, размазанным по всей
# длине, иначе обрезает содержимое на середине ролика.
CONTENT_FRAMES = 12
# Если контент занимает почти весь кадр - обрезать нечего.
CONTENT_CROP_MAX_FILL = 0.98

# Горизонтальный вылет за safe box. Задаётся в конфиге бренда как
# horizontal_bleed_ratio (доля ширины кадра на сторону). Сейчас insets нулевые,
# поэтому вылет ничего не даёт и нужен только если вернуть отступы площадок.
# Вертикальный вылет запрещён всегда - баннер не должен вылезать за кадр.

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


_CONTENT_BOX_CACHE = {}
# translate() переводит альфу в 0/1 на скорости C, поэтому поиск границ - это
# find() по строке, а не цикл по пикселям в Python.
_ALPHA_MASK = bytes(1 if i > CONTENT_ALPHA_MIN else 0 for i in range(256))


def _measure_content_box(path, chroma, sample, frames):
    """Ищет bbox непрозрачных пикселей после colorkey. Возвращает (x, y, w, h)
    в пикселях исходника или None.

    Кадры берёмся РАСПРЕДЕЛЁННЫМИ ПО ВСЕЙ ДЛИНЕ, а не подряд с начала: баннеры
    анимированные, контент в них ездит и меняет размер, и bbox по первым
    кадрам обрезал бы содержимое на середине ролика. Объединяем всё в один
    прямоугольник, чтобы не срезать контент ни на одном кадре.
    """
    frame_w, frame_h, duration = probe(path)
    fps = f"fps={frames / duration:.6f}," if duration > 0 else ""
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-vf", (f"{fps}scale={sample}:{sample}:flags=area,"
                f"colorkey={chroma['color']}:{chroma['similarity']}:{chroma['blend']},"
                f"format=rgba"),
        "-frames:v", str(frames),
        "-f", "rawvideo", "-pix_fmt", "rgba", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if not raw:
        return None

    frame_bytes = sample * sample * 4
    count = min(len(raw) // frame_bytes, frames)
    if count == 0:
        return None

    # Объединение по кадрам: баннер может быть анимированным, и обрезать надо
    # так, чтобы не срезать контент ни на одном кадре. Параллельно запоминаем
    # самый крупный кадр - по нему поймём, сколько графики видно в моменте,
    # когда баннер развернулся полностью.
    min_x, min_y, max_x, max_y = sample, sample, -1, -1
    peak = 0
    for i in range(count):
        # Альфа - каждый 4-й байт rgba.
        alpha = raw[i * frame_bytes + 3:(i + 1) * frame_bytes:4]
        mask = alpha.translate(_ALPHA_MASK)
        if b"\x01" not in mask:
            continue
        fmin_x, fmax_x, fmin_y, fmax_y = sample, -1, sample, -1
        for y in range(sample):
            row = mask[y * sample:(y + 1) * sample]
            x0 = row.find(b"\x01")
            if x0 < 0:
                continue
            x1 = sample - 1 - row[::-1].find(b"\x01")
            fmin_x = min(fmin_x, x0)
            fmax_x = max(fmax_x, x1)
            fmin_y = min(fmin_y, y)
            fmax_y = max(fmax_y, y)
        if fmax_x < 0:
            continue
        min_x = min(min_x, fmin_x)
        max_x = max(max_x, fmax_x)
        min_y = min(min_y, fmin_y)
        max_y = max(max_y, fmax_y)
        peak = max(peak, (fmax_x - fmin_x + 1) * (fmax_y - fmin_y + 1))

    if max_x < 0:
        return None

    # В долях кадра -> в пиксели исходника, с запасом по краям.
    mx = frame_w * CONTENT_MARGIN_RATIO
    my = frame_h * CONTENT_MARGIN_RATIO
    x0 = max(0, int(min_x / sample * frame_w) - int(mx))
    y0 = max(0, int(min_y / sample * frame_h) - int(my))
    x1 = min(frame_w, int((max_x + 1) / sample * frame_w) + int(mx))
    y1 = min(frame_h, int((max_y + 1) / sample * frame_h) + int(my))

    # Чётные стороны: обрезанный кадр уходит дальше в scale и colorkey, а
    # нечётная ширина на yuv420p местами даёт артефакты на краю.
    x0 -= x0 % 2
    y0 -= y0 % 2
    w = min(frame_w - x0, max(2, (x1 - x0) // 2 * 2))
    h = min(frame_h - y0, max(2, (y1 - y0) // 2 * 2))

    if w * h >= frame_w * frame_h * CONTENT_CROP_MAX_FILL:
        return None  # контент почти во весь кадр, обрезать нечего

    # Доля объединения, которую занимает самый крупный кадр. У анимированного
    # баннера графика ездит внутри объединения, поэтому даже когда слот равен
    # 18% кадра, в кадре видно меньше - и это надо показывать честно.
    union_cells = (max_x - min_x + 1) * (max_y - min_y + 1)
    fill = peak / union_cells if union_cells else 1.0
    return (x0, y0, w, h), round(fill, 3)


def detect_content_box(banner_path, config, chroma=None, sample=CONTENT_SAMPLE,
                       frames=CONTENT_FRAMES, with_fill=False):
    """Прямоугольник с видимым контентом баннера в пикселях исходника.

    Только для хромакеЙных баннеров: у них кадр шире контента, и без обрезки
    план считает площадь зелёного фона вместо площади баннера. Возвращает
    (x, y, w, h) или None - тогда баннер планируем по всему кадру.

    with_fill=True отдаёт ещё и заполнение объединения самым крупным кадром
    (0..1): у анимированного баннера графика ездит внутри обрезанной области,
    и по одной площади слота нельзя сказать, сколько видно в кадре.

    Результат кэшируется по файлу: plan_banner считается для карточки перед
    рендером и потом ещё раз внутри build_ffmpeg_cmd, а каждый замер - это
    ffmpeg с чтением кадров.
    """
    chroma = chroma or resolve_chroma(config, banner_path)
    if not chroma:
        return None if not with_fill else (None, 1.0)

    path = Path(banner_path)
    try:
        stat = path.stat()
    except OSError:
        return None if not with_fill else (None, 1.0)
    key = (str(path), stat.st_mtime_ns, stat.st_size, chroma["color"])
    if key not in _CONTENT_BOX_CACHE:
        try:
            _CONTENT_BOX_CACHE[key] = _measure_content_box(path, chroma, sample, frames)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            # Не смогли померить - планируем по всему кадру, как раньше.
            _CONTENT_BOX_CACHE[key] = None
    got = _CONTENT_BOX_CACHE[key]
    if not with_fill:
        return got[0] if got else None
    return (got[0], got[1]) if got else (None, 1.0)



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
    (x0, y0, x1, y1) - область, в которую кладётся баннер. При нулевых
    отступах это весь кадр, и тогда safe box совпадает с самим кадром."""
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


def resolve_position(config, position=None):
    """Позиция баннера: выбор юзера, иначе настройка бренда, иначе снизу.

    Поддерживается только POSITIONS. Всё остальное (включая "right" из старых
    конфигов) молча падает на DEFAULT_POSITION, чтобы баннер гарантированно
    не уехал под кнопки интерфейса.
    """
    pos = position or config.get("position") or config.get("anchor") or DEFAULT_POSITION
    return pos if pos in POSITIONS else DEFAULT_POSITION


def position_slot(x0, y0, x1, y1, position):
    """Прямоугольник, внутри которого центрируется баннер.

    Для всех позиций это весь safe box: там сверху, снизу и по центру
    интерфейс ничего не перекрывает, и вертикаль задаётся отдельно в
    plan_overlay. Боковых вариантов нет намеренно - узкая колонка не
    набирает площадь из ТЗ (см. configs/platforms.json).
    """
    return x0, y0, x1, y1


def plan_overlay(platform, video_w, video_h, banner_w, banner_h, config, position=None,
                 content_box=None, content_fill=1.0):
    """Считает финальный размер и координаты баннера.

    content_box - (x, y, w, h) с видимым контентом хромакеЙного баннера в
    пикселях исходника. Когда он задан, планируем размер ИМЕННО контента:
    зелёный фон в кадре не считается, его срезает crop в build_filter. Без
    content_box поведение прежнее - размер считается по всему кадру.

    content_fill - какая доля объединения занята самым крупным кадром
    анимации. Нужна, чтобы показать в плане не только размер слота, но и
    реально видимую площадь: у анимированного баннера они различаются.
    """
    position = resolve_position(config, position)
    x0, y0, x1, y1 = safe_box(platform, video_w, video_h)
    sx0, sy0, sx1, sy1 = position_slot(x0, y0, x1, y1, position)
    slot_w, slot_h = sx1 - sx0, sy1 - sy0

    # Пропорции берём у того, что реально видно.
    if content_box:
        src_w, src_h = content_box[2], content_box[3]
    else:
        src_w, src_h = banner_w, banner_h
    aspect = src_w / src_h if src_h else 1.0

    def even(v):
        """Чётное значение не меньше 2. Баннер масштабируется отдельным
        scale, и нечётный размер там ломает yuv420p так же, как на исходнике."""
        return max(2, int(round(v)) // 2 * 2)

    def fit(width):
        """Размер баннера под заданную ширину, вписанный в зону с сохранением пропорций."""
        w = min(even(width), even(slot_w))
        h = even(w / aspect)
        if h > slot_h:
            h = even(slot_h)
            w = even(h * aspect)
            if w > even(slot_w):
                w = even(slot_w)
                h = even(w / aspect)
        return max(2, w), max(2, h)

    min_area_ratio = float(config.get("min_area_ratio", 0))
    min_area = min_area_ratio * video_w * video_h

    # Потолок по ширине: ширина safe box плюс разрешённый вылет по бокам.
    # При нулевых insets это ровно video_w, и вылет ничего не добавляет.
    bleed_ratio = float(config.get("horizontal_bleed_ratio", 0))
    bleed = int(round(bleed_ratio * video_w))
    max_w = even(min(video_w, slot_w + 2 * bleed))

    def fit_area(area):
        """Наибольший размер с площадью не больше area, вписанный в лимиты."""
        if area <= 0:
            return 2, 2
        w = (area * aspect) ** 0.5
        h = w / aspect
        if w > max_w:
            w = max_w
            h = w / aspect
        if h > slot_h:
            h = even(slot_h)
            w = even(h * aspect)
            if w > max_w:
                w = max_w
                h = even(w / aspect)
        return max(2, even(w)), max(2, even(h))

    # ТЗ по площади. Считать площадь по контенту можно ТОЛЬКО когда мы его
    # знаем: иначе зелёный фон хромакея попал бы в площадь, и мы бы снова
    # получили ТЗ на пустом кадре. Поэтому без content_box работает старая
    # логика - размер по ширине слота.
    use_area = content_box is not None
    target_area_ratio = (float(config.get("target_area_ratio", 0)) or min_area_ratio) \
        if use_area else 0.0
    if target_area_ratio > 0:
        target_w, target_h = fit_area(target_area_ratio * video_w * video_h)
    else:
        target_w, target_h = fit(slot_w * float(config.get("width_ratio", 0.8)))

    if target_w * target_h < min_area:
        bigger = fit_area(min_area) if target_area_ratio > 0 else fit(slot_w)
        if bigger[0] * bigger[1] > target_w * target_h:
            target_w, target_h = bigger

    # Максимум, который вообще можно получить, не вылезая из зоны и не ломая пропорции.
    max_possible = fit_area(float("inf")) if target_area_ratio > 0 else fit(slot_w)

    min_area_met = target_w * target_h >= min_area
    gap = int(round(slot_h * float(config.get("gap_ratio", 0.06))))

    if target_w <= slot_w:
        pos_x = sx0 + (slot_w - target_w) // 2
    else:
        # Баннер шире safe box - центрируем по кадру, иначе он уедет влево.
        pos_x = (video_w - target_w) // 2
        # Центрировка по кадру не уважает разрешённый вылет, если safe box
        # несимметричный (у shorts отступы 48/192 - при центрировке правый
        # вылет получался 129px при разрешённых 108). Поэтому сдвигаем баннер
        # внутрь окна [x0 - bleed, x1 + bleed], не давая вылету превысить
        # разрешённый ни с одной стороны.
        lo, hi = x0 - bleed, x1 + bleed - target_w
        if lo <= hi:
            pos_x = max(lo, min(pos_x, hi))
        else:
            # Вылет не покрывает даже ширину баннера - прижимаем к левому краю.
            pos_x = x0

    if position == "top":
        pos_y = sy0 + gap
    elif position == "center":
        pos_y = sy0 + (slot_h - target_h) // 2
    else:  # bottom
        pos_y = sy1 - target_h - gap

    # Баннер не вылезает ни по вертикали, ни по горизонтали за кадр.
    pos_x = max(0, min(pos_x, video_w - target_w))
    pos_y = max(y0, min(pos_y, y1 - target_h))

    area = target_w * target_h
    return {
        "pos": (pos_x, pos_y),
        "size": (target_w, target_h),
        "safe_box": (x0, y0, x1, y1),
        "slot": (sx0, sy0, sx1, sy1),
        "position": position,
        "content_box": content_box,
        "content_size": (src_w, src_h),
        # Насколько контент растянули. Больше 1.5 - уже видно мыло: исходник
        # мелкий, и правильное решение - отдать баннер покрупнее.
        "upscale": round(target_w / src_w, 2) if src_w else 1.0,
        "area_ratio": round(area / (video_w * video_h), 4),
        # Сколько графики реально видно в самом крупном кадре анимации. Для
        # неподвижного баннера совпадает с area_ratio, для анимированного
        # меньше: контент ездит внутри отведённого слота.
        "visible_area_ratio": round(area / (video_w * video_h) * content_fill, 4),
        "content_fill": round(content_fill, 3),
        "min_area_ratio": round(min_area / (video_w * video_h), 4) if min_area else 0,
        "min_area_met": area >= min_area,
        "max_possible_area_ratio": round(max_possible[0] * max_possible[1]
                                         / (video_w * video_h), 4),
    }


def plan_banner(platform, banner_path, config, video_w=REF_W, video_h=REF_H, position=None):
    """Считает план раскладки баннера, ничего не рендеря.

    Нужно, чтобы показать пользователю размер и предупредить про площадь
    ДО того, как он отправит видео в рендер на пару минут.

    video_w/video_h - размер ИСХОДНИКА. Если он выше потолка, раскладка
    считается для ужатого кадра - ровно так же, как это делает build_filter.
    """
    banner_path = Path(banner_path)
    banner_w, banner_h, _ = probe(banner_path)
    content_box, content_fill = detect_content_box(banner_path, config, with_fill=True)

    out_w, out_h = out_size(video_w, video_h)
    plan = plan_overlay(platform, out_w, out_h, banner_w, banner_h, config, position,
                        content_box, content_fill)
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


def build_filter(platform, video_w, video_h, banner_w, banner_h, config, chroma, duration,
                 position=None, content_box=None, content_fill=1.0):
    out_w, out_h = out_size(video_w, video_h)

    # Сначала ужимаем исходник до потолка, и уже на ужатом кадре считаем
    # раскладку баннера: safe box и координаты должны быть в координатах
    # выходного кадра, иначе баннер уедет.
    plan = plan_overlay(platform, out_w, out_h, banner_w, banner_h, config, position,
                        content_box, content_fill)
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

    # Обрезка хромакея по контенту ДО scale: зелёные поля не масштабируются
    # (дешевле и не мылит край), а плановый размер достаётся видимому
    # контенту, а не пустому фону.
    if content_box:
        cx, cy, cw, ch = content_box
        chains.append(
            f"[1:v]crop={cw}:{ch}:{cx}:{cy},"
            f"scale={bw}:{bh}:force_original_aspect_ratio=decrease[banner_raw]"
        )
    else:
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


def build_ffmpeg_cmd(input_vid, banner_vid, out_vid, platform, brand, duration=0.0,
                     position=None):
    config = load_brand(brand)
    vw, vh, _ = probe(input_vid)
    bw, bh, _ = probe(banner_vid)
    ext = Path(banner_vid).suffix.lstrip(".").lower()
    chroma = resolve_chroma(config, banner_vid)
    content_box, content_fill = detect_content_box(banner_vid, config, chroma, with_fill=True)

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

    filters, plan = build_filter(platform, vw, vh, bw, bh, config, chroma, duration,
                                 position, content_box, content_fill)
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


def render(input_vid, banner_vid, out_vid, platform, brand, position=None):
    src_w, src_h, duration = probe(input_vid)

    # Крупный кадр ужимаем отдельным проходом: колорокей и overlay поверх
    # 2160x3840 в одном процессе не помещаются в 512 МБ Render.
    if src_w > MAX_OUT_W or src_h > MAX_OUT_H:
        with tempfile.TemporaryDirectory(prefix="prepass_") as tmp:
            mid = Path(tmp) / "mid.mp4"
            _run_ffmpeg(build_prepass_cmd(input_vid, mid, duration))
            cmd, plan = build_ffmpeg_cmd(mid, banner_vid, out_vid, platform, brand,
                                         duration, position)
            # Плану показываем исходник, а не промежуточный файл.
            plan["src_size"] = (src_w, src_h)
            plan["prepassed"] = True
            _run_ffmpeg(cmd)
        return plan

    cmd, plan = build_ffmpeg_cmd(input_vid, banner_vid, out_vid, platform, brand,
                                 duration, position)
    plan["prepassed"] = False
    _run_ffmpeg(cmd)
    return plan
