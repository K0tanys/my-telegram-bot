import asyncio
import logging
import os
import re
import aiohttp
import xml.etree.ElementTree as ET
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.types import URLInputFile, Message
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# Настройка логирования для Render
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "br_dev").strip().lstrip("@")
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Храним ID последних отправленных постов в памяти (чтобы избежать дубликатов)
SENT_IDS_POOL = set()

# --- Фоновый веб-сервер для Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и готов к парсингу 3 постов!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный сервер успешно запущен на порту {port}!")

# --- Логика обработки картинок и очистки текста ---
def extract_image_url(html_text):
    if not html_text:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text)
    return match.group(1) if match else None

def clean_html_tags(html_text):
    if not html_text:
        return ""
    cleaned = re.sub(r'<img[^>]+>', '', html_text)
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned)
    return cleaned.strip()

# --- Функция проверки RSS-ленты (Берет 3 последних поста) ---
async def check_news(force_publish=False):
    logger.info("Запуск проверки RSS-ленты (сканируем последние посты)...")
    rss_url = f"https://slarker.me{SOURCE_CHANNEL}"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(rss_url, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Зеркало ответило статусом: {response.status}")
                    return "error"
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                items = root.findall(".//item")
                if not items:
                    logger.info("Лента новостей пуста.")
                    return "empty"
                
                # Берем до 3 самых свежих постов из начала ленты
                target_items = items[:3]
                
                # Чтобы посты публиковались в правильном хронологическом порядке (от старого к новому),
                # разворачиваем список задом наперед
                target_items.reverse()
                
                posts_sent_count = 0
                thread_id = TOPIC_ID if TOPIC_ID != 0 else None
                
                global SENT_IDS_POOL
                
                for item in target_items:
                    post_id = item.find("link").text if item.find("link") is not None else item.find("guid").text
                    raw_description = item.find("description").text
                    
                    # Если этот пост мы уже отправляли И это не принудительный ручной тест команды /chek
                    if post_id in SENT_IDS_POOL and not force_publish:
                        continue
                    
                    logger.info(f"Обработка публикации {post_id}...")
                    image_url = extract_image_url(raw_description)
                    clean_text = clean_html_tags(raw_description)
                    
                    if len(clean_text) > 1024:
                        clean_text = clean_text[:1020] + "..."
                    
                    # Отправка медиа или текста в Telegram
                    if image_url:
                        await bot.send_photo(
                            chat_id=TARGET_CHAT_ID,
                            photo=URLInputFile(image_url),
                            caption=clean_text,
                            message_thread_id=thread_id,
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=clean_text,
                            message_thread_id=thread_id,
                            parse_mode=ParseMode.HTML
                        )
                    
                    # Запоминаем ID, чтобы не слать повторно по таймеру
                    SENT_IDS_POOL.add(post_id)
                    posts_sent_count += 1
                    # Небольшая пауза между отправками, чтобы Telegram не ругался на спам
                    await asyncio.sleep(2)
                
                if posts_sent_count > 0:
                    logger.info(f"Успешно опубликовано постов: {posts_sent_count}")
                    return "sent"
                else:
                    logger.info("Новых публикаций с момента прошлой проверки не появилось.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Ошибка при обработке новостей: {e}", exc_info=True)
            return "error"

# --- ОБРАБОТЧИК КОМАНДЫ /chek ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Запускаю сканирование ленты. Собираю 3 последних поста...")
            
            # Передаем True, чтобы бот принудительно выгрузил 3 верхних поста для теста
            result = await check_news(force_publish=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Проверка завершена! 3 последних поста успешно отправлены в вашу тему форума.")
            elif result == "empty" or result == "no_new":
                await status_msg.edit_text("🔄 Связь с сервером есть, но постов в ленте не найдено.")
            else:
                await status_msg.edit_text("❌ Зеркало новостей временно перегружено. Попробуйте еще раз через минуту.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    
    # Автоматическая проверка каждые 10 минут
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # При самом первом "холодном" старте в облаке бот сразу соберет текущие 3 поста и запомнит их
    await check_news(force_publish=False)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
