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

# Указываем 2312 как точку отсчета, чтобы бот точно знал текущую позицию
LAST_SENT_ID_RAM = 2312

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
    logger.info("Проверка... Сканируем канал на наличие новых публикаций...")
    
    thread_id = TOPIC_ID if TOPIC_ID != 0 else None

    if force_publish:
        logger.info("Принудительный перенос сообщения запрошен.")
        return "forced"

    try:
        # Автоматический режим (работает каждые 10 минут) — ищет пост 2313 и далее
        next_id = int(LAST_SENT_ID_RAM) + 1
        
        await bot.copy_message(
            chat_id=TARGET_CHAT_ID,
            from_chat_id=SOURCE_CHANNEL,
            message_id=next_id,
            message_thread_id=thread_id
        )
        LAST_SENT_ID_RAM = next_id
        logger.info(f"Успешно перенесен новый пост с ID: {next_id}")
        
    except TelegramBadRequest:
        logger.warning("Новых постов в источнике пока не появилось.")
        return "no_new"
    except Exception as e:
        logger.error(f"Ошибка при получении обновлений: {e}")
        return "error"

# ХЭНДЛЕР КОМАНДЫ /check
@dp.message(Command("check"))
async def cmd_check(message: Message):
    global LAST_SENT_ID_RAM
    try:
        status_msg = await message.reply("⏳ Связь установлена. Получаю пост под номером 2312...")
        
        # Копируем пост 2312 прямо в чат к пользователю для проверки
        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=SOURCE_CHANNEL,
            message_id=2312
        )
        
        await status_msg.edit_text("✅ Бот работает идеально! Последний пост из группы успешно отправлен выше. Автопроверка новых публикаций активна.")
            
    except Exception as e:
        logger.error(f"Ошибка выполнения проверки: {e}")
        await status_msg.edit_text(f"❌ Ошибка: Не удалось скопировать пост №2312. Убедитесь, что бот добавлен в канал @br_dev как администратор.")

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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
