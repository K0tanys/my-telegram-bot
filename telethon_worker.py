import os
import logging
from telethon import TelegramClient, events

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Данные авторизации из окружения Render
API_ID = 38378220
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("TELETHON_SESSION")

if not API_HASH or not SESSION_STRING:
    logger.error("Критические переменные окружения (API_HASH или TELETHON_SESSION) отсутствуют!")
    exit(1)

# Юзернеймы для автоматического поиска ID при старте
TARGET_CHAT_USERNAME = "GameFloid"
TARGET_BOT_USERNAMES = ["BrAssGay", "BlackRussiaAssistantBot"]
IGNORE_TOPIC_ID = 18600

# Глобальные переменные для хранения ID
target_chat_id = None
target_bot_ids = set()

# Инициализация клиента
from telethon.sessions import StringSession
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

@client.on(events.NewMessage)
async def my_event_handler(event):
    global target_chat_id, target_bot_ids
    
    if not target_chat_id:
        return

    if event.chat_id == target_chat_id:
        if event.reply_to_msg_id == IGNORE_TOPIC_ID:
            return
            
        if event.sender_id in target_bot_ids:
            me = await client.get_me()
            if event.sender_id == me.id:
                return
                
            try:
                await event.delete()
                logger.info(f"[Успешно] Удалено новое сообщение от бота {event.sender_id} в теме {event.reply_to_msg_id}")
            except Exception as e:
                logger.error(f"[Ошибка] Не удалось удалить сообщение: {e}")

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
        logger.info(f"Целевая группа определена. ID: {target_chat_id}")
        
        for username in TARGET_BOT_USERNAMES:
            try:
                bot_entity = await client.get_entity(username)
                target_bot_ids.add(bot_entity.id)
                logger.info(f"Бот {username} определен. ID: {bot_entity.id}")
            except Exception as bot_err:
                logger.warning(f"Не удалось найти ID для бота @{username}: {bot_err}")
                
    except Exception as e:
        logger.error(f"Ошибка при инициализации сущностей Telegram: {e}")
        return

    logger.info("Воркер начал отслеживание новых сообщений...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
