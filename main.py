import asyncio
import logging
import sqlite3
import json
import os
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ---------------------------- Настройка логирования ----------------------------
logging.basicConfig(level=logging.INFO)

# ---------------------------- Загрузка/сохранение конфигурации ----------------------------
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(token, admin_id):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"BOT_TOKEN": token, "ADMIN_ID": admin_id}, f, ensure_ascii=False, indent=4)

config = load_config()
BOT_TOKEN = config.get("BOT_TOKEN")
ADMIN_ID = config.get("ADMIN_ID")

if not BOT_TOKEN or not ADMIN_ID:
    BOT_TOKEN = input("Введите токен бота: ").strip()
    ADMIN_ID = input("Введите Telegram ID администратора (число): ").strip()
    try:
        ADMIN_ID = int(ADMIN_ID)
    except ValueError:
        print("Ошибка: ID администратора должен быть числом.")
        exit(1)
    save_config(BOT_TOKEN, ADMIN_ID)
else:
    print("Конфигурация загружена из config.json")

# ---------------------------- Инициализация бота и диспетчера ----------------------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------------- Работа с базой данных SQLite ----------------------------
DB_NAME = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Таблица пользователей с полем для пути к последнему файлу списка
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            is_blocked INTEGER DEFAULT 0,
            last_list_file TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

# Вспомогательные функции для работы с БД
def db_execute(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(query, params)
    if fetchone:
        res = cur.fetchone()
    elif fetchall:
        res = cur.fetchall()
    else:
        res = None
    conn.commit()
    conn.close()
    return res

def register_user(user_id, username, full_name):
    db_execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
        (user_id, username, full_name)
    )

def get_user(user_id):
    return db_execute("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_blocked(user_id):
    user = get_user(user_id)
    return user and user[3] == 1  # is_blocked поле

def update_block_status(username, block: bool):
    db_execute("UPDATE users SET is_blocked = ? WHERE username = ?", (1 if block else 0, username))

def get_all_users():
    return db_execute("SELECT user_id, username, full_name, is_blocked FROM users", fetchall=True)

def update_last_list_file(user_id, file_path):
    db_execute("UPDATE users SET last_list_file = ? WHERE user_id = ?", (file_path, user_id))

def get_last_list_file(user_id):
    user = get_user(user_id)
    return user[4] if user else None  # last_list_file

# ---------------------------- Работа с файлами списков ----------------------------
SOLDIER_LISTS_DIR = "soldier_lists"
os.makedirs(SOLDIER_LISTS_DIR, exist_ok=True)

def save_soldier_list_to_file(user_id, username, soldiers_list):
    """
    Сохраняет список рядовых в файл: soldier_lists/{username}_{datetime}.txt
    Удаляет предыдущий файл пользователя, если он есть.
    Возвращает путь к сохранённому файлу.
    """
    # Удаляем предыдущий файл, если он есть
    old_file = get_last_list_file(user_id)
    if old_file and os.path.exists(old_file):
        try:
            os.remove(old_file)
        except Exception as e:
            logging.error(f"Не удалось удалить старый файл {old_file}: {e}")

    # Формируем имя нового файла
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_username = username.replace("@", "").replace("/", "_") if username else f"user_{user_id}"
    filename = f"{safe_username}_{timestamp}.txt"
    file_path = os.path.join(SOLDIER_LISTS_DIR, filename)

    # Записываем список построчно
    with open(file_path, "w", encoding="utf-8") as f:
        for soldier in soldiers_list:
            f.write(soldier + "\n")

    # Обновляем запись в БД
    update_last_list_file(user_id, file_path)
    return file_path

def read_soldier_list_from_file(user_id):
    """Читает последний файл списка пользователя и возвращает список строк."""
    file_path = get_last_list_file(user_id)
    if not file_path or not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

# ---------------------------- Клавиатуры ----------------------------
def build_main_menu_keyboard(is_admin: bool = False):
    kb = [
        [KeyboardButton(text="📥 Загрузить список рядовых")],
        [KeyboardButton(text="🔍 Проверить списки")],
        [KeyboardButton(text="📋 Показать список рядовых")],   # новая кнопка
        [KeyboardButton(text="📝 Отзыв")]
    ]
    if is_admin:
        kb.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_panel_keyboard():
    kb = [
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_list_users")],
        [InlineKeyboardButton(text="🔒 Заблокировать пользователя", callback_data="admin_block_user")],
        [InlineKeyboardButton(text="🔓 Разблокировать пользователя", callback_data="admin_unblock_user")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ---------------------------- Машины состояний (FSM) ----------------------------
class RegisterState(StatesGroup):
    waiting_for_fullname = State()

class LoadListState(StatesGroup):
    waiting_for_format = State()                # выбор формата (через ; или enter)
    waiting_for_space_after_semicolon = State() # есть ли пробел после ";"
    waiting_for_choice = State()                # выбор способа загрузки (файл/текст)
    waiting_for_file = State()                  # ожидание файла
    waiting_for_text = State()                   # ожидание текста

class CheckListState(StatesGroup):
    waiting_for_input = State()

class FeedbackState(StatesGroup):
    waiting_for_feedback = State()

class AdminState(StatesGroup):
    waiting_for_username_to_block = State()
    waiting_for_username_to_unblock = State()

# ---------------------------- Хэндлеры ----------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    if user:
        if user[3] == 1:
            await message.answer("Вы заблокированы и не можете использовать бота.")
            return
        await message.answer(
            "Главное меню:",
            reply_markup=build_main_menu_keyboard(is_admin(user_id))
        )
    else:
        await state.set_state(RegisterState.waiting_for_fullname)
        await message.answer(
            "Привет сержант, напиши своё ФИО для регистрации, это нужно для анализа работы бота, "
            "в случае неправдивой регистрации доступ к боту будет ограничен, будем рады, "
            "если после использования оставите отзыв в меню бота 🥰"
        )

@dp.message(RegisterState.waiting_for_fullname)
async def process_fullname(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if not full_name:
        await message.answer("Пожалуйста, введите ФИО.")
        return
    user_id = message.from_user.id
    username = message.from_user.username
    register_user(user_id, username, full_name)
    await state.clear()
    await message.answer(
        f"Спасибо, {full_name}, регистрация завершена!",
        reply_markup=build_main_menu_keyboard(is_admin(user_id))
    )

@dp.message(F.text == "📋 Показать список рядовых")
async def show_soldier_list(message: types.Message):
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    soldiers = read_soldier_list_from_file(user_id)
    if not soldiers:
        await message.answer("Список рядовых пуст. Сначала загрузите список через меню.")
        return
    # Формируем пронумерованный список
    lines = [f"{i+1}. {name}" for i, name in enumerate(soldiers)]
    result = "Ваш список рядовых:\n" + "\n".join(lines)
    # Если список слишком длинный, разбиваем на несколько сообщений
    if len(result) > 4096:
        for x in range(0, len(result), 4096):
            await message.answer(result[x:x+4096])
    else:
        await message.answer(result)

# ---------------------------- Загрузка списка рядовых (новый порядок) ----------------------------
@dp.message(F.text == "📥 Загрузить список рядовых")
async def load_list_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    # Сначала спрашиваем формат
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Через точку с запятой (;)", callback_data="format_semicolon")],
        [InlineKeyboardButton(text="Через Enter", callback_data="format_enter")]
    ])
    await state.set_state(LoadListState.waiting_for_format)
    await message.answer("В каком формате будет список рядовых?", reply_markup=kb)

@dp.callback_query(lambda c: c.data in ["format_semicolon", "format_enter"], StateFilter(LoadListState.waiting_for_format))
async def process_format(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "format_semicolon":
        # Уточняем про пробел после ";"
        await state.set_state(LoadListState.waiting_for_space_after_semicolon)
        await state.update_data(format='semicolon')
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="space_yes")],
            [InlineKeyboardButton(text="Нет", callback_data="space_no")]
        ])
        await callback.message.edit_text("Есть ли пробел после ';'?", reply_markup=kb)
    else:  # enter
        await state.update_data(format='enter', space=False)
        # Переходим к выбору способа загрузки
        await ask_upload_method(callback.message, state)

@dp.callback_query(lambda c: c.data in ["space_yes", "space_no"], StateFilter(LoadListState.waiting_for_space_after_semicolon))
async def process_space(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    has_space = (callback.data == "space_yes")
    await state.update_data(space=has_space)
    # Переходим к выбору способа загрузки
    await ask_upload_method(callback.message, state)

async def ask_upload_method(message: types.Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Файлом", callback_data="upload_file")],
        [InlineKeyboardButton(text="✏️ Текстом", callback_data="upload_text")]
    ])
    await state.set_state(LoadListState.waiting_for_choice)
    await message.answer("Выберите способ загрузки списка:", reply_markup=kb)

@dp.callback_query(lambda c: c.data in ["upload_file", "upload_text"], StateFilter(LoadListState.waiting_for_choice))
async def upload_choice(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "upload_file":
        await state.set_state(LoadListState.waiting_for_file)
        await callback.message.edit_text(
            "Отправьте файл со списком рядовых (текстовый файл).\n"
            "Формат: каждая строка должна содержать фамилию и первую букву имени (например, Иванов И)."
        )
    else:
        await state.set_state(LoadListState.waiting_for_text)
        await callback.message.edit_text(
            "Введите список рядовых текстом.\n"
            "Формат: каждая строка должна содержать фамилию и первую букву имени (например, Иванов И)."
        )

# Обработка текстового ввода
@dp.message(F.text, StateFilter(LoadListState.waiting_for_text))
async def load_text(message: types.Message, state: FSMContext):
    raw_text = message.text
    data = await state.get_data()
    format_type = data.get('format')
    space = data.get('space', False)

    soldiers = parse_soldier_list(raw_text, format_type, space)
    if not soldiers:
        await message.answer("Не удалось извлечь ни одной записи. Проверьте формат и попробуйте снова.")
        return

    await save_and_notify(message, state, soldiers)

# Обработка файлового ввода
@dp.message(F.document, StateFilter(LoadListState.waiting_for_file))
async def load_file(message: types.Message, state: FSMContext):
    document = message.document
    if not document.file_name.endswith('.txt'):
        await message.answer("Пожалуйста, отправьте текстовый файл (.txt).")
        return
    file = await bot.get_file(document.file_id)
    file_content = await bot.download_file(file.file_path)
    raw_text = file_content.read().decode('utf-8')

    data = await state.get_data()
    format_type = data.get('format')
    space = data.get('space', False)

    soldiers = parse_soldier_list(raw_text, format_type, space)
    if not soldiers:
        await message.answer("Не удалось извлечь ни одной записи из файла. Проверьте формат и попробуйте снова.")
        return

    await save_and_notify(message, state, soldiers)

def parse_soldier_list(raw_text: str, format_type: str, space: bool) -> list:
    """Парсит сырой текст в зависимости от формата и возвращает список строк (фамилия + инициал)"""
    lines = []
    if format_type == 'enter':
        # Разделяем по строкам
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    elif format_type == 'semicolon':
        # Разделяем по ";"
        if space:
            # Предполагаем, что после ";" есть пробел, но split(";") всё равно уберёт его, потом strip()
            parts = raw_text.split(';')
        else:
            parts = raw_text.split(';')
        lines = [p.strip() for p in parts if p.strip()]
    # Дополнительно можно проверить, что каждая строка соответствует формату "Фамилия И"
    # Но пока просто возвращаем как есть
    return lines

async def save_and_notify(message: types.Message, state: FSMContext, soldiers):
    user_id = message.from_user.id
    user = get_user(user_id)
    username = message.from_user.username or f"user_{user_id}"
    file_path = save_soldier_list_to_file(user_id, username, soldiers)
    await state.clear()
    await message.answer(f"Список сохранён в файл. Всего рядовых: {len(soldiers)}")

# ---------------------------- Проверка списков ----------------------------
@dp.message(F.text == "🔍 Проверить списки")
async def check_list_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    soldiers = read_soldier_list_from_file(user_id)
    if not soldiers:
        await message.answer("Сначала загрузите список рядовых через меню.")
        return
    await state.set_state(CheckListState.waiting_for_input)
    await message.answer("Отправьте текст или файл со списками для проверки наличия рядовых.")

@dp.message(F.document, StateFilter(CheckListState.waiting_for_input))
async def check_file(message: types.Message, state: FSMContext):
    document = message.document
    if not document.file_name.endswith('.txt'):
        await message.answer("Пожалуйста, отправьте текстовый файл (.txt).")
        return
    file = await bot.get_file(document.file_id)
    file_content = await bot.download_file(file.file_path)
    text = file_content.read().decode('utf-8')
    await perform_check(message, text, state)

@dp.message(F.text, StateFilter(CheckListState.waiting_for_input))
async def check_text(message: types.Message, state: FSMContext):
    await perform_check(message, message.text, state)

async def perform_check(message: types.Message, text: str, state: FSMContext):
    user_id = message.from_user.id
    soldiers = read_soldier_list_from_file(user_id)
    text_lower = text.lower()
    found = []
    for s in soldiers:
        if s.lower() in text_lower:
            found.append(s)
    if found:
        result = "Найденные рядовые:\n" + "\n".join(f"{i+1}. {name}" for i, name in enumerate(found))
    else:
        result = "Никого не найдено."
    await state.clear()
    await message.answer(result)

# ---------------------------- Отзыв ----------------------------
@dp.message(F.text == "📝 Отзыв")
async def feedback_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    await state.set_state(FeedbackState.waiting_for_feedback)
    await message.answer("Напишите ваш отзыв и, при желании, прикрепите фото.")

@dp.message(F.text | F.photo, StateFilter(FeedbackState.waiting_for_feedback))
async def feedback_receive(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    full_name = user[2] if user else "Неизвестно"
    username = message.from_user.username or f"id{user_id}"
    
    caption = f"📬 Отзыв от {full_name} (@{username}):\n\n"
    if message.text:
        caption += message.text
    elif message.caption:
        caption += message.caption
    else:
        caption += "Пустой отзыв."
    
    if message.photo:
        photo = message.photo[-1]
        await bot.send_photo(ADMIN_ID, photo.file_id, caption=caption)
    else:
        await bot.send_message(ADMIN_ID, caption)
    
    await message.answer("Спасибо! Ваш отзыв отправлен администратору.")
    await state.clear()

# ---------------------------- Админ-панель ----------------------------
@dp.message(F.text == "🛠 Админ-панель")
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await message.answer("Админ-панель", reply_markup=admin_panel_keyboard())

@dp.callback_query(lambda c: c.data == "admin_list_users")
async def admin_list_users(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    users = get_all_users()
    if not users:
        await callback.message.edit_text("Нет зарегистрированных пользователей.")
        return
    text = "Зарегистрированные пользователи:\n"
    for uid, uname, fname, blocked in users:
        uname_display = f"@{uname}" if uname else f"id{uid}"
        status = "🔴 заблокирован" if blocked else "🟢 активен"
        text += f"• {fname} ({uname_display}) - {status}\n"
    await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())

@dp.callback_query(lambda c: c.data == "admin_block_user")
async def admin_block_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.set_state(AdminState.waiting_for_username_to_block)
    await callback.message.edit_text("Введите username пользователя для блокировки (без @):")

@dp.callback_query(lambda c: c.data == "admin_unblock_user")
async def admin_unblock_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.set_state(AdminState.waiting_for_username_to_unblock)
    await callback.message.edit_text("Введите username пользователя для разблокировки (без @):")

@dp.message(AdminState.waiting_for_username_to_block)
async def admin_block_user_process(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    update_block_status(username, block=True)
    await message.answer(f"Пользователь @{username} заблокирован.")
    await state.clear()

@dp.message(AdminState.waiting_for_username_to_unblock)
async def admin_unblock_user_process(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    update_block_status(username, block=False)
    await message.answer(f"Пользователь @{username} разблокирован.")
    await state.clear()

@dp.callback_query(lambda c: c.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=build_main_menu_keyboard(is_admin=True)
    )

# ---------------------------- Запуск бота ----------------------------
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())