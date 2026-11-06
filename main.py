
import asyncio
import html
import hmac
import json
import logging
import os
from contextlib import suppress

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, Update

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

# ID разрешённой темы
ALLOWED_TOPIC_ID = 18600

# Username BR Assistant
TARGET_BOT_USERNAME = "brassgay"

# Время жизни предупреждения
WARNING_DELETE_DELAY = 12

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not RENDER_EXTERNAL_URL.startswith("https://"):
    raise RuntimeError("Неверный RENDER_EXTERNAL_URL")

if not WEBHOOK_SECRET:
    raise RuntimeError("Не задан WEBHOOK_SECRET")

if not (1 <= len(WEBHOOK_SECRET) <= 256):
    raise RuntimeError("Неверная длина WEBHOOK_SECRET")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

BOT_ID = None
background_tasks = set()


# ==================================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==================================================

def escape(value):
    return html.escape(str(value or ""), quote=True)


def normalize_commands(commands):
    if isinstance(commands, str):
        commands = (commands,)

    result = set()

    for command in commands:
        command = str(command).strip().lower()

        if not command:
            continue

        command = command.lstrip("/")

        result.add(command)
        result.add("/" + command)

    return result


TARGET_COMMAND_SET = normalize_commands(TARGET_COMMANDS)


def is_target_command(text):
    if not text:
        return False

    first_part = text.strip().split(maxsplit=1)[0]

    # Учитываем команды вида /start@BotName
    command = first_part.split("@", 1)[0].lower()

    return command in TARGET_COMMAND_SET


def is_target_bot_message(message: Message):
    if message.from_user:
        username = message.from_user.username

        if (
            message.from_user.is_bot
            and username
            and username.lower() == TARGET_BOT_USERNAME
        ):
            return True

    if message.via_bot:
        username = message.via_bot.username

        if username and username.lower() == TARGET_BOT_USERNAME:
            return True

    return False


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


# ==================================================
# 3. ССЫЛКА НА КОНКРЕТНУЮ ТЕМУ
# ==================================================

def get_topic_link(topic_id):
    if not topic_id:
        return None

    # CHAT_LINK содержит ссылку на разрешённую тему.
    # Используем её основу для построения ссылок
    # на остальные темы той же публичной группы.

    base_url = CHAT_LINK.rstrip("/").rsplit("/", 1)[0]

    return f"{base_url}/{topic_id}"


# ==================================================
# 4. ОТЧЁТ АДМИНИСТРАЦИИ
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
                f"Тема {topic_id}" if topic_id else "Основной чат"
            )
        )

        # Создаём кликабельную ссылку на тему.
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
# 5. УДАЛЕНИЕ ПРЕДУПРЕЖДЕНИЯ
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
# 6. МОДЕРАЦИЯ ЧАТА
# ==================================================

@dp.message()
async def moderate_chat(message: Message):

    # Не модерируем административный чат.
    if ADMIN_CHAT_ID and message.chat.id == ADMIN_CHAT_ID:
        return

    current_topic = message.message_thread_id or 0

    # Разрешённая тема.
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # Удаляем только BR Assistant.
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

    # Остальных ботов не трогаем.
    if message.from_user and message.from_user.is_bot:
        return

    # Не модерируем сообщения собственного бота.
    if message.from_user and message.from_user.id == BOT_ID:
        return

    # Проверяем текст сообщения.
    if not message.text:
        return

    if not is_target_command(message.text):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id
    first_name = message.from_user.first_name or "Пользователь"
    username = message.from_user.username

    chat_id = message.chat.id
    command_text = message.text

    logger.info(
        "Запрещённая команда: %s, пользователь: %s",
        command_text,
        user_id
    )

    # Формируем предупреждение.
    warning_text = (
        f"⚠️ <a href=\"tg://user?id={user_id}\">"
        f"{escape(first_name)}</a>, использовать команды бота "
        f"<b>BR Assistant</b>\n"
        f'можно строго в теме '
        f'<b><a href="{escape(CHAT_LINK)}">'
        f'Чат для общения</a></b>!'
    )

    warning_id = None

    # Отправляем предупреждение в текущую тему.
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

    # Удаляем только запрещённую команду.
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

    # Отправляем отчёт администрации.
    await send_admin_report(
        topic_id=current_topic,
        user_id=user_id,
        first_name=first_name,
        username=username,
        command_text=command_text,
        command_deleted=command_deleted
    )

    # Удаляем предупреждение через 12 секунд.
    if warning_id is not None:
        create_background_task(
            cleanup_warning(chat_id, warning_id)
        )


# ==================================================
# 7. ПРОВЕРКА ДОСТУПНОСТИ RENDER
# ==================================================

async def healthcheck(request):

    return web.Response(
        text="Telegram bot server is running",
        status=200
    )


# ==================================================
# 8. TELEGRAM WEBHOOK
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

    except (ValueError, TypeError, json.JSONDecodeError):
        logger.exception(
            "Некорректный JSON вебхука"
        )

        return web.Response(
            text="Bad Request",
            status=400
        )

    try:
        await dp.feed_update(
            bot,
            telegram_update
        )

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


# ==================================================
# 9. ЗАПУСК СЕРВЕРА
# ==================================================

async def main_async():

    global BOT_ID

    runner = web.AppRunner(
        web.Application()
    )

    app = runner.app

    app.router.add_get("/", healthcheck)
    app.router.add_post("/", handle_webhook)

    try:

        # Получаем ID собственного бота.
        bot_info = await bot.get_me()

        BOT_ID = bot_info.id

        logger.info(
            "Бот запущен: @%s, ID: %s",
            bot_info.username,
            BOT_ID
        )

        # Запускаем HTTP-сервер.
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

        # Устанавливаем защищённый вебхук.
        await bot.set_webhook(
            url=RENDER_EXTERNAL_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False
        )

        logger.info(
            "Telegram Webhook успешно установлен"
        )

        # Поддерживаем работу приложения.
        await asyncio.Event().wait()

    finally:

        logger.info("Остановка бота")

        for task in list(background_tasks):
            task.cancel()

        for task in list(background_tasks):
            with suppress(asyncio.CancelledError):
                await task

        await runner.cleanup()
        await bot.session.close()

        logger.info("Бот остановлен")


# ==================================================
# 10. ТОЧКА ВХОДА
# ==================================================

if __name__ == "__main__":

    try:
        asyncio.run(main_async())

    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа завершена")
