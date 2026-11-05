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
# ID темы "Чат общения", где разрешено использовать команды и отвечать боту
ALLOWED_TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
# URL твоего приложения на Render (берется автоматически из настроек)
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Юзернейм бота, сообщения которого мы будем полностью стирать в запрещенных темах.
# По скриншотам его юзернейм обычно @BlackRussia_Assist_bot или похожий. 
# Мы заложим проверку на имя "BR Assistant" и "assist", чтобы точно не промазать.
TARGET_BOT_USERNAME_PART = "assist"
TARGET_BOT_DISPLAY_NAME = "br assistant"

# 1. ЗАЩИТА ОТ СНА (ПИНГЕР)
async def keep_alive():
    if not RENDER_URL:
        logger.warning("RENDER_EXTERNAL_URL не найдена. Автопинг отключен.")
        return

    logger.info(f"Фоновый автопинг запущен для адреса: {RENDER_URL}")
    await asyncio.sleep(10)
    
    async with ClientSession() as session:
        while True:
            try:
                async with session.get(RENDER_URL, timeout=10) as response:
                    logger.info(f"⏰ Автопинг выполнен успешно. Статус сервера: {response.status}")
            except Exception as e:
                logger.error(f"Ошибка при выполнении автопинга: {e}")
            await asyncio.sleep(35)

# 2. ОБЩИЙ ХЭНДЛЕР ДЛЯ МОДЕРАЦИИ ЧАТА
@dp.message()
async def moderate_chat(message: Message):
    current_topic = message.message_thread_id if message.message_thread_id else 0
    
    # Если это разрешенный "Чат общения" — вообще ничего не трогаем
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # --- СЦЕНАРИЙ 1: ЭТО СООБЩЕНИЕ ОТ БОТА BR ASSIST ---
    if message.from_user.is_bot:
        username = message.from_user.username.lower() if message.from_user.username else ""
        first_name = message.from_user.first_name.lower() if message.from_user.first_name else ""
        
        # Проверяем, что этот бот — именно ассистент Black Russia
        if TARGET_BOT_USERNAME_PART in username or TARGET_BOT_DISPLAY_NAME in first_name:
            try:
                # Даем микропаузу 0.3 сек, чтобы сообщение успело полностью долететь, и сносим его
                await asyncio.sleep(0.3)
                await message.delete()
                logger.info(f"❌ Удалено системное сообщение от бота BR Assist в топике {current_topic}")
            except Exception as e:
                logger.error(f"Не удалось удалить сообщение стороннего бота: {e}")
        return

    # --- СЦЕНАРИЙ 2: ЭТО КОМАНДА ОТ ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ ---
    if message.text:
        text = message.text.lower().strip()
        
        # Полный список триггеров команд со скриншотов Black Russia Assistant
        target_commands = (
            "/online", "!online", "онлайн", "online",
            "/start", "/help",
            "/car", "/cont", "/skin", "/craft", "/blackpass", "/biz",
            "/event", "/cases", "/donat", "/rules", "/drop",
            "/duel", "/duel_top", "/top_chat", "/duelon", "/donat_top", "/top",
            "/calendar", "/vodo", "/farm", "/notif"
        )
        
        if text.startswith(target_commands):
            try:
                await message.delete()
                logger.info(f"🧹 Удалена команда {text.split()[0]} от пользователя {message.from_user.id} в топике {current_topic}")
            except Exception as e:
                logger.error(f"Не удалось удалить команду пользователя: {e}")

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

    asyncio.create_task(keep_alive())

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
