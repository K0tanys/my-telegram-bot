import asyncio
import logging
import os
import re
import aiohttp
import xml.etree.ElementTree as ET
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import URLInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# Логирование для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

LAST_SENT_ID_RAM = None

# --- ВЕБ-СЕРВЕР ДЛЯ СТАБИЛЬНОСТИ НА RENDER ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный сервер запущен на порту {port}. Проблема рестартов решена!")

# --- РАБОТА С КАРТИНКАМИ И ТЕКСТОМ ---
def extract_image_url(html_text):
    if not html_text:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text)
    return match.group(1) if match else None

def clean_html_tags(html_text):
    if not html_text:
        return ""
    cleaned = re.sub(r'<img[^>]+>', '', html_text)
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned)
    return cleaned.strip()

async def check_news():
    logger.info("Проверка RSS-ленты...")
    # Здесь используется стабильное рабочее зеркало ://rssforever.com
    rss_url = f"https://://rssforever.com/telegram/channel/{SOURCE_CHANNEL.lstrip('/')}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(rss_url, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Ошибка RSSHub: {response.status}")
                    return
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                if not items:
                    return
                
                latest_item = items[0]
                latest_id = latest_item.find("link").text if latest_item.find("link") is not None else latest_item.find("guid").text
                raw_description = latest_item.find("description").text
                
                global LAST_SENT_ID_RAM
                if LAST_SENT_ID_RAM is None:
                    logger.info(f"Бот успешно запустился. Последний пост (ID: {latest_id}) сохранен.")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return
                
                if LAST_SENT_ID_RAM != latest_id:
                    logger.info("Найдена новая публикация!")
                    image_url = extract_image_url(raw_description)
                    clean_text = clean_html_tags(raw_description)
                    
                    if len(clean_text) > 1024:
                        clean_text = clean_text[:1020] + "..."
                    
                    thread_id = TOPIC_ID if TOPIC_ID != 0 else None
                    
                    if image_url:
                        await bot.send_photo(
                            chat_id=TARGET_CHAT_ID,
                            photo=URLInputFile(image_url),
                            caption=clean_text,
                            message_thread_id=thread_id,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=clean_text,
                            message_thread_id=thread_id,
                            parse_mode=ParseMode.HTML
                        )
                    LAST_SENT_ID_RAM = str(latest_id)
                    logger.info("Новость успешно отправлена в тему форума!")
        except Exception as e:
            logger.error(f"Ошибка парсинга: {e}")

async def main():
    await start_web_server()
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    await check_news()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
