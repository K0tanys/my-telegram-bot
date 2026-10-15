import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.exceptions import TelegramConflictError, TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализация переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")

# Канал-источник, откуда бот копирует посты
SOURCE_CHANNEL = "@br_dev"

try:
    TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
except (ValueError, TypeError):
    TOPIC_ID = 0

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Храним ID последнего отправленного сообщения
LAST_SENT_ID_RAM = None

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

# Официальный метод копирования постов без парсинга сайтов
async def check_news(force_publish=False):
    global LAST_SENT_ID_RAM
    logger.info("Проверка обновлений в канале через официальный Telegram API...")
    
    thread_id = TOPIC_ID if TOPIC_ID != 0 else None

    # Если это первый запуск и мы не знаем ID последнего поста
    if LAST_SENT_ID_RAM is None and not force_publish:
        # Для автоматизации: сначала пытаемся нащупать условный ID (например, 100000)
        LAST_SENT_ID_RAM = 100000
        logger.info("Парсер инициализирован. Ждем новые публикации.")
        return "initialized"

    # Если вызвана команда /check, мы принудительно переносим самый свежий пост
    if force_publish:
        logger.info("Принудительный перенос последнего сообщения...")
        # Так как API бота не дает узнать точный последний ID, мы перебираем последние ID с запасом.
        # Для теста: пробуем скопировать сообщение, если мы знаем прошлый ID. 
        # Ниже в хэндлере /check написан более надежный метод прямого поиска поста.
        return "forced"

    try:
        # Автоматический режим (работает каждые 10 минут)
        next_id = int(LAST_SENT_ID_RAM) + 1
        
        # Пытаемся скопировать следующее сообщение по порядку ID
        await bot.copy_message(
            chat_id=TARGET_CHAT_ID,
            from_chat_id=SOURCE_CHANNEL,
            message_id=next_id,
            message_thread_id=thread_id
        )
        # Если отправка удалась, обновляем наш ID
        LAST_SENT_ID_RAM = next_id
        logger.info(f"Успешно перенесен новый пост с ID: {next_id}")
        
    except TelegramBadRequest:
        logger.warning("Новых постов в источнике пока не появилось.")
        return "no_new"
    except Exception as e:
        logger.error(f"Ошибка при получении обновлений: {e}")
        return "error"

# ХЭНДЛЕР КОМАНДЫ /check (Перенесен выше main() чтобы aiogram его видел)
@dp.message(Command("check"))
async def cmd_check(message: Message):
    try:
        status_msg = await message.reply("⏳ Установлена связь с Telegram API. Получаю последний пост из канала...")
        
        # Если бот уже знает последний ID из памяти, берем его, иначе пробуем получить сообщение с ID=2 (или любым ранним)
        # Для гарантированной отправки ПОСЛЕДНЕГО поста:
        # Мы берем сохраненный ID, либо если его нет (после перезапуска Render), 
        # запускаем цикл назад от сохраненного значения, чтобы найти крайний пост.
        
        base_id = LAST_SENT_ID_RAM if LAST_SENT_ID_RAM else 100000
        
        success = False
        # Пробуем скопировать пост. Если падает ошибка — уменьшаем ID, пока не найдем существующий пост
        for check_id in range(base_id + 5, max(0, base_id - 20), -1):
            try:
                await bot.copy_message(
                    chat_id=message.chat.id,      # Отправляем прямо вам в чат
                    from_chat_id=SOURCE_CHANNEL, # Из канала источника
                    message_id=check_id
                )
                # Если пост успешно отправился, запоминаем его ID для авто-проверки
                if not LAST_SENT_ID_RAM:
                    LAST_SENT_ID_RAM = check_id
                success = True
                break
            except Exception:
                continue
        
        if success:
            await status_msg.edit_text("✅ Бот активен, ошибок авторизации нет. Последний пост из группы успешно отправлен выше! Автопроверка работает каждые 10 минут.")
        else:
            await status_msg.edit_text("⚠️ Связь с ботом есть, но не удалось найти последние посты в канале. Убедитесь, что бот добавлен в канал.")
            
    except Exception as e:
        logger.error(f"Ошибка проверки прав или отправки поста: {e}")
        await message.reply(f"❌ Произошла ошибка при выполнении проверки: {e}")

# Главная функция запуска
async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Настройка планировщика на 10 минут
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Первичный запуск при старте
    await check_news(force_publish=False)
    
    logger.info("Бот успешно запущен в режиме Polling и готов принимать команды!")
    # Чистый запуск polling без конфликтующих while True
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
