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

# Настройка логирования для контроля в панели Render
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализация переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Храним ID последнего отправленного поста в ОЗУ, чтобы не спамить при перезапусках
LAST_SENT_ID_RAM = None

# --- ВЕБ-СЕРВЕР ДЛЯ СТАБИЛЬНОСТИ НА RENDER ---
async def handle_ping(request):
    """Отвечает платформе Render, что бот жив и порт работает"""
    return web.Response(text="Бот GameFloid запущен и стабилен!")

async def start_web_server():
    """Запускает сервер на порту, который требует Render для статуса Live"""
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Веб-сервер заглушки запущен на порту {port}. Проблема с рестартами решена!")

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ОБРАБОТКИ ТЕКСТА И МЕДИА ---
def extract_image_url(html_text):
    """Ищет ссылку на картинку внутри HTML-тега img"""
    if not html_text:
        return None
    # Ищем url в атрибуте src="..."
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text)
    if match:
        return match.group(1)
    return None

def clean_html_tags(html_text):
    """Очищает текст от картинок, оставляя только разрешенные Telegram HTML-теги"""
    if not html_text:
        return ""
    # Удаляем полностью теги <img>, так как их нельзя отправлять в тексте Telegram
    cleaned = re.sub(r'<img[^>]+>', '', html_text)
    # Заменяем частые брейки на обычный перенос строки
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned)
    return cleaned.strip()

# --- ОСНОВНАЯ ЛОГИКА ПАРСИНГА И ОТПРАВКИ ---
async def check_news():
    logger.info("Проверяю обновления в RSS-ленте...")
    rss_url = f"https://rsshub.app{SOURCE_CHANNEL.lstrip('/')}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(rss_url, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Не удалось получить RSS: статус {response.status}")
                    return
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                
                if not items:
                    logger.info("В ленте нет записей.")
                    return
                
                latest_item = items[0]
                latest_id = latest_item.find("link").text if latest_item.find("link") is not None else latest_item.find("guid").text
                raw_description = latest_item.find("description").text
                
                global LAST_SENT_ID_RAM
                # Защита от спама при первом включении
                if LAST_SENT_ID_RAM is None:
                    logger.info(f"Первый запуск бота. Зафиксирован текущий пост как старый (ID: {latest_id})")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return
                
                # Если появился новый пост
                if LAST_SENT_ID_RAM != latest_id:
                    logger.info(f"Нашли новый пост! Начинаю обработку медиа и текста...")
                    
                    # Извлекаем картинку и чистим текст от мусорных HTML-тегов
                    image_url = extract_image_url(raw_description)
                    clean_text = clean_html_tags(raw_description)
                    
                    # Ограничение Telegram: текст под картинкой не может быть длиннее 1024 символов
                    if len(clean_text) > 1024:
                        clean_text = clean_text[:1020] + "..."
                    
                    # Параметры отправки (включая топик форума)
                    thread_id = TOPIC_ID if TOPIC_ID != 0 else None
                    
                    if image_url:
                        logger.info(f"Обнаружена картинка в посте: {image_url}. Отправляю фото с подписью...")
                        await bot.send_photo(
                            chat_id=TARGET_CHAT_ID,
                            photo=URLInputFile(image_url),
                            caption=clean_text,
                            message_thread_id=thread_id,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        logger.info("Картинок не найдено. Отправляю как обычное текстовое сообщение...")
                        await bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=clean_text,
                            message_thread_id=thread_id,
                            parse_mode=ParseMode.HTML
                        )
                    
                    LAST_SENT_ID_RAM = str(latest_id)
                    logger.info("Новость успешно опубликована в тему форума.")
                else:
                    logger.info("Изменений нет, новых постов не обнаружено.")
                    
        except Exception as e:
            logger.error(f"Ошибка в процессе check_news: {e}", exc_info=True)

async def main():
    # 1. Сразу запускаем веб-сервер, чтобы Render мгновенно перевел статус в "Live"
    await start_web_server()
    
    # 2. Настраиваем планировщик на 10 минут
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # 3. Делаем мгновенный холостой чек при старте приложения
    await check_news()
    
    # 4. Включаем постоянное слушание Telegram API
    logger.info("Бот полностью готов к работе и переходит в режим ожидания.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
