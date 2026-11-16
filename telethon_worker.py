
import os
import logging
import asyncio

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeVideo,
    DocumentAttributeFilename,
    DocumentAttributeAnimated,
)

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BufferedInputFile,
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    LinkPreviewOptions,
)


# ==================================================
# 1. ЛОГИРОВАНИЕ
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ==================================================
# 2. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==================================================

API_ID = int(os.getenv("API_ID", "38378220"))
API_HASH = os.getenv("API_HASH")
TELETHON_SESSION = os.getenv("TELETHON_SESSION")
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not API_HASH or not TELETHON_SESSION:
    logger.error("Отсутствует API_HASH или TELETHON_SESSION")
    raise SystemExit(1)

if not BOT_TOKEN:
    logger.error("Отсутствует BOT_TOKEN")
    raise SystemExit(1)

session = StringSession(TELETHON_SESSION.strip())


# ==================================================
# 3. НАСТРОЙКИ МОДЕРАЦИИ
# ==================================================

TARGET_CHAT_ID = -1002165454770
TARGET_BOT_ID = 8878352778
IGNORE_TOPIC_ID = 18600


# ==================================================
# 4. НАСТРОЙКИ НОВОСТЕЙ
# ==================================================

SOURCE_CHANNEL = "br_dev"

NEWS_TARGET_CHAT_ID = -1002165454770
NEWS_TOPIC_ID = 25114


# ==================================================
# 5. КЛИЕНТЫ
# ==================================================

client = TelegramClient(
    session,
    API_ID,
    API_HASH.strip()
)

bot = Bot(
    token=BOT_TOKEN.strip(),
    default=DefaultBotProperties(
        parse_mode=None
    )
)

news_lock = asyncio.Lock()


# ==================================================
# 6. ОПРЕДЕЛЕНИЕ ID ТЕМЫ
# ==================================================

def get_topic_id(event):

    message = event.message
    reply = message.reply_to

    if not reply:
        return None

    top_id = getattr(reply, "reply_to_top_id", None)

    if top_id:
        return top_id

    reply_id = getattr(reply, "reply_to_msg_id", None)

    if reply_id:
        return reply_id

    return None


# ==================================================
# 7. ПРЕОБРАЗОВАНИЕ ФОРМАТИРОВАНИЯ
# ==================================================

def convert_entities(entities):

    if not entities:
        return None

    entity_mapping = {
        "MessageEntityBold": "bold",
        "MessageEntityItalic": "italic",
        "MessageEntityUnderline": "underline",
        "MessageEntityStrike": "strikethrough",
        "MessageEntitySpoiler": "spoiler",
        "MessageEntityCode": "code",
        "MessageEntityPre": "pre",
        "MessageEntityTextUrl": "text_link",
        "MessageEntityUrl": "url",
        "MessageEntityMention": "mention",
        "MessageEntityHashtag": "hashtag",
        "MessageEntityCashtag": "cashtag",
        "MessageEntityBotCommand": "bot_command",
        "MessageEntityEmail": "email",
        "MessageEntityPhone": "phone_number",
        "MessageEntityBankCard": "bank_card",
        "MessageEntityCustomEmoji": "custom_emoji",
        "MessageEntityBlockquote": "blockquote",
    }

    result = []

    for entity in entities:

        class_name = entity.__class__.__name__
        entity_type = entity_mapping.get(class_name)

        # Упоминание пользователя с ID.
        if class_name == "MessageEntityMentionName":

            entity_type = "text_link"

        if not entity_type:
            continue

        data = {
            "type": entity_type,
            "offset": entity.offset,
            "length": entity.length,
        }

        if class_name == "MessageEntityTextUrl":
            data["url"] = entity.url

        elif class_name == "MessageEntityMentionName":
            data["url"] = f"tg://user?id={entity.user_id}"

        elif class_name == "MessageEntityPre":
            data["language"] = entity.language or ""

        elif class_name == "MessageEntityCustomEmoji":
            data["custom_emoji_id"] = str(entity.document_id)

        result.append(MessageEntity(**data))

    return result or None


# ==================================================
# 8. ПОЛУЧЕНИЕ ИНФОРМАЦИИ О ФАЙЛЕ
# ==================================================

def get_document_info(message):

    document = message.document

    filename = None
    is_video = False
    is_animation = False

    if document:

        for attribute in document.attributes:

            if isinstance(attribute, DocumentAttributeFilename):
                filename = attribute.file_name

            elif isinstance(attribute, DocumentAttributeVideo):
                is_video = True

            elif isinstance(attribute, DocumentAttributeAnimated):
                is_animation = True

    if not filename:

        if is_video:
            filename = f"video_{message.id}.mp4"
        else:
            filename = f"document_{message.id}"

    return filename, is_video, is_animation


# ==================================================
# 9. ПОДГОТОВКА МЕДИА ДЛЯ BOT API
# ==================================================

async def prepare_media(message):

    if message.photo:

        file_data = await client.download_media(
            message,
            file=bytes
        )

        if not file_data:
            return None

        file = BufferedInputFile(
            file_data,
            filename=f"photo_{message.id}.jpg"
        )

        return {
            "type": "photo",
            "file": file,
        }

    if message.document:

        file_data = await client.download_media(
            message,
            file=bytes
        )

        if not file_data:
            return None

        filename, is_video, is_animation = get_document_info(message)

        file = BufferedInputFile(
            file_data,
            filename=filename
        )

        if is_video:

            return {
                "type": "video",
                "file": file,
            }

        return {
            "type": "document",
            "file": file,
        }

    return None


# ==================================================
# 10. ОТПРАВКА ТЕКСТА
# ==================================================

async def send_news_text(text, entities=None):

    if not text:
        return

    await bot.send_message(
        chat_id=NEWS_TARGET_CHAT_ID,
        message_thread_id=NEWS_TOPIC_ID,
        text=text,
        entities=convert_entities(entities),
        link_preview_options=LinkPreviewOptions(
            is_disabled=False
        )
    )


# ==================================================
# 11. ОТПРАВКА ОДНОЙ НОВОСТИ
# ==================================================

async def copy_single_news(message):

    try:

        text = message.message or ""
        entities = message.entities or []

        if message.photo or message.document:

            media = await prepare_media(message)

            if not media:
                logger.error(
                    "Не удалось скачать медиа публикации %s",
                    message.id
                )
                return False

            caption = text if len(text) <= 1024 else None
            caption_entities = (
                convert_entities(entities)
                if caption
                else None
            )

            if media["type"] == "photo":

                await bot.send_photo(
                    chat_id=NEWS_TARGET_CHAT_ID,
                    message_thread_id=NEWS_TOPIC_ID,
                    photo=media["file"],
                    caption=caption,
                    caption_entities=caption_entities
                )

            elif media["type"] == "video":

                await bot.send_video(
                    chat_id=NEWS_TARGET_CHAT_ID,
                    message_thread_id=NEWS_TOPIC_ID,
                    video=media["file"],
                    caption=caption,
                    caption_entities=caption_entities
                )

            else:

                await bot.send_document(
                    chat_id=NEWS_TARGET_CHAT_ID,
                    message_thread_id=NEWS_TOPIC_ID,
                    document=media["file"],
                    caption=caption,
                    caption_entities=caption_entities
                )

            # Если подпись длиннее лимита Bot API,
            # отправляем полный текст отдельным сообщением.
            if text and len(text) > 1024:
                await send_news_text(text, entities)

        elif text:

            await send_news_text(text, entities)

        else:

            logger.warning(
                "Пустая публикация %s пропущена.",
                message.id
            )
            return False

        logger.info(
            "Новость опубликована от имени GameFloid. "
            "Источник: %s",
            message.id
        )

        return True

    except Exception:

        logger.exception(
            "Ошибка копирования новости %s",
            message.id
        )
        return False


# ==================================================
# 12. ОТПРАВКА АЛЬБОМА
# ==================================================

async def copy_album_news(messages):

    try:

        messages = sorted(
            messages,
            key=lambda item: item.id
        )

        prepared = []

        for message in messages:

            media = await prepare_media(message)

            if media:
                prepared.append((message, media))

        if not prepared:
            logger.warning("В альбоме нет доступных медиа.")
            return False

        # Подпись ищем среди всех элементов альбома.
        caption_message = next(
            (
                message
                for message in messages
                if message.message
            ),
            None
        )

        caption = (
            caption_message.message
            if caption_message
            else None
        )

        caption_entities = (
            caption_message.entities or []
            if caption_message
            else []
        )

        # Подпись Bot API ограничена 1024 символами.
        use_caption = bool(caption) and len(caption) <= 1024

        media_group = []

        for index, (message, media) in enumerate(prepared):

            item_caption = (
                caption
                if index == 0 and use_caption
                else None
            )

            item_entities = (
                convert_entities(caption_entities)
                if item_caption
                else None
            )

            if media["type"] == "photo":

                item = InputMediaPhoto(
                    media=media["file"],
                    caption=item_caption,
                    caption_entities=item_entities
                )

            elif media["type"] == "video":

                item = InputMediaVideo(
                    media=media["file"],
                    caption=item_caption,
                    caption_entities=item_entities
                )

            else:

                item = InputMediaDocument(
                    media=media["file"],
                    caption=item_caption,
                    caption_entities=item_entities
                )

            media_group.append(item)

        if len(media_group) == 1:

            # Telegram не принимает альбом из одного элемента.
            # Отправляем единственный файл отдельно.
            message, media = prepared[0]

            if media["type"] == "photo":

                await bot.send_photo(
                    chat_id=NEWS_TARGET_CHAT_ID,
                    message_thread_id=NEWS_TOPIC_ID,
                    photo=media["file"],
                    caption=caption if use_caption else None,
                    caption_entities=(
                        convert_entities(caption_entities)
                        if use_caption
                        else None
                    )
                )

            elif media["type"] == "video":

                await bot.send_video(
                    chat_id=NEWS_TARGET_CHAT_ID,
                    message_thread_id=NEWS_TOPIC_ID,
                    video=media["file"],
                    caption=caption if use_caption else None,
                    caption_entities=(
                        convert_entities(caption_entities)
                        if use_caption
                        else None
                    )
                )

            else:

                await bot.send_document(
                    chat_id=NEWS_TARGET_CHAT_ID,
                    message_thread_id=NEWS_TOPIC_ID,
                    document=media["file"],
                    caption=caption if use_caption else None,
                    caption_entities=(
                        convert_entities(caption_entities)
                        if use_caption
                        else None
                    )
                )

        else:

            await bot.send_media_group(
                chat_id=NEWS_TARGET_CHAT_ID,
                message_thread_id=NEWS_TOPIC_ID,
                media=media_group
            )

        if caption and not use_caption:
            await send_news_text(caption, caption_entities)

        logger.info(
            "Альбом опубликован от имени GameFloid. "
            "Элементов: %s",
            len(media_group)
        )

        return True

    except Exception:

        logger.exception("Ошибка копирования альбома")
        return False


# ==================================================
# 13. УДАЛЕНИЕ СООБЩЕНИЙ @BrAssGay
# ==================================================

@client.on(events.NewMessage)
async def my_event_handler(event):

    if event.chat_id != TARGET_CHAT_ID:
        return

    if event.sender_id != TARGET_BOT_ID:
        return

    topic_id = get_topic_id(event)

    if topic_id is None:

        logger.warning(
            "Не удалось определить тему сообщения %s. "
            "Удаление отменено.",
            event.id
        )
        return

    if topic_id == IGNORE_TOPIC_ID:

        logger.info(
            "Тема 18600 исключена. Сообщение %s оставлено.",
            event.id
        )
        return

    try:

        await event.delete()

        logger.info(
            "Удалено сообщение @BrAssGay: %s, тема: %s",
            event.id,
            topic_id
        )

    except Exception:

        logger.exception(
            "Ошибка удаления сообщения @BrAssGay"
        )


# ==================================================
# 14. АВТОМАТИЧЕСКОЕ КОПИРОВАНИЕ НОВОСТЕЙ
# ==================================================

@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def news_handler(event):

    message = event.message

    if message.grouped_id:
        return

    logger.info(
        "Получена новая публикация из @%s: %s",
        SOURCE_CHANNEL,
        message.id
    )

    async with news_lock:
        await copy_single_news(message)


# ==================================================
# 15. АВТОМАТИЧЕСКОЕ КОПИРОВАНИЕ АЛЬБОМОВ
# ==================================================

@client.on(events.Album(chats=SOURCE_CHANNEL))
async def news_album_handler(event):

    if not event.messages:
        return

    logger.info(
        "Получен альбом из @%s. Элементов: %s",
        SOURCE_CHANNEL,
        len(event.messages)
    )

    async with news_lock:
        await copy_album_news(event.messages)


# ==================================================
# 16. ПРОВЕРКА ПРАВ АДМИНИСТРАТОРА
# ==================================================

async def is_group_admin(user_id):

    if not user_id:
        return False

    try:

        member = await bot.get_chat_member(
            chat_id=TARGET_CHAT_ID,
            user_id=user_id
        )

        return member.status in (
            "administrator",
            "creator"
        )

    except Exception:

        logger.exception(
            "Не удалось проверить права пользователя %s",
            user_id
        )

        return False


# ==================================================
# 17. РУЧНАЯ ПРОВЕРКА /check
# ==================================================

@client.on(events.NewMessage)
async def check_command_handler(event):

    if event.chat_id != NEWS_TARGET_CHAT_ID:
        return

    command = (event.raw_text or "").strip().split()

    if not command:
        return

    if command[0].split("@")[0].lower() != "/check":
        return

    # Команда должна быть отправлена именно в теме новостей.
    topic_id = get_topic_id(event)

    if topic_id != NEWS_TOPIC_ID:
        return

    # Проверяем права отправителя через Bot API.
    if not await is_group_admin(event.sender_id):

        logger.warning(
            "Пользователь %s попытался выполнить /check "
            "без прав администратора.",
            event.sender_id
        )

        try:
            await event.delete()
        except Exception:
            logger.exception(
                "Не удалось удалить неавторизованную команду /check"
            )

        return

    logger.info(
        "Администратор %s запустил /check.",
        event.sender_id
    )

    async with news_lock:

        try:

            latest = await client.get_messages(
                SOURCE_CHANNEL,
                limit=1
            )

            if not latest:

                logger.warning(
                    "В канале @%s нет публикаций.",
                    SOURCE_CHANNEL
                )
                return

            last_message = latest[0]

            if last_message.grouped_id:

                recent_messages = await client.get_messages(
                    SOURCE_CHANNEL,
                    limit=10
                )

                album_messages = [
                    message
                    for message in recent_messages
                    if message.grouped_id == last_message.grouped_id
                ]

                success = await copy_album_news(
                    album_messages
                )

            else:

                success = await copy_single_news(
                    last_message
                )

            if success:

                await event.delete()

                logger.info(
                    "/check выполнена успешно. "
                    "Скопирована публикация %s",
                    last_message.id
                )

            else:

                logger.error(
                    "/check завершилась ошибкой. "
                    "Команда оставлена в теме."
                )

        except Exception:

            logger.exception(
                "Ошибка выполнения команды /check"
            )


# ==================================================
# 18. ЗАПУСК
# ==================================================

async def main():

    logger.info("Запуск Telethon Worker...")

    await client.start()

    me = await client.get_me()
    bot_info = await bot.get_me()

    logger.info(
        "Telethon подключён. Аккаунт ID: %s",
        me.id
    )

    logger.info(
        "Bot API подключён. Бот: @%s, ID: %s",
        bot_info.username,
        bot_info.id
    )

    logger.info("Целевая группа: %s", TARGET_CHAT_ID)
    logger.info("Исключённая тема: %s", IGNORE_TOPIC_ID)
    logger.info("Целевой бот модерации: %s", TARGET_BOT_ID)
    logger.info("Источник новостей: @%s", SOURCE_CHANNEL)
    logger.info("Тема публикации новостей: %s", NEWS_TOPIC_ID)
    logger.info("Модерация @BrAssGay активна")
    logger.info("Автоматическое копирование новостей активно")
    logger.info("Публикация выполняется от имени GameFloid")
    logger.info("Команда /check доступна администраторам")

    try:
        await client.run_until_disconnected()

    finally:

        await bot.session.close()


# ==================================================
# 19. ТОЧКА ВХОДА
# ==================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа Telethon завершена")
