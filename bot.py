import os
import asyncio
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


class VideoState(StatesGroup):
    chosen_platform = State()
    chosen_brand = State()
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
        f"📱 Формат: <b>{label}</b>\nВыбери бренд:", reply_markup=get_brands_keyboard(), parse_mode="HTML"
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
    await state.set_state(VideoState.waiting_for_video)

    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    rules = []
    if config.get("chroma_key"):
        rules.append("🟢 <i>Хромакей: бот сам вырежет фон из баннера.</i>")
    rules.append(f"📏 <i>Ширина: {int(config.get('width_ratio', 0.8) * 100)}% безопасной зоны, зациклен на всё видео.</i>")

    per_platform = config.get("media_by_platform") or {}
    expected = per_platform.get(platform) or config.get("media") or "banner.mp4"
    if not render.find_banner(brand, config, platform=platform):
        rules.append(
            f"\n🚨 <b>НЕТ ФАЙЛА БАННЕРА</b> — положи <code>{expected}</code> "
            f"в папку <code>banners/{brand}/</code> на сервере."
        )
    elif expected in per_platform:
        rules.append(f"\n🎬 <i>Баннер: <code>{expected}</code></i>")

    await callback.message.edit_text(
        f"✅ Готов делать <b>{label}</b>! Кидай видео (до {MAX_DURATION} сек).\n" + "\n".join(rules),
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
    if not banner_path:
        expected = (config.get("media_by_platform") or {}).get(platform) \
            or config.get("media") or "banner.mp4"
        return await message.answer(
            f"❌ Файл баннера для <b>{brand}</b> на {platform.upper()} не найден.\n"
            f"Ожидается: <code>banners/{brand}/{expected}</code>",
            parse_mode="HTML",
        )

    label = render.BRAND_LABELS.get(brand, brand.upper())
    msg = await message.answer(f"⏳ Рендер <b>{label}</b> на {platform.upper()}...")

    file_id = message.video.file_id
    in_vid = BASE / "videos" / f"in_{file_id}.mp4"
    out_vid = BASE / "videos" / f"out_{file_id}.mp4"

    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=in_vid)

        plan = await asyncio.to_thread(render.render, in_vid, banner_path, out_vid, platform, brand)

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
        await msg.edit_text("🚀 Загружаю результат...")
        await message.answer_video(
            video=FSInputFile(out_vid),
            caption=(
                f"Трафик готов! ({label} · {platform.upper()})\n"
                f"Баннер {bw}×{bh} @ ({pos_x},{pos_y}) — {plan['area_ratio'] * 100:.1f}% экрана"
                + warn
            ),
        )
    except Exception as e:
        await message.answer("❌ Ошибка обработки. Подробности в логе сервера.")
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
    await message.answer("Ещё?", reply_markup=get_platforms_keyboard())


async def main():
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
