import os
import threading
import asyncio
import random
import sqlite3
from datetime import date, timedelta

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

TOKEN = os.environ["TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)

# ТВОЙ TELEGRAM ID
ADMIN_IDS = {5329061561}

DB_NAME = "nexora.db"

START_COINS = 100
DAILY_REWARD = 50


# =========================================================
# DATABASE
# =========================================================

def get_db():
    con = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False
    )

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")

    return con


def init_db():
    con = get_db()
    cur = con.cursor()

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
            item_id TEXT,
            amount INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, item_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            user_id INTEGER,
            achievement_id TEXT,
            PRIMARY KEY (user_id, achievement_id)
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
            quest_id TEXT,
            quest_date TEXT,
            PRIMARY KEY (user_id, quest_id, quest_date)
        )
    """)

    con.commit()
    con.close()


# =========================================================
# USERS
# =========================================================

def ensure_user(user):
    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    )

    if cur.fetchone():
        cur.execute("""
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
        """, (
            user.username or "",
            user.first_name or "",
            user.id
        ))
    else:
        cur.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                coins
            )
            VALUES (?, ?, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            START_COINS
        ))

    con.commit()
    con.close()


def get_user(user_id):
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        SELECT
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
        FROM users
        WHERE user_id=?
    """, (user_id,))

    result = cur.fetchone()

    con.close()

    return result


def is_banned(user_id):
    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT 1 FROM banned WHERE user_id=?",
        (user_id,)
    )

    result = cur.fetchone() is not None

    con.close()

    return result


# =========================================================
# COINS
# =========================================================

def add_coins(user_id, amount):
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        UPDATE users
        SET coins=MAX(coins+?, 0)
        WHERE user_id=?
    """, (amount, user_id))

    con.commit()
    con.close()


def set_coins(user_id, amount):
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        UPDATE users
        SET coins=?
        WHERE user_id=?
    """, (
        max(0, amount),
        user_id
    ))

    success = cur.rowcount > 0

    con.commit()
    con.close()

    return success


# =========================================================
# XP
# =========================================================

def add_xp(user_id, amount):
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        SELECT xp, level
        FROM users
        WHERE user_id=?
    """, (user_id,))

    row = cur.fetchone()

    if not row:
        con.close()
        return False

    old_level = row[1]
    new_xp = row[0] + max(0, amount)
    new_level = (new_xp // 100) + 1

    cur.execute("""
        UPDATE users
        SET xp=?, level=?
        WHERE user_id=?
    """, (
        new_xp,
        new_level,
        user_id
    ))

    con.commit()
    con.close()

    return new_level > old_level


def xp_bar(xp):
    current = xp % 100
    filled = current // 10

    bar = "🟩" * filled + "⬜" * (10 - filled)

    return bar, current


# =========================================================
# GAME STATS
# =========================================================

def register_game(user_id, win=False):
    today = date.today().isoformat()

    con = get_db()
    cur = con.cursor()

    cur.execute("""
        SELECT daily_date
        FROM users
        WHERE user_id=?
    """, (user_id,))

    row = cur.fetchone()

    if not row:
        con.close()
        return

    if row[0] != today:
        cur.execute("""
            UPDATE users
            SET daily_games=0,
                daily_wins=0,
                daily_date=?
            WHERE user_id=?
        """, (today, user_id))

    cur.execute("""
        UPDATE users
        SET
            games=games+1,
            wins=wins+?,
            daily_games=daily_games+1,
            daily_wins=daily_wins+?
        WHERE user_id=?
    """, (
        1 if win else 0,
        1 if win else 0,
        user_id
    ))

    con.commit()
    con.close()


# =========================================================
# DAILY
# =========================================================

def claim_daily(user_id):
    today = date.today()

    con = get_db()
    cur = con.cursor()

    cur.execute("""
        SELECT last_daily, streak
        FROM users
        WHERE user_id=?
    """, (user_id,))

    row = cur.fetchone()

    if not row:
        con.close()
        return False, 0, 0

    last_daily = row[0]
    streak = row[1] or 0

    if last_daily == today.isoformat():
        con.close()
        return False, 0, streak

    if last_daily:
        try:
            last_date = date.fromisoformat(last_daily)

            if last_date == today - timedelta(days=1):
                streak += 1
            else:
                streak = 1

        except ValueError:
            streak = 1
    else:
        streak = 1

    reward = DAILY_REWARD + min(streak * 10, 150)

    cur.execute("""
        UPDATE users
        SET
            coins=coins+?,
            streak=?,
            last_daily=?
        WHERE user_id=?
    """, (
        reward,
        streak,
        today.isoformat(),
        user_id
    ))

    con.commit()
    con.close()

    return True, reward, streak


# =========================================================
# SHOP
# =========================================================

SHOP = {
    "mystery": ("🎁 Mystery Box", 500),
    "lucky": ("🍀 Lucky Token", 300),
    "badge": ("🏅 Golden Badge", 1000),
    "boost": ("⚡ XP Boost", 250),
}


def buy_item(user_id, item_id):
    if item_id not in SHOP:
        return False, 0

    item_name, price = SHOP[item_id]

    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT coins FROM users WHERE user_id=?",
        (user_id,)
    )

    row = cur.fetchone()

    if not row:
        con.close()
        return False, 0

    if row[0] < price:
        con.close()
        return False, 0

    cur.execute("""
        UPDATE users
        SET coins=coins-?
        WHERE user_id=?
    """, (price, user_id))

    cur.execute("""
        INSERT INTO inventory (
            user_id,
            item_id,
            amount
        )
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, item_id)
        DO UPDATE SET amount=amount+1
    """, (user_id, item_id))

    con.commit()
    con.close()

    bonus = 0

    if item_id == "mystery":
        bonus = random.randint(100, 800)
        add_coins(user_id, bonus)

    return True, bonus


# =========================================================
# ACHIEVEMENTS
# =========================================================

ACHIEVEMENTS = {
    "first_game": (
        "🎮 Первый шаг",
        "Сыграть первую игру",
        50
    ),
    "ten_wins": (
        "🏆 Победитель",
        "Одержать 10 побед",
        200
    ),
    "fifty_games": (
        "🔥 Ветеран",
        "Сыграть 50 игр",
        500
    ),
    "streak_7": (
        "🔥 Неделя",
        "Получить streak 7",
        300
    ),
    "rich": (
        "💰 Богач",
        "Накопить 5000 Coins",
        1000
    ),
    "level_10": (
        "⭐ Level 10",
        "Достичь 10 уровня",
        500
    ),
}


def check_achievements(user_id):
    user = get_user(user_id)

    if not user:
        return []

    games = user[6]
    wins = user[7]
    coins = user[3]
    streak = user[8]
    level = user[5]

    conditions = {
        "first_game": games >= 1,
        "ten_wins": wins >= 10,
        "fifty_games": games >= 50,
        "streak_7": streak >= 7,
        "rich": coins >= 5000,
        "level_10": level >= 10,
    }

    con = get_db()
    cur = con.cursor()

    unlocked = []

    for achievement_id, condition in conditions.items():

        if not condition:
            continue

        cur.execute("""
            SELECT 1
            FROM achievements
            WHERE user_id=? AND achievement_id=?
        """, (
            user_id,
            achievement_id
        ))

        if cur.fetchone():
            continue

        reward = ACHIEVEMENTS[achievement_id][2]

        cur.execute("""
            INSERT INTO achievements (
                user_id,
                achievement_id
            )
            VALUES (?, ?)
        """, (
            user_id,
            achievement_id
        ))

        cur.execute("""
            UPDATE users
            SET coins=coins+?
            WHERE user_id=?
        """, (
            reward,
            user_id
        ))

        unlocked.append(
            (achievement_id, reward)
        )

    con.commit()
    con.close()

    return unlocked


# =========================================================
# QUESTS
# =========================================================

QUESTS = {
    "play3": (
        "🎮 Новичок дня",
        "Сыграть 3 игры",
        "games",
        3,
        100,
        30
    ),
    "play7": (
        "🔥 Активный игрок",
        "Сыграть 7 игр",
        "games",
        7,
        200,
        60
    ),
    "win2": (
        "🏆 Охотник",
        "Одержать 2 победы",
        "wins",
        2,
        150,
        40
    ),
    "win5": (
        "👑 Чемпион",
        "Одержать 5 побед",
        "wins",
        5,
        350,
        80
    ),
}


def quest_progress(user_id, quest):
    user = get_user(user_id)

    if not user:
        return 0

    if quest[2] == "games":
        value = user[10]
    else:
        value = user[11]

    return min(value or 0, quest[3])


def quest_claimed(user_id, quest_id):
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        SELECT 1
        FROM quest_claims
        WHERE user_id=?
        AND quest_id=?
        AND quest_date=?
    """, (
        user_id,
        quest_id,
        date.today().isoformat()
    ))

    result = cur.fetchone() is not None

    con.close()

    return result


def claim_quest(user_id, quest_id):
    if quest_id not in QUESTS:
        return False, "Задание не найдено."

    quest = QUESTS[quest_id]

    if quest_progress(user_id, quest) < quest[3]:
        return False, "Задание ещё не выполнено."

    if quest_claimed(user_id, quest_id):
        return False, "Награда уже получена."

    con = get_db()
    cur = con.cursor()

    try:
        cur.execute("""
            INSERT INTO quest_claims (
                user_id,
                quest_id,
                quest_date
            )
            VALUES (?, ?, ?)
        """, (
            user_id,
            quest_id,
            date.today().isoformat()
        ))

        cur.execute("""
            UPDATE users
            SET
                coins=coins+?,
                xp=xp+?
            WHERE user_id=?
        """, (
            quest[4],
            quest[5],
            user_id
        ))

        con.commit()

    except sqlite3.IntegrityError:
        con.rollback()
        con.close()
        return False, "Награда уже получена."

    con.close()

    return True, (
        f"🪙 +{quest[4]} Coins\n"
        f"✨ +{quest[5]} XP"
    )


# =========================================================
# MENUS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎮 Игры",
                callback_data="games"
            ),
            InlineKeyboardButton(
                "🤖 AI",
                callback_data="ai"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Профиль",
                callback_data="profile"
            ),
            InlineKeyboardButton(
                "🎁 Бонус",
                callback_data="daily"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Задания",
                callback_data="quests"
            ),
            InlineKeyboardButton(
                "🛒 Магазин",
                callback_data="shop"
            )
        ],
        [
            InlineKeyboardButton(
                "🎒 Инвентарь",
                callback_data="inventory"
            ),
            InlineKeyboardButton(
                "🏅 Достижения",
                callback_data="achievements"
            )
        ],
        [
            InlineKeyboardButton(
                "🏆 Coins",
                callback_data="rating"
            ),
            InlineKeyboardButton(
                "⭐ XP",
                callback_data="xprating"
            )
        ]
    ])


def games_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎯 Угадай число",
                callback_data="guess"
            )
        ],
        [
            InlineKeyboardButton(
                "✂️ Камень / Бумага / Ножницы",
                callback_data="rps"
            )
        ],
        [
            InlineKeyboardButton(
                "🎲 Кубик",
                callback_data="dice"
            )
        ],
        [
            InlineKeyboardButton(
                "⚡ Реакция",
                callback_data="reaction"
            )
        ],
        [
            InlineKeyboardButton(
                "🧠 Быстрый выбор",
                callback_data="quick"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Главное меню",
                callback_data="main"
            )
        ]
    ])


def back_button(target="main"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=target
            )
        ]
    ])


# =========================================================
# AI COMMAND
# =========================================================

async def ai_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text(
            "⛔ Твой доступ к боту заблокирован."
        )
        return

    ensure_user(user)

    if not context.args:
        await update.message.reply_text(
            "🤖 Напиши вопрос после команды.\n\n"
            "Пример:\n"
            "/ai расскажи интересный факт"
        )
        return

    prompt = " ".join(context.args)

    try:
        response = client.responses.create(
            model="gpt-5-mini",
            input=prompt
        )

        answer = response.output_text

        if not answer:
            answer = "❌ AI не смог сформировать ответ."

        await update.message.reply_text(
            f"🤖 <b>NEXORA AI</b>\n\n{answer}",
            parse_mode="HTML"
        )

    except Exception as e:
        print("AI ERROR:", repr(e))

        await update.message.reply_text(
            "❌ Не удалось получить ответ от AI.\n\n"
            "Проверь OPENAI_API_KEY на Render."
        )


# =========================================================
# AI BUTTON
# =========================================================

async def ai_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user = query.from_user

    if is_banned(user.id):
        await query.edit_message_text(
            "⛔ Твой доступ к боту заблокирован."
        )
        return

    ensure_user(user)

    context.user_data["ai_mode"] = True

    await query.edit_message_text(
        """
🤖 <b>NEXORA AI</b>

AI режим включён.

Напиши свой вопрос 👇

Например:

💬 Расскажи интересный факт
💬 Объясни Python
💬 Помоги с домашним заданием
💬 Придумай идею для игры

Я отвечу прямо здесь.

Нажми кнопку ниже, чтобы выйти.
""",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Выйти из AI",
                    callback_data="main"
                )
            ]
        ])
    )


# =========================================================
# AI MESSAGE
# =========================================================

async def ai_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not context.user_data.get("ai_mode"):
        return

    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text(
            "⛔ Твой доступ к боту заблокирован."
        )
        return

    ensure_user(user)

    prompt = update.message.text.strip()

    if not prompt:
        return

    try:
        thinking = await update.message.reply_text(
            "🤖 Думаю..."
        )

        response = client.responses.create(
            model="gpt-5-mini",
            input=prompt
        )

        answer = response.output_text

        try:
            await thinking.delete()
        except Exception:
            pass

        if not answer:
            answer = "❌ AI не дал ответ."

        max_length = 4000

        for i in range(0, len(answer), max_length):
            await update.message.reply_text(
                answer[i:i + max_length],
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Выйти из AI",
                            callback_data="main"
                        )
                    ]
                ])
            )

    except Exception as e:
        print("AI CHAT ERROR:", repr(e))

        await update.message.reply_text(
            "❌ Ошибка при обращении к AI.\n\n"
            "Проверь OPENAI_API_KEY на Render."
        )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text(
            "⛔ Твой доступ к боту заблокирован."
        )
        return

    ensure_user(user)

    context.user_data["ai_mode"] = False

    data = get_user(user.id)

    await update.message.reply_text(
        f"""
🔥 <b>NEXORA</b>

Привет, <b>{user.first_name}</b>! 👋

🎮 Мини-игры
🤖 AI
⭐ XP и уровни
🪙 Coins
🎁 Daily
📋 Задания
🛒 Магазин
🏅 Достижения
🏆 Рейтинги

━━━━━━━━━━━━━━

🪙 Coins: <b>{data[3]}</b>
⭐ Level: <b>{data[5]}</b>
✨ XP: <b>{data[4]}</b>
🔥 Streak: <b>{data[8]}</b>
""",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


# =========================================================
# ID
# =========================================================

async def my_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        f"""
🆔 <b>Твой Telegram ID</b>

<code>{update.effective_user.id}</code>

Скопируй этот ID и вставь его
в ADMIN_IDS в bot.py.
""",
        parse_mode="HTML"
    )


# =========================================================
# CALLBACK
# =========================================================

async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user = query.from_user
    user_id = user.id

    try:
        await query.answer()
    except Exception:
        pass

    if is_banned(user_id):
        try:
            await query.edit_message_text(
                "⛔ Твой доступ к боту заблокирован."
            )
        except Exception:
            pass
        return

    ensure_user(user)

    action = query.data

    # =====================================================
    # AI
    # =====================================================

    if action == "ai":
        await ai_button(update, context)
        return

    # =====================================================
    # MAIN
    # =====================================================

    if action == "main":
        context.user_data["ai_mode"] = False

        data = get_user(user_id)

        await query.edit_message_text(
            f"""
🏠 <b>NEXORA</b>

🪙 Coins: <b>{data[3]}</b>
⭐ Level: <b>{data[5]}</b>
✨ XP: <b>{data[4]}</b>
🔥 Streak: <b>{data[8]}</b>

Выбирай раздел 👇
""",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        return

    # =====================================================
    # GAMES
    # =====================================================

    if action == "games":
        await query.edit_message_text(
            """
🎮 <b>ЦЕНТР ИГР</b>

Выбирай игру 👇
""",
            parse_mode="HTML",
            reply_markup=games_menu()
        )
        return

    # =====================================================
    # GUESS
    # =====================================================

    if action == "guess":
        number = random.randint(1, 5)

        context.user_data["guess_number"] = number

        await query.edit_message_text(
            """
🎯 <b>УГАДАЙ ЧИСЛО</b>

Я загадал число от 1 до 5.

Выбирай 👇
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1️⃣", callback_data="guess_1"),
                    InlineKeyboardButton("2️⃣", callback_data="guess_2"),
                    InlineKeyboardButton("3️⃣", callback_data="guess_3"),
                    InlineKeyboardButton("4️⃣", callback_data="guess_4"),
                    InlineKeyboardButton("5️⃣", callback_data="guess_5")
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Игры",
                        callback_data="games"
                    )
                ]
            ])
        )
        return

    if action.startswith("guess_"):
        chosen = int(action.split("_")[1])

        number = context.user_data.pop(
            "guess_number",
            None
        )

        if number is None:
            await query.edit_message_text(
                "❌ Эта игра уже закончилась.",
                reply_markup=back_button("games")
            )
            return

        if chosen == number:
            reward = random.randint(40, 80)

            add_coins(user_id, reward)

            level_up = add_xp(
                user_id,
                30
            )

            register_game(
                user_id,
                True
            )

            text = (
                "🎉 <b>ПОБЕДА!</b>\n\n"
                f"Число: <b>{number}</b>\n\n"
                f"🪙 +{reward} Coins\n"
                "✨ +30 XP"
            )

            if level_up:
                text += "\n\n🎉 <b>НОВЫЙ УРОВЕНЬ!</b>"

        else:
            add_xp(user_id, 10)
            register_game(user_id)

            text = (
                "😢 <b>Не угадал!</b>\n\n"
                f"Правильное число: <b>{number}</b>\n\n"
                "✨ +10 XP"
            )

        unlocked = check_achievements(user_id)

        if unlocked:
            text += "\n\n🏅 <b>Новое достижение!</b>"

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🎯 Ещё раз",
                        callback_data="guess"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎮 Игры",
                        callback_data="games"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Меню",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # RPS
    # =====================================================

    if action == "rps":
        await query.edit_message_text(
            """
✂️ <b>КАМЕНЬ / БУМАГА / НОЖНИЦЫ</b>

Выбирай:
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🪨 Камень",
                        callback_data="rps_rock"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📄 Бумага",
                        callback_data="rps_paper"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✂️ Ножницы",
                        callback_data="rps_scissors"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Игры",
                        callback_data="games"
                    )
                ]
            ])
        )
        return

    if action.startswith("rps_"):
        player = action.replace("rps_", "")

        choices = [
            "rock",
            "paper",
            "scissors"
        ]

        bot_choice = random.choice(choices)

        names = {
            "rock": "🪨 Камень",
            "paper": "📄 Бумага",
            "scissors": "✂️ Ножницы"
        }

        if player == bot_choice:
            result = "🤝 <b>Ничья!</b>"
            add_xp(user_id, 10)
            register_game(user_id)

        elif (
            (player == "rock" and bot_choice == "scissors")
            or
            (player == "paper" and bot_choice == "rock")
            or
            (player == "scissors" and bot_choice == "paper")
        ):
            result = "🎉 <b>Победа!</b>"

            add_coins(user_id, 30)
            add_xp(user_id, 25)
            register_game(user_id, True)

        else:
            result = "😢 <b>Поражение!</b>"

            add_xp(user_id, 5)
            register_game(user_id)

        check_achievements(user_id)

        await query.edit_message_text(
            f"""
✂️ <b>РЕЗУЛЬТАТ</b>

Ты: {names[player]}
Бот: {names[bot_choice]}

{result}
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔄 Ещё раз",
                        callback_data="rps"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎮 Игры",
                        callback_data="games"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Меню",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # DICE
    # =====================================================

    if action == "dice":
        player = random.randint(1, 6)
        bot_roll = random.randint(1, 6)

        if player > bot_roll:
            result = "🎉 <b>Победа!</b>"

            add_coins(user_id, 40)
            add_xp(user_id, 25)
            register_game(user_id, True)

        elif player == bot_roll:
            result = "🤝 <b>Ничья!</b>"

            add_xp(user_id, 10)
            register_game(user_id)

        else:
            result = "😢 <b>Поражение!</b>"

            add_xp(user_id, 5)
            register_game(user_id)

        check_achievements(user_id)

        await query.edit_message_text(
            f"""
🎲 <b>КУБИК</b>

Твой результат: <b>{player}</b>
Бот: <b>{bot_roll}</b>

{result}
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🎲 Ещё раз",
                        callback_data="dice"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎮 Игры",
                        callback_data="games"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Меню",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # REACTION
    # =====================================================

    if action == "reaction":
        correct = random.randint(1, 3)

        context.user_data["reaction"] = correct

        await query.edit_message_text(
            """
⚡ <b>РЕАКЦИЯ</b>

В одной из трёх кнопок
спрятан правильный ответ.

Выбирай быстро 👇
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "1️⃣",
                        callback_data="reaction_1"
                    ),
                    InlineKeyboardButton(
                        "2️⃣",
                        callback_data="reaction_2"
                    ),
                    InlineKeyboardButton(
                        "3️⃣",
                        callback_data="reaction_3"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Игры",
                        callback_data="games"
                    )
                ]
            ])
        )
        return

    if action.startswith("reaction_"):
        chosen = int(action.split("_")[1])

        correct = context.user_data.pop(
            "reaction",
            None
        )

        if correct is None:
            await query.edit_message_text(
                "❌ Эта игра уже закончилась.",
                reply_markup=back_button("games")
            )
            return

        if chosen == correct:
            reward = random.randint(25, 60)

            add_coins(user_id, reward)

            level_up = add_xp(
                user_id,
                20
            )

            register_game(
                user_id,
                True
            )

            text = (
                "⚡ <b>ОТЛИЧНАЯ РЕАКЦИЯ!</b>\n\n"
                f"Правильный ответ: <b>{correct}</b>\n\n"
                f"🪙 +{reward} Coins\n"
                "✨ +20 XP"
            )

            if level_up:
                text += "\n\n🎉 <b>НОВЫЙ УРОВЕНЬ!</b>"

        else:
            add_xp(user_id, 5)
            register_game(user_id)

            text = (
                "😅 <b>Не получилось!</b>\n\n"
                f"Правильный ответ: <b>{correct}</b>\n\n"
                "✨ +5 XP"
            )

        check_achievements(user_id)

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⚡ Ещё раз",
                        callback_data="reaction"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎮 Игры",
                        callback_data="games"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Меню",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # QUICK
    # =====================================================

    if action == "quick":
        correct = random.choice([
            "red",
            "blue",
            "green"
        ])

        context.user_data["quick"] = correct

        color_name = {
            "red": "🔴 КРАСНЫЙ",
            "blue": "🔵 СИНИЙ",
            "green": "🟢 ЗЕЛЁНЫЙ"
        }[correct]

        await query.edit_message_text(
            f"""
🧠 <b>БЫСТРЫЙ ВЫБОР</b>

Запомни цвет:

<b>{color_name}</b>

Теперь выбери его 👇
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔴 Красный",
                        callback_data="quick_red"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔵 Синий",
                        callback_data="quick_blue"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🟢 Зелёный",
                        callback_data="quick_green"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Игры",
                        callback_data="games"
                    )
                ]
            ])
        )
        return

    if action.startswith("quick_"):
        chosen = action.replace("quick_", "")

        correct = context.user_data.pop(
            "quick",
            None
        )

        if correct is None:
            await query.edit_message_text(
                "❌ Эта игра уже закончилась.",
                reply_markup=back_button("games")
            )
            return

        if chosen == correct:
            reward = random.randint(35, 75)

            add_coins(user_id, reward)

            level_up = add_xp(
                user_id,
                35
            )

            register_game(
                user_id,
                True
            )

            text = (
                "🧠 <b>ОТЛИЧНО!</b>\n\n"
                f"Правильный цвет: <b>{correct}</b>\n\n"
                f"🪙 +{reward} Coins\n"
                "✨ +35 XP"
            )

            if level_up:
                text += "\n\n🎉 <b>НОВЫЙ УРОВЕНЬ!</b>"

        else:
            add_xp(user_id, 5)
            register_game(user_id)

            text = (
                "😅 <b>Не угадал!</b>\n\n"
                f"Правильный цвет: <b>{correct}</b>\n\n"
                "✨ +5 XP"
            )

        check_achievements(user_id)

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🧠 Ещё раз",
                        callback_data="quick"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎮 Игры",
                        callback_data="games"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Меню",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # DAILY
    # =====================================================

    if action == "daily":
        success, reward, streak = claim_daily(user_id)

        if success:
            text = (
                "🎁 <b>ЕЖЕДНЕВНЫЙ БОНУС!</b>\n\n"
                f"🪙 +{reward} Coins\n"
                f"🔥 Streak: <b>{streak}</b>"
            )
        else:
            text = (
                "🎁 <b>БОНУС УЖЕ ПОЛУЧЕН</b>\n\n"
                f"🔥 Streak: <b>{streak}</b>\n\n"
                "Возвращайся завтра!"
            )

        check_achievements(user_id)

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_button()
        )
        return

    # =====================================================
    # QUESTS
    # =====================================================

    if action == "quests":
        text = "📋 <b>ЕЖЕДНЕВНЫЕ ЗАДАНИЯ</b>\n\n"
        buttons = []

        for quest_id, quest in QUESTS.items():
            name = quest[0]
            description = quest[1]
            target = quest[3]
            reward = quest[4]
            xp_reward = quest[5]

            progress = quest_progress(
                user_id,
                quest
            )

            claimed = quest_claimed(
                user_id,
                quest_id
            )

            status = (
                "✅ Получено"
                if claimed
                else f"{progress}/{target}"
            )

            text += (
                f"{name}\n"
                f"▫️ {description}\n"
                f"▫️ Прогресс: <b>{status}</b>\n"
                f"▫️ 🪙 {reward} + ✨ {xp_reward} XP\n\n"
            )

            if progress >= target and not claimed:
                buttons.append([
                    InlineKeyboardButton(
                        f"🎁 Забрать {name}",
                        callback_data=f"claim_{quest_id}"
                    )
                ])

        buttons.append([
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="main"
            )
        ])

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # =====================================================
    # CLAIM QUEST
    # =====================================================

    if action.startswith("claim_"):
        quest_id = action.replace("claim_", "")

        success, result = claim_quest(
            user_id,
            quest_id
        )

        try:
            await query.answer(
                "🎉 Награда получена!"
                if success
                else result,
                show_alert=True
            )
        except Exception:
            pass

        if success:
            text = (
                "🎉 <b>НАГРАДА ПОЛУЧЕНА!</b>\n\n"
                f"{result}"
            )
        else:
            text = f"❌ {result}"

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_button("quests")
        )
        return

    # =====================================================
    # SHOP
    # =====================================================

    if action == "shop":
        buttons = []

        for item_id, item in SHOP.items():
            name = item[0]
            price = item[1]

            buttons.append([
                InlineKeyboardButton(
                    f"{name} — {price} 🪙",
                    callback_data=f"buy_{item_id}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data="main"
            )
        ])

        await query.edit_message_text(
            """
🛒 <b>NEXORA SHOP</b>

Выбирай предмет 👇
""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # =====================================================
    # BUY
    # =====================================================

    if action.startswith("buy_"):
        item_id = action.replace("buy_", "")

        success, bonus = buy_item(
            user_id,
            item_id
        )

        if success:
            text = (
                "✅ <b>ПОКУПКА УСПЕШНА!</b>\n\n"
                f"{SHOP[item_id][0]}"
            )

            if bonus:
                text += (
                    f"\n\n🎁 Mystery Box:\n"
                    f"🪙 +{bonus}"
                )
        else:
            text = (
                "❌ <b>Не удалось купить.</b>\n\n"
                "Возможно, недостаточно Coins."
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🛒 Магазин",
                        callback_data="shop"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Меню",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # INVENTORY
    # =====================================================

    if action == "inventory":
        con = get_db()
        cur = con.cursor()

        cur.execute("""
            SELECT item_id, amount
            FROM inventory
            WHERE user_id=?
        """, (user_id,))

        items = cur.fetchall()

        con.close()

        text = "🎒 <b>ИНВЕНТАРЬ</b>\n\n"

        if not items:
            text += "Пока пусто."
        else:
            for item_id, amount in items:
                if item_id in SHOP:
                    text += (
                        f"{SHOP[item_id][0]} "
                        f"× <b>{amount}</b>\n"
                    )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_button()
        )
        return

    # =====================================================
    # ACHIEVEMENTS
    # =====================================================

    if action == "achievements":
        con = get_db()
        cur = con.cursor()

        cur.execute("""
            SELECT achievement_id
            FROM achievements
            WHERE user_id=?
        """, (user_id,))

        unlocked = {
            row[0]
            for row in cur.fetchall()
        }

        con.close()

        text = "🏅 <b>ДОСТИЖЕНИЯ</b>\n\n"

        for achievement_id, data in ACHIEVEMENTS.items():
            name = data[0]
            description = data[1]
            reward = data[2]

            status = (
                "✅"
                if achievement_id in unlocked
                else "🔒"
            )

            text += (
                f"{status} {name}\n"
                f"{description}\n"
                f"🪙 Награда: {reward}\n\n"
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_button()
        )
        return

    # =====================================================
    # PROFILE
    # =====================================================

    if action == "profile":
        user_data = get_user(user_id)

        bar, current_xp = xp_bar(
            user_data[4]
        )

        games = user_data[6]
        wins = user_data[7]

        winrate = (
            round(wins / games * 100, 1)
            if games
            else 0
        )

        username = (
            f"@{user_data[1]}"
            if user_data[1]
            else "не указан"
        )

        await query.edit_message_text(
            f"""
👤 <b>ПРОФИЛЬ</b>

👤 {user_data[2]}
🔗 {username}

━━━━━━━━━━━━━━

⭐ Уровень: <b>{user_data[5]}</b>

✨ XP:
{bar}
<b>{current_xp}/100</b>

━━━━━━━━━━━━━━

🪙 Coins: <b>{user_data[3]}</b>

🎮 Игр: <b>{games}</b>
🏆 Побед: <b>{wins}</b>
📈 Винрейт: <b>{winrate}%</b>

🔥 Streak: <b>{user_data[8]}</b>
""",
            parse_mode="HTML",
            reply_markup=back_button()
        )
        return

    # =====================================================
    # COINS RATING
    # =====================================================

    if action == "rating":
        con = get_db()
        cur = con.cursor()

        cur.execute("""
            SELECT first_name, coins
            FROM users
            ORDER BY coins DESC
            LIMIT 10
        """)

        players = cur.fetchall()

        con.close()

        medals = ["🥇", "🥈", "🥉"]

        text = "🏆 <b>ТОП ПО COINS</b>\n\n"

        for index, player in enumerate(players):
            name = player[0]
            coins = player[1]

            prefix = (
                medals[index]
                if index < 3
                else f"{index + 1}."
            )

            text += (
                f"{prefix} <b>{name}</b> — "
                f"🪙 {coins}\n"
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⭐ Топ XP",
                        callback_data="xprating"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data="main"
                    )
                ]
            ])
        )
        return

    # =====================================================
    # XP RATING
    # =====================================================

    if action == "xprating":
        con = get_db()
        cur = con.cursor()

        cur.execute("""
            SELECT first_name, xp, level
            FROM users
            ORDER BY xp DESC
            LIMIT 10
        """)

        players = cur.fetchall()

        con.close()

        medals = ["🥇", "🥈", "🥉"]

        text = "⭐ <b>ТОП ПО XP</b>\n\n"

        for index, player in enumerate(players):
            name = player[0]
            xp = player[1]
            level = player[2]

            prefix = (
                medals[index]
                if index < 3
                else f"{index + 1}."
            )

            text += (
                f"{prefix} <b>{name}</b>\n"
                f"⭐ Level {level} | ✨ {xp} XP\n\n"
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🏆 Топ Coins",
                        callback_data="rating"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data="main"
                    )
                ]
            ])
        )
        return


# =========================================================
# ADMIN
# =========================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


async def admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    await update.message.reply_text(
        """
👑 <b>NEXORA ADMIN PANEL</b>

📊 /stats
👥 /users

💰 /give ID сумма
💰 /take ID сумма
💰 /setcoins ID сумма

🔒 /ban ID
🔓 /unban ID

📢 /broadcast текст

🆔 /id
""",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN STATS
# =========================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    con = get_db()
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM banned")
    bans = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(SUM(coins), 0) FROM users"
    )
    coins = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(SUM(games), 0) FROM users"
    )
    games = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(SUM(wins), 0) FROM users"
    )
    wins = cur.fetchone()[0]

    con.close()

    await update.message.reply_text(
        f"""
📊 <b>СТАТИСТИКА NEXORA</b>

👥 Пользователей: <b>{users}</b>
🚫 Заблокировано: <b>{bans}</b>

🪙 Всего Coins: <b>{coins}</b>

🎮 Всего игр: <b>{games}</b>
🏆 Всего побед: <b>{wins}</b>
""",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN USERS
# =========================================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    con = get_db()
    cur = con.cursor()

    cur.execute("""
        SELECT
            user_id,
            first_name,
            username,
            coins,
            level
        FROM users
        ORDER BY coins DESC
        LIMIT 30
    """)

    users = cur.fetchall()

    con.close()

    text = "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\n"

    for index, user in enumerate(users, start=1):
        uid = user[0]
        name = user[1]
        username = user[2]
        coins = user[3]
        level = user[4]

        text += (
            f"<b>{index}. {name}</b>\n"
            f"🆔 <code>{uid}</code>\n"
            f"🪙 {coins} | ⭐ {level}\n"
        )

        if username:
            text += f"🔗 @{username}\n"

        text += "\n"

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )


# =========================================================
# ADMIN GIVE
# =========================================================

async def give(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "/give ID сумма"
        )
        return

    try:
        user_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text(
            "❌ ID и сумма должны быть числами."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ Сумма должна быть больше 0."
        )
        return

    if not get_user(user_id):
        await update.message.reply_text(
            "❌ Пользователь не найден."
        )
        return

    add_coins(user_id, amount)

    await update.message.reply_text(
        f"""
✅ <b>Coins выданы</b>

👤 ID: <code>{user_id}</code>
🪙 +{amount}
""",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN TAKE
# =========================================================

async def take(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "/take ID сумма"
        )
        return

    try:
        user_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text(
            "❌ ID и сумма должны быть числами."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ Сумма должна быть больше 0."
        )
        return

    if not get_user(user_id):
        await update.message.reply_text(
            "❌ Пользователь не найден."
        )
        return

    add_coins(user_id, -amount)

    await update.message.reply_text(
        f"""
✅ <b>Coins сняты</b>

👤 ID: <code>{user_id}</code>
🪙 -{amount}
""",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN SETCOINS
# =========================================================

async def setcoins_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "/setcoins ID сумма"
        )
        return

    try:
        user_id = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text(
            "❌ ID и сумма должны быть числами."
        )
        return

    if amount < 0:
        await update.message.reply_text(
            "❌ Сумма не может быть отрицательной."
        )
        return

    if not set_coins(user_id, amount):
        await update.message.reply_text(
            "❌ Пользователь не найден."
        )
        return

    await update.message.reply_text(
        f"""
✅ <b>Баланс изменён</b>

👤 ID: <code>{user_id}</code>
🪙 Новый баланс: <b>{amount}</b>
""",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN BAN
# =========================================================

async def ban_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "/ban ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ ID должен быть числом."
        )
        return

    if user_id in ADMIN_IDS:
        await update.message.reply_text(
            "❌ Нельзя заблокировать администратора."
        )
        return

    con = get_db()
    cur = con.cursor()

    cur.execute("""
        INSERT OR IGNORE INTO banned(user_id)
        VALUES (?)
    """, (user_id,))

    con.commit()
    con.close()

    await update.message.reply_text(
        f"""
🔒 <b>Пользователь заблокирован</b>

ID: <code>{user_id}</code>
""",
        parse_mode="HTML"
    )


# =========================================================
# ADMIN UNBAN
# =========================================================

async def unban_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "/unban ID"
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ ID должен быть числом."
        )
        return

    con = get_db()
    cur = con.cursor()

    cur.execute(
        "DELETE FROM banned WHERE user_id=?",
        (user_id,)
    )

    changed = cur.rowcount > 0

    con.commit()
    con.close()

    if changed:
        await update.message.reply_text(
            "✅ Пользователь разблокирован."
        )
    else:
        await update.message.reply_text(
            "ℹ️ Пользователь не был заблокирован."
        )


# =========================================================
# ADMIN BROADCAST
# =========================================================

async def broadcast(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "⛔ Нет доступа."
        )
        return

    if not context.args:
        await update.message.reply_text(
            "/broadcast текст"
        )
        return

    message = " ".join(context.args)

    con = get_db()
    cur = con.cursor()

    cur.execute("SELECT user_id FROM users")

    user_ids = [
        row[0]
        for row in cur.fetchall()
    ]

    con.close()

    await update.message.reply_text(
        "📢 Начинаю рассылку..."
    )

    sent = 0
    failed = 0

    for user_id in user_ids:

        if is_banned(user_id):
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=message
            )

            sent += 1

        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    await update.message.reply_text(
        f"""
📢 <b>РАССЫЛКА ЗАКОНЧЕНА</b>

✅ Отправлено: <b>{sent}</b>
❌ Ошибок: <b>{failed}</b>
""",
        parse_mode="HTML"
    )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):
    error = context.error

    if "Query is too old" in str(error):
        return

    print(
        "BOT ERROR:",
        repr(error)
    )


# =========================================================
# RENDER WEB SERVER
# =========================================================

web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "NEXORA BOT IS RUNNING", 200


@web_app.route("/health")
def health():
    return "OK", 200


def run_web_server():
    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    web_app.run(
        host="0.0.0.0",
        port=port
    )


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True
    )

    web_thread.start()

    application = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # =====================================================
    # USER COMMANDS
    # =====================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "id",
            my_id
        )
    )

    application.add_handler(
        CommandHandler(
            "ai",
            ai_command
        )
    )

    # =====================================================
    # ADMIN COMMANDS
    # =====================================================

    application.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    application.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    application.add_handler(
        CommandHandler(
            "users",
            users_command
        )
    )

    application.add_handler(
        CommandHandler(
            "give",
            give
        )
    )

    application.add_handler(
        CommandHandler(
            "take",
            take
        )
    )

    application.add_handler(
        CommandHandler(
            "setcoins",
            setcoins_command
        )
    )

    application.add_handler(
        CommandHandler(
            "ban",
            ban_command
        )
    )

    application.add_handler(
        CommandHandler(
            "unban",
            unban_command
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast",
            broadcast
        )
    )

    # =====================================================
    # AI TEXT
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            ai_message
        )
    )

    # =====================================================
    # CALLBACK BUTTONS
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # =====================================================
    # ERRORS
    # =====================================================

    application.add_error_handler(
        error_handler
    )

    print(
        "🔥 NEXORA BOT STARTED"
    )

    application.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
