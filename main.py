import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup
from aiohttp import web, ClientSession

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
# ID темы "Чат общения", где разрешено использовать команды
ALLOWED_TOPIC_ID = int(os.getenv("TOPIC_ID", 0))
# URL твоего приложения на Render
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Твоя точная прямая ссылка на тему "Чат общения"
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

# 2. ОБЩИЙ ХЭНДЛЕР ДЛЯ МОДЕРАЦИИ И УВЕДОМЛЕНИЙ
@dp.message()
async def moderate_chat(message: Message):
    current_topic = message.message_thread_id if message.message_thread_id else 0

    # Если это разрешенный "Чат общения" — вообще ничего не трогаем
    if current_topic == ALLOWED_TOPIC_ID:
        return

    # --- СЦЕНАРИЙ 1: ЛЮБОЕ ПРЯМОЕ СООБЩЕНИЕ ОТ СТОРОННЕГО БОТА (ЕСЛИ ДОЛЕТИТ ВЕБХУК) ---
    is_target_bot = False
    
    if message.from_user:
        if message.from_user.is_bot:
            is_target_bot = True
        elif message.from_user.username and message.from_user.username.lower() == "brasggay":
            is_target_bot = True

    if message.via_bot and message.via_bot.username and message.via_bot.username.lower() == "brasggay":
        is_target_bot = True

    if is_target_bot:
        try:
            await message.delete()
            logger.info(f"Мгновенно удалено прямое сообщение BR Assistant в топике {current_topic}")
        except Exception:
            pass
        return

    # --- СЦЕНАРИЙ 2: ПЕРЕХВАТ И ИНВЕРТИРОВАННАЯ ОЧИСТКА С УНИЧТОЖЕНИЕМ КЛАВИАТУРЫ ---
    if message.text:
        text = message.text.lower().strip()

        target_commands = (
            "/online", "online", "онлайн",
            "/start", "/help",
            "/car", "/cont", "/skin", "/craft", "/blackpass", "/biz",
            "/event", "/cases", "/donat", "/rules", "/drop",
            "/duel", "/duel top", "/top chat", "/duelon", "/donat_top", "/top",
            "/calendar", "/vodo", "/farm", "/notif"
        )

        if text.startswith(target_commands):
            try:
                command_msg_id = message.message_id
                chat_id = message.chat.id
                user_id = message.from_user.id
                user_first_name = message.from_user.first_name

                logger.info(f"Обнаружена команда (ID: {command_msg_id}) в топике {current_topic}. Запуск дожима кнопок...")

                # Запускаем асинхронную задачу дожима
                async def smart_inverted_cleanup(chat_id, cmd_id, u_id, u_name):
                    # Даем BR Assistant 0.3 секунды гарантированно выплюнуть ответ на команду
                    await asyncio.sleep(0.3)

                    # Проверяем появление ответа в течение 4 секунд (8 циклов по 0.5 сек)
                    for cycle in range(8):
                        # Прочесываем ID сообщений вперед
                        for next_id in range(cmd_id + 1, cmd_id + 11):
                            try:
                                # ХИТРОСТЬ: Принудительно очищаем инлайн-кнопки (схлопываем меню подписки)
                                await bot.edit_message_reply_markup(
                                    chat_id=chat_id, 
                                    message_id=next_id, 
                                    reply_markup=None
                                )
                                logger.info(f"Цикл {cycle+1}: Схлопнуты инлайн-кнопки у сообщения ID {next_id}")
                            except Exception:
                                pass

                            try:
                                # Сразу же удаляем само очищенное сообщение
                                await bot.delete_message(chat_id=chat_id, message_id=next_id)
                                logger.info(f"Цикл {cycle+1}: Точечно удален очищенный текст бота (ID: {next_id})")
                            except Exception:
                                pass
                        
                        await asyncio.sleep(0.5)

                    # Когда чужой бот полностью обезврежен, удаляем исходную команду пользователя
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=cmd_id)
                        logger.info(f"Исходная команда юзера (ID: {cmd_id}) удалена.")
                    except Exception as e:
                        logger.warning(f"Не удалось удалить команду юзера: {e}")

                    # Отправляем чистое предупреждение
                    try:
                        user_html_mention = f'<a href="tg://user?id={u_id}">{u_name}</a>'
                        warning_text = (
                            f"⚠️ {user_html_mention}, использовать команды бота <b>BR Assistant</b>\n"
                            f"можно строго в теме <b><a href='{CHAT_LINK}'>Чат общения</a></b>!"
                        )
                        warn_msg = await bot.send_message(
                            chat_id=chat_id, 
                            text=warning_text, 
                            parse_mode="HTML", 
                            message_thread_id=current_topic,
                            disable_web_page_preview=True
                        )
                        logger.info(f"Предупреждение успешно отправлено в финале (ID: {warn_msg.message_id})")
                        
                        # Варн висит 10 секунд и удаляется
                        await asyncio.sleep(10.0)
                        await warn_msg.delete()
                        logger.info("Временное предупреждение успешно удалено по таймеру.")
                    except Exception as err:
                        logger.error(f"Ошибка при обработке варна: {err}")

                asyncio.create_task(smart_inverted_cleanup(chat_id, command_msg_id, user_id, user_first_name))

            except Exception as e:
                logger.error(f"Ошибка инициализации модерации: {e}")

# 3. ОБРАБОТЧИК ВЕБХУКОВ
async def handle_webhook(request):
    if request.method == "GET":
        return web.Response(text="Сервер активен. Функция автопинга работает.", status=200)
    
    try:
        if not request.can_read_body:
            logger.warning("Получен пустой POST-запрос (возможно, пинг от Render)")
            return web.Response(text="Empty body", status=400)
            
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
