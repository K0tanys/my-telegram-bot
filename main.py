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
    return web.Response(text="Бот GameFloid активен и копирует посты автоматически!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

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

# --- Автоматическая проверка RSS-ленты и копирование постов ---
async def check_news(force_publish=False):
    logger.info("Запуск проверки RSS-ленты...")
    
    urls = [
        "https://slarker.me",
        "https://rssforever.com",
        "https://rsshub.app"
    ]
    
    xml_data = None
    
    # Жесткий таймаут на 30 секунд общего времени соединения
    custom_timeout = aiohttp.ClientTimeout(total=30)
    
    async with aiohttp.ClientSession(timeout=custom_timeout) as session:
        for url in urls:
            try:
                logger.info(f"Подключаюсь к: {url} (ожидание до 30 секунд)...")
                async with session.get(url) as response:
                    if response.status == 200:
                        xml_data = await response.text()
                        logger.info(f"Успешно получили данные от {url}")
                        break
                    else:
                        logger.warning(f"Сервер {url} вернул статус {response.status}")
            except Exception as e:
                logger.warning(f"Ошибка при запросе к {url}: {e}")
                continue
            
    if not xml_data:
        logger.error("Все доступные зеркала не ответили.")
        return "error"
        
    try:
        items = parse_rss_items(xml_data)
        if not items:
            logger.info("Постов в ленте не найдено.")
            return "empty"
        
        # Берем самый свежий пост
        latest_item = items[0]
        latest_id = latest_item["id"]
        raw_description = latest_item["description"]
        
        global LAST_SENT_ID_RAM
        
        # При первом включении просто запоминаем текущий последний пост
        if LAST_SENT_ID_RAM is None and not force_publish:
            logger.info(f"Бот запущен. Текущий пост (ID: {latest_id}) занесен в память как прочитанный.")
            LAST_SENT_ID_RAM = str(latest_id)
            return "initialized"
        
        # Если появился новый пост ИЛИ мы нажали команду /chek для принудительного копирования
        if LAST_SENT_ID_RAM != latest_id or force_publish:
            logger.info(f"Автоматически копирую пост ID {latest_id} в вашу тему...")
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
            logger.info("Пост успешно скопирован!")
            return "sent"
        else:
            logger.info("Новых постов в канале пока нет.")
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
            status_msg = await message.reply("⏳ Связываюсь с серверами новостей. Проверяю ленту и выгружаю последний пост...")
            
            # force_publish=True заставит бота принудительно скопировать самый верхний пост прямо сейчас
            result = await check_news(force_publish=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Последний пост из канала успешно скопирован в вашу тему!")
            else:
                await status_msg.edit_text("❌ Серверы-зеркала сейчас перегружены. Но не волнуйтесь, автоматическая проверка в фоновом режиме продолжает работать каждые 10 минут!")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    # Автоматический таймер проверки каждые 10 минут
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Холостой запуск при старте для фиксации
    await check_news(force_publish=False)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
