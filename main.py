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
ALLOWED_TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Твоя точная прямая ссылка на тему "Чат для общения"
CHAT_LINK = "https://t.me"

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


# 3. АБСОЛЮТНО ИЗОЛИРОВАННЫЙ КРАТКОСРОЧНЫЙ ПАТРУЛЬ (Работает строго по числовым ID)
async def quick_cleanup(chat_id, topic_id, cmd_msg_id, warn_msg_id, user_id, first_name, username, command_text):
    # Проверяем чат 4 раза каждые 0.5 секунды для зачистки хвостов
    for cycle in range(4):
        await asyncio.sleep(0.5)
        for next_id in range(cmd_msg_id + 1, cmd_msg_id + 5):
            # Жесткая изоляция: не трогаем именно этот конкретный варн
            if next_id == warn_msg_id:
                continue
            try:
                await bot.delete_message(chat_id=chat_id, message_id=next_id)
                logger.info(f"[Патруль топика {topic_id}]: Удален хвост ID {next_id}")
            except Exception:
                pass

    # Ждем оставшиеся 8 секунд и удаляем строго свой варн по его ID
    await asyncio.sleep(8.0)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=warn_msg_id)
        logger.info(f"Варн ID {warn_msg_id} успешно удален в топике {topic_id}")
    except Exception as err:
        logger.error(f"Не удалось принудительно удалить варн {warn_msg_id}: {err}")

    # --- КРАСИВЫЙ КРАТКИЙ ОТЧЁТ ДЛЯ АДМИНОВ ---
    if ADMIN_CHAT_ID:
        try:
            mention = f"[{first_name}](tg://user?id={user_id})"
            tg_username = f" | @{username}" if username else ""
            
            report_text = (
                "🚨 **Отчёт о нарушении правил чата**\n\n"
                f"👤 **Нарушитель:** {mention}{tg_username}\n"
                f"🆔 **ID пользователя:** `{user_id}`\n"
                f"🏷 **Топик:** `{topic_id}`\n"
                f"⌨️ **Запрещённая команда:** `{command_text}`\n\n"
                "🗑 *Команда пользователя и предупреждение бота успешно зачищены.*"
            )
            # Отправляем текстовый отчёт в админ-чат
            await bot.send_message(chat_id=ADMIN_CHAT_ID, text=report_text, parse_mode="Markdown")
            logger.info(f"Отчёт по пользователю {user_id} успешно отправлен администрации.")
        except Exception as e:
            logger.error(f"Не удалось отправить отчёт админам: {e}")


# 2. ОБЩИЙ ХЕНДЛЕР ДЛЯ МОДЕРАЦИИ И УВЕДОМЛЕНИЙ
@dp.message()
async def moderate_chat(message: Message):
    current_topic = message.message_thread_id if message.message_thread_id else 0

    # Если это разрешенный "Чат для общения" — вообще ничего не трогаем
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # --- СЦЕНАРИЙ 1: ЛЮБОЕ ПРЯМОЕ СООБЩЕНИЕ ОТ СТОРОННЕГО БОТА (ЕСЛИ ВДРУГ ДОЛЕТИТ) ---
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
            logger.info(f"Мгновенно удалено входящее сообщение BR Assistant в топике {current_topic}")
        except Exception:
            pass
        return

    # --- СЦЕНАРИЙ 2: МГНОВЕННОЕ УДАЛЕНИЕ ЗАПРЕЩЕННОЙ КОМАНДЫ ЮЗЕРА ---
    if message.text:
        text = message.text.lower().strip()

        target_commands = (
            "/online", "online", "онлайн",
            "/start", "/help",
            "/car", "/vcont", "/vskin", "/vcraft", "/vblackpass", "/vbiz",
            "/vevent", "/vcases", "/vdonat", "/vrules", "/vdrop",
            "/vduel", "/vduel top", "/vtop chat", "/vduelon", "/vdonat_top", "/vtop",
            "/vcalendar", "/vvodo", "/vfarm", "/vnotif"
        )

        if text.startswith(target_commands):
            try:
                command_msg_id = message.message_id
                chat_id = message.chat.id

                # 1. Мгновенно стираем команду пользователя
                await message.delete()
                logger.info(f"Мгновенно стерта команда пользователя {command_msg_id} в топике {current_topic}")

                # 2. Отправляем предупреждение через HTML
                user_html_mention = f'<a href="tg://user?id={message.from_user.id}">{message.from_user.first_name}</a>'
                warning_text = (
                    f"⚠️ {user_html_mention}, использовать команды бота <b>BR Assistant</b>\n"
                    f"можно строго в теме <b><a href='{CHAT_LINK}'>Чат для общения</a></b>!"
                )

                warn_msg = await message.answer(warning_text, parse_mode="HTML", disable_web_page_preview=True)
                warn_msg_id = warn_msg.message_id
                logger.info(f"Предупреждение отправлено в топик {current_topic} (ID варна: {warn_msg_id})")

                # Передаем изолированные параметры и данные нарушителя в фоновый патруль
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
        
        # ИСПРАВЛЕНО ДЛЯ AIOGRAM 3.X: преобразуем словарь в объект Update
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
