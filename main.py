import os
import asyncio
import logging
import random
import time
import importlib
from datetime import datetime, timedelta

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.filters import BaseFilter, Command

import asyncpg
import httpx
from bs4 import BeautifulSoup

# Оновлений офіційний пакет Google GenAI
from google import genai

# Безпечний імпорт асинхронного планувальника для APScheduler v4+
# Оскільки 'async' є зарезервованим словом, використовуємо динамічний імпорт:
try:
    _async_mod = importlib.import_module("apscheduler.schedulers.async")
    AsyncScheduler = getattr(_async_mod, "AsyncScheduler")
except ImportError:
    # Фолбек на випадок старіших версій
    from apscheduler.schedulers.asyncio import AsyncioScheduler as AsyncScheduler

from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback

# =====================================================================
# 1. НАЛАШТУВАННЯ ТА ІНІЦІАЛІЗАЦІЯ
# =====================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
REVIEWS_CHAT_ID = int(os.getenv("REVIEWS_CHAT_ID", 0))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

FEEDBACK_HOUR = int(os.getenv("FEEDBACK_HOUR", 12))
FEEDBACK_MINUTE = int(os.getenv("FEEDBACK_MINUTE", 0))
ASSISTANT_HOUR = int(os.getenv("ASSISTANT_HOUR", 10))
ASSISTANT_MINUTE = int(os.getenv("ASSISTANT_MINUTE", 40))

# Сучасна ініціалізація клієнта Google AI
ai_client = genai.Client(api_key=GEMINI_API_KEY)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Ініціалізація планувальника
scheduler = AsyncScheduler()
pool = None

BOT_COMMANDS = ["start", "discount", "admin", "use_discount", "users"]

DIAL_COUNTRIES = {
    "region_europe": {
        "title": "🇪🇺 Європа",
        "items": {
            "болгарія": "Болгарія", 
            "греція": "Греція", 
            "чорногорія": "Чорногорія", 
            "хорватія": "Хорватія",
            "іспанія": "Іспанія", 
            "італія": "Італія",
            "кіпр": "Кіпр",
            "албанія": "Албанія",
            "португалія": "Португалія",
            "франція": "Франція"
        }
    },
    "region_east": {
        "title": "🕌 Близький Схід & Африка",
        "items": {
            "турція": "Туреччина", 
            "египет": "Єгипет", 
            "оае": "ОАЕ", 
            "туніс": "Туніс"
        }
    },
    "region_exotic": {
        "title": "🏝 Екзотика",
        "items": {
            "мальдіви": "Мальдіви", 
            "таїланд": "Таїланд", 
            "домінікана": "Домінікана", 
            "занзібар": "Занзібар", 
            "балі": "Балі (Індонезія)",
            "шрі ланка": "Шрі-Ланка"
        }
    }
}

# =====================================================================
# 2. MIDDLEWARES (АНТИ-СПАМ)
# =====================================================================

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, slow_mode_delay: float = 0.7):
        self.delay = slow_mode_delay
        self.user_caches = {}
        super().__init__()

    async def __call__(self, handler, event: types.TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user:
            user_id = user.id
            current_time = time.time()
            last_time = self.user_caches.get(user_id, 0)
            
            if current_time - last_time < self.delay:
                if isinstance(event, types.CallbackQuery):
                    await event.answer("⚠️ Не спамте кнопками!", show_alert=True)
                return
            self.user_caches[user_id] = current_time
        return await handler(handler, event, data)

dp.message.middleware(ThrottlingMiddleware())
dp.callback_query.middleware(ThrottlingMiddleware())

# =====================================================================
# 3. СТАНИ FSM
# =====================================================================

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

class FeedbackState(StatesGroup):
    waiting_for_text = State()

class AdminPanel(StatesGroup):
    waiting_for_client_info = State()
    waiting_for_date = State()

# =====================================================================
# 4. ФІЛЬТРИ ТА КЛАВІАТУРИ (ХЕЛПЕРИ)
# =====================================================================

class CommandFilter(BaseFilter):
    def __init__(self, commands: list):
        self.commands = commands
    async def __call__(self, message: types.Message) -> bool:
        if not message.text or not message.text.startswith("/"):
            return False
        cmd = message.text.split()[0][1:].split("@")[0]
        return cmd in self.commands

def add_back_button(builder: InlineKeyboardBuilder, back_callback: str):
    builder.row(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback))
    return builder.as_markup()

def get_dropdown_countries_kb(opened_region: str = None):
    builder = InlineKeyboardBuilder()
    
    for reg_id, reg_data in DIAL_COUNTRIES.items():
        if opened_region == reg_id:
            builder.row(types.InlineKeyboardButton(text=f"🔽 {reg_data['title']}", callback_data="toggle_close"))
            for item_cmd, item_name in reg_data["items"].items():
                builder.row(types.InlineKeyboardButton(text=f"   ▪️ {item_name}", callback_data=f"select_country_{item_cmd}"))
        else:
            builder.row(types.InlineKeyboardButton(text=f"▶️ {reg_data['title']}", callback_data=f"toggle_{reg_id}"))
            
    builder.row(types.InlineKeyboardButton(text="🌍 Інша країна (ввести вручну)", callback_data="select_country_other"))
    return builder.as_markup()

def stars_kb():
    builder = InlineKeyboardBuilder()
    builder.add(
        types.InlineKeyboardButton(text="3*", callback_data="star_3"),
        types.InlineKeyboardButton(text="4*", callback_data="star_4"),
        types.InlineKeyboardButton(text="5*", callback_data="star_5"),
        types.InlineKeyboardButton(text="Будь-яка", callback_data="star_any")
    )
    builder.adjust(3, 1)
    return builder.as_markup()

def meals_kb():
    builder = InlineKeyboardBuilder()
    builder.add(
        types.InlineKeyboardButton(text="🍳 BB (Сніданки)", callback_data="meal_BB"),
        types.InlineKeyboardButton(text="🍲 HB (Сніданок+Вечеря)", callback_data="meal_HB"),
        types.InlineKeyboardButton(text="🍹 AI (Все включено)", callback_data="meal_AI"),
        types.InlineKeyboardButton(text="👑 UAI (Ультра Все Включено)", callback_data="meal_UAI"),
        types.InlineKeyboardButton(text="❌ RO (Без харчування)", callback_data="meal_RO"),
        types.InlineKeyboardButton(text="🤷‍♂️ Будь-яке", callback_data="meal_any")
    )
    builder.adjust(1)
    return builder.as_markup()

async def save_msg(message: types.Message, state: FSMContext):
    data = await state.get_data()
    msgs = data.get("msgs_to_delete", [])
    msgs.append(message.message_id)
    await state.update_data(msgs_to_delete=msgs)

# =====================================================================
# 5. РОБОТА З БАЗОЮ ДАНИХ
# =====================================================================

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                full_name VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS feedbacks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                return_date VARCHAR(50),
                sent INT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discounts (
                user_id BIGINT PRIMARY KEY,
                discount_value INT DEFAULT 5,
                is_used BOOLEAN DEFAULT FALSE
            );
        """)

async def track_user(user: types.User):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, username, full_name) 
               VALUES ($1, $2, $3) ON CONFLICT (user_id) 
               DO UPDATE SET username = $2, full_name = $3""",
            user.id, user.username, user.full_name
        )

# =====================================================================
# 6. ПАРСЕР ТУРІВ ТА ПОВНИЙ ОРИГІНАЛЬНИЙ ПРОМТ GEMINI AI
# =====================================================================

async def fetch_hot_tours():
    url = "https://www.otpusk.com/hot/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                return []
        soup = BeautifulSoup(response.text, 'html.parser')
        tours = []
        items = soup.find_all('div', class_='story') or soup.find_all('div', class_='hot-b-item')
        for item in items[:4]:
            title = item.find('h3').text.strip() if item.find('h3') else "Гарячий тур"
            price = item.find('span', class_='price').text.strip() if item.find('span', class_='price') else ""
            desc = item.find('div', class_='description').text.strip() if item.find('div', class_='description') else ""
            tours.append(f"Курорт: {title}. Деталі: {desc}. Ціна: {price}")
        return tours
    except Exception as e:
        logging.error(f"Парсинг гарячих турів не вдався: {e}")
        return []

async def generate_and_send_ai_tour_post():
    raw_tours = await fetch_hot_tours()
    country_data = "\n".join(raw_tours) if raw_tours else "Наразі доступні загальні акційні пропозиції по Туреччині, Єгипту та Греції."
    
    cat = {
        "name": "Гарячі тури",
        "prompt_part": "Зроби вибірку найсмачніших гарячих турів, які підійдуть для сімейного або парного відпочинку, з акцентом на вигідну ціну."
    }
    
    prompt = (
        f"Ти — професійний travel-копірайтер компанії. На основі НАДАНИХ ТЕКСТОВИХ ДАНИХ склади один цікавий, "
        f"структурований і залучаючий пост для Telegram-каналу українською мовою.\n\n"
        f"ТВОЄ ГОЛОВНЕ ЗАВДАННЯ: {cat['prompt_part']}\n\n"
        
        f"🚫  ПРАВИЛО ЛОГІКИ ТРАНСПОРТУ ТА ЕМОДЗІ (ЗАБОРОНА ПЛУТАНИНИ):\n"
        f"1. Автобусні тури маркуються ЛИШЕ символом 🚌 в усьому рядку. Наприклад: '🚌 Автобус із Києва' або '🚌 Автобус зі Львова'. Заборонено в цей рядок ставити літак ✈️!\n"
        f"2. Авіатури маркуються ЛИШЕ символом ✈️ в усьому рядку. Наприклад: '✈️ Авіа з Кишинева' або '✈️ Авіа з Варшави'. Заборонено в цей рядок ставити автобус 🚌!\n"
        f"3. Ніколи не зліплюй емодзі автобуса з текстом про авіарейси. Кожен тип транспорту має відповідати своему єдиному значку.\n"
        f"4. ЗАБОРОНА ФЕЙКОВИХ ВИЛЬОТІВ З УКРАЇНИ: Наразі авіатури з міст України не здійснюються! Якщо тур авіаційний — місто вильоту обов'язково має бути іноземним. З українських міст можливий ТІЛЬКИ автобусний виїзд чи власний транспорт. Не вигадуй вильоти літаків з України!\n\n"

        f"❌ КАТЕГОРИЧНА ЗАБОРОНА НА ФРАЗИ 'ЗА ЗАПИТОМ', 'УТОЧНЮЙТЕ' ТА АБСТРАКЦІЇ:\n"
        f"- Тобі законодавчо заборонено використовувати у фінальному тексті фрази: 'з міст Європи', 'уточнюйте', 'за запитом', 'уточнюйте у менеджера', 'деталі за телефоном', 'кількість ночей за запитом'. Пост повинен містити виключно готову, фіксовану та конкретну інформацію для туриста.\n"
        f"- У полі трансферу та виїзду ОБОВ'ЯЗКОВО має бути чітко вказано конкретне місто вильоту або виїзду.\n"
        f"- Якщо для якогось готелю в наданому тексті замість конкретної дати, кількості ночей чи типу харчування вказано 'уточнюйте' або 'за запитом' — повністю ІГНОРУЙ такий готель.\n\n"
        
        f"⚠️ КРИТИЧНО ВАЖЛИВЕ ПРАВИЛО ПРІОРИТЕТУ ЗІРКОВОСТІ ГОТЕЛІВ:\n"
        f"Ти повинен робити вибірку готелів (до 5 штук) суворо за такому каскадним пріоритетом:\n"
        f"1. Вибрати до 5 найкращих готелів 5★. Сформуй добірку виключно або переважно з них.\n"
        f"2. Тільки якщо готелів 5★ у списку виявиться менше 5 штук, добирай решту з готелів зірковості 4★ (4*).\n"
        f"3. Якщо в тексті немає взагалі ні 5★, ні 4★ готелів для цієї країни, тільки в цьому крайньому разі дозволено брати та показувати готелі зірковості 3★ (3*).\n\n"
        
        f"⚠️ ХУДОЖНІЙ ПЕРЕХІД:\n"
        f"У вступному абзаці обов'язково адаптуй фразу-перехід під фактично обрану зірковість готелів.\n\n"
        
        f"⚠️ ГОЛОВНИЙ КРИТЕРІЙ ВІДБОРУ — ПРІОРІТЕТ ТРАНСПОРТУ ТА ПОВНОГО ПАКЕТУ:\n"
        f"Проаналізуй тип транспорту для готелів та відбере варіанти за суворим каскадним пріоритетом:\n"
        f"1. ПРІОРІТЕТ №1 — АВІАТУРИ: Шукай у тексті готелі, де вказано авіапереліт.\n"
        f"    🔥 КРИТИЧНА ВИМОГА ДЛЯ АВІА: Серед усіх знайдених авіатурів ти зобов'язаний ПЕРШОЧЕРГОВО вибирати варіанти, які є ПОВНИМ ПАКЕТОМ (куди одночасно включено: Проїзд, Страховка та Трасфер до готелю).\n"
        f"2. АВТОБУСНІ ТУРИ: Включай автобусні тури у пост ТІЛЬКИ у випадку, якщо в наданих даних взагалі немає авіатурів по цій країні.\n"
        f"3. БЕЗ ТРАНСФЕРУ: Варіанти 'Власний транспорт / Без трансферу' дозволено брати лише в крайньому разі.\n\n"
        
        f"⚠️ КРИТИЧНО ВАЖЛИВЕ ПРАВИЛО ДЛЯ НАЙНИЖЧОЇ ЦІНИ ТА СИНХРОНІЗАЦІЇ:\n"
        f"- НІКОЛИ не зліплюй ціну від дешевого автобусного туру з описом авіатуру! Ціна, тип харчування, трансфер та кількість ночей у шаблоні мають бути СУВОРО СИНХРОНІЗОВАНІ між собою.\n\n"
        
        f"⚠️ СУВОРЕ ПРАВИЛО ДЛЯ АНАЛІЗУ ТА ФОРМАТУ ДАТ (ТОЧНИЙ ПОВТОР З САЙТУ):\n"
        f"1. Знайди в тексті конкретного туру дату вильоту/виїзду, яка прописана поруч із обраною ціною.\n"
        f"2. КРИТИЧНО ВАЖЛИВО: Переноси дату в шаблон СУВОРО в тому форматі, в якому вона написана в джерелі (на сайті). Нічого від себе не змінюй.\n\n"
        
        f"⚠️ Суворо дотримуйся наступних правил конструювання текста:\n"
        f"1. НІКОЛИ не згадуй назву сторонніх сайтів чи парсерів.\n"     
        f"2. Текст ОБОВ'ЯЗКОВО має починатися одразу з ХУДОЖНЬОГО ВСТУПУ.\n"
        f"3. Заборони будь-які технічні чи робочі фрази типу 'Згідно з наявними даними...'.\n"
        f"4. СУВОРЕ ПРАВИЛО ДЛЯ РОЗДІЛЕННЯ ТУРИВ: Відокремлюй картки готелів одну від одної СУВОРО одним порожнім рядком. Категорично ЗАБОРОНЕНО малювати штучні лінії ('---')!\n"
        f"5. Після художнього вступу виведи список готелів. Для КОЖНОГО готелю суворо використовуй наступний візуальний шаблон:\n\n"
        
        f"📍 <b>{cat['name'].upper()} ([Вкажи регіон/курорт])</b>\n"
        f"🏨 <b>[Назва готелю латиницею в оригіналі] [Зірковість, наприклад: 4* або 5*]</b>\n"
        f"🚌 <b>Трансфер та виїзд:</b> [Залежно від туру, вкажи чітке місто вильоту чи виїзду!]\n"
        f"🍽 <b>Харчування:</b> [Вкажи тип харчування, що відповідає обраній ціні]\n"
        f"📅 <b>Виліт/Дата:</b> [Вкажи точную дату туру], [Вкажи кількість ночей]\n"
        f"💰 <b>Ціна:</b> [Вкажи вартість] грн. за 2-х дорослих\n"
        f"<i>[Тут напиши короткий художній опис саме цього готелю.]</i>\n\n"
        
        f"⚠️ ОБМЕЖЕННЯ: Описуй каждый готель ємно. Твій підсумковий текст має бути не більше за 3000 символів. Використовуй тільки HTML-теги <b> та <i>.\n\n"
        f"Ось текстові дані з готелями: {country_data}"
    )
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        post_text = response.text
        
        builder = InlineKeyboardBuilder()
        builder.add(types.InlineKeyboardButton(text="🤖 Підібрати свій тур", url=f"https://t.me/{(await bot.get_me()).username}?start=channel_post"))
        
        await bot.send_message(chat_id=CHANNEL_ID, text=post_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        logging.info("AI-пост успішно згенеровано.")
    except Exception as e:
        logging.error(f"Помилка генерації AI поста: {e}")

# =====================================================================
# 7. АДМІНІСТРАТИВНА ПАНЕЛЬ
# =====================================================================

async def show_admin_base(message: types.Message, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.add(
        types.InlineKeyboardButton(text="📅 Перепризначити відгук", callback_data="adm_feedback"),
        types.InlineKeyboardButton(text="👥 Список користувачів", callback_data="adm_users")
    )
    builder.adjust(1)
    msg = await message.answer("🛠 <b>ПАНЕЛЬ МЕНЕДЖЕРА АГЕНЦІЇ</b>", reply_markup=builder.as_markup(), parse_mode="HTML")
    
    data = await state.get_data()
    msgs = data.get("admin_msgs_to_clean", [])
    msgs.append(msg.message_id)
    await state.update_data(admin_msgs_to_clean=msgs)

async def clean_admin_messages(state: FSMContext, chat_id: int):
    data = await state.get_data()
    msgs = data.get("admin_msgs_to_clean", [])
    for m_id in msgs:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=m_id)
        except:
            pass
    await state.update_data(admin_msgs_to_clean=[])

@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await show_admin_base(message, state)

@dp.callback_query(F.data == "adm_feedback", F.from_user.id == ADMIN_ID)
async def adm_feedback_start(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup(reply_markup=None)
    msg = await callback_query.message.answer("📝 Введіть Telegram ID клієнта або його @username для налаштування опитування:")
    
    data = await state.get_data()
    msgs = data.get("admin_msgs_to_clean", [])
    msgs.append(msg.message_id)
    await state.update_data(admin_msgs_to_clean=msgs)
    await state.set_state(AdminPanel.waiting_for_client_info)

@dp.message(Command("use_discount"), F.from_user.id == ADMIN_ID)
async def use_discount_cmd(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Формат команди: <code>/use_discount [ID_Користувача]</code>", parse_mode="HTML")
        return
    u_id = int(args[1])
    async with pool.acquire() as conn:
        await conn.execute("UPDATE discounts SET is_used = TRUE WHERE user_id = $1", u_id)
    await message.answer(f"✅ Усі активні знижки для користувача <code>{u_id}</code> успішно анульовані.", parse_mode="HTML")

@dp.message(Command("users"), F.from_user.id == ADMIN_ID)
@dp.callback_query(F.data == "adm_users", F.from_user.id == ADMIN_ID)
async def show_users_list(event: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_reply_markup(reply_markup=None)
        msg_ctx = event.message
    else:
        msg_ctx = event

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, full_name FROM users ORDER BY created_at DESC LIMIT 30")
    
    if not rows:
        await msg_ctx.answer("База даних користувачів наразі порожня.")
        return
        
    text = "👥 <b>ОСТАННІ ТУРИСТИ В БАЗІ БОТА:</b>\n━━━━━━━━━━━━━━━\n"
    for r in rows:
        user_link = f"@{r['username']}" if r['username'] else "немає"
        text += f"• <code>{r['user_id']}</code> — {r['full_name']} ({user_link})\n"
    text += "━━━━━━━━━━━━━━━"
    
    msg = await msg_ctx.answer(text, parse_mode="HTML")
    if isinstance(event, types.CallbackQuery):
        data = await state.get_data()
        msgs = data.get("admin_msgs_to_clean", [])
        msgs.append(msg.message_id)
        await state.update_data(admin_msgs_to_clean=msgs)
        await show_admin_base(msg_ctx, state)

# =====================================================================
# 8. КОМАНДИ СТАРТУ ТА ЗНИЖОК ДЛЯ ТУРИСТІВ
# =====================================================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await track_user(message.from_user)
        
    welcome_text = (
        f"👋 <b>Вітаємо, {message.from_user.first_name} у trevel-боті нашої агенції!</b>\n\n"
        f"🌴 Ми підберемо для Вас найкращі пропозиції відпочинку за лічені хвилини.\n\n"
        f"Натисніть кнопку нижче, щоб розпочати інтерактивний пошук туру своєї мрії 👇"
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="🚀 ПОЧАТИ ПІДБІР ТУРУ", callback_data="start_selection"))
    
    msg = await message.answer(welcome_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await state.set_state(TourRequest.start_confirmed)
    await state.update_data(msgs_to_delete=[msg.message_id])

@dp.message(Command("discount"))
async def discount_cmd(message: types.Message):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", message.from_user.id)
    if row:
        await message.answer(f"🎁 Ваша активна знижка лояльності становить: <b>{row['discount_value']}%</b>.", parse_mode="HTML")
    else:
        await message.answer("ℹ️ Наразі у вас немає активних індивідуальних знижок лояльності.")

# =====================================================================
# 9. ДІАЛОГ ПІДБОРУ ТУРУ (КРОКИ ЗБОРУ ІНФОРМАЦІЇ)
# =====================================================================

@dp.message(TourRequest.start_confirmed, ~CommandFilter(commands=BOT_COMMANDS))
async def check_start_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, натисніть на кнопку «🚀 ПОЧАТИ ПІДБІР ТУРУ»")
    await save_msg(msg, state)

@dp.callback_query(F.data == "start_selection")
async def process_start_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear() 
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    msg = await callback_query.message.answer(
        "🌍 <b>Оберіть напрямок для відпочинку.</b>\n"
        "Натисніть на регіон, щоб розкрити список країн, або оберіть ручне введення:", 
        reply_markup=get_dropdown_countries_kb(),
        parse_mode="HTML"
    )
    await save_msg(msg, state)
    await state.set_state(TourRequest.destination)

@dp.callback_query(F.data.startswith("toggle_"), TourRequest.destination)
async def toggle_region(callback_query: types.CallbackQuery):
    region_id = callback_query.data.split("_")[1]
    await callback_query.message.edit_reply_markup(reply_markup=get_dropdown_countries_kb(opened_region=f"region_{region_id}"))
    await callback_query.answer()

@dp.callback_query(F.data == "toggle_close", TourRequest.destination)
async def toggle_close_region(callback_query: types.CallbackQuery):
    await callback_query.message.edit_reply_markup(reply_markup=get_dropdown_countries_kb())
    await callback_query.answer()

@dp.callback_query(F.data.startswith("select_country_"), TourRequest.destination)
async def process_dropdown_selection(callback_query: types.CallbackQuery, state: FSMContext):
    choice = callback_query.data.replace("select_country_", "")
    
    if choice == "other":
        await callback_query.message.edit_reply_markup(reply_markup=None)
        msg = await callback_query.message.answer("✍️ Будь ласка, напишіть назву країни вручну у повідомленні:")
        await save_msg(msg, state)
        await callback_query.answer()
        return

    final_destination = "Невідома країна"
    for reg in DIAL_COUNTRIES.values():
        if choice in reg["items"]:
            final_destination = reg["items"][choice]
            break

    await callback_query.message.edit_reply_markup(reply_markup=None)
    await state.update_data(destination=final_destination)
    
    builder = InlineKeyboardBuilder()
    builder.add(
        types.InlineKeyboardButton(text="1", callback_data="adults_1"),
        types.InlineKeyboardButton(text="2", callback_data="adults_2"),
        types.InlineKeyboardButton(text="3+", callback_data="adults_3+")
    )
    
    msg1 = await callback_query.message.answer(f"✅ Напрямок: {final_destination}")
    msg2 = await callback_query.message.answer(f"👤 Оберіть кількість дорослих:", reply_markup=add_back_button(builder, "back_to_dest"))
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.adults_count)
    await callback_query.answer()

@dp.message(TourRequest.destination, ~CommandFilter(commands=BOT_COMMANDS))
async def process_dest_text(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    text = message.text.strip()
    
    if text.isdigit() or len(text) < 2:
        msg = await message.answer("⚠️ Введіть назву країни літерами.")
        await save_msg(msg, state)
        return

    final_destination = text.capitalize()
    await state.update_data(destination=final_destination)
    
    builder = InlineKeyboardBuilder()
    builder.add(
        types.InlineKeyboardButton(text="1", callback_data="adults_1"),
        types.InlineKeyboardButton(text="2", callback_data="adults_2"),
        types.InlineKeyboardButton(text="3+", callback_data="adults_3+")
    )
    
    msg1 = await message.answer(f"✅ Напрямок: {final_destination}")
    msg2 = await message.answer(f"👤 Оберіть кількість дорослих:", reply_markup=add_back_button(builder, "back_to_dest"))
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.adults_count)

@dp.callback_query(F.data == "back_to_dest", TourRequest.adults_count)
async def back_to_dest(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    msg = await callback_query.message.answer(
        "🌍 <b>Оберіть напрямок для відпочинку.</b>\n", 
        reply_markup=get_dropdown_countries_kb(), parse_mode="HTML"
    )
    await save_msg(msg, state)
    await state.set_state(TourRequest.destination)
    await callback_query.answer()

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
    builder.add(
        types.InlineKeyboardButton(text="1", callback_data="child_1"),
        types.InlineKeyboardButton(text="2", callback_data="child_2"),
        types.InlineKeyboardButton(text="3+", callback_data="child_3")
    )
    builder.adjust(1, 3)
    
    msg1 = await callback_query.message.answer(f"👤 Дорослих: {count}")
    msg2 = await callback_query.message.answer(f"👶 Скільки будет дітей?", reply_markup=add_back_button(builder, "back_to_adults"))
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.children_count)

@dp.callback_query(F.data == "back_to_adults", TourRequest.children_count)
async def back_to_adults(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    data = await state.get_data()
    builder = InlineKeyboardBuilder()
    builder.add(
        types.InlineKeyboardButton(text="1", callback_data="adults_1"),
        types.InlineKeyboardButton(text="2", callback_data="adults_2"),
        types.InlineKeyboardButton(text="3+", callback_data="adults_3+")
    )
    msg = await callback_query.message.answer(
        f"👤 Оберіть кількість дорослих:", reply_markup=add_back_button(builder, "back_to_dest")
    )
    await save_msg(msg, state)
    await state.set_state(TourRequest.adults_count)

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
    
    calendar_markup = await SimpleCalendar().start_calendar()
    calendar_markup.inline_keyboard.append([
        types.InlineKeyboardButton(text="⬅️ Назад до вибору дітей", callback_data="back_to_children")
    ])

    msg2 = await callback_query.message.answer(f"📅 Оберіть дату, з якої можна планувати виліт (З):", reply_markup=calendar_markup)
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.date_from) 

@dp.callback_query(F.data == "back_to_children", TourRequest.date_from)
async def back_to_children(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="Без дітей (0)", callback_data="child_0"))
    builder.add(
        types.InlineKeyboardButton(text="1", callback_data="child_1"),
        types.InlineKeyboardButton(text="2", callback_data="child_2"),
        types.InlineKeyboardButton(text="3+", callback_data="child_3")
    )
    builder.adjust(1, 3)
    msg = await callback_query.message.answer(f"👶 Скільки буде дітей?", reply_markup=add_back_button(builder, "back_to_adults"))
    await save_msg(msg, state)
    await state.set_state(TourRequest.children_count)

@dp.message(TourRequest.date_from, ~CommandFilter(commands=BOT_COMMANDS))
async def check_date_from_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть дату на календарі вище.")
    await save_msg(msg, state)

@dp.callback_query(SimpleCalendarCallback.filter(), TourRequest.date_from)
async def process_date_from(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        if date.date() < datetime.now().date():
            await callback_query.answer("⚠️ Не можна обрати дату в минулому!", show_alert=True)
            return

        formatted = date.strftime("%d.%m.%Y")
        await state.update_data(date_from=formatted)
        
        msg1 = await callback_query.message.answer(f"📅 Дата вильоту (З): {formatted}")
        
        calendar_markup = await SimpleCalendar().start_calendar()
        calendar_markup.inline_keyboard.append([
            types.InlineKeyboardButton(text="⬅️ Назад до дати (З)", callback_data="back_to_date_from")
        ])

        msg2 = await callback_query.message.answer(f"📅 Оберіть дату, до якої можна планувати виліт (ПО):", reply_markup=calendar_markup)
        await save_msg(msg1, state)
        await save_msg(msg2, state)
        await state.set_state(TourRequest.date_to)

@dp.callback_query(F.data == "back_to_date_from", TourRequest.date_to)
async def back_to_date_from(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    calendar_markup = await SimpleCalendar().start_calendar()
    calendar_markup.inline_keyboard.append([
        types.InlineKeyboardButton(text="⬅️ Назад до вибору дітей", callback_data="back_to_children")
    ])
    msg = await callback_query.message.answer(f"📅 Оберіть дату, з якої можна планувати виліт (З):", reply_markup=calendar_markup)
    await save_msg(msg, state)
    await state.set_state(TourRequest.date_from)

@dp.message(TourRequest.date_to, ~CommandFilter(commands=BOT_COMMANDS))
async def check_date_to_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть дату на календарі вище.")
    await save_msg(msg, state)

@dp.callback_query(SimpleCalendarCallback.filter(), TourRequest.date_to)
async def process_date_to(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        data = await state.get_data()
        date_from = datetime.strptime(data.get('date_from'), "%d.%m.%Y")
        
        if date < date_from:
            await callback_query.answer("⚠️ Дата 'ПО' не може бути ранішою за дату 'З'!", show_alert=True)
            return

        formatted = date.strftime("%d.%m.%Y")
        await state.update_data(date_to=formatted)
        
        builder = InlineKeyboardBuilder()
        msg1 = await callback_query.message.answer(f"✅ Дата вильоту (ПО): {formatted}")
        msg2 = await callback_query.message.answer(f"🌙 На скільки ночей плануєте відпочинок?", reply_markup=add_back_button(builder, "back_to_date_to"))
        await save_msg(msg1, state)
        await save_msg(msg2, state)
        await state.set_state(TourRequest.nights_count)

@dp.callback_query(F.data == "back_to_date_to", TourRequest.nights_count)
async def back_to_date_to(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    calendar_markup = await SimpleCalendar().start_calendar()
    calendar_markup.inline_keyboard.append([
        types.InlineKeyboardButton(text="⬅️ Назад до дати (З)", callback_data="back_to_date_from")
    ])
    msg = await callback_query.message.answer(f"📅 Оберіть дату, до якої можна планувати виліт (ПО):", reply_markup=calendar_markup)
    await save_msg(msg, state)
    await state.set_state(TourRequest.nights_count)

@dp.message(TourRequest.nights_count, ~CommandFilter(commands=BOT_COMMANDS))
async def process_nights(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    nights_input = message.text.strip()
    
    if not nights_input.isdigit():
        msg = await message.answer("⚠️ Введіть кількість ночей цифрами:")
        await save_msg(msg, state)
        return

    await state.update_data(nights=nights_input)
    builder = InlineKeyboardBuilder()
    builder.attach(InlineKeyboardBuilder.from_markup(stars_kb()))

    msg = await message.answer("⭐ Оберіть категорію готелю", reply_markup=add_back_button(builder, "back_to_nights"))
    await save_msg(msg, state)
    await state.set_state(TourRequest.hotel_stars)

@dp.callback_query(F.data == "back_to_nights", TourRequest.hotel_stars)
async def back_to_nights(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    msg = await callback_query.message.answer(
        "🌙 На скільки ночей плануєте відпочинок?", reply_markup=add_back_button(InlineKeyboardBuilder(), "back_to_date_to")
    )
    await save_msg(msg, state)
    await state.set_state(TourRequest.nights_count)

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
    
    builder = InlineKeyboardBuilder()
    builder.attach(InlineKeyboardBuilder.from_markup(meals_kb()))

    msg1 = await callback_query.message.answer(f"⭐ Готель: {label}")
    msg2 = await callback_query.message.answer(f"🍴 Яке харчування Вам підходить:", reply_markup=add_back_button(builder, "back_to_stars"))
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.meal_type)

@dp.callback_query(F.data == "back_to_stars", TourRequest.meal_type)
async def back_to_stars(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    builder = InlineKeyboardBuilder()
    builder.attach(InlineKeyboardBuilder.from_markup(stars_kb()))
    msg = await callback_query.message.answer("⭐ Оберіть категорію готелю", reply_markup=add_back_button(builder, "back_to_nights"))
    await save_msg(msg, state)
    await state.set_state(TourRequest.hotel_stars)

@dp.message(TourRequest.meal_type, ~CommandFilter(commands=BOT_COMMANDS))
async def check_meals_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть тип харчування кнопкою.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("meal_"), TourRequest.meal_type)
async def process_meals(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup(reply_markup=None)
    meal_map = {"BB": "Сніданки", "HB": "Сніданок+вечеря", "AI": "Все включено", "UAI": "Ультра Все Включено", "RO": "Без харчування"}
    meal_text = meal_map.get(callback_query.data.split("_")[1], "Будь-яке")
    await state.update_data(meals=meal_text)
    
    builder = InlineKeyboardBuilder()
    msg1 = await callback_query.message.answer(f"🍴 Харчування: {meal_text}")
    msg2 = await callback_query.message.answer(f"💰 Який Ви плануєте бюджет у гривнях (цифрами):", reply_markup=add_back_button(builder, "back_to_meals"))
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.budget)

@dp.callback_query(F.data == "back_to_meals", TourRequest.budget)
async def back_to_meals(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    builder = InlineKeyboardBuilder()
    builder.attach(InlineKeyboardBuilder.from_markup(meals_kb()))
    msg = await callback_query.message.answer(f"🍴 Яке харчування Вам підходить:", reply_markup=add_back_button(builder, "back_to_stars"))
    await save_msg(msg, state)
    await state.set_state(TourRequest.meal_type)

@dp.message(TourRequest.budget, ~CommandFilter(commands=BOT_COMMANDS))
async def process_budget(message: types.Message, state: FSMContext):
    budget_raw = message.text.lower().replace(" ", "").replace("грн", "").replace("$", "").replace("usd", "").replace("eur", "")
    
    if not budget_raw.isdigit():
        msg = await message.answer("⚠️ Будь ласка, введіть бюджет лише цифрами:")
        await save_msg(msg, state)
        return

    await save_msg(message, state)
    await state.update_data(budget=budget_raw)
    
    contact_keyboard = types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text="📱 ПОДІЛИТИСЯ НОМЕРОМ", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    
    inline_builder = InlineKeyboardBuilder()
    msg_back_info = await message.answer("ℹ️ Якщо хочете змінити бюджет, натисніть кнопку нижче:", reply_markup=add_back_button(inline_builder, "back_to_budget"))
    
    msg = await message.answer("📞 Поділіться контактом або напишіть свій нікнейм/телефон вручну:", reply_markup=contact_keyboard)
    await save_msg(msg_back_info, state)
    await save_msg(msg, state)
    await state.set_state(TourRequest.contact)

@dp.callback_query(F.data == "back_to_budget", TourRequest.contact)
async def back_to_budget(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.delete()
    inline_builder = InlineKeyboardBuilder()
    msg = await callback_query.message.answer(f"💰 Який Ви плануєте бюджет у гривнях (цифрами):", reply_markup=add_back_button(inline_builder, "back_to_meals"))
    await save_msg(msg, state)
    await state.set_state(TourRequest.budget)

# --- КРОК 10: ОБРОБКА ТА ВІДПРАВКА ЗАЯВКИ (ВИПРАВЛЕНО ДУБЛЮВАННЯ reply_markup) ---
@dp.message(TourRequest.contact, ~CommandFilter(commands=BOT_COMMANDS))
async def process_contact(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    contact_info = message.contact.phone_number if message.contact else message.text

    data = await state.get_data()
    user = message.from_user
    
    async with pool.acquire() as conn:
        discount_row = await conn.fetchrow("SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", user.id)
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
        f"📱 <b>Контакт:</b> {contact_info}"
    )
    
    report = (
        f"🔥 <b>НОВА ЗАЯВКА НА ТУР!</b>\n━━━━━━━━━━━━━━━\n{info_table}\n━━━━━━━━━━━━━━━\n"
        f"👤 <b>Клієнт:</b> <a href='tg://user?id={user.id}'>{user.full_name}</a>\n"
        f"🆔 <b>ID для відгуку:</b> <code>{user.id}</code>\n━━━━━━━━━━━━━━━"
    )
    
    await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
    
    msgs_to_delete = data.get("msgs_to_delete", [])
    tasks = [bot.delete_message(chat_id=message.chat.id, message_id=m_id) for m_id in msgs_to_delete]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        
    re_builder = ReplyKeyboardBuilder()
    re_builder.add(types.KeyboardButton(text="🔄 СТВОРИТИ НОВУ ЗАЯВКУ"))
    
    # ВИПРАВЛЕНО: ТУТ БУЛА ПОМИЛКА ДУБЛЮВАННЯ reply_markup. Тепер все передається один раз.
    await message.answer(
        f"✅ Дякуємо! Заявку успішно відправлено!\n\n<b>ДЕТАЛІ ВАШОЇ ЗАЯВКИ:</b>\n"
        f"━━━━━━━━━━━━━━━\n{info_table}\n━━━━━━━━━━━━━━━", 
        parse_mode="HTML",
        reply_markup=re_builder.as_markup(resize_keyboard=True)
    )
    await state.clear()

# =====================================================================
# 11. АВТОМАТИЗОВАНІ ВІДГУКИ ТА ОПИТУВАННЯ
# =====================================================================

async def check_returns():
    today_str = datetime.now().strftime("%d.%m.%Y")
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, user_id FROM feedbacks WHERE return_date = $1 AND sent = 0", today_str)
        
    for row in rows:
        f_id, u_id = row['id'], row['user_id']
        builder = InlineKeyboardBuilder()
        for i in range(1, 6):
            builder.add(types.InlineKeyboardButton(text=f"{i}⭐", callback_data=f"rate_{i}"))
        
        try:
            await bot.send_message(
                chat_id=u_id,
                text="👋 З поверненням додому! Будь ласка, оцініть якість нашого сервісу:",
                reply_markup=builder.as_markup()
            )
            async with pool.acquire() as conn:
                await conn.execute("UPDATE feedbacks SET sent = 1 WHERE id = $1", f_id)
                await conn.execute("INSERT INTO discounts (user_id, discount_value) VALUES ($1, 5) ON CONFLICT (user_id) DO UPDATE SET discount_value = 5, is_used = FALSE", u_id)
        except Exception as e:
            logging.error(f"Не вдалося відправити запит на відгук: {e}")

@dp.callback_query(F.data.startswith("rate_"))
async def process_rating(callback_query: types.CallbackQuery, state: FSMContext):
    rating = int(callback_query.data.split("_")[1])
    await state.update_data(user_rating=rating)
    await callback_query.message.edit_text(
        f"Ви поставили {rating}⭐!\nБудь ласка, напишіть декілька слів про Вашу подорож:"
    )
    await state.set_state(FeedbackState.waiting_for_text)

async def send_delayed_feedback(forwarded_msg_id: int, rating: int):
    reply_text = "😍 Дякуємо, що обираєте нас! ❤️" if rating >= 4 else "🙏 Дякуємо, ми врахуємо ваші зауваження."
    try:
        await bot.send_message(chat_id=REVIEWS_CHAT_ID, text=reply_text, reply_to_message_id=forwarded_msg_id)
    except Exception as e:
        logging.error(f"Помилка відповіді на відгук: {e}")

@dp.message(FeedbackState.waiting_for_text, ~CommandFilter(commands=BOT_COMMANDS))
async def process_feedback_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("user_rating")
    user = message.from_user
    feedback_header = (
        f"🌟 <b>НОВИЙ ВІДГУК!</b>\n━━━━━━━━━━━━━━━\n"
        f"👤 <b>Від:</b> {user.full_name}\n⭐ <b>Оцінка:</b> {rating}⭐\n━━━━━━━━━━━━━━━"
    )
    await bot.send_message(REVIEWS_CHAT_ID, feedback_header, parse_mode="HTML")
    forwarded_msg = await message.forward(chat_id=REVIEWS_CHAT_ID)
    await message.answer("❤️ Дякуємо за Ваш відгук! Отримано 5% знижки на наступний тур!")
    await state.clear()
    
    wait_seconds = random.randint(60, 600)
    
    await scheduler.add_schedule(
        send_delayed_feedback,
        trigger='date',
        start_time=datetime.now() + timedelta(seconds=wait_seconds),
        args=[forwarded_msg.message_id, rating]
    )

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
                username = "ID введено вручну"
        else:
            row = await conn.fetchrow("SELECT user_id, username FROM users WHERE LOWER(username) = $1", input_data)
            if row:
                target_id = row['user_id']
                username = f"@{row['username']}"

    if target_id:
        await state.update_data(client_id=target_id, client_username=username)
        msg = await message.answer(
            f"✅ Клієнта знайдено:\nID: <code>{target_id}</code>\n\nТепер оберіть дату повернення:", 
            reply_markup=await SimpleCalendar().start_calendar(), parse_mode="HTML"
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
            await conn.execute("DELETE FROM feedbacks WHERE user_id = $1 AND sent = 0", client_id)
            await conn.execute("INSERT INTO feedbacks (user_id, return_date, sent) VALUES ($1, $2, 0)", client_id, formatted)

        await clean_admin_messages(state, callback_query.message.chat.id)
        
        report_msg = await callback_query.message.answer(
            f"✅ <b>Запит на відгук перепризначено!</b>\n📅 <b>Нова дата:</b> {formatted}", parse_mode="HTML"
        )
        
        await state.update_data(admin_msgs_to_clean=[report_msg.message_id])
        await show_admin_base(callback_query.message, state)
        await state.set_state(None)

# =====================================================================
# 12. ТОЧКА ВХОДУ ТА ЖИТТЄВИЙ ЦИКЛ СЕРВЕРА (WEBHOOK)
# =====================================================================

async def on_shutdown(app: web.Application):
    global pool
    if pool:
        await pool.close()
    await scheduler.stop()

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super_secret_key")
    
    if not WEBHOOK_URL:
        logging.error("🛑 КРИТИЧНА ПОМИЛКА: WEBHOOK_URL порожня!")
        return

    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", secret_token=WEBHOOK_SECRET)
    
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get("/", lambda request: web.Response(text="Travel bot runs smoothly."))

    user_commands = [
        types.BotCommand(command="start", description="🚀 Почати підбір туру"), 
        types.BotCommand(command="discount", description="🎁 Моя знижка")
    ]
    admin_commands = user_commands + [
        types.BotCommand(command="admin", description="🛠 Запит на відгук"),
        types.BotCommand(command="use_discount", description="✅ Використати знижку"),
        types.BotCommand(command="users", description="👥 Список туристів")
    ]
   
    await bot.set_my_commands(user_commands, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(admin_commands, scope=types.BotCommandScopeChat(chat_id=ADMIN_ID))
 
    # Реєстрація регулярних задач
    await scheduler.add_schedule(check_returns, trigger='cron', hour=FEEDBACK_HOUR, minute=FEEDBACK_MINUTE)
    await scheduler.add_schedule(generate_and_send_ai_tour_post, trigger='cron', hour=ASSISTANT_HOUR, minute=ASSISTANT_MINUTE)
    
    await scheduler.start()
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот зупинений.")
