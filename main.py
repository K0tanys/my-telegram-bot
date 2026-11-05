import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiohttp import web, ClientSession

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
# ID темы "Чат общения", где разрешено использовать команду
ALLOWED_TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
# URL твоего приложения на Render (берется автоматически из настроек)
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Словарь для отслеживания недавних нарушителей (очищается автоматически)
user_commands = {}

# 1. ФОНОВАЯ ЗАДАЧА ДЛЯ ЗАЩИТЫ ОТ СНА (ПИНГЕР)
async def keep_alive():
    """Каждые 35 секунд пингует сам себя, чтобы Render не тушил сервер"""
    if not RENDER_URL:
        logger.warning("RENDER_EXTERNAL_URL не найдена. Автопинг отключен.")
        return

    logger.info(f"Фоновый автопинг запущен для адреса: {RENDER_URL}")
    await asyncio.sleep(10)  # Даем серверу полностью запуститься перед первым пингом
    
    async with ClientSession() as session:
        while True:
            try:
                # Отправляем легкий GET-запрос на собственный URL
                async with session.get(RENDER_URL, timeout=10) as response:
                    logger.info(f"⏰ Автопинг выполнен успешно. Статус сервера: {response.status}")
            except Exception as e:
                logger.error(f"Ошибка при выполнении автопинга: {e}")
            
            # Спим 35 секунд (меньше лимита в 50 секунд, чтобы сервер гарантированно не заснул)
            await asyncio.sleep(35)

# 2. ХЭНДЛЕР ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ
@dp.message(F.text)
async def handle_user_command(message: Message):
    if message.from_user.is_bot:
        return

    text = message.text.lower().strip()
    
    if text.startswith(("/online", "!online", "онлайн", "online")):
        current_topic = message.message_thread_id if message.message_thread_id else 0
        
        if current_topic != ALLOWED_TOPIC_ID:
            try:
                user_commands[message.from_user.id] = message.message_id
                await message.delete()
                logger.info(f"Удалена команда /online от пользователя {message.from_user.id}")
            except Exception as e:
                logger.error(f"Не удалось удалить сообщение пользователя: {e}")

# 3. ХЭНДЛЕР ДЛЯ ОТВЕТА БОТА BR ASSIST
@dp.message(F.reply_to_message | F.text)
async def handle_bot_reply(message: Message):
    if not message.from_user.is_bot:
        return

    current_topic = message.message_thread_id if message.message_thread_id else 0
    
    if current_topic != ALLOWED_TOPIC_ID:
        is_target_reply = False
        
        if message.reply_to_message:
            is_target_reply = True
        else:
            text = message.text.lower() if message.text else ""
            if "онлайн" in text or "server" in text or "игроки" in text:
                is_target_reply = True

        if is_target_reply:
            try:
                await asyncio.sleep(0.5)
                await message.delete()
                logger.info(f"Успешно удален ответ стороннего бота в запрещенной теме")
            except Exception as e:
                logger.error(f"Не удалось удалить ответ бота: {e}")

# 4. ОБРАБОТЧИК ВЕБХУКОВ (Сюда прилетают пинги и апдейты от Telegram)
async def handle_webhook(request):
    if request.method == "GET":
        return web.Response(text="Сервер активен. Функция автопинга работает.", status=200)
    
    try:
        json_data = await request.json()
        update = dp.feed_raw_update(bot, json_data)
        await dp.feed_update(bot, update)
        return web.Response(text="ok", status=200)
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")
        return web.Response(text="error", status=400)

# Очистка словаря раз в час, чтобы не забивать ОЗУ
async def memory_cleaner():
    while True:
        await asyncio.sleep(3600)
        user_commands.clear()
        logger.info("Внутренняя память кэша команд очищена.")

async def main_async():
    app = web.Application()
    app.router.add_post("/", handle_webhook)
    app.router.add_get("/", handle_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    if RENDER_URL:
        await bot.set_webhook(url=RENDER_URL, drop_pending_updates=True)
        logger.info(f"Вебхук установлен на: {RENDER_URL}")

    # Запускаем фоновые задачи: автопинг и очистку памяти
    asyncio.create_task(keep_alive())
    asyncio.create_task(memory_cleaner())

    # Вечный цикл работы основного процесса
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")

