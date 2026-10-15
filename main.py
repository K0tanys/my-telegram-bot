import asyncio
import logging
import os
import re
import aiohttp
import xml.etree.ElementTree as ET
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
    return web.Response(text="Бот GameFloid активен и использует гибридный парсинг!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

# --- Способ №1: Резервный текстовый парсер через регулярные выражения ---
def parse_rss_with_regex(xml_data):
    """Безопасно вытаскивает посты, если XML-структура сломана спецсимволами"""
    items = re.findall(r'<item>(.*?)</item>', xml_data, re.DOTALL)
    if not items:
        return None
        
    latest_item = items[0]
    link_match = re.search(r'<link>(.*?)</link>', latest_item, re.DOTALL)
    if not link_match:
        link_match = re.search(r'<guid[^>]*>(.*?)</guid>', latest_item, re.DOTALL)
    
    desc_match = re.search(r'<description>(.*?)</description>', latest_item, re.DOTALL)
    
    if link_match and desc_match:
        post_id = link_match.group(1).strip()
        post_id = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', post_id, re.DOTALL)
        
        raw_desc = desc_match.group(1)
        raw_desc = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', raw_desc, re.DOTALL)
        
        return post_id, raw_desc
    return None

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

# --- Главная функция проверки RSS-ленты (Гибридный режим) ---
async def check_news(force_publish=False):
    logger.info("Проверка обновлений через гибридный шлюз...")
    rss_url = "https://rss.app"
    
    custom_timeout = aiohttp.ClientTimeout(total=30)
    
    async with aiohttp.ClientSession(timeout=custom_timeout) as session:
        try:
            async with session.get(rss_url) as response:
                if response.status != 200:
                    logger.error(f"Сервер новостей вернул статус: {response.status}")
                    return "error"
                
                xml_data = await response.text()
                
                latest_id = None
                raw_description = None
                
                # --- ПОПЫТКА №1: Стандартный XML-парсер (ElementTree) ---
                try:
                    root = ET.fromstring(xml_data)
                    items = root.findall(".//item")
                    if items:
                        latest_item = items[0]
                        latest_id = latest_item.find("link").text if latest_item.find("link") is not None else latest_item.find("guid").text
                        raw_description = latest_item.find("description").text
                        logger.info("Лента успешно обработана стандартным XML-парсером.")
                except ET.ParseError:
                    # --- ПОПЫТКА №2: Переключение на Регулярные выражения при сбое символов ---
                    logger.warning("XML содержит битые токены! Переключаюсь на резервный текстовый парсер...")
                    regex_result = parse_rss_with_regex(xml_data)
                    if regex_result:
                        latest_id, raw_description = regex_result
                        logger.info("Лента успешно обработана резервным текстовым парсером.")
                
                # Если ни один из способов не смог прочитать данные
                if not latest_id or not raw_description:
                    logger.error("Не удалось разобрать структуру данных ни одним из способов.")
                    return "error"
                
                global LAST_SENT_ID_RAM
                
                if LAST_SENT_ID_RAM is None and not force_publish:
                    logger.info(f"Успешный старт. Текущий пост сохранен в памяти (ID: {latest_id})")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return "initialized"
                
                if LAST_SENT_ID_RAM != latest_id or force_publish:
                    logger.info(f"Копирую актуальный пост в тему форума {TOPIC_ID}...")
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
                    logger.info("Новых постов не обнаружено.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Критическая ошибка в check_news: {e}", exc_info=True)
            return "error"

# --- ОБРАБОТЧИК КОМАНДЫ /chek ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Связываюсь с глобальным шлюзом. Проверяю ленту новостей в гибридном режиме...")
            result = await check_news(force_publish=True)
            if result == "sent":
                await status_msg.edit_text("✅ Гибридная проверка завершена! Последний актуальный пост успешно скопирован.")
            else:
                await status_msg.edit_text("❌ Ошибка шлюза. Но автоматический таймер продолжит работу.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    await check_news(force_publish=False)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
