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
# URL твоего приложения на Render
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Твоя прямая ссылка на тему "Чат общения"
CHAT_LINK = "https://t.me"

# Список активных топиков, где прямо сейчас тикает режим зачистки
active_cleanup_topics = set()

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

# 2. ОБЩИЙ ХЭНДЛЕР ДЛЯ МОДЕРАЦИИ И УВЕДОМЛЕНИЙ
@dp.message()
async def moderate_chat(message: Message):
    current_topic = message.message_thread_id if message.message_thread_id else 0
    
    # Если это разрешенный "Чат общения" — вообще ничего не трогаем
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # --- СЦЕНАРИЙ 1: СООБЩЕНИЕ ОТ ЛЮБОГО БОТА В МОМЕНТ ЗАЧИСТКИ ---
    if message.from_user.is_bot:
        # Если в этой теме сейчас активен режим зачистки — сносим сообщения любого бота
        if current_topic in active_cleanup_topics:
            try:
                await message.delete()
                logger.info(f"❌ Перехвачен и удален запоздалый ответ бота (ID: {message.from_user.id}) в топике {current_topic}")
            except Exception as e:
                logger.error(f"Не удалось удалить сообщение стороннего бота: {e}")
        return

    # --- СЦЕНАРИЙ 2: КОМАНДА ОТ ОБЫЧНОГО ПОЛЬЗОВАТЕЛЯ ---
    if message.text:
        text = message.text.lower().strip()
        
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
                # Включаем режим усиленной зачистки для этого топика
                active_cleanup_topics.add(current_topic)
                
                # 1. Мгновенно удаляем запрещенную команду юзера
                await message.delete()
                logger.info(f"🧹 Удалена команда от пользователя {message.from_user.id} в топике {current_topic}")
                
                # Задержка перед выводом предупреждения (1 секунда)
                await asyncio.sleep(1.0)
                
                # 2. Отправляем предупреждение через HTML (теперь без лишних слэшей)
                user_html_mention = f'<a href="tg://user?id={message.from_user.id}">{message.from_user.first_name}</a>'
                warning_text = (
                    f"⚠️ {user_html_mention}, использовать команды бота <b>Black Russia Assistant</b> "
                    f'можно строго в теме <b><a href="{CHAT_LINK}">Чат общения</a></b>!'
                )
                
                warn_msg = await message.answer(warning_text, parse_mode="HTML", disable_web_page_preview=True)
                logger.info(f"Предупреждение отправлено в топик {current_topic}")
                
                # 3. Ждем 10 секунд и удаляем НАШЕ предупреждение, чтобы оно не висело долго
                await asyncio.sleep(10)
                await warn_msg.delete()
                logger.info(f"Предупреждение удалено, но режим зачистки топика {current_topic} продолжается...")
                
                # 4. Ждем ЕЩЕ 5 секунд (итого 15 секунд защиты чата), чтобы поймать супер-медленные ответы BR Assist
                await asyncio.sleep(5)
                
                # Полностью выключаем режим зачистки для этого топика
                active_cleanup_topics.discard(current_topic)
                logger.info(f"Режим зачистки топика {current_topic} полностью завершен.")
                
            except Exception as e:
                logger.error(f"Ошибка в процессе модерации: {e}")
                active_cleanup_topics.discard(current_topic)

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
