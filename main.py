import asyncio
import logging
import os
import re
import aiohttp
from bs4 import BeautifulSoup
import html
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

# Безопасное приведение TOPIC_ID к числу
try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except ValueError:
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# ID последнего отправленного поста в ОЗУ
LAST_SENT_ID_RAM = None

# Фоновый веб-сервер для Render
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и работает через собственный пинг.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

# Собственная логика прямого сбора постов через BeautifulSoup
async def check_news(force_publish=False):
    global LAST_SENT_ID_RAM
    logger.info("Собственный парсер BeautifulSoup запущен...")
    url = "https://t.me"  # Пример ссылки (можете изменить на нужный канал)
    
    custom_timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=custom_timeout) as session:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Telegram вернул статус: {response.status}")
                    return "error"
                
                html_data = await response.text()
                soup = BeautifulSoup(html_data, "lxml") # Используем быстрый lxml
                
                posts = soup.find_all("div", class_="tgme_widget_message")
                if not posts:
                    logger.warning("Постов на веб-странице Telegram не найдено.")
                    return "empty"
                
                latest_post = posts[-1]
                post_id_attr = latest_post.get("data-post")
                if not post_id_attr:
                    return "error"
                
                latest_id = post_id_attr.split("/")[-1]
                
                # Вытаскиваем текст поста
                text_block = latest_post.find("div", class_="tgme_widget_message_text")
                clean_text = ""
                if text_block:
                    for br in text_block.find_all("br"):
                        br.replace_with("\n")
                    clean_text = text_block.get_text().strip()
                
                # Вытаскиваем ссылку на картинку
                image_url = None
                photo_block = latest_post.find("a", class_="tgme_widget_message_photo_wrap")
                if photo_block:
                    style_attr = photo_block.get("style", "")
                    img_match = re.search(r"background-image:url\(['\"]?(.*?)['\"]?\)", style_attr)
                    if img_match:
                        image_url = img_match.group(1)
                
                if LAST_SENT_ID_RAM is None and not force_publish:
                    logger.info(f"Успешный запуск. Последний пост занесен в память: {latest_id}")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return "initialized"
                
                if LAST_SENT_ID_RAM != latest_id or force_publish:
                    logger.info(f"Автоматически копирую пост ID {latest_id}")
                    
                    if len(clean_text) > 1024:
                        clean_text = clean_text[:1020] + "..."
                    
                    thread_id = TOPIC_ID if TOPIC_ID != 0 else None
                    
                    # ИСПРАВЛЕНО: aiogram 3.x принимает ссылку на фото строкой напрямую
                    if image_url:
                        await bot.send_photo(
                            chat_id=TARGET_CHAT_ID,
                            photo=image_url,
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
                    logger.info("Пост успешно перенесен.")
                    return "sent"
                else:
                    logger.info("Новых постов в канале пока нет.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Ошибка в модуле BeautifulSoup: {e}", exc_info=True)
            return "error"

# ОБРАБОТЧИК КОМАНДЫ /check ДЛЯ АДМИНИСТРАТОРОВ (Исправлена опечатка)
@dp.message(Command("check"))
async def cmd_check(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Запускаю прямой сбор данных...")
            result = await check_news(force_publish=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Последний пост из канала автоматически перенесен!")
            elif result in ["no_new", "initialized"]:
                await status_msg.edit_text("🔄 Новых постов нет, но прямая связь с каналом стабильна.")
            else:
                await status_msg.edit_text("❌ Ошибка сбора данных. Попробуйте еще раз или проверьте логи.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа пытался вызвать команду.")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    
    # Автоматическая проверка каждые 10 минут
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    await check_news(force_publish=False)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
