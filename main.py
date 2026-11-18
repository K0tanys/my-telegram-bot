
import asyncio
import asyncpg
import html
import hmac
import logging
import os
import re
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType, ChatMemberStatus
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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
TEST_MODE = os.getenv("TEST_MODE", "true").strip().lower() in ("1", "true", "yes", "on")
PORT = int(os.getenv("PORT", "10000"))


# ==================================================
# 2. НАСТРОЙКИ МОДЕРАЦИИ
# ==================================================

ALLOWED_TOPIC_ID = 18600
TARGET_GROUP_ID = -1002165454770
NEWS_TOPIC_ID = 25114
WARNING_DELETE_DELAY = 12

# Ровно девять тем, в которых публикуются объявления.
COOLDOWN_SECONDS = 5 * 60 * 60
COOLDOWN_TOPIC_NAMES = {
    21617: "Дома/гаражи",
    21619: "Авто",
    21621: "Скины",
    21622: "Аксы",
    21655: "Бизнес",
    21656: "Номерные знаки",
    21657: "SIM-card",
    21658: "Прочие услуги",
    22032: "Реклама семьи",
}
COOLDOWN_TOPIC_IDS = set(COOLDOWN_TOPIC_NAMES)

db_pool = None


# ==================================================
# 3. ЗАЩИТА ОТ ПОВТОРНЫХ UPDATE
# ==================================================

MAX_PROCESSED_UPDATES = 5000

processed_update_ids = set()
processed_update_order = deque()
pending_update_ids = set()

update_lock = asyncio.Lock()


# ==================================================
# 4. ФОНОВЫЕ ЗАДАЧИ
# ==================================================

background_tasks = set()

BOT_ID = None


# ==================================================
# 5. ПРОВЕРКА НАСТРОЕК
# ==================================================

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
# 6. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==================================================

def escape(value):
    return html.escape(
        str(value or ""),
        quote=True
    )


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


TARGET_COMMAND_SET = normalize_commands(
    TARGET_COMMANDS
)


def is_target_command(text):

    if not text or not text.strip():
        return False

    first_part = text.strip().split(
        maxsplit=1
    )[0]

    command = first_part.split(
        "@",
        1
    )[0].lower()

    return command in TARGET_COMMAND_SET


def make_check_relay_signature(
    chat_id: int,
    topic_id: int,
    original_message_id: int
) -> str:
    """
    Создаёт подпись для подтверждённой анонимной команды /check.

    Подпись проверяется Telethon Worker.
    """

    payload = (
        f"{chat_id}:{topic_id}:{original_message_id}"
    )

    return hmac.new(
        BOT_TOKEN.encode("utf-8"),
        payload.encode("utf-8"),
        "sha256"
    ).hexdigest()


# ==================================================
# 7. ПРОВЕРКА ЦЕЛЕВОЙ ГРУППЫ
# ==================================================

def is_target_group(message: Message):

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return False

    return message.chat.id == TARGET_GROUP_ID


# ==================================================
# 8. ПРОВЕРКА ИСКЛЮЧЁННОЙ ТЕМЫ
# ==================================================

def is_ignored_topic(message: Message):

    return (
        message.message_thread_id
        == ALLOWED_TOPIC_ID
    )


# ==================================================
# 9. ФОНОВЫЕ ЗАДАЧИ
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

    base_url = (
        CHAT_LINK.rstrip("/")
        .rsplit("/", 1)[0]
    )

    return f"{base_url}/{topic_id}"



# ==================================================
# 10. NEON POSTGRESQL И ТАЙМЕРЫ ОБЪЯВЛЕНИЙ
# ==================================================

async def init_database():
    global db_pool

    if not DATABASE_URL:
        raise RuntimeError(
            "Не задан DATABASE_URL. Добавьте Neon connection string "
            "в Render → Environment."
        )

    parsed_url = urlsplit(DATABASE_URL)
    safe_query = [
        (key, value)
        for key, value in parse_qsl(
            parsed_url.query,
            keep_blank_values=True
        )
        if key.lower() not in ("sslmode", "channel_binding")
    ]
    asyncpg_dsn = urlunsplit((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path,
        urlencode(safe_query),
        parsed_url.fragment,
    ))

    db_pool = await asyncpg.create_pool(
        dsn=asyncpg_dsn,
        min_size=1,
        max_size=5,
        ssl="require",
        command_timeout=30,
    )

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS announcement_timers (
                user_id BIGINT NOT NULL,
                topic_id BIGINT NOT NULL,
                published_at TIMESTAMPTZ NOT NULL,
                last_message_id BIGINT NOT NULL,
                last_media_group_id TEXT,
                PRIMARY KEY (user_id, topic_id)
            )
            """
        )
        await connection.execute(
            """
            ALTER TABLE announcement_timers
            ADD COLUMN IF NOT EXISTS last_media_group_id TEXT
            """
        )

    logger.info("Подключение main.py к Neon PostgreSQL успешно.")
    logger.info("Таблица announcement_timers проверена/создана.")


def format_remaining(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


async def register_announcement(user_id, topic_id, message_id, media_group_id=None):
    """
    Атомарно регистрирует публикацию.
    Возвращает (True, 0) при разрешённой публикации,
    (False, remaining_seconds) при активном таймере.
    Повторная доставка того же Telegram message_id не продлевает таймер.
    """
    if db_pool is None:
        raise RuntimeError("Neon PostgreSQL ещё не инициализирован.")

    lock_key = f"announcement:{user_id}:{topic_id}"

    async with db_pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                lock_key
            )

            current = await connection.fetchrow(
                """
                SELECT
                    last_message_id,
                    last_media_group_id,
                    GREATEST(
                        0,
                        CEIL(EXTRACT(
                            EPOCH FROM (
                                published_at
                                + INTERVAL '5 hours'
                                - NOW()
                            )
                        ))
                    )::BIGINT AS remaining
                FROM announcement_timers
                WHERE user_id = $1 AND topic_id = $2
                """,
                user_id,
                topic_id
            )

            if current:
                # Защита от повторной доставки того же сообщения после рестарта.
                if current["last_message_id"] == message_id:
                    return True, 0

                if (
                    media_group_id
                    and current["last_media_group_id"] == media_group_id
                ):
                    return True, 0

                remaining = int(current["remaining"] or 0)
                if remaining > 0:
                    return False, remaining

            await connection.execute(
                """
                INSERT INTO announcement_timers (
                    user_id,
                    topic_id,
                    published_at,
                    last_message_id,
                    last_media_group_id
                )
                VALUES ($1, $2, NOW(), $3, $4)
                ON CONFLICT (user_id, topic_id)
                DO UPDATE SET
                    published_at = EXCLUDED.published_at,
                    last_message_id = EXCLUDED.last_message_id,
                    last_media_group_id = EXCLUDED.last_media_group_id
                """,
                user_id,
                topic_id,
                message_id,
                media_group_id
            )

            return True, 0


async def get_announcement_remaining(user_id, topic_id):
    if db_pool is None:
        raise RuntimeError("Neon PostgreSQL ещё не инициализирован.")

    async with db_pool.acquire() as connection:
        remaining = await connection.fetchval(
            """
            SELECT GREATEST(
                0,
                CEIL(EXTRACT(
                    EPOCH FROM (
                        published_at
                        + INTERVAL '5 hours'
                        - NOW()
                    )
                ))
            )::BIGINT
            FROM announcement_timers
            WHERE user_id = $1 AND topic_id = $2
            """,
            user_id,
            topic_id
        )

    return int(remaining or 0)


async def is_group_admin(user_id):
    try:
        member = await bot.get_chat_member(
            chat_id=TARGET_GROUP_ID,
            user_id=user_id
        )
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось проверить права пользователя %s",
            user_id
        )
        return False


async def send_temporary_topic_message(message, text):
    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
            text=text,
            disable_web_page_preview=True
        )
        create_background_task(
            cleanup_warning(message.chat.id, sent.message_id)
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить ответ команды таймера")


async def delete_timer_command(message):
    """Удаляет команду управления таймером после её распознавания."""
    try:
        await message.delete()
    except TelegramAPIError:
        logger.exception(
            "Не удалось удалить команду управления таймером. "
            "Message ID: %s",
            message.message_id
        )


async def handle_timer_command(message, current_topic):
    """
    Обрабатывает /my_timer и /reset_timer только в девяти рекламных темах.
    Команда удаляется, а ответное уведомление автоматически удаляется через 12 секунд.
    Возвращает True, если сообщение было одной из этих команд.
    """
    if current_topic not in COOLDOWN_TOPIC_IDS or not message.text:
        return False

    parts = message.text.strip().split()
    if not parts:
        return False

    command = parts[0].split("@", 1)[0].lower()

    if command == "/my_timer":
        await delete_timer_command(message)

        if not message.from_user:
            return True

        remaining = await get_announcement_remaining(
            message.from_user.id,
            current_topic
        )
        topic_name = COOLDOWN_TOPIC_NAMES[current_topic]

        if remaining > 0:
            response = (
                f"⏳ В теме «{escape(topic_name)}» до следующей публикации "
                f"осталось <b>{format_remaining(remaining)}</b>."
            )
        else:
            response = (
                f"✅ В теме «{escape(topic_name)}» публикация разрешена."
            )

        await send_temporary_topic_message(message, response)
        return True

    if command != "/reset_timer":
        return False

    # Удаляем команду администратора при любом результате её обработки:
    # успешный сброс, недостаток прав, неверный аргумент или отсутствие reply.
    await delete_timer_command(message)

    if not message.from_user or not await is_group_admin(message.from_user.id):
        await send_temporary_topic_message(
            message,
            "⛔ Сбрасывать таймеры могут только администраторы и создатель группы."
        )
        return True

    target_user_id = None
    target_user_name = None

    if len(parts) == 1:
        if not message.reply_to_message:
            await send_temporary_topic_message(
                message,
                "Использование: ответьте на сообщение пользователя командой /reset_timer "
                "или укажите /reset_timer USER_ID."
            )
            return True

        replied = message.reply_to_message
        if not replied.from_user:
            await send_temporary_topic_message(
                message,
                "Не удалось определить ID автора сообщения."
            )
            return True

        target_user_id = replied.from_user.id
        target_user_name = replied.from_user.full_name

    elif len(parts) == 2 and parts[1].lower() == "all":
        async with db_pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM announcement_timers WHERE topic_id = ANY($1::BIGINT[])",
                list(COOLDOWN_TOPIC_IDS)
            )
        await send_temporary_topic_message(
            message,
            "✅ Таймеры всех пользователей во всех девяти темах сброшены."
        )
        logger.info(
            "Администратор %s сбросил все таймеры. DB result: %s",
            message.from_user.id,
            result
        )
        return True

    elif len(parts) == 2:
        try:
            target_user_id = int(parts[1])
            if target_user_id <= 0:
                raise ValueError
            # При сбросе по числовому ID пробуем получить имя через Telegram.
            # Если Telegram не отдаст профиль, используем сам ID как подпись ссылки.
            try:
                target_chat = await bot.get_chat(target_user_id)
                target_user_name = (
                    getattr(target_chat, "full_name", None)
                    or getattr(target_chat, "first_name", None)
                    or str(target_user_id)
                )
            except TelegramAPIError:
                target_user_name = str(target_user_id)
        except ValueError:
            await send_temporary_topic_message(
                message,
                "ID пользователя должен быть положительным числом."
            )
            return True
    else:
        await send_temporary_topic_message(
            message,
            "Использование: /reset_timer, /reset_timer USER_ID или /reset_timer all."
        )
        return True

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            DELETE FROM announcement_timers
            WHERE user_id = $1 AND topic_id = $2
            """,
            target_user_id,
            current_topic
        )

    display_name = escape(target_user_name or str(target_user_id))
    await send_temporary_topic_message(
        message,
        f"✅ Таймер пользователя "
        f'<a href="tg://user?id={target_user_id}">{display_name}</a> '
        f"в этой теме сброшен."
    )
    logger.info(
        "Администратор %s сбросил таймер пользователя %s в теме %s",
        message.from_user.id,
        target_user_id,
        current_topic
    )
    return True


def is_announcement_message(message):
    """
    Учитывает текст, подписи и пользовательские медиа.
    Сервисные сообщения и пустые сообщения не считаются объявлениями.
    """
    if message.text or message.caption:
        return True

    media_fields = (
        "photo",
        "video",
        "animation",
        "document",
        "audio",
        "voice",
        "video_note",
        "sticker",
        "contact",
        "location",
        "venue",
        "poll",
        "dice",
        "paid_media",
    )
    return any(getattr(message, field, None) for field in media_fields)


async def moderate_announcement(message, current_topic):
    """
    Проверка 5-часового ограничения в whitelist-темах.
    """
    if current_topic not in COOLDOWN_TOPIC_IDS:
        return False

    # Анонимные сообщения нельзя связать с конкретным user_id.
    if not message.from_user:
        logger.info(
            "Объявление без user_id пропущено системой таймеров. "
            "Message ID: %s",
            message.message_id
        )
        return True

    user_id = message.from_user.id

    # TEST_MODE=True ограничивает и администраторов/создателя.
    # При TEST_MODE=False администраторы и создатель освобождаются.
    if not TEST_MODE and await is_group_admin(user_id):
        return False

    if not is_announcement_message(message):
        return False

    try:
        allowed, remaining = await register_announcement(
            user_id=user_id,
            topic_id=current_topic,
            message_id=message.message_id,
            media_group_id=message.media_group_id
        )
    except Exception:
        logger.exception(
            "Ошибка проверки/записи таймера в Neon. "
            "Сообщение %s пользователя %s оставлено в группе.",
            message.message_id,
            user_id
        )
        return True

    if allowed:
        logger.info(
            "Объявление зарегистрировано: user_id=%s, topic_id=%s, "
            "message_id=%s, cooldown=5h",
            user_id,
            current_topic,
            message.message_id
        )
        return True

    topic_name = COOLDOWN_TOPIC_NAMES[current_topic]
    remaining_text = format_remaining(remaining)

    warning_text = (
        f"⚠️ Вы уже публиковали объявление в теме "
        f"<b>«{escape(topic_name)}»</b>!\n\n"
        f"Следующая публикация будет доступна через "
        f"<b>{remaining_text}</b>.\n\n"
        f"Пожалуйста, дождитесь окончания ограничения."
    )

    try:
        await message.delete()
        logger.info(
            "Повторное объявление удалено: user_id=%s, topic_id=%s, "
            "message_id=%s",
            user_id,
            current_topic,
            message.message_id
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось удалить повторное объявление: "
            "user_id=%s, topic_id=%s, message_id=%s",
            user_id,
            current_topic,
            message.message_id
        )

    try:
        warning = await bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=current_topic,
            text=warning_text,
            disable_web_page_preview=True
        )
        create_background_task(
            cleanup_warning(message.chat.id, warning.message_id)
        )
    except TelegramAPIError:
        logger.exception(
            "Не удалось отправить предупреждение о лимите"
        )

    return True


# ==================================================
# 10. ОТЧЁТ АДМИНИСТРАЦИИ
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

        if user_id:

            mention = (
                f'<a href="tg://user?id={user_id}">'
                f'{escape(first_name)}</a>'
            )

            user_id_text = f"<code>{user_id}</code>"

        else:

            mention = escape(first_name)

            user_id_text = (
                "Анонимно, от имени группы"
            )

        username_text = (
            f" | @{escape(username)}"
            if username
            else ""
        )

        topic_name = escape(
            TOPIC_NAMES.get(
                topic_id,
                f"Тема {topic_id}"
                if topic_id
                else "Основной чат"
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
            f"👤 <b>Нарушитель:</b> "
            f"{mention}{username_text}\n"
            f"🆔 <b>ID пользователя:</b> "
            f"{user_id_text}\n"
            f"🏷 <b>Тема нарушения:</b> "
            f"{topic_display}\n"
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
            user_id if user_id else "анонимно"
        )

    except TelegramAPIError:

        logger.exception(
            "Ошибка отправки отчёта администрации"
        )


# ==================================================
# 11. УДАЛЕНИЕ ПРЕДУПРЕЖДЕНИЯ
# ==================================================

async def cleanup_warning(
    chat_id,
    warning_id
):

    try:

        await asyncio.sleep(
            WARNING_DELETE_DELAY
        )

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
# 12. ОСНОВНАЯ МОДЕРАЦИЯ
# ==================================================

@dp.message()
async def moderate_chat(message: Message):

    # ШАГ 1. ПРОВЕРЯЕМ ГРУППУ

    if not is_target_group(message):
        return

    # ШАГ 2. ИСКЛЮЧАЕМ АДМИНСКИЙ ЧАТ

    if message.chat.id == ADMIN_CHAT_ID:
        return

    # ШАГ 3. ПОЛУЧАЕМ ID ТЕМЫ

    current_topic = message.message_thread_id or 0

    logger.info(
        "Сообщение в GameFloid: chat_id=%s, "
        "thread_id=%s, message_id=%s",
        message.chat.id,
        current_topic,
        message.message_id
    )

    # ШАГ 4. ПЕРЕДАЁМ /check TELETHON WORKER

    if (
        current_topic == NEWS_TOPIC_ID
        and message.text
    ):

        first_part = message.text.strip().split(
            maxsplit=1
        )[0]

        command = first_part.split(
            "@",
            1
        )[0].lower()

        if command == "/check":

            is_anonymous_group_sender = (
                message.sender_chat is not None
                and message.sender_chat.id == message.chat.id
            )

            if is_anonymous_group_sender:

                signature = make_check_relay_signature(
                    chat_id=message.chat.id,
                    topic_id=current_topic,
                    original_message_id=message.message_id
                )

                relay_text = (
                    f"/check_relay "
                    f"{message.message_id} "
                    f"{signature}"
                )

                try:

                    await bot.send_message(
                        chat_id=TARGET_GROUP_ID,
                        message_thread_id=NEWS_TOPIC_ID,
                        text=relay_text,
                        disable_notification=True
                    )

                    logger.info(
                        "Анонимная /check подтверждена по sender_chat. "
                        "Подписанная команда передана Worker. "
                        "Original message ID: %s",
                        message.message_id
                    )

                except TelegramAPIError:

                    logger.exception(
                        "Не удалось передать подтверждённую "
                        "анонимную /check в Telethon Worker"
                    )

                return

            logger.info(
                "Обычная команда /check передана Telethon Worker. "
                "Message ID: %s",
                message.message_id
            )

            return

    # ШАГ 5. ПОЛНОСТЬЮ ИСКЛЮЧАЕМ ТЕМУ 18600

    if is_ignored_topic(message):

        logger.info(
            "Тема %s исключена из модерации. "
            "Сообщение %s оставлено без изменений.",
            current_topic,
            message.message_id
        )

        return

    # ШАГ 6. ОПРЕДЕЛЯЕМ АНОНИМНОГО ОТПРАВИТЕЛЯ

    is_anonymous_group_sender = (
        message.sender_chat is not None
        and message.sender_chat.id == message.chat.id
    )

    # ШАГ 7. НЕ ТРОГАЕМ САМОГО СЕБЯ И ДРУГИХ БОТОВ

    if (
        message.from_user
        and message.from_user.id == BOT_ID
    ):
        return

    if (
        message.from_user
        and message.from_user.is_bot
        and not is_anonymous_group_sender
    ):
        return

    # ШАГ 8. КОМАНДЫ УПРАВЛЕНИЯ ТАЙМЕРАМИ И ЛИМИТ ОБЪЯВЛЕНИЙ
    # Работают только в девяти whitelist-темах.
    if current_topic in COOLDOWN_TOPIC_IDS:

        if await handle_timer_command(message, current_topic):
            return

        # Запрещённые команды продолжают обрабатываться старой
        # системой модерации, а не засчитываются как объявления.
        if not (message.text and is_target_command(message.text)):
            await moderate_announcement(message, current_topic)
            return

    # ШАГ 9. ПРОВЕРЯЕМ ТЕКСТ

    if not message.text:
        return

    # ШАГ 10. ПРОВЕРЯЕМ ЗАПРЕЩЁННЫЕ КОМАНДЫ

    if not is_target_command(message.text):
        return

    # ШАГ 10. СОБИРАЕМ ДАННЫЕ ОТПРАВИТЕЛЯ

    if is_anonymous_group_sender:

        user_id = None

        first_name = (
            message.sender_chat.title
            or "Администратор группы"
        )

        username = None

        logger.info(
            "Обнаружено анонимное сообщение от имени группы: %s",
            first_name
        )

    else:

        if not message.from_user:
            return

        user_id = message.from_user.id

        first_name = (
            message.from_user.first_name
            or "Пользователь"
        )

        username = message.from_user.username

    chat_id = message.chat.id
    command_text = message.text

    logger.info(
        "Запрещённая команда: %s, пользователь: %s, тема: %s",
        command_text,
        user_id if user_id else "анонимно",
        current_topic
    )

    # ШАГ 11. ТЕКСТ ПРЕДУПРЕЖДЕНИЯ

    if user_id:

        warning_mention = (
            f'<a href="tg://user?id={user_id}">'
            f'{escape(first_name)}</a>'
        )

    else:

        warning_mention = escape(first_name)

    warning_text = (
        f'⚠️ {warning_mention}, '
        f'использовать команды бота '
        f'<b>BR Assistant</b>\n'
        f'можно строго в теме '
        f'<b><a href="{escape(CHAT_LINK)}">'
        f'Чат для общения</a></b>!'
    )

    warning_id = None

    # ШАГ 12. ОТПРАВЛЯЕМ ПРЕДУПРЕЖДЕНИЕ

    try:

        warning = await bot.send_message(
            chat_id=chat_id,
            text=warning_text,
            message_thread_id=current_topic or None,
            disable_web_page_preview=True
        )

        warning_id = warning.message_id

    except TelegramAPIError:

        logger.exception(
            "Не удалось отправить предупреждение"
        )

    # ШАГ 13. УДАЛЯЕМ КОМАНДУ

    command_deleted = False

    try:

        await message.delete()

        command_deleted = True

        logger.info(
            "Команда удалена. Message ID: %s",
            message.message_id
        )

    except TelegramAPIError:

        logger.exception(
            "Не удалось удалить команду"
        )

    # ШАГ 14. ОТПРАВЛЯЕМ ОТЧЁТ АДМИНИСТРАЦИИ

    await send_admin_report(
        topic_id=current_topic,
        user_id=user_id,
        first_name=first_name,
        username=username,
        command_text=command_text,
        command_deleted=command_deleted
    )

    # ШАГ 15. УДАЛЯЕМ ПРЕДУПРЕЖДЕНИЕ ЧЕРЕЗ 12 СЕКУНД

    if warning_id is not None:

        create_background_task(
            cleanup_warning(
                chat_id,
                warning_id
            )
        )


# ==================================================
# 13. ПРОВЕРКА ДОСТУПНОСТИ RENDER
# ==================================================

async def healthcheck(request):

    return web.Response(
        text="Telegram bot server is running",
        status=200
    )


# ==================================================
# 14. ЗАЩИТА ОТ ПОВТОРНОЙ ОБРАБОТКИ UPDATE
# ==================================================

async def reserve_update(update_id):

    async with update_lock:

        if update_id in processed_update_ids:
            return "processed"

        if update_id in pending_update_ids:
            return "pending"

        pending_update_ids.add(
            update_id
        )

        return "reserved"


async def finish_update(
    update_id,
    success
):

    async with update_lock:

        pending_update_ids.discard(
            update_id
        )

        if (
            not success
            or update_id in processed_update_ids
        ):
            return

        if (
            len(processed_update_order)
            >= MAX_PROCESSED_UPDATES
        ):

            oldest = processed_update_order.popleft()

            processed_update_ids.discard(
                oldest
            )

        processed_update_order.append(
            update_id
        )

        processed_update_ids.add(
            update_id
        )


# ==================================================
# 15. TELEGRAM WEBHOOK
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

    reservation = await reserve_update(
        update_id
    )

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
# 16. ЗАПУСК СЕРВЕРА
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

        await init_database()

        bot_info = await bot.get_me()

        BOT_ID = bot_info.id

        logger.info(
            "Бот запущен: @%s, ID: %s",
            bot_info.username,
            BOT_ID
        )

        logger.info(
            "Целевая группа GameFloid ID: %s",
            TARGET_GROUP_ID
        )

        logger.info(
            "Разрешённая тема ID: %s",
            ALLOWED_TOPIC_ID
        )

        logger.info(
            "Тема новостей ID: %s",
            NEWS_TOPIC_ID
        )

        logger.info(
            "Темы с лимитом объявлений 5 часов: %s",
            COOLDOWN_TOPIC_NAMES
        )

        logger.info(
            "TEST_MODE: %s",
            TEST_MODE
        )

        logger.info(
            "Команды под модерацией: %s",
            sorted(TARGET_COMMAND_SET)
        )

        logger.info(
            "Сообщения других ботов основной бот не модерирует"
        )

        logger.info(
            "Модерация @BrAssGay передана Telethon"
        )

        logger.info(
            "Анонимные /check подтверждаются через sender_chat "
            "и подписанный relay"
        )

        logger.info(
            "Проверка подписки TendorX: ОТКЛЮЧЕНА"
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
                "message"
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

        if db_pool is not None:
            await db_pool.close()
            logger.info("Подключение main.py к Neon PostgreSQL закрыто.")

        await bot.session.close()

        logger.info(
            "Бот остановлен"
        )


# ==================================================
# 17. ТОЧКА ВХОДА
# ==================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main_async()
        )

    except (KeyboardInterrupt, SystemExit):

        logger.info(
            "Работа завершена"
        )
