import asyncio
import logging
import os
import re
import aiohttp
from html import unescape
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
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# ID последнего отправленного поста в ОЗУ
LAST_SENT_ID_RAM = None

# --- Фоновый веб-сервер для Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и использует большие таймауты!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный сервер успешно запущен на порту {port}!")

# --- Безопасный парсинг данных через регулярные выражения ---
def parse_rss_items(xml_data):
    items = re.findall(r'<item>(.*?)</item>', xml_data, re.DOTALL)
    parsed_items = []
    
    for item in items:
        link_match = re.search(r'<link>(.*?)</link>', item, re.DOTALL)
        if not link_match:
            link_match = re.search(r'<guid[^>]*>(.*?)</guid>', item, re.DOTALL)
        
        desc_match = re.search(r'<description>(.*?)</description>', item, re.DOTALL)
        
        if link_match and desc_match:
            post_id = link_match.group(1).strip()
            post_id = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', post_id, re.DOTALL)
            
            raw_desc = desc_match.group(1)
            raw_desc = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', raw_desc, re.DOTALL)
            
            parsed_items.append({
                "id": post_id,
                "description": raw_desc
            })
            
    return parsed_items

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
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    return unescape(cleaned).strip()

# --- Проверка RSS-ленты с тремя зеркалами и долгим таймаутом ---
async def check_news(force_publish=False):
    logger.info("Запуск глубокой проверки RSS-ленты...")
    
    # 3 независимых публичных зеркала
    urls = [
        "https://slarker.me",
        "https://rssforever.com",
        "https://rsshub.app"
    ]
    
    xml_data = None
    for url in urls:
        try:
            logger.info(f"Подключаюсь к: {url} (ожидание до 30 секунд)...")
            async with aiohttp.ClientSession() as session:
                # Увеличили таймаут до честных 30 секунд для полной прогрузки
                async with session.get(url, timeout=30) as response:
                    if response.status == 200:
                        xml_data = await response.text()
                        logger.info(f"Успешно скачали данные от {url}")
                        break
                    else:
                        logger.warning(f"Сервер {url} ответил статусом {response.status}")
        except Exception as e:
            logger.warning(f"Сервер {url} не ответил за отведенное время: {e}")
            continue
            
    if not xml_data:
        logger.error("Ни одно из 3 зеркал не успело ответить.")
        return "error"
        
    try:
        items = parse_rss_items(xml_data)
        if not items:
            logger.info("Постов в ленте не найдено.")
            return "empty"
        
        latest_item = items[0]
        latest_id = latest_item["id"]
        raw_description = latest_item["description"]
        
        global LAST_SENT_ID_RAM
        
        if LAST_SENT_ID_RAM is None and not force_publish:
            logger.info(f"Первый старт бота. Зафиксирован текущий пост (ID: {latest_id})")
            LAST_SENT_ID_RAM = str(latest_id)
            return "initialized"
        
        if LAST_SENT_ID_RAM != latest_id or force_publish:
            logger.info(f"Публикация поста ID {latest_id} в тему форума...")
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
            logger.info("Пост успешно отправлен.")
            return "sent"
        else:
            logger.info("Новых постов в канале нет.")
            return "no_new"
            
    except Exception as e:
        logger.error(f"Ошибка обработки XML-структуры: {e}", exc_info=True)
        return "error"

# --- ОБРАБОТЧИК КОМАНДЫ /chek ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Связываюсь с серверами новостей (это может занять до 30 сек). Забираю пост...")
            
            result = await check_news(force_publish=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Последний актуальный пост из канала успешно доставлен!")
            else:
                await status_msg.edit_text("❌ Все серверы-зеркала сейчас перегружены или не успели ответить. Попробуйте еще раз.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Холостой запуск для фиксации при старте
    await check_news(force_publish=False)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
