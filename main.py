import asyncio
import logging
import os
import aiohttp
import xml.etree.ElementTree as ET
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

LAST_ID_FILE = "last_news_id.txt"

def get_last_sent_id():
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE, "r") as f:
            return f.read().strip()
    return None

def save_last_sent_id(news_id):
    with open(LAST_ID_FILE, "w") as f:
        f.write(str(news_id))

async def check_news():
    logger.info("Проверяю новые посты...")
    rss_url = f"https://rsshub.app{SOURCE_CHANNEL}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(rss_url) as response:
                if response.status != 200:
                    logger.error(f"Ошибка проверки RSS: статус {response.status}")
                    return
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                
                items = root.findall(".//item")
                if not items:
                    return
                
                latest_item = items[0]
                latest_id = latest_item.find("link").text
                latest_text = latest_item.find("description").text
                
                last_sent_id = get_last_sent_id()
                
                if last_sent_id != latest_id:
                    logger.info("Найдена новая новость! Отправляю...")
                    
                    await bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=latest_text,
                        parse_mode="HTML"
                    )
                    
                    save_last_sent_id(latest_id)
        except Exception as e:
            logger.error(f"Ошибка: {e}")

async def main():
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
