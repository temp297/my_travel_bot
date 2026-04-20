import asyncio
import logging
import os
import aiosqlite
import random
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.exceptions import TelegramBadRequest
from datetime import datetime

# --- НАЛАШТУВАННЯ ---
API_TOKEN = '8742210436:AAEX2p71Tpp4V1cKsm10WnPZ385ZTolRVok'
ADMIN_ID = 7185133060
REVIEWS_CHAT_ID = -1003818943967

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- СТАНИ ---
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

# --- ФУНКЦІЯ ЗБЕРЕЖЕННЯ ПОВІДОМЛЕНЬ ДЛЯ ВИДАЛЕННЯ ---
async def save_msg(message: types.Message, state: FSMContext):
    data = await state.get_data()
    msgs = data.get("msgs_to_delete", [])
    msgs.append(message.message_id)
    await state.update_data(msgs_to_delete=msgs)

# --- БАЗА ДАНИХ ТА ПЛАНУВАЛЬНИК ---
async def init_db():
    async with aiosqlite.connect("travel_bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedbacks (
                user_id INTEGER,
                return_date TEXT,
                sent INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        await db.commit()

async def save_user(user: types.User):
    if user.username:
        async with aiosqlite.connect("travel_bot.db") as db:
            await db.execute(
                "INSERT OR REPLACE INTO users (user_id, username) VALUES (?, ?)",
                (user.id, user.username.lower())
            )
            await db.commit()

async def check_returns():
    today = datetime.now().strftime("%d.%m.%Y")
    async with aiosqlite.connect("travel_bot.db") as db:
        async with db.execute("SELECT user_id FROM feedbacks WHERE return_date = ? AND sent = 0", (today,)) as cursor:
            users = await cursor.fetchall()
            for row in users:
                user_id = row[0]
                try:
                    await bot.send_message(
                        user_id,
                        "✈️ З поверненням! Сподіваємося, Ваш відпочинок був чудовим.\n\n"
                        "Будь ласка, оцініть нашу роботу:",
                        reply_markup=rating_kb()
                    )
                    await db.execute("UPDATE feedbacks SET sent = 1 WHERE user_id = ?", (user_id,))
                except Exception as e:
                    logging.error(f"Error: {e}")
        await db.commit()

# --- КЛАВІАТУРИ ---
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

# --- ОБРОБНИКИ АНКЕТИ ---

@dp.message(Command("start"))
@dp.message(F.text == "🔄 СТВОРИТИ НОВУ ЗАЯВКУ")
async def cmd_start(message: types.Message, state: FSMContext):
    await save_user(message.from_user)
    await state.clear()
    # Надсилаємо Inline-кнопку замість звичайної
    msg = await message.answer(
        f"👋 Вітаю, {message.from_user.first_name}!\nЯ допоможу Вам підібрати ідеальний тур. Натисніть кнопку нижче:", 
        reply_markup=start_inline_kb()
    )
    await save_msg(message, state)
    await save_msg(msg, state)
    await state.set_state(TourRequest.start_confirmed)

# ПЕРЕВІРКА: якщо користувач замість кнопки ввів текст "123" або інший
@dp.message(TourRequest.start_confirmed)
async def check_start_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, натисніть на кнопку «🚀 ПОЧАТИ ПІДБІР ТУРУ»")
    await save_msg(msg, state)

# ОБРОБНИК НАТИСКАННЯ: спрацює тільки при кліку на Inline-кнопку
@dp.callback_query(F.data == "start_selection", TourRequest.start_confirmed)
async def process_start_callback(callback_query: types.CallbackQuery, state: FSMContext):
    # Видаляємо кнопку, щоб вона не залишалася в тексті
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    msg = await callback_query.message.answer("🌍 Вкажіть пріоритетну країну та назву готелю (якщо визначилися)", reply_markup=types.ReplyKeyboardRemove())
    await save_msg(msg, state)
    await state.set_state(TourRequest.destination)

@dp.message(TourRequest.destination)
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
        "таїланд": "Таїланд", "таиland": "Таїланд", "тайланд": "Таїланд", "тай": "Таїланд", "пхукет": "Таїланд (Пхукет)",
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

# ПЕРЕВІРКА КНОПОК ДОРОСЛИХ
@dp.message(TourRequest.adults_count)
async def check_adults_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть кількість дорослих натиснувши кнопку вище.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("adults_"), TourRequest.adults_count)
async def process_adults(callback_query: types.CallbackQuery, state: FSMContext):
    # Видаляємо кнопки лише після успішного натискання
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

# ПЕРЕВІРКА КНОПОК ДІТЕЙ
@dp.message(TourRequest.children_count)
async def check_children_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть кількість дітей натиснувши кнопку вище.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("child_"), TourRequest.children_count)
async def process_children(callback_query: types.CallbackQuery, state: FSMContext):
    # Видаляємо кнопки вибору дітей
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

# ВСТАНОВЛЮЄМО СТАН ДЛЯ КАЛЕНДАРЯ
# ПЕРЕВІРКА КАЛЕНДАРЯ (З)
@dp.message(TourRequest.date_from)
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
        await state.set_state(TourRequest.date_to) # ПЕРЕХОДИМО ДО НАСТУПНОЇ ДАТИ

# ПЕРЕВІРКА КАЛЕНДАРЯ (ПО)
@dp.message(TourRequest.date_to)
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
        await state.set_state(TourRequest.nights_count) # ВСТАНОВЛЮЄМО СТАН ДЛЯ НОЧЕЙ

@dp.message(TourRequest.nights_count)
async def process_nights(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    await state.update_data(nights=message.text)
    msg = await message.answer("⭐ Оберіть категорію готелю", reply_markup=stars_kb())
    await save_msg(msg, state)
    await state.set_state(TourRequest.hotel_stars)

# ПЕРЕВІРКА КНОПОК ЗІРОК
@dp.message(TourRequest.hotel_stars)
async def check_stars_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть категорію готелю кнопкою.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("star_"), TourRequest.hotel_stars)
async def process_stars(callback_query: types.CallbackQuery, state: FSMContext):
    # Видаляємо кнопки зірок
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    star = callback_query.data.split("_")[1]
    label = "Будь-яка" if star == "any" else f"{star}*"
    await state.update_data(stars=label)
    msg1 = await callback_query.message.answer(f"⭐ Готель: {label}")
    msg2 = await callback_query.message.answer(f"🍴 Яке харчування Вам підходить:", reply_markup=meals_kb())
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.meal_type)

# ПЕРЕВІРКА КНОПОК ХАРЧУВАННЯ
@dp.message(TourRequest.meal_type)
async def check_meals_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть тип харчування кнопкою.")
    await save_msg(msg, state)

@dp.callback_query(F.data.startswith("meal_"), TourRequest.meal_type)
async def process_meals(callback_query: types.CallbackQuery, state: FSMContext):
    # Видаляємо кнопки харчування
    await callback_query.message.edit_reply_markup(reply_markup=None)
    
    meal_map = {"BB": "Сніданки", "HB": "Сніданок+вечеря", "AI": "Все включено", "UAI": "Ультра все включено", "RO": "Без харчування"}
    meal_text = meal_map.get(callback_query.data.split("_")[1], "Будь-яке")
    await state.update_data(meals=meal_text)
    msg1 = await callback_query.message.answer(f"🍴 Харчування: {meal_text}")
    msg2 = await callback_query.message.answer(f"💰 Який Ви плануєте витратити бюджет у гривнях (цифрами):")
    await save_msg(msg1, state)
    await save_msg(msg2, state)
    await state.set_state(TourRequest.budget)

@dp.message(TourRequest.budget)
async def process_budget(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    await state.update_data(budget=message.text)
    msg = await message.answer("📞 Ваш номер телефону або нікнейм для зв'язку:")
    await save_msg(msg, state)
    await state.set_state(TourRequest.contact)

@dp.message(TourRequest.contact)
async def process_contact(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    data = await state.get_data()
    user = message.from_user
    
    info_table = (
        f"🌍 <b>Напрямок:</b> {data.get('destination')}\n"
        f"👥 <b>Склад:</b> {data.get('adults')} дор. + {data.get('children')} діт.\n"
        f"📅 <b>Дати:</b> {data.get('date_from')} - {data.get('date_to')}\n"
        f"🌙 <b>Ночей:</b> {data.get('nights')}\n"
        f"⭐ <b>Готель:</b> {data.get('stars')}\n"
        f"🍴 <b>Харчування:</b> {data.get('meals')}\n"
        f"💰 <b>Бюджет:</b> {data.get('budget')} ГРН\n"
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
    for m_id in msgs_to_delete:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=m_id)
        except Exception:
            pass

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

@dp.message(FeedbackState.waiting_for_text)
async def process_feedback_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("user_rating")
    user = message.from_user
    
    feedback_header = (
        f"🌟 <b>НОВИЙ ВІДГУК!</b>\n"
        f"👤 <b>Від:</b> {user.full_name}\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"📱 <b>Username:</b> @{user.username if user.username else 'немає'}\n"
        f"⭐ <b>Оцінка:</b> {rating}⭐\n"
        f"━━━━━━━━━━━━━━━"
    )
    
    await bot.send_message(REVIEWS_CHAT_ID, feedback_header, parse_mode="HTML")
    forwarded_msg = await message.forward(chat_id=REVIEWS_CHAT_ID)
    await message.answer("❤️ Дякуємо за Ваш відгук! Його опубліковано у чаті мандрівників.")
    await state.clear()
    
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
        logging.error(f"Error replying: {e}")

# --- ПАНЕЛЬ АДМІНА ---

@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_start(message: types.Message, state: FSMContext):
    await state.clear()
    msg = await message.answer("🛠 <b>Панель менеджера</b>\n\nВведіть <b>ID</b> клієнта або його <b>@username</b>:", parse_mode="HTML")
    await save_msg(message, state)
    await save_msg(msg, state)
    await state.set_state(AdminPanel.waiting_for_client_info)

@dp.message(AdminPanel.waiting_for_client_info)
async def process_admin_search(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    input_data = message.text.strip().replace("@", "").lower()
    
    target_id = None
    username = "не вказано"

    async with aiosqlite.connect("travel_bot.db") as db:
        if input_data.isdigit():
            async with db.execute("SELECT user_id, username FROM users WHERE user_id = ?", (int(input_data),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id, row_username = row
                    username = f"@{row_username}" if row_username else "не вказано"
        else:
            async with db.execute("SELECT user_id, username FROM users WHERE LOWER(username) = ?", (input_data,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    target_id, row_username = row
                    username = f"@{row_username}" if row_username else f"@{input_data}"

    if target_id is not None:
        await state.update_data(client_id=target_id, client_username=username)
        
        # Створюємо клавіатуру календаря
        calendar_kb = await SimpleCalendar().start_calendar()
        
        msg = await message.answer(
            f"✅ Клієнта знайдено:\nID: <code>{target_id}</code>\nUser: {username}\n\nТепер оберіть дату повернення:", 
            reply_markup=calendar_kb, # ПЕРЕВІРТЕ ЦЕЙ РЯДОК
            parse_mode="HTML"
        )
        await save_msg(msg, state)
        await state.set_state(AdminPanel.waiting_for_date)
    else:
        msg = await message.answer("❌ Клієнта не знайдено. Спробуйте ще раз:")
        await save_msg(msg, state)

# ПЕРЕВІРКА КАЛЕНДАРЯ АДМІНА
@dp.message(AdminPanel.waiting_for_date)
async def check_admin_date_input(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    msg = await message.answer("⚠️ Будь ласка, оберіть дату повернення на календарі вище.")
    await save_msg(msg, state)

@dp.callback_query(SimpleCalendarCallback.filter(), AdminPanel.waiting_for_date)
async def process_admin_date(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    
    if selected:
        today = datetime.now().date()
        if date.date() < today:
            # 1. Показуємо спливаюче вікно
            await callback_query.answer("❌ Дата не може бути в минулому!", show_alert=True)
            
            # 2. ПЕРЕМАЛЬОВУЄМО календар у тому самому повідомленні
            # Це важливо: ми не даємо йому зникнути
            await callback_query.message.edit_reply_markup(
                reply_markup=await SimpleCalendar().start_calendar()
            )
            return # Зупиняємо виконання, чекаємо на новий клік

        # ЯКЩО ДАТА КОРЕКТНА:
        # Прибираємо кнопки (тепер вони точно не потрібні)
        await callback_query.message.edit_reply_markup(reply_markup=None)
        
        formatted = date.strftime("%d.%m.%Y")
        data = await state.get_data()
        client_id = data['client_id']
        username = data['client_username']
        
        async with aiosqlite.connect("travel_bot.db") as db:
            await db.execute(
                "INSERT INTO feedbacks (user_id, return_date) VALUES (?, ?)", 
                (client_id, formatted)
            )
            await db.commit()

        # Видаляємо всі проміжні повідомлення (включаючи попередження про текст)
        msgs_to_delete = data.get("msgs_to_delete", [])
        for m_id in msgs_to_delete:
            try:
                await bot.delete_message(chat_id=callback_query.message.chat.id, message_id=m_id)
            except Exception:
                pass

        await callback_query.message.answer(
            f"✅ <b>Заплановано на {formatted}</b>\n"
            f"👤 Клієнт: <code>{client_id}</code> ({username})",
            parse_mode="HTML"
        )
        await state.clear()

# --- ТЕХНІЧНИЙ БЛОК ---
async def handle(request): return web.Response(text="Live")

async def main():
    await init_db()
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 8080))).start()
    await bot.set_my_commands([
        types.BotCommand(command="start", description="🚀 Почати підбір туру"), 
        types.BotCommand(command="admin", description="🛠 Панель менеджера")
    ])
    scheduler.add_job(check_returns, 'cron', hour=10, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
