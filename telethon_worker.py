
import asyncio
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from contextlib import suppress

from telethon import TelegramClient, events, functions, helpers
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeVideo,
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityUnderline,
    MessageEntityStrike,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageEntityMention,
    MessageEntityHashtag,
    MessageEntityCashtag,
    MessageEntityBotCommand,
    MessageEntityEmail,
    MessageEntityPhone,
    MessageEntitySpoiler,
    MessageEntityCustomEmoji,
)

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramEntityTooLarge
from aiogram.types import (
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    FSInputFile,
)


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
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "br_dev").strip().lstrip("@")

# These are intentionally explicit. Render's TOPIC_ID=18600 is the
# moderation-excluded topic, NOT the news destination topic.
TARGET_CHAT_ID = -1002165454770
TARGET_BOT_ID = 8878352778
NEWS_TOPIC_ID = 25114
IGNORE_TOPIC_ID = 18600

DATABASE_PATH = os.getenv("NEWS_DATABASE", "news_state.sqlite3")

# Conservative Bot API upload thresholds.
PHOTO_LIMIT = 10 * 1024 * 1024
GENERAL_FILE_LIMIT = 50 * 1024 * 1024

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

_db = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
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
        "SELECT 1 FROM published_news WHERE news_key = ? LIMIT 1",
        (news_key,),
    ).fetchone()
    return row is not None


def mark_published(news_key: str) -> None:
    _db.execute(
        "INSERT OR IGNORE INTO published_news (news_key) VALUES (?)",
        (news_key,),
    )
    _db.commit()


# ============================================================
# 4. RUNTIME STATE AND ENTITY CONVERSION
# ============================================================

publish_lock = asyncio.Lock()
check_in_progress = False
auto_keys_in_progress = set()

ENTITY_TYPES = {
    MessageEntityBold: "bold",
    MessageEntityItalic: "italic",
    MessageEntityUnderline: "underline",
    MessageEntityStrike: "strikethrough",
    MessageEntityCode: "code",
    MessageEntityPre: "pre",
    MessageEntityTextUrl: "text_link",
    MessageEntityUrl: "url",
    MessageEntityMention: "mention",
    MessageEntityHashtag: "hashtag",
    MessageEntityCashtag: "cashtag",
    MessageEntityBotCommand: "bot_command",
    MessageEntityEmail: "email",
    MessageEntityPhone: "phone_number",
    MessageEntitySpoiler: "spoiler",
    MessageEntityCustomEmoji: "custom_emoji",
}


def convert_entities(entities):
    """Convert supported Telethon entities to Bot API entities."""
    if not entities:
        return None

    converted = []
    for entity in entities:
        entity_type = ENTITY_TYPES.get(type(entity))
        if not entity_type:
            continue

        kwargs = {
            "type": entity_type,
            "offset": entity.offset,
            "length": entity.length,
        }

        if isinstance(entity, MessageEntityTextUrl):
            kwargs["url"] = entity.url
        elif isinstance(entity, MessageEntityPre) and entity.language:
            kwargs["language"] = entity.language
        elif isinstance(entity, MessageEntityCustomEmoji):
            kwargs["custom_emoji_id"] = str(entity.document_id)

        converted.append(MessageEntity(**kwargs))

    return converted or None


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def is_caption_too_long(text: str) -> bool:
    return utf16_length(text or "") > 1024


# ============================================================
# 5. TOPIC DETECTION AND ORIGINAL MODERATION
# ============================================================

def get_topic_id(event):
    """Return the forum topic root ID, or None if it cannot be determined."""
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
    # Keep the original moderation scope unchanged.
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
            "Тема %s исключена. Сообщение %s оставлено.",
            IGNORE_TOPIC_ID,
            event.id,
        )
        return

    try:
        await event.delete()
        logger.info(
            "Удалено сообщение @BrAssGay: %s, тема: %s",
            event.id,
            topic_id,
        )
    except Exception:
        logger.exception("Ошибка удаления сообщения @BrAssGay")


# ============================================================
# 6. SOURCE MESSAGE / MEDIA HELPERS
# ============================================================

def get_media_kind(message):
    if message.photo:
        return "photo"

    document = getattr(message, "document", None)

    if document:
        attrs = document.attributes or []

        if any(isinstance(a, DocumentAttributeAnimated) for a in attrs):
            return "animation"

        if any(isinstance(a, DocumentAttributeVideo) for a in attrs):
            return "video"

        return "document"

    return None


def get_media_size(message) -> int:
    try:
        return int(message.file.size or 0) if message.file else 0
    except Exception:
        return 0


def exceeds_known_upload_limit(message) -> bool:
    kind = get_media_kind(message)
    size = get_media_size(message)

    if not kind or not size:
        return False

    if kind == "photo":
        return size > PHOTO_LIMIT

    return size > GENERAL_FILE_LIMIT


def is_size_error(error: Exception) -> bool:
    if isinstance(error, TelegramEntityTooLarge):
        return True

    message = str(error).lower()

    phrases = (
        "file is too big",
        "file too large",
        "request entity too large",
        "entity too large",
    )

    return any(phrase in message for phrase in phrases)


async def download_media_to_path(message, directory: str) -> str:
    extension = ".bin"

    try:
        if message.file and message.file.ext:
            extension = message.file.ext
    except Exception:
        pass

    path = str(Path(directory) / f"{message.id}{extension}")

    downloaded = await client.download_media(message, file=path)

    if not downloaded:
        raise RuntimeError(
            f"Не удалось скачать медиа исходного сообщения {message.id}"
        )

    return path


# ============================================================
# 7. TELETHON FALLBACK
# ============================================================

async def publish_via_telethon(messages):
    """
    Forward original source messages directly into the news forum topic.

    Telethon 1.45.0 high-level forward_messages() has no reply_to argument.
    Raw MTProto ForwardMessagesRequest supports top_msg_id.
    """

    if not source_entity:
        raise RuntimeError("Источник новостей ещё не инициализирован")

    ordered = sorted(messages, key=lambda item: item.id)

    if not ordered:
        return

    logger.warning(
        "Bot API не может принять файл/альбом. "
        "Переключаюсь на Telethon; IDs=%s",
        [m.id for m in ordered],
    )

    source_peer = await client.get_input_entity(source_entity)
    target_peer = await client.get_input_entity(TARGET_CHAT_ID)

    request = functions.messages.ForwardMessagesRequest(
        from_peer=source_peer,
        id=[message.id for message in ordered],
        random_id=[
            helpers.generate_random_long()
            for _ in ordered
        ],
        to_peer=target_peer,
        top_msg_id=NEWS_TOPIC_ID,
    )

    await client(request)

    logger.info(
        "Резервная публикация через Telethon завершена "
        "в теме %s; IDs=%s",
        NEWS_TOPIC_ID,
        [m.id for m in ordered],
    )


# ============================================================
# 8. BOT API TEXT AND SINGLE MEDIA
# ============================================================

async def send_text_via_bot(message):
    text = message.message or ""

    if not text:
        return

    await bot.send_message(
        chat_id=TARGET_CHAT_ID,
        message_thread_id=NEWS_TOPIC_ID,
        text=text,
        entities=convert_entities(message.entities),
    )


async def send_single_media(message):
    kind = get_media_kind(message)

    if not kind:
        # Unknown media is safer to forward as-is.
        if message.media:
            await publish_via_telethon([message])
        else:
            await send_text_via_bot(message)

        return

    # GIF/animation uses Telethon fallback.
    if kind == "animation" or exceeds_known_upload_limit(message):
        await publish_via_telethon([message])
        return

    original_caption = message.message or ""
    caption_too_long = is_caption_too_long(original_caption)

    caption = "" if caption_too_long else original_caption

    caption_entities = (
        None
        if caption_too_long
        else convert_entities(message.entities)
    )

    with tempfile.TemporaryDirectory(prefix="news_") as directory:
        path = await download_media_to_path(message, directory)
        upload = FSInputFile(path)

        try:
            common = {
                "chat_id": TARGET_CHAT_ID,
                "message_thread_id": NEWS_TOPIC_ID,
                "caption": caption or None,
                "caption_entities": caption_entities,
            }

            if kind == "photo":
                await bot.send_photo(
                    photo=upload,
                    **common,
                )

            elif kind == "video":
                await bot.send_video(
                    video=upload,
                    **common,
                )

            else:
                await bot.send_document(
                    document=upload,
                    **common,
                )

        except Exception as error:
            # Only confirmed size errors trigger fallback.
            if is_size_error(error):
                await publish_via_telethon([message])
                return

            raise

    # Media is already published. Do not resend it if the separate
    # long caption fails.
    if caption_too_long:
        try:
            await send_text_via_bot(message)
        except Exception:
            logger.exception(
                "Медиа %s опубликовано, но не удалось отправить "
                "длинную подпись отдельным сообщением",
                message.id,
            )


# ============================================================
# 9. BOT API ALBUMS
# ============================================================

async def send_album(messages):
    ordered = sorted(messages, key=lambda item: item.id)

    # Bot API media groups accept 2-10 items.
    if not 2 <= len(ordered) <= 10:
        await publish_via_telethon(ordered)
        return

    kinds = [get_media_kind(m) for m in ordered]

    if any(kind not in ("photo", "video", "document") for kind in kinds):
        await publish_via_telethon(ordered)
        return

    if any(kind == "animation" for kind in kinds):
        await publish_via_telethon(ordered)
        return

    if any(exceeds_known_upload_limit(m) for m in ordered):
        await publish_via_telethon(ordered)
        return

    kind_set = set(kinds)

    # Telegram does not permit mixing document-type items with
    # photo/video items in one media group.
    if "document" in kind_set and len(kind_set) > 1:
        await publish_via_telethon(ordered)
        return

    caption_message = next(
        (m for m in ordered if m.message),
        None,
    )

    caption_text = caption_message.message if caption_message else ""

    caption_too_long = is_caption_too_long(caption_text)
    caption = "" if caption_too_long else caption_text

    caption_entities = (
        None
        if caption_too_long or not caption_message
        else convert_entities(caption_message.entities)
    )

    with tempfile.TemporaryDirectory(prefix="news_album_") as directory:
        media_items = []

        for index, message in enumerate(ordered):
            path = await download_media_to_path(message, directory)
            upload = FSInputFile(path)

            item_caption = caption if index == 0 else None
            item_entities = caption_entities if index == 0 else None

            kind = get_media_kind(message)

            if kind == "photo":
                item = InputMediaPhoto(
                    media=upload,
                    caption=item_caption,
                    caption_entities=item_entities,
                )

            elif kind == "video":
                item = InputMediaVideo(
                    media=upload,
                    caption=item_caption,
                    caption_entities=item_entities,
                )

            else:
                item = InputMediaDocument(
                    media=upload,
                    caption=item_caption,
                    caption_entities=item_entities,
                )

            media_items.append(item)

        try:
            await bot.send_media_group(
                chat_id=TARGET_CHAT_ID,
                message_thread_id=NEWS_TOPIC_ID,
                media=media_items,
            )

        except Exception as error:
            if is_size_error(error):
                await publish_via_telethon(ordered)
                return

            raise

    if caption_too_long and caption_message:
        try:
            await send_text_via_bot(caption_message)
        except Exception:
            logger.exception(
                "Альбом %s опубликован, но не удалось отправить "
                "длинную подпись отдельно",
                ordered[0].grouped_id,
            )


# ============================================================
# 10. COMMON PUBLICATION AND DEDUPLICATION
# ============================================================

def make_news_key(messages) -> str:
    ordered = sorted(messages, key=lambda item: item.id)

    grouped_id = getattr(ordered[0], "grouped_id", None)

    if grouped_id:
        return f"album:{source_channel_id}:{grouped_id}"

    return f"message:{source_channel_id}:{ordered[0].id}"


async def publish_news(messages):
    ordered = sorted(messages, key=lambda item: item.id)

    if not ordered:
        return

    if len(ordered) == 1:
        await send_single_media(ordered[0])
    else:
        await send_album(ordered)


async def process_automatic_news(messages):
    ordered = sorted(messages, key=lambda item: item.id)

    if not ordered:
        return

    key = make_news_key(ordered)

    if key in auto_keys_in_progress:
        return

    auto_keys_in_progress.add(key)

    try:
        async with publish_lock:
            if was_published(key):
                logger.info(
                    "Автоматический дубль пропущен: %s",
                    key,
                )
                return

            await publish_news(ordered)

            mark_published(key)

            logger.info(
                "Автоматическая публикация завершена: %s",
                key,
            )

    except Exception:
        logger.exception(
            "Ошибка автоматической публикации %s",
            key,
        )

    finally:
        auto_keys_in_progress.discard(key)


# ============================================================
# 11. SOURCE CHANNEL HANDLERS
# ============================================================

@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handle_source_message(event):
    # Album events are processed once by the Album handler.
    if event.message.grouped_id:
        return

    await process_automatic_news([event.message])


@client.on(events.Album(chats=SOURCE_CHANNEL))
async def handle_source_album(event):
    if not event.messages:
        return

    await process_automatic_news(event.messages)


# ============================================================
# 12. /check COMMAND
# ============================================================

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
    """Get latest channel post; if album, collect its complete group."""

    latest_messages = await client.get_messages(
        source_entity,
        limit=30,
    )

    if not latest_messages:
        return []

    latest = next(
        (m for m in latest_messages if m.message or m.media),
        None,
    )

    if latest is None:
        return []

    if latest.grouped_id:
        album = [
            m
            for m in latest_messages
            if m.grouped_id == latest.grouped_id
        ]

        # If the album straddles the 30-message lookback boundary,
        # fetch neighboring messages around its first known member.
        if album:
            min_id = min(m.id for m in album)
            max_id = max(m.id for m in album)

            if len(album) < 10:
                nearby = await client.get_messages(
                    source_entity,
                    min_id=max_id - 10,
                    max_id=min_id + 10,
                    limit=30,
                )

                by_id = {
                    m.id: m
                    for m in album
                }

                for item in nearby:
                    if item.grouped_id == latest.grouped_id:
                        by_id[item.id] = item

                album = list(by_id.values())

        return sorted(
            album,
            key=lambda item: item.id,
        )

    return [latest]


@client.on(events.NewMessage(chats=TARGET_CHAT_ID))
async def handle_check(event):
    global check_in_progress

    text = event.raw_text or ""

    if not text.strip():
        return

    if get_topic_id(event) != NEWS_TOPIC_ID:
        return

    first_token = text.strip().split(maxsplit=1)[0]
    command = first_token.split("@", 1)[0].lower()

    if command != "/check":
        return

    logger.info(
        "Получена команда /check: message_id=%s, "
        "user_id=%s, topic_id=%s",
        event.id,
        event.sender_id,
        get_topic_id(event),
    )

    # Duplicate requests are deleted and discarded immediately.
    if check_in_progress or publish_lock.locked():
        with suppress(Exception):
            await event.delete()

        logger.warning(
            "Повторная /check пропущена: публикация уже выполняется"
        )
        return

    check_in_progress = True

    # Delete the accepted command before network work/admin check.
    try:
        await event.delete()

    except Exception:
        logger.exception(
            "Не удалось удалить команду /check"
        )
        check_in_progress = False
        return

    try:
        user_id = event.sender_id

        if not user_id:
            logger.warning(
                "/check без определённого отправителя"
            )
            return

        if not await is_group_admin(user_id):
            logger.warning(
                "Неавторизованная попытка /check. User ID: %s",
                user_id,
            )
            return

        logger.info(
            "Права администратора для /check подтверждены. "
            "User ID: %s",
            user_id,
        )

        # No waiting queue.
        if publish_lock.locked():
            logger.warning(
                "/check пропущена: в это время началась другая публикация"
            )
            return

        await publish_lock.acquire()

        try:
            latest_post = await get_latest_source_post()

            if not latest_post:
                logger.warning(
                    "В источнике нет публикаций для /check"
                )
                return

            logger.info(
                "Последняя публикация источника получена для /check; "
                "IDs=%s",
                [message.id for message in latest_post],
            )

            # Manual /check deliberately bypasses automatic deduplication.
            await publish_news(latest_post)

            logger.info(
                "Принудительная публикация /check завершена. "
                "User ID: %s",
                user_id,
            )

        finally:
            publish_lock.release()

    except Exception:
        logger.exception(
            "Ошибка обработки /check"
        )

    finally:
        check_in_progress = False


# ============================================================
# 13. STARTUP AND SHUTDOWN
# ============================================================

async def main():
    global source_entity, source_channel_id

    logger.info("Запуск Telethon Worker...")

    await client.start()

    telethon_me = await client.get_me()

    logger.info(
        "Telethon подключён. Аккаунт ID: %s",
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

    logger.info(
        "Целевая группа: %s",
        TARGET_CHAT_ID,
    )

    logger.info(
        "Тема новостей: %s",
        NEWS_TOPIC_ID,
    )

    logger.info(
        "Исключённая тема модерации: %s",
        IGNORE_TOPIC_ID,
    )

    logger.info("Модерация @BrAssGay включена")
    logger.info("Автоматические новости и /check включены")

    logger.info(
        "Публикация: Bot API с резервным Telethon для больших файлов"
    )

    try:
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
        logger.info(
            "Работа Telethon Worker завершена"
        )
