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
def start_kb():
    builder = ReplyKeyboardBuilder()
    builder.add(types.KeyboardButton(text="🚀 ПОЧАТИ ПІДБІР ТУРУ"))
    return builder.as_markup(resize_keyboard=True)

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
    msg = await message.answer(
        f"👋 Вітаю, {message.from_user.first_name}!\nЯ допоможу Вам підібрати ідеальний тур. Натисніть кнопку нижче:", 
        reply_markup=start_kb()
    )
    await save_msg(message, state)
    await save_msg(msg, state)
    await state.set_state(TourRequest.start_confirmed)

@dp.message(TourRequest.start_confirmed)
async def process_start_button(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    if message.text == "🚀 ПОЧАТИ ПІДБІР ТУРУ":
        msg = await message.answer("🌍 Куди б Ви хотіли поїхати?", reply_markup=types.ReplyKeyboardRemove())
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
    msg = await message.answer(f"✅ Напрямок: {final_destination}\n\n👤 Оберіть кількість дорослих:", reply_markup=builder.as_markup())
    await save_msg(msg, state)
    await state.set_state(TourRequest.adults_count)

@dp.callback_query(F.data.startswith("adults_"), TourRequest.adults_count)
async def process_adults(callback_query: types.CallbackQuery, state: FSMContext):
    count = callback_query.data.split("_")[1]
    await state.update_data(adults=count)
    
    builder = InlineKeyboardBuilder()
    builder.add(types.InlineKeyboardButton(text="Без дітей (0)", callback_data="child_0"))
    builder.add(types.InlineKeyboardButton(text="1", callback_data="child_1"),
                types.InlineKeyboardButton(text="2", callback_data="child_2"),
                types.InlineKeyboardButton(text="3+", callback_data="child_3"))
    builder.adjust(1, 3)
    
    await callback_query.message.edit_text(f"👤 Дорослих: {count}\n\n👶 Скільки буде дітей?", reply_markup=builder.as_markup())
    await state.set_state(TourRequest.children_count)

@dp.callback_query(F.data.startswith("child_"), TourRequest.children_count)
async def process_children(callback_query: types.CallbackQuery, state: FSMContext):
    count = callback_query.data.split("_")[1]
    await state.update_data(children=count)
    
    await callback_query.message.edit_text(f"👶 Дітей: {count}\n\n📅 Оберіть дату, з якої можна планувати виліт (З):", reply_markup=await SimpleCalendar().start_calendar())
    await state.set_state(TourRequest.date_from)

@dp.callback_query(SimpleCalendarCallback.filter(), TourRequest.date_from)
async def process_date_from(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        formatted = date.strftime("%d.%m.%Y")
        await state.update_data(date_from=formatted)
        await callback_query.message.edit_text(f"📅 Дата вильоту (З): {formatted}\n\n📅 Тепер оберіть дату (ПО):", reply_markup=await SimpleCalendar().start_calendar())
        await state.set_state(TourRequest.date_to)

@dp.callback_query(SimpleCalendarCallback.filter(), TourRequest.date_to)
async def process_date_to(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        formatted = date.strftime("%d.%m.%Y")
        await state.update_data(date_to=formatted)
        
        await callback_query.message.edit_text(f"✅ Період вильоту обрано.\n\n🌙 На скільки ночей плануєте відпочинок?")
        await state.set_state(TourRequest.nights_count)

@dp.message(TourRequest.nights_count)
async def process_nights(message: types.Message, state: FSMContext):
    await save_msg(message, state)
    await state.update_data(nights=message.text)
    msg = await message.answer("⭐ Оберіть категорію готелю", reply_markup=stars_kb())
    await save_msg(msg, state)
    await state.set_state(TourRequest.hotel_stars)

@dp.callback_query(F.data.startswith("star_"), TourRequest.hotel_stars)
async def process_stars(callback_query: types.CallbackQuery, state: FSMContext):
    star = callback_query.data.split("_")[1]
    label = "Будь-яка" if star == "any" else f"{star}*"
    await state.update_data(stars=label)
    await callback_query.message.edit_text(f"⭐ Готель: {label}\n\n🍴 Яке харчування Вам підходить:", reply_markup=meals_kb())
    await state.set_state(TourRequest.meal_type)

@dp.callback_query(F.data.startswith("meal_"), TourRequest.meal_type)
async def process_meals(callback_query: types.CallbackQuery, state: FSMContext):
    meal_map = {"BB": "Сніданки", "HB": "Сніданок+вечеря", "AI": "Все включено", "UAI": "Ультра все включено", "RO": "Без харчування"}
    meal_text = meal_map.get(callback_query.data.split("_")[1], "Будь-яке")
    await state.update_data(meals=meal_text)
    await callback_query.message.edit_text(f"🍴 Харчування: {meal_text}\n\n💰 Який Ви плануєте витратити бюджет у гривнях (цифрами):")
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
    # Додаємо останнє повідомлення користувача до списку видалення
    await save_msg(message, state)
    data = await state.get_data()
    user = message.from_user
    
    # Формуємо таблицю даних
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

    # Звіт для адміна
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
    
    # Відправка менеджеру
    await bot.send_message(ADMIN_ID, report, parse_mode="HTML")

    # ВИДАЛЕННЯ ВСІХ ПОВІДОМЛЕНЬ
    msgs_to_delete = data.get("msgs_to_delete", [])
    for m_id in msgs_to_delete:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=m_id)
        except Exception:
            pass

    re_builder = ReplyKeyboardBuilder()
    re_builder.add(types.KeyboardButton(text="🔄 СТВОРИТИ НОВУ ЗАЯВКУ"))
    
    # Фінальне повідомлення клієнту з таблицею
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
            target_id = int(input_data)
            async with db.execute("SELECT username FROM users WHERE user_id = ?", (target_id,)) as cursor:
                row = await cursor.fetchone()
                if row: username = f"@{row[0]}"
        else:
            async with db.execute("SELECT user_id FROM users WHERE username = ?", (input_data,)) as cursor:
                row = await cursor.fetchone()
                if row: 
                    target_id = row[0]
                    username = f"@{input_data}"

    if target_id:
        await state.update_data(client_id=target_id, client_username=username)
        msg = await message.answer(
            f"✅ Клієнта знайдено:\nID: <code>{target_id}</code>\nUser: {username}\n\nТепер оберіть дату повернення:", 
            reply_markup=await SimpleCalendar().start_calendar(),
            parse_mode="HTML"
        )
        await save_msg(msg, state)
        await state.set_state(AdminPanel.waiting_for_date)
    else:
        msg = await message.answer("❌ Клієнта не знайдено в базі.")
        await save_msg(msg, state)

@dp.callback_query(SimpleCalendarCallback.filter(), AdminPanel.waiting_for_date)
async def process_admin_date(callback_query: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback_query, callback_data)
    if selected:
        formatted = date.strftime("%d.%m.%Y")
        data = await state.get_data()
        client_id = data['client_id']
        username = data['client_username']
        
        async with aiosqlite.connect("travel_bot.db") as db:
            await db.execute("INSERT INTO feedbacks (user_id, return_date) VALUES (?, ?)", (client_id, formatted))
            await db.commit()

        # ВИДАЛЕННЯ ПОВІДОМЛЕНЬ ПІСЛЯ ВИБОРУ ДАТИ
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
    scheduler.add_job(check_returns, 'cron', hour=16, minute=0)
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
