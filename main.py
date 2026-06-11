import os
import logging
import asyncio
import random
import pytz
import asyncpg
import aiogram
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from datetime import datetime
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter, Command as CommandFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
import google.generativeai as genai
import aiohttp
import re
import json
import time

# Список команд для фільтрації
BOT_COMMANDS = ["start", "cancel", "admin", "discount", "check_discounts", "use_discount", "users"]

# НАЛАШТУВАННЯ
API_TOKEN = os.getenv("API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# --- НАЛАШТУВАННЯ ДЛЯ ШІ ТА КАНАЛУ ПОМІЧНИКА ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
AUTO_POST_CHAT_ID = os.getenv("AUTO_POST_CHAT_ID")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID"))
    REVIEWS_CHAT_ID = int(os.getenv("REVIEWS_CHAT_ID"))
    FEEDBACK_HOUR = int(os.getenv("FEEDBACK_HOUR", "11"))
    FEEDBACK_MINUTE = int(os.getenv("FEEDBACK_MINUTE", "0"))
    ASSISTANT_HOUR = int(os.getenv("ASSISTANT_HOUR", "22"))
    ASSISTANT_MINUTE = int(os.getenv("ASSISTANT_MINUTE", "45"))
except ValueError:
    raise ValueError("ADMIN_ID, REVIEWS_CHAT_ID, FEEDBACK_HOUR та FEEDBACK_MINUTE мають бути цілими числами!")

if not API_TOKEN or not DATABASE_URL:
    raise ValueError("Помилка: API_TOKEN або DATABASE_URL не встановлені в Environment Variables!")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

ukraine_tz = pytz.timezone('Europe/Kyiv')
scheduler = AsyncIOScheduler(timezone=ukraine_tz)

# Ініціалізація Google Gemini для Електронного помічника
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    ai_model = genai.GenerativeModel('gemini-2.5-flash')
else:
    ai_model = None
    logging.warning("⚠️ GOOGLE_API_KEY не знайдено. Електронний помічник (ШІ) вимкнено.")

# СТАНИ
class TourRequest(StatesGroup):
    start_confirmed = State()
    destination = State()
    adults_count = State()
    children_count = State()
    date_from = State()
    date_to = State()
    nights_count = State()
    hotel_stars = State()
    meal_type = State()
    budget = State()
    contact = State()
    
class AdminPanel(StatesGroup):
    waiting_for_client_info = State()
    waiting_for_date = State()

class FeedbackState(StatesGroup):
    waiting_for_rating = State()
    waiting_for_text = State()

async def save_msg(message: types.Message, state: FSMContext):
    data = await state.get_data()
    msgs = data.get("msgs_to_delete", [])
    msgs.append(message.message_id)
    await state.update_data(msgs_to_delete=msgs)

async def clean_admin_messages(state: FSMContext, chat_id: int):
    """Видаляє всі зареєстровані тимчасові повідомлення"""
    data = await state.get_data()
    msgs_to_delete = data.get("admin_msgs_to_clean", [])
    for msg_id in msgs_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass
    await state.update_data(admin_msgs_to_clean=[])

async def show_admin_base(message: types.Message, state: FSMContext):
    """Надсилає актуальний список туристів зі статусами відгуків"""
    global pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                u.user_id, u.username, u.full_name, 
                d.discount_value,
                f.return_date, f.sent
            FROM users u 
            LEFT JOIN discounts d ON u.user_id = d.user_id AND d.is_used = FALSE
            LEFT JOIN (
                SELECT DISTINCT ON (user_id) user_id, return_date, sent 
                FROM feedbacks 
                ORDER BY user_id, id DESC
            ) f ON u.user_id = f.user_id
        """)

    text = "👥 <b>Список туристів:</b>\n━━━━━━━━━━━━━━━\n"
    
    if not rows:
        text += "База порожня."
    else:
        for row in rows:
            username = f"@{row['username']}" if row['username'] else "немає"
            name = row['full_name'] or "Ім'я не вказано"
            discount = f" | 🎁 {row['discount_value']}%" if row['discount_value'] else ""
            
            feedback_status = ""
            if row['return_date']:
                if row['sent'] == 1:
                    feedback_status = f"\n     └ ✅ Запит на відгук надіслано ({row['return_date']})"
                else:
                    feedback_status = f"\n     └ ⏳ Запит на відгук заплановано ({row['return_date']})"

            text += f"👤 <b>{name}</b> — {username} (<code>{row['user_id']}</code>){discount}{feedback_status}\n"
    
    text += "━━━━━━━━━━━━━━━"
    
    new_msg = await bot.send_message(chat_id=message.chat.id, text=text, parse_mode="HTML")
    
    data = await state.get_data()
    current_msgs = data.get("admin_msgs_to_clean", [])
    current_msgs.append(new_msg.message_id)
    await state.update_data(admin_msgs_to_clean=current_msgs)

pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=2
    )
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS discounts (
                user_id BIGINT PRIMARY KEY,
                discount_value INTEGER,
                is_used BOOLEAN DEFAULT FALSE
                )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feedbacks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                return_date TEXT,
                sent INTEGER DEFAULT 0
                )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT
                )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_posts (
                message_id INTEGER PRIMARY KEY
            )
        """)
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name TEXT")
        except Exception as e:
            logging.info(f"Колонка full_name вже існує або помилка: {e}")

async def save_user(user: types.User):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, full_name) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "username = EXCLUDED.username, full_name = EXCLUDED.full_name",
            user.id, user.username, user.full_name
        )

async def get_user_discount(user_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", 
            user_id
        )

async def check_returns():
    now = datetime.now(ukraine_tz)
    today = now.strftime("%d.%m.%Y")
    logging.info(f"🔎 [SCHEDULER] Перевірка bases на дату: {today}")
    
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id FROM feedbacks WHERE return_date = $1 AND sent = 0", 
            today
        )
        
        logging.info(f"📊 [SCHEDULER] Знайдено записів: {len(rows)}")
        
        for row in rows:
            try:
                await bot.send_message(
                    row['user_id'],
                    "✈️ З поверненням! Сподіваємося, Ваш відпочинок був чудовим.\n\nБудь ласка, оцініть нашу роботу:",
                    reply_markup=rating_kb()
                )
                await conn.execute(
                    "UPDATE feedbacks SET sent = 1 WHERE user_id = $1 AND return_date = $2", 
                    row['user_id'], today
                )
                logging.info(f"✅ [SCHEDULER] Відгук надіслано ID: {row['user_id']}")
            except Exception as e:
                logging.error(f"❌ [SCHEDULER] Помилка для ID {row['user_id']}: {e}")

# КЛАВІАТУРИ
def start_inline_kb():
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="🚀 ПОЧАТИ ПІДБІР ТУРУ", callback_data="start_selection"))
    return builder.as_markup()

def rating_kb():
    builder = InlineKeyboardBuilder()
    for i in range(1, 6):
        builder.add(types.InlineKeyboardButton(text=f"{i}⭐", callback_data=f"rate_{i}"))
    builder.adjust(5)
    return builder.as_markup()

def stars_kb():
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="3*", callback_data="star_3"),
        types.InlineKeyboardButton(text="4*", callback_data="star_4"),
        types.InlineKeyboardButton(text="5*", callback_data="star_5"))
    builder.add(types.InlineKeyboardButton(text="Будь-яка", callback_data="star_any"))
    builder.adjust(3, 1)
    return builder.as_markup()

def meals_kb():
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="Сніданки (BB)", callback_data="meal_BB"),
        types.InlineKeyboardButton(text="Сніданок+вечеря (HB)", callback_data="meal_HB"),
        types.InlineKeyboardButton(text="Все включено (AI)", callback_data="meal_AI"),
        types.InlineKeyboardButton(text="Ультра все включено (UAI)", callback_data="meal_UAI"),
        types.InlineKeyboardButton(text="Без харчування (RO)", callback_data="meal_RO"))
    builder.adjust(1)
    return builder.as_markup()

def generate_discount():
    chance = random.random()
    if chance < 0.80:
        return random.randint(2, 3)
    elif chance < 0.95:
        return 4
    else:
        return 5

# --- ФУНКЦІЇ ЕЛЕКТРОННОГО ПОМІЧНИКА (ПАРСИНГ ТА ШІ) ---

async def fetch_tat_ua_data():
    country_urls = {
        "turkey": "https://tat.ua/search/turkey/",
        "egypt": "https://tat.ua/search/egypt/",
        "bulgaria": "https://tat.ua/search/bulgaria/",
        "greece": "https://tat.ua/search/greece/",
        "cyprus": "https://tat.ua/search/cyprus/",
        "spain": "https://tat.ua/search/spain/",
        "ukraine": "https://tat.ua/search/ukraine/"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://turne.ua/ua/hottours"
    }
    
    logging.info(f"🚀 [ПАРСЕР] Початок ОДНОРАЗОВОГО швидкого збору даних з сортуванням зірковості...")
    cleaned_country_data = {}
    max_pages_per_country = 1 
    
    words_to_clean = [
        "Цена за tour 2 взрослых", "Цена за tour", "Цена за tour 2 взрослых", "ПОДРОБНЕЕ", 
        "Цена за tour", "Цена за тур", "Все типы отдыха", "Горящие туры", "Поиск туров", "Екскурсійні тури"
    ]

    try:
        for country_slug, base_url in country_urls.items():
            logging.info(f"🌍 Збираємо пропозиції для напрямку: {country_slug.upper()}")
            country_raw_blocks = []
            
            for page_num in range(1, max_pages_per_country + 1):
                url = base_url if page_num == 1 else f"{base_url}?page={page_num}"
                
                try:
                    response = requests.get(url, headers=headers, timeout=15)
                    if response.status_code == 200:
                        soup = BeautifulSoup(response.text, 'html.parser')
                        
                        for script in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
                            script.decompose()
                            
                        page_text = " ".join(soup.get_text(separator=" ", strip=True).split())
                        
                        if "грн" in page_text or "ноч" in page_text:
                            # Розбиваємо суцільний текст сторінки на окремі картки готелів
                            raw_hotel_blocks = page_text.split("грн")
                            for b in raw_hotel_blocks:
                                cleaned_block = b.strip()
                                if len(cleaned_block) > 100 and ("ноч" in cleaned_block or "*" in cleaned_block or "★" in cleaned_block):
                                    country_raw_blocks.append(cleaned_block + " грн")
                        else:
                            break
                    else:
                        break
                    time.sleep(0.4)
                except Exception as page_err:
                    logging.error(f"❌ Помилка пагінації {page_num} для {country_slug}: {page_err}")
                    continue

            # --- РОЗУМНЕ СОРТУВАННЯ ТА ПЕРЕМІШУВАННЯ ДЛЯ УНІКАЛЬНОСТІ ЩОДНЯ ---
            if country_raw_blocks:
                hotels_5_stars = []
                hotels_4_stars = []
                hotels_3_stars = []
                
                for block in country_raw_blocks:
                    if "5*" in block or "5★" in block:
                        hotels_5_stars.append(block)
                    elif "4*" in block or "4★" in block:
                        hotels_4_stars.append(block)
                    else:
                        hotels_3_stars.append(block)
                
                # Перемішуємо категорії незалежно одна від одної
                random.shuffle(hotels_5_stars)
                random.shuffle(hotels_4_stars)
                random.shuffle(hotels_3_stars)
                
                # Об'єднуємо назад (5★ гарантовано стають на самий початок списку)
                sorted_blocks = hotels_5_stars + hotels_4_stars + hotels_3_stars
                
                # Беремо велику вибірку з 35 готелів, щоб у ШІ був простір для вибору якісних турів
                selected_blocks = sorted_blocks[:35]
                country_text_combined = " | ".join(selected_blocks)
                
                # Очищення від сміттєвих слів
                for word in words_to_clean:
                    country_text_combined = country_text_combined.replace(word, "").replace(word.lower(), "")
                
                final_chunk = " ".join(country_text_combined.split())
                
                if len(final_chunk) > 200:
                    cleaned_country_data[country_slug] = (
                        f"\n=== ПОЧАТОК БЛОКУ КРАЇНИ: {country_slug.upper()} ===\n"
                        f"{final_chunk}"
                        f"\n=== КІНЕЦЬ БЛОКУ КРАЇНИ: {country_slug.upper()} ===\n"
                    )
                    logging.info(f"🎯 Сортування завершено ({country_slug.upper()}): {len(hotels_5_stars)}шт 5★, {len(hotels_4_stars)}шт 4★. Всього передано ШІ: {len(selected_blocks)} готелів.")
            else:
                logging.warning(f"⚠️ Не вдалося виділити готелі для напрямку {country_slug.upper()}")
                
        return cleaned_country_data if cleaned_country_data else None

    except Exception as e:
        logging.error(f"❌ Загальна помилка під час збору даних: {e}")
        return None


async def generate_and_send_ai_tour_post():
    if not ai_model or not AUTO_POST_CHAT_ID:
        logging.info("🤖 Помічник пропущений: немає моделі ШІ або AUTO_POST_CHAT_ID.")
        return

    # --- БЕЗПЕЧНА ПЕРЕВІРКА ТА ПЕРЕНАПРАВЛЕННЯ ---
    raw_topic_id = os.getenv("NAVIGATOR_DAY_TOPIC_ID")
    if raw_topic_id and raw_topic_id.strip() != "None":
        try:
            NAVIGATOR_DAY_TOPIC_ID = int(raw_topic_id)
            CURRENT_CHAT_ID = AUTO_POST_CHAT_ID
        except ValueError:
            NAVIGATOR_DAY_TOPIC_ID = None
            CURRENT_CHAT_ID = ADMIN_ID
    else:
        NAVIGATOR_DAY_TOPIC_ID = None
        CURRENT_CHAT_ID = ADMIN_ID

    bot_link1 = "https://t.me/NavigatorToursBot?start=welcome"
    bot_link2 = "https://t.me/NavigatorToursBot?start=discount"
    current_date_str = datetime.now().strftime("%d.%m.%Y")

    cta_text = (
        f"⚠️ <b>Зверніть увагу: всі ціни вказані за тур та є актуальними на сьогодні!</b>\n\n"
        f"✈️ Бажаєте забронювати або підібрати інший варіант?\n"
        f"Наш electronic помічник допоможе вам швидко сформувати запит, а професійний менеджер особисто опрацює ваші побажання.\n"
        f"👉 <a href='{bot_link1}'>Залишити запит менеджеру</a>\n\n"
        f"🎁 <b>Приємний бонус:</b> кожен наш клієнт може отримати персональну знижку за програмую лояльності!\n"
        f"👉 <a href='{bot_link2}'>Отримати знижку</a>\n\n"
        f"🗣 <b>Сподобалася добірка?</b>\n"
        f"Поширюйте канал серед знайомих мандрівників — разом шукати вигідні тури цікавіше!"
    )

    # --- 1. ВИДАЛЕННЯ ВЧОРАШНІХ ПОСТІВ З БАЗИ ДАНИХ ПЕРЕД ЗАПУСКОМ ---
    async with pool.acquire() as conn:
        old_rows = await conn.fetch("SELECT message_id FROM daily_posts")
        if old_rows:
            logging.info(f"🧹 Знайдено вчорашні пости для видалення в БД. Кількість: {len(old_rows)}")
            for row in old_rows:
                try:
                    await bot.delete_message(chat_id=CURRENT_CHAT_ID, message_id=row['message_id'])
                except Exception as del_err:
                    logging.warning(f"Не вдалося видалити старий post {row['message_id']}: {del_err}")
            await conn.execute("DELETE FROM daily_posts")
            logging.info("✨ Таблиця вчорашніх постів в БД успішно очищена.")

    # --- 2. ОДНОРАЗОВИЙ СКАНОР ВСЬОГО САЙТУ ЧЕРЕЗ REQUESTS ---
    global_raw_tour_data = await fetch_tat_ua_data()
    if not global_raw_tour_data:
        logging.error("🛑 Не вдалося отримати контент із сайту. Роботу ШІ зупинено.")
        return

    categories = [
        {"name": "ТУРЕЧЧИНА", "slug": "turkey", "flag": "🇹🇷", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО в ТУРЕЧЧИНІ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: TURKEY]) за суворим пріоритетом зірковості."},
        {"name": "ЄГИПЕТ", "slug": "egypt", "flag": "🇪🇬", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО в ЄГИПТІ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: EGYPT]) за суворим пріоритетом зірковості."},
        {"name": "БОЛГАРІЯ", "slug": "bulgaria", "flag": "🇧🇬", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО в БОЛГАРІЇ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: BULGARIA]) за суворим пріоритетом зірковості."},
        {"name": "ГРЕЦІЯ", "slug": "greece", "flag": "🇬🇷", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО в ГРЕЦІЇ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: GREECE]) за суворим пріоритетом зірковості."},
        {"name": "КІПР", "slug": "cyprus", "flag": "🇨🇾", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО на КІПРІ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: CYPRUS]) за суворим пріоритетом зірковості."},
        {"name": "ІСПАНІЯ", "slug": "spain", "flag": "🇪🇸", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО в ІСПАНІЇ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: SPAIN]) за суворим пріоритетом зірковості."},
        {"name": "УКРАЇНА", "slug": "ukraine", "flag": "🇺🇦", "stars": "5★, 4★, 3★", "prompt_part": "Уважно проскануй весь наданий текст. Твоє завдання — вибрати до 5 НАЙКРАЩИХ РІЗНИХ готелів СУТО в УКРАЇНІ (шукай маркер [ПОЧАТОК БЛОКУ КРАЇНИ: UKRAINE]) за суворим пріоритетом зірковості."}
    ]
    
    successful_posts_count = 0

    for index, cat in enumerate(categories):
        if index > 0:
            logging.info(f"⏳ Очікуємо 10 секунд перед обробкою ШІ для напрямку '{cat['name']}'...")
            await asyncio.sleep(10)

        country_data = global_raw_tour_data.get(cat['slug'], "") if global_raw_tour_data else ""
        if not country_data:
            logging.info(f"⏩ Пропущено блок '{cat['name']}', бо в парсері немає даних для цієї країни.")
            continue

        prompt = (
            f"Ти — професійний travel-копірайтер компанії. На основі НАДАНИХ ТЕКСТОВИХ ДАНИХ склади один цікавий, "
            f"структурований і залучаючий пост для Telegram-каналу українською мовою.\n\n"
            f"ТВОЄ ГОЛОВНЕ ЗАВДАННЯ: {cat['prompt_part']}\n\n"
            
            f"🚫  ПРАВИЛО ЛОГІКИ ТРАНСПОРТУ ТА ЕМОДЗІ (ЗАБОРОНА ПЛУТАНИНИ):\n"
            f"1. Автобусні тури маркуються ЛИШЕ символом 🚌 в усьому рядку. Наприклад: '🚌 Автобус із Києва' або '🚌 Автобус зі Львова'. Заборонено в цей рядок ставити літак ✈️!\n"
            f"2. Авіатури маркуються ЛИШЕ символом ✈️ в усьому рядку. Наприклад: '✈️ Авіа з Кишинева' або '✈️ Авіа з Варшави'. Заборонено в цей рядок ставити автобус 🚌!\n"
            f"3. Ніколи не зліплюй емодзі автобуса з текстом про авіарейси. Кожен тип транспорту має відповідати своєму єдиному значку.\n"
            f"4. ЗАБОРОНА ФЕЙКОВИХ ВИЛЬОТІВ З УКРАЇНИ: Наразі авіатури з міст України (Київ, Харків, Львів, Одеса тощо) не здійснюються! Якщо тур авіаційний — місто вильоту обов'язково має бути іноземним (Кишинів, Варшава, Жешув, Сучава, Катовіце тощо). З українських міст можливий ТІЛЬКИ автобусний виїзд чи власний транспорт. Не вигадуй вильоти літаків з України!\n\n"

            f"❌ КАТЕГОРИЧНА ЗАБОРОНА НА ФРАЗИ 'ЗА ЗАПИТОМ', 'УТОЧНЮЙТЕ' ТА АБСТРАКЦІЇ:\n"
            f"- Тобі категорично заборонено використовувати у фінальному тексті фрази: 'з міст Європи', 'уточнюйте', 'за запитом', 'уточнюйте у менеджера', 'деталі за телефоном', 'кількість ночей за запитом'. Пост повинен містити виключно готову, фіксовану та конкретну інформацію для туриста.\n"
            f"- У полі трансферу та виїзду ОБОВ'ЯЗКОВО має бути чітко вказано конкретне місто вильоту або виїзду. Жодних розмитих формулювань. Якщо це авіатур — проаналізуй контекст і вкажи конкретне закордонне місто, з якого виконується цей рейс (наприклад, Кишинів, Варшава тощо).\n"
            f"- Якщо для якогось готелю в наданому тексті замість конкретної дати, кількості ночей чи типу харчування вказано 'уточнюйте' або 'за запитом' — повністю ІГНОРУЙ такий готель і переходь до аналізу наступних варіантів, де є точні та прописані дані.\n\n"
            
            f"⚠️ КРИТИЧНО ВАЖЛИВЕ ПРАВИЛО ПРІОРИТЕТУ ЗІРКОВОСТІ ГОТЕЛІВ:\n"
            f"Ти повинен робити вибірку готелів (до 5 штук) суворо за такому каскадним пріоритетом:\n"
            f"1. Наданий тобі список вже відсортований на рівні Python: на самому початку йдуть преміум-готелі 5★. Твоє головне завдання — вибрати до 5 найкращих готелів 5★. Сформуй добірку виключно або переважно з них.\n"
            f"2. Тільки якщо готелів 5★ у списку виявиться менше 5 штук, добирай решту з готелів зірковості 4★ (4*).\n"
            f"3. Якщо в тексті немає взагалі ні 5★, ні 4★ готелів для цієї країни, тільки в цьому крайньому разі дозволено брати та показувати готелі зірковості 3★ (3*).\n"
            f"Порушення цього пріоритету заборонено! Якщо є вища зірковість — нижчу ігноруй.\n\n"
            
            f"⚠️ ХУДОЖНІЙ ПЕРЕХІД:\n"
            f"У вступному абзаці обов'язково адаптуй фразу-перехід під фактично обрану зірковість готелів. Наприклад, якщо у списку опинилися лише 5★, напиши: 'Ось наша добірка найкращих преміум-готелів 5★...'. Якщо вийшов мікс 5★ та 4★, напиши: 'Ось добірка найкращих готелів 4★ та 5★...'.\n\n"
            
            f"⚠️ ГОЛОВНИЙ КРИТЕРІЙ ВІДБОРУ — ПРІОРІТЕТ ТРАНСПОРТУ ТА ПОВНОГО ПАКЕТУ:\n"
            f"Проаналізуй тип транспорту для готелів та відбере варіанти за суворим каскадним пріоритетом:\n"
            f"1. ПРІОРІТЕТ №1 — АВІАТУРИ: Шукай у тексті готелі, де вказано авіапереліт (літак/авіа з Кишинева, Сучави, Жешува, Варшави тощо).\n"
            f"    🔥 КРИТИЧНА ВИМОГА ДЛЯ АВІА: Серед усіх знайдених авіатурів ти зобов'язаний ПЕРШОЧЕРГОВО вибирати варіанти, які є ПОВНИМ ПАКЕТОМ (куди одночасно включено: Проїзд, Страховка та Трансфер до готелю). Формуй добірку з них. Якщо авіатурів у тексті є хоча б 3-5 штук, повністю ігноруй автобуси та власний транспорт!\n"
            f"2. АВТОБУСНІ ТУРИ: Включай автобусні тури у пост ТІЛЬКИ у випадку, якщо в наданих даних взагалі немає авіатурів по цій країні.\n"
            f"    🔥 КРИТИЧНА ВИМОГА ДЛЯ АВТОБУСІВ: Аналогічно, серед автобусних турів вибирай насамперед ті, куди включено повний пакет (Проїзд + Страховка + Трасфер).\n"
            f"3. БЕЗ ТРАНСФЕРУ: Варіанти 'Власний транспорт / Без трансферу' дозволено брати лише в крайньому разі, якщо немає ні авіа, ні автобусів.\n"
            f"Підсумок: Завжди зберігай пріоритет транспорту (Авіа -> Автобус), але ВСЕРЕДИНІ обраного транспорту вибирай ТІЛЬКИ ті тури, де чітко включено проїзд, страховку та трансфер!\n\n"
            
            f"⚠️ КРИТИЧНО ВАЖЛИВЕ ПРАВИЛО ДЛЯ НАЙНИЖЧОЇ ЦІНИ ТА СИНХРОНІЗАЦІЇ:\n"
            f"- Коли алгоритм відібрав готелі за пріоритетом трансферу (наприклад, авіа з повним пакетом), вибирай серед них варіанти за ЯКІСТЮ та НАЙВИГІДНІШОЮ ціною для конкретної кількості ночей. \n"
            f"- НІКОЛИ не зліплюй ціну від дешевого автобусного туру з описом авіатуру! Ціна, тип харчування, трансфер та кількість ночей у шаблоні мають бути СУВОРО СИНХРОНІЗОВАНІ між собою для обраного варіанту.\n"
            f"- Якщо для одного готелю є кілька цін (за 4, 5 чи 7 ночей), виводь ту, яка є найпривабливішою, чітко вказавши відповідну їй кількість ночей.\n"
            f"- Уникай 'оверпрайсу': не вибирай готелі з космічними цінами (наприклад, за 250-500 тис. грн), якщо в тексті є чудові варіанти за 60-100 тис. грн. Шукай 'золоту середину' ціни та сервісу.\n\n"
            
            f"⚠️ СУВОРЕ ПРАВИЛО ДЛЯ АНАЛІЗУ ТА ФОРМАТУ ДАТ (ТОЧНИЙ ПОВТОР З САЙТУ):\n"
            f"1. Знайди в тексті конкретного туру дату вильоту/виїзду (день, месяц, рік), яка прописана поруч із обраною ціною. Заборонено вигадувати дані самостійно.\n"
            f"2. КРИТИЧНО ВАЖЛИВО: Переноси дату в шаблон СУВОРО в тому форматі, в якому вона написана в джерелі (на сайті). "
            f"Якщо на сайті вказано цифровий формат (наприклад, '15.06.2026'), пиши '15.06.2026'. "
            f"Якщо на сайті вказано словами (наприклад, '19 червня' або '5 липня'), ти зобов'язаний перенести саме так: '19 червня' або '5 липня'. "
            f"Нічого від себе не змінюй, не трансформуй формати та не додавай зайвих слів чи літер (наприклад, р., рік тощо).\n\n"
            
            f"⚠️ Суворо дотримуйся наступних правил конструювання текста:\n"
            f"1. НІКОЛИ не згадуй назву сторонніх сайтів чи парсерів.\n"     
            f"2. Текст ОБОВ'ЯЗКОВО має починатися одразу з ХУДОЖНЬОГО ВСТУПУ: напиши один короткий, емоційний та вступний художній абзац про країну {cat['name']}, який яскраво описує переваги відпочинку в цьому напрямку.\n"
            f"3. СУВОРЕ ПРАВИЛО ДЛЯ ВСТУПУ:\n"
            f"- Заборони будь-які технічні чи робочі фрази типу 'Згідно з наявними даними...', 'Ми знайшли...'. Текст повинен виглядати як рекомендація живого експерта.\n"
            f"4. СУВОРЕ ПРАВИЛО ДЛЯ РОЗДІЛЕННЯ ТУРИВ: Відокремлюй картки готелів одну від одної СУВОРО одним порожнім рядком. Категорично ЗАБОРОНЕНО самостійно малювати штучні лінії, ставити знаки мінусів, дефісів чи символи на кшталт '---' між готелями! Тільки порожній рядок.\n"
            f"5. Після художнього вступу виведи список готелів. Для КОЖНОГО готелю суворо використовуй наступний візуальний шаблон (заповнюй дані, зберігаючи емодзі та жирний шрифт, у полі виїзду обов'язково вказуй назву міста):\n\n"
            
            f"📍 <b>{cat['name'].upper()} ([Вкажи регіон/курорт])</b>\n"
            f"🏨 <b>[Назва готелю латиницею в оригіналі] [Зірковість, наприклад: 4* або 5*]</b>\n"
            f"🚌 <b>Трансфер та виїзд:</b> [Залежно від туру, вкажи чітке місто вильоту чи виїзду! Приклади: '✈️ Авіа з Кишинева', '✈️ Авіа з Варшави', '🚌 Автобус із Києва', '🚗 Власний транспорт'. Жодних загальних фраз типу 'міст Європи' чи 'уточнюйте'!]\n"
            f"🍽 <b>Харчування:</b> [Вкажи тип харчування, що відповідає обраній ціні, наприклад: 'Все включено (AI)' або 'Без харчування (RO)']\n"
            f"📅 <b>Виліт/Дата:</b> [Вкажи точну дату туру з тексту СУВОРО в оригінальному форматі сайту: ДД.ММ.РРРР або ДД місяця], [Вкажи кількість ночей саме для цієї ціни]\n"
            f"💰 <b>Ціна:</b> [Вкажи саме вартість для обраного типу трансферу] грн. за 2-х дорослих\n"
            f"<i>[Тут напиши короткий художній опис саме цього готелю. Коротко вкажи реальні матеріальні переваги самого готелю: інфраструктура, перша лінія, басейни, спа, свіжий ремонт, зелена територія, аквапарк тощо]</i>\n"
            
            f"⚠️ ДОДАТКОВІ ОБМЕЖЕННЯ ДЛЯ ОФОРМЛЕННЯ ГОТЕЛІВ:\n"
            f"- СУВОРЕ ПРАВИЛО ДЛЯ ДАТИ: Заборонено примусово міняти формат дати. Відображай символ в символ, як у тексті джерела. Без самодіяльності та зайвих знаків.\n"
            f"- СУВОРЕ ПРАВИЛО ДЛЯ НАЗВИ ГОТЕЛЮ: Виводь назву готелю в оригіналі так, як вона вказана в тексті джерела (латиницею).\n"
            
            f"⚠️ ОБМЕЖЕННЯ: Описуй каждый готель ємно. Твій підсумковий текст має бути не більше за 3000 символів. Використовуй тільки HTML-теги <b> та <i>.\n\n"
            
            f"⚠️ СУВОРЕ ТА КАТЕГОРИЧНЕ ПРАВИЛО ЩОДО СТИЛЮ МІНУСІВ ТА ЗАБОРОНИ НЕВВЕРНЕНОСТІ:\n"
            f"- КАТЕГОРИЧНО ЗАБОРОНЕНО використовувати конструкції: 'Може бути...', 'Здається...', 'Можливо...', 'Не всім сподобається...'. Текст має бути чітким, професійним та констатувати факти.\n"
            f"- Пиши прямо і професійно: НЕ 'Може бути завеликим' -> А 'Велика територія'; НЕ 'Може бути далеко від моря' -> А 'Друга лінія, 400 м до пляжу'; НЕ 'Можливо старі номери' -> А 'Класичний/традиційний номерний фонд'.\n"
            f"- Також повністю заборонені фрази типу: 'Не вказано конкретних матеріальних недоліків в наданому тексті', 'Інформація відсутня'. Якщо даних немає — напиши нейтральний художній нюанс, який підходить усім готелям, але НІКОЛИ не пиши технічні виправдання!.\n\n"
            
            f"Ось текстові дані з готелями СУТО ДЛЯ ЦІЄЇ КРАЇНИ: {country_data}"
        )

        try:
            response = ai_model.generate_content(prompt)
            post_text = response.text
            
            if len(post_text.strip()) < 100 or "📍" not in post_text or "🏨" not in post_text:
                logging.info(f"⏩ Пропущено блок '{cat['name']}', бо в згенерованому ШІ тексті немає карток готелів (можливо, країна зараз відсутня у вивантаженні).")
                continue

            header_text = f"🧭 <b>Навігатор дня: {cat['name'].upper()} {cat['flag']} | {current_date_str}</b>\n\n"
            full_message = f"{header_text}{post_text}"
            
            msg = await bot.send_message(
                chat_id=CURRENT_CHAT_ID, 
                text=full_message, 
                parse_mode="HTML",
                message_thread_id=NAVIGATOR_DAY_TOPIC_ID if NAVIGATOR_DAY_TOPIC_ID else None,
                disable_web_page_preview=True
            )

            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO daily_posts (message_id) VALUES ($1)", msg.message_id)
                
            successful_posts_count += 1
            logging.info(f"✅ Пост для категорії '{cat['name']}' успішно опубліковано!")
            
        except Exception as ai_err:
            logging.error(f"❌ Помилка роботи ШІ Gemini для категорії {cat['name']}: {ai_err}")

    # --- НАДСИЛАЄТЬСЯ ОБ'ЄДНАНИЙ ФІНАЛЬНИЙ БЛОК (ЯКЩО БУЛИ ПУБЛІКАЦІЇ КРАЇН) ---
    if successful_posts_count > 0:
        try:
            logging.info("⏳ Очікуємо 2 секунди перед надсиланням фінального блоку...")
            await asyncio.sleep(2)
            final_msg = await bot.send_message(
                chat_id=CURRENT_CHAT_ID,
                text=cta_text,
                parse_mode="HTML",
                message_thread_id=NAVIGATOR_DAY_TOPIC_ID if NAVIGATOR_DAY_TOPIC_ID else None,
                disable_web_page_preview=True
            )
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO daily_posts (message_id) VALUES ($1)", final_msg.message_id)
            logging.info(f"✅ Фінальний CTA-пост успішно опубліковано!")
        except Exception as final_err:
            logging.error(f"❌ Помилка надсилання фінального повідомлення: {final_err}")
            
# --- ОБРОБНИКИ КОМАНД (ВЕРХНІЙ ПРІОРИТЕТ) ---

@dp.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    global pool 
    args = command.args
    user = message.from_user
    name = user.full_name or "Мандрівник"
    await save_user(user)
    
    if args == "discount":
        existing_discount = await get_user_discount(user.id)
        if existing_discount:
            discount = existing_discount['discount_value']
            greeting = f"Вітаємо, {name}! 🎁 У вас є активна знижка: {discount}%.\nВикористайте її під час бронювання наступного туру!"
        else:
            discount = generate_discount()
            await pool.execute("""
                INSERT INTO discounts (user_id, discount_value, is_used) 
                VALUES ($1, $2, FALSE) 
                ON CONFLICT (user_id) DO UPDATE 
                SET discount_value = EXCLUDED.discount_value, is_used = FALSE
                """, user.id, discount)
            greeting = f"Вітаємо, {name}! 🎁 Ви активували знижку {discount}%."
    else:
        discount_row = await get_user_discount(user.id)
        if discount_row:
            greeting = f"Вітаємо, {name}! 🎁 У вас є активна знижка: {discount_row['discount_value']}%.\nВикористайте її під час бронювання наступного туру!"
        else:
            greeting = f"Вітаємо, {name}! Я допоможу вам підібрати тур."
    
    # ВІДПРАВКА: Текст вітання + Кнопка в одному повідомленні
    msg = await message.answer(greeting, reply_markup=start_inline_kb())
    
    await state.set_state(TourRequest.start_confirmed)
    
    # ЗБЕРЕЖЕННЯ: Зберігаємо вхідне повідомлення від юзера та відповідь бота
    await save_msg(message, state)
    await save_msg(msg, state)

@dp.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Дія скасована. Тепер ви можете вільно користуватися іншими командами.", 
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(Command("discount"), StateFilter("*"))
async def cmd_discount(message: types.Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    name = user.full_name or "Мандрівник"
    user_id = user.id
    username = user.username or "none"

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, full_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id, username, name)
        row = await conn.fetchrow(
            "SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", 
            user_id
        )

        if row:
            discount = row['discount_value']
            text = f"🎁 Вітаємо, {name}, у вас є активна знижка: **{discount}%**\nВикористайте її під час бронювання наступного туру!"
        else:
            chance = random.random()
            if chance < 0.70:
                discount = random.randint(2, 3)
            elif chance < 0.95:
                discount = 4
            else:
                discount = 5

            await conn.execute("""
                INSERT INTO discounts (user_id, discount_value, is_used) 
                VALUES ($1, $2, FALSE)
                ON CONFLICT (user_id) DO UPDATE SET discount_value = $2, is_used = FALSE
            """, user_id, discount)

            text = f"Вітаємо, {name}! 🎉 Ви виграли знижку на наступну подорож: **{discount}%.**\nВикористайте її під час бронювання наступного туру!"

    # 5. Встановлюємо стан та відправляємо відповідь
    await state.set_state(TourRequest.start_confirmed)
    await message.answer(
        text, 
        parse_mode="Markdown", 
        reply_markup=start_inline_kb()
    )

@dp.message(Command("check_discounts"), F.from_user.id == ADMIN_ID, StateFilter("*"))
async def check_active_discounts(message: types.Message, state: FSMContext):
    await clean_admin_messages(state, message.chat.id)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, discount_value FROM discounts WHERE is_used = FALSE")
        if not rows:
            msg = await message.answer("Активних знижок зараз немає.")
            await state.update_data(admin_msgs_to_clean=[message.message_id, msg.message_id])
            return
        text = "🎁 <b>Список клієнтів з активними знижками:</b>\n"
        for row in rows:
            text += f"👤 ID: <code>{row['user_id']}</code> — {row['discount_value']}%\n"
        new_msg = await message.answer(text, parse_mode="HTML")
        await state.update_data(admin_msgs_to_clean=[message.message_id, new_msg.message_id])

@dp.message(Command("admin"), F.from_user.id == ADMIN_ID, StateFilter("*"))
async def admin_start(message: types.Message, state: FSMContext):
    await clean_admin_messages(state, message.chat.id)
    try: await message.delete() # Видаляємо текст "/admin"
    except: pass
    await state.clear()
    msg = await message.answer("🛠 <b>Панель менеджера</b>\n\nВведіть <b>ID</b> або <b>Username</b> клієнта:", parse_mode="HTML")
    await state.update_data(admin_msgs_to_clean=[msg.message_id])
    await show_admin_base(message, state)
    await state.set_state(AdminPanel.waiting_for_client_info)

@dp.message(Command("users"), F.from_user.id == ADMIN_ID, StateFilter("*"))
async def list_users(message: types.Message, state: FSMContext):
    await clean_admin_messages(state, message.chat.id)
    try: await message.delete() # Видаляємо текст "/users"
    except: pass
    await show_admin_base(message, state)

@dp.message(Command("use_discount"), F.from_user.id == ADMIN_ID, StateFilter("*"))
async def start_use_discount(message: types.Message, state: FSMContext):
    await clean_admin_messages(state, message.chat.id)
    try: 
        await message.delete()
    except: 
        pass
    await state.clear()
    
    async with pool.acquire() as conn:
        # Додаємо username до запиту, щоб використати його в кнопці
        rows = await conn.fetch("""
            SELECT u.user_id, u.full_name, u.username, d.discount_value 
            FROM users u 
            JOIN discounts d ON u.user_id = d.user_id 
            WHERE d.is_used = FALSE
        """)
        
    if not rows:
        msg = await message.answer("❌ Немає активних знижок.")
        await state.update_data(admin_msgs_to_clean=[msg.message_id])
        await show_admin_base(message, state)
        return

    kb = InlineKeyboardBuilder()
    for row in rows:
        # Формуємо текст кнопки точно як у списку туристів
        username = f"@{row['username']}" if row['username'] else "немає"
        button_text = f"{row['full_name']} — {username} ({row['user_id']}) | 🎁 {row['discount_value']}%"
        
        kb.row(types.InlineKeyboardButton(
            text=button_text, 
            callback_data=f"apply_{row['user_id']}")
        )
        
    msg = await message.answer("🎁 <b>Оберіть клієнта для використання знижки:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    
    # Реєструємо повідомлення для чистки
    data = await state.get_data()
    current_msgs = data.get("admin_msgs_to_clean", [])
    current_msgs.append(msg.message_id)
    await state.update_data(admin_msgs_to_clean=current_msgs)
    
    await show_admin_base(message, state)
    
# --- ОБРОБНИКИ СТАНІВ (З ФІЛЬТРАЦІЄЮ КОМАНД) ---

@dp.message(TourRequest.start_confirmed, ~CommandFilter(commands=BOT_COMMANDS))
async def check_start_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, натисніть на кнопку «🚀 ПОЧАТИ ПІДБІР ТУРУ»")
    await save_msg(msg, state)

@dp.callback_query(F.data == "start_selection")
async def process_start_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear() 
    await callback_query.message.edit_reply_markup(reply_markup=None)
    msg = await callback_query.message.answer("🌍 Вкажіть пріоритетну країну та назву готелю (якщо визначилися)", reply_markup=types.ReplyKeyboardRemove())
    await save_msg(msg, state)
    await state.set_state(TourRequest.destination)

@dp.message(TourRequest.destination, ~CommandFilter(commands=BOT_COMMANDS))
async def process_dest(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    text = message.text.strip().lower()
    if text.isdigit() or len(text) < 2:
        msg = await message.answer("⚠️ Введіть назву країни літерами.")
        await save_msg(msg, state)
        return
    replacements = {
        "турция": "Туреччина", "туреччина": "Туреччина", "турція": "Туреччина", "анталія": "Туреччина (Анталія)", "анталия": "Туреччина (Анталія)", "кемер": "Туреччина (Кемер)", "аланія": "Туреччина (Аланія)", "белек": "Туреччина (Белек)",
        "египет": "Єгипет", "єгипет": "Єгипет", "егіпет": "Єгипет", "єгіпет": "Єгипет", "египт": "Єгипет", "єгіпєт": "Єгипет", "егіпєт": "Єгипет", "шарм": "Єгипет (Шарм-ель-Шейх)", "хургада": "Єгипет (Хургада)", "марса": "Єгипет (Марса-Алам)",
        "болгарія": "Болгарія", "болгария": "Болгарія", "греція": "Греція", "греция": "Греція", "крит": "Греція (Крит)",
        "чорногорія": "Чорногорія", "черногория": "Чорногорія", "хорватія": "Хорватія", "хорватия": "Хорватія",
        "іспанія": "Іспанія", "испания": "Іспанія", "італія": "Італія", "италия": "Італія", "кіпр": "Кіпр", "кипр": "Кіпр",
        "албанія": "Албанія", "албания": "Албанія", "португалія": "Португалія", "португалия": "Португалія", "франція": "Франція", "франция": "Франція",
        "оае": "ОАЕ", "оаэ": "ОАЕ", "емираты": "ОАЕ", "емірати": "ОАЕ", "дубай": "ОАЕ (Дубай)", "дубаи": "ОАЕ (Дубай)",
        "таїланд": "Таїланд", "thailand": "Таїланд", "тайланд": "Таїланд", "тай": "Таїланд", "пхукет": "Таїланд (Пхукет)",
        "мальдіви": "Мальдіви", "мальдивы": "Мальдіви", "мальдиви": "Мальдіви", "домінікана": "Домінікана", "доминикана": "Домінікана",
        "занзібар": "Занзібар", "занзибар": "Занзібар", "шрі ланка": "Шрі-Ланка", "шри ланка": "Шрі-Ланка", "балі": "Балі (Індонезія)", "бали": "Балі (Індонезія)"
    }

    user_text = message.text.strip().lower()
    final_destination = replacements.get(user_text, message.text.strip().capitalize())
    await state.update_data(destination=final_destination)
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="1", callback_data="adults_1"),
        types.InlineKeyboardButton(text="2", callback_data="adults_2"),
        types.InlineKeyboardButton(text="3+", callback_data="adults_3+"))
    msg1 = await message.answer(f"✅ Напрямок: {final_destination}")
    msg2 = await message.answer(f"👤 Оберіть кількість дорослих:", reply_markup=builder.as_markup())
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.adults_count)

@dp.message(TourRequest.adults_count, ~CommandFilter(commands=BOT_COMMANDS))
async def check_adults_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть кількість дорослих натиснувши кнопку вище.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("adults_"), TourRequest.adults_count)
async def process_adults(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup(reply_markup=None)
    count = callback_query.data.split("_")[1]
    await state.update_data(adults=count)
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="Без дітей (0)", callback_data="child_0"))
    builder.add(types.InlineKeyboardButton(text="1", callback_data="child_1"),
        types.InlineKeyboardButton(text="2", callback_data="child_2"),
        types.InlineKeyboardButton(text="3+", callback_data="child_3"))
    builder.adjust(1, 3)
    msg1 = await callback_query.message.answer(f"👤 Дорослих: {count}")
    msg2 = await callback_query.message.answer(f"👶 Скільки буде дітей?", reply_markup=builder.as_markup())
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.children_count)

@dp.message(TourRequest.children_count, ~CommandFilter(commands=BOT_COMMANDS))
async def check_children_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть кількість дітей натиснувши кнопку вище.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("child_"), TourRequest.children_count)
async def process_children(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup(reply_markup=None)
    count = callback_query.data.split("_")[1]
    await state.update_data(children=count)
    msg1 = await callback_query.message.answer(f"👶 Дітей: {count}")
    msg2 = await callback_query.message.answer(
        f"📅 Оберіть дату, з якої можна планувати виліт (З):", 
        reply_markup=await SimpleCalendar().start_calendar()
    )
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.date_from) 

@dp.message(TourRequest.date_from, ~CommandFilter(commands=BOT_COMMANDS))
async def check_date_from_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть дату на календарі вище.")
    await save_msg(msg, state)

@dp.callback_query(SimpleCalendarCallback.filter(), TourRequest.date_from)
async def process_date_from(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        formatted = date.strftime("%d.%m.%Y")
        await state.update_data(date_from=formatted)
        msg1 = await callback_query.message.answer(f"📅 Дата вильоту (З): {formatted}")
        msg2 = await callback_query.message.answer(
            f"📅 Оберіть дату, до якої можна планувати виліт (ПО):", 
            reply_markup=await SimpleCalendar().start_calendar()
        )
        await save_msg(msg1, state)
        await save_msg(msg2, state)
        await state.set_state(TourRequest.date_to)

@dp.message(TourRequest.date_to, ~CommandFilter(commands=BOT_COMMANDS))
async def check_date_to_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть дату на календарі вище.")
    await save_msg(msg, state)

@dp.callback_query(SimpleCalendarCallback.filter(), TourRequest.date_to)
async def process_date_to(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        formatted = date.strftime("%d.%m.%Y")
        await state.update_data(date_to=formatted)
        msg1 = await callback_query.message.answer(f"✅ Дата вильоту (ПО): {formatted}")
        msg2 = await callback_query.message.answer(f"🌙 На скільки ночей плануєте відпочинок?")
        await save_msg(msg1, state)
        await save_msg(msg2, state)
        await state.set_state(TourRequest.nights_count)

@dp.message(TourRequest.nights_count, ~CommandFilter(commands=BOT_COMMANDS))
async def process_nights(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    nights_input = message.text.strip()
    
    # Перевірка, чи є введений текст числом
    if not nights_input.isdigit():
        msg = await message.answer("⚠️ Будь ласка, введіть кількість ночей тільки цифрами (наприклад: 7):")
        await save_msg(msg, state)
        return

    await state.update_data(nights=nights_input)
    msg = await message.answer("⭐ Оберіть категорію готелю", reply_markup=stars_kb())
    await save_msg(msg, state)
    await state.set_state(TourRequest.hotel_stars)
    
@dp.message(TourRequest.hotel_stars, ~CommandFilter(commands=BOT_COMMANDS))
async def check_stars_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть категорію готелю кнопкою.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("star_"), TourRequest.hotel_stars)
async def process_stars(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup(reply_markup=None)
    star = callback_query.data.split("_")[1]
    label = "Будь-яка" if star == "any" else f"{star}*"
    await state.update_data(stars=label)
    msg1 = await callback_query.message.answer(f"⭐ Готель: {label}")
    msg2 = await callback_query.message.answer(f"🍴 Яке харчування Вам підходить:", reply_markup=meals_kb())
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.meal_type)

@dp.message(TourRequest.meal_type, ~CommandFilter(commands=BOT_COMMANDS))
async def check_meals_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть тип харчування кнопкою.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("meal_"), TourRequest.meal_type)
async def process_meals(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup(reply_markup=None)
    meal_map = {"BB": "Сніданки", "HB": "Сніданок+вечеря", "AI": "Все включено", "UAI": "Ультра все включено", "RO": "Без харчування"}
    meal_text = meal_map.get(callback_query.data.split("_")[1], "Будь-яке")
    await state.update_data(meals=meal_text)
    msg1 = await callback_query.message.answer(f"🍴 Харчування: {meal_text}")
    msg2 = await callback_query.message.answer(f"💰 Який Ви плануєте витратити бюджет у гривнях (цифрами):")
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.budget)

@dp.message(TourRequest.budget, ~CommandFilter(commands=BOT_COMMANDS))
async def process_budget(message: types.Message, state: FSMContext):
    budget_raw = message.text.lower().replace(" ", "").replace("грн", "").replace("$", "").replace("usd", "").replace("eur", "")
    
    if not budget_raw.isdigit():
        msg = await message.answer("⚠️ Будь ласка, введіть бюджет лише цифрами (наприклад: 20000):")
        await save_msg(msg, state)
        return

    await save_msg(message, state)
    await state.update_data(budget=budget_raw)
    msg = await message.answer("📞 Ваш номер телефону або нікнейм для зв'язку:")
    await save_msg(msg, state)
    await state.set_state(TourRequest.contact)

@dp.message(TourRequest.contact, ~CommandFilter(commands=BOT_COMMANDS))
async def process_contact(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    data = await state.get_data()
    user = message.from_user
    async with pool.acquire() as conn:
        discount_row = await conn.fetchrow(
        "SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", 
        user.id
        )
    discount_status = f"{discount_row['discount_value']}%" if discount_row else "Немає"
    info_table = (
f"🌍 <b>Напрямок:</b> {data.get('destination')}\n"
f"👥 <b>Склад:</b> {data.get('adults')} дор. + {data.get('children')} діт.\n"
f"📅 <b>Дати початку туру:</b> {data.get('date_from')} - {data.get('date_to')}\n"
f"🌙 <b>Ночей:</b> {data.get('nights')}\n"
f"⭐ <b>Готель:</b> {data.get('stars')}\n"
f"🍴 <b>Харчування:</b> {data.get('meals')}\n"
f"💰 <b>Бюджет:</b> {data.get('budget')} ГРН\n"
f"🎁 <b>Знижка:</b> {discount_status}\n"
f"📱 <b>Контакт:</b> {message.text}"
    )
    report = (
f"🔥 <b>НОВА ЗАЯВКА НА ТУР!</b>\n"
f"━━━━━━━━━━━━━━━\n"
f"{info_table}\n"
f"━━━━━━━━━━━━━━━\n"
f"👤 <b>Клієнт:</b> <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
f"🆔 <b>Username:</b> @{user.username if user.username else 'немає'}\n"
f"🆔 <b>ID для відгуку:</b> <code>{user.id}</code>\n"
f"━━━━━━━━━━━━━━━"
    )
    await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
    msgs_to_delete = data.get("msgs_to_delete", [])
    tasks = [bot.delete_message(chat_id=message.chat.id, message_id=m_id) for m_id in msgs_to_delete]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    re_builder = ReplyKeyboardBuilder()
    re_builder.add(types.KeyboardButton(text="🔄 СТВОРИТИ НОВУ ЗАЯВКУ"))
    await message.answer(
f"✅ Дякуємо! Заявку успішно відправлено!\nМи зв'яжемося з Вами найближчим часом 😊\n\n"
f"<b>ДЕТАЛІ ВАШОЇ ЗАЯВКИ:</b>\n"
f"━━━━━━━━━━━━━━━\n"
f"{info_table}\n"
f"━━━━━━━━━━━━━━━", 
        parse_mode="HTML",
        reply_markup=re_builder.as_markup(resize_keyboard=True)
    )
    await state.clear()

# --- ОБРОБНИКИ ВІДГУКІВ ---
@dp.callback_query(F.data.startswith("rate_"))
async def process_rating(callback_query: types.CallbackQuery, state: FSMContext):
    rating = int(callback_query.data.split("_")[1])
    await state.update_data(user_rating=rating)
    await callback_query.message.edit_text(
        f"Ви поставили {rating}⭐!\n"
        "Будь ласка, напишіть декілька слів про Вашу подорож (Ваш відгук буде опубліковано у чаті мандрівників):"
    )
    await state.set_state(FeedbackState.waiting_for_text)

async def delayed_feedback_reply(forwarded_msg, rating):
    wait_time = random.randint(60, 600)
    await asyncio.sleep(wait_time)
    if rating == 5:
        reply_text = "😍 Неймовірно! Ми дуже раді, що відпочинок пройшов ідеально. Дякуємо, що обираєте нас! ❤️"
    elif rating == 4:
        reply_text = "😊 Дякуємо за відгук! Раді, що вам сподобалося. Будемо чекати на вас знову! ✨"
    elif rating == 3:
        reply_text = "🙏 Дякуємо за ваш відгук. Ми обов'язково врахуємо ваші зауваження, щоб стати кращими!"
    else: 
        reply_text = "😔 Нам дуже прикро, що ви залишилися незадоволені. Менеджер вже вивчає ситуацію, щоб зв'язатися з вами та все владнати."
    try:
        await forwarded_msg.reply(reply_text)
    except Exception as e:
        logging.error(f"Error sending delayed reply: {e}")

@dp.message(FeedbackState.waiting_for_text, ~CommandFilter(commands=BOT_COMMANDS))
async def process_feedback_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("user_rating")
    user = message.from_user
    feedback_header = (
f"🌟 <b>НОВИЙ ВІДГУК!</b>\n"
f"━━━━━━━━━━━━━━━\n"
f"👤 <b>Від:</b> {user.full_name}\n"
f"📱 <b>Username:</b> @{user.username if user.username else 'немає'}\n"
f"⭐ <b>Оцінка:</b> {rating}⭐\n"
f"━━━━━━━━━━━━━━━"
)
    await bot.send_message(REVIEWS_CHAT_ID, feedback_header, parse_mode="HTML")
    forwarded_msg = await message.forward(chat_id=REVIEWS_CHAT_ID)
    await message.answer("❤️ Дякуємо за Ваш відгук! Його опубліковано у чаті мандрівників.")
    await state.clear()
    asyncio.create_task(delayed_feedback_reply(forwarded_msg, rating))

@dp.callback_query(F.data.startswith("apply_"), F.from_user.id == ADMIN_ID)
async def apply_discount_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = int(callback_query.data.split("_")[1])
    await clean_admin_messages(state, callback_query.message.chat.id)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE discounts SET is_used = TRUE WHERE user_id = $1", user_id)
    await callback_query.answer("✅ Знижку використано!")
    try:
        await callback_query.message.delete()
    except:
        pass
    await show_admin_base(callback_query.message, state)

@dp.message(AdminPanel.waiting_for_client_info, ~CommandFilter(commands=BOT_COMMANDS))
async def process_admin_search(message: types.Message, state: FSMContext):
    input_data = message.text.strip().replace("@", "").lower()
    target_id = None
    username = "невідомий"
    
    async with pool.acquire() as conn:
        if input_data.isdigit():
            row = await conn.fetchrow("SELECT user_id, username FROM users WHERE user_id = $1", int(input_data))
            if row:
                target_id = row['user_id']
                username = f"@{row['username']}" if row['username'] else "без юзернейму"
            else:
                target_id = int(input_data)
                username = "Введено вручну (ID)"
        else:
            row = await conn.fetchrow("SELECT user_id, username FROM users WHERE LOWER(username) = $1", input_data)
            if row:
                target_id = row['user_id']
                username = f"@{row['username']}"

    if target_id:
        await state.update_data(client_id=target_id, client_username=username)
        msg = await message.answer(
            f"✅ Клієнта знайдено:\nID: <code>{target_id}</code>\nUser: {username}\n\nТепер оберіть дату повернення:", 
            reply_markup=await SimpleCalendar().start_calendar(),
            parse_mode="HTML"
        )
        data = await state.get_data()
        msgs = data.get("admin_msgs_to_clean", [])
        msgs.extend([message.message_id, msg.message_id])
        await state.update_data(admin_msgs_to_clean=msgs)
        await state.set_state(AdminPanel.waiting_for_date)
    else:
        msg = await message.answer("❌ Клієнта не знайдено в базі. Спробуйте ще раз:")
        data = await state.get_data()
        msgs = data.get("admin_msgs_to_clean", [])
        msgs.extend([message.message_id, msg.message_id])
        await state.update_data(admin_msgs_to_clean=msgs)

@dp.callback_query(SimpleCalendarCallback.filter(), AdminPanel.waiting_for_date)
async def process_admin_date(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        formatted = date.strftime("%d.%m.%Y")
        data = await state.get_data()
        client_id = data.get('client_id')
        username = data.get('client_username')

        async with pool.acquire() as conn:
            # 1. Спочатку видаляємо старі записи для цього юзера, які ще не були відправлені
            await conn.execute(
                "DELETE FROM feedbacks WHERE user_id = $1 AND sent = 0", 
                client_id
            )
            # 2. Записуємо нову актуальну дату
            await conn.execute(
                "INSERT INTO feedbacks (user_id, return_date, sent) VALUES ($1, $2, 0)", 
                client_id, formatted
            )

        await clean_admin_messages(state, callback_query.message.chat.id)
        
        report_msg = await callback_query.message.answer(
            f"✅ <b>Запит на відгук перепризначено!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📅 <b>Нова дата:</b> {formatted}\n"
            f"👤 <b>Клієнт:</b> {username} (<code>{client_id}</code>)\n"
            f"<i>(Старі запити для цього клієнта скасовано)</i>\n"
            f"━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )
        
        # Оновлюємо список повідомлень для видалення та показуємо базу
        await state.update_data(admin_msgs_to_clean=[report_msg.message_id])
        await show_admin_base(callback_query.message, state)
        await state.set_state(None)

async def on_shutdown(app: web.Application):
    global pool
    if pool:
        await pool.close()
        logging.info("Пул БД закрито.")
    scheduler.shutdown()
    logging.info("Планувальник зупинено.")

async def main():
    logging.info("--- БОТ ЗАПУСКАЄТЬСЯ ---")
    await bot.delete_webhook(drop_pending_updates=True)
    await init_db()
    
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super_secret_key")
    await bot.set_webhook(
        url=f"{WEBHOOK_URL}/webhook",
        secret_token=WEBHOOK_SECRET
    )
    
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_shutdown.append(on_shutdown)

    user_commands = [
        types.BotCommand(command="start", description="🚀 Почати підбір туру"), 
        types.BotCommand(command="discount", description="🎁 Моя знижка")
    ]

    admin_commands = user_commands + [
        types.BotCommand(command="admin", description="🛠 Запит на відгук"),
      #  types.BotCommand(command="check_discounts", description="📊 Активні знижки"),
        types.BotCommand(command="use_discount", description="✅ Використати знижку"),
        types.BotCommand(command="users", description="👥 Список туристів")
    ]
   
    await bot.set_my_commands(user_commands, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
 
    scheduler.add_job(check_returns, 'cron', hour=FEEDBACK_HOUR, minute=FEEDBACK_MINUTE)
    
    scheduler.add_job(generate_and_send_ai_tour_post, 'cron', hour=ASSISTANT_HOUR, minute=ASSISTANT_MINUTE)
    
    scheduler.start()
    
    app.router.add_get("/", lambda request: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
