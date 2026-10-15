import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message, Update
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web, ClientSession
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "br_dev")

try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except (ValueError, TypeError):
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()
LAST_SENT_TEXT = None

async def handle_webhook(request):
    if request.method == "GET":
        return web.Response(text="Got GameFloid активен и работает через Webhook.")
    try:
        json_data = await request.json()
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Ошибка при обработке вебхука: {e}")
    return web.Response(text="ok")

async def fetch_last_post():
    url = f"https://t.me{SOURCE_CHANNEL}"
    try:
        async with ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers, timeout=15) as response:
                if response.status != 200:
                    return None
                html = await response.text()
                soup = BeautifulSoup(html, "html.parser")
                
                # Находим все блоки с сообщениями на странице
                messages = soup.find_all("div", class_="tgme_widget_message_wrap")
                if not messages:
                    return None
                
                # Берем самый последний пост
                last_msg = messages[-1]
                
                text_div = last_msg.find("div", class_="tgme_widget_message_text")
                text = text_div.get_text(separator="\n").strip() if text_div else ""
                
                photo_element = last_msg.find("a", class_="tgme_widget_message_photo_wrap")
                img_url = None
                if photo_element:
                    style = photo_element.get("style", "")
                    match = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", style)
                    if match:
                        img_url = match.group(1)
                
                if not text and not img_url:
                    return None
                    
                return {"text": text, "image": img_url}
    except Exception as e:
        logger.error(f"Ошибка парсинга веб-страницы Telegram: {e}")
        return None
