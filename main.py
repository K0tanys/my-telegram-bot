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
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "br_dev").strip().lstrip("@")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

LAST_SENT_ID_RAM = None

# --- Фоновый веб-сервер для Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и работает стабильно через прямой парсинг!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный сервер успешно запущен на порту {port}!")

# --- ПРЯМОЙ ПАРСИНГ ТЕЛЕГРАМ-СТРАНИЦЫ ---
async def check_news(force=False):
    logger.info(f"Запуск прямой проверки канала t.me/s/{SOURCE_CHANNEL}...")
    url = f"https://t.me{SOURCE_CHANNEL}"
    
    async with aiohttp.ClientSession() as session:
        try:
            # Имитируем обычный браузер, чтобы Telegram отдал страницу
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Не удалось открыть страницу Telegram: статус {response.status}")
                    return "error"
                
                html_text = await response.text()
                
                # Находим все блоки постов на веб-странице
                posts = re.findall(r'<div class="tgme_widget_message_widget[^">]*".*?<\/div>\s*<\/div>\s*<\/div>', html_text, re.DOTALL)
                if not posts:
                    logger.info("На странице канала не найдено постов.")
                    return "empty"
                
                # Берем самый последний (свежий) пост со страницы
                latest_post = posts[-1]
                
                # Извлекаем уникальный ID поста в Telegram (номер сообщения)
                id_match = re.search(r'data-post="[^"\/]+\/(\d+)"', latest_post)
                if not id_match:
                    return "error"
                latest_id = id_match.group(1)
                
                # Извлекаем текст поста
                text_match = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)<\/div>', latest_post, re.DOTALL)
                raw_text = text_match.group(1) if text_match else ""
                
                # Чистим текст от внутренних HTML-тегов Телеграма, переводя их в понятные боту
                clean_text = re.sub(r'<br\s*/?>', '\n', raw_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text)
                clean_text = unescape(clean_text).strip()
                
                # Извлекаем картинку поста (если она есть)
                image_url = None
                img_match = re.search(r'background-image:url\(\'([^\']+)\'\)', latest_post)
                if img_match:
                    image_url = img_match.group(1)
                
                global LAST_SENT_ID_RAM
                
                if LAST_SENT_ID_RAM is None and not force:
                    logger.info(f"Первый запуск. Запомнили пост с ID: {latest_id}")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return "initialized"
                
                if LAST_SENT_ID_RAM != latest_id or force:
                    logger.info(f"Отправка публикации ID {latest_id} в Telegram...")
                    
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
                    logger.info("Пост успешно доставлен напрямую!")
                    return "sent"
                else:
                    logger.info("Новых постов не обнаружено.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Ошибка прямого парсинга: {e}", exc_info=True)
            return "error"

# --- ОБРАБОТЧИК КОМАНДЫ ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Подключаюсь напрямую к серверам Telegram и проверяю канал...")
            
            result = await check_news(force=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Проверка завершена. Пост успешно отправлен!")
            elif result == "no_new" or result == "initialized":
                await status_msg.edit_text("🔄 Новых постов нет, но прямая связь с каналом работает идеально, без ошибок!")
            else:
                await status_msg.edit_text("❌ Не удалось связаться с веб-страницей Telegram. Попробуйте еще раз.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    await check_news()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
