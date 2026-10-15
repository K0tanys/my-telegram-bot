import asyncio
import logging
import os
import re
import aiohttp
import xml.etree.ElementTree as ET
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import URLInputFile, Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

LAST_SENT_ID_RAM = None

# --- Фоновый веб-сервер для Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и работает стабильно!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный сервер успешно запущен на порту {port}!")

# --- Логика обработки картинок и очистки текста ---
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

# --- Главная функция проверки RSS-ленты ---
async def check_news(force=False):
    logger.info("Запуск проверки RSS-ленты...")
    channel_name = SOURCE_CHANNEL.strip().lstrip("/")
    rss_url = f"https://rssforever.com{channel_name}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(rss_url, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Зеркало RSSHub ответило статусом: {response.status}")
                    return "error"
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                if not items:
                    logger.info("Новых записей в ленте пока нет.")
                    return "empty"
                
                latest_item = items[0]
                latest_id = latest_item.find("link").text if latest_item.find("link") is not None else latest_item.find("guid").text
                raw_description = latest_item.find("description").text
                
                global LAST_SENT_ID_RAM
                
                if LAST_SENT_ID_RAM is None and not force:
                    logger.info(f"Первый запуск. Запомнили последний пост с ID: {latest_id}")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return "initialized"
                
                if LAST_SENT_ID_RAM != latest_id or force:
                    logger.info("Отправка публикации в Telegram...")
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
                    logger.info("Пост успешно переслан.")
                    return "sent"
                else:
                    logger.info("Новых постов не выходило.")
                    return "no_new"
        except Exception as e:
            logger.error(f"Ошибка при проверке новостей: {e}", exc_info=True)
            return "error"

# --- ОБРАБОТЧИК КОМАНДЫ ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        # Проверяем, является ли отправитель админом в нашей группе
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Проверяю ленту новостей вручную...")
            
            # Запускаем принудительную выгрузку поста
            result = await check_news(force=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Проверка завершена. Последний пост отправлен в тред!")
            elif result == "no_new" or result == "initialized":
                await status_msg.edit_text("🔄 Новых постов в канале нет, но я проверил — связь с лентой отличная!")
            else:
                await status_msg.edit_text("❌ Ошибка при обращении к серверу новостей.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа пытался прописать /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Холостой запуск при старте
    await check_news()
    
    # Включаем чтение команд из чата
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
