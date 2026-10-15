import asyncio
import logging
import os
import re
import aiohttp
from html import unescape
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
TOPIC_ID = int(os.getenv("TOPIC_ID", 0))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Храним ID последних отправленных постов в памяти
SENT_IDS_POOL = set()

# --- Фоновый веб-сервер для Render ---
async def handle_ping(request):
    return web.Response(text="Бот GameFloid активен и готов к безопасному парсингу!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000)) 
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный сервер успешно запущен на порту {port}!")

# --- Безопасный парсинг данных без использования ElementTree ---
def parse_rss_items(xml_data):
    """Вытаскивает блоки постов, ссылки и описания с помощью регулярных выражений"""
    items = re.findall(r'<item>(.*?)</item>', xml_data, re.DOTALL)
    parsed_items = []
    
    for item in items:
        # Ищем ID (ссылку или guid)
        link_match = re.search(r'<link>(.*?)</link>', item, re.DOTALL)
        if not link_match:
            link_match = re.search(r'<guid[^>]*>(.*?)</guid>', item, re.DOTALL)
        
        # Ищем текст описания
        desc_match = re.search(r'<description>(.*?)</description>', item, re.DOTALL)
        
        if link_match and desc_match:
            post_id = link_match.group(1).strip()
            # Очищаем от возможных XML-оберток CDATA
            post_id = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', post_id, re.DOTALL)
            
            raw_desc = desc_match.group(1)
            raw_desc = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', raw_desc, re.DOTALL)
            
            parsed_items.append({
                "id": post_id,
                "description": raw_desc
            })
            
    return parsed_items

def extract_image_url(html_text):
    if not html_text:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text)
    return match.group(1) if match else None

def clean_html_tags(html_text):
    if not html_text:
        return ""
    # Удаляем картинки и исправляем переносы строк
    cleaned = re.sub(r'<img[^>]+>', '', html_text)
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned)
    # Мягко чистим оставшиеся неподдерживаемые теги, сохраняя текст внутри них
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    return unescape(cleaned).strip()

# --- Функция проверки RSS-ленты (Берет 3 последних поста) ---
async def check_news(force_publish=False):
    logger.info("Запуск безопасной проверки RSS-ленты...")
    rss_url = "https://slarker.me"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(rss_url, timeout=15) as response:
                if response.status != 200:
                    logger.error(f"Зеркало отвело статусом: {response.status}")
                    return "error"
                
                xml_data = await response.text()
                # Используем регулярные выражения вместо ломающегося ElementTree
                items = parse_rss_items(xml_data)
                
                if not items:
                    logger.info("Постов в ленте не обнаружено.")
                    return "empty"
                
                # Берем до 3 самых свежих постов и разворачиваем хронологически
                target_items = items[:3]
                target_items.reverse()
                
                posts_sent_count = 0
                thread_id = TOPIC_ID if TOPIC_ID != 0 else None
                
                global SENT_IDS_POOL
                
                for item in target_items:
                    post_id = item["id"]
                    raw_description = item["description"]
                    
                    if post_id in SENT_IDS_POOL and not force_publish:
                        continue
                    
                    logger.info(f"Безопасная обработка публикации {post_id}...")
                    image_url = extract_image_url(raw_description)
                    clean_text = clean_html_tags(raw_description)
                    
                    if len(clean_text) > 1024:
                        clean_text = clean_text[:1020] + "..."
                    
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
                    
                    SENT_IDS_POOL.add(post_id)
                    posts_sent_count += 1
                    await asyncio.sleep(2)
                
                if posts_sent_count > 0:
                    logger.info(f"Успешно опубликовано постов: {posts_sent_count}")
                    return "sent"
                else:
                    logger.info("Новых постов не появилось.")
                    return "no_new"
                    
        except Exception as e:
            logger.error(f"Ошибка в check_news: {e}", exc_info=True)
            return "error"

# --- ОБРАБОТЧИК КОМАНДЫ /chek ДЛЯ АДМИНИСТРАТОРОВ ---
@dp.message(Command("chek"))
async def cmd_chek(message: Message):
    try:
        member = await bot.get_chat_member(chat_id=TARGET_CHAT_ID, user_id=message.from_user.id)
        
        if member.status in ["administrator", "creator"]:
            status_msg = await message.reply("⏳ Запускаю безопасное сканирование ленты. Собираю 3 последних поста...")
            
            result = await check_news(force_publish=True)
            
            if result == "sent":
                await status_msg.edit_text("✅ Проверка завершена! 3 последних поста успешно отправлены в тему форума.")
            elif result == "empty" or result == "no_new":
                await status_msg.edit_text("🔄 Связь с сервером отличная, но новых постов в ленте не найдено.")
            else:
                await status_msg.edit_text("❌ Произошла непредвиденная ошибка на сервере новостей. Попробуйте позже.")
        else:
            logger.warning(f"Пользователь {message.from_user.id} без прав админа ввел /chek")
    except Exception as e:
        logger.error(f"Ошибка проверки прав админа: {e}")

async def main():
    await start_web_server()
    scheduler.add_job(check_news, "interval", minutes=10)
    scheduler.start()
    
    # Холостой запуск для заполнения пула при старте
    await check_news(force_publish=False)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
