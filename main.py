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
# ID темы "Чат общения", где разрешено использовать команды
ALLOWED_TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
# URL твоего приложения на Render (берется автоматически из настроек)
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 1. ЗАЩИТА ОТ СНА (ПИНГЕР)
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
                async with session.get(RENDER_URL, timeout=10) as response:
                    logger.info(f"⏰ Автопинг выполнен успешно. Статус сервера: {response.status}")
            except Exception as e:
                logger.error(f"Ошибка при выполнении автопинга: {e}")
            
            await asyncio.sleep(35)

# 2. ХЭНДЛЕР ДЛЯ УДАЛЕНИЯ КОМАНД ПОЛЬЗОВАТЕЛЕЙ
@dp.message(F.text)
async def handle_user_command(message: Message):
    # Игнорируем сообщения от любых ботов
    if message.from_user.is_bot:
        return

    text = message.text.lower().strip()
    
    # Полный список триггеров команд со скриншотов Black Russia Assistant
    target_commands = (
        # Основные команды и онлайн
        "/online", "!online", "онлайн", "online",
        "/start", "/help",
        # Имущество, скины, крафт
        "/car", "/cont", "/skin", "/craft", "/blackpass", "/biz",
        # Ивенты, кейсы, правила, донат
        "/event", "/cases", "/donat", "/rules", "/drop",
        # Дуэли и топы
        "/duel", "/duel_top", "/top_chat", "/duelon", "/donat_top", "/top",
        # Работы и разное
        "/calendar", "/vodo", "/farm", "/notif"
    )
    
    # Проверяем, начинается ли текст сообщения с какой-либо команды из списка
    if text.startswith(target_commands):
        current_topic = message.message_thread_id if message.message_thread_id else 0
        
        # Если команда введена вне разрешенного "Чата общения"
        if current_topic != ALLOWED_TOPIC_ID:
            try:
                await message.delete()
                logger.info(f"Мгновенно удалена команда {text.split()[0]} от пользователя {message.from_user.id} в топике {current_topic}")
            except Exception as e:
                logger.error(f"Не удалось удалить сообщение пользователя: {e}")

# 3. ОБРАБОТЧИК ВЕБХУКОВ
async def handle_webhook(request):
    if request.method == "GET":
        return web.Response(text="Сервер активен. Функция автопинга работает.", status=200)
    
    try:
        json_data = await request.json()
        await dp.feed_raw_update(bot, json_data)
        return web.Response(text="ok", status=200)
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")
        return web.Response(text="error", status=400)

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

    # Запускаем фоновую задачу автопинга
    asyncio.create_task(keep_alive())

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
