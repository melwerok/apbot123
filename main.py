import asyncio
import logging
import sqlite3
import json
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberOwner, ChatMemberAdministrator
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
    # Таблица пользователей (личные списки)
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
    # Таблица групповых списков
    cur.execute("""
        CREATE TABLE IF NOT EXISTS group_lists (
            group_id INTEGER PRIMARY KEY,
            list_file TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

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

# ---------------------------- Работа с пользователями ----------------------------
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
    return user and user[3] == 1

def update_block_status(username, block: bool):
    db_execute("UPDATE users SET is_blocked = ? WHERE username = ?", (1 if block else 0, username))

def get_all_users():
    """Возвращает список всех пользователей (user_id, username, full_name, is_blocked)"""
    return db_execute("SELECT user_id, username, full_name, is_blocked FROM users", fetchall=True)

def get_active_users():
    """Возвращает список ID активных пользователей (не заблокированных)"""
    users = db_execute("SELECT user_id FROM users WHERE is_blocked = 0", fetchall=True)
    return [row[0] for row in users] if users else []

def update_last_list_file(user_id, file_path):
    db_execute("UPDATE users SET last_list_file = ? WHERE user_id = ?", (file_path, user_id))

def get_last_list_file(user_id):
    user = get_user(user_id)
    return user[4] if user else None

# ---------------------------- Работа с файлами личных списков ----------------------------
SOLDIER_LISTS_DIR = "soldier_lists"
os.makedirs(SOLDIER_LISTS_DIR, exist_ok=True)

def extract_surname_initial(text: str) -> str | None:
    words = text.strip().split()
    if len(words) < 2:
        return None
    surname = words[0].capitalize()
    initial = words[1][0].upper()
    return f"{surname} {initial}"

def save_soldier_list_to_file(user_id, username, soldiers_list):
    old_file = get_last_list_file(user_id)
    if old_file and os.path.exists(old_file):
        try:
            os.remove(old_file)
        except Exception as e:
            logging.error(f"Не удалось удалить старый файл {old_file}: {e}")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_username = username.replace("@", "").replace("/", "_") if username else f"user_{user_id}"
    filename = f"{safe_username}_{timestamp}.txt"
    file_path = os.path.join(SOLDIER_LISTS_DIR, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        for soldier in soldiers_list:
            f.write(soldier + "\n")

    update_last_list_file(user_id, file_path)
    return file_path

def read_soldier_list_from_file(user_id):
    file_path = get_last_list_file(user_id)
    if not file_path or not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

# ---------------------------- Работа с групповыми списками ----------------------------
GROUP_LISTS_DIR = "group_lists"
os.makedirs(GROUP_LISTS_DIR, exist_ok=True)

def get_group_list_file(group_id: int) -> str | None:
    res = db_execute("SELECT list_file FROM group_lists WHERE group_id = ?", (group_id,), fetchone=True)
    return res[0] if res else None

def save_group_list_to_file(group_id: int, soldiers_list: list, admin_usernames: list[str]) -> str:
    old_file = get_group_list_file(group_id)
    if old_file and os.path.exists(old_file):
        try:
            os.remove(old_file)
        except Exception as e:
            logging.error(f"Не удалось удалить старый групповой файл {old_file}: {e}")

    admin_part = "_".join(admin_usernames[:3])
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{admin_part}_group_{timestamp}.txt"
    file_path = os.path.join(GROUP_LISTS_DIR, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        for soldier in soldiers_list:
            f.write(soldier + "\n")

    db_execute(
        "INSERT OR REPLACE INTO group_lists (group_id, list_file) VALUES (?, ?)",
        (group_id, file_path)
    )
    return file_path

def read_group_list_from_file(group_id: int) -> list[str]:
    file_path = get_group_list_file(group_id)
    if not file_path or not os.path.exists(file_path):
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

# ---------------------------- Проверка на администратора группы (исправлено) ----------------------------
async def is_group_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
    except Exception as e:
        logging.error(f"Ошибка при проверке администратора группы {chat_id} для пользователя {user_id}: {e}")
        return False

# ---------------------------- Клавиатуры ----------------------------
def build_main_menu_keyboard(is_admin: bool = False):
    kb = [
        [KeyboardButton(text="📥 Загрузить список рядовых")],
        [KeyboardButton(text="🔍 Проверить списки")],
        [KeyboardButton(text="📋 Показать список рядовых")],
        [KeyboardButton(text="📝 Отзыв или поддержка")]  # изменено
    ]
    if is_admin:
        kb.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_panel_keyboard():
    kb = [
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_list_users")],
        [InlineKeyboardButton(text="🔒 Заблокировать пользователя", callback_data="admin_block_user")],
        [InlineKeyboardButton(text="🔓 Разблокировать пользователя", callback_data="admin_unblock_user")],
        [InlineKeyboardButton(text="📨 Написать всем", callback_data="admin_broadcast")],  # новая кнопка
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=[kb])

# ---------------------------- Машины состояний (FSM) ----------------------------
class RegisterState(StatesGroup):
    waiting_for_fullname = State()

class LoadListState(StatesGroup):
    waiting_for_format = State()
    waiting_for_space_after_semicolon = State()
    waiting_for_choice = State()
    waiting_for_file = State()
    waiting_for_text = State()

class CheckListState(StatesGroup):
    waiting_for_input = State()

class FeedbackState(StatesGroup):
    waiting_for_feedback = State()

class AdminState(StatesGroup):
    waiting_for_username_to_block = State()
    waiting_for_username_to_unblock = State()
    waiting_for_broadcast = State()  # новое состояние для рассылки

# ---------------------------- Вспомогательная функция проверки регистрации ----------------------------
async def check_registration(message: types.Message) -> bool:
    """Проверяет, зарегистрирован ли пользователь в личном чате.
       Если нет или заблокирован, отправляет соответствующее сообщение и возвращает False."""
    if message.chat.type != "private":
        return True  # в группах регистрация не требуется
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.answer("Пожалуйста, сначала зарегистрируйтесь, отправив /start")
        return False
    if user[3] == 1:
        await message.answer("Вы заблокированы.")
        return False
    return True

# ---------------------------- Хэндлеры личных сообщений ----------------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return

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
    if message.chat.type != "private":
        return
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

# Личная загрузка списка (с кнопками)
@dp.message(F.text == "📥 Загрузить список рядовых")
async def load_list_start_private(message: types.Message, state: FSMContext):
    if not await check_registration(message):
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Через точку с запятой (;)", callback_data="format_semicolon")],
        [InlineKeyboardButton(text="Через Enter", callback_data="format_enter")]
    ])
    await state.set_state(LoadListState.waiting_for_format)
    await message.answer("В каком формате будет список рядовых?", reply_markup=kb)

# Обработка формата (общая для лички и групп)
@dp.callback_query(lambda c: c.data in ["format_semicolon", "format_enter"], StateFilter(LoadListState.waiting_for_format))
async def process_format(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "format_semicolon":
        await state.set_state(LoadListState.waiting_for_space_after_semicolon)
        await state.update_data(format='semicolon')
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data="space_yes")],
            [InlineKeyboardButton(text="Нет", callback_data="space_no")]
        ])
        await callback.message.edit_text("Есть ли пробел после ';'?", reply_markup=kb)
    else:
        await state.update_data(format='enter', space=False)
        await ask_upload_method(callback.message, state)

@dp.callback_query(lambda c: c.data in ["space_yes", "space_no"], StateFilter(LoadListState.waiting_for_space_after_semicolon))
async def process_space(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    has_space = (callback.data == "space_yes")
    await state.update_data(space=has_space)
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
            "Отправьте файл со списком ФИО рядовых (текстовый файл).\n"
            "Бот автоматически выделит фамилию и первую букву имени."
        )
    else:
        await state.set_state(LoadListState.waiting_for_text)
        await callback.message.edit_text(
            "Введите список ФИО рядовых текстом.\n"
            "Бот автоматически выделит фамилию и первую букву имени."
        )

@dp.message(F.text, StateFilter(LoadListState.waiting_for_text))
async def load_text(message: types.Message, state: FSMContext):
    raw_text = message.text
    data = await state.get_data()
    format_type = data.get('format')
    space = data.get('space', False)
    soldiers = parse_soldier_list(raw_text, format_type, space)
    if not soldiers:
        await message.answer("Не удалось извлечь ни одной записи. Проверьте, что каждая строка содержит фамилию и имя.")
        return

    if message.chat.type == "private":
        if not await check_registration(message):
            await state.clear()
            return
        user_id = message.from_user.id
        user = get_user(user_id)
        username = message.from_user.username or f"user_{user_id}"
        file_path = save_soldier_list_to_file(user_id, username, soldiers)
        await message.answer(f"Личный список сохранён. Всего рядовых: {len(soldiers)}")
    else:
        if not await is_group_admin(message.chat.id, message.from_user.id):
            await message.answer("Только администраторы группы могут загружать списки.")
            await state.clear()
            return
        admins = await bot.get_chat_administrators(message.chat.id)
        admin_usernames = []
        for admin in admins:
            if admin.user.username:
                admin_usernames.append(admin.user.username)
            else:
                admin_usernames.append(f"id{admin.user.id}")
        file_path = save_group_list_to_file(message.chat.id, soldiers, admin_usernames)
        await message.answer(f"Групповой список сохранён. Всего рядовых: {len(soldiers)}")
    await state.clear()

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
        await message.answer("Не удалось извлечь ни одной записи из файла. Проверьте, что каждая строка содержит фамилию и имя.")
        return

    if message.chat.type == "private":
        if not await check_registration(message):
            await state.clear()
            return
        user_id = message.from_user.id
        user = get_user(user_id)
        username = message.from_user.username or f"user_{user_id}"
        file_path = save_soldier_list_to_file(user_id, username, soldiers)
        await message.answer(f"Личный список сохранён. Всего рядовых: {len(soldiers)}")
    else:
        if not await is_group_admin(message.chat.id, message.from_user.id):
            await message.answer("Только администраторы группы могут загружать списки.")
            await state.clear()
            return
        admins = await bot.get_chat_administrators(message.chat.id)
        admin_usernames = []
        for admin in admins:
            if admin.user.username:
                admin_usernames.append(admin.user.username)
            else:
                admin_usernames.append(f"id{admin.user.id}")
        file_path = save_group_list_to_file(message.chat.id, soldiers, admin_usernames)
        await message.answer(f"Групповой список сохранён. Всего рядовых: {len(soldiers)}")
    await state.clear()

def parse_soldier_list(raw_text: str, format_type: str, space: bool) -> list:
    lines = []
    if format_type == 'enter':
        raw_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    elif format_type == 'semicolon':
        parts = raw_text.split(';')
        raw_lines = [p.strip() for p in parts if p.strip()]
    else:
        raw_lines = []
    soldiers = []
    for line in raw_lines:
        extracted = extract_surname_initial(line)
        if extracted:
            soldiers.append(extracted)
    return soldiers

# Личная проверка списков (с кнопкой)
@dp.message(F.text == "🔍 Проверить списки")
async def check_list_start_private(message: types.Message, state: FSMContext):
    if not await check_registration(message):
        return
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

# Показать личный список
@dp.message(F.text == "📋 Показать список рядовых")
async def show_soldier_list_private(message: types.Message):
    if not await check_registration(message):
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    soldiers = read_soldier_list_from_file(user_id)
    if not soldiers:
        await message.answer("Список рядовых пуст. Сначала загрузите список через меню.")
        return
    lines = [f"{i+1}. {name}" for i, name in enumerate(soldiers)]
    result = "Ваш список рядовых:\n" + "\n".join(lines)
    if len(result) > 4096:
        for i in range(0, len(result), 4096):
            await message.answer(result[i:i+4096])
    else:
        await message.answer(result)

# ---------------------------- Команды для групп ----------------------------
@dp.message(Command("addpeople"))
async def cmd_addpeople_group(message: types.Message, state: FSMContext):
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("Эта команда доступна только в группах.")
        return
    if not await is_group_admin(message.chat.id, message.from_user.id):
        await message.answer("Только администраторы группы могут использовать эту команду.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Через точку с запятой (;)", callback_data="format_semicolon")],
        [InlineKeyboardButton(text="Через Enter", callback_data="format_enter")]
    ])
    await state.set_state(LoadListState.waiting_for_format)
    await message.answer("В каком формате будет список рядовых?", reply_markup=kb)

@dp.message(Command("checkpeople"))
async def cmd_checkpeople_group(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("Эта команда доступна только в группах.")
        return
    if not await is_group_admin(message.chat.id, message.from_user.id):
        await message.answer("Только администраторы группы могут просматривать список.")
        return
    soldiers = read_group_list_from_file(message.chat.id)
    if not soldiers:
        await message.answer("Список рядовых для этой группы ещё не загружен.")
        return
    lines = [f"{i+1}. {name}" for i, name in enumerate(soldiers)]
    result = "Список рядовых группы:\n" + "\n".join(lines)
    if len(result) > 4096:
        for i in range(0, len(result), 4096):
            await message.answer(result[i:i+4096])
    else:
        await message.answer(result)

# ---------------------------- Автоматическая проверка сообщений в группах ----------------------------
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def auto_check_group_message(message: types.Message, state: FSMContext):
    # Если у пользователя есть активное состояние — пропускаем, не мешаем
    current_state = await state.get_state()
    if current_state is not None:
        return

    if message.from_user.id == bot.id:
        return
    soldiers = read_group_list_from_file(message.chat.id)
    if not soldiers:
        return

    text = message.text or message.caption
    if not text:
        return

    text_lower = text.lower()
    found = []
    for s in soldiers:
        if s.lower() in text_lower:
            found.append(s)

    if found:
        result = "Найденные рядовые в сообщении:\n" + "\n".join(f"{i+1}. {name}" for i, name in enumerate(found))
        await message.reply(result)

# ---------------------------- Обработка проверки (текст/файл) для личных сообщений ----------------------------
@dp.message(F.document, StateFilter(CheckListState.waiting_for_input))
async def check_file_private(message: types.Message, state: FSMContext):
    if not await check_registration(message):
        await state.clear()
        return
    document = message.document
    if not document.file_name.endswith('.txt'):
        await message.answer("Пожалуйста, отправьте текстовый файл (.txt).")
        return
    file = await bot.get_file(document.file_id)
    file_content = await bot.download_file(file.file_path)
    text = file_content.read().decode('utf-8')
    await perform_check_private(message, text, state)

@dp.message(F.text, StateFilter(CheckListState.waiting_for_input))
async def check_text_private(message: types.Message, state: FSMContext):
    if not await check_registration(message):
        await state.clear()
        return
    await perform_check_private(message, message.text, state)

async def perform_check_private(message: types.Message, text: str, state: FSMContext):
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

# ---------------------------- Отзыв или поддержка (изменено) ----------------------------
@dp.message(F.text == "📝 Отзыв или поддержка")
async def feedback_start_private(message: types.Message, state: FSMContext):
    if not await check_registration(message):
        return
    user_id = message.from_user.id
    if is_blocked(user_id):
        await message.answer("Вы заблокированы.")
        return
    await state.set_state(FeedbackState.waiting_for_feedback)
    await message.answer("Напишите ваш отзыв или вопрос в службу поддержки. Можно прикрепить фото.")

@dp.message(F.text | F.photo, StateFilter(FeedbackState.waiting_for_feedback))
async def feedback_receive_private(message: types.Message, state: FSMContext):
    if not await check_registration(message):
        await state.clear()
        return
    user_id = message.from_user.id
    user = get_user(user_id)
    full_name = user[2] if user else "Неизвестно"
    username = message.from_user.username or f"id{user_id}"

    caption = f"📬 Отзыв/поддержка от {full_name} (@{username}):\n\n"
    if message.text:
        caption += message.text
    elif message.caption:
        caption += message.caption
    else:
        caption += "Пустое сообщение."

    if message.photo:
        photo = message.photo[-1]
        await bot.send_photo(ADMIN_ID, photo.file_id, caption=caption)
    else:
        await bot.send_message(ADMIN_ID, caption)

    await message.answer("Спасибо! Ваше сообщение отправлено администратору.")
    await state.clear()

# ---------------------------- Админ-панель (личная) ----------------------------
@dp.message(F.text == "🛠 Админ-панель")
async def admin_panel_private(message: types.Message):
    if message.chat.type != "private":
        return
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await message.answer("Админ-панель", reply_markup=admin_panel_keyboard())

@dp.callback_query(lambda c: c.data == "admin_list_users")
async def admin_list_users(callback: types.CallbackQuery):
    if callback.message.chat.type != "private" or not is_admin(callback.from_user.id):
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
    if callback.message.chat.type != "private" or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.set_state(AdminState.waiting_for_username_to_block)
    await callback.message.edit_text("Введите username пользователя для блокировки (без @):")

@dp.callback_query(lambda c: c.data == "admin_unblock_user")
async def admin_unblock_user_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private" or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.set_state(AdminState.waiting_for_username_to_unblock)
    await callback.message.edit_text("Введите username пользователя для разблокировки (без @):")

@dp.message(AdminState.waiting_for_username_to_block)
async def admin_block_user_process(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return
    username = message.text.strip().lstrip('@')
    update_block_status(username, block=True)
    await message.answer(f"Пользователь @{username} заблокирован.")
    await state.clear()

@dp.message(AdminState.waiting_for_username_to_unblock)
async def admin_unblock_user_process(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return
    username = message.text.strip().lstrip('@')
    update_block_status(username, block=False)
    await message.answer(f"Пользователь @{username} разблокирован.")
    await state.clear()

# ---------------------------- Рассылка всем пользователям (новый функционал) ----------------------------
@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private" or not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text(
        "Отправьте сообщение для рассылки всем пользователям (можно текст или фото с подписью).\n"
        "Сообщение будет доставлено всем активным пользователям бота."
    )

@dp.message(AdminState.waiting_for_broadcast, F.text | F.photo)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    # Получаем список активных пользователей
    users = get_active_users()
    if not users:
        await message.answer("Нет активных пользователей для рассылки.")
        await state.clear()
        return

    success_count = 0
    fail_count = 0

    # Рассылаем сообщение каждому пользователю
    for uid in users:
        try:
            if message.photo:
                # Отправляем фото с подписью
                await bot.send_photo(
                    uid,
                    message.photo[-1].file_id,
                    caption=message.caption or "Рассылка от администратора"
                )
            else:
                # Отправляем текст
                await bot.send_message(uid, message.text)
            success_count += 1
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {uid}: {e}")
            fail_count += 1

    await message.answer(f"Рассылка завершена.\nУспешно: {success_count}\nНеудачно: {fail_count}")
    await state.clear()

# ---------------------------- Назад в админ-панель ----------------------------
@dp.callback_query(lambda c: c.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if callback.message.chat.type != "private" or not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=build_main_menu_keyboard(is_admin=True)
    )

# ---------------------------- Обработчик любых сообщений в личке от незарегистрированных ----------------------------
@dp.message(F.chat.type == "private")
async def handle_unregistered_private(message: types.Message):
    # Если сообщение не обработано ни одним из предыдущих хэндлеров (в том числе не команда и не состояние)
    # Проверим, зарегистрирован ли пользователь. Если нет — напомним.
    user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.answer("Пожалуйста, сначала зарегистрируйтесь, отправив /start")
        return
    if user[3] == 1:
        await message.answer("Вы заблокированы.")
        return
    # Если дошло сюда, значит пользователь зарегистрирован, но сообщение не подошло ни под один хэндлер.
    # Можно проигнорировать или дать общую подсказку.
    await message.answer("Используйте кнопки меню для навигации.")

# ---------------------------- Установка команд для отображения в меню ----------------------------
async def set_commands():
    # Команды для личных чатов
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Начать работу с ботом")
    ], scope=types.BotCommandScopeAllPrivateChats())

    # Команды для групп
    await bot.set_my_commands([
        types.BotCommand(command="addpeople", description="Загрузить список рядовых (только админы)"),
        types.BotCommand(command="checkpeople", description="Показать список группы (только админы)")
    ], scope=types.BotCommandScopeAllGroupChats())

# ---------------------------- Запуск бота ----------------------------
async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Вебхук успешно удалён")
    except Exception as e:
        logging.error(f"Не удалось удалить вебхук: {e}")
        # Продолжаем запуск, так как это не критично
    await set_commands()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())