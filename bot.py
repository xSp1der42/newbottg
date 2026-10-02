import os
import html
import re
import socket
import asyncio
import hashlib
import ipaddress
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
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

# Жёсткие лимиты Bot API: getFile отдаёт не больше 20 МБ, sendVideo принимает
# не больше 50 МБ. Это ограничение самого Telegram, обойти его нельзя, поэтому
# проверяем заранее и говорим юзеру понятную причину, а не сырой TelegramBadRequest.
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TG_UPLOAD_LIMIT = 50 * 1024 * 1024

# Приём ролика по ссылке. Нужен потому, что 20 МБ - это потолок Telegram, и
# файл крупнее бот в чат не получит никак. По URL он качает сам, и там
# ограничение только наш: диск и память хостинга.
LINK_DOWNLOAD_LIMIT = 512 * 1024 * 1024
LINK_CHUNK = 1 << 20
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
GDRIVE_API = "https://drive.usercontent.google.com/download"
YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"

# Рендер ОДИН за раз. Замеры: ролик 1080x1920@60 на insta ест 415 МБ из 512 МБ
# контейнера. Два параллельных ffmpeg - это 800 МБ, и Render убьёт контейнер
# с OOM, уронив рендеры обоих. Второй и последующие ждут в очереди.
render_lock = asyncio.Lock()


async def safe_edit(message: Message, text: str, **kwargs):
    """edit_text, который не падает, если текст не изменился.

    Telegram отвечает "message is not modified", когда новое содержимое
    совпадает с текущим. Для нас это не ошибка: сообщение уже в нужном виде.
    Случай частый - двойной тап по кнопке или повторная доставка callback.
    Любые другие ошибки редактирования прокидываем наверх.
    """
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return None
        raise


class VideoState(StatesGroup):
    chosen_platform = State()
    chosen_brand = State()
    chosen_variant = State()
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
    variants = render.list_variants(brand, config)
    if len(variants) > 1:
        marks.append(f"{len(variants)} варианта")
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
        await safe_edit(callback.message,
            "✅ Доступ открыт.\n\n📱 Выбери платформу:", reply_markup=get_platforms_keyboard()
        )
    else:
        await callback.answer("❌ Ты ещё не подписался!", show_alert=True)


@dp.callback_query(F.data == "back_to_platforms")
async def back_to_platforms(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message,
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
    await safe_edit(callback.message,
        f"✅ Формат: <b>{label}</b>\n\n"
        f"Ролик должен быть <b>вертикальным</b> (9:16, обычно 1080×1920).\n"
        f"Теперь выбери бренд:",
        reply_markup=get_brands_keyboard(platform=platform),
        parse_mode="HTML",
    )


def get_variants_keyboard(brand: str):
    """Клавиатура выбора варианта баннера внутри бренда."""
    config = render.load_brand(brand)
    builder = InlineKeyboardBuilder()
    for v in render.list_variants(brand, config):
        # Имя файла в callback_data: оно короткое, латиницей и не ломает парсинг.
        builder.button(text=v["title"], callback_data=f"var_{v['file']}")
    builder.button(text="🔙 Назад", callback_data="back_to_brands")
    builder.adjust(1, 1)
    return builder.as_markup()


def get_positions_keyboard(brand: str | None = None):
    builder = InlineKeyboardBuilder()
    for position in render.POSITIONS:
        builder.button(
            text=render.POSITION_LABELS[position],
            callback_data=f"pos_{position}",
        )
    # Назад ведёт на выбор варианта, если у бренда он есть, иначе - на бренды.
    back = "back_to_variants" if brand and render.list_variants(
        brand, render.load_brand(brand)) else "back_to_brands"
    builder.button(text="🔙 Назад", callback_data=back)
    builder.adjust(2, 2)
    return builder.as_markup()


async def finish_setup(message: Message, state: FSMContext, platform: str, brand: str,
                       variant: str | None = None):
    """Доводит выбор до конца и уводит в ожидание ролика.

    Вопрос про позицию задаётся не всегда: у бренда с однократной вставкой и
    insertion.position = "fixed" выбор всё равно игнорируется, и спрашивать
    юзера про то, что потом не применяется, - враньё в интерфейсе. Такой бренд
    сразу спрашивает ролик. С insertion.position = "user" (Musor Drop)
    позиция выбирается как обычно.
    """
    config = render.load_brand(brand)
    insertion = render.resolve_insertion(config)
    if insertion and insertion.get("position") != "user":
        position = render.resolve_position(config, None)
        await state.update_data(chosen_position=position, chosen_variant=variant)
        await state.set_state(VideoState.waiting_for_video)
        return await send_ready_message(message, platform, brand, position, variant)

    await state.update_data(chosen_variant=variant)
    await state.set_state(VideoState.chosen_position)
    return await safe_edit(message,
        f"✅ Формат: <b>{render.PLATFORM_LABELS.get(platform, (platform or '?').upper())}</b>\n"
        f"Бренд: <b>{render.BRAND_LABELS.get(brand, config.get('title', brand))}</b>\n\n"
        f"Где поставить баннер?\n\n"
        f"{POSITION_HINT}",
        reply_markup=get_positions_keyboard(brand),
        parse_mode="HTML",
    )


async def send_ready_message(message: Message, platform: str, brand: str, position: str,
                              variant: str | None = None):
    """Финальная карточка перед отправкой ролика: что выбрано, куда ляжет
    баннер и сколько он занимает. Реклама обещаний не даём - только цифры."""
    config = render.load_brand(brand)
    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    platform_label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())

    banner_path = render.find_banner(brand, config, platform=platform, variant=variant)

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
        "🔗 <b>Ролик крупнее 20 МБ — пришли ссылкой</b>, и я скачаю его сам: "
        "прямая ссылка на видео, Google Drive или Яндекс.Диск. Файл в Telegram "
        "больше 20 МБ бот не получает в принципе.",
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

    # Требования к длине при однократной вставке: баннер занимает середину
    # ролика целиком, поэтому слишком короткий ролик вставка съедает целиком.
    ins = (plan.get("insertion") or plan.get("insertion_required")) if plan else None
    if ins:
        # Ролик ещё не загружен, поэтому конкретное окно вставки ещё неизвестно -
        # сообщаем правило и порог, а секунды скажем после загрузки ролика.
        min_total = ins.get("min_total") or render.min_total_duration(
            ins["banner_duration"], render.resolve_insertion(config))
        lines.append(f"• <b>не короче {min_total:.0f} секунд</b> — баннер идёт "
                     f"в середине ролика и занимает {ins['banner_duration']:.0f} сек")
        lines += [
            "",
            f"🎞 Баннер покажется <b>один раз в середине</b>: ролик на "
            f"{ins['banner_duration']:.0f} сек замрёт, баннер отыграет, "
            f"ролик пойдёт дальше с того же места. Длина не изменится.",
        ]
    if plan and plan.get("audio_warning") == "no_banner_audio":
        lines += [
            "",
            "🔇 <i>У этого баннера нет звуковой дорожки — во время вставки "
            "продолжится звук исходного ролика.</i>",
        ]

    await safe_edit(message, "\n".join(lines), parse_mode="HTML")


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
    variant = data.get("chosen_variant")
    if not platform or not brand:
        await state.clear()
        return await safe_edit(callback.message,
            "❌ Сначала выбери платформу и бренд.",
            reply_markup=get_platforms_keyboard(),
        )

    await state.update_data(chosen_position=position)
    await state.set_state(VideoState.waiting_for_video)
    await send_ready_message(callback.message, platform, brand, position, variant)


@dp.callback_query(F.data.startswith("var_"))
async def process_variant(callback: CallbackQuery, state: FSMContext):
    """Выбор варианта баннера внутри бренда: у FunPay это игры или сервисы."""
    if not await is_subscribed(callback.from_user.id):
        return
    variant = callback.data.split("_", 1)[1]

    data = await state.get_data()
    platform = data.get("chosen_platform")
    brand = data.get("chosen_brand")
    if not platform or not brand:
        await state.clear()
        return await safe_edit(callback.message,
            "❌ Сначала выбери платформу и бренд.",
            reply_markup=get_platforms_keyboard(),
        )

    config = render.load_brand(brand)
    # Файл приходит из callback_data, поэтому проверяем его по списку
    # вариантов: иначе в рендер уехал бы любой путь из сообщения.
    files = {v["file"] for v in render.list_variants(brand, config)}
    if variant not in files:
        await callback.answer("❌ Неизвестный вариант", show_alert=True)
        return

    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    platform_label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())

    await state.update_data(chosen_variant=variant)
    # Дальше - вопрос про позицию или сразу ролик, решает finish_setup: у
    # однократной вставки с фиксированной позицией вопроса нет.
    return await finish_setup(callback.message, state, platform, brand, variant)


@dp.callback_query(F.data == "back_to_variants")
async def back_to_variants(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    brand = data.get("chosen_brand")
    if not brand:
        return await back_to_brands(callback, state)
    await state.set_state(VideoState.chosen_variant)
    await safe_edit(callback.message,
        "Какой баннер?",
        reply_markup=get_variants_keyboard(brand),
    )


@dp.callback_query(F.data == "back_to_brands")
async def back_to_brands(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    platform = data.get("chosen_platform")
    await state.set_state(VideoState.chosen_brand)
    # Вариант сбрасываем: у другого бренда его может не быть вовсе.
    await state.update_data(chosen_variant=None)
    label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())
    await safe_edit(callback.message,
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

    data = await state.get_data()
    platform = data.get("chosen_platform")

    label = render.BRAND_LABELS.get(brand, config.get("title", brand))
    platform_label = render.PLATFORM_LABELS.get(platform, (platform or "?").upper())

    # Бренд с вариантами (у FunPay это игры и сервисы) сначала спрашивает, какой
    # ролик показать, и только потом - куда его поставить.
    if render.list_variants(brand, config):
        await state.set_state(VideoState.chosen_variant)
        return await safe_edit(callback.message,
            f"✅ Формат: <b>{platform_label}</b>\n"
            f"Бренд: <b>{label}</b>\n\n"
            f"Какой баннер?",
            reply_markup=get_variants_keyboard(brand),
            parse_mode="HTML",
        )

    await state.set_state(VideoState.chosen_position)
    banner_path = render.find_banner(brand, config, platform=platform)

    if not banner_path:
        expected = (config.get("media_by_platform") or {}).get(platform) \
            or config.get("media") or "banner.mp4"
        return await safe_edit(callback.message,
            f"❌ <b>Баннер не найден на сервере</b>\n\n"
            f"Формат: <b>{platform_label}</b>\n"
            f"Бренд: <b>{label}</b>\n\n"
            f"Ожидается <code>banners/{brand}/{expected}</code> — "
            f"а такого файла нет. Напиши разработчику, поправим.",
            parse_mode="HTML",
        )

    # Дальше - вопрос про позицию или сразу ролик, решает finish_setup.
    return await finish_setup(callback.message, state, platform, brand)


class LinkError(Exception):
    """Ошибка скачивания по ссылке. Текст можно показывать юзеру как есть."""


# Бот качает то, что прислал юзер, поэтому запросы во внутреннюю сеть хоста
# закрываем: иначе ссылкой вида http://127.0.0.1:PORT можно было бы стянуть
# метаданные Render или залезть в соседний контейнер.
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4", "::1/128",
    "fc00::/7", "fe80::/10",
))


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not any(ip in net for net in PRIVATE_NETS)


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """Резолвер aiohttp, отбрасывающий непубличные адреса.

    Проверка именно на этапе resolve, а не до запроса: иначе один и тот же домен
    может снаружи отдавать публичный IP, а боту внутри - 127.0.0.1.
    """

    def __init__(self):
        self._base = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await self._base.resolve(host, port, family)
        for info in infos:
            if not _is_public(info["host"]):
                raise LinkError(
                    f"Ссылка ведёт на внутренний адрес ({info['host']}). "
                    f"Бот качает только из открытого интернета."
                )
        return infos

    async def close(self):
        await self._base.close()


URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    """Достаёт первый http(s)-адрес из текста. Хвостовую пунктуацию отрезаем:
    иначе ссылка, законченная точкой или скобкой, не откроется."""
    m = URL_RE.search(text or "")
    return m.group(0).rstrip('.,;:!?)]}>"\'') if m else None


async def _pump(resp, dest: Path, progress=None) -> int:
    """Сливает открытый HTTP-ответ в файл, упираясь в LINK_DOWNLOAD_LIMIT."""
    if resp.status >= 400:
        raise LinkError(
            f"Сервер ответил {resp.status}. Проверь, что ссылка живая и файл открыт."
        )
    if "text/html" in (resp.headers.get("Content-Type") or "").lower():
        raise LinkError(
            "По ссылке пришла веб-страница, а не файл. Нужен адрес, который сразу "
            "отдаёт видео, или публичная ссылка на облако."
        )
    total = int(resp.headers.get("Content-Length") or 0) or None
    if total and total > LINK_DOWNLOAD_LIMIT:
        raise LinkError(
            f"Файл {total / 1024 / 1024:.0f} МБ, а бот берёт не больше "
            f"{LINK_DOWNLOAD_LIMIT // 1024 // 1024} МБ."
        )

    written = 0
    last = 0.0
    loop = asyncio.get_running_loop()
    with dest.open("wb") as f:
        async for chunk in resp.content.iter_chunked(LINK_CHUNK):
            written += len(chunk)
            if written > LINK_DOWNLOAD_LIMIT:
                raise LinkError(
                    f"Файл перевалил за {LINK_DOWNLOAD_LIMIT // 1024 // 1024} МБ - "
                    f"скачивание прервано."
                )
            f.write(chunk)
            if progress and loop.time() - last >= 4:
                last = loop.time()
                await progress(written, total)
    if not written:
        raise LinkError("Файл пустой - по ссылке нечего сохранять.")
    return written


def _gdrive_file_id(url: str) -> str | None:
    parsed = urlparse(url)
    if "/file/d/" in parsed.path:
        return parsed.path.split("/file/d/", 1)[1].split("/")[0]
    found = parse_qs(parsed.query)
    for key in ("id", "usg"):
        if found.get(key):
            return found[key][0]
    return None


def _parse_gdrive_confirm(page: str):
    """Google на файлах крупнее ~100 МБ отдаёт HTML-прослойку с формой вместо
    видео. Достаём action и скрытые поля, чтобы повторить запрос от неё."""
    m = re.search(r'<form[^>]+action="([^"]+)"', page)
    if not m:
        return None
    action = urljoin(GDRIVE_API, m.group(1).replace("&amp;", "&"))
    fields = {
        name: value.replace("&amp;", "&")
        for name, value in re.findall(
            r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', page
        )
    }
    return action, fields


async def _download_gdrive(session, url, dest, progress):
    file_id = _gdrive_file_id(url)
    if not file_id:
        raise LinkError(
            "Не разобрал ссылку Google Drive. Пришли адрес вида "
            "https://drive.google.com/file/d/.../view"
        )
    params = {"id": file_id, "export": "download", "confirm": "t"}
    action = fields = None
    async with session.get(GDRIVE_API, params=params) as resp:
        if "text/html" in (resp.headers.get("Content-Type") or "").lower():
            form = _parse_gdrive_confirm(await resp.text())
            if not form:
                raise LinkError(
                    "Google Drive не отдал файл. Открой доступ «Все, у кого есть "
                    "ссылка» и убедись, что файл не удалён."
                )
            action, fields = form
    if action:
        async with session.get(action, params=fields) as resp:
            return await _pump(resp, dest, progress)
    async with session.get(GDRIVE_API, params=params) as resp:
        return await _pump(resp, dest, progress)


async def _download_yandex(session, url, dest, progress):
    async with session.get(YANDEX_API, params={"public_key": url}) as resp:
        if resp.status != 200:
            try:
                detail = (await resp.json(content_type=None)).get("message", "")
            except Exception:
                detail = ""
            raise LinkError(
                "Яндекс.Диск не отдал файл. Проверь, что доступ по ссылке открыт "
                "и ссылка не истёкла." + (f" Ответ: {detail}" if detail else "")
            )
        payload = await resp.json(content_type=None)
    href = payload.get("href")
    if not href:
        raise LinkError(
            "Яндекс.Диск не вернул ссылку на скачивание - по этой ссылке файл "
            "недоступен."
        )
    async with session.get(href) as resp:
        return await _pump(resp, dest, progress)


async def download_by_url(url: str, dest: Path, progress=None) -> int:
    """Качает видео по ссылке в dest. Понимает три вида ссылок: прямую на файл,
    Google Drive и публичный Яндекс.Диск."""
    host = (urlparse(url).hostname or "").strip("[]")
    if not host:
        raise LinkError("В ссылке нет адреса.")
    # aiohttp не зовёт резолвер для IP-литералов: подставил в URL 127.0.0.1 -
    # и он ушёл в сеть вообще без проверки. Такие адреса отсекаем сами, до
    # установления соединения.
    try:
        literal = bool(ipaddress.ip_address(host))
    except ValueError:
        literal = False
    if literal and not _is_public(host):
        raise LinkError(
            f"Ссылка ведёт на внутренний адрес ({host}). "
            f"Бот качает только из открытого интернета."
        )

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as session:
        if "disk.yandex" in host or "yadi.sk" in host:
            return await _download_yandex(session, url, dest, progress)
        if host in ("drive.google.com", "docs.google.com",
                    "drive.usercontent.google.com"):
            return await _download_gdrive(session, url, dest, progress)
        async with session.get(url) as resp:
            return await _pump(resp, dest, progress)


def _media_problems(duration: int, width: int | None, height: int | None,
                    banner_path, brand: str, config: dict) -> str | None:
    """Общие проверки исходника. duration/width/height приходят из Telegram для
    файлов и из ffprobe для ссылок - правила одни и те же."""
    if duration > MAX_DURATION:
        return f"❌ Видео длиннее {MAX_DURATION} секунд ({duration} сек)."

    # Короткий ролик при однократной вставке отсекаем ДО рендера: баннер
    # занимает середину ролика целиком, и в 20-секундный ролик 20-секундный
    # баннер влезет только формально - исходника не останется ни до, ни после.
    if banner_path and duration > 0:
        insertion = render.resolve_insertion(config)
        if insertion:
            _, _, banner_duration = render.probe(banner_path)
            min_total = render.min_total_duration(banner_duration, insertion)
            if duration < min_total:
                return (
                    f"❌ <b>Ролик короче {min_total:.0f} секунд</b> "
                    f"({duration} сек).\n\n"
                    f"Баннер {render.BRAND_LABELS.get(brand, config.get('title', brand))} "
                    f"показывается один раз в середине ролика и "
                    f"занимает {banner_duration:.0f} сек, а до и после него должно "
                    f"остаться хотя бы {render.MIN_TAIL_SECONDS:.0f} сек "
                    f"исходника — иначе вставка съедает ролик целиком.\n\n"
                    f"📱 Возьми ролик подлиннее или вырежи лишнее."
                )

    # Отсекаем горизонтальные ролики: safe zones заданы под вертикальный 9:16,
    # на альбомном кадре баннер уедет в неправильное место.
    if width and height and height <= width:
        return (
            f"❌ Это <b>горизонтальное</b> видео ({width}×{height}).\n\n"
            f"Нужен вертикальный ролик 9:16 — обычно 1080×1920. "
            f"Возьми исходник вертикальной съёмки, а не обрезанный/повёрнутый ролик из ленты."
        )

    # Кадр крупнее 4 Мп отклоняем: ужатие идёт отдельным проходом, и на 4K
    # он сам по себе ест 446 МБ из 512 - запас в 13%. Всё, что меньше,
    # бот ужимает сам, включая 1440x2560 и любые iPhone-разрешения.
    if (width or 0) * (height or 0) > render.MAX_IN_PIXELS:
        return (
            f"❌ Слишком большое разрешение: <b>{width}×{height}</b>.\n\n"
            f"Бот сам ужмёт ролик до {render.MAX_OUT_W}×{render.MAX_OUT_H}, "
            f"но на хостинге помещается кадр не крупнее 4 Мп — "
            f"на {width * height / 1_000_000:.1f} Мп памяти не хватит.\n\n"
            f"📱 Как пережать на телефоне: открой ролик → <b>Поделиться</b> → "
            f"<b>Сохранить видео</b> — iPhone сам отдаёт 1080p."
        )
    return None


def _banner_problem(brand: str, platform: str, config: dict, banner_path) -> str | None:
    if banner_path:
        return None
    expected = (config.get("media_by_platform") or {}).get(platform) \
        or config.get("media") or "banner.mp4"
    return (
        f"❌ Файл баннера для <b>{brand}</b> на {platform.upper()} не найден.\n"
        f"Ожидается: <code>banners/{brand}/{expected}</code>"
    )


async def _render_and_send(message: Message, state: FSMContext, msg: Message,
                            in_vid: Path, out_vid: Path, banner_path,
                            platform: str, brand: str, variant: str | None,
                            position: str):
    """Рендерит баннер на уже скачанный исходник и отправляет результат.

    Скачивание (файл из Telegram или по ссылке) делает вызывающий и заодно
    создаёт msg для прогресса. Сюда приходит готовый in_vid, поэтому рендер,
    подписи и отправка общие для обоих путей.
    """
    config = render.load_brand(brand)
    label = render.BRAND_LABELS.get(brand, brand.upper())
    platform_label = render.PLATFORM_LABELS.get(platform, platform.upper())
    position_label = render.POSITION_LABELS[position]

    vw = vh = ow = oh = 0

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
                await safe_edit(msg,
                    f"⏳ <b>Рендер идёт</b> · {label} · {platform_label}\n"
                    f"<i>Прошло {spent} сек.{extra}</i>"
                )
            except Exception:
                pass

    try:
        vw, vh, _ = render.probe(in_vid)
        ow, oh = render.out_size(vw, vh)

        # Рендер строго по одному: два параллельных ffmpeg на 512 МБ не
        # помещаются. Если рендер уже идёт - честно говорим про очередь.
        if render_lock.locked():
            await safe_edit(msg,
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
        ins_note = ""
        if plan.get("insertion"):
            i = plan["insertion"]
            ins_note = (f"\n🎞 Баннер один раз: {i['t_start']:.0f}–{i['t_end']:.0f} сек, "
                        f"ролик на это время замрёт и пойдёт дальше без сдвига")
            if plan.get("audio_warning") == "no_banner_audio":
                ins_note += "\n🔇 У баннера нет звука — в этот момент продолжился звук ролика"

        out_size = out_vid.stat().st_size
        if out_size > TG_UPLOAD_LIMIT:
            await message.answer(
                f"❌ Ролик собрался, но весит {out_size / 1024 / 1024:.0f} МБ — "
                f"Telegram принимает от бота не больше "
                f"{TG_UPLOAD_LIMIT // 1024 // 1024} МБ.\n\n"
                f"📱 Возьми ролик покороче: баннер тот же, а результат легче.",
            )
            return
        await safe_edit(msg, "🚀 Загружаю результат...")
        await message.answer_video(
            video=FSInputFile(out_vid),
            caption=(
                f"Готово ({label} · {platform_label} · {position_label})\n"
                f"Баннер {bw}×{bh} — {plan['area_ratio'] * 100:.0f}% экрана"
                + anim
                + ins_note
                + warn
            ),
        )
    except Exception as e:
        # Причину показываем целиком: по одному "TelegramBadRequest" невозможно
        # понять, что случилось - лимит размера, битый файл или что-то ещё.
        reason = html.escape(str(e))[:300]
        await message.answer(
            f"❌ <b>Не получилось сделать ролик.</b>\n\n"
            f"Причина: <code>{type(e).__name__}</code>\n"
            f"<i>{reason}</i>\n\n"
            f"Если повторяется — пришли это разработчику.",
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


@dp.message(VideoState.waiting_for_video, F.video)
async def process_video(message: Message, state: FSMContext):
    duration = message.video.duration or 0
    if duration > MAX_DURATION:
        return await message.answer(f"❌ Видео длиннее {MAX_DURATION} секунд ({duration} сек).")

    # Telegram Bot API отдаёт боту файлы не крупнее 20 МБ: get_file падает с
    # Bad Request: file is too big. Проверяем размер до скачивания и объясняем
    # причину по-человечески, а не ловим сырое исключение в конце пайплайна.
    size = message.video.file_size or 0
    if size > TG_DOWNLOAD_LIMIT:
        return await message.answer(
            f"❌ <b>Ролик слишком тяжёлый</b>: {size / 1024 / 1024:.1f} МБ.\n\n"
            f"Telegram отдаёт боту файлы не крупнее "
            f"{TG_DOWNLOAD_LIMIT // 1024 // 1024} МБ, поэтому такой я скачать "
            f"не смогу.\n\n"
            f"📱 Обрежь его покороче или пережми "
            f"(<b>Поделиться → Сохранить видео</b>) и пришли снова.",
            parse_mode="HTML",
        )

    data = await state.get_data()
    platform = data.get("chosen_platform")
    brand = data.get("chosen_brand")
    variant = data.get("chosen_variant")

    if not platform or not brand:
        await state.clear()
        return await message.answer("❌ Сначала выбери платформу и бренд.", reply_markup=get_platforms_keyboard())

    config = render.load_brand(brand)
    # Файл берём по выбранному варианту: у FunPay это games или services, и
    # без variant find_banner отдал бы первый попавшийся в папке.
    banner_path = render.find_banner(brand, config, platform=platform, variant=variant)
    # Позицию нормализуем здесь: в state могли прийти мусорные данные, а
    # resolve_position гарантирует одну из POSITIONS и не даёт баннеру уехать
    # под кнопки интерфейса.
    position = render.resolve_position(config, data.get("chosen_position"))

    problem = _media_problems(
        duration, message.video.width, message.video.height, banner_path, brand, config
    ) or _banner_problem(brand, platform, config, banner_path)
    if problem:
        return await message.answer(problem, parse_mode="HTML")

    variant_title = ""
    if variant:
        for v in render.list_variants(brand, config):
            if v["file"] == variant:
                variant_title = f" · {v['title']}"
                break
    msg = await message.answer(
        f"⏳ Скачиваю ролик и рендерю <b>{label}</b> на {platform_label} "
        f"({position_label}{variant_title})...",
        parse_mode="HTML",
    )
    file_id = message.video.file_id
    in_vid = BASE / "videos" / f"in_{file_id}.mp4"
    out_vid = BASE / "videos" / f"out_{file_id}.mp4"
    try:
        tg_file = await bot.get_file(file_id)
        await bot.download_file(tg_file.file_path, destination=in_vid)
    except Exception as e:
        # get_file на файле крупнее 20 МБ падает сам, и раньше эта ошибка
        # уезжала в общий except в самом конце пайплайна. Ловим здесь и сразу
        # предлагаем ссылку - по ней ограничения Telegram не действуют.
        in_vid.unlink(missing_ok=True)
        return await message.answer(
            f"❌ Не смог скачать ролик из Telegram: {html.escape(str(e))[:200]}\n\n"
            f"Пришли файл помельче или кинь <b>ссылку</b> на него — "
            f"по ссылке я качаю без ограничения Telegram.",
            parse_mode="HTML",
        )
    return await _render_and_send(
        message, state, msg, in_vid, out_vid, banner_path, platform, brand, variant, position
    )


@dp.message(VideoState.waiting_for_video, F.text)
async def process_link(message: Message, state: FSMContext):
    """Ролик приходит ссылкой.

    Нужно потому, что Telegram отдаёт боту файлы максимум 20 МБ: ролик крупнее
    в чат не загрузится никак. По URL бот качает сам, и там ограничение только
    наше - диск и память хостинга.
    """
    url = extract_url(message.text or "")
    if not url:
        return await message.answer(
            "Пришли <b>файлом</b> ролик или <b>ссылкой</b> на него.\n\n"
            "🔗 Подойдёт прямая ссылка на видео либо публичная ссылка Google Drive "
            "или Яндекс.Диска — то, что отдаёт сам файл, а не страницу.",
            parse_mode="HTML",
        )

    data = await state.get_data()
    platform = data.get("chosen_platform")
    brand = data.get("chosen_brand")
    variant = data.get("chosen_variant")
    if not platform or not brand:
        await state.clear()
        return await message.answer(
            "❌ Сначала выбери платформу и бренд.",
            reply_markup=get_platforms_keyboard(),
        )

    config = render.load_brand(brand)
    banner_path = render.find_banner(brand, config, platform=platform, variant=variant)
    # Баннер проверяем до скачивания: если его нет, файл качать незачем.
    problem = _banner_problem(brand, platform, config, banner_path)
    if problem:
        return await message.answer(problem, parse_mode="HTML")

    position = render.resolve_position(config, data.get("chosen_position"))
    label = render.BRAND_LABELS.get(brand, brand.upper())
    platform_label = render.PLATFORM_LABELS.get(platform, platform.upper())
    position_label = render.POSITION_LABELS[position]
    # Название варианта показываем в прогрессе, чтобы было видно, что в ролик
    # едет именно тот баннер, который выбрали, а не первый файл в папке.
    variant_title = ""
    if variant:
        for v in render.list_variants(brand, config):
            if v["file"] == variant:
                variant_title = f" · {v['title']}"
                break

    tag = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:12]
    in_vid = BASE / "videos" / f"in_link_{tag}.mp4"
    out_vid = BASE / "videos" / f"out_link_{tag}.mp4"

    async def on_progress(done: int, total: int | None):
        got = f"{done / 1024 / 1024:.0f} МБ"
        if total:
            got += f" из {total / 1024 / 1024:.0f} МБ ({done * 100 // total}%)"
        try:
            await safe_edit(msg, f"⏳ <b>Качаю ролик по ссылке</b> · {got}",
                            parse_mode="HTML")
        except Exception:
            pass

    msg = await message.answer(
        f"⏳ <b>Качаю ролик по ссылке</b>\n"
        f"<i>{label} · {platform_label} · {position_label}{variant_title}</i>",
        parse_mode="HTML",
    )
    try:
        await download_by_url(url, in_vid, progress=on_progress)
    except LinkError as e:
        in_vid.unlink(missing_ok=True)
        return await message.answer(f"❌ {html.escape(str(e))}", parse_mode="HTML")
    except Exception as e:
        in_vid.unlink(missing_ok=True)
        print(f"[LINK-ERROR] {url}: {type(e).__name__}: {e}")
        return await message.answer(
            f"❌ Не смог скачать по ссылке: <code>{type(e).__name__}</code>\n"
            f"<i>{html.escape(str(e))[:200]}</i>",
            parse_mode="HTML",
        )

    # Геометрию и длительность узнаём из самого файла: в ответе на ссылку их
    # нет, а Telegram доверять меткам с каждым пережатием тоже нельзя.
    try:
        vw, vh, duration = render.probe(in_vid)
    except Exception:
        in_vid.unlink(missing_ok=True)
        return await message.answer(
            "❌ По ссылке скачался не видеофайл.\n\n"
            "Нужен mp4/mov, а не аудио, архив или веб-страница.",
            parse_mode="HTML",
        )

    problem = _media_problems(
        int(round(duration or 0)), vw, vh, banner_path, brand, config
    )
    if problem:
        in_vid.unlink(missing_ok=True)
        return await message.answer(problem, parse_mode="HTML")

    return await _render_and_send(
        message, state, msg, in_vid, out_vid, banner_path, platform, brand, variant, position
    )


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
        variants = render.list_variants(brand, config)
        for platform in platforms:
            found = render.find_banner(brand, config, platform=platform)
            state = f"OK {found.name}" if found else "НЕТ ФАЙЛА БАННЕРА"
            marker = "" if platform not in per_platform else " *"
            print(f"  {brand}/{platform}{marker}: {state}")
        # Варианты не зависят от площадки, поэтому печатаем их один раз.
        for v in variants:
            print(f"  {brand}/вариант: OK {v['file']} — {v['title']}")
        if not per_platform and not render.find_banner(brand, config):
            print(f"  {brand}: НЕТ ФАЙЛА БАННЕРА")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
