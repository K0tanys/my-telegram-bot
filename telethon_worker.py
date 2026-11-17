
import asyncio
import logging
import os
import sqlite3
from contextlib import suppress

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError


# ============================================================
# 1. LOGGING AND CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("telethon_worker")

API_ID = int(os.getenv("API_ID", "38378220"))
API_HASH = os.getenv("API_HASH", "").strip()
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SOURCE_CHANNEL = os.getenv(
    "SOURCE_CHANNEL",
    "br_dev",
).strip().lstrip("@")

TARGET_CHAT_ID = -1002165454770
TARGET_BOT_ID = 8878352778

NEWS_TOPIC_ID = 25114
IGNORE_TOPIC_ID = 18600

DATABASE_PATH = os.getenv(
    "NEWS_DATABASE",
    "news_state.sqlite3",
)

if not API_HASH or not TELETHON_SESSION:
    raise RuntimeError("Не заданы API_HASH или TELETHON_SESSION")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")

if not SOURCE_CHANNEL:
    raise RuntimeError("Не задан SOURCE_CHANNEL")


# ============================================================
# 2. CLIENTS
# ============================================================

client = TelegramClient(
    StringSession(TELETHON_SESSION),
    API_ID,
    API_HASH,
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)

source_entity = None
source_channel_id = None


# ============================================================
# 3. PERSISTENT DEDUPLICATION
# ============================================================

_db = sqlite3.connect(
    DATABASE_PATH,
    check_same_thread=False,
)

_db.execute("PRAGMA journal_mode=WAL")
_db.execute("PRAGMA synchronous=NORMAL")

_db.execute(
    """
    CREATE TABLE IF NOT EXISTS published_news (
        news_key TEXT PRIMARY KEY,
        published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """
)

_db.commit()


def was_published(news_key: str) -> bool:
    row = _db.execute(
        """
        SELECT 1 FROM published_news
        WHERE news_key = ?
        LIMIT 1
        """,
        (news_key,),
    ).fetchone()

    return row is not None


def mark_published(news_key: str) -> None:
    _db.execute(
        """
        INSERT OR IGNORE INTO published_news (news_key)
        VALUES (?)
        """,
        (news_key,),
    )
    _db.commit()


# ============================================================
# 4. RUNTIME STATE
# ============================================================

publish_lock = asyncio.Lock()
check_in_progress = False
auto_keys_in_progress = set()


# ============================================================
# 5. TEXT HELPERS
# ============================================================

def utf16_length(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


def is_caption_too_long(text: str) -> bool:
    return utf16_length(text or "") > 1024


def get_media_kind(message):
    if message.photo:
        return "photo"

    document = getattr(message, "document", None)

    if document:
        from telethon.tl.types import (
            DocumentAttributeAnimated,
            DocumentAttributeVideo,
        )

        attributes = document.attributes or []

        if any(
            isinstance(attribute, DocumentAttributeAnimated)
            for attribute in attributes
        ):
            return "animation"

        if any(
            isinstance(attribute, DocumentAttributeVideo)
            for attribute in attributes
        ):
            return "video"

        return "document"

    return None


# ============================================================
# 6. TOPIC DETECTION AND MODERATION
# ============================================================

def get_topic_id(event):
    message = event.message
    reply = getattr(message, "reply_to", None)

    if not reply:
        return None

    top_id = getattr(reply, "reply_to_top_id", None)

    if top_id:
        return top_id

    reply_id = getattr(reply, "reply_to_msg_id", None)

    if reply_id:
        return reply_id

    return None


@client.on(events.NewMessage)
async def moderate_brassgay(event):
    if event.chat_id != TARGET_CHAT_ID:
        return

    if event.sender_id != TARGET_BOT_ID:
        return

    topic_id = get_topic_id(event)

    if topic_id is None:
        logger.warning(
            "Не удалось определить тему сообщения %s. Удаление отменено.",
            event.id,
        )
        return

    if topic_id == IGNORE_TOPIC_ID:
        logger.info(
            "Сообщение @BrAssGay оставлено: исключённая тема %s, message_id=%s",
            IGNORE_TOPIC_ID,
            event.id,
        )
        return

    try:
        await event.delete()
        logger.info(
            "Удалено сообщение @BrAssGay: message_id=%s, topic_id=%s",
            event.id,
            topic_id,
        )
    except Exception:
        logger.exception("Ошибка удаления сообщения @BrAssGay")


# ============================================================
# 7. TELETHON PUBLICATION
# ============================================================

async def send_telethon_text(message):
    text = message.message or ""

    if not text:
        return

    await client.send_message(
        entity=TARGET_CHAT_ID,
        message=text,
        reply_to=NEWS_TOPIC_ID,
        formatting_entities=message.entities or None,
        parse_mode=None,
        link_preview=True,
    )


async def send_media_caption_separately(message):
    if not message.message:
        return

    try:
        await send_telethon_text(message)
    except Exception:
        logger.exception(
            "Медиа %s опубликовано, но отдельная подпись не отправлена",
            message.id,
        )


async def publish_single_via_telethon(message):
    text = message.message or ""

    if not message.media:
        if text:
            await send_telethon_text(message)
        return

    caption_too_long = is_caption_too_long(text)

    kwargs = {
        "entity": TARGET_CHAT_ID,
        "file": message.media,
        "reply_to": NEWS_TOPIC_ID,
        "parse_mode": None,
    }

    if text and not caption_too_long:
        kwargs["caption"] = text
        kwargs["formatting_entities"] = message.entities or None

    await client.send_file(**kwargs)

    if caption_too_long:
        await send_media_caption_separately(message)

    logger.info(
        "Публикация отправлена через Telethon. ID=%s",
        message.id,
    )


async def publish_album_via_telethon(messages):
    ordered = sorted(messages, key=lambda item: item.id)

    if not ordered:
        return

    media_messages = [
        message for message in ordered if message.media
    ]

    if not media_messages:
        for message in ordered:
            if message.message:
                await send_telethon_text(message)
        return

    caption_message = next(
        (message for message in ordered if message.message),
        None,
    )

    caption = caption_message.message if caption_message else ""
    caption_too_long = is_caption_too_long(caption)

    kinds = [get_media_kind(message) for message in media_messages]

    has_documents = "document" in kinds
    has_other_media = any(
        kind in ("photo", "video", "animation")
        for kind in kinds
    )

    mixed_album = has_documents and has_other_media

    if mixed_album:
        logger.info(
            "Обнаружен смешанный альбом. Отправляю элементы отдельно."
        )

        for message in ordered:
            if not message.media:
                if message.message:
                    await send_telethon_text(message)
                continue

            item_text = message.message or ""
            item_caption_too_long = is_caption_too_long(item_text)

            kwargs = {
                "entity": TARGET_CHAT_ID,
                "file": message.media,
                "reply_to": NEWS_TOPIC_ID,
                "parse_mode": None,
            }

            if item_text and not item_caption_too_long:
                kwargs["caption"] = item_text
                kwargs["formatting_entities"] = message.entities or None

            await client.send_file(**kwargs)

            if item_caption_too_long:
                await send_media_caption_separately(message)

        logger.info(
            "Смешанный альбом опубликован. IDs=%s",
            [message.id for message in ordered],
        )
        return

    media = [message.media for message in media_messages]

    kwargs = {
        "entity": TARGET_CHAT_ID,
        "file": media,
        "reply_to": NEWS_TOPIC_ID,
        "parse_mode": None,
    }

    if caption and not caption_too_long:
        kwargs["caption"] = caption
        kwargs["formatting_entities"] = (
            caption_message.entities or None
        )

    await client.send_file(**kwargs)

    if caption_too_long and caption_message:
        await send_media_caption_separately(caption_message)

    logger.info(
        "Альбом опубликован через Telethon. IDs=%s",
        [message.id for message in ordered],
    )


async def publish_news(messages):
    ordered = sorted(messages, key=lambda item: item.id)

    if not ordered:
        return

    if len(ordered) == 1:
        await publish_single_via_telethon(ordered[0])
    else:
        await publish_album_via_telethon(ordered)


# ============================================================
# 8. NEWS KEYS AND AUTOMATIC PUBLICATION
# ============================================================

def make_news_key(messages) -> str:
    ordered = sorted(messages, key=lambda item: item.id)

    grouped_id = getattr(ordered[0], "grouped_id", None)

    if grouped_id:
        return f"album:{source_channel_id}:{grouped_id}"

    return f"message:{source_channel_id}:{ordered[0].id}"


async def process_automatic_news(messages):
    ordered = sorted(messages, key=lambda item: item.id)

    if not ordered:
        return

    key = make_news_key(ordered)

    if key in auto_keys_in_progress:
        logger.info("Публикация уже обрабатывается: %s", key)
        return

    auto_keys_in_progress.add(key)

    try:
        async with publish_lock:
            if was_published(key):
                logger.info("Автоматический дубль пропущен: %s", key)
                return

            logger.info("Начинаю автоматическую публикацию: %s", key)

            await publish_news(ordered)
            mark_published(key)

            logger.info("Автоматическая публикация завершена: %s", key)

    except Exception:
        logger.exception("Ошибка автоматической публикации: %s", key)

    finally:
        auto_keys_in_progress.discard(key)


# ============================================================
# 9. SOURCE CHANNEL HANDLERS
# ============================================================

@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handle_source_message(event):
    if event.message.grouped_id:
        return

    await process_automatic_news([event.message])


@client.on(events.Album(chats=SOURCE_CHANNEL))
async def handle_source_album(event):
    if not event.messages:
        return

    await process_automatic_news(event.messages)


# ============================================================
# 10. /CHECK — ADMIN AND ANONYMOUS ADMIN SUPPORT
# ============================================================

def get_anonymous_sender_debug(event) -> dict:
    """
    Собирает диагностические поля сообщения.

    Важно:
    peer_id показывает адресата сообщения,
    post_author является подписью.
    Ни одно из этих полей само по себе
    не подтверждает права администратора.
    """

    message = event.message
    from_id = getattr(message, "from_id", None)
    peer_id = getattr(message, "peer_id", None)

    resolved_from_id = None
    resolved_peer_id = None

    try:
        if from_id is not None:
            resolved_from_id = utils.get_peer_id(from_id)
    except Exception as error:
        resolved_from_id = f"ERROR: {error}"

    try:
        if peer_id is not None:
            resolved_peer_id = utils.get_peer_id(peer_id)
    except Exception as error:
        resolved_peer_id = f"ERROR: {error}"

    return {
        "event_chat_id": event.chat_id,
        "sender_id": event.sender_id,
        "from_id": repr(from_id),
        "from_id_resolved": resolved_from_id,
        "peer_id": repr(peer_id),
        "peer_id_resolved": resolved_peer_id,
        "post": getattr(message, "post", None),
        "post_author": getattr(message, "post_author", None),
        "via_bot_id": getattr(message, "via_bot_id", None),
        "fwd_from": repr(getattr(message, "fwd_from", None)),
        "reply_to": repr(getattr(message, "reply_to", None)),
    }


def is_anonymous_group_admin(event) -> bool:
    """
    Не разрешает анонимную команду без подтверждения прав.

    Для полноценной поддержки анонимных администраторов
    потребуется проверка исходного сообщения в main.py.
    """

    debug = get_anonymous_sender_debug(event)

    logger.warning(
        "ANONYMOUS DEBUG: %s",
        debug,
    )

    from_id = getattr(event.message, "from_id", None)

    if from_id is not None:
        try:
            resolved_from_id = utils.get_peer_id(from_id)

            if resolved_from_id == TARGET_CHAT_ID:
                logger.warning(
                    "Обнаружен from_id целевой группы. "
                    "Это ещё не подтверждает права администратора."
                )

        except Exception:
            logger.exception(
                "Не удалось определить from_id сообщения"
            )

    return False


async def is_group_admin(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(
            chat_id=TARGET_CHAT_ID,
            user_id=user_id,
        )

        status = getattr(
            member.status,
            "value",
            member.status,
        )

        return status in ("administrator", "creator")

    except TelegramAPIError:
        logger.exception(
            "Не удалось проверить права пользователя %s",
            user_id,
        )
        return False


async def get_latest_source_post():
    latest_messages = await client.get_messages(
        source_entity,
        limit=100,
    )

    if not latest_messages:
        return []

    latest = next(
        (
            message
            for message in latest_messages
            if message.message or message.media
        ),
        None,
    )

    if latest is None:
        return []

    if latest.grouped_id:
        album = [
            message
            for message in latest_messages
            if message.grouped_id == latest.grouped_id
        ]

        return sorted(album, key=lambda item: item.id)

    return [latest]


@client.on(events.NewMessage(chats=TARGET_CHAT_ID))
async def handle_check(event):
    global check_in_progress

    text = event.raw_text or ""

    if not text.strip():
        return

    first_token = text.strip().split(maxsplit=1)[0]
    command = first_token.split("@", 1)[0].lower()

    if command != "/check":
        return

    topic_id = get_topic_id(event)

    logger.info(
        "Обнаружена команда /check: message_id=%s, "
        "sender_id=%s, topic_id=%s",
        event.id,
        event.sender_id,
        topic_id,
    )

    anonymous_admin = is_anonymous_group_admin(event)

    # Команда работает только в теме новостей.
    if topic_id != NEWS_TOPIC_ID:
        logger.warning(
            "/check отклонена: неправильная тема. "
            "Получено: %s, ожидается: %s",
            topic_id,
            NEWS_TOPIC_ID,
        )
        return

    # Не допускаем одновременные публикации.
    if check_in_progress or publish_lock.locked():
        with suppress(Exception):
            await event.delete()

        logger.warning(
            "Повторная /check пропущена: публикация уже выполняется"
        )
        return

    check_in_progress = True

    try:
        user_id = event.sender_id

        # Анонимный или неизвестный отправитель.
        # Не разрешаем публикацию без подтверждения из main.py.
        if anonymous_admin or not user_id:
            logger.warning(
                "/check от анонимного или неизвестного отправителя. "
                "Публикация запрещена до подтверждения прав."
            )

            try:
                await event.delete()
                logger.info(
                    "Неподтверждённая команда /check удалена."
                )
            except Exception:
                logger.exception(
                    "Не удалось удалить неподтверждённую команду /check"
                )

            return

        # Проверяем права обычного пользователя.
        if not await is_group_admin(user_id):
            logger.warning(
                "Отказ в /check: пользователь %s не является администратором",
                user_id,
            )

            try:
                await event.delete()
                logger.info(
                    "Команда /check от неадминистратора удалена."
                )
            except Exception:
                logger.exception(
                    "Не удалось удалить команду неадминистратора"
                )

            return

        logger.info(
            "Права администратора подтверждены. User ID: %s",
            user_id,
        )

        # Удаляем команду после проверки прав.
        try:
            await event.delete()
            logger.info(
                "Команда /check удалена. message_id=%s",
                event.id,
            )
        except Exception:
            logger.exception(
                "Не удалось удалить команду /check"
            )

        # Получаем и публикуем последнюю новость.
        async with publish_lock:
            latest_post = await get_latest_source_post()

            if not latest_post:
                logger.warning(
                    "В источнике нет публикаций для /check"
                )
                return

            logger.info(
                "Последняя публикация получена для /check. IDs=%s",
                [message.id for message in latest_post],
            )

            await publish_news(latest_post)

            mark_published(make_news_key(latest_post))

            logger.info(
                "Принудительная публикация /check завершена."
            )

    except Exception:
        logger.exception("Ошибка обработки /check")

    finally:
        check_in_progress = False


# ============================================================
# 11. STARTUP AND SHUTDOWN
# ============================================================

async def main():
    global source_entity, source_channel_id

    logger.info("Запуск Telethon Worker...")

    try:
        await client.start()

        telethon_me = await client.get_me()

        logger.info(
            "Telethon подключён. Личный аккаунт ID: %s",
            telethon_me.id,
        )

        bot_me = await bot.get_me()

        logger.info(
            "Bot API подключён: @%s, ID: %s",
            bot_me.username,
            bot_me.id,
        )

        source_entity = await client.get_entity(SOURCE_CHANNEL)

        source_channel_id = (
            getattr(source_entity, "channel_id", None)
            or source_entity.id
        )

        logger.info(
            "Источник новостей: @%s, ID: %s",
            SOURCE_CHANNEL,
            source_channel_id,
        )

        await client.get_entity(TARGET_CHAT_ID)

        logger.info("Целевая группа: %s", TARGET_CHAT_ID)
        logger.info("Тема новостей: %s", NEWS_TOPIC_ID)
        logger.info("Исключённая тема модерации: %s", IGNORE_TOPIC_ID)
        logger.info("Модерация @BrAssGay включена")
        logger.info("Автоматические новости и /check включены")
        logger.info(
            "Способ публикации: Telethon send_file "
            "с прямым использованием исходного media"
        )
        logger.info(
            "Локальное скачивание медиа отключено"
        )

        await client.run_until_disconnected()

    finally:
        logger.info("Остановка Telethon Worker...")

        await bot.session.close()
        _db.close()

        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа Telethon Worker завершена")
