
# 1. Импорты:
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from google import genai
from google.genai import types
import random
from datetime import datetime, time, timezone
import pytz
import logging
import json
import os
import tokens # ИЗМЕНЕНО: Импорт файла с токенами

# 2. Настройка логгирования (без изменений)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# 3. Конфигурация API и Модели
# !!! ВАЖНО: Ключи теперь берутся из файла tokens.py !!!
GEMINI_API_KEY = tokens.GEMINI_API_KEY # ИЗМЕНЕНО: Получение ключа из файла
TELEGRAM_TOKEN = tokens.TELEGRAM_TOKEN # ИЗМЕНЕНО: Получение токена из файла

# 4. Инициализация Gemini API клиента
client = None
MODEL = None
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
    # ИЗМЕНЕНО: Устанавливаем модель gemini-2.0-flash
    MODEL = "gemini-2.0-flash"
    logger.info(f"Gemini API клиент инициализирован с моделью {MODEL}.")
except Exception as e:
    logger.error(f"Критическая ошибка инициализации Gemini API: {e}", exc_info=True)
    # Если ключ невалиден или другая проблема, Gemini будет недоступен

# 5. Глобальные переменные / Константы (без изменений)
DB_NAME = "bot.db"
IDIOMS_JSON_FILE = "idioms.json"
THEMES = [] # Будут загружены из JSON

# 6. Настройка базы данных SQLite (без изменений)
conn = None
cursor = None
try:
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    logger.info(f"Подключение к SQLite ({DB_NAME}) установлено.")
    # Создание/Обновление схемы таблиц (idioms, users, user_logs)
    # Таблица 'idioms'
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS idioms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, theme TEXT, idiom TEXT UNIQUE NOT NULL,
            pinyin TEXT, translation TEXT, meaning TEXT, example TEXT
        )""")
    try: cursor.execute("CREATE INDEX IF NOT EXISTS idx_idiom ON idioms(idiom)")
    except sqlite3.OperationalError: pass
    logger.info("Таблица 'idioms' проверена/создана.")
    # Таблица 'users'
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            daily_time TEXT DEFAULT '09:00', dictionary TEXT DEFAULT '',
            practice_correct INTEGER DEFAULT 0, practice_total INTEGER DEFAULT 0
        )""")
    logger.info("Таблица 'users' проверена/создана.")
    user_columns = [("username", "TEXT"), ("first_name", "TEXT"), ("last_name", "TEXT"), ("practice_correct", "INTEGER DEFAULT 0"), ("practice_total", "INTEGER DEFAULT 0"), ("daily_time", "TEXT DEFAULT '09:00'"), ("dictionary", "TEXT DEFAULT ''")]
    for col_name, col_type in user_columns:
        try: cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError: pass
    # Таблица 'user_logs'
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, action_type TEXT, details TEXT,
            FOREIGN KEY (chat_id) REFERENCES users (chat_id)
        )""")
    logger.info("Таблица 'user_logs' проверена/создана.")
    conn.commit()
    logger.info("Изменения схемы БД сохранены.")
except sqlite3.Error as e:
    logger.critical(f"Критическая ошибка при инициализации БД: {e}", exc_info=True)
    conn = cursor = None
except Exception as e:
    logger.critical(f"Неизвестная критическая ошибка при инициализации БД: {e}", exc_info=True)
    conn = cursor = None

# 7. Функция загрузки идиом из JSON (без изменений)
def load_idioms_from_json(db_cursor: sqlite3.Cursor, db_conn: sqlite3.Connection):
    """Загружает или обновляет идиомы в БД из файла IDIOMS_JSON_FILE."""
    if not os.path.exists(IDIOMS_JSON_FILE):
        logger.warning(f"Файл {IDIOMS_JSON_FILE} не найден. Загрузка идиом пропущена.")
        return 0
    try:
        with open(IDIOMS_JSON_FILE, 'r', encoding='utf-8') as f: data = json.load(f)
    except Exception as e:
        logger.error(f"Ошибка при чтении/декодировании {IDIOMS_JSON_FILE}: {e}", exc_info=True)
        return 0

    loaded_count, replaced_count, skipped_count = 0, 0, 0
    global THEMES
    new_themes = set()

    if not isinstance(data, dict):
          logger.error(f"Ошибка: Ожидаемая структура JSON - словарь тем (dict), получен {type(data)}")
          return 0

    for theme, idioms_list in data.items():
        new_themes.add(theme)
        if not isinstance(idioms_list, list):
            logger.warning(f"Ожидался список идиом для темы '{theme}', получен {type(idioms_list)}. Пропускаем.")
            continue
        for idiom_data in idioms_list:
            if not isinstance(idiom_data, dict) or not idiom_data.get("idiom"):
                logger.warning(f"Пропущена некорректная запись в теме '{theme}': {idiom_data}")
                skipped_count += 1
                continue
            try:
                db_cursor.execute("SELECT 1 FROM idioms WHERE idiom = ?", (idiom_data["idiom"],))
                exists = db_cursor.fetchone()
                db_cursor.execute(
                    """INSERT OR REPLACE INTO idioms (theme, idiom, pinyin, translation, meaning, example) VALUES (?, ?, ?, ?, ?, ?)""",
                    (theme, idiom_data.get("idiom"), idiom_data.get("pinyin"), idiom_data.get("translation"), idiom_data.get("meaning"), idiom_data.get("example"))
                )
                if exists: replaced_count += 1
                else: loaded_count += 1
            except sqlite3.Error as e:
                logger.error(f"Ошибка SQLite при загрузке/замене идиомы '{idiom_data.get('idiom')}': {e}")
                skipped_count += 1
            except Exception as e:
                  logger.error(f"Неизвестная ошибка при обработке идиомы {idiom_data}: {e}")
                  skipped_count += 1

    if loaded_count > 0 or replaced_count > 0 or skipped_count > 0:
        try:
            db_conn.commit()
            logger.info(f"Загрузка из {IDIOMS_JSON_FILE}: добавлено {loaded_count}, заменено {replaced_count}, пропущено {skipped_count}.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при коммите изменений после загрузки идиом: {e}")
            return 0
    else: logger.info(f"Загрузка из {IDIOMS_JSON_FILE}: не найдено данных для загрузки/обновления.")

    THEMES = sorted(list(new_themes))
    logger.info(f"Обновлены темы из JSON: {THEMES}")
    return loaded_count + replaced_count

# --- Функции бота ---

# 8. Функция логирования действий пользователя (без изменений)
async def log_user_action(chat_id: int, action_type: str, details: dict = None):
    if not cursor or not conn: return
    details_json = json.dumps(details, ensure_ascii=False, sort_keys=True) if details else None
    try:
        cursor.execute("INSERT INTO user_logs (chat_id, action_type, details) VALUES (?, ?, ?)", (chat_id, action_type, details_json))
        conn.commit()
    except sqlite3.Error as e: logger.error(f"Ошибка записи лога для {chat_id} (Action: {action_type}): {e}")

# 9. Вспомогательная функция для обновления данных пользователя (без изменений)
async def update_user_info(chat_id: int, user: 'telegram.User'):
    if not cursor or not conn or not user: return
    try:
        cursor.execute(
            """INSERT INTO users (chat_id, username, first_name, last_name) VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name""",
            (chat_id, user.username, user.first_name, user.last_name)
        )
        conn.commit()
    except sqlite3.Error as e: logger.error(f"Ошибка обновления/вставки пользователя {chat_id}: {e}")

# 10. Функция отображения главного меню (`show_main_menu`) (без изменений)
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id and user:
        await update_user_info(chat_id, user)
        await log_user_action(chat_id, "command_start")
    keyboard = [
        [InlineKeyboardButton("📚 Идиома дня", callback_data="idiom")],
        [InlineKeyboardButton("🏷 Тематические идиомы", callback_data="theme")],
        [InlineKeyboardButton("🎓 Интерактивная практика", callback_data="practice")],
        [InlineKeyboardButton("📖 Личный словарь", callback_data="dictionary")],
        [InlineKeyboardButton("❓ Свободный режим", callback_data="free_mode")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🇨🇳 *Бот для изучения китайских идиом*\nВыбери режим:"
    message_to_edit = update.callback_query.message if update.callback_query else None
    current_message = update.message
    if message_to_edit:
        try: await message_to_edit.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение в show_main_menu (callback): {e}")
            if chat_id: await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")
    elif current_message: await current_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif chat_id: await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="Markdown")

# 11. Обработчик нажатий на Inline-кнопки (`button_handler`)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message or not query.data:
        if query: await query.answer()
        return
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
    message = query.message
    log_details = {"callback_data": data}
    await log_user_action(chat_id, "button_press", log_details)

    # Сброс состояний ожидания ввода
    keys_to_pop = ['awaiting_idiom', 'awaiting_time', 'practice_type', 'current_practice_idiom_data']
    for key in keys_to_pop: context.user_data.pop(key, None)

    # Сбрасываем режимы мульти-вопросов, если нажата НЕ их кнопка выхода
    if data != 'exit_asking_mode':
        context.user_data.pop('asking_about_idiom', None)
        # context.user_data.pop('asking_mode_history', None) # Очистка истории, если нужно (пока не делаем)
    if data != 'exit_free_mode': # ИЗМЕНЕНО: Проверяем кнопку выхода из нового режима
        context.user_data.pop('in_free_mode_conversation', None)
        context.user_data.pop('free_mode_history', None) # ИЗМЕНЕНО: Очистка истории при выходе НЕ через кнопку выхода

    # Также сбрасываем старый флаг ОЖИДАНИЯ вопроса свободного режима (на всякий случай)
    context.user_data.pop('awaiting_free_mode', None)

    try:
        # Маршрутизация
        if data == "idiom": await idiom(message, context)
        elif data == "theme": await theme(message, context)
        elif data == "practice": await practice(message, context)
        elif data == "dictionary": await dictionary(message, context)
        elif data == "free_mode": await start_free_mode_conversation(message, context)
        elif data == "settings": await settings(message, context)
        elif data.startswith("theme_"): await theme_selected(message, context, data.split("_", 1)[1])
        elif data.startswith("practice_"): await practice_selected(message, context, data.split("_", 1)[1])
        elif data == "add_idiom": await add_idiom_prompt(message, context)
        elif data.startswith("confirm_add_"): await confirm_add_idiom(message, context, data.split("confirm_add_", 1)[1])
        elif data == "view_dictionary": await view_dictionary(message, context)
        elif data == "repeat_idioms": await repeat_idioms(message, context)
        elif data == "set_time": await set_time_prompt(message, context)
        elif data.startswith("question_"): await start_asking_mode(message, context, data.split("question_", 1)[1])
        elif data == 'exit_asking_mode':
              context.user_data.pop('asking_about_idiom', None)
              # context.user_data.pop('asking_mode_history', None) # Очистка истории, если нужно
              await dictionary(message, context) # Возврат в словарь (или куда логичнее)
        elif data == 'exit_free_mode': # НОВЫЙ обработчик
              context.user_data.pop('in_free_mode_conversation', None)
              context.user_data.pop('free_mode_history', None) # ИЗМЕНЕНО: Очищаем историю при выходе
              await log_user_action(chat_id, "free_mode_exit_button")
              await show_main_menu(update, context) # Возврат в главное меню
        elif data.startswith("delete_"): await confirm_delete_idiom(message, context, data.split("delete_", 1)[1])
        elif data.startswith("confirm_delete_"): await delete_idiom(message, context, data.split("confirm_delete_", 1)[1])
        elif data == "back": await show_main_menu(update, context)
        elif data == "back_to_dictionary": await dictionary(message, context)
        else:
            logger.warning(f"Неизвестный callback_data: {data} от {chat_id}")
            await message.edit_text("Произошла ошибка: неизвестная команда.", reply_markup=back_button())
    except Exception as e:
        logger.error(f"Ошибка в button_handler (data: {data}, chat: {chat_id}): {e}", exc_info=True)
        try: await message.edit_text("Произошла внутренняя ошибка.", reply_markup=back_button())
        except Exception as edit_e: logger.error(f"Не удалось отредактировать сообщение об ошибке: {edit_e}")

# 12. Функции "Идиома дня", "Тематические идиомы", "Практика", "Словарь" (без изменений в логике)
async def idiom(message, context: ContextTypes.DEFAULT_TYPE):
    if not cursor: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_button()); return
    try: cursor.execute("SELECT * FROM idioms ORDER BY RANDOM() LIMIT 1"); result = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite в idiom: {e}"); await message.edit_text("Ошибка.", reply_markup=back_button()); return
    if result:
        idiom_text = result['idiom']
        msg_text = format_idiom_details(result)
        keyboard = [[InlineKeyboardButton("❓ Задать вопрос", callback_data=f"question_{idiom_text}")], [InlineKeyboardButton("➕ Добавить в словарь", callback_data=f"confirm_add_{idiom_text}")], [InlineKeyboardButton("⬅️ Назад", callback_data="back")]]
        await message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await message.edit_text("❌ База идиом пуста!", reply_markup=back_button())

async def theme(message, context: ContextTypes.DEFAULT_TYPE):
    if not THEMES: await message.edit_text("Ошибка: Темы идиом не загружены.", reply_markup=back_button()); return
    keyboard = [[InlineKeyboardButton(theme.capitalize(), callback_data=f"theme_{theme}")] for theme in THEMES]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    await message.edit_text("🏷 Выбери тему:", reply_markup=InlineKeyboardMarkup(keyboard))

async def theme_selected(message, context: ContextTypes.DEFAULT_TYPE, theme_name: str):
    if not cursor: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_button()); return
    try: cursor.execute("SELECT * FROM idioms WHERE theme = ? ORDER BY RANDOM() LIMIT 1", (theme_name,)); result = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite в theme_selected (тема: {theme_name}): {e}"); await message.edit_text("Ошибка.", reply_markup=back_button()); return
    if result:
        idiom_text = result['idiom']
        msg_text = f"🏷 *Идиома по теме '{theme_name.capitalize()}'*:\n\n" + format_idiom_details(result)
        keyboard = [[InlineKeyboardButton(f"➕ '{idiom_text}' в словарь", callback_data=f"confirm_add_{idiom_text}")], [InlineKeyboardButton(f"❓ Вопрос про '{idiom_text}'", callback_data=f"question_{idiom_text}")], [InlineKeyboardButton("🔄 Другая идиома по теме", callback_data=f"theme_{theme_name}")], [InlineKeyboardButton("⬅️ Назад к темам", callback_data="theme")], [InlineKeyboardButton("⬅️ Главное меню", callback_data="back")]]
        await message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await message.edit_text(f"❌ Идиом по теме '{theme_name.capitalize()}' не найдено!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к темам", callback_data="theme")]]))

async def practice(message, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Перевод", callback_data="practice_translate")], [InlineKeyboardButton("Пример предложения", callback_data="practice_example")], [InlineKeyboardButton("⬅️ Назад", callback_data="back")]]
    await message.edit_text("🎓 Выбери тип задания:", reply_markup=InlineKeyboardMarkup(keyboard))

async def practice_selected(message, context: ContextTypes.DEFAULT_TYPE, practice_type: str):
    if not cursor: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_button()); return
    try: cursor.execute("SELECT * FROM idioms ORDER BY RANDOM() LIMIT 1"); result = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite в practice_selected: {e}"); await message.edit_text("Ошибка.", reply_markup=back_button()); return
    if result:
        idiom_data = dict(result)
        context.user_data["current_practice_idiom_data"] = idiom_data
        context.user_data["practice_type"] = practice_type
        prompt_text = f"🎓 *Переведи идиому*: {idiom_data['idiom']} ({idiom_data['pinyin']})" if practice_type == "translate" else f"🎓 *Напиши пример предложения с идиомой*: {idiom_data['idiom']}"
        await message.edit_text(prompt_text, reply_markup=back_button(), parse_mode="Markdown")
    else: await message.edit_text("❌ База идиом пуста!", reply_markup=back_button())

async def dictionary(message, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("➕ Добавить идиому", callback_data="add_idiom")], [InlineKeyboardButton("📋 Просмотреть словарь", callback_data="view_dictionary")], [InlineKeyboardButton("🔄 Повторить идиомы", callback_data="repeat_idioms")], [InlineKeyboardButton("⬅️ Назад", callback_data="back")]]
    await message.edit_text("📖 Личный словарь:", reply_markup=InlineKeyboardMarkup(keyboard))

async def add_idiom_prompt(message, context: ContextTypes.DEFAULT_TYPE):
    await message.edit_text("➕ Напиши идиому для добавления:", reply_markup=back_to_dictionary_button())
    context.user_data["awaiting_idiom"] = True

async def confirm_add_idiom(message, context: ContextTypes.DEFAULT_TYPE, idiom_str: str):
    chat_id = message.chat_id; idiom_str = idiom_str.strip()
    if not idiom_str: await message.edit_text("❌ Вы не ввели идиому.", reply_markup=back_to_dictionary_button()); return
    if not cursor or not conn: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_to_dictionary_button()); return
    try:
        cursor.execute("SELECT 1 FROM idioms WHERE idiom = ?", (idiom_str,)); exists_in_main_db = cursor.fetchone()
        if not exists_in_main_db: await message.edit_text(f"🤔 Идиома '{idiom_str}' не найдена в базе.", reply_markup=back_to_dictionary_button()); return
        cursor.execute("SELECT dictionary FROM users WHERE chat_id = ?", (chat_id,)); user_data = cursor.fetchone()
        user_dictionary = user_data['dictionary'].split(';') if user_data and user_data['dictionary'] else []
        user_dictionary = [item for item in user_dictionary if item]
        if idiom_str not in user_dictionary:
            user_dictionary.append(idiom_str); new_dictionary_str = ";".join(user_dictionary)
            cursor.execute("UPDATE users SET dictionary = ? WHERE chat_id = ?", (new_dictionary_str, chat_id)); conn.commit()
            await log_user_action(chat_id, "dictionary_add", {"idiom": idiom_str})
            await message.edit_text(f"✅ Идиома '{idiom_str}' добавлена!", reply_markup=back_to_dictionary_button())
        else: await message.edit_text(f"ℹ️ Идиома '{idiom_str}' уже в словаре!", reply_markup=back_to_dictionary_button())
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite в confirm_add_idiom для {chat_id}: {e}"); await message.edit_text("Ошибка.", reply_markup=back_to_dictionary_button())

async def view_dictionary(message, context: ContextTypes.DEFAULT_TYPE):
    chat_id = message.chat_id
    if not cursor: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_to_dictionary_button()); return
    try: cursor.execute("SELECT dictionary FROM users WHERE chat_id = ?", (chat_id,)); user_data = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite при получении словаря {chat_id}: {e}"); await message.edit_text("Ошибка.", reply_markup=back_to_dictionary_button()); return
    user_dictionary = user_data['dictionary'].split(';') if user_data and user_data['dictionary'] else []
    user_dictionary = [item for item in user_dictionary if item]
    if user_dictionary:
        msg_text = "📖 *Твой словарь*:\n\n"; keyboard_buttons = []; idioms_details = {}
        try:
            placeholders = ','.join('?' * len(user_dictionary))
            cursor.execute(f"SELECT idiom, translation FROM idioms WHERE idiom IN ({placeholders})", user_dictionary); rows = cursor.fetchall()
            for row in rows: idioms_details[row['idiom']] = row['translation']
        except sqlite3.Error as e: logger.error(f"Ошибка SQLite при получении переводов для словаря {chat_id}: {e}")
        for item in user_dictionary:
            translation = idioms_details.get(item, "?")
            msg_text += f"- {item} ({translation})\n"
            keyboard_buttons.append([InlineKeyboardButton(f"❓ Вопрос про '{item}'", callback_data=f"question_{item}"), InlineKeyboardButton(f"🗑 Удалить '{item}'", callback_data=f"delete_{item}")])
        keyboard_buttons.append([InlineKeyboardButton("⬅️ Назад в словарь", callback_data="back_to_dictionary")])
        await message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard_buttons), parse_mode="Markdown")
    else: await message.edit_text("📖 Словарь пуст!", reply_markup=back_to_dictionary_button())

async def repeat_idioms(message, context: ContextTypes.DEFAULT_TYPE):
    chat_id = message.chat_id
    if not cursor: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_to_dictionary_button()); return
    try: cursor.execute("SELECT dictionary FROM users WHERE chat_id = ?", (chat_id,)); user_data = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite при получении словаря для повтора ({chat_id}): {e}"); await message.edit_text("Ошибка.", reply_markup=back_to_dictionary_button()); return
    user_dictionary = user_data['dictionary'].split(';') if user_data and user_data['dictionary'] else []
    user_dictionary = [item for item in user_dictionary if item]
    if user_dictionary:
        idiom_text = random.choice(user_dictionary)
        try: cursor.execute("SELECT * FROM idioms WHERE idiom = ?", (idiom_text,)); idiom_details = cursor.fetchone()
        except sqlite3.Error as e: logger.error(f"Ошибка получения деталей для повтора {idiom_text}: {e}"); idiom_details = None
        msg_text = "🔄 *Повторяем идиому*:\n\n" + format_idiom_details(idiom_details) if idiom_details else f"🔄 *Повторяем*:\n{idiom_text}\n_(Детали не найдены)_"
        keyboard = [[InlineKeyboardButton("❓ Задать вопрос", callback_data=f"question_{idiom_text}")], [InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{idiom_text}")], [InlineKeyboardButton("➡️ Следующая", callback_data="repeat_idioms")], [InlineKeyboardButton("⬅️ Назад в словарь", callback_data="back_to_dictionary")]]
        await message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else: await message.edit_text("📖 Словарь пуст!", reply_markup=back_to_dictionary_button())

# Режим мульти-вопросов по идиоме (start_asking_mode) - без изменений
# При желании, можно сюда тоже добавить сохранение истории, аналогично свободному режиму
async def start_asking_mode(message, context: ContextTypes.DEFAULT_TYPE, idiom: str):
    context.user_data['asking_about_idiom'] = idiom
    # context.user_data['asking_mode_history'] = [] # Инициализация истории, если нужно
    logger.info(f"Chat {message.chat_id} entered asking mode for idiom: {idiom}")
    exit_button = exit_asking_button()
    await message.edit_text(
        f"❓ Вы вошли в режим вопросов по идиоме: *{idiom}*.\n"
        f"Напишите свой вопрос. Чтобы выйти, нажмите кнопку ниже или напишите 'выйти'.",
        reply_markup=exit_button, parse_mode="Markdown"
    )

# Функции удаления идиомы (confirm_delete_idiom, delete_idiom) - без изменений в логике
async def confirm_delete_idiom(message, context: ContextTypes.DEFAULT_TYPE, idiom: str):
    keyboard = [[InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{idiom}")], [InlineKeyboardButton("❌ Отмена", callback_data="back_to_dictionary")]]
    await message.edit_text(f"🗑 Удалить идиому '{idiom}' из словаря?", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_idiom(message, context: ContextTypes.DEFAULT_TYPE, idiom: str):
    chat_id = message.chat_id
    if not cursor or not conn: await message.edit_text("Ошибка: БД недоступна.", reply_markup=back_to_dictionary_button()); return
    try:
        cursor.execute("SELECT dictionary FROM users WHERE chat_id = ?", (chat_id,)); user_data = cursor.fetchone()
        user_dictionary = user_data['dictionary'].split(';') if user_data and user_data['dictionary'] else []
        user_dictionary = [item for item in user_dictionary if item]
        if idiom in user_dictionary:
            user_dictionary.remove(idiom); new_dictionary_str = ";".join(user_dictionary)
            cursor.execute("UPDATE users SET dictionary = ? WHERE chat_id = ?", (new_dictionary_str, chat_id)); conn.commit()
            await log_user_action(chat_id, "dictionary_delete", {"idiom": idiom})
            await message.edit_text(f"✅ Идиома '{idiom}' удалена!", reply_markup=back_to_dictionary_button())
        else: await message.edit_text(f"❌ Идиома '{idiom}' не найдена в словаре!", reply_markup=back_to_dictionary_button())
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite в delete_idiom для {chat_id}: {e}"); await message.edit_text("Ошибка.", reply_markup=back_to_dictionary_button())

# 13. Функции для режима "Свободный режим" - ИЗМЕНЕНО: Добавлена инициализация истории
async def start_free_mode_conversation(message, context: ContextTypes.DEFAULT_TYPE):
    """Инициирует режим 'свободного диалога'."""
    chat_id = message.chat_id
    context.user_data['in_free_mode_conversation'] = True # Устанавливаем флаг режима
    context.user_data['free_mode_history'] = [] # ИЗМЕНЕНО: Инициализируем пустую историю
    logger.info(f"Chat {chat_id} entered free mode conversation.")
    await log_user_action(chat_id, "free_mode_start")

    # Можно добавить системный промпт в начало истории, если нужно
    # initial_system_prompt = "Ты ИИ-ассистент по китайскому языку. Отвечай кратко и по существу на русском."
    # context.user_data['free_mode_history'].append({'role': 'system', 'parts': [{'text': initial_system_prompt}]}) # Формат может отличаться

    await message.edit_text(
        "❓ Вы вошли в свободный режим.\n"
        "Задавайте вопросы о китайском языке (идиомы, слова, грамматика...). "
        "Чтобы выйти, нажмите кнопку ниже или напишите 'выйти'.",
        reply_markup=exit_free_mode_button()
    )

# 14. Функции для режима "Настройки" (`settings`, `set_time_prompt`) - без изменений в логике
async def settings(message, context: ContextTypes.DEFAULT_TYPE):
    chat_id = message.chat_id; current_time = "09:00"; correct = 0; total = 0; accuracy = 0.0
    if cursor:
        try:
            cursor.execute("SELECT daily_time, practice_correct, practice_total FROM users WHERE chat_id = ?", (chat_id,)); result = cursor.fetchone()
            if result:
                current_time = result['daily_time'] or "09:00"; correct = result['practice_correct'] or 0; total = result['practice_total'] or 0
                if total > 0: accuracy = (correct / total * 100)
        except sqlite3.Error as e: logger.error(f"Ошибка SQLite в settings для {chat_id}: {e}")
    keyboard = [[InlineKeyboardButton(f"⏰ Задать время рассылки ({current_time} UTC)", callback_data="set_time")], [InlineKeyboardButton("⬅️ Назад", callback_data="back")]]
    msg_text = f"⚙️ *Настройки*\n⏰ Время рассылки: {current_time} (UTC)\n📊 Статистика практики: {correct}/{total} ({accuracy:.1f}%)"
    await message.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def set_time_prompt(message, context: ContextTypes.DEFAULT_TYPE):
    await message.edit_text("⏰ Укажи время для 'Идиомы дня' в ЧЧ:ММ (UTC):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в настройки", callback_data="settings")]]))
    context.user_data["awaiting_time"] = True

# 15. Обработчик текстовых сообщений (`handle_message`) - ИЗМЕНЕНО: Логика свободного режима с историей
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return

    chat_id = update.message.chat_id
    text = update.message.text.strip()
    user_data = context.user_data
    user = update.effective_user
    if user: await update_user_info(chat_id, user)

    exit_commands = ['выйти', 'хватит', 'stop', 'exit', '/stop', '/выход'] # Добавил /выход

    # 0. Проверка на выход из режима мульти-вопроса по идиоме
    current_asking_idiom = user_data.get('asking_about_idiom')
    if current_asking_idiom and text.lower() in exit_commands:
        user_data.pop('asking_about_idiom', None)
        # user_data.pop('asking_mode_history', None) # Очистка истории, если была
        logger.info(f"Chat {chat_id} exited asking mode for idiom: {current_asking_idiom} by command")
        await log_user_action(chat_id, "asking_mode_exit_cmd", {"idiom": current_asking_idiom})
        await update.message.reply_text("Вы вышли из режима вопросов по идиоме.", reply_markup=back_to_dictionary_button())
        return

    # 1. Проверка на выход из режима свободного диалога
    in_free_mode = user_data.get('in_free_mode_conversation')
    if in_free_mode and text.lower() in exit_commands:
        user_data.pop('in_free_mode_conversation', None)
        user_data.pop('free_mode_history', None) # ИЗМЕНЕНО: Очищаем историю при выходе
        logger.info(f"Chat {chat_id} exited free mode conversation by command.")
        await log_user_action(chat_id, "free_mode_exit_cmd")
        await update.message.reply_text("Вы вышли из свободного режима.", reply_markup=back_button())
        return

    # 2. Режим мульти-вопросов по идиоме
    elif current_asking_idiom:
        question = text; await log_user_action(chat_id, "asking_mode_question", {"idiom": current_asking_idiom, "question": question})
        if not client or not MODEL: await update.message.reply_text("❌ Сервис ответов недоступен.", reply_markup=exit_asking_button()); return

        # --- Логика для режима вопросов по идиоме (без истории пока) ---
        prompt = f"Ты ассистент по китайскому языку. Вопрос об идиоме '{current_asking_idiom}': '{question}'. Если вопрос релевантен, ответь КРАТКО и по существу на русском. Если нет, скажи 'Этот вопрос не об идиоме {current_asking_idiom}. Спросите о ней или используйте Свободный режим.'"
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            # Используем измененную модель MODEL
            response = client.models.generate_content(model=MODEL, contents=[prompt])
            await update.message.reply_text(response.text, reply_markup=exit_asking_button())
        except Exception as e: logger.error(f"Ошибка Gemini (asking_mode): {e}", exc_info=True); await update.message.reply_text(f"❌ Ошибка: {str(e)}", reply_markup=exit_asking_button())
        # --- Конец логики для режима вопросов по идиоме ---


    # 3. Режим свободного диалога - НОВЫЙ БЛОК с ИСТОРИЕЙ
    elif in_free_mode:
        user_message = text
        await log_user_action(chat_id, "free_mode_question", {"question": user_message})
        if not client or not MODEL:
            await update.message.reply_text("❌ Сервис ответов недоступен.", reply_markup=exit_free_mode_button())
            return

        # Получаем текущую историю или инициализируем пустую
        history = context.user_data.get('free_mode_history', [])

        # Добавляем новое сообщение пользователя в историю
        # Формат: список словарей с ключами 'role' и 'parts'
        history.append({'role': 'user', 'parts': [{'text': user_message}]})

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")

            # --- Вызов Gemini API с передачей всей истории ---
            # 'contents' должен быть списком словарей, представляющих историю чата
            # Используем измененную модель MODEL
            response = client.models.generate_content(model=MODEL, contents=history)
            reply_text = response.text
            # --- Конец вызова Gemini API ---

            # Добавляем ответ модели в историю
            history.append({'role': 'model', 'parts': [{'text': reply_text}]})

            # Ограничиваем размер истории, если нужно (например, последние N сообщений)
            # MAX_HISTORY_LEN = 10 # Пример
            # if len(history) > MAX_HISTORY_LEN:
            #     history = history[-MAX_HISTORY_LEN:] # Оставляем последние N

            # Сохраняем обновленную историю обратно в user_data
            context.user_data['free_mode_history'] = history

            # Отправляем ответ пользователю и кнопку выхода
            await update.message.reply_text(reply_text, reply_markup=exit_free_mode_button())

        except Exception as e:
            logger.error(f"Ошибка Gemini (free_mode_conversation): {e}", exc_info=True)
            # Важно: не добавляем ошибочный ответ в историю
            await update.message.reply_text(f"❌ Ошибка при генерации ответа: {str(e)}", reply_markup=exit_free_mode_button())
        # --- Конец НОВОГО БЛОКА ---

    # 4. Ожидание ввода идиомы для добавления (без изменений)
    elif user_data.get("awaiting_idiom"):
        user_data.pop("awaiting_idiom", None); await log_user_action(chat_id, "add_idiom_input", {"text": text})
        await confirm_add_idiom(update.message, context, text)

    # 5. Ожидание ввода времени для рассылки (без изменений)
    elif user_data.get("awaiting_time"):
        user_data.pop("awaiting_time", None); await log_user_action(chat_id, "set_time_input", {"text": text})
        try:
            valid_time = datetime.strptime(text, "%H:%M").strftime("%H:%M")
            if cursor and conn: cursor.execute("UPDATE users SET daily_time = ? WHERE chat_id = ?", (valid_time, chat_id)); conn.commit()
            else: raise sqlite3.Error("DB not available")
            await update.message.reply_text(f"✅ Время рассылки: {valid_time} UTC", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в настройки", callback_data="settings")]]))
        except ValueError: await update.message.reply_text("❌ Неверный формат! (ЧЧ:ММ)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в настройки", callback_data="settings")]]))
        except sqlite3.Error as e: logger.error(f"Ошибка SQLite при обновлении времени {chat_id}: {e}"); await update.message.reply_text("Ошибка сохранения.", reply_markup=back_button())

    # 6. Ожидание ответа на задание в режиме практики (без изменений в логике, но использует новую модель)
    elif user_data.get("practice_type"):
        practice_type = user_data.pop("practice_type", None); idiom_data = user_data.pop("current_practice_idiom_data", None)
        if not idiom_data or not practice_type: logger.warning(f"Нет данных практики {chat_id}"); await update.message.reply_text("❌ Ошибка практики.", reply_markup=back_button()); return
        await log_user_action(chat_id, f"practice_answer_{practice_type}", {"idiom": idiom_data.get('idiom'), "answer": text})
        if not client or not MODEL: await update.message.reply_text("❌ Сервис проверки недоступен.", reply_markup=back_button()); return
        task_desc = "перевод" if practice_type == "translate" else "составление примера"
        prompt = (f"Проверь задание по идиоме: {idiom_data.get('idiom')} ({idiom_data.get('pinyin')}). Перевод: {idiom_data.get('translation')}. Задание: {task_desc}. Ответ: '{text}'.\n"
                  f"Оцени КРАТКО. Если верно, начни с '✅ Верно!'. Если нет, с '❌ Не совсем верно.', дай пояснение.\n"
                  f"В КОНЦЕ добавь ТОЛЬКО '[correct]' или '[incorrect]'.")
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            # Используем измененную модель MODEL
            response = client.models.generate_content(model=MODEL, contents=[prompt]); reply_text_raw = response.text
            is_correct = False; reply_text_clean = reply_text_raw
            if reply_text_raw.rstrip().endswith("[correct]"): is_correct = True; reply_text_clean = reply_text_raw.rsplit("[correct]", 1)[0].strip()
            elif reply_text_raw.rstrip().endswith("[incorrect]"): is_correct = False; reply_text_clean = reply_text_raw.rsplit("[incorrect]", 1)[0].strip()
            else: logger.warning(f"Нет маркера Gemini в практике {chat_id}: '{reply_text_raw}'"); reply_text_clean += "\n_(Точность не определена)_"; is_correct = False # Дефолт - неверно
            if cursor and conn:
                try: cursor.execute("UPDATE users SET practice_total = practice_total + 1, practice_correct = practice_correct + ? WHERE chat_id = ?", (1 if is_correct else 0, chat_id)); conn.commit(); await log_user_action(chat_id, "practice_result", {"idiom": idiom_data.get('idiom'), "correct": is_correct})
                except sqlite3.Error as e: logger.error(f"Ошибка SQLite обновления статистики {chat_id}: {e}")
            await update.message.reply_text(reply_text_clean or "Не удалось получить оценку.", reply_markup=back_button())
        except Exception as e: logger.error(f"Ошибка Gemini (практика): {e}", exc_info=True); await update.message.reply_text(f"❌ Ошибка проверки: {str(e)}", reply_markup=back_button())

    # 7. Если не ожидается никакого специфического ввода (без изменений)
    else:
        await log_user_action(chat_id, "unhandled_message", {"text": text})
        await update.message.reply_text("Используйте кнопки или команду /start.")

# 16. Вспомогательные функции для форматирования и кнопок (без изменений)
def format_idiom_details(idiom_data: sqlite3.Row) -> str:
    if not idiom_data: return "Идиома не найдена."
    try: return (f"🔤 *Идиома*: {idiom_data['idiom']}\n🈷️ *Пиньинь*: {idiom_data['pinyin']}\n🌐 *Перевод*: {idiom_data['translation']}\n💡 *Значение*: {idiom_data['meaning']}\n📝 *Пример*: {idiom_data['example']}")
    except (IndexError, KeyError) as e: logger.error(f"Ошибка форматирования идиомы: {e} - Data: {dict(idiom_data) if idiom_data else 'None'}"); return "Ошибка данных."

def back_button(): return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Главное меню", callback_data="back")]])
def back_to_dictionary_button(): return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в словарь", callback_data="back_to_dictionary")]])
def exit_asking_button(): return InlineKeyboardMarkup([[InlineKeyboardButton("🚪 Выйти из режима вопросов", callback_data="exit_asking_mode")]])
def exit_free_mode_button(): return InlineKeyboardMarkup([[InlineKeyboardButton("🚪 Выйти из свободного режима", callback_data="exit_free_mode")]])

# 17. Функция для ежедневной рассылки (`send_daily_idiom`) - без изменений в логике
async def send_daily_idiom(context: ContextTypes.DEFAULT_TYPE):
    if not cursor: return
    now_utc = datetime.now(pytz.utc); current_time_str = now_utc.strftime("%H:%M")
    if now_utc.minute == 0: logger.debug(f"Проверка рассылки {current_time_str} UTC") # Логируем только раз в час для чистоты
    users_to_notify = []
    try: cursor.execute("SELECT chat_id FROM users WHERE daily_time = ?", (current_time_str,)); users_to_notify = [row['chat_id'] for row in cursor.fetchall()]
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite получения пользователей рассылки: {e}"); return
    if not users_to_notify: return
    try: cursor.execute("SELECT * FROM idioms ORDER BY RANDOM() LIMIT 1"); idiom_data = cursor.fetchone()
    except sqlite3.Error as e: logger.error(f"Ошибка SQLite получения идиомы для рассылки: {e}"); return
    if not idiom_data: logger.warning("БД идиом пуста для рассылки."); return
    idiom_text = idiom_data['idiom']
    msg_text = f"📚 *Идиома дня* ({now_utc.strftime('%d.%m.%Y')})\n\n" + format_idiom_details(idiom_data)
    keyboard = [[InlineKeyboardButton("➕ Добавить в словарь", callback_data=f"confirm_add_{idiom_text}")], [InlineKeyboardButton("❓ Задать вопрос", callback_data=f"question_{idiom_text}")]]
    sent_count, failed_count = 0, 0
    for chat_id in users_to_notify:
        try: await context.bot.send_message(chat_id=chat_id, text=msg_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)); sent_count += 1; await log_user_action(chat_id, "daily_idiom_sent", {"idiom": idiom_text})
        except Exception as e: failed_count += 1; logger.warning(f"Ошибка отправки рассылки {chat_id}: {e}")
    if sent_count > 0 or failed_count > 0: logger.info(f"Рассылка в {current_time_str} UTC: отпр={sent_count}, ошибки={failed_count}.")

# 18. Команда для просмотра логов (`show_logs`) (без изменений)
async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat: return
    chat_id = update.effective_chat.id
    user = update.effective_user
    if user: await update_user_info(chat_id, user)
    await log_user_action(chat_id, "command_log")

    if not cursor: await update.message.reply_text("Ошибка: БД недоступна."); return

    log_limit = 15
    try:
        query = """
            SELECT ul.timestamp, ul.action_type, ul.details, u.username, u.first_name
            FROM user_logs ul
            LEFT JOIN users u ON ul.chat_id = u.chat_id
            WHERE ul.chat_id = ?
            ORDER BY ul.timestamp DESC
            LIMIT ?
        """
        cursor.execute(query, (chat_id, log_limit))
        logs = cursor.fetchall()

        if not logs: await update.message.reply_text("Нет записей в логе для вас."); return

        log_message = f"📋 *Последние {len(logs)} ваших действий* (новейшие сверху):\n\n"
        for log_row in logs:
            ts_str = log_row['timestamp']; action = log_row['action_type']; details_json = log_row['details']
            username = log_row['username']; first_name = log_row['first_name']
            try: formatted_ts = datetime.fromisoformat(ts_str.split('.')[0]).strftime('%Y-%m-%d %H:%M UTC') # Убрал секунды для краткости
            except: formatted_ts = ts_str
            details_str = ""
            if details_json:
                try: details_dict = json.loads(details_json); details_str = " | " + ", ".join(f"`{k}`=`{v}`" for k, v in details_dict.items()) # Изменил формат для краткости
                except: details_str = f" | {details_json}"
            log_message += f"`{formatted_ts}`: **{action}**{details_str}\n"

        await update.message.reply_text(log_message, parse_mode="Markdown")
        logger.info(f"Лог успешно отправлен пользователю {chat_id}.")

    except sqlite3.Error as e: logger.error(f"Ошибка SQLite при чтении логов {chat_id}: {e}", exc_info=True); await update.message.reply_text("Ошибка получения логов.")
    except Exception as e: logger.error(f"Неизвестная ошибка в show_logs {chat_id}: {e}", exc_info=True); await update.message.reply_text("Ошибка формирования лога.")


# 19. Основная функция запуска бота (`main`) (без изменений)
def main():
    # ИЗМЕНЕНО: Проверки токенов теперь внутри tokens.py при импорте, но можно добавить и здесь
    if not TELEGRAM_TOKEN or "YOUR_REAL_TELEGRAM_BOT_TOKEN" in TELEGRAM_TOKEN: logger.critical("!!! НЕТ TELEGRAM_TOKEN в tokens.py !!!"); return
    if not GEMINI_API_KEY or "YOUR_REAL_GEMINI_API_KEY" in GEMINI_API_KEY: logger.warning("!!! НЕТ GEMINI_API_KEY в tokens.py !!!")
    if not client: logger.warning("!!! Gemini API клиент не инициализирован (проверьте ключ или API). Функции Gemini не будут работать. !!!")
    if not conn or not cursor: logger.critical("!!! Ошибка инициализации БД !!!"); return

    logger.info(f"Загрузка/обновление идиом из {IDIOMS_JSON_FILE}...")
    load_idioms_from_json(cursor, conn)
    logger.info("Загрузка идиом завершена.")

    try:
        app = Application.builder().token(TELEGRAM_TOKEN).build()
        logger.info("Приложение Telegram бота создано.")
        app.add_handler(CommandHandler("start", show_main_menu))
        app.add_handler(CommandHandler("log", show_logs)) # Добавили обработчик /log
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        logger.info("Обработчики добавлены.")
        if app.job_queue: app.job_queue.run_repeating(send_daily_idiom, interval=60, first=10); logger.info("Рассылка запланирована.")
        else: logger.warning("Job Queue недоступен.")
        logger.info("Запуск бота (polling)...")
        app.run_polling()
    except Exception as e: logger.critical(f"Критическая ошибка запуска: {str(e)}", exc_info=True)
    finally:
          if conn: conn.close(); logger.info("Соединение с БД закрыто.")

# 20. Точка входа (без изменений)
if __name__ == "__main__":
    main()
