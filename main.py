import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
import pytz

from aiohttp import web
import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.filters import Command as CommandFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback

# --- КОНФІГУРАЦІЯ (ЧАСТИНА 1 & 2) ---
API_TOKEN = os.getenv("API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
REVIEWS_CHAT_ID = int(os.getenv("REVIEWS_CHAT_ID"))
FEEDBACK_HOUR = int(os.getenv("FEEDBACK_HOUR", "11"))
FEEDBACK_MINUTE = int(os.getenv("FEEDBACK_MINUTE", "10"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super_secret_key")

BOT_COMMANDS = ["start", "discount", "admin", "use_discount", "users"]

# Правила для паспорта з Частини 3
COUNTRY_RULES = {
    "Туреччина": 183, "Єгипет": 183, "Туніс": 183, "ОАЕ": 183,
    "Таїланд": 183, "Греція": 183, "Іспанія": 183, "Чорногорія": 183,
    "Хорватія": 183, "Албанія": 183, "Шрі-Ланка": 183
}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
ukraine_tz = pytz.timezone('Europe/Kyiv')
scheduler = AsyncIOScheduler(timezone=ukraine_tz)
pool = None

# --- СТАНИ (ОБ'ЄДНАНІ З УСІХ ЧАСТИН) ---
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

class PassportForm(StatesGroup):
    country = State()
    start_date = State()
    nights = State()

# --- ФУНКЦІЇ БАЗИ ДАНИХ ТА ДОПОМІЖНІ ---
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT);
            CREATE TABLE IF NOT EXISTS discounts (user_id BIGINT PRIMARY KEY, discount_value INTEGER, is_used BOOLEAN DEFAULT FALSE);
            CREATE TABLE IF NOT EXISTS feedbacks (id SERIAL PRIMARY KEY, user_id BIGINT, return_date TEXT, sent INTEGER DEFAULT 0);
        """)

async def save_msg(message: types.Message, state: FSMContext):
    data = await state.get_data()
    msgs = data.get("msgs_to_delete", [])
    msgs.append(message.message_id)
    await state.update_data(msgs_to_delete=msgs)

async def clean_admin_messages(state: FSMContext, chat_id: int):
    data = await state.get_data()
    msgs = data.get("admin_msgs_to_clean", [])
    for m_id in msgs:
        try: await bot.delete_message(chat_id, m_id)
        except: pass
    await state.update_data(admin_msgs_to_clean=[])

# --- КЛАВІАТУРИ ---
def stars_kb():
    builder = InlineKeyboardBuilder()
    for s in ["3*", "4*", "5*", "any"]:
        label = "Будь-яка" if s == "any" else s
        builder.add(types.InlineKeyboardButton(text=label, callback_data=f"star_{s}"))
    builder.adjust(3, 1)
    return builder.as_markup()

def meals_kb():
    builder = InlineKeyboardBuilder()
    meal_map = {"BB": "Сніданки", "HB": "Сніданок+вечеря", "AI": "Все включено", "UAI": "Ультра все включено", "RO": "Без харчування"}
    for k, v in meal_map.items():
        builder.row(types.InlineKeyboardButton(text=v, callback_data=f"meal_{k}"))
    return builder.as_markup()

# --- ГОЛОВНИЙ СТАРТ ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="🚀 ПОЧАТИ ПІДБІР ТУРУ", callback_data="start_selection"))
    builder.row(types.InlineKeyboardButton(text="🛂 ПЕРЕВІРКА ПАСПОРТА", callback_data="passport_start"))
    
    await message.answer(
        "👋 Вітаю! Я допоможу Вам підібрати тур або перевірити термін дії паспорта.",
        reply_markup=builder.as_markup()
    )

# --- ЛОГІКА ПЕРЕВІРКИ ПАСПОРТА (ЧАСТИНА 3) ---
@dp.callback_query(F.data == "passport_start")
async def passport_init(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PassportForm.country)
    builder = InlineKeyboardBuilder()
    for country in COUNTRY_RULES.keys():
        builder.add(types.InlineKeyboardButton(text=country, callback_data=f"pselect_{country}"))
    builder.adjust(2)
    await callback.message.edit_text(
        "👋 Вітаю!\nЯ допоможу Вам перевірити термін дії закордонного паспорта для отримання візи.\n\nОберіть країну відпочинку:",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data.startswith("pselect_"), PassportForm.country)
async def process_passport_country(callback: types.CallbackQuery, state: FSMContext):
    country = callback.data.split("_")[1]
    await state.update_data(country=country)
    await callback.message.edit_text(
        f"🌍 Обрано: {country}\nТепер оберіть дату початку подорожі на календарі:",
        reply_markup=await SimpleCalendar().start_calendar()
    )
    await state.set_state(PassportForm.start_date)

@dp.callback_query(SimpleCalendarCallback.filter(), PassportForm.start_date)
async def process_passport_calendar(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        await state.update_data(start_date=date.strftime("%d.%m.%Y"))
        await callback_query.message.edit_text(
            f"✅ Дата вильоту: {date.strftime('%d.%m.%Y')}\n\nСкільки ночей Ви плануєте відпочивати?"
        )
        await state.set_state(PassportForm.nights)

@dp.message(PassportForm.nights)
async def process_passport_nights(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("⚠️ Введіть тільки число (кількість ночей).")
        return
    user_data = await state.get_data()
    start_dt = datetime.strptime(user_data['start_date'], "%d.%m.%Y")
    nights = int(message.text)
    country = user_data['country']
    buffer_days = COUNTRY_RULES.get(country, 183)
    final_dt = start_dt + timedelta(days=nights) + timedelta(days=buffer_days)
    
    result = (
        f"Для отримання візи до країни **{country}** — термін дії паспорта повинен бути не менше ніж до:\n\n"
        f"👉 **{final_dt.strftime('%d.%m.%Y')}**\n\n"
        f"_(Вимога: +{buffer_days} дні з кінця поїздки)_"
    )
    await message.answer(result, parse_mode="Markdown")
    await state.clear()
    re_builder = InlineKeyboardBuilder()
    re_builder.add(types.InlineKeyboardButton(text="🔄 НОВИЙ РОЗРАХУНОК", callback_data="passport_start"))
    await message.answer("Бажаєте зробити ще один розрахунок?", reply_markup=re_builder.as_markup())

# --- ЛОГІКА ПІДБОРУ ТУРУ (ЧАСТИНА 2) ---

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
    if not nights_input.isdigit():
        msg = await message.answer("⚠️ Будь ласка, введіть кількість ночей тільки цифрами (наприклад: 7):")
        await save_msg(msg, state)
        return
    await state.update_data(nights=nights_input)
    msg = await message.answer("⭐ Оберіть категорію готелю", reply_markup=stars_kb())
    await save_msg(msg, state)
    await state.set_state(TourRequest.hotel_stars)

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
        discount_row = await conn.fetchrow("SELECT discount_value FROM discounts WHERE user_id = $1 AND is_used = FALSE", user.id)
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
    report = (f"🔥 <b>НОВА ЗАЯВКА НА ТУР!</b>\n━━━━━━━━━━━━━━━\n{info_table}\n━━━━━━━━━━━━━━━\n"
              f"👤 <b>Клієнт:</b> <a href='tg://user?id={user.id}'>{user.full_name}</a>\n🆔 <b>ID:</b> <code>{user.id}</code>")
    
    await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
    
    # Видалення повідомлень
    msgs_to_delete = data.get("msgs_to_delete", [])
    for m_id in msgs_to_delete:
        try: await bot.delete_message(message.chat.id, m_id)
        except: pass
        
    re_builder = ReplyKeyboardBuilder()
    re_builder.add(types.KeyboardButton(text="🔄 СТВОРИТИ НОВУ ЗАЯВКУ"))
    await message.answer(f"✅ Дякуємо! Заявку успішно відправлено!\n\n<b>Деталі вашої заявки:</b>\n{info_table}", 
                         parse_mode="HTML", reply_markup=re_builder.as_markup(resize_keyboard=True))
    await state.clear()

# --- ВІДГУКИ ТА АДМІН-ФУНКЦІЇ ---
@dp.callback_query(F.data.startswith("rate_"))
async def process_rating(callback_query: types.CallbackQuery, state: FSMContext):
    rating = int(callback_query.data.split("_")[1])
    await state.update_data(user_rating=rating)
    await callback_query.message.edit_text(f"Ви поставили {rating}⭐!\nНапишіть відгук:")
    await state.set_state(FeedbackState.waiting_for_text)

@dp.message(FeedbackState.waiting_for_text)
async def process_feedback_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("user_rating")
    await bot.send_message(REVIEWS_CHAT_ID, f"🌟 Новий відгук ({rating}⭐) від {message.from_user.full_name}")
    await message.forward(chat_id=REVIEWS_CHAT_ID)
    await message.answer("❤️ Дякуємо!")
    await state.clear()

async def check_returns():
    # Логіка планувальника (спрощена для стабільності)
    pass

# --- КОРЕКТНИЙ ЗАПУСК ДЛЯ RENDER ---

async def on_startup(app):
    # Ініціалізуємо базу даних ТІЛЬКИ ТУТ
    await init_db()
    
    # Налаштування Webhook
    webhook_url_full = f"{WEBHOOK_URL}/webhook"
    await bot.set_webhook(
        url=webhook_url_full, 
        secret_token=WEBHOOK_SECRET
    )
    
    # Запуск планувальника
    if not scheduler.running:
        scheduler.start()
    
    logging.info(f"Webhook set to: {webhook_url_full}")

async def on_shutdown(app):
    logging.info("Shutting down...")
    if pool:
        await pool.close()
    if scheduler.running:
        scheduler.shutdown()
    await bot.delete_webhook()

def main():
    # Створюємо додаток aiohttp
    app = web.Application()
    
    # Налаштовуємо обробник запитів від Telegram
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    )
    webhook_requests_handler.register(app, path="/webhook")
    
    # Підключаємо aiogram до aiohttp
    setup_application(app, dp, bot=bot)
    
    # Додаємо обробники подій запуску та зупинки
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Проста сторінка для перевірки працездатності (Health Check)
    app.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
    
    # Отримуємо порт від Render
    port = int(os.environ.get("PORT", 8000))
    
    # ЗАПУСКАЄМО СЕРВЕР (без asyncio.run)
    web.run_app(app, host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()
