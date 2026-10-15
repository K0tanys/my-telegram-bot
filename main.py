import asyncio
import logging
import os
import re
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Update, Message
from aiogram.filters import Command
from aiohttp import web, ClientSession
from bs4 import BeautifulSoup

# Наше оригинальное логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

# ИСПРАВЛЕНО: Стабильный адрес открытого канала для парсинга напрямую через HTML
SOURCE_CHANNEL = "br_dev"  

try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except (ValueError, TypeError):
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

LAST_SENT_POST_LINK = None

# Обработчик входных вебхуков от Telegram и пингов от Render
async def handle_webhook(request):
    # ИСПРАВЛЕНО: Защита от HEAD и GET запросов Render, чтобы не было ошибки "Expecting value"
    if request.method in ("GET", "HEAD"):
        return web.Response(text="Бот активен и работает через Webhook.", status=200)
    
    try:
        json_data = await request.json()
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return web.Response(text="ok", status=200)
    except Exception as e:
        logger.error(f"Ошибка при обработке вебхука: {e}")
        return web.Response(text="error", status=400)

# Функция чтения постов канала напрямую через BeautifulSoup (Взамен старого RSS)
async def fetch_last_post():
    url = f"https://t.me{SOURCE_CHANNEL}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Ошибка парсинга канала: статус {response.status}")
                    return None

                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Находим все блоки сообщений
                messages = soup.find_all('div', class_='tgme_widget_message_wrap')
                if not messages:
                    logger.warning("Посты в канале не найдены. Проверьте SOURCE_CHANNEL.")
                    return None

                # Берем самый последний (свежий) пост
                last_message = messages[-1]

                # Извлекаем текст поста
                text_block = last_message.find('div', class_='tgme_widget_message_text')
                clean_text = text_block.get_text(separator='\n') if text_block else ""
                
                if not clean_text:
                    return None

                # Извлекаем картинку (если она есть)
                img_block = last_message.find('a', class_='tgme_widget_message_photo_styled')
                img_url = None
                if img_block and 'style' in img_block.attrs:
                    style = img_block['style']
                    if "url('" in style:
                        img_url = style.split("url('")[1].split("')")[0]

                # В качестве уникального линка используем первые 50 символов текста, чтобы определять новизну
                post_link = str(hash(clean_text))

                return {
                    "text": clean_text,
                    "image": img_url,
                    "link": post_link
                }

        except Exception as e:
            logger.error(f"Не удалось распарсить Telegram напрямую: {e}")
            return None

# Автопроверка (каждые 10 минут)
async def check_news():
    global LAST_SENT_POST_LINK
    logger.info("Автопроверка: сканируем открытую веб-страницу канала...")
    
    post = await fetch_last_post()
    if not post:
        return

    if LAST_SENT_POST_LINK == post["link"]:
        logger.info("Новых публикаций в канале пока нет.")
        return

    if LAST_SENT_POST_LINK is None:
        LAST_SENT_POST_LINK = post["link"]
        logger.info("Парсер инициализирован. Ждем новые публикации.")
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
        logger.info("Успешно перенесен новый пост")
    except Exception as e:
        logger.error(f"Ошибка при автоматической отправке: {e}")

# Ваш оригинальный хэндлер команды /check
@dp.message(Command("check"))
async def cmd_check(message: Message):
    try:
        post = await fetch_last_post()
        
        if post:
            if post["image"]:
                await bot.send_photo(chat_id=message.chat.id, photo=post["image"], caption=post["text"][:1024])
            else:
                await bot.send_message(chat_id=message.chat.id, text=post["text"][:4096])
        
        await message.reply(
            "✅ Бот активен, ошибок авторизации нет.\n"
            "Автопроверка работает каждые 10 минут."
        )
    except Exception as e:
        logger.error(f"Ошибка выполнения проверки: {e}")

# Основная асинхронная функция запуска веб-сервера
async def main_async():
    app = web.Application()
    app.router.add_post("/", handle_webhook)
    app.router.add_get("/", handle_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")
    
    # Привязка вебхука на внешний адрес Render
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "https://onrender.com")
    await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Вебхук успешно установлен на: {webhook_url}")
    
    # Запускаем фоновую задачу периодической проверки (раз в 10 минут)
    async def loop_scheduler():
        while True:
            await check_news()
            await asyncio.sleep(600)

    asyncio.create_task(loop_scheduler())
    
    # Удерживаем процесс активным непрерывно
    while True:
        await asyncio.sleep(3600)

# ИСПРАВЛЕНО: Главная точка входа, которой не хватало для запуска скрипта на Render
if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот принудительно остановлен.")
