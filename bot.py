import os
import asyncio
import threading
from pathlib import Path

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import render

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@xSp1der42"
CHANNEL_URL = "https://t.me/xSp1der42"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0")) or None

BASE = Path(__file__).resolve().parent
os.chdir(BASE)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MAX_DURATION = 59

# Рендер ОДИН за раз. Замеры: ролик 1080x1920@60 на insta ест 415 МБ из 512 МБ
# контейнера. Два параллельных ffmpeg - это 800 МБ, и Render убьёт контейнер
# с OOM, уронив рендеры обоих. Второй и последующие ждут в очереди.
render_lock = asyncio.Lock()


class VideoState(StatesGroup):
    chosen_platform = State()
    chosen_brand = State()
    chosen_position = State()
    waiting_for_video = State()


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


def get_sub_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])


def get_platforms_keyboard():
    builder = InlineKeyboardBuilder()
    for key in render.list_platforms():
        label = render.PLATFORM_LABELS.get(key, key.upper())
        icon = {"tiktok": "🎵", "insta": "📸", "shorts": "📺"}.get(key, "🎬")
        builder.button(text=f"{icon} {label}", callback_data=f"plat_{key}")
    builder.adjust(1)
    return builder.as_markup()


def brand_button_text(brand: str, platform: str | None = None) -> str:
    config = render.load_brand(brand)
    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    marks = []
    if config.get("chroma_key"):
        marks.append("🟢хромакей")
    marks.append(f"{int(config.get('width_ratio', 0.8) * 100)}% ширины")
    if not render.find_banner(brand, config, platform=platform):
        marks.append("❌ нет файла")
    return f"{label} · {' · '.join(marks)}"


POSITION_HINT = (
    "<i>Баннер идёт с хромакеем, зелёный фон срезается, и графика ложится "
    "поверх ролика. Позиция привязана к краю кадра.</i>"
)


def get_brands_keyboard(platform: str | None = None):
    builder = InlineKeyboardBuilder()
    for brand in render.list_brands():
        builder.button(
            text=brand_button_text(brand, platform=platform),
            callback_data=f"brand_{brand}",
        )
    builder.button(text="🔙 Назад", callback_data="back_to_platforms")
    builder.adjust(1, 1)
    return builder.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not await is_subscribed(message.from_user.id):
        await message.answer("👋 Подпишись на канал!", reply_markup=get_sub_keyboard())
        return
    await message.answer("📱 Выбери формат/платформу:", reply_markup=get_platforms_keyboard())


@dp.callback_query(F.data == "check_sub")
async def process_sub_check(callback: CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ Доступ открыт.\n\n📱 Выбери платформу:", reply_markup=get_platforms_keyboard()
        )
    else:
        await callback.answer("❌ Ты ещё не подписался!", show_alert=True)


@dp.callback_query(F.data == "back_to_platforms")
async def back_to_platforms(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "📱 Выбери формат/платформу:", reply_markup=get_platforms_keyboard()
    )


@dp.callback_query(F.data.startswith("plat_"))
async def process_platform(callback: CallbackQuery, state: FSMContext):
    if not await is_subscribed(callback.from_user.id):
        return
    platform = callback.data.split("_", 1)[1]
    if platform not in render.list_platforms():
        await callback.answer("❌ Неизвестная платформа", show_alert=True)
        return
    await state.update_data(chosen_platform=platform)
    await state.set_state(VideoState.chosen_brand)
    label = render.PLATFORM_LABELS.get(platform, platform.upper())
    await callback.message.edit_text(
        f"✅ Формат: <b>{label}</b>\n\n"
        f"Ролик должен быть <b>вертикальным</b> (9:16, обычно 1080×1920).\n"
        f"Теперь выбери бренд:",
        reply_markup=get_brands_keyboard(platform=platform),
        parse_mode="HTML",
    )


def get_positions_keyboard():
    builder = InlineKeyboardBuilder()
    for position in render.POSITIONS:
        builder.button(
            text=render.POSITION_LABELS[position],
            callback_data=f"pos_{position}",
        )
    builder.button(text="🔙 Назад", callback_data="back_to_brands")
    builder.adjust(2, 2)
    return builder.as_markup()


async def send_ready_message(message: Message, platform: str, brand: str, position: str):
    """Финальная карточка перед отправкой ролика: что выбрано, куда ляжет
    баннер и сколько он занимает. Реклама обещаний не даём - только цифры."""
    config = render.load_brand(brand)
    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    platform_label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())

    per_platform = config.get("media_by_platform") or {}
    expected = per_platform.get(platform) or config.get("media") or "banner.mp4"
    banner_path = render.find_banner(brand, config, platform=platform)

    # Считаем раскладку заранее, чтобы предупредить про площадь сразу,
    # а не через 3 минуты ожидания рендера.
    plan = None
    try:
        plan = render.plan_banner(platform, banner_path, config, position=position)
    except Exception:
        pass

    bw, bh = plan["size"] if plan else (0, 0)
    lines = [
        "🟢 <b>Готово. Теперь скинь видео.</b>",
        "",
        f"Формат: <b>{platform_label}</b>",
        f"Бренд: <b>{label}</b>",
        f"Баннер: <code>{banner_path.name}</code> · {render.POSITION_LABELS[position]}",
        "",
        "📎 <b>Прикрепи ролик файлом</b> или запиши кружочек — "
        "дальше всё сделает бот сам.",
        "",
        "<i>Требования к ролику:</i>",
        "• вертикальный, 9:16 (лучше всего 1080×1920)",
        f"• до {MAX_DURATION} секунд",
    ]
    if plan:
        # Показываем и область баннера, и то, сколько графики реально видно:
        # у анимированного баннера контент ездит внутри отведённой области, и
        # без второй цифры пользователь снова увидит «мелкий баннер».
        fill = plan.get("content_fill", 1.0)
        lines.append(f"• баннер ляжет в безопасную зону, {bw}×{bh} "
                     f"({plan['area_ratio'] * 100:.0f}% экрана)")
        if fill < 0.95:
            lines.append(f"• графика анимирована: видно от "
                         f"{plan['visible_area_ratio'] * 100:.0f}% экрана в момент, "
                         f"когда она развернулась полностью, и меньше, когда сжата")

    if plan and not plan["min_area_met"]:
        lines += [
            "",
            f"⚠️ <i>Баннер займёт {plan['area_ratio'] * 100:.0f}% экрана, "
            f"ТЗ требует {plan['min_area_ratio'] * 100:.0f}% — пропорции баннера "
            f"слишком вытянутые. Рендер всё равно пойдёт, но площадь недобор.</i>",
        ]

    await message.edit_text("\n".join(lines), parse_mode="HTML")


@dp.callback_query(F.data.startswith("pos_"))
async def process_position(callback: CallbackQuery, state: FSMContext):
    if not await is_subscribed(callback.from_user.id):
        return
    position = callback.data.split("_", 1)[1]
    if position not in render.POSITIONS:
        await callback.answer("❌ Неизвестная позиция", show_alert=True)
        return

    data = await state.get_data()
    platform = data.get("chosen_platform")
    brand = data.get("chosen_brand")
    if not platform or not brand:
        await state.clear()
        return await callback.message.edit_text(
            "❌ Сначала выбери платформу и бренд.",
            reply_markup=get_platforms_keyboard(),
        )

    await state.update_data(chosen_position=position)
    await state.set_state(VideoState.waiting_for_video)
    await send_ready_message(callback.message, platform, brand, position)


@dp.callback_query(F.data == "back_to_brands")
async def back_to_brands(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    platform = data.get("chosen_platform")
    await state.set_state(VideoState.chosen_brand)
    label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())
    await callback.message.edit_text(
        f"✅ Формат: <b>{label}</b>\n\nВыбери бренд:",
        reply_markup=get_brands_keyboard(platform=platform),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("brand_"))
async def process_brand(callback: CallbackQuery, state: FSMContext):
    if not await is_subscribed(callback.from_user.id):
        return
    brand = callback.data.split("_", 1)[1]
    try:
        config = render.load_brand(brand)
    except FileNotFoundError:
        await callback.answer("❌ Нет конфига бренда", show_alert=True)
        return

    await state.update_data(chosen_brand=brand)
    await state.set_state(VideoState.chosen_position)

    data = await state.get_data()
    platform = data.get("chosen_platform")

    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    platform_label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())

    per_platform = config.get("media_by_platform") or {}
    expected = per_platform.get(platform) or config.get("media") or "banner.mp4"
    banner_path = render.find_banner(brand, config, platform=platform)

    if not banner_path:
        return await callback.message.edit_text(
            f"❌ <b>Баннер не найден на сервере</b>\n\n"
            f"Формат: <b>{platform_label}</b>\n"
            f"Бренд: <b>{label}</b>\n\n"
            f"Ожидается <code>banners/{brand}/{expected}</code> — "
            f"а такого файла нет. Напиши разработчику, поправим.",
            parse_mode="HTML",
        )

    await callback.message.edit_text(
        f"✅ Формат: <b>{platform_label}</b>\n"
        f"Бренд: <b>{label}</b>\n\n"
        f"Где поставить баннер?\n\n"
        f"{POSITION_HINT}",
        reply_markup=get_positions_keyboard(),
        parse_mode="HTML",
    )


@dp.message(VideoState.waiting_for_video, F.video)
async def process_video(message: Message, state: FSMContext):
    duration = message.video.duration or 0
    if duration > MAX_DURATION:
        return await message.answer(f"❌ Видео длиннее {MAX_DURATION} секунд ({duration} сек).")

    data = await state.get_data()
    platform = data.get("chosen_platform")
    brand = data.get("chosen_brand")

    if not platform or not brand:
        await state.clear()
        return await message.answer("❌ Сначала выбери платформу и бренд.", reply_markup=get_platforms_keyboard())

    config = render.load_brand(brand)
    banner_path = render.find_banner(brand, config, platform=platform)
    # Позицию нормализуем здесь: в state могли прийти мусорные данные, а
    # resolve_position гарантирует одну из POSITIONS и не даёт баннеру уехать
    # под кнопки интерфейса.
    position = render.resolve_position(config, data.get("chosen_position"))

    # Отсекаем горизонтальные ролики: safe zones заданы под вертикальный 9:16,
    # на альбомном кадре баннер уедет в неправильное место.
    if message.video.width and message.video.height and message.video.height <= message.video.width:
        return await message.answer(
            f"❌ Это <b>горизонтальное</b> видео ({message.video.width}×{message.video.height}).\n\n"
            f"Нужен вертикальный ролик 9:16 — обычно 1080×1920. "
            f"Возьми исходник вертикальной съёмки, а не обрезанный/повёрнутый ролик из ленты.",
            parse_mode="HTML",
        )

    # Кадр крупнее 4 Мп отклоняем: ужатие идёт отдельным проходом, и на 4K
    # он сам по себе ест 446 МБ из 512 - запас в 13%. Всё, что меньше,
    # бот ужимает сам, включая 1440x2560 и любые iPhone-разрешения.
    vw_in = message.video.width or 0
    vh_in = message.video.height or 0
    if vw_in * vh_in > render.MAX_IN_PIXELS:
        return await message.answer(
            f"❌ Слишком большое разрешение: <b>{vw_in}×{vh_in}</b>.\n\n"
            f"Бот сам ужмёт ролик до {render.MAX_OUT_W}×{render.MAX_OUT_H}, "
            f"но на хостинге помещается кадр не крупнее 4 Мп — "
            f"на {vw_in * vh_in / 1_000_000:.1f} Мп памяти не хватит.\n\n"
            f"📱 Как пережать на телефоне: открой ролик → <b>Поделиться</b> → "
            f"<b>Сохранить видео</b> — iPhone сам отдаёт 1080p.",
            parse_mode="HTML",
        )

    if not banner_path:
        expected = (config.get("media_by_platform") or {}).get(platform) \
            or config.get("media") or "banner.mp4"
        return await message.answer(
            f"❌ Файл баннера для <b>{brand}</b> на {platform.upper()} не найден.\n"
            f"Ожидается: <code>banners/{brand}/{expected}</code>",
            parse_mode="HTML",
        )

    label = render.BRAND_LABELS.get(brand, brand.upper())
    platform_label = render.PLATFORM_LABELS.get(platform, platform.upper())
    position_label = render.POSITION_LABELS[position]
    msg = await message.answer(
        f"⏳ Скачиваю ролик и рендерю <b>{label}</b> на {platform_label} "
        f"({position_label})...",
        parse_mode="HTML",
    )

    file_id = message.video.file_id
    in_vid = BASE / "videos" / f"in_{file_id}.mp4"
    out_vid = BASE / "videos" / f"out_{file_id}.mp4"

    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=in_vid)

        vw, vh, _ = render.probe(in_vid)
        ow, oh = render.out_size(vw, vh)
        platform_label = render.PLATFORM_LABELS.get(platform, platform.upper())

        async def keep_alive():
            """Пока ffmpeg работает в отдельном потоке, трогаем сообщение:
            так юзер видит, что бот жив, а не завис намертво."""
            started = asyncio.get_running_loop().time()
            while True:
                await asyncio.sleep(20)
                spent = int(asyncio.get_running_loop().time() - started)
                extra = ""
                if (ow, oh) != (vw, vh):
                    extra = f" Исходник {vw}×{vh} ужимается до {ow}×{oh}, чтобы влезать в память."
                try:
                    await msg.edit_text(
                        f"⏳ <b>Рендер идёт</b> · {label} · {platform_label}\n"
                        f"<i>Прошло {spent} сек.{extra}</i>"
                    )
                except Exception:
                    pass

        # Рендер строго по одному: два параллельных ffmpeg на 512 МБ не
        # помещаются. Если рендер уже идёт - честно говорим про очередь.
        if render_lock.locked():
            await msg.edit_text(
                "⏳ <b>Рендер занят</b> — предыдущий ролик ещё собирается.\n"
                "<i>Твой встанет в очередь, начну сразу, как освободится.</i>",
                parse_mode="HTML",
            )

        async with render_lock:
            ticker = asyncio.create_task(keep_alive())
            try:
                plan = await asyncio.to_thread(
                    render.render, in_vid, banner_path, out_vid, platform, brand, position
                )
            finally:
                ticker.cancel()

        pos_x, pos_y = plan["pos"]
        bw, bh = plan["size"]
        warn = ""
        if not plan["min_area_met"]:
            warn = (
                f"\n⚠️ Баннер занимает {plan['area_ratio'] * 100:.1f}% экрана, "
                f"ТЗ требует {plan['min_area_ratio'] * 100:.0f}%. При таких пропорциях "
                f"баннера это физически недостижимо без выхода за безопасную зону - "
                f"используй баннер покрупнее."
            )
        anim = ""
        if plan.get("content_fill", 1.0) < 0.95:
            anim = (
                f"\nГрафика анимирована: полностью развернутая видна на "
                f"{plan['visible_area_ratio'] * 100:.0f}% экрана, в сжатых кадрах — меньше."
            )
        await msg.edit_text("🚀 Загружаю результат...")
        await message.answer_video(
            video=FSInputFile(out_vid),
            caption=(
                f"Готово ({label} · {platform_label} · {position_label})\n"
                f"Баннер {bw}×{bh} — {plan['area_ratio'] * 100:.0f}% экрана"
                + anim
                + warn
            ),
        )
    except Exception as e:
        await message.answer(
            f"❌ <b>Не получилось сделать ролик.</b>\n\n"
            f"Причина на сервере: <code>{type(e).__name__}</code>\n"
            f"Если это повторяется — скинь это сообщение разработчику.",
            parse_mode="HTML",
        )
        print(f"[ERROR] {type(e).__name__}: {e}")
    finally:
        for p in (in_vid, out_vid):
            if p.exists():
                p.unlink()
        try:
            await msg.delete()
        except Exception:
            pass

    await state.clear()
    await message.answer("Ещё ролик?", reply_markup=get_platforms_keyboard())


def start_health_server(port: int) -> bool:
    """Поднимаем минимальный HTTP-ответчик, чтобы процесс держал порт.

    Render запускает сервис как web service и после старта сканирует порты.
    Бот работает на long polling и сам по себе не слушает ничего, поэтому деплой
    доходил до "No open ports detected" и помечался как неудачный, хотя бот к
    этому моменту уже работал.

    Заодно это готовая точка для внешнего мониторинга: на фри-хостинге инстанс
    засыпает после простоя, и периодический пинг по /healthz его будит.

    Порт берём из PORT - Render задаёт его сам, локально можно задать руками.
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            # Пинг мониторинга не должен сыпать лог рендера.
            pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        # Бот должен работать и без health-сервера: это подсказка, а не причина
        # не подниматься. Если порт занят - предупреждаем и едем дальше.
        print(f"[WARN] порт {port} занят, health-сервер не поднят: {e}")
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Health-сервер слушает порт {port} (/healthz)")
    return True


async def main():
    port = int(os.getenv("PORT") or 8000)
    await asyncio.to_thread(start_health_server, port)

    brands = render.list_brands()
    platforms = render.list_platforms()
    print(f"Бот запущен. Платформы: {platforms}")
    for brand in brands:
        config = render.load_brand(brand)
        per_platform = config.get("media_by_platform") or {}
        for platform in platforms:
            found = render.find_banner(brand, config, platform=platform)
            state = f"OK {found.name}" if found else "НЕТ ФАЙЛА БАННЕРА"
            marker = "" if platform not in per_platform else " *"
            print(f"  {brand}/{platform}{marker}: {state}")
        if not per_platform and not render.find_banner(brand, config):
            print(f"  {brand}: НЕТ ФАЙЛА БАННЕРА")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
