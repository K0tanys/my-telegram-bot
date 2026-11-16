
import os
import logging
import asyncio

from telethon import TelegramClient, events
from telethon.sessions import StringSession


# ==================================================
# 1. ЛОГИРОВАНИЕ
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ==================================================
# 2. АВТОРИЗАЦИЯ TELETHON
# ==================================================

API_ID = int(os.getenv("API_ID", "38378220"))
API_HASH = os.getenv("API_HASH")
TELETHON_SESSION = os.getenv("TELETHON_SESSION")

if not API_HASH or not TELETHON_SESSION:
    logger.error("Отсутствует API_HASH или TELETHON_SESSION")
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
# 5. TELETHON CLIENT
# ==================================================

client = TelegramClient(
    session,
    API_ID,
    API_HASH.strip()
)


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
# 7. УДАЛЕНИЕ СООБЩЕНИЙ @BrAssGay
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
            "Не удалось определить тему сообщения %s. Удаление отменено.",
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
        logger.exception("Ошибка удаления сообщения @BrAssGay")


# ==================================================
# 8. КОПИРОВАНИЕ ОДНОЙ НОВОСТИ
# ==================================================

async def copy_single_news(message):

    try:
        text = message.message or ""
        entities = message.entities or []

        if message.media:

            await client.send_file(
                entity=NEWS_TARGET_CHAT_ID,
                file=message.media,
                caption=text or None,
                formatting_entities=entities or None,
                reply_to=NEWS_TOPIC_ID,
                parse_mode=None
            )

        elif text:

            await client.send_message(
                entity=NEWS_TARGET_CHAT_ID,
                message=text,
                formatting_entities=entities or None,
                reply_to=NEWS_TOPIC_ID,
                parse_mode=None,
                link_preview=True
            )

        else:
            logger.warning(
                "Пустая публикация %s пропущена.",
                message.id
            )
            return False

        logger.info(
            "Новость успешно скопирована. Источник: %s",
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
# 9. КОПИРОВАНИЕ АЛЬБОМА
# ==================================================

async def copy_album_news(messages):

    try:
        messages = sorted(
            messages,
            key=lambda item: item.id
        )

        files = [
            message.media
            for message in messages
            if message.media
        ]

        if not files:
            return False

        first_message = messages[0]

        caption = first_message.message or ""
        entities = first_message.entities or []

        await client.send_file(
            entity=NEWS_TARGET_CHAT_ID,
            file=files,
            caption=caption or None,
            formatting_entities=entities or None,
            reply_to=NEWS_TOPIC_ID,
            parse_mode=None
        )

        logger.info(
            "Альбом успешно скопирован. Элементов: %s",
            len(files)
        )

        return True

    except Exception:
        logger.exception("Ошибка копирования альбома")
        return False


# ==================================================
# 10. ОБРАБОТКА НОВЫХ ОДИНОЧНЫХ ПУБЛИКАЦИЙ
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

    await copy_single_news(message)


# ==================================================
# 11. ОБРАБОТКА НОВЫХ АЛЬБОМОВ
# ==================================================

@client.on(events.Album(chats=SOURCE_CHANNEL))
async def news_album_handler(event):

    if not event.messages:
        return

    await copy_album_news(event.messages)


# ==================================================
# 12. РУЧНАЯ ПРОВЕРКА /check
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

    # Команда должна быть отправлена именно в тему новостей.
    topic_id = get_topic_id(event)

    if topic_id != NEWS_TOPIC_ID:
        logger.warning(
            "/check отправлена не в тему новостей. "
            "Сообщение: %s, тема: %s",
            event.id,
            topic_id
        )
        return

    logger.info("Получена команда /check. Ищу последнюю новость...")

    try:
        # Берём последнюю публикацию канала,
        # независимо от даты её выхода.
        latest = await client.get_messages(
            SOURCE_CHANNEL,
            limit=1
        )

        if not latest:
            logger.warning("В канале @%s нет публикаций.", SOURCE_CHANNEL)
            return

        last_message = latest[0]

        # Если последняя публикация является частью альбома,
        # собираем остальные элементы этого альбома.
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

            success = await copy_album_news(album_messages)

        else:
            success = await copy_single_news(last_message)

        if success:
            # Удаляем команду только после успешного копирования.
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
        logger.exception("Ошибка выполнения команды /check")


# ==================================================
# 13. ЗАПУСК TELETHON
# ==================================================

async def main():

    logger.info("Запуск Telethon worker...")

    await client.start()

    me = await client.get_me()

    logger.info("Telethon подключён. Аккаунт ID: %s", me.id)
    logger.info("Целевая группа модерации: %s", TARGET_CHAT_ID)
    logger.info("Исключённая тема: %s", IGNORE_TOPIC_ID)
    logger.info("Целевой бот: %s", TARGET_BOT_ID)
    logger.info("Источник новостей: @%s", SOURCE_CHANNEL)
    logger.info("Группа публикации новостей: %s", NEWS_TARGET_CHAT_ID)
    logger.info("Тема публикации новостей: %s", NEWS_TOPIC_ID)
    logger.info("Модерация @BrAssGay активна")
    logger.info("Автоматическое копирование новостей активно")
    logger.info("Команда /check активна")

    await client.run_until_disconnected()


# ==================================================
# 14. ТОЧКА ВХОДА
# ==================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа Telethon завершена")
