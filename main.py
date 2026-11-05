import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update
from aiohttp import web, ClientSession

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения (Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", 0))
ALLOWED_TOPIC_ID = 18600  # Жестко закрепленный ID темы из ссылки на скриншоте
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Ссылка для нарушителей строго со скриншота
CHAT_LINK = "https://t.me/GameFloid/18600"

# Полный словарь тем
TOPIC_NAMES = {
    1: "General",
    18600: "Чат для общения",
    21617: "Дома/гаражи",
    21619: "Авто",
    21621: "Скины",
    21622: "Аксы",
    21655: "Бизнес",
    21656: "Номерные знаки",
    21657: "SIM-card",
    21658: "Прочие услуги",
    21660: "Правила группы",
    22032: "Реклама семьи",
    22422: "Безопасность сделок | Анти-Скам",
    22436: "Жалобы на нарушителей",
    25114: "Новости",
    37914: "Промокоды"
}

# 1. ЗАЩИТА ОТ СНА (ПИНГЕР)
async def keep_alive():
    if not RENDER_EXTERNAL_URL:
        logger.warning("RENDER_EXTERNAL_URL не найдена. Автопинг отключен.")
        return

    logger.info(f"Фоновый автопинг запущен для адреса: {RENDER_EXTERNAL_URL}")
    await asyncio.sleep(10)

    async with ClientSession() as session:
        while True:
            try:
                async with session.get(RENDER_EXTERNAL_URL, timeout=10) as response:
                    logger.info(f"Автопинг выполнен успешно. Статус сервера: {response.status}")
            except Exception as e:
                logger.error(f"Ошибка при выполнении автопинга: {e}")
            await asyncio.sleep(35)

# 3. АБСОЛЮТНО ИЗОЛИРОВАННЫЙ КРАТКОСРОЧНЫЙ ПАТРУЛЬ
async def quick_cleanup(chat_id, topic_id, cmd_msg_id, warn_msg_id, user_id, first_name, username, command_text):
    # --- ОТЧЁТ С НАЗВАНИЕМ ТЕМЫ ДЛЯ АДМИНОВ ---
    await asyncio.sleep(0.5)
    if ADMIN_CHAT_ID:
        try:
            # Безопасное экранирование для HTML
            safe_name = first_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_cmd = command_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            
            mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
            tg_username = f" | @{username}" if username else ""
            
            name_of_topic = TOPIC_NAMES.get(topic_id, str(topic_id))
            
            if topic_id and topic_id != 0:
                # Ссылка для админов
                full_url = f"https://t.me/GameFloid/{topic_id}"
                topic_display = f'<a href="{full_url}">{name_of_topic}</a>'
            else:
                topic_display = "<code>Основной чат</code>"
            
            report_text = (
                "🚨 <b>Отчёт о нарушении правил чата</b>\n\n"
                f"👤 <b>Нарушитель:</b> {mention}{tg_username}\n"
                f"🆔 <b>ID пользователя:</b> <code>{user_id}</code>\n"
                f"🏷 <b>Тема нарушения:</b> {topic_display}\n"
                f"⌨️ <b>Запрещённая команда:</b> <code>{safe_cmd}</code>\n\n"
                "<i>🗑 Команда пользователя и предупреждение бота успешно зачищены.</i>"
            )
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=report_text, parse_mode="HTML")
            logger.info(f"Отчёт по пользователю {user_id} успешно отправлен администрации.")
        except Exception as e:
            logger.error(f"Не удалось отправить отчёт админам: {e}")

    # Патрулируем хвосты в фоне
    try:
        for cycle in range(4):
            await asyncio.sleep(0.5)
            for next_id in range(cmd_msg_id + 1, cmd_msg_id + 5):
                if next_id == warn_msg_id:
                    continue
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=next_id)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Ошибка патруля хвостов: {e}")

    # Ждем, чтобы пользователь успел прочесть варн (10 секунд удержания)
    await asyncio.sleep(10.5)
    
    # Принудительное удаление варна бота
    try:
        await bot.delete_message(chat_id=chat_id, message_id=warn_msg_id)
        logger.info(f"Варн ID {warn_msg_id} успешно удален принудительно.")
    except Exception as err:
        logger.error(f"Не удалось принудительно удалить варн {warn_msg_id}: {err}")

# 2. ОБЩИЙ ХЕНДЛЕР ДЛЯ МОДЕРАЦИИ И УВЕДОМЛЕНИЙ
@dp.message()
async def moderate_chat(message: Message):
    # ЖЕСТКАЯ ЗАЩИТА: Если сообщение из чата админов — бот его ВООБЩЕ не трогает
    if ADMIN_CHAT_ID and message.chat.id == ADMIN_CHAT_ID:
        return

    current_topic = message.message_thread_id if message.message_thread_id else 0

    # Если это разрешенный "Чат для общения" — игнорируем
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # --- СЦЕНАРИЙ 1: ЛЮБОЕ ПРЯМОЕ СООБЩЕНИЕ ОТ СТОРОННЕГО БОТА ---
    is_target_bot = False

    if message.from_user:
        if message.from_user.is_bot:
            is_target_bot = True
        elif message.from_user.username and message.from_user.username.lower() == "brassgay":
            is_target_bot = True

    if message.via_bot and message.via_bot.username and message.via_bot.username.lower() == "brassgay":
        is_target_bot = True

    if is_target_bot:
        try:
            await message.delete()
            logger.info(f"Мгновенно удалено входящее сообщение BR Assistant в теме {current_topic}")
        except Exception:
            pass
        return

    # --- СЦЕНАРИЙ 2: МГНОВЕННОЕ УДАЛЕНИЕ ЗАПРЕЩЕННОЙ КОМАНДЫ ЮЗЕРА ---
    if message.text:
        text = message.text.lower().strip()

                # Полный список всех команд со скриншотов
        target_commands = (
            "/online", "online", "онлайн",
            "/start", "start", "/help", "help",
            "/car", "car", "/cont", "cont", "/skin", "skin", "/craft", "craft", 
            "/blackpass", "blackpass", "/biz", "biz", "/event", "event", 
            "/cases", "cases", "/donat", "donat", "/rules", "rules", "/drop", "drop",
            "/duel", "duel", "/duel_top", "duel_top", "/top_chat", "top_chat", 
            "/duelon", "duelon", "/donat_top", "donat_top", "/top", "top",
            "/calendar", "calendar", "/vodo", "vodo", "/farm", "farm", "/notif", "notif"
        )


        if text.startswith(target_commands):
            try:
                command_msg_id = message.message_id
                chat_id = message.chat.id

                # 1. Мгновенно стираем команду пользователя
                await message.delete()
                logger.info(f"Мгновенно стерта команда пользователя {command_msg_id} в теме {current_topic}")

                # 2. Отправляем предупреждение через HTML
                safe_first_name = message.from_user.first_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                user_mention = f'<a href="tg://user?id={message.from_user.id}">{safe_first_name}</a>'
                
                warning_text = (
                    f"⚠️ {user_mention}, использовать команды бота <b>BR Assistant</b>\n"
                    f'можно строго в теме <b><a href="{CHAT_LINK}">Чат для общения</a></b>!'
                )

                warn_msg = await message.answer(warning_text, parse_mode="HTML", disable_web_page_preview=True)
                warn_msg_id = warn_msg.message_id
                logger.info(f"Предупреждение отправлено в тему {current_topic} (ID варна: {warn_msg_id})")

                # Передаем параметры в фоновый патруль
                asyncio.create_task(
                    quick_cleanup(
                        chat_id=chat_id,
                        topic_id=current_topic,
                        cmd_msg_id=command_msg_id,
                        warn_msg_id=warn_msg_id,
                        user_id=message.from_user.id,
                        first_name=message.from_user.first_name,
                        username=message.from_user.username,
                        command_text=message.text
                    )
                )

            except Exception as e:
                logger.error(f"Ошибка в процессе изолированной модерации: {e}")

# 4. ОБРАБОТЧИК ВЕБХУКОВ
async def handle_webhook(request):
    if request.method == "GET":
        return web.Response(text="Сервер активен, функция автопинга работает.", status=200)

    try:
        if not request.can_read_body:
            logger.warning("Получен пустой POST-запрос (возможно, пинг от Render)")
            return web.Response(text="Empty body", status=400)

        json_data = await request.json()
        
        telegram_update = Update(**json_data)
        await dp.feed_update(bot, telegram_update)
        
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

    if RENDER_EXTERNAL_URL:
        await bot.set_webhook(url=RENDER_EXTERNAL_URL, drop_pending_updates=True)
        logger.info(f"Вебхук установлен на: {RENDER_EXTERNAL_URL}")

    asyncio.create_task(keep_alive())

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
