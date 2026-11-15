
import os
import logging
import asyncio

from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "38378220"))
API_HASH = os.getenv("API_HASH")
TELETHON_SESSION = os.getenv("TELETHON_SESSION")

if not API_HASH or not TELETHON_SESSION:
    logger.error("Отсутствует API_HASH или TELETHON_SESSION")
    raise SystemExit(1)

session = StringSession(TELETHON_SESSION.strip())

TARGET_CHAT_ID = -1002165454770
TARGET_BOT_ID = 8878352778
IGNORE_TOPIC_ID = 18600

client = TelegramClient(session, API_ID, API_HASH.strip())


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


@client.on(events.NewMessage)
async def my_event_handler(event):
    if event.chat_id != TARGET_CHAT_ID:
        return

    if event.sender_id != TARGET_BOT_ID:
        return

    topic_id = get_topic_id(event)

    # Если тему нельзя достоверно определить — ничего не удаляем.
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
        logger.exception("Ошибка удаления сообщения")


async def main():
    logger.info("Запуск Telethon worker...")

    await client.start()

    me = await client.get_me()

    logger.info("Telethon подключён. Аккаунт ID: %s", me.id)
    logger.info("Целевая группа: %s", TARGET_CHAT_ID)
    logger.info("Исключённая тема: %s", IGNORE_TOPIC_ID)
    logger.info("Целевой бот: %s", TARGET_BOT_ID)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
