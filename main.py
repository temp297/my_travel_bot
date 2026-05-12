import os
import logging
import asyncio
import random
from datetime import datetime
import pytz
from aiohttp import web
import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiogram
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, CommandObject, StateFilter, Command as CommandFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback

# Список команд для фільтрації
BOT_COMMANDS = ["start", "cancel", "admin", "discount", "check_discounts", "use_discount", "users"]

# НАЛАШТУВАННЯ
API_TOKEN = os.getenv("API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "7185133060"))
    REVIEWS_CHAT_ID = int(os.getenv("REVIEWS_CHAT_ID", "-1003818943967"))
    FEEDBACK_HOUR = int(os.getenv("FEEDBACK_HOUR", "20"))
    # Додаємо хвилини (за замовчуванням 0)
    FEEDBACK_MINUTE = int(os.getenv("FEEDBACK_MINUTE", "45")) 
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
    """Надсилає актуальний список туристів та реєструє його для видалення"""
    global pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.user_id, u.username, u.full_name, d.discount_value 
            FROM users u 
            LEFT JOIN discounts d ON u.user_id = d.user_id AND d.is_used = FALSE
        """)
    text = "👥 <b>Список туристів:</b>\n━━━━━━━━━━━━━━━\n"
    if not rows:
        text += "База порожня."
    else:
        for row in rows:
            username = f"@{row['username']}" if row['username'] else "немає"
            name = row['full_name'] or "Ім'я не вказано"
            discount = f" | 🎁 {row['discount_value']}%" if row['discount_value'] else ""
            text += f"👤 <b>{name}</b> — {username} (<code>{row['user_id']}</code>){discount}\n"
    
    new_msg = await bot.send_message(chat_id=message.chat.id, text=text, parse_mode="HTML")
    
    data = await state.get_data()
    current_msgs = data.get("admin_msgs_to_clean", [])
    current_msgs.append(new_msg.message_id)
    await state.update_data(admin_msgs_to_clean=current_msgs)

pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
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
    logging.info(f"🔎 [SCHEDULER] Перевірка бази на дату: {today}")
    
    async with pool.acquire() as conn:
        # Вибираємо лише user_id, оскільки стовпця id немає
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
                # Оновлюємо статус за user_id
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

# --- ОБРОБНИКИ КОМАНД (ВЕРХНІЙ ПРІОРИТЕТ) ---

@dp.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    global pool # Додай для впевненості
    args = command.args
    user = message.from_user
    name = user.full_name or "Мандрівник"
    await save_user(user)
    
    if args == "discount":
        existing_discount = await get_user_discount(user.id)
        if existing_discount:
            discount = existing_discount['discount_value']
            greeting = f"Вітаємо, {name}! 🎁 У вас вже є активна знижка: {discount}%.\nВикористайте її під час бронювання наступного туру!"
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
            greeting = f"Вітаємо, {name}! 🎁 Ваша знижка: {discount_row['discount_value']}%."
        else:
            greeting = f"Вітаємо, {name}! Я допоможу вам підібрати тур."
    
    await message.answer(greeting)
    await state.set_state(TourRequest.start_confirmed)
    msg = await message.answer("Натисніть кнопку нижче, щоб розпочати:", reply_markup=start_inline_kb())
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
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", user_id)
        if row:
            discount = row['discount_value']
            text = f"🎁 {name}, у вас є активна знижка: **{discount}%**\nВикористайте її під час бронювання наступного туру!"
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
    await state.set_state(TourRequest.start_confirmed)
    await message.answer(text, parse_mode="Markdown", reply_markup=start_inline_kb())

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
    final_destination = replacements.get(text, message.text.strip().capitalize())
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
    await state.update_data(nights=message.text)
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
f"📅 <b>Дати:</b> {data.get('date_from')} - {data.get('date_to')}\n"
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
f"<b>Деталі вашої заявки:</b>\n"
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
            await conn.execute("INSERT INTO feedbacks (user_id, return_date, sent) VALUES ($1, $2, 0)", client_id, formatted)
        await clean_admin_messages(state, callback_query.message.chat.id)
        report_msg = await callback_query.message.answer(
            f"✅ <b>Запит на відгук заплановано!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📅 <b>Дата:</b> {formatted}\n"
            f"⏰ <b>Час:</b> {FEEDBACK_HOUR}:00\n"
            f"👤 <b>Клієнт:</b> {username} (<code>{client_id}</code>)\n"
            f"━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )
        msgs = [report_msg.message_id]
        await state.update_data(admin_msgs_to_clean=msgs)
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
