import os
import random
import sqlite3
import threading
from datetime import date

from flask import Flask
from openai import OpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# НАСТРОЙКИ
# =========================================================

TOKEN = os.getenv("TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

DB_FILE = "nexora.db"

START_COINS = 100
DAILY_REWARD = 50

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в Environment Variables")

if OPENAI_API_KEY:
    ai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    ai_client = None


# =========================================================
# FLASK ДЛЯ RENDER
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "NEXORA is running!"


@app.route("/health")
def health():
    return "OK"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                coins INTEGER DEFAULT 100,
                xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                games INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                last_daily TEXT DEFAULT '',
                daily_games INTEGER DEFAULT 0,
                daily_wins INTEGER DEFAULT 0,
                daily_date TEXT DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER,
                item TEXT,
                amount INTEGER DEFAULT 1,
                PRIMARY KEY(user_id, item)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                user_id INTEGER,
                achievement TEXT,
                PRIMARY KEY(user_id, achievement)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned (
                user_id INTEGER PRIMARY KEY
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS quest_claims (
                user_id INTEGER,
                quest TEXT,
                claimed_date TEXT,
                PRIMARY KEY(user_id, quest, claimed_date)
            )
        """)

        conn.commit()
        conn.close()


# =========================================================
# USER FUNCTIONS
# =========================================================

def ensure_user(user):
    today = str(date.today())

    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user.id,)
        )

        row = cur.fetchone()

        if not row:
            cur.execute("""
                INSERT INTO users
                (
                    user_id,
                    username,
                    first_name,
                    coins,
                    xp,
                    level,
                    games,
                    wins,
                    streak,
                    last_daily,
                    daily_games,
                    daily_wins,
                    daily_date
                )
                VALUES (?, ?, ?, ?, 0, 1, 0, 0, 0, '', 0, 0, ?)
            """, (
                user.id,
                user.username or "",
                user.first_name or "",
                START_COINS,
                today
            ))
        else:
            cur.execute("""
                UPDATE users
                SET username = ?, first_name = ?
                WHERE user_id = ?
            """, (
                user.username or "",
                user.first_name or "",
                user.id
            ))

            if row["daily_date"] != today:
                cur.execute("""
                    UPDATE users
                    SET daily_games = 0,
                        daily_wins = 0,
                        daily_date = ?
                    WHERE user_id = ?
                """, (today, user.id))

        conn.commit()
        conn.close()


def get_user(user_id):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        )

        row = cur.fetchone()
        conn.close()

        return row


def is_banned(user_id):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT 1 FROM banned WHERE user_id = ?",
            (user_id,)
        )

        result = cur.fetchone()
        conn.close()

        return result is not None


# =========================================================
# COINS / XP
# =========================================================

def add_coins(user_id, amount):
    with db_lock:
        conn = get_db()
        conn.execute("""
            UPDATE users
            SET coins = coins + ?
            WHERE user_id = ?
        """, (amount, user_id))
        conn.commit()
        conn.close()


def set_coins(user_id, amount):
    with db_lock:
        conn = get_db()
        conn.execute("""
            UPDATE users
            SET coins = ?
            WHERE user_id = ?
        """, (amount, user_id))
        conn.commit()
        conn.close()


def add_xp(user_id, amount):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            "SELECT xp, level FROM users WHERE user_id = ?",
            (user_id,)
        )

        row = cur.fetchone()

        if not row:
            conn.close()
            return 1, False

        old_level = row["level"]
        new_xp = row["xp"] + amount
        new_level = max(1, new_xp // 100 + 1)

        cur.execute("""
            UPDATE users
            SET xp = ?, level = ?
            WHERE user_id = ?
        """, (
            new_xp,
            new_level,
            user_id
        ))

        conn.commit()
        conn.close()

        return new_level, new_level > old_level


def xp_bar(xp):
    current = xp % 100
    filled = current // 10
    empty = 10 - filled

    return "🟩" * filled + "⬜" * empty


# =========================================================
# GAME STATS
# =========================================================

def register_game(user_id, win=False):
    today = str(date.today())

    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            UPDATE users
            SET games = games + 1,
                wins = wins + ?,
                daily_games = daily_games + 1,
                daily_wins = daily_wins + ?,
                daily_date = ?
            WHERE user_id = ?
        """, (
            1 if win else 0,
            1 if win else 0,
            today,
            user_id
        ))

        conn.commit()
        conn.close()


# =========================================================
# DAILY BONUS
# =========================================================

def claim_daily(user_id):
    today = str(date.today())

    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT coins, streak, last_daily
            FROM users
            WHERE user_id = ?
        """, (user_id,))

        row = cur.fetchone()

        if not row:
            conn.close()
            return False, 0, 0

        if row["last_daily"] == today:
            conn.close()
            return False, 0, row["streak"]

        old_streak = row["streak"]

        if old_streak >= 1:
            reward = DAILY_REWARD + min(old_streak * 10, 100)
            new_streak = old_streak + 1
        else:
            reward = DAILY_REWARD
            new_streak = 1

        cur.execute("""
            UPDATE users
            SET coins = coins + ?,
                streak = ?,
                last_daily = ?
            WHERE user_id = ?
        """, (
            reward,
            new_streak,
            today,
            user_id
        ))

        conn.commit()
        conn.close()

        return True, reward, new_streak


# =========================================================
# INVENTORY
# =========================================================

def add_item(user_id, item):
    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT INTO inventory(user_id, item, amount)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, item)
            DO UPDATE SET amount = amount + 1
        """, (
            user_id,
            item
        ))

        conn.commit()
        conn.close()


def get_inventory(user_id):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT item, amount
            FROM inventory
            WHERE user_id = ?
            ORDER BY item
        """, (user_id,))

        rows = cur.fetchall()
        conn.close()

        return rows


# =========================================================
# ACHIEVEMENTS
# =========================================================

ACHIEVEMENTS = {
    "first_game": "🎮 Первая игра",
    "ten_wins": "🏆 10 побед",
    "fifty_games": "🎯 50 игр",
    "streak_7": "🔥 Серия 7 дней",
    "rich": "💰 1000 монет",
    "level_10": "⭐ 10 уровень",
}


def has_achievement(user_id, achievement):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT 1
            FROM achievements
            WHERE user_id = ? AND achievement = ?
        """, (
            user_id,
            achievement
        ))

        result = cur.fetchone()
        conn.close()

        return result is not None


def give_achievement(user_id, achievement):
    if has_achievement(user_id, achievement):
        return False

    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT OR IGNORE INTO achievements(user_id, achievement)
            VALUES (?, ?)
        """, (
            user_id,
            achievement
        ))

        conn.commit()
        conn.close()

    return True


def check_achievements(user_id):
    user = get_user(user_id)

    if not user:
        return []

    unlocked = []

    checks = [
        ("first_game", user["games"] >= 1),
        ("ten_wins", user["wins"] >= 10),
        ("fifty_games", user["games"] >= 50),
        ("streak_7", user["streak"] >= 7),
        ("rich", user["coins"] >= 1000),
        ("level_10", user["level"] >= 10),
    ]

    for achievement, condition in checks:
        if condition and give_achievement(user_id, achievement):
            unlocked.append(ACHIEVEMENTS[achievement])

    return unlocked


# =========================================================
# SHOP
# =========================================================

SHOP = {
    "mystery": {
        "name": "🎁 Mystery Box",
        "price": 500,
    },
    "lucky": {
        "name": "🍀 Lucky Item",
        "price": 300,
    },
    "badge": {
        "name": "👑 VIP Badge",
        "price": 1000,
    },
    "boost": {
        "name": "⚡ XP Boost",
        "price": 250,
    },
}


# =========================================================
# QUESTS
# =========================================================

QUESTS = {
    "play3": {
        "name": "🎮 Сыграть 3 игры",
        "type": "games",
        "target": 3,
        "reward": 30,
    },
    "play7": {
        "name": "🎮 Сыграть 7 игр",
        "type": "games",
        "target": 7,
        "reward": 70,
    },
    "win2": {
        "name": "🏆 Выиграть 2 игры",
        "type": "wins",
        "target": 2,
        "reward": 50,
    },
    "win5": {
        "name": "🏆 Выиграть 5 игр",
        "type": "wins",
        "target": 5,
        "reward": 100,
    },
}


def quest_claimed(user_id, quest):
    today = str(date.today())

    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT 1
            FROM quest_claims
            WHERE user_id = ?
              AND quest = ?
              AND claimed_date = ?
        """, (
            user_id,
            quest,
            today
        ))

        result = cur.fetchone()
        conn.close()

        return result is not None


def claim_quest(user_id, quest):
    if quest_claimed(user_id, quest):
        return False, 0

    q = QUESTS.get(quest)

    if not q:
        return False, 0

    user = get_user(user_id)

    if not user:
        return False, 0

    if q["type"] == "games":
        progress = user["daily_games"]
    else:
        progress = user["daily_wins"]

    if progress < q["target"]:
        return False, 0

    today = str(date.today())

    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT INTO quest_claims
            (user_id, quest, claimed_date)
            VALUES (?, ?, ?)
        """, (
            user_id,
            quest,
            today
        ))

        conn.commit()
        conn.close()

    add_coins(user_id, q["reward"])

    return True, q["reward"]


# =========================================================
# KEYBOARDS
# =========================================================

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton("🎮 Игра", callback_data="games"),
            InlineKeyboardButton("🤖 Nexora AI", callback_data="ai"),
        ],
        [
            InlineKeyboardButton("💰 Мои очки", callback_data="coins"),
            InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        ],
        [
            InlineKeyboardButton("🏆 TOP", callback_data="top"),
            InlineKeyboardButton("🎁 Бонус", callback_data="daily"),
        ],
        [
            InlineKeyboardButton("🛒 Магазин", callback_data="shop"),
            InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory"),
        ],
        [
            InlineKeyboardButton("📋 Задания", callback_data="quests"),
            InlineKeyboardButton("🏅 Достижения", callback_data="achievements"),
        ],
        [
            InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def games_menu():
    keyboard = [
        [
            InlineKeyboardButton("🔢 Угадай число", callback_data="game_guess"),
        ],
        [
            InlineKeyboardButton("✊ Камень", callback_data="game_rps_rock"),
            InlineKeyboardButton("✋ Бумага", callback_data="game_rps_paper"),
            InlineKeyboardButton("✌️ Ножницы", callback_data="game_rps_scissors"),
        ],
        [
            InlineKeyboardButton("🎲 Кубик", callback_data="game_dice"),
            InlineKeyboardButton("⚡ Реакция", callback_data="game_reaction"),
        ],
        [
            InlineKeyboardButton("🔙 Назад", callback_data="back"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def shop_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                f"🎁 Mystery — {SHOP['mystery']['price']} 🪙",
                callback_data="buy_mystery"
            )
        ],
        [
            InlineKeyboardButton(
                f"🍀 Lucky — {SHOP['lucky']['price']} 🪙",
                callback_data="buy_lucky"
            )
        ],
        [
            InlineKeyboardButton(
                f"👑 VIP — {SHOP['badge']['price']} 🪙",
                callback_data="buy_badge"
            )
        ],
        [
            InlineKeyboardButton(
                f"⚡ XP Boost — {SHOP['boost']['price']} 🪙",
                callback_data="buy_boost"
            )
        ],
        [
            InlineKeyboardButton("🔙 Назад", callback_data="back"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    ensure_user(user)

    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 Вы заблокированы."
        )
        return

    await update.message.reply_text(
        f"🚀 <b>Добро пожаловать в NEXORA, {user.first_name}!</b>\n\n"
        "🎮 Играй\n"
        "💰 Зарабатывай монеты\n"
        "🏆 Поднимайся в TOP\n"
        "🎁 Получай ежедневные бонусы\n"
        "🤖 Общайся с Nexora AI\n\n"
        "Выбери действие:",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# PROFILE
# =========================================================

async def show_profile(query):
    user = get_user(query.from_user.id)

    if not user:
        return

    text = (
        f"👤 <b>Профиль {user['first_name']}</b>\n\n"
        f"🪙 Монеты: <b>{user['coins']}</b>\n"
        f"⭐ Уровень: <b>{user['level']}</b>\n"
        f"✨ XP: <b>{user['xp'] % 100}/100</b>\n"
        f"{xp_bar(user['xp'])}\n\n"
        f"🎮 Игр: <b>{user['games']}</b>\n"
        f"🏆 Побед: <b>{user['wins']}</b>\n"
        f"🔥 Серия: <b>{user['streak']}</b>"
    )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# TOP
# =========================================================

async def show_top(query):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT first_name, username, coins, wins, level
            FROM users
            ORDER BY coins DESC
            LIMIT 10
        """)

        rows = cur.fetchall()
        conn.close()

    text = "🏆 <b>NEXORA TOP 10</b>\n\n"

    if not rows:
        text += "Пока никого нет."
    else:
        for i, row in enumerate(rows, 1):
            name = row["first_name"] or row["username"] or "Игрок"

            text += (
                f"<b>{i}.</b> {name} — "
                f"🪙 {row['coins']} | "
                f"🏆 {row['wins']} побед | "
                f"⭐ {row['level']} lvl\n"
            )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# DAILY
# =========================================================

async def show_daily(query):
    success, reward, streak = claim_daily(query.from_user.id)

    if success:
        text = (
            "🎁 <b>Ежедневный бонус получен!</b>\n\n"
            f"🪙 +{reward} монет\n"
            f"🔥 Серия: {streak} дней"
        )
    else:
        text = (
            "⏳ <b>Бонус уже получен сегодня.</b>\n\n"
            f"🔥 Текущая серия: {streak}"
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# COINS
# =========================================================

async def show_coins(query):
    user = get_user(query.from_user.id)

    await query.edit_message_text(
        f"💰 <b>Твои монеты</b>\n\n"
        f"🪙 Баланс: <b>{user['coins']}</b>",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# GAMES
# =========================================================

async def show_games(query):
    await query.edit_message_text(
        "🎮 <b>Выбери игру:</b>",
        parse_mode="HTML",
        reply_markup=games_menu()
    )


async def guess_game(query):
    number = random.randint(1, 10)

    context_data = {
        "number": number,
        "attempts": 5,
    }

    # Сохраняем игру в user_data
    query.message.chat_id

    # Telegram callback не позволяет напрямую передать число.
    # Поэтому создаём состояние через глобальное user_data невозможно
    # в callback-функции без context.
    #
    # Используем простую игру через сообщение с кнопками.

    keyboard = []

    for i in range(1, 11):
        keyboard.append(
            InlineKeyboardButton(
                str(i),
                callback_data=f"guess_{number}_{i}"
            )
        )

    rows = [
        keyboard[i:i + 5]
        for i in range(0, len(keyboard), 5)
    ]

    rows.append([
        InlineKeyboardButton("🔙 Назад", callback_data="games")
    ])

    await query.edit_message_text(
        "🔢 <b>Угадай число от 1 до 10!</b>\n\n"
        "Выбери один вариант:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows)
    )


async def process_guess(query, secret, selected):
    secret = int(secret)
    selected = int(selected)

    user_id = query.from_user.id

    if selected == secret:
        register_game(user_id, True)

        reward = random.randint(15, 30)
        add_coins(user_id, reward)

        new_level, level_up = add_xp(user_id, 50)

        text = (
            "🎉 <b>Ты угадал!</b>\n\n"
            f"🔢 Число: <b>{secret}</b>\n"
            f"🪙 Награда: <b>+{reward}</b>\n"
            f"✨ XP: <b>+50</b>"
        )

        if level_up:
            text += f"\n\n⭐ <b>Новый уровень: {new_level}!</b>"

        unlocked = check_achievements(user_id)

        if unlocked:
            text += "\n\n🏅 <b>Новое достижение:</b>\n"
            text += "\n".join(unlocked)

    else:
        register_game(user_id, False)
        add_xp(user_id, 10)

        if selected < secret:
            hint = "📈 Загаданное число больше."
        else:
            hint = "📉 Загаданное число меньше."

        text = (
            "❌ <b>Не угадал!</b>\n\n"
            f"Твой вариант: {selected}\n"
            f"{hint}\n\n"
            "Попробуй ещё раз!"
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=games_menu()
    )


# =========================================================
# RPS
# =========================================================

async def play_rps(query, player_choice):
    choices = ["rock", "paper", "scissors"]

    bot_choice = random.choice(choices)

    names = {
        "rock": "🪨 Камень",
        "paper": "📄 Бумага",
        "scissors": "✂️ Ножницы",
    }

    if player_choice == bot_choice:
        result = "🤝 Ничья!"
        win = False

    elif (
        player_choice == "rock" and bot_choice == "scissors"
        or player_choice == "paper" and bot_choice == "rock"
        or player_choice == "scissors" and bot_choice == "paper"
    ):
        result = "🎉 Ты победил!"
        win = True
    else:
        result = "😢 Ты проиграл!"
        win = False

    register_game(query.from_user.id, win)

    if win:
        reward = random.randint(10, 25)
        add_coins(query.from_user.id, reward)
        add_xp(query.from_user.id, 30)
    else:
        add_xp(query.from_user.id, 10)

    text = (
        "✊ <b>Камень, ножницы, бумага</b>\n\n"
        f"👤 Ты: {names[player_choice]}\n"
        f"🤖 NEXORA: {names[bot_choice]}\n\n"
        f"<b>{result}</b>"
    )

    if win:
        text += f"\n\n🪙 +{reward} монет"

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=games_menu()
    )


# =========================================================
# DICE
# =========================================================

async def dice_game(query):
    player = random.randint(1, 6)
    bot = random.randint(1, 6)

    if player > bot:
        result = "🎉 Ты победил!"
        win = True
    elif player == bot:
        result = "🤝 Ничья!"
        win = False
    else:
        result = "😢 NEXORA победил!"
        win = False

    register_game(query.from_user.id, win)

    if win:
        reward = random.randint(10, 25)
        add_coins(query.from_user.id, reward)
        add_xp(query.from_user.id, 30)
    else:
        reward = 0
        add_xp(query.from_user.id, 10)

    text = (
        "🎲 <b>Кубик</b>\n\n"
        f"👤 Ты: <b>{player}</b>\n"
        f"🤖 NEXORA: <b>{bot}</b>\n\n"
        f"<b>{result}</b>"
    )

    if reward:
        text += f"\n\n🪙 +{reward} монет"

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=games_menu()
    )


# =========================================================
# REACTION
# =========================================================

async def reaction_game(query):
    reward = random.randint(5, 20)

    register_game(query.from_user.id, True)
    add_coins(query.from_user.id, reward)
    add_xp(query.from_user.id, 20)

    await query.edit_message_text(
        "⚡ <b>РЕАКЦИЯ!</b>\n\n"
        "🔥 Ты успел первым!\n\n"
        f"🪙 +{reward} монет\n"
        "✨ +20 XP",
        parse_mode="HTML",
        reply_markup=games_menu()
    )


# =========================================================
# SHOP
# =========================================================

async def show_shop(query):
    text = (
        "🛒 <b>NEXORA SHOP</b>\n\n"
        "🎁 Mystery Box — 500 🪙\n"
        "🍀 Lucky Item — 300 🪙\n"
        "👑 VIP Badge — 1000 🪙\n"
        "⚡ XP Boost — 250 🪙\n\n"
        "Выбери предмет:"
    )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=shop_menu()
    )


async def buy_item(query, item):
    if item not in SHOP:
        return

    user = get_user(query.from_user.id)

    price = SHOP[item]["price"]

    if user["coins"] < price:
        await query.answer(
            "❌ Недостаточно монет!",
            show_alert=True
        )
        return

    add_coins(query.from_user.id, -price)
    add_item(query.from_user.id, item)

    if item == "mystery":
        bonus = random.randint(100, 700)
        add_coins(query.from_user.id, bonus)

        text = (
            "🎁 <b>Mystery Box открыт!</b>\n\n"
            f"🪙 Дополнительно: +{bonus} монет"
        )

    elif item == "lucky":
        text = (
            "🍀 <b>Lucky Item куплен!</b>\n\n"
            "Удача теперь с тобой!"
        )

    elif item == "badge":
        text = (
            "👑 <b>VIP Badge получен!</b>\n\n"
            "Теперь у тебя есть VIP-предмет."
        )

    elif item == "boost":
        text = (
            "⚡ <b>XP Boost получен!</b>\n\n"
            "Используй его из инвентаря."
        )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=shop_menu()
    )


# =========================================================
# INVENTORY
# =========================================================

async def show_inventory(query):
    rows = get_inventory(query.from_user.id)

    text = "🎒 <b>Твой инвентарь</b>\n\n"

    if not rows:
        text += "Пусто 😢"
    else:
        names = {
            "mystery": "🎁 Mystery Box",
            "lucky": "🍀 Lucky Item",
            "badge": "👑 VIP Badge",
            "boost": "⚡ XP Boost",
        }

        for row in rows:
            name = names.get(row["item"], row["item"])
            text += f"{name} × {row['amount']}\n"

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# QUESTS
# =========================================================

async def show_quests(query):
    user = get_user(query.from_user.id)

    text = "📋 <b>Ежедневные задания</b>\n\n"

    for key, q in QUESTS.items():

        if q["type"] == "games":
            progress = min(user["daily_games"], q["target"])
        else:
            progress = min(user["daily_wins"], q["target"])

        claimed = quest_claimed(query.from_user.id, key)

        if claimed:
            status = "✅ Получено"
        elif progress >= q["target"]:
            status = f"🎁 +{q['reward']} 🪙"
        else:
            status = f"{progress}/{q['target']}"

        text += (
            f"{q['name']}\n"
            f"Прогресс: {status}\n\n"
        )

    keyboard = []

    for key, q in QUESTS.items():
        keyboard.append([
            InlineKeyboardButton(
                f"🎁 Забрать {key}",
                callback_data=f"quest_{key}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("🔙 Назад", callback_data="back")
    ])

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def claim_quest_callback(query, quest):
    success, reward = claim_quest(
        query.from_user.id,
        quest
    )

    if success:
        await query.answer(
            f"🎉 +{reward} монет!",
            show_alert=True
        )
    else:
        await query.answer(
            "❌ Задание ещё не выполнено или уже забрано.",
            show_alert=True
        )

    await show_quests(query)


# =========================================================
# ACHIEVEMENTS MENU
# =========================================================

async def show_achievements(query):
    user_id = query.from_user.id

    text = "🏅 <b>Достижения NEXORA</b>\n\n"

    for key, name in ACHIEVEMENTS.items():
        if has_achievement(user_id, key):
            text += f"✅ {name}\n"
        else:
            text += f"🔒 {name}\n"

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# HELP
# =========================================================

async def show_help(query):
    text = (
        "ℹ️ <b>NEXORA HELP</b>\n\n"
        "🎮 <b>Игра</b> — играй и получай монеты\n"
        "💰 <b>Мои очки</b> — твой баланс\n"
        "👤 <b>Профиль</b> — статистика\n"
        "🏆 <b>TOP</b> — лучшие игроки\n"
        "🎁 <b>Бонус</b> — ежедневная награда\n"
        "🛒 <b>Магазин</b> — покупка предметов\n"
        "🎒 <b>Инвентарь</b> — твои предметы\n"
        "📋 <b>Задания</b> — дополнительные награды\n"
        "🏅 <b>Достижения</b> — твои достижения\n"
        "🤖 <b>Nexora AI</b> — AI-помощник\n\n"
        "💡 Играй каждый день, чтобы увеличивать серию!"
    )

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# AI
# =========================================================

async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    ensure_user(user)

    if is_banned(user.id):
        await update.message.reply_text(
            "🚫 Вы заблокированы."
        )
        return

    if not ai_client:
        await update.message.reply_text(
            "⚠️ Nexora AI пока не настроен.\n\n"
            "Добавь OPENAI_API_KEY в Environment Variables на Render."
        )
        return

    await update.message.reply_text(
        "🤖 <b>Nexora AI активирован!</b>\n\n"
        "Напиши свой вопрос следующим сообщением.",
        parse_mode="HTML"
    )

    context.user_data["waiting_ai"] = True


async def handle_ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not context.user_data.get("waiting_ai"):
        return False

    if not ai_client:
        context.user_data["waiting_ai"] = False

        await update.message.reply_text(
            "❌ OPENAI_API_KEY не найден в Render."
        )

        print("AI ERROR: OPENAI_API_KEY is None")

        return True

    prompt = update.message.text

    if not prompt:
        return True

    context.user_data["waiting_ai"] = False

    wait_message = await update.message.reply_text(
        "🤖 Думаю..."
    )

    try:
        print("AI: отправляю запрос в OpenAI...")
        print("AI KEY EXISTS:", bool(OPENAI_API_KEY))
        print("AI KEY LENGTH:", len(OPENAI_API_KEY) if OPENAI_API_KEY else 0)

        response = ai_client.responses.create(
            model="gpt-5.6",
            input=(
                "Ты — Nexora AI, помощник Telegram-бота NEXORA. "
                "Отвечай понятно, дружелюбно и на русском языке.\n\n"
                f"Сообщение пользователя:\n{prompt}"
            )
        )

        answer = response.output_text

        print("AI: ответ получен")

        if not answer:
            answer = "OpenAI не вернул текст ответа."

        await wait_message.edit_text(
            f"🤖 <b>Nexora AI:</b>\n\n{answer}",
            parse_mode="HTML"
        )

    except Exception as e:
        print("================================")
        print("AI ERROR:")
        print(type(e).__name__)
        print(str(e))
        print("================================")

        await wait_message.edit_text(
            "❌ Ошибка Nexora AI.\n\n"
            "Открой Render → Logs и посмотри строку "
            "AI ERROR."
        )

    return True


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    user = query.from_user

    ensure_user(user)

    if is_banned(user.id):
        try:
            await query.edit_message_text(
                "🚫 Вы заблокированы."
            )
        except Exception:
            pass
        return

    data = query.data

    # -------------------------
    # MAIN
    # -------------------------

    if data == "back":
        await query.edit_message_text(
            "🚀 <b>NEXORA</b>\n\nВыбери действие:",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        return

    if data == "games":
        await show_games(query)
        return

    if data == "profile":
        await show_profile(query)
        return

    if data == "coins":
        await show_coins(query)
        return

    if data == "top":
        await show_top(query)
        return

    if data == "daily":
        await show_daily(query)
        return

    if data == "shop":
        await show_shop(query)
        return

    if data == "inventory":
        await show_inventory(query)
        return

    if data == "quests":
        await show_quests(query)
        return

    if data == "achievements":
        await show_achievements(query)
        return

    if data == "help":
        await show_help(query)
        return

    if data == "ai":
        context.user_data["waiting_ai"] = True

        await query.edit_message_text(
            "🤖 <b>Nexora AI</b>\n\n"
            "Напиши сообщение, и я постараюсь помочь.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Назад",
                        callback_data="back"
                    )
                ]
            ])
        )
        return

    # -------------------------
    # GAMES
    # -------------------------

    if data == "game_guess":
        await guess_game(query)
        return

    if data.startswith("guess_"):
        parts = data.split("_")

        if len(parts) == 3:
            await process_guess(
                query,
                parts[1],
                parts[2]
            )

        return

    if data == "game_dice":
        await dice_game(query)
        return

    if data == "game_reaction":
        await reaction_game(query)
        return

    if data == "game_rps_rock":
        await play_rps(query, "rock")
        return

    if data == "game_rps_paper":
        await play_rps(query, "paper")
        return

    if data == "game_rps_scissors":
        await play_rps(query, "scissors")
        return

    # -------------------------
    # SHOP
    # -------------------------

    if data.startswith("buy_"):
        item = data.replace("buy_", "", 1)
        await buy_item(query, item)
        return

    # -------------------------
    # QUESTS
    # -------------------------

    if data.startswith("quest_"):
        quest = data.replace("quest_", "", 1)
        await claim_quest_callback(query, quest)
        return


# =========================================================
# TEXT MESSAGE HANDLER
# =========================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # Сначала проверяем AI
    handled = await handle_ai_message(
        update,
        context
    )

    if handled:
        return

    text = update.message.text

    if text == "🎮 Игра":
        await update.message.reply_text(
            "🎮 Выбери игру:",
            reply_markup=games_menu()
        )

    elif text == "💰 Мои очки":
        user = get_user(update.effective_user.id)

        await update.message.reply_text(
            f"💰 Твой баланс: <b>{user['coins']}</b> 🪙",
            parse_mode="HTML"
        )

    elif text == "👤 Профиль":
        user = get_user(update.effective_user.id)

        await update.message.reply_text(
            f"👤 <b>{user['first_name']}</b>\n\n"
            f"🪙 Монеты: {user['coins']}\n"
            f"⭐ Уровень: {user['level']}\n"
            f"✨ XP: {user['xp'] % 100}/100\n"
            f"🎮 Игр: {user['games']}\n"
            f"🏆 Побед: {user['wins']}\n"
            f"🔥 Серия: {user['streak']}",
            parse_mode="HTML"
        )

    elif text == "🏆 TOP":
        with db_lock:
            conn = get_db()
            cur = conn.cursor()

            cur.execute("""
                SELECT first_name, coins
                FROM users
                ORDER BY coins DESC
                LIMIT 10
            """)

            rows = cur.fetchall()
            conn.close()

        result = "🏆 <b>TOP 10</b>\n\n"

        for i, row in enumerate(rows, 1):
            result += (
                f"{i}. {row['first_name']} — "
                f"{row['coins']} 🪙\n"
            )

        await update.message.reply_text(
            result,
            parse_mode="HTML"
        )

    elif text == "🎁 Бонус":
        success, reward, streak = claim_daily(
            update.effective_user.id
        )

        if success:
            await update.message.reply_text(
                f"🎁 Бонус получен!\n\n"
                f"🪙 +{reward}\n"
                f"🔥 Серия: {streak}"
            )
        else:
            await update.message.reply_text(
                f"⏳ Бонус уже получен сегодня.\n"
                f"🔥 Серия: {streak}"
            )

    elif text == "🛒 Магазин":
        await update.message.reply_text(
            "🛒 <b>NEXORA SHOP</b>",
            parse_mode="HTML",
            reply_markup=shop_menu()
        )

    elif text == "🎒 Инвентарь":
        rows = get_inventory(update.effective_user.id)

        if not rows:
            text_result = "🎒 Инвентарь пуст."
        else:
            text_result = "🎒 <b>Инвентарь</b>\n\n"

            names = {
                "mystery": "🎁 Mystery Box",
                "lucky": "🍀 Lucky Item",
                "badge": "👑 VIP Badge",
                "boost": "⚡ XP Boost",
            }

            for row in rows:
                text_result += (
                    f"{names.get(row['item'], row['item'])}"
                    f" × {row['amount']}\n"
                )

        await update.message.reply_text(
            text_result,
            parse_mode="HTML"
        )

    elif text == "📋 Задания":
        user = get_user(update.effective_user.id)

        result = "📋 <b>Задания</b>\n\n"

        for key, q in QUESTS.items():

            if q["type"] == "games":
                progress = min(
                    user["daily_games"],
                    q["target"]
                )
            else:
                progress = min(
                    user["daily_wins"],
                    q["target"]
                )

            result += (
                f"{q['name']}\n"
                f"{progress}/{q['target']} "
                f"→ +{q['reward']} 🪙\n\n"
            )

        await update.message.reply_text(
            result,
            parse_mode="HTML"
        )

    elif text == "ℹ️ Помощь":
        await update.message.reply_text(
            "ℹ️ Используй кнопки меню NEXORA.",
            reply_markup=main_menu()
        )

    elif text == "🤖 Nexora AI":
        context.user_data["waiting_ai"] = True

        await update.message.reply_text(
            "🤖 Nexora AI активирован!\n\n"
            "Напиши вопрос."
        )

    elif text == "🏅 Достижения":
        user_id = update.effective_user.id

        result = "🏅 <b>Достижения</b>\n\n"

        for key, name in ACHIEVEMENTS.items():
            result += (
                ("✅ " if has_achievement(user_id, key)
                 else "🔒 ")
                + name
                + "\n"
            )

        await update.message.reply_text(
            result,
            parse_mode="HTML"
        )


# =========================================================
# COMMANDS
# =========================================================

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    user = get_user(update.effective_user.id)

    await update.message.reply_text(
        f"👤 <b>{user['first_name']}</b>\n\n"
        f"🪙 Монеты: {user['coins']}\n"
        f"⭐ Уровень: {user['level']}\n"
        f"✨ XP: {user['xp'] % 100}/100\n"
        f"🎮 Игр: {user['games']}\n"
        f"🏆 Побед: {user['wins']}\n"
        f"🔥 Серия: {user['streak']}",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    success, reward, streak = claim_daily(
        update.effective_user.id
    )

    if success:
        await update.message.reply_text(
            f"🎁 Бонус получен!\n\n"
            f"🪙 +{reward}\n"
            f"🔥 Серия: {streak}"
        )
    else:
        await update.message.reply_text(
            f"⏳ Ты уже забрал бонус сегодня.\n"
            f"🔥 Серия: {streak}"
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    print(
        "BOT ERROR:",
        repr(context.error)
    )


# =========================================================
# MAIN
# =========================================================

def main():
    print("===================================")
    print("       NEXORA STARTING...")
    print("===================================")

    init_db()

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("profile", profile_command)
    )

    application.add_handler(
        CommandHandler("daily", daily_command)
    )

    application.add_handler(
        CommandHandler("ai", ai_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("NEXORA BOT STARTED!")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
