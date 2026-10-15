import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message, Update
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web, ClientSession

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") # Автоматическая переменная Render

# Стабильное альтернативное зеркало для чтения Telegram без блокировок
SOURCE_CHANNEL = "br_dev"
RSS_URL = f"https://v2ex.com{SOURCE_CHANNEL}"

try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except (ValueError, TypeError):
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

LAST_SENT_POST_LINK = None

# Обработчик вебхуков от Telegram + пинг от Render
async def handle_webhook(request):
    # Если это простой запрос для проверки активности (Ping от Render или UptimeRobot)
    if request.method == "GET":
        return web.Response(text="Got GameFloid активен и работает через Webhook.")
    
    # Если это входящее сообщение от Telegram
    try:
        json_data = await request.json()
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Ошибка при обработке вебхука: {e}")
    
    return web.Response(text="ok")

# Функция парсинга RSS
async def fetch_last_post():
    try:
        async with ClientSession() as session:
            # Используем кастомный User-Agent, чтобы сайт не блокировал хостинг
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with session.get(RSS_URL, headers=headers, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Ошибка RSS: статус {response.status}")
                    return None
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                
                item = root.find(".//item")
                if item is None:
                    return None
                
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                description = item.find("description").text if item.find("description") is not None else ""
                
                img_match = re.search(r'<img[^>]+src="([^"]+)"', description)
                img_url = img_match.group(1) if img_match else None
                
                clean_text = re.sub(r'<[^>]+>', '', description).strip()
                if not clean_text:
                    clean_text = title

                return {"text": clean_text, "image": img_url, "link": link}
    except Exception as e:
        logger.error(f"Не удалось распарсить RSS: {e}")
        return None

# Автопроверка (каждые 10 минут)
async def check_news():
    global LAST_SENT_POST_LINK
    logger.info("Автопроверка: сканируем стабильное RSS-зеркало...")
    
    post = await fetch_last_post()
    if not post:
        return

    if LAST_SENT_POST_LINK == post["link"]:
        logger.info("Новых публикаций в канале пока нет.")
        return

    if LAST_SENT_POST_LINK is None:
        LAST_SENT_POST_LINK = post["link"]
        logger.info("Бот запущен. Текущий пост зафиксирован.")
        return

    thread_id = TOPIC_ID if TOPIC_ID != 0 else None

    try:
        if post["image"]:
            await bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=post["image"],
                caption=post["text"][:1024],
                message_thread_id=thread_id
            )
        else:
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=post["text"][:4096],
                message_thread_id=thread_id
            )
        LAST_SENT_POST_LINK = post["link"]
        logger.info("Новый пост успешно перенесен в группу!")
    except Exception as e:
        logger.error(f"Ошибка автоматической отправки: {e}")

# ХЭНДЛЕР КОМАНДЫ /check
@dp.message(Command("check"))
async def cmd_check(message: Message):
    try:
        status_msg = await message.reply("⏳ Запрашиваю последний пост через резервное RSS-зеркало...")
        post = await fetch_last_post()
        
        if post:
            if post["image"]:
                await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=post["image"],
                    caption=f"<b>Последний пост из канала:</b>\n\n{post['text']}"[:1024],
                    parse_mode=ParseMode.HTML
                )
            else:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=f"<b>Последний пост из канала:</b>\n\n{post['text']}"[:4096],
                    parse_mode=ParseMode.HTML
                )
            await status_msg.edit_text("✅ Бот работает! Последний пост успешно получен без прав администратора.")
        else:
            await status_msg.edit_text("⚠️ Не удалось получить данные. Возможно, зеркало обновляется, попробуйте через пару минут.")
    except Exception as e:
        await message.reply(f"❌ Произошла ошибка: {e}")

# Запуск сервера вебхуков
async def main():
    app = web.Application()
    # Обрабатываем и корень для пинга, и запросы от Telegram в одном месте
    app.router.add_post("/", handle_webhook)
    app.router.add_get("/", handle_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Сервер Вебхуков запущен на порту {port}!")

    # Настраиваем вебхук в самом Telegram
    # Render автоматически предоставляет переменную RENDER_EXTERNAL_URL (например, https://onrender.com)
    webhook_url = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else f"https://onrender.com"
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Вебхук успешно привязан к адресу: {webhook_url}")

    # Запуск планировщика
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    await check_news()

    # Держим приложение запущенным
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
