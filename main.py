
import asyncio
import html
import hmac
import logging
import os
import re
import time
from collections import deque
from contextlib import suppress

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    Message,
    Update,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import CHAT_LINK, TARGET_COMMANDS, TOPIC_NAMES


# ==================================================
# 1. НАСТРОЙКИ
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", "10000"))

# Настройки модерации
ALLOWED_TOPIC_ID = 18600
TARGET_BOT_USERNAME = "brassgay"
WARNING_DELETE_DELAY = 12

# Настройки TendorX
# Обязательная подписка временно отключена.
TARGET_GROUP_USERNAME = "gamefloid"
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", "0"))

TENDORX_CHANNEL = "@TendorX"
TENDORX_CHANNEL_LINK = "https://t.me/TendorX"

SUBSCRIPTION_PROMPT_COOLDOWN = 60

# Защита от повторных обновлений
MAX_PROCESSED_UPDATES = 5000

processed_update_ids = set()
processed_update_order = deque()
pending_update_ids = set()

update_lock = asyncio.Lock()

# Фоновые задачи
background_tasks = set()

BOT_ID = None

# Защита от повторных уведомлений
subscription_prompt_times = {}

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not RENDER_EXTERNAL_URL.startswith("https://"):
    raise RuntimeError("Неверный RENDER_EXTERNAL_URL")

if not WEBHOOK_SECRET or not re.fullmatch(
    r"[A-Za-z0-9_-]{1,256}",
    WEBHOOK_SECRET
):
    raise RuntimeError("Неверный WEBHOOK_SECRET")


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()


# ==================================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==================================================

def escape(value):
    return html.escape(str(value or ""), quote=True)


def normalize_commands(commands):

    if isinstance(commands, str):
        commands = (commands,)

    result = set()

    for item in commands:

        command = str(item).strip().lower()

        if not command:
            continue

        command = command.lstrip("/")

        result.add(command)
        result.add("/" + command)

    return result


TARGET_COMMAND_SET = normalize_commands(TARGET_COMMANDS)


def is_target_command(text):

    if not text or not text.strip():
        return False

    first_part = text.strip().split(maxsplit=1)[0]

    command = first_part.split("@", 1)[0].lower()

    return command in TARGET_COMMAND_SET


def is_target_bot_message(message: Message):

    if message.from_user:

        username = message.from_user.username

        if (
            message.from_user.is_bot
            and username
            and username.lower() == TARGET_BOT_USERNAME.lower()
        ):
            return True

    if message.via_bot:

        username = message.via_bot.username

        if (
            username
            and username.lower() == TARGET_BOT_USERNAME.lower()
        ):
            return True

    return False


def is_target_group(message: Message) -> bool:

    if TARGET_GROUP_ID:
        return message.chat.id == TARGET_GROUP_ID

    username = message.chat.username or ""

    return username.lower() == TARGET_GROUP_USERNAME.lower()


# ==================================================
# 3. ПРОВЕРКА ПОДПИСКИ TENDORX
# ==================================================

# Функции сохранены для будущего использования.
# Автоматическая проверка подписки отключена.


async def is_subscribed(user_id: int):

    try:

        member = await bot.get_chat_member(
            chat_id=TENDORX_CHANNEL,
            user_id=user_id
        )

        if member.status in (
            "member",
            "administrator",
            "creator"
        ):
            return True

        if (
            member.status == "restricted"
            and getattr(member, "is_member", False)
        ):
            return True

        return False

    except TelegramAPIError:

        logger.exception(
            "Ошибка проверки подписки пользователя %s",
            user_id
        )

        return None


def subscription_keyboard(chat_id: int, user_id: int):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подписаться на TendorX",
                    url=TENDORX_CHANNEL_LINK
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить подписку",
                    callback_data=f"subcheck:{chat_id}:{user_id}"
                )
            ]
        ]
    )


async def ask_to_subscribe(message: Message):

    if not message.from_user:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    now = time.monotonic()

    key = (chat_id, user_id)

    last_prompt = subscription_prompt_times.get(key, 0)

    try:

        await message.delete()

    except TelegramAPIError:

        logger.exception(
            "Не удалось удалить сообщение пользователя %s",
            user_id
        )

    if now - last_prompt < SUBSCRIPTION_PROMPT_COOLDOWN:
        return

    subscription_prompt_times[key] = now

    if len(subscription_prompt_times) > 10000:

        cutoff = now - SUBSCRIPTION_PROMPT_COOLDOWN * 2

        stale_keys = [
            stored_key
            for stored_key, stored_time
            in subscription_prompt_times.items()
            if stored_time < cutoff
        ]

        for stored_key in stale_keys:
            subscription_prompt_times.pop(stored_key, None)

    first_name = escape(
        message.from_user.first_name or "Пользователь"
    )

    user_mention = (
        f'<a href="tg://user?id={user_id}">'
        f'{first_name}</a>'
    )

    text = (
        f"👋 {user_mention}, чтобы писать в сообществе GameFloid, "
        "необходимо подписаться на официальный канал TendorX.\n\n"
        "1. Нажмите «Подписаться на TendorX».\n"
        "2. Вернитесь сюда.\n"
        "3. Нажмите «Проверить подписку».\n\n"
        "После подтверждения подписки ваши сообщения будут доступны."
    )

    try:

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=message.message_thread_id,
            reply_markup=subscription_keyboard(
                chat_id,
                user_id
            ),
            disable_web_page_preview=True
        )

    except TelegramAPIError:

        logger.exception(
            "Не удалось отправить сообщение о подписке"
        )


# ==================================================
# 4. ФОНОВЫЕ ЗАДАЧИ
# ==================================================

def create_background_task(coro):

    task = asyncio.create_task(coro)

    background_tasks.add(task)

    def finished(completed):

        background_tasks.discard(completed)

        if completed.cancelled():
            return

        error = completed.exception()

        if error:

            logger.error(
                "Ошибка фоновой задачи: %s",
                error
            )

    task.add_done_callback(finished)

    return task


def get_topic_link(topic_id):

    if not topic_id:
        return None

    base_url = CHAT_LINK.rstrip("/").rsplit("/", 1)[0]

    return f"{base_url}/{topic_id}"


# ==================================================
# 5. ОТЧЁТ АДМИНИСТРАЦИИ
# ==================================================

async def send_admin_report(
    topic_id,
    user_id,
    first_name,
    username,
    command_text,
    command_deleted
):

    if not ADMIN_CHAT_ID:
        return

    try:

        mention = (
            f'<a href="tg://user?id={user_id}">'
            f'{escape(first_name)}</a>'
        )

        username_text = (
            f" | @{escape(username)}"
            if username else ""
        )

        topic_name = escape(
            TOPIC_NAMES.get(
                topic_id,
                f"Тема {topic_id}"
                if topic_id else "Основной чат"
            )
        )

        topic_url = get_topic_link(topic_id)

        if topic_url:

            topic_display = (
                f'<a href="{escape(topic_url)}">'
                f'{topic_name}</a>'
            )

        else:

            topic_display = topic_name

        report_text = (
            "🚨 <b>Отчёт о нарушении правил чата</b>\n\n"
            f"👤 <b>Нарушитель:</b> {mention}{username_text}\n"
            f"🆔 <b>ID пользователя:</b> "
            f"<code>{user_id}</code>\n"
            f"🏷 <b>Тема нарушения:</b> {topic_display}\n"
            f"⌨️ <b>Запрещённая команда:</b> "
            f"<code>{escape(command_text)}</code>\n\n"
            f"🗑 <b>Удаление команды:</b> "
            f"{'Успешно' if command_deleted else 'Не удалось'}"
        )

        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=report_text,
            disable_web_page_preview=True
        )

        logger.info(
            "Отчёт отправлен администрации. User ID: %s",
            user_id
        )

    except TelegramAPIError:

        logger.exception(
            "Ошибка отправки отчёта администрации"
        )


# ==================================================
# 6. УДАЛЕНИЕ ПРЕДУПРЕЖДЕНИЯ
# ==================================================

async def cleanup_warning(chat_id, warning_id):

    try:

        await asyncio.sleep(WARNING_DELETE_DELAY)

        await bot.delete_message(
            chat_id=chat_id,
            message_id=warning_id
        )

        logger.info(
            "Предупреждение %s удалено",
            warning_id
        )

    except asyncio.CancelledError:

        raise

    except TelegramAPIError:

        logger.exception(
            "Не удалось удалить предупреждение %s",
            warning_id
        )


# ==================================================
# 7. КНОПКА ПРОВЕРКИ ПОДПИСКИ
# ==================================================

# Обработчик оставлен на будущее.
# Автоматически пользователям кнопка не отправляется.

@dp.callback_query(
    lambda c: c.data and c.data.startswith("subcheck:")
)
async def check_subscription_callback(
    callback: CallbackQuery
):

    try:

        _, chat_id_text, user_id_text = (
            callback.data.split(":", 2)
        )

        chat_id = int(chat_id_text)
        user_id = int(user_id_text)

        if callback.from_user.id != user_id:

            await callback.answer(
                "Эта кнопка предназначена другому пользователю.",
                show_alert=True
            )

            return

        if (
            callback.message
            and callback.message.chat.id != chat_id
        ):

            await callback.answer(
                "Некорректный запрос.",
                show_alert=True
            )

            return

        subscribed = await is_subscribed(user_id)

        if subscribed is None:

            await callback.answer(
                "Не удалось проверить подписку. "
                "Попробуйте немного позже.",
                show_alert=True
            )

            return

        if not subscribed:

            await callback.answer(
                "Подписка пока не найдена. "
                "Подпишитесь на TendorX и повторите проверку.",
                show_alert=True
            )

            return

        await callback.answer(
            "Подписка подтверждена! Теперь можно писать."
        )

        if callback.message:

            try:

                await callback.message.edit_text(
                    "✅ <b>Подписка подтверждена!</b>\n\n"
                    "Теперь вы можете общаться в GameFloid."
                )

            except TelegramAPIError:

                logger.exception(
                    "Не удалось обновить сообщение проверки"
                )

        subscription_prompt_times.pop(
            (chat_id, user_id),
            None
        )

        logger.info(
            "Подписка TendorX подтверждена для пользователя %s",
            user_id
        )

    except (ValueError, TypeError):

        logger.exception(
            "Некорректные данные кнопки проверки подписки"
        )

        await callback.answer(
            "Некорректный запрос.",
            show_alert=True
        )

    except TelegramAPIError:

        logger.exception(
            "Ошибка Telegram при проверке подписки"
        )

        await callback.answer(
            "Ошибка Telegram. Попробуйте ещё раз позже.",
            show_alert=True
        )

    except Exception:

        logger.exception(
            "Непредвиденная ошибка проверки подписки"
        )

        await callback.answer(
            "Произошла ошибка. Попробуйте ещё раз.",
            show_alert=True
        )


# ==================================================
# 8. ОСНОВНАЯ МОДЕРАЦИЯ ЧАТА
# ==================================================

@dp.message()
async def moderate_chat(message: Message):

    # Не модерируем административный чат
    if ADMIN_CHAT_ID and message.chat.id == ADMIN_CHAT_ID:
        return

    # Не модерируем сообщения собственного бота
    if (
        message.from_user
        and message.from_user.id == BOT_ID
    ):
        return

    # ------------------------------------------------
    # ОБЯЗАТЕЛЬНАЯ ПОДПИСКА TENDORX
    # ВРЕМЕННО ПОЛНОСТЬЮ ОТКЛЮЧЕНА
    # ------------------------------------------------

    # Проверка подписки не выполняется.
    # Сообщения пользователей не удаляются из-за подписки.
    # Уведомления о подписке не отправляются.

    # ------------------------------------------------
    # СТАРАЯ МОДЕРАЦИЯ
    # ------------------------------------------------

    current_topic = message.message_thread_id or 0

    # Разрешённая тема
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # Удаляем только BR Assistant
    if is_target_bot_message(message):

        try:

            await message.delete()

            logger.info(
                "Удалено сообщение BR Assistant. Тема: %s",
                current_topic
            )

        except TelegramAPIError:

            logger.exception(
                "Не удалось удалить сообщение BR Assistant"
            )

        return

    # Других ботов не трогаем
    if message.from_user and message.from_user.is_bot:
        return

    if not message.text:
        return

    if not is_target_command(message.text):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id

    first_name = (
        message.from_user.first_name or "Пользователь"
    )

    username = message.from_user.username

    chat_id = message.chat.id

    command_text = message.text

    logger.info(
        "Запрещённая команда: %s, пользователь: %s",
        command_text,
        user_id
    )

    warning_text = (
        f'⚠️ <a href="tg://user?id={user_id}">'
        f'{escape(first_name)}</a>, '
        f'использовать команды бота <b>BR Assistant</b>\n'
        f'можно строго в теме '
        f'<b><a href="{escape(CHAT_LINK)}">'
        f'Чат для общения</a></b>!'
    )

    warning_id = None

    # Отправляем предупреждение
    try:

        warning = await bot.send_message(
            chat_id=chat_id,
            text=warning_text,
            message_thread_id=(
                current_topic if current_topic else None
            ),
            disable_web_page_preview=True
        )

        warning_id = warning.message_id

    except TelegramAPIError:

        logger.exception(
            "Не удалось отправить предупреждение"
        )

    # Удаляем запрещённую команду
    command_deleted = False

    try:

        await message.delete()

        command_deleted = True

        logger.info(
            "Команда %s удалена",
            message.message_id
        )

    except TelegramAPIError:

        logger.exception(
            "Не удалось удалить команду"
        )

    # Отправляем отчёт администрации
    await send_admin_report(
        topic_id=current_topic,
        user_id=user_id,
        first_name=first_name,
        username=username,
        command_text=command_text,
        command_deleted=command_deleted
    )

    # Удаляем предупреждение через 12 секунд
    if warning_id is not None:

        create_background_task(
            cleanup_warning(chat_id, warning_id)
        )


# ==================================================
# 9. ПРОВЕРКА ДОСТУПНОСТИ RENDER
# ==================================================

async def healthcheck(request):

    return web.Response(
        text="Telegram bot server is running",
        status=200
    )


# ==================================================
# 10. ЗАЩИТА ОТ ПОВТОРНОЙ ОБРАБОТКИ UPDATE
# ==================================================

async def reserve_update(update_id):

    async with update_lock:

        if update_id in processed_update_ids:
            return "processed"

        if update_id in pending_update_ids:
            return "pending"

        pending_update_ids.add(update_id)

        return "reserved"


async def finish_update(update_id, success):

    async with update_lock:

        pending_update_ids.discard(update_id)

        if (
            not success
            or update_id in processed_update_ids
        ):
            return

        if len(processed_update_order) >= MAX_PROCESSED_UPDATES:

            oldest = processed_update_order.popleft()

            processed_update_ids.discard(oldest)

        processed_update_order.append(update_id)

        processed_update_ids.add(update_id)


# ==================================================
# 11. TELEGRAM WEBHOOK
# ==================================================

async def handle_webhook(request):

    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET
    ):

        logger.warning(
            "Получен вебхук с неверным секретом"
        )

        return web.Response(
            text="Forbidden",
            status=403
        )

    try:

        json_data = await request.json()

        telegram_update = Update.model_validate(
            json_data,
            context={"bot": bot}
        )

    except Exception:

        logger.exception(
            "Некорректный JSON или структура вебхука"
        )

        return web.Response(
            text="Bad Request",
            status=400
        )

    update_id = telegram_update.update_id

    reservation = await reserve_update(update_id)

    if reservation == "processed":

        logger.info(
            "Уже обработанный update %s пропущен",
            update_id
        )

        return web.Response(
            text="ok",
            status=200
        )

    if reservation == "pending":

        logger.info(
            "Update %s уже обрабатывается",
            update_id
        )

        return web.Response(
            text="Retry",
            status=503
        )

    success = False

    try:

        await dp.feed_update(
            bot,
            telegram_update
        )

        success = True

        return web.Response(
            text="ok",
            status=200
        )

    except Exception:

        logger.exception(
            "Ошибка обработки Telegram Update"
        )

        return web.Response(
            text="Internal Server Error",
            status=500
        )

    finally:

        await finish_update(
            update_id,
            success
        )


# ==================================================
# 12. ЗАПУСК СЕРВЕРА
# ==================================================

async def main_async():

    global BOT_ID

    app = web.Application(
        client_max_size=1024 * 1024
    )

    app.router.add_get(
        "/",
        healthcheck
    )

    app.router.add_post(
        "/",
        handle_webhook
    )

    runner = web.AppRunner(app)

    try:

        bot_info = await bot.get_me()

        BOT_ID = bot_info.id

        logger.info(
            "Бот запущен: @%s, ID: %s",
            bot_info.username,
            BOT_ID
        )

        await runner.setup()

        site = web.TCPSite(
            runner,
            host="0.0.0.0",
            port=PORT
        )

        await site.start()

        logger.info(
            "HTTP-сервер запущен на порту %s",
            PORT
        )

        await bot.set_webhook(
            url=RENDER_EXTERNAL_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
            allowed_updates=[
                "message",
                "callback_query"
            ]
        )

        logger.info(
            "Telegram Webhook успешно установлен"
        )

        await asyncio.Event().wait()

    finally:

        logger.info(
            "Остановка бота"
        )

        for task in list(background_tasks):
            task.cancel()

        for task in list(background_tasks):

            with suppress(asyncio.CancelledError):
                await task

        await runner.cleanup()

        await bot.session.close()

        logger.info(
            "Бот остановлен"
        )


# ==================================================
# 13. ТОЧКА ВХОДА
# ==================================================

if __name__ == "__main__":

    try:

        asyncio.run(main_async())

    except (KeyboardInterrupt, SystemExit):

        logger.info(
            "Работа завершена"
        )
