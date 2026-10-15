import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web, ClientSession

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

# Канал-источник (для RSS используем открытый мост)
SOURCE_CHANNEL = "br_dev"
RSS_URL = f"https://rsshub.app{SOURCE_CHANNEL}"

try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except (ValueError, TypeError):
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Храним ссылку или заголовок последнего отправленного поста, чтобы не спамить дубликатами
LAST_SENT_POST_LINK = None

# Фоновый веб-сервер для прохождения проверок Render
async def handle_ping(request):
    return web.Response(text="Got GameFloid активен и работает.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

# Функция для парсинга RSS и поиска текста с картинкой
async def fetch_last_post():
    try:
        async with ClientSession() as session:
            async with session.get(RSS_URL, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Ошибка при запросе к RSSHub: статус {response.status}")
                    return None
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                
                # Ищем первый (самый свежий) пост в ленте <item>
                item = root.find(".//item")
                if item is None:
                    return None
                
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                description = item.find("description").text if item.find("description") is not None else ""
                
                # Очищаем текст от HTML-тегов, кроме ссылок на изображения
                # Ищем картинку в теге <img src="...">
                img_match = re.search(r'<img[^>]+src="([^"]+)"', description)
                img_url = img_match.group(1) if img_match else None
                
                # Удаляем все HTML-теги из описания, чтобы Telegram не ругался
                clean_text = re.sub(r'<[^>]+>', '', description).strip()
                
                # Если описание пустое, используем заголовок
                if not clean_text:
                    clean_text = title

                return {"text": clean_text, "image": img_url, "link": link}
    except Exception as e:
        logger.error(f"Не удалось распарсить RSS: {e}")
        return None

# Автоматический метод проверки обновлений
async def check_news():
    global LAST_SENT_POST_LINK
    logger.info("Автопроверка: сканируем RSS-ленту чужого канала...")
    
    post = await fetch_last_post()
    if not post:
        return

    # Если этот пост мы уже отправляли — ничего не делаем
    if LAST_SENT_POST_LINK == post["link"]:
        logger.info("Новых постов в RSS пока нет.")
        return

    # Если бот только запустился, запоминаем текущий пост как отправленный
    if LAST_SENT_POST_LINK is None:
        LAST_SENT_POST_LINK = post["link"]
        logger.info("Бот запущен. Первичный пост зафиксирован, ждем новые публикации.")
        return

    thread_id = TOPIC_ID if TOPIC_ID != 0 else None

    try:
        # Отправляем пост в вашу целевую группу
        if post["image"]:
            await bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=post["image"],
                caption=post["text"][:1024], # Лимит Telegram на подпись к фото
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
        logger.error(f"Ошибка при автоматической отправке поста: {e}")

# ХЭНДЛЕР КОМАНДЫ /check
@dp.message(Command("check"))
async def cmd_check(message: Message):
    try:
        status_msg = await message.reply("⏳ Связь установлена. Запрашиваю последний пост через RSS-мост...")
        
        post = await fetch_last_post()
        
        if post:
            # Отправляем последний пост прямо вам в чат для проверки
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
            await status_msg.edit_text("✅ Бот работает идеально! Последний пост успешно получен без прав администратора.")
        else:
            await status_msg.edit_text("⚠️ Не удалось получить данные из RSS-ленты. Возможно, зеркало временно недоступно.")
            
    except Exception as e:
        logger.error(f"Ошибка выполнения проверки: {e}")
        await message.reply(f"❌ Произошла ошибка: {e}")

# Главная функция запуска
async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Настройка планировщика на 10 минут
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Первичный холостой запуск для фиксации текущего поста
    await check_news()
    
    logger.info("Бот успешно запущен в режиме Polling через RSS и готов принимать команды!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
