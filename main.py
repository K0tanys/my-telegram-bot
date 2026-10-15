import asyncio
import logging
import os
import re
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.filters import Command
from aiohttp import web

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Фоновый веб-сервер для прохождения проверок Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и работает в автономном режиме!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер успешно запущен на порту {port}!")

# --- ОБРАБОТЧИК ДЛЯ АДМИНИСТРАТОРОВ (Команда /chek) ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        # Проверяем права пользователя в группе
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        if member.status in ["administrator", "creator"]:
            await message.reply("🤖 Бот работает идеально! Защита Render активна. Теперь вы можете переслать мне любой пост (текст или картинку с текстом), и я мгновенно отправлю его в тему форума без всяких сбойных сайтов!")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

# --- ФУНКЦИЯ ДЛЯ ПЕРЕСЫЛКИ И ОФОРМЛЕНИЯ ПОСТОВ В ТОПИК ---
@dp.message()
async def forward_to_topic(message: Message):
    try:
        # Бот реагирует только на сообщения от админов группы
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        if member.status not in ["administrator", "creator"]:
            return  # Игнорируем обычных участников

        # Если админ прислал команду, этот блок её пропускает
        if message.text and message.text.startswith("/"):
            return

        thread_id = TOPIC_ID if TOPIC_ID != 0 else None
        
        # Если админ прислал или переслал картинку с текстом
        if message.photo:
            logger.info("Админ прислал фото для публикации в топик...")
            await bot.send_photo(
                chat_id=TARGET_CHAT_ID,
                photo=message.photo[-1].file_id,
                caption=message.caption,
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML
            )
            await message.reply("✅ Пост с картинкой успешно опубликован в тему форума!")
            
        # Если админ прислал или переслал просто текст
        elif message.text:
            logger.info("Админ прислал текст для публикации в топик...")
            await bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=message.text,
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML
            )
            await message.reply("✅ Текстовый пост успешно опубликован в тему форума!")
            
    except Exception as e:
        logger.error(f"Ошибка при публикации поста: {e}")

async def main():
    # Запускаем обязательный сервер для Render
    await start_web_server()
    
    # Включаем чтение чата
    logger.info("Бот полностью автономен и слушает чат...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
