import asyncio
import logging
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.exceptions import TelegramConflictError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except (ValueError, TypeError):
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

LAST_SENT_ID_RAM = None

# Фоновый веб-сервер для Render
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

# Логика сбора постов (ИСПРАВЛЕНО на RSS-поток канала)
async def check_news(force_publish=False):
    global LAST_SENT_ID_RAM
    logger.info("Безопасный парсер запущен...")
    
    # ИСПРАВЛЕНО: Изменили на rss, чтобы Render не блокировался защитой Telegram
    url = "https://t.me"
    
    custom_timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=custom_timeout) as session:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Telegram вернул статус: {response.status}")
                    return "error"
                
                html_data = await response.text()
                soup = BeautifulSoup(html_data, "html.parser")
                
                # Ищем элементы постов в RSS-ленте
                posts = soup.find_all("item")
                if not posts:
                    # Если RSS пуст, пробуем стандартный веб-поиск в качестве резерва
                    url_backup = "https://t.me"
                    async with session.get(url_backup, headers=headers) as res_backup:
                        html_data = await res_backup.text()
                        soup = BeautifulSoup(html_data, "html.parser")
                        posts = soup.find_all("div", class_="tgme_widget_message")
                
                if not posts:
                    logger.warning("Контент канала временно недоступен.")
                    return "empty"
                
                latest_post = posts[-1]
                
                # Извлечение ID и текста в зависимости от формата (RSS или HTML)
                if latest_post.name == "item":
                    link_text = latest_post.find("link").get_text() if latest_post.find("link") else ""
                    latest_id = link_text.split("/")[-1] if "/" in link_text else "1"
                    
                    desc_block = latest_post.find("description")
                    clean_text = desc_block.get_text().strip() if desc_block else ""
                    # Убираем HTML-теги из описания RSS, если они есть
                    clean_text = re.sub(r'<[^>]+>', '', clean_text)
                    image_url = None
                else:
                    post_id_attr = latest_post.get("data-post")
                    if not post_id_attr:
                        return "error"
                    latest_id = post_id_attr.split("/")[-1]
                    
                    text_block = latest_post.find("div", class_="tgme_widget_message_text")
                    clean_text = ""
                    if text_block:
                        for br in text_block.find_all("br"):
                            br.replace_with("\n")
                        clean_text = text_block.get_text().strip()
                    
                    image_url = None
                    photo_block = latest_post.find("a", class_="tgme_widget_message_photo_wrap")
                    if photo_block:
                        style_attr = photo_block.get("style", "")
                        img_match = re.search(r"background-image:url\(['\"]?(.*?)['\"]?\)", style_attr)
                        if img_match:
                            image_url = img_match.group(1)
                
                if LAST_SENT_ID_RAM is None and not force_publish:
                    logger.info(f"Первый запуск. Пост ID {latest_id} сохранен в ОЗУ.")
                    LAST_SENT_ID_RAM = str(latest_id)
                    return "initialized"
                
                if LAST_SENT_ID_RAM != latest_id or force_publish:
                    logger.info(f"Переносим пост ID {latest_id}...")
                    
                    if len(clean_text) > 1024:
                        clean_text = clean_text[:1020] + "..."
                    
                    thread_id = TOPIC_ID if TOPIC_ID != 0 else None
                    
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
                    logger.info("Пост успешно отправлен!")
                    return "sent"
                else:
                    logger.info("Новых публикаций в канале нет.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Ошибка парсинга: {e}")
            return "error"

@dp.message(Command("check"))
async def cmd_check(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Запускаю проверку...")
            result = await check_news(force_publish=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Пост успешно перенесен!")
            elif result in ["no_new", "initialized"]:
                await status_msg.edit_text("🔄 Новых постов нет, связь с каналом проверена.")
            else:
                await status_msg.edit_text("❌ Ошибка при переносе. Проверьте панель логов.")
    except Exception as e:
        logger.error(f"Ошибка проверки прав: {e}")

async def main():
    await start_web_server()
    
    # Полная очистка вебхуков и зависших обновлений перед стартом
    await bot.delete_webhook(drop_pending_updates=True)
    
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    await check_news(force_publish=False)
    
    while True:
        try:
            await dp.start_polling(bot)
        except TelegramConflictError:
            logger.error("КРИТИЧЕСКИЙ КОНФЛИКТ ТОКЕНОВ! Ожидание ручного сброса токена...")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Непредвиденная ошибка цикла: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
