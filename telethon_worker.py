
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

# Получение настроек
API_ID = os.getenv("API_ID", "38378220")
API_HASH = os.getenv("API_HASH")
TELETHON_SESSION = os.getenv("TELETHON_SESSION")

# Проверка конфигурации
if not API_HASH:
    logger.error("API_HASH не найден в Environment Render")
    raise SystemExit(1)

if not TELETHON_SESSION:
    logger.error("TELETHON_SESSION не найден в Environment Render")
    raise SystemExit(1)

TELETHON_SESSION = TELETHON_SESSION.strip()

logger.info("Проверка конфигурации:")
logger.info("API_ID: OK")
logger.info("API_HASH: OK")
logger.info("SESSION LENGTH: %s", len(TELETHON_SESSION))
logger.info("SESSION START: %s", TELETHON_SESSION[:5])
logger.info("SESSION END: %s", TELETHON_SESSION[-5:])

# Проверка формата сессии
try:
    session = StringSession(TELETHON_SESSION)
except Exception as error:
    logger.error("Некорректный формат TELETHON_SESSION: %s", error)
    raise SystemExit(1)

# Настройки группы и ботов
TARGET_CHAT_USERNAME = "GameFloid"
TARGET_BOT_USERNAMES = [
    "BrAssGay",
    "BlackRussiaAssistantBot"
]
IGNORE_TOPIC_ID = 18600

target_chat_id = None
target_bot_ids = set()

# Создание клиента
client = TelegramClient(
    session,
    int(API_ID),
    API_HASH.strip()
)


@client.on(events.NewMessage)
async def my_event_handler(event):
    global target_chat_id, target_bot_ids

    if not target_chat_id:
        return

    if event.chat_id != target_chat_id:
        return

    if event.reply_to_msg_id == IGNORE_TOPIC_ID:
        return

    if event.sender_id in target_bot_ids:
        try:
            me = await client.get_me()

            if event.sender_id == me.id:
                return

            await event.delete()

            logger.info(
                "Удалено сообщение от бота %s в теме %s",
                event.sender_id,
                event.reply_to_msg_id
            )

        except Exception as error:
            logger.error("Не удалось удалить сообщение: %s", error)


async def main():
    global target_chat_id, target_bot_ids

    logger.info("Запуск Telethon воркера...")

    await client.start()

    logger.info("Telethon успешно подключился!")

    try:
        chat_entity = await client.get_entity(TARGET_CHAT_USERNAME)
        target_chat_id = chat_entity.id

        if not str(target_chat_id).startswith("-100"):
            target_chat_id = int(f"-100{target_chat_id}")

        logger.info("Целевая группа определена. ID: %s", target_chat_id)

        for username in TARGET_BOT_USERNAMES:
            try:
                bot_entity = await client.get_entity(username)
                target_bot_ids.add(bot_entity.id)

                logger.info(
                    "Бот @%s определен. ID: %s",
                    username,
                    bot_entity.id
                )

            except Exception as bot_error:
                logger.warning(
                    "Не удалось найти ID бота @%s: %s",
                    username,
                    bot_error
                )

    except Exception as error:
        logger.error("Ошибка инициализации Telegram: %s", error)
        await client.disconnect()
        return

    logger.info("Воркер начал отслеживание новых сообщений!")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
