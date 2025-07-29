import logging
import asyncio
import sqlite3
import random

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, JobQueue

# Включите ведение журнала
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Установите более низкий уровень журнала для httpx (используется telegram-bot)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И НАСТРОЙКИ ---
TOKEN = "8236911467:AAG1wlOOoW1L1s4NMOGw2V3_-NGwEBp-Xvs"  # Замените на свой токен бота
OWNER_ID = 5620803063  # ID владельца для админских команд
ADMIN_IDS = [5620803063]  # Список ID администраторов
ADMIN_COMMANDS = ["/admin", "/ban", "/unban", "/add_balance", "/set_match_id", "/register", "/find_match_debug", "/check_ban", "/end_match", "/set_username", "/set_rank", "/view_profile", "/change_rank"] # Добавлены новые админские команды
# Глобальный словарь для отслеживания состояния матчей
# chat_id: {
#   'players': [user_id1, user_id2, ...], # Теперь храним user_id
#   'current_phase': 'search' | 'map_vote' | 'captain_pick' | 'finished',
#   'message_id': ID сообщения для редактирования,
#   'message_thread_id': ID темы, если команда была в теме (иначе None)
#   'map_votes': {map_name: count, ...},
#   'players_voted_map': [user_id, ...], # Теперь храним user_id
#   'captains': [captain1_user_id, captain2_user_id], # Теперь храним user_id
#   'teams': {'team1': [player_user_id, player_user_id], 'team2': [player_user_id, player_user_id]}, # Теперь храним user_id
#   'remaining_players_for_pick': [player_user_id, ...], # Игроки, которых еще не пикнули (user_id)
#   'current_picker_index': 0 or 1 (индекс текущего капитана, который пикает)
#   'search_timeout_job': Job object for timeout cancellation
# }
GLOBAL_MATCH_FLOW = {}

# Глобальный словарь для отслеживания состояния регистрации пользователя
# user_id: "waiting_for_username"
user_registration_states = {}

# Конфигурация карт
MAPS = ["Sandstone", "Sakura", "Rust", "Zone 7", "Dune", "Breeze", "Province"]

# Добавим состояния матча для удобства
MATCH_PHASES = {
    "SEARCH": "search",
    "MAP_VOTE": "map_vote",
    "CAPTAIN_PICK": "captain_pick",
    "FINISHED": "finished"
}

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            is_banned BOOLEAN DEFAULT FALSE,
            rank TEXT DEFAULT 'Новичок' -- Добавляем столбец для ранга
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            match_id INTEGER PRIMARY KEY AUTOINCREMENT,
            player1_id INTEGER,
            player2_id INTEGER,
            result TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS match_players (
            match_id INTEGER,
            user_id INTEGER,
            team_number INTEGER,
            FOREIGN KEY(match_id) REFERENCES matches(match_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            PRIMARY KEY (match_id, user_id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_matches (
            telegram_chat_id INTEGER PRIMARY KEY,
            match_id INTEGER,
            owner_id INTEGER,
            player_list TEXT,
            state TEXT,
            FOREIGN KEY(match_id) REFERENCES matches(match_id)
        )
    ''')
    conn.commit()
    conn.close()

# Функция для миграции БД (добавления нового столбца rank, если он не существует)
def migrate_db():
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN rank TEXT DEFAULT 'Новичок'")
        conn.commit()
        logger.info("Столбец 'rank' успешно добавлен в таблицу 'users'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            logger.info("Столбец 'rank' уже существует.")
        else:
            logger.error(f"Ошибка при добавлении столбца 'rank': {e}")
            raise
    conn.close()


def get_user_data(user_id):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user_data = cursor.fetchone()
    conn.close()
    return user_data

# Модифицируем add_user для обновления юзернейма, если он изменился, и для первичной регистрации
def add_user(user_id, username):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    # Если пользователь уже существует, обновляем его юзернейм (если он изменился)
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
    conn.commit()
    conn.close()

def update_user_balance(user_id, amount):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def ban_user(user_id):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = TRUE WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = FALSE WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_user_banned(user_id):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] == 1 if result else False # SQLite stores BOOLEAN as INTEGER (0 or 1)

# Новые функции для обновления ника и ранга
def update_user_username(user_id, new_username):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (new_username, user_id))
    conn.commit()
    conn.close()

def update_user_rank(user_id, new_rank):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET rank = ? WHERE user_id = ?", (new_rank, user_id))
    conn.commit()
    conn.close()

# --- ОБЩИЕ ФУНКЦИИ ---
async def is_admin(user_id):
    return user_id == OWNER_ID or user_id in ADMIN_IDS

# Вспомогательная функция для получения юзернейма по user_id
def get_username_for_display(user_id):
    user_data = get_user_data(user_id)
    if user_data and user_data[1]: # Проверяем, что username не None
        return user_data[1]
    return f"Игрок_{user_id}" # Запасной вариант, если юзернейм не найден

# --- НОВЫЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ЛОГИКИ МАТЧА ---

async def process_map_selection(query, chat_id, match_data, context): # Добавляем context
    """Обрабатывает выбор карты после голосования."""
    # Отменяем таймаут поиска, если матч запускается
    if 'search_timeout_job' in match_data and match_data['search_timeout_job'] is not None:
        try:
            match_data['search_timeout_job'].schedule_removal()
            logger.info(f"Scheduled removal for search_timeout_job in chat {chat_id} due to map selection.")
        except Exception as e:
            logger.warning(f"Error removing search_timeout_job for chat {chat_id} during map selection: {e}")
        match_data['search_timeout_job'] = None # Обнуляем ссылку

    # Определяем карту с наибольшим количеством голосов
    max_votes = -1
    winning_maps = []

    for map_name, votes in match_data['map_votes'].items():
        if votes > max_votes:
            max_votes = votes
            winning_maps = [map_name]
        elif votes == max_votes:
            winning_maps.append(map_name)

    selected_map = random.choice(winning_maps) if winning_maps else "Случайная карта" # Выбираем случайную, если голоса равны, или дефолт
    match_data['selected_map'] = selected_map # Сохраняем выбранную карту в данных матча

    logger.info(f"Map selected for match in chat {chat_id}: {selected_map}")

    # Удаляем сообщение с выбором карт
    try:
        if query and query.message: # query может быть None, если вызывается напрямую из start_game_force
            await query.message.delete()
        elif match_data.get('message_id'): # Если нет query, но есть message_id из предыдущего сообщения
            await context.bot.delete_message(chat_id=chat_id, message_id=match_data['message_id'], message_thread_id=match_data.get('message_thread_id'))
    except Exception as e:
        logger.warning(f"Could not delete map vote message in chat {chat_id}: {e}")
    match_data['message_id'] = None # Обнуляем, т.к. сообщение удалено


    player_count = len(match_data['players'])

    if player_count <= 2: # Если 2 игрока (или меньше, хотя логика кнопки Start Game должна предотвратить <2)
        # Если 2 игрока, сразу переходим к финальному сообщению
        # Выбираем случайного капитана (первый капитан)
        captain1 = random.choice(match_data['players']) # Теперь это user_id
        match_data['captains'].append(captain1)
        match_data['teams']['team1'].append(captain1) # Капитан в своей команде

        final_message_text = (
            f"Игроки собраны!\n"
            f"Карта выбрана! ({selected_map})\n"
            f"Команды собраны!\n"
            f"Желаем вам удачной игры! Лобби создает 1 капитан ({get_username_for_display(captain1)})"
        )
        await context.bot.send_message(chat_id=chat_id, text=final_message_text, message_thread_id=match_data.get('message_thread_id'))
        match_data['current_phase'] = MATCH_PHASES["FINISHED"]
        logger.info(f"Match in chat {chat_id} finished for {player_count} players. Captain: {get_username_for_display(captain1)}")
    else:
        # Если игроков больше 2, переходим к выбору капитанов и пикам
        match_data['current_phase'] = MATCH_PHASES["CAPTAIN_PICK"]

        # Выбираем двух случайных капитанов
        match_data['captains'] = random.sample(match_data['players'], 2) # Теперь это user_id
        match_data['teams']['team1'].append(match_data['captains'][0]) # Капитаны сразу в своих командах
        match_data['teams']['team2'].append(match_data['captains'][1])

        match_data['remaining_players_for_pick'] = [p for p in match_data['players'] if p not in match_data['captains']]
        random.shuffle(match_data['remaining_players_for_pick']) # Перемешиваем, чтобы не было предсказуемого порядка
        match_data['current_picker_index'] = 0 # Первый капитан пикает (индекс текущего капитана в match_data['captains'])

        # Формируем сообщение для пиков
        current_picker_username = get_username_for_display(match_data['captains'][match_data['current_picker_index']])
        pick_message_text = (
            f"Игроки собраны!\n"
            f"Карта выбрана! ({selected_map})\n"
            f"Капитаны:\n{get_username_for_display(match_data['captains'][0])} и {get_username_for_display(match_data['captains'][1])}\n\n"
            f"⚡️ Сейчас пикает: {current_picker_username}\n"
            f"Пики игроков:\n"
        )
        # Создаем кнопки для пиков игроков (доступные игроки)
        pick_keyboard = []
        for player_id in match_data['remaining_players_for_pick']:
            pick_keyboard.append([InlineKeyboardButton(get_username_for_display(player_id), callback_data=f"pick_player_{player_id}")])
        pick_reply_markup = InlineKeyboardMarkup(pick_keyboard)

        sent_message = await context.bot.send_message(chat_id=chat_id, text=pick_message_text, reply_markup=pick_reply_markup, message_thread_id=match_data.get('message_thread_id'))
        match_data['message_id'] = sent_message.message_id # Сохраняем ID нового сообщения
        logger.info(f"Match in chat {chat_id} transitioning to captain pick. Captains: {[get_username_for_display(c) for c in match_data['captains']]}")


async def process_player_pick(query, chat_id, match_data, picked_player_id, context): # Добавляем context
    """Обрабатывает выбор игрока капитаном."""
    current_picker_id = match_data['captains'][match_data['current_picker_index']]
    
    if query.from_user.id != current_picker_id:
        await query.answer("Сейчас не ваша очередь пикать!")
        return

    # picked_player_id придет как строка из callback_data, нужно преобразовать в int
    picked_player_id = int(picked_player_id) 

    if picked_player_id not in match_data['remaining_players_for_pick']:
        await query.answer("Этот игрок уже выбран или не доступен для пика.")
        return

    # Добавляем игрока в команду текущего капитана
    team_to_add_to = 'team1' if match_data['current_picker_index'] == 0 else 'team2'
    match_data['teams'][team_to_add_to].append(picked_player_id)
    match_data['remaining_players_for_pick'].remove(picked_player_id)

    logger.info(f"Captain {get_username_for_display(current_picker_id)} picked {get_username_for_display(picked_player_id)} for {team_to_add_to}.")

    # Переключаем пикающего капитана
    match_data['current_picker_index'] = 1 - match_data['current_picker_index'] # 0 -> 1, 1 -> 0

    # Обновляем сообщение с пиками
    await update_pick_message(query, chat_id, match_data, context)

    # Проверяем, все ли игроки выбраны (4 пика + 1 капитан = 5 игроков в команде, т.е. 8 пиков + 2 капитана = 10 игроков)
    if not match_data['remaining_players_for_pick']:
        await process_team_formation_complete(query, chat_id, match_data, context)


async def update_pick_message(query, chat_id, match_data, context): # Добавляем context
    """Обновляет сообщение с пиками игроков."""
    current_picker_username = get_username_for_display(match_data['captains'][match_data['current_picker_index']]) if match_data['remaining_players_for_pick'] else "Все выбраны!"
    
    # Формируем списки игроков для отображения
    team1_display = [get_username_for_display(p_id) for p_id in match_data['teams']['team1']]
    team2_display = [get_username_for_display(p_id) for p_id in match_data['teams']['team2']]

    # Корректируем отображение игроков в команде
    team1_players_str = ', '.join(team1_display) if team1_display else 'пока нет'
    team2_players_str = ', '.join(team2_display) if team2_display else 'пока нет'

    pick_message_text = (
        f"Игроки собраны!\n"
        f"Карта выбрана! ({match_data.get('selected_map', 'Неизвестно')})\n"
        f"**Команда 1 ({len(team1_display)}):** {team1_players_str}\n"
        f"**Команда 2 ({len(team2_display)}):** {team2_players_str}\n\n"
        f"⚡️ Сейчас пикает: {current_picker_username}\n"
        f"Доступные игроки:\n"
    )

    pick_keyboard = []
    for player_id in match_data['remaining_players_for_pick']:
        pick_keyboard.append([InlineKeyboardButton(get_username_for_display(player_id), callback_data=f"pick_player_{player_id}")])
    pick_reply_markup = InlineKeyboardMarkup(pick_keyboard)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=match_data['message_id'],
            text=pick_message_text,
            reply_markup=pick_reply_markup,
            parse_mode='Markdown',
            message_thread_id=match_data.get('message_thread_id')
        )
        logger.info(f"Successfully edited pick message {match_data['message_id']} in chat {chat_id}.")
    except Exception as e:
        logger.error(f"Error updating pick message {match_data['message_id']} in chat {chat_id}: {e}. Sending new message.")
        # Если не удалось отредактировать (например, сообщение слишком старое), отправляем новое
        sent_message = await context.bot.send_message(chat_id=chat_id, text=pick_message_text, reply_markup=pick_reply_markup, parse_mode='Markdown', message_thread_id=match_data.get('message_thread_id'))
        match_data['message_id'] = sent_message.message_id
        logger.info(f"Sent new pick message {match_data['message_id']} in chat {chat_id}.")


async def process_team_formation_complete(query, chat_id, match_data, context): # Добавляем context
    """Отправляет финальное сообщение после завершения пиков."""
    # Удаляем сообщение с пиками
    try:
        if match_data.get('message_id'):
            await context.bot.delete_message(chat_id=chat_id, message_id=match_data['message_id'], message_thread_id=match_data.get('message_thread_id'))
            logger.info(f"Deleted pick message {match_data['message_id']} in chat {chat_id} after team formation.")
    except Exception as e:
        logger.warning(f"Could not delete pick message {match_data['message_id']} in chat {chat_id}: {e}")

    # Формируем окончательные списки игроков для отображения
    team1_display = [get_username_for_display(p_id) for p_id in match_data['teams']['team1']]
    team2_display = [get_username_for_display(p_id) for p_id in match_data['teams']['team2']]

    final_message_text = (
        f"**Матч готов!**\n"
        f"Карта: **{match_data.get('selected_map', 'Неизвестно')}**\n\n"
        f"**Команда 1 ({len(team1_display)}/{len(match_data['players']) // 2}):**\n" # Обновлено для динамического размера команды
        f"Капитан: {get_username_for_display(match_data['captains'][0])}\n"
        f"Игроки: {', '.join(team1_display)}\n\n"
        f"**Команда 2 ({len(team2_display)}/{len(match_data['players']) // 2}):**\n" # Обновлено для динамического размера команды
        f"Капитан: {get_username_for_display(match_data['captains'][1])}\n"
        f"Игроки: {', '.join(team2_display)}\n\n"
        f"Удачи в игре!"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=final_message_text,
        parse_mode='Markdown',
        message_thread_id=match_data.get('message_thread_id')
    )
    match_data['current_phase'] = MATCH_PHASES["FINISHED"]
    # Не удаляем GLOBAL_MATCH_FLOW[chat_id] сразу, чтобы можно было посмотреть команды
    # Удаление будет при следующей команде /find_match или по /end_match
    logger.info(f"Match in chat {chat_id} finished. Teams formed.")


async def cancel_match_on_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Функция, которая вызывается JobQueue по истечении таймаута,
    если матч не начался.
    """
    chat_id = context.job.chat_id
    if chat_id in GLOBAL_MATCH_FLOW:
        match_data = GLOBAL_MATCH_FLOW[chat_id]
        if match_data['current_phase'] == MATCH_PHASES["SEARCH"]:
            logger.info(f"Match search in chat {chat_id} timed out.")
            # Удаляем сообщение с кнопками, если оно есть
            if match_data.get('message_id'): # Используем .get() для безопасности
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=match_data['message_id'], message_thread_id=match_data.get('message_thread_id'))
                    logger.info(f"Deleted match message {match_data['message_id']} in chat {chat_id}.")
                except Exception as e:
                    logger.warning(f"Could not delete match message {match_data['message_id']} in chat {chat_id} on timeout: {e}")

            await context.bot.send_message(
                chat_id=chat_id,
                text="Поиск игроков завершен. Не удалось набрать достаточно игроков в течение 5 минут.",
                message_thread_id=match_data.get('message_thread_id')
            )
            del GLOBAL_MATCH_FLOW[chat_id] # Удаляем данные о матче
            logger.info(f"Match data for chat {chat_id} cleared due to timeout.")
        else:
            # Если фаза изменилась, значит, матч уже начался или отменен вручную,
            # и этот таймаут уже неактуален.
            logger.info(f"Timeout job for chat {chat_id} fired but match phase is {match_data['current_phase']}, no action needed.")


# --- ОБРАБОТЧИКИ КОМАНД ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    add_user(user_id, username) # Убедимся, что пользователь есть в БД

    chat_type = update.effective_chat.type

    if chat_type == "private":
        await update.message.reply_text(f"Привет, {get_username_for_display(user_id)}! Я бот для организации матчей. Используй /help, чтобы узнать, что я могу.")
    elif chat_type in ["group", "supergroup"]:
        await update.message.reply_text(
            "Привет! Я бот для создания и управления Faceit-матчами.\n"
            "Используй /find_match для поиска игры.\n"
            "Для полного списка команд используй /help."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Вот команды, которые я умею выполнять:\n"
                                     "/start - Начать взаимодействие с ботом\n"
                                     "/help - Показать список команд\n"
                                     "/profile - Показать ваш профиль (баланс, ник, ранг, ID)\n"
                                     "/rank - Показать ваш текущий ранг\n" # Добавлено
                                     "/balance - Показать ваш баланс\n"
                                     "/registration - (только в ЛС) Зарегистрировать свой юзернейм для участия в матчах.\n"
                                     "/find_match - Начать поиск игроков\n"
                                     "Админские команды:\n"
                                     "/admin - Показать админские команды\n"
                                     "/ban [user_id] - Забанить пользователя\n"
                                     "/unban [user_id] - Разбанить пользователя\n"
                                     "/add_balance [user_id] [amount] - Добавить баланс пользователю\n"
                                     "/set_username [user_id] [новый_никнейм] - Изменить никнейм пользователя\n"
                                     "/change_rank [user_id] [новый_ранг] - Изменить ранг пользователя\n" # Обновлено
                                     "/view_profile [user_id] - Посмотреть профиль другого пользователя\n"
                                     "/set_match_id [id_чата] [match_id] - Установить ID матча (для владельца)\n"
                                     "/register - Зарегистрировать матч (для владельца)\n"
                                     "/find_match_debug - Отладочная информация о текущих матчах\n"
                                     "/check_ban - Проверить статус бана пользователя\n"
                                     "/end_match - Завершить текущий поиск матча в чате")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if user_data:
        await update.message.reply_text(f"Ваш текущий баланс: {user_data[2]} монет.")
    else:
        await update.message.reply_text("Вы не зарегистрированы в системе. Используйте /start или /registration.")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message_thread_id = update.effective_message.message_thread_id

    user_data = get_user_data(user_id) # Получаем все данные пользователя

    if user_data:
        # user_data теперь должен содержать (user_id, username, balance, is_banned, rank)
        # Индексы: 0=user_id, 1=username, 2=balance, 3=is_banned, 4=rank
        username = user_data[1] if user_data[1] else "Не установлен"
        balance = user_data[2]
        is_banned_status = "Да" if user_data[3] else "Нет"
        rank = user_data[4] if user_data[4] else "Новичок"

        profile_text = (
            f"**Ваш Профиль:**\n"
            f"🆔 ID: `{user_id}`\n"
            f"👤 Никнейм: **{username}**\n"
            f"💰 Баланс: **{balance}** монет\n"
            f"🎖️ Ранг: **{rank}**\n"
            f"🚫 Забанен: **{is_banned_status}**"
        )
        await update.message.reply_text(profile_text, parse_mode='Markdown', message_thread_id=message_thread_id)
    else:
        await update.message.reply_text("Вы не зарегистрированы в системе. Используйте /start или /registration.", message_thread_id=message_thread_id)

async def rank_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message_thread_id = update.effective_message.message_thread_id

    user_data = get_user_data(user_id)

    if user_data:
        rank = user_data[4] if user_data[4] else "Новичок"
        rank_text = (
            f"**Ваш текущий ранг:**\n"
            f"🎖️ Ранг: **{rank}**"
        )
        await update.message.reply_text(rank_text, parse_mode='Markdown', message_thread_id=message_thread_id)
    else:
        await update.message.reply_text("Вы не зарегистрированы в системе. Используйте /start или /registration.", message_thread_id=message_thread_id)


async def check_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите ID пользователя. Пример: /check_ban 123456789")
        return

    try:
        target_user_id = int(context.args[0])
        if is_user_banned(target_user_id):
            await update.message.reply_text(f"Пользователь с ID {target_user_id} забанен.")
        else:
            await update.message.reply_text(f"Пользователь с ID {target_user_id} не забанен.")
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для доступа к админ-панели.")
        return
    await update.message.reply_text("Добро пожаловать в админ-панель!\n"
                                     "Доступные команды:\n"
                                     "/ban [user_id] - Забанить пользователя\n"
                                     "/unban [user_id] - Разбанить пользователя\n"
                                     "/add_balance [user_id] [amount] - Добавить баланс пользователю\n"
                                     "/set_username [user_id] [новый_никнейм] - Изменить никнейм пользователя\n"
                                     "/change_rank [user_id] [новый_ранг] - Изменить ранг пользователя\n" # Обновлено
                                     "/view_profile [user_id] - Посмотреть профиль другого пользователя\n"
                                     "/set_match_id [id_чата] [match_id] - Установить ID матча\n"
                                     "/register - Зарегистрировать матч (для владельца)\n"
                                     "/find_match_debug - Отладочная информация о текущих матчах\n"
                                     "/end_match - Завершить текущий поиск матча в чате")

async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите ID пользователя для бана. Пример: /ban 123456789")
        return
    try:
        target_user_id = int(context.args[0])
        ban_user(target_user_id)
        await update.message.reply_text(f"Пользователь с ID {target_user_id} забанен.")
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")

async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите ID пользователя для разбана. Пример: /unban 123456789")
        return
    try:
        target_user_id = int(context.args[0])
        unban_user(target_user_id)
        await update.message.reply_text(f"Пользователь с ID {target_user_id} разбанен.")
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")

async def add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Пожалуйста, укажите ID пользователя и сумму. Пример: /add_balance 123456789 100")
        return
    try:
        target_user_id = int(context.args[0])
        amount = int(context.args[1])
        update_user_balance(target_user_id, amount)
        await update.message.reply_text(f"Баланс пользователя с ID {target_user_id} увеличен на {amount}.")
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя или суммы.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")

async def set_username_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_username [user_id] [новый_никнейм]")
        return
    try:
        target_user_id = int(context.args[0])
        new_username = " ".join(context.args[1:])
        update_user_username(target_user_id, new_username)
        await update.message.reply_text(f"Никнейм пользователя с ID {target_user_id} изменен на **{new_username}**.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")

async def set_rank_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_rank [user_id] [новый_ранг]")
        return
    try:
        target_user_id = int(context.args[0])
        new_rank = " ".join(context.args[1:])
        update_user_rank(target_user_id, new_rank)
        await update.message.reply_text(f"Ранг пользователя с ID {target_user_id} изменен на **{new_rank}**.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")

# Добавляем алиас для /change_rank, указывающий на set_rank_admin
async def change_rank_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_rank_admin(update, context)


async def view_profile_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите ID пользователя. Пример: /view_profile 123456789")
        return
    try:
        target_user_id = int(context.args[0])
        user_data = get_user_data(target_user_id)
        if user_data:
            username = user_data[1] if user_data[1] else "Не установлен"
            balance = user_data[2]
            is_banned_status = "Да" if user_data[3] else "Нет"
            rank = user_data[4] if user_data[4] else "Новичок"

            profile_text = (
                f"**Профиль пользователя {target_user_id}:**\n"
                f"👤 Никнейм: **{username}**\n"
                f"💰 Баланс: **{balance}** монет\n"
                f"🎖️ Ранг: **{rank}**\n"
                f"🚫 Забанен: **{is_banned_status}**"
            )
            await update.message.reply_text(profile_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"Пользователь с ID {target_user_id} не найден.")
    except ValueError:
        await update.message.reply_text("Неверный формат ID пользователя.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")


async def register_match_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Эта команда доступна только владельцу бота.")
        return
    try:
        # Пример: /register @player1 @player2 win (где player1 победил)
        # Это очень упрощенная логика, требующая доработки для реальных матчей
        player1_username = context.args[0].replace('@', '')
        player2_username = context.args[1].replace('@', '')
        result_type = context.args[2].lower() # 'win' или 'lose'

        conn = sqlite3.connect('bot.db')
        cursor = conn.cursor()

        # Получаем ID игроков
        cursor.execute("SELECT user_id FROM users WHERE username = ?", (player1_username,))
        player1_id_data = cursor.fetchone()
        cursor.execute("SELECT user_id FROM users WHERE username = ?", (player2_username,))
        player2_id_data = cursor.fetchone()

        if not player1_id_data or not player2_id_data:
            await update.message.reply_text("Один или оба пользователя не найдены.")
            conn.close()
            return

        player1_id = player1_id_data[0]
        player2_id = player2_id_data[0]

        # Добавляем матч в базу данных
        match_result = f"{player1_username} vs {player2_username} - {result_type}"
        cursor.execute("INSERT INTO matches (player1_id, player2_id, result) VALUES (?, ?, ?)", (player1_id, player2_id, match_result))
        match_id = cursor.lastrowid

        # Добавляем игроков в match_players
        cursor.execute("INSERT INTO match_players (match_id, user_id, team_number) VALUES (?, ?, ?)", (match_id, player1_id, 1))
        cursor.execute("INSERT INTO match_players (match_id, user_id, team_number) VALUES (?, ?, ?)", (match_id, player2_id, 2))

        conn.commit()
        conn.close()
        await update.message.reply_text(f"Результат матча '{match_result}' зарегистрирован под ID: {match_id}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при регистрации матча: {e}")

# Функции для работы с таблицей active_matches
def get_active_match_id(telegram_chat_id):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT match_id FROM active_matches WHERE telegram_chat_id = ?", (telegram_chat_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def set_active_match_id(telegram_chat_id, match_id, owner_id, player_list="", state=""):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO active_matches (telegram_chat_id, match_id, owner_id, player_list, state) VALUES (?, ?, ?, ?, ?)",
                   (telegram_chat_id, match_id, owner_id, player_list, state))
    conn.commit()
    conn.close()

def delete_active_match(telegram_chat_id):
    conn = sqlite3.connect('bot.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_matches WHERE telegram_chat_id = ?", (telegram_chat_id,))
    conn.commit()
    conn.close()

async def set_match_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Эту команду может использовать только владелец бота
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Использование: /set_match_id [ID_чата] [ID_матча]")
        return

    try:
        chat_id = int(context.args[0])
        match_id = int(context.args[1])
        set_active_match_id(chat_id, match_id, update.effective_user.id)
        await update.message.reply_text(f"Для чата {chat_id} установлен ID матча: {match_id}")
    except ValueError:
        await update.message.reply_text("Неверный формат ID чата или ID матча.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка: {e}")


async def find_match_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    message_thread_id = update.effective_message.message_thread_id # Получаем ID темы

    # Убедимся, что пользователь зарегистрирован и не забанен
    add_user(user_id, username)
    if is_user_banned(user_id):
        await update.message.reply_text("Вы забанены и не можете участвовать в поиске матча.")
        logger.info(f"Banned user {get_username_for_display(user_id)} tried to use /find_match in chat {chat_id}.")
        return

    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("Эта команда работает только в групповых чатах.")
        return

    if chat_id in GLOBAL_MATCH_FLOW:
        match_data = GLOBAL_MATCH_FLOW[chat_id]
        if match_data['current_phase'] == MATCH_PHASES["SEARCH"]:
            # Если поиск уже активен, но пользователь не в нем, добавить его
            if user_id not in match_data['players']:
                match_data['players'].append(user_id)
                player_count = len(match_data['players'])
                logger.info(f"User {get_username_for_display(user_id)} joined existing match in chat {chat_id}. Current players: {player_count}")
                await update_match_message(chat_id, match_data, context, is_initial_message=False)
                await update.message.reply_text(f"{get_username_for_display(user_id)} присоединился к поиску. Игроков: {player_count}/10", message_thread_id=message_thread_id)
            else:
                await update.message.reply_text("Вы уже находитесь в поиске матча.", message_thread_id=message_thread_id)
            return
        elif match_data['current_phase'] != MATCH_PHASES["FINISHED"]:
            await update.message.reply_text(f"В этом чате уже идет матч в фазе '{match_data['current_phase']}'. Дождитесь его завершения или отмените его через /end_match.", message_thread_id=message_thread_id)
            return
        else:
            # Если фаза 'finished', сбрасываем данные для нового поиска
            del GLOBAL_MATCH_FLOW[chat_id]
            delete_active_match(chat_id) # Очищаем из БД
            logger.info(f"Cleared finished match data for chat {chat_id} to start new search.")


    # Инициализация нового поиска матча
    GLOBAL_MATCH_FLOW[chat_id] = {
        'players': [],
        'current_phase': MATCH_PHASES["SEARCH"],
        'message_id': None, # Здесь будет ID сообщения с кнопками
        'message_thread_id': message_thread_id, # Сохраняем ID темы!
        'map_votes': {map_name: 0 for map_name in MAPS},
        'players_voted_map': [],
        'captains': [],
        'teams': {'team1': [], 'team2': []},
        'remaining_players_for_pick': [],
        'current_picker_index': 0,
        'selected_map': None,
        'search_timeout_job': None # Будет содержать объект Job
    }
    match_data = GLOBAL_MATCH_FLOW[chat_id]
    match_data['players'].append(user_id) # Добавляем инициировавшего игрока
    
    logger.info(f"Initialized new match search for chat {chat_id}.")
    logger.info(f"User {get_username_for_display(user_id)} joined match in chat {chat_id}. Current players: 1")

    # Устанавливаем таймаут на 5 минут для поиска игроков
    if context.job_queue:
        match_data['search_timeout_job'] = context.job_queue.run_once(
            cancel_match_on_timeout, 300, chat_id=chat_id, name=f"match_timeout_{chat_id}"
        )
        logger.info(f"Scheduled match timeout job for chat {chat_id}.")
    else:
        logger.warning(f"No JobQueue set up for chat {chat_id}. Match timeout will not be scheduled.")
        await update.message.reply_text("Внимание: JobQueue не настроен, автоматическая отмена поиска матча по таймауту не будет работать. Пожалуйста, установите 'python-telegram-bot[job-queue]'.", message_thread_id=message_thread_id)

    await update_match_message(chat_id, match_data, context, is_initial_message=True)


async def update_match_message(chat_id, match_data, context, is_initial_message=False) -> None:
    current_players_usernames = [get_username_for_display(p_id) for p_id in match_data['players']]
    player_count = len(current_players_usernames)
    
    keyboard = [
        [InlineKeyboardButton("Присоединиться", callback_data="join_match"),
         InlineKeyboardButton("Покинуть очередь", callback_data="leave_match")],
        [InlineKeyboardButton("Начать игру", callback_data="start_game_force")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    message_text = (
        f"**🔥 Идет поиск игроков! 🔥**\n\n"
        f"Игроков в лобби: **{player_count}/10**\n\n"
        f"**Текущие игроки:**\n"
        f"{', '.join(current_players_usernames) if current_players_usernames else 'Пока никого нет.'}\n\n"
        f"Нажмите 'Присоединиться', чтобы войти в игру."
    )

    # Получаем message_thread_id из данных матча
    thread_id = match_data.get('message_thread_id')

    if is_initial_message or not match_data.get('message_id'):
        # Это блок для первой отправки сообщения
        try:
            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown',
                message_thread_id=thread_id # Используем ID темы
            )
            match_data['message_id'] = sent_message.message_id
            logger.info(f"Sent new match search message {sent_message.message_id} in chat {chat_id} (thread: {thread_id}).")
        except Exception as e:
            # Логируем ошибку и отправляем простое сообщение пользователю
            logger.error(f"Failed to send initial match message in chat {chat_id} (thread: {thread_id}): {e}", exc_info=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Произошла ошибка при отправке сообщения о поиске матча. Пожалуйста, попробуйте еще раз. Ошибка: {e}",
                message_thread_id=thread_id # Попытка отправить ошибку в ту же тему
            )
            # Если отправка не удалась, очищаем состояние матча, чтобы можно было начать заново
            if chat_id in GLOBAL_MATCH_FLOW:
                del GLOBAL_MATCH_FLOW[chat_id]
                delete_active_match(chat_id)
            
    else:
        # Это блок для редактирования существующего сообщения
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=match_data['message_id'],
                text=message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown',
                message_thread_id=thread_id # Используем ID темы
            )
            logger.info(f"Edited match search message {match_data['message_id']} in chat {chat_id} (thread: {thread_id}).")
        except Exception as e:
            logger.warning(f"Could not edit message {match_data['message_id']} in chat {chat_id} (thread: {thread_id}): {e}. Sending new message.", exc_info=True)
            # Если не удалось отредактировать (например, сообщение удалено вручную, слишком старое)
            # пытаемся отправить новое сообщение.
            try:
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown',
                    message_thread_id=thread_id # Используем ID темы
                )
                match_data['message_id'] = sent_message.message_id
                logger.info(f"Sent new match search message {match_data['message_id']} in chat {chat_id} (thread: {thread_id}) after edit failure.")
            except Exception as inner_e:
                logger.error(f"Failed to send new message after edit failure in chat {chat_id} (thread: {thread_id}): {inner_e}", exc_info=True)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Произошла критическая ошибка при обновлении сообщения о поиске матча. Пожалуйста, попробуйте начать новый поиск командой /find_match. Ошибка: {inner_e}",
                    message_thread_id=thread_id # Попытка отправить ошибку в ту же тему
                )
                # Если и новая отправка не удалась, очищаем состояние матча
                if chat_id in GLOBAL_MATCH_FLOW:
                    del GLOBAL_MATCH_FLOW[chat_id]
                    delete_active_match(chat_id)


async def find_match_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return
    
    chat_id = update.effective_chat.id
    message_thread_id = update.effective_message.message_thread_id # Получаем ID темы
    if chat_id in GLOBAL_MATCH_FLOW:
        match_data = GLOBAL_MATCH_FLOW[chat_id]
        players_usernames = [get_username_for_display(p_id) for p_id in match_data['players']]
        debug_info = (
            f"**Детали матча для чата {chat_id}:**\n"
            f"Фаза: `{match_data['current_phase']}`\n"
            f"Игроков: `{len(match_data['players'])}`\n"
            f"Список игроков: `{', '.join(players_usernames)}`\n"
            f"ID сообщения: `{match_data['message_id']}`\n"
            f"ID темы: `{match_data['message_thread_id']}`\n" # Добавлено
            f"Голоса за карты: `{match_data['map_votes']}`\n"
            f"Проголосовавшие за карты: `{[get_username_for_display(p_id) for p_id in match_data['players_voted_map']]}`\n"
            f"Капитаны: `{[get_username_for_display(c_id) for c_id in match_data['captains']]}`\n"
            f"Команды: `Team1: {[get_username_for_display(p_id) for p_id in match_data['teams']['team1']]}, Team2: {[get_username_for_display(p_id) for p_id in match_data['teams']['team2']]}`\n"
            f"Осталось для пика: `{[get_username_for_display(p_id) for p_id in match_data['remaining_players_for_pick']]}`\n"
            f"Текущий пикер: `{get_username_for_display(match_data['captains'][match_data['current_picker_index']]) if match_data['captains'] else 'N/A'}`"
        )
    else:
        debug_info = f"В чате {chat_id} нет активного поиска матча."
    
    await update.message.reply_text(debug_info, parse_mode='Markdown', message_thread_id=message_thread_id)


async def end_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    message_thread_id = update.effective_message.message_thread_id

    if not await is_admin(user_id):
        await update.message.reply_text("У вас нет прав для выполнения этой команды.", message_thread_id=message_thread_id)
        return

    if chat_id not in GLOBAL_MATCH_FLOW:
        await update.message.reply_text("В этом чате нет активного поиска матча или он уже завершен.", message_thread_id=message_thread_id)
        return

    match_data = GLOBAL_MATCH_FLOW[chat_id]

    # Отменяем таймаут, если он был активен
    if 'search_timeout_job' in match_data and match_data['search_timeout_job'] is not None:
        try:
            match_data['search_timeout_job'].schedule_removal()
            logger.info(f"Scheduled removal for search_timeout_job in chat {chat_id}.")
        except Exception as e:
            logger.warning(f"Error removing search_timeout_job for chat {chat_id}: {e}")

    # Удаляем сообщение с кнопками, если оно есть
    if match_data.get('message_id'):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=match_data['message_id'], message_thread_id=match_data.get('message_thread_id'))
            logger.info(f"Deleted match message {match_data['message_id']} in chat {chat_id}.")
        except Exception as e:
            logger.warning(f"Could not delete match message {match_data['message_id']} in chat {chat_id}: {e}")

    # Удаляем данные о матче из GLOBAL_MATCH_FLOW
    del GLOBAL_MATCH_FLOW[chat_id]
    logger.info(f"Match data for chat {chat_id} cleared by /end_match.")

    # Удаляем из active_matches в БД, если там было что-то
    delete_active_match(chat_id)

    await update.message.reply_text("Поиск матча в этом чате отменен администратором.", message_thread_id=message_thread_id)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer() # Всегда отвечаем на callbackQuery, чтобы убрать "часики" на кнопке
    
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    # Проверка на бан
    if is_user_banned(user_id):
        await query.answer("Вы забанены и не можете взаимодействовать с ботом.")
        return

    if chat_id not in GLOBAL_MATCH_FLOW:
        # Если матч исчез (например, после перезапуска бота или ручной очистки)
        # Отправляем сообщение в ту же тему, если это callback из темы
        thread_id_from_callback = query.message.message_thread_id
        await query.message.reply_text("Произошла ошибка: поиск матча не активен. Начните новый поиск с /find_match.", message_thread_id=thread_id_from_callback)
        return

    match_data = GLOBAL_MATCH_FLOW[chat_id]
    current_phase = match_data['current_phase']

    # --- Обработка кнопки "Присоединиться" (join_match) ---
    if query.data == "join_match":
        if current_phase != MATCH_PHASES["SEARCH"]:
            await query.answer("Сейчас нельзя присоединиться, поиск матча неактивен.")
            return

        if user_id in match_data['players']:
            await query.answer("Вы уже в поиске матча.")
        else:
            match_data['players'].append(user_id)
            player_count = len(match_data['players'])
            logger.info(f"User {get_username_for_display(user_id)} joined existing match in chat {chat_id}. Current players: {player_count}")
            await update_match_message(chat_id, match_data, context, is_initial_message=False)
            
            if player_count == 10:
                await query.message.reply_text("Набрано 10 игроков! Переходим к выбору карт.", message_thread_id=match_data.get('message_thread_id'))
                match_data['current_phase'] = MATCH_PHASES["MAP_VOTE"]
                # Отменяем таймаут, так как игроки набраны
                if 'search_timeout_job' in match_data and match_data['search_timeout_job'] is not None:
                    try:
                        match_data['search_timeout_job'].schedule_removal()
                        logger.info(f"Search timeout job for chat {chat_id} removed as 10 players joined.")
                    except Exception as e:
                        logger.warning(f"Error removing search_timeout_job for chat {chat_id} after 10 players: {e}")
                    match_data['search_timeout_job'] = None

                # Отправляем сообщение для выбора карт
                map_keyboard = []
                for map_name in MAPS:
                    map_keyboard.append([InlineKeyboardButton(f"{map_name} (0)", callback_data=f"vote_map_{map_name}")])
                map_reply_markup = InlineKeyboardMarkup(map_keyboard)
                
                # Удаляем старое сообщение с кнопками "Присоединиться"
                try:
                    await query.message.delete()
                    logger.info(f"Deleted old match message {match_data['message_id']} in chat {chat_id} for map vote.")
                except Exception as e:
                    logger.warning(f"Could not delete old match message {match_data['message_id']} in chat {chat_id} for map vote: {e}")

                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text="**Голосуйте за карты!**\n\nТекущие голоса:",
                    reply_markup=map_reply_markup,
                    parse_mode='Markdown',
                    message_thread_id=match_data.get('message_thread_id')
                )
                match_data['message_id'] = sent_message.message_id
                logger.info(f"Sent map vote message {match_data['message_id']} in chat {chat_id}.")
            else:
                await query.answer(f"Вы присоединились. Игроков: {player_count}/10")

    # --- Обработка кнопки "Покинуть очередь" (leave_match) ---
    elif query.data == "leave_match":
        if current_phase != MATCH_PHASES["SEARCH"]:
            await query.answer("Сейчас нельзя покинуть очередь, поиск матча неактивен.")
            return

        if user_id not in match_data['players']:
            await query.answer("Вы не находитесь в поиске матча.")
        else:
            match_data['players'].remove(user_id)
            player_count = len(match_data['players'])
            logger.info(f"User {get_username_for_display(user_id)} left match in chat {chat_id}. Current players: {player_count}")
            
            if player_count < 2: # Если игроков меньше 2, отменяем поиск
                await query.message.reply_text("Количество игроков слишком мало. Поиск матча отменен.", message_thread_id=match_data.get('message_thread_id'))
                # Отменяем таймаут, если он был активен
                if 'search_timeout_job' in match_data and match_data['search_timeout_job'] is not None:
                    try:
                        match_data['search_timeout_job'].schedule_removal()
                        logger.info(f"Search timeout job for chat {chat_id} removed due to low players.")
                    except Exception as e:
                        logger.warning(f"Error removing search_timeout_job for chat {chat_id} due to low players: {e}")
                    match_data['search_timeout_job'] = None
                
                del GLOBAL_MATCH_FLOW[chat_id]
                delete_active_match(chat_id)
                # Удаляем сообщение с кнопками, если оно еще есть
                try:
                    await query.message.delete()
                    logger.info(f"Deleted match message {match_data.get('message_id')} in chat {chat_id} after cancellation.")
                except Exception as e:
                    logger.warning(f"Could not delete match message {match_data.get('message_id')} in chat {chat_id} on low players: {e}")
                
            else:
                await update_match_message(chat_id, match_data, context, is_initial_message=False)
                await query.answer(f"Вы покинули очередь. Игроков: {player_count}/10")

    # --- Обработка кнопки "Начать игру" (start_game_force) ---
    elif query.data == "start_game_force":
        if current_phase != MATCH_PHASES["SEARCH"]:
            await query.answer("Игра уже находится не в фазе поиска.")
            return

        player_count = len(match_data['players'])
        allowed_player_counts = [2, 4, 6, 8, 10]

        if player_count in allowed_player_counts:
            await query.answer("Запускаем игру с текущим количеством игроков!")
            logger.info(f"User {get_username_for_display(user_id)} forced start of game in chat {chat_id} with {player_count} players.")
            
            # Переход к голосованию за карты
            match_data['current_phase'] = MATCH_PHASES["MAP_VOTE"]
            
            # Отправляем сообщение для выбора карт
            map_keyboard = []
            for map_name in MAPS:
                map_keyboard.append([InlineKeyboardButton(f"{map_name} (0)", callback_data=f"vote_map_{map_name}")])
            map_reply_markup = InlineKeyboardMarkup(map_keyboard)
            
            # Удаляем старое сообщение с кнопками "Присоединиться"
            try:
                await query.message.delete()
                logger.info(f"Deleted old match message {match_data['message_id']} in chat {chat_id} for map vote (forced start).")
            except Exception as e:
                logger.warning(f"Could not delete old match message {match_data['message_id']} in chat {chat_id} for map vote (forced start): {e}")

            sent_message = await context.bot.send_message(
                chat_id=chat_id,
                text="**Голосуйте за карты!**\n\nТекущие голоса:",
                reply_markup=map_reply_markup,
                parse_mode='Markdown',
                message_thread_id=match_data.get('message_thread_id')
            )
            match_data['message_id'] = sent_message.message_id
            logger.info(f"Sent map vote message {match_data['message_id']} in chat {chat_id} after forced start.")
        else:
            await query.answer(f"Недостаточно игроков или неверное количество для начала игры (нужно {', '.join(map(str, allowed_player_counts))}). Текущее: {player_count}.")

    # --- Обработка голосования за карты (vote_map_) ---
    elif query.data.startswith("vote_map_"):
        if current_phase != MATCH_PHASES["MAP_VOTE"]:
            await query.answer("Сейчас нельзя голосовать за карты.")
            return

        map_name = query.data.replace("vote_map_", "")
        if map_name not in MAPS:
            await query.answer("Неизвестная карта.")
            return

        if user_id in match_data['players_voted_map']:
            await query.answer("Вы уже голосовали за карту в этом раунде.")
            return
        
        match_data['map_votes'][map_name] += 1
        match_data['players_voted_map'].append(user_id)
        logger.info(f"User {get_username_for_display(user_id)} voted for map {map_name} in chat {chat_id}. Current votes: {match_data['map_votes'][map_name]}")

        # Обновляем сообщение с голосами
        updated_map_keyboard = []
        for map_n in MAPS:
            votes = match_data['map_votes'][map_n]
            updated_map_keyboard.append([InlineKeyboardButton(f"{map_n} ({votes})", callback_data=f"vote_map_{map_n}")])
        updated_reply_markup = InlineKeyboardMarkup(updated_map_keyboard)

        try:
            await query.message.edit_reply_markup(reply_markup=updated_reply_markup)
            await query.answer(f"Ваш голос за {map_name} учтен.")
            logger.info(f"Edited map vote message {query.message.message_id} in chat {chat_id}.")
        except Exception as e:
            logger.warning(f"Could not edit map vote message {query.message.message_id} in chat {chat_id}: {e}")
            await query.answer("Не удалось обновить голоса, но ваш голос учтен.")

        # Проверяем, все ли игроки проголосовали
        if len(match_data['players_voted_map']) == len(match_data['players']):
            await query.message.reply_text("Все игроки проголосовали за карты! Выбираем лучшую...", message_thread_id=match_data.get('message_thread_id'))
            await process_map_selection(query, chat_id, match_data, context)

    # --- Обработка пиков игроков (pick_player_) ---
    elif query.data.startswith("pick_player_"):
        if current_phase != MATCH_PHASES["CAPTAIN_PICK"]:
            await query.answer("Сейчас нельзя пикать игроков.")
            return

        picked_player_id = int(query.data.replace("pick_player_", ""))
        await process_player_pick(query, chat_id, match_data, picked_player_id, context)

    else:
        await query.answer("Неизвестное действие.")


# --- ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ (для регистрации) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message_thread_id = update.effective_message.message_thread_id # Получаем ID темы
    if user_id in user_registration_states and user_registration_states[user_id] == "waiting_for_username":
        new_username = update.message.text
        # Дополнительная валидация юзернейма, если нужно
        
        add_user(user_id, new_username) # Обновляем юзернейм в БД
        del user_registration_states[user_id] # Удаляем состояние ожидания
        await update.message.reply_text(f"Ваш юзернейм успешно обновлен на **{new_username}**.", parse_mode='Markdown', message_thread_id=message_thread_id)
        logger.info(f"User {user_id} registered/updated username to {new_username}.")
    else:
        # Игнорируем обычные текстовые сообщения, если они не являются частью процесса регистрации
        pass

async def registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message_thread_id = update.effective_message.message_thread_id # Получаем ID темы
    if update.effective_chat.type != "private":
        await update.message.reply_text("Пожалуйста, используйте эту команду в личных сообщениях с ботом.", message_thread_id=message_thread_id)
        return
    
    user_data = get_user_data(user_id)
    if user_data and user_data[1]: # Если юзернейм уже есть
        await update.message.reply_text(f"Ваш текущий юзернейм: **{user_data[1]}**. Если хотите изменить, просто введите новый юзернейм.", parse_mode='Markdown', message_thread_id=message_thread_id)
    else:
        await update.message.reply_text("Пожалуйста, введите юзернейм, который будет отображаться в матчах (например, ваш ник в Faceit или Steam).", message_thread_id=message_thread_id)
    
    user_registration_states[user_id] = "waiting_for_username"
    logger.info(f"User {user_id} entered registration state.")


# --- ГЛАВНАЯ ФУНКЦИЯ ---
def main() -> None:
    init_db()  # Инициализация базы данных при запуске бота
    # Запустите migrate_db() ОДИН РАЗ, если вы добавляете столбец 'rank' в существующую БД.
    # После первого успешного запуска этой миграции, вы можете ее закомментировать.
    migrate_db() 

    application = Application.builder().token(TOKEN).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("rank", rank_command)) # Добавляем команду /rank
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("unban", unban))
    application.add_handler(CommandHandler("add_balance", add_balance))
    application.add_handler(CommandHandler("register", register_match_result))
    application.add_handler(CommandHandler("set_match_id", set_match_id))
    application.add_handler(CommandHandler("check_ban", check_ban))
    application.add_handler(CommandHandler("registration", registration))
    application.add_handler(CommandHandler("find_match", find_match_command))
    application.add_handler(CommandHandler("find_match_debug", find_match_debug_command))
    application.add_handler(CommandHandler("end_match", end_match))

    # Новые админские команды для изменения профиля
    application.add_handler(CommandHandler("set_username", set_username_admin))
    application.add_handler(CommandHandler("set_rank", set_rank_admin))
    application.add_handler(CommandHandler("change_rank", change_rank_admin)) # Добавляем алиас для set_rank_admin
    application.add_handler(CommandHandler("view_profile", view_profile_admin))

    # Добавление обработчиков колбэк-запросов (для кнопок)
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Добавление обработчика для обычных текстовых сообщений (для регистрации никнейма)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запуск бота
    logger.info("Bot started polling.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()