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

# Загрузка конфигурации строго из ваших Environment Variables на Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Храним ID последнего отправленного поста в оперативной памяти
LAST_SENT_ID_RAM = None

# --- Фоновый веб-сервер для прохождения проверок портов Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и работает через стабильный RSS-шлюз!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

# --- Логика обработки картинок и очистки HTML ---
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
    return cleaned.strip()

# --- Автоматическая проверка стабильной ленты ---
async def check_news(force_publish=False):
    logger.info("Проверка обновлений в официальном канале...")
    
    # Используем выделенный глобальный шлюз RSS.app для канала br_dev
    rss_url = "https://rss.app"
    
    # Настраиваем надежный таймаут ожидания ответа
    custom_timeout = aiohttp.ClientTimeout(total=25)
    
    async with aiohttp.ClientSession(timeout=custom_timeout) as session:
        try:
            async with session.get(rss_url) as response:
                if response.status != 200:
                    logger.error(f"Сервер новостей вернул статус: {response.status}")
                    return "error"
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                if not items:
                    logger.info("Лента новостей пуста.")
                    return "empty"
                
                # Забираем самую свежую (верхнюю) запись
                latest_item = items[0]
                latest_id = latest_item.find("link").text if latest_item.find("link") is not None else latest_item.find("guid").text
                raw_description = latest_item.find("description").text
                
                global LAST_SENT_ID_RAM
                
                # При самом первом старте просто запоминаем пост
                if LAST_SENT_ID_RAM is None and not force_publish:
                    logger.info(f"Успешный старт. Текущий пост сохранен в памяти (ID: {latest_id})")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return "initialized"
                
                # Пересылаем, если пост новый ИЛИ если админ нажал /chek
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
                    logger.info("Пост успешно переслан автоматически.")
                    return "sent"
                else:
                    logger.info("Новых постов не обнаружено.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Ошибка в модуле check_news: {e}", exc_info=True)
            return "error"

# --- ОБРАБОТЧИК КОМАНДЫ /chek ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Связываюсь с глобальным шлюзом. Проверяю ленту новостей...")
            result = await check_news(force_publish=True)
            if result == "sent":
                await status_msg.edit_text("✅ Проверка завершена. Последний пост успешно скопирован в вашу тему!")
            else:
                await status_msg.edit_text("❌ Ошибка шлюза. Но автоматический таймер продолжит работу.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    # Таймер автоматической проверки (каждые 10 минут)
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Холостой запуск для фиксации при старте приложения
    await check_news(force_publish=False)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
