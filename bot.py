from pathlib import Path

import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================================================
# CONFIG
# =========================================================
# IMPORTANT:
# Put secrets into environment variables.
#
# Windows local:
#   setx BOT_TOKEN "YOUR_NEW_TOKEN"
#   setx ADMIN_ID "123456789"
#   setx ADMIN_PIN "YOUR_PIN"
#   setx PAYMENT_CARD "YOUR_CARD"
#   setx PAYMENT_OWNER "OWNER_NAME"
#
# Render:
# Add the same variables in Environment.
#
# The old token from your previous code was exposed. Revoke it
# in @BotFather and create a NEW token before deploying.
# =========================================================

BOT_TOKEN = os.getenv("8902562007:AAFN5vq84c6ntVSBtWfnTAiAJwZTVv5IimM", "8902562007:AAFN5vq84c6ntVSBtWfnTAiAJwZTVv5IimM").strip()
ADMIN_ID = int(os.getenv("8972505646", "8972505646"))
ADMIN_PIN = os.getenv("jasur.2011", "jasur.2011").strip()

PAYMENT_CARD = os.getenv("5614 6812 8226 6067", "5614 6812 8226 6067").strip()
PAYMENT_OWNER = os.getenv("K.M", "K.M").strip()

DB_NAME = os.getenv("DB_NAME", "kino_bot.db")
TZ = ZoneInfo("Asia/Tashkent")

PLANS = {
    "1": {"name": "1 kunlik Premium", "days": 1, "price": 3000},
    "7": {"name": "7 kunlik Premium", "days": 7, "price": 15000},
    "10": {"name": "10 kunlik Premium", "days": 10, "price": 20000},
    "30": {"name": "30 kunlik Premium", "days": 30, "price": 49000},
    "365": {"name": "1 yillik Premium", "days": 365, "price": 99000},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN sozlanmagan. Yangi tokenni @BotFather orqali oling "
        "va BOT_TOKEN environment variable ga yozing."
    )

if ADMIN_ID <= 0:
    raise RuntimeError(
        "ADMIN_ID noto'g'ri. Render/Windows Environment Variables ichida "
        "ADMIN_ID ni Telegram raqamingiz bilan yozing."
    )

if not ADMIN_PIN:
    raise RuntimeError(
        "ADMIN_PIN sozlanmagan. Environment Variables ichida ADMIN_PIN yarating."
    )

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Runtime state
ADMIN_STATE = {}
USER_PAYMENT_PLAN = {}


# =========================================================
# DATABASE
# =========================================================
def db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            premium_until TEXT,
            created_at TEXT NOT NULL,
            total_movies INTEGER DEFAULT 0,
            last_seen TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            file_id TEXT NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            plan_key TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            receipt_file_id TEXT NOT NULL,
            receipt_type TEXT DEFAULT 'photo',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            approved_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_stats (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        )
    """)

    # =====================================================
    # REQUIRED SUBSCRIPTION CHANNELS
    # =====================================================
    cur.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            title TEXT NOT NULL,
            invite_link TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    # Upgrade old databases without deleting data.
    ensure_column("payments", "approved_at", "TEXT")


def ensure_column(table, column, definition):
    conn = db()
    cur = conn.cursor()

    cur.execute(f"PRAGMA table_info({table})")
    columns = {row["name"] for row in cur.fetchall()}

    if column not in columns:
        cur.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )
        conn.commit()

    conn.close()


# =========================================================
# GENERAL HELPERS
# =========================================================
def now():
    return datetime.now(TZ)


def now_text():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)

        return dt
    except Exception:
        return None


def fmt_dt(value):
    dt = parse_dt(value) if isinstance(value, str) else value

    if not dt:
        return "-"

    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def esc(value):
    return html.escape(str(value or ""))


def money(value):
    return f"{int(value):,}".replace(",", " ")


def normalize_code(value):
    return str(value or "").strip().upper()


def username_text(user):
    if user.username:
        return f"@{esc(user.username)}"

    return "Username yo'q"


# =========================================================
# STATS
# =========================================================
def stat_add(key, amount=1):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO bot_stats(key, value)
        VALUES(?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value = value + excluded.value
    """, (key, amount))

    conn.commit()
    conn.close()


def stat_get(key):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT value FROM bot_stats WHERE key=?",
        (key,),
    )

    row = cur.fetchone()
    conn.close()

    return int(row["value"]) if row else 0


# =========================================================
# USERS
# =========================================================
def get_user(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,),
    )

    row = cur.fetchone()
    conn.close()

    return row


def create_or_update_user(message: Message):
    user = message.from_user
    existing = get_user(user.id)

    conn = db()
    cur = conn.cursor()

    if existing:
        cur.execute("""
            UPDATE users
            SET first_name=?,
                username=?,
                last_seen=?
            WHERE id=?
        """, (
            user.first_name or "User",
            user.username,
            now_text(),
            user.id,
        ))

        conn.commit()
        conn.close()

        return False

    cur.execute("""
        INSERT INTO users(
            id,
            first_name,
            username,
            premium_until,
            created_at,
            total_movies,
            last_seen
        )
        VALUES(?, ?, ?, NULL, ?, 0, ?)
    """, (
        user.id,
        user.first_name or "User",
        user.username,
        now_text(),
        now_text(),
    ))

    conn.commit()
    conn.close()

    stat_add("new_users")

    return True


def all_user_ids():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM users ORDER BY id"
    )

    rows = cur.fetchall()
    conn.close()

    return [int(row["id"]) for row in rows]


def find_user(value):
    value = str(value or "").strip()

    if not value:
        return None

    if value.isdigit():
        user = get_user(int(value))
        return int(value) if user else None

    username = value.replace("@", "").strip().lower()

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id
        FROM users
        WHERE LOWER(COALESCE(username,''))=?
    """, (username,))

    row = cur.fetchone()
    conn.close()

    return int(row["id"]) if row else None


def count_users():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) c FROM users")
    result = cur.fetchone()["c"]

    conn.close()

    return result


def count_active_premium():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) c
        FROM users
        WHERE premium_until IS NOT NULL
          AND premium_until > ?
    """, (now().isoformat(),))

    result = cur.fetchone()["c"]
    conn.close()

    return result


# =========================================================
# PREMIUM
# =========================================================
def premium_until(user_id):
    user = get_user(user_id)
    return user["premium_until"] if user else None


def is_premium(user_id):
    dt = parse_dt(premium_until(user_id))
    return bool(dt and dt > now())


def premium_days_left(user_id):
    dt = parse_dt(premium_until(user_id))

    if not dt or dt <= now():
        return 0

    seconds = (dt - now()).total_seconds()

    return max(1, int((seconds + 86399) // 86400))


def activate_premium(user_id, days):
    user = get_user(user_id)

    if not user:
        return None

    current = now()
    old = parse_dt(user["premium_until"])

    base = old if old and old > current else current
    new_until = base + timedelta(days=days)

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET premium_until=?,
            last_seen=?
        WHERE id=?
    """, (
        new_until.isoformat(),
        now_text(),
        user_id,
    ))

    conn.commit()
    conn.close()

    return new_until


# =========================================================
# MOVIES
# =========================================================
def movie_exists(code):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM movies WHERE code=?",
        (normalize_code(code),),
    )

    row = cur.fetchone()
    conn.close()

    return row is not None


def get_movie(code):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM movies WHERE code=?",
        (normalize_code(code),),
    )

    row = cur.fetchone()
    conn.close()

    return row


def add_movie(code, name, file_id, category):
    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO movies(
                code,
                name,
                file_id,
                category,
                created_at
            )
            VALUES(?,?,?,?,?)
        """, (
            normalize_code(code),
            name.strip(),
            file_id,
            category,
            now_text(),
        ))

        movie_id = cur.lastrowid

        conn.commit()
        conn.close()

        return movie_id

    except sqlite3.IntegrityError:
        conn.close()
        return None


def delete_movie(code):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT name FROM movies WHERE code=?",
        (normalize_code(code),),
    )

    row = cur.fetchone()

    if not row:
        conn.close()
        return None

    cur.execute(
        "DELETE FROM movies WHERE code=?",
        (normalize_code(code),),
    )

    conn.commit()
    conn.close()

    return row["name"]


def movie_counts():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) c FROM movies")
    total = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) c
        FROM movies
        WHERE category='free'
    """)
    free = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) c
        FROM movies
        WHERE category='premium'
    """)
    premium = cur.fetchone()["c"]

    conn.close()

    return total, free, premium


def increment_movie_stat(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET total_movies=total_movies+1,
            last_seen=?
        WHERE id=?
    """, (
        now_text(),
        user_id,
    ))

    conn.commit()
    conn.close()

    stat_add("movies_sent")


# =========================================================
# REQUIRED SUBSCRIPTION DATABASE
# =========================================================
def all_required_channels():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM required_channels
        ORDER BY id ASC
    """)

    rows = cur.fetchall()
    conn.close()

    return rows


def get_required_channel(channel_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM required_channels
        WHERE id=?
    """, (channel_id,))

    row = cur.fetchone()
    conn.close()

    return row


def add_required_channel(
    chat_id,
    username,
    title,
    invite_link=None,
):
    conn = db()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO required_channels(
                chat_id,
                username,
                title,
                invite_link,
                created_at
            )
            VALUES(?,?,?,?,?)
        """, (
            int(chat_id),
            username,
            title,
            invite_link,
            now_text(),
        ))

        row_id = cur.lastrowid

        conn.commit()
        conn.close()

        return row_id

    except sqlite3.IntegrityError:
        conn.close()
        return None


def delete_required_channel_by_id(channel_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "DELETE FROM required_channels WHERE id=?",
        (channel_id,),
    )

    changed = cur.rowcount

    conn.commit()
    conn.close()

    return changed == 1


def channel_join_url(channel):
    invite_link = channel["invite_link"]
    username = channel["username"]

    if invite_link:
        return invite_link

    if username:
        return f"https://t.me/{str(username).lstrip('@')}"

    return None


# =========================================================
# REQUIRED SUBSCRIPTION CHECK
# =========================================================
async def is_user_subscribed(channel, user_id):
    try:
        member = await bot.get_chat_member(
            chat_id=channel["chat_id"],
            user_id=user_id,
        )

        if member.status in (
            "member",
            "administrator",
            "creator",
        ):
            return True

        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))

        return False

    except TelegramBadRequest as exc:
        logging.warning(
            "Subscription check failed | channel=%s | user=%s | %s",
            channel["chat_id"],
            user_id,
            exc,
        )
        return False

    except TelegramForbiddenError as exc:
        logging.warning(
            "Bot has no access to channel | channel=%s | %s",
            channel["chat_id"],
            exc,
        )
        return False

    except Exception:
        logging.exception(
            "Unexpected subscription check error | channel=%s | user=%s",
            channel["chat_id"],
            user_id,
        )
        return False


async def get_missing_required_channels(user_id):
    missing = []

    for channel in all_required_channels():
        subscribed = await is_user_subscribed(
            channel,
            user_id,
        )

        if not subscribed:
            missing.append(channel)

    return missing


def subscription_keyboard(channels):
    rows = []

    for channel in channels:
        url = channel_join_url(channel)

        if url:
            rows.append([
                InlineKeyboardButton(
                    text=f"📢 {channel['title']}",
                    url=url,
                )
            ])

    rows.append([
        InlineKeyboardButton(
            text="✅ Obunani tekshirish",
            callback_data="check_subscription",
        )
    ])

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


async def require_subscription(message: Message):
    channels = all_required_channels()

    # No required channels configured = normal bot behavior.
    if not channels:
        return True

    missing = await get_missing_required_channels(
        message.from_user.id
    )

    if not missing:
        return True

    await message.answer(
        "🔒 <b>MAJBURIY OBUNA</b>\n\n"
        "Kino olish uchun quyidagi kanallarga obuna bo'ling:\n\n"
        "1️⃣ Kanalga kiring\n"
        "2️⃣ <b>Obuna bo'lish</b> tugmasini bosing\n"
        "3️⃣ Pastdagi <b>✅ Obunani tekshirish</b> tugmasini bosing\n\n"
        "⚠️ Barcha majburiy kanallarga obuna bo'lmaguningizcha "
        "kino berilmaydi.",
        reply_markup=subscription_keyboard(missing),
    )

    return False


@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    create_user = get_user(callback.from_user.id)

    if not create_user:
        # Callback has no Message object suitable for create_or_update_user,
        # so insert/update manually through a small helper message is avoided.
        pass

    missing = await get_missing_required_channels(
        callback.from_user.id
    )

    if missing:
        await callback.answer(
            "❌ Hali barcha kanallarga obuna bo'lmagansiz.",
            show_alert=True,
        )

        try:
            await callback.message.edit_reply_markup(
                reply_markup=subscription_keyboard(missing)
            )
        except Exception:
            pass

        return

    await callback.answer(
        "✅ Obuna tasdiqlandi!",
        show_alert=True,
    )

    await callback.message.answer(
        "✅ <b>Barcha majburiy kanallarga obuna bo'lgansiz.</b>\n\n"
        "Endi kino kodini yuborishingiz mumkin."
    )


# =========================================================
# PAYMENTS
# =========================================================
def create_payment(
    user_id,
    username,
    first_name,
    plan_key,
    file_id,
    receipt_type,
):
    plan = PLANS.get(plan_key)

    if not plan:
        return None

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO payments(
            user_id,
            username,
            first_name,
            plan_key,
            plan_name,
            price,
            receipt_file_id,
            receipt_type,
            status,
            created_at
        )
        VALUES(?,?,?,?,?,?,?,?, 'pending', ?)
    """, (
        user_id,
        username,
        first_name,
        plan_key,
        plan["name"],
        plan["price"],
        file_id,
        receipt_type,
        now_text(),
    ))

    payment_id = cur.lastrowid

    conn.commit()
    conn.close()

    stat_add("receipts")

    return payment_id


def get_payment(payment_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,),
    )

    row = cur.fetchone()
    conn.close()

    return row


def approve_payment(payment_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,),
    )

    row = cur.fetchone()

    if not row:
        conn.close()
        return None

    if row["status"] == "approved":
        conn.close()
        return "already"

    if row["status"] != "pending":
        conn.close()
        return "processed"

    cur.execute("""
        UPDATE payments
        SET status='approved',
            approved_at=?
        WHERE id=?
          AND status='pending'
    """, (
        now_text(),
        payment_id,
    ))

    changed = cur.rowcount

    conn.commit()
    conn.close()

    return row if changed == 1 else "processed"


def reject_payment(payment_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE payments
        SET status='rejected'
        WHERE id=?
          AND status='pending'
    """, (payment_id,))

    changed = cur.rowcount

    conn.commit()
    conn.close()

    return changed == 1


def payment_stats():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) c
        FROM payments
        WHERE status='pending'
    """)
    pending = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) c
        FROM payments
        WHERE status='approved'
    """)
    approved = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) c
        FROM payments
        WHERE status='rejected'
    """)
    rejected = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(DISTINCT user_id) c
        FROM payments
        WHERE status='approved'
    """)
    paid_users = cur.fetchone()["c"]

    cur.execute("""
        SELECT COALESCE(SUM(price),0) s
        FROM payments
        WHERE status='approved'
    """)
    revenue = cur.fetchone()["s"]

    conn.close()

    return (
        pending,
        approved,
        rejected,
        paid_users,
        revenue,
    )


# =========================================================
# ADMIN AUTH
# =========================================================
def is_admin(user_id):
    return int(user_id) == int(ADMIN_ID)


def admin_ok(user_id):
    state = ADMIN_STATE.get(user_id)

    return bool(
        state and
        state.get("authenticated")
    )


def require_admin(obj):
    uid = obj.from_user.id

    return (
        is_admin(uid)
        and admin_ok(uid)
    )


# =========================================================
# KEYBOARDS
# =========================================================
def user_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔎 Kino qidirish",
                callback_data="user_search",
            )
        ],
        [
            InlineKeyboardButton(
                text="💎 Premium",
                callback_data="user_premium",
            ),
            InlineKeyboardButton(
                text="👤 Profil",
                callback_data="user_profile",
            ),
        ],
    ])


def premium_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💎 1 kun • 3 000 so'm",
                callback_data="plan_1",
            )
        ],
        [
            InlineKeyboardButton(
                text="💎 7 kun • 15 000 so'm",
                callback_data="plan_7",
            )
        ],
        [
            InlineKeyboardButton(
                text="💎 10 kun • 20 000 so'm",
                callback_data="plan_10",
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐ 30 kun • 49 000 so'm",
                callback_data="plan_30",
            )
        ],
        [
            InlineKeyboardButton(
                text="👑 1 yil • 99 000 so'm",
                callback_data="plan_365",
            )
        ],
    ])


def category_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🆓 Oddiy kino",
                callback_data="category_free",
            )
        ],
        [
            InlineKeyboardButton(
                text="💎 Premium kino",
                callback_data="category_premium",
            )
        ],
    ])


def vip_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="1 kun",
                callback_data="vip_1",
            ),
            InlineKeyboardButton(
                text="7 kun",
                callback_data="vip_7",
            ),
        ],
        [
            InlineKeyboardButton(
                text="10 kun",
                callback_data="vip_10",
            ),
            InlineKeyboardButton(
                text="30 kun",
                callback_data="vip_30",
            ),
        ],
        [
            InlineKeyboardButton(
                text="1 yil",
                callback_data="vip_365",
            )
        ],
    ])


def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➕ Kino qo'shish",
                callback_data="admin_add_movie",
            ),
            InlineKeyboardButton(
                text="🗑 Kino o'chirish",
                callback_data="admin_delete_movie",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📚 Kinolar",
                callback_data="admin_movies",
            ),
            InlineKeyboardButton(
                text="👥 Userlar",
                callback_data="admin_users",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📊 Statistika",
                callback_data="admin_stats",
            )
        ],
        [
            InlineKeyboardButton(
                text="🧾 To'lovlar",
                callback_data="admin_payments",
            ),
            InlineKeyboardButton(
                text="💎 Premium berish",
                callback_data="admin_vip",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📢 Majburiy obuna",
                callback_data="admin_channels",
            )
        ],
        [
            InlineKeyboardButton(
                text="💬 Userga xabar",
                callback_data="admin_message_user",
            ),
            InlineKeyboardButton(
                text="📢 Hammaga xabar",
                callback_data="admin_broadcast",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔄 Premium/stat reset",
                callback_data="admin_reset",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔒 Chiqish",
                callback_data="admin_logout",
            )
        ],
    ])


def back_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⬅️ Admin panel",
                callback_data="admin_back",
            )
        ]
    ])


def payment_admin_keyboard(payment_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ TASDIQLASH",
                callback_data=f"approve_{payment_id}",
            ),
            InlineKeyboardButton(
                text="❌ RAD ETISH",
                callback_data=f"reject_{payment_id}",
            ),
        ]
    ])


def channels_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➕ Kanal qo'shish",
                callback_data="admin_add_channel",
            )
        ],
        [
            InlineKeyboardButton(
                text="📋 Kanallar",
                callback_data="admin_list_channels",
            )
        ],
        [
            InlineKeyboardButton(
                text="🗑 Kanal o'chirish",
                callback_data="admin_delete_channel",
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Admin panel",
                callback_data="admin_back",
            )
        ],
    ])


# =========================================================
# HOME
# =========================================================
async def send_home(message: Message):
    user = get_user(message.from_user.id)

    if not user:
        create_or_update_user(message)
        user = get_user(message.from_user.id)

    if is_premium(message.from_user.id):
        status = "💎 Premium"

        premium_line = (
            f"{fmt_dt(user['premium_until'])} • "
            f"{premium_days_left(message.from_user.id)} kun qoldi"
        )
    else:
        status = "🆓 Free"
        premium_line = "Faol Premium yo'q"

    await message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🎬 <b>KINO BOT</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👋 Salom, <b>{esc(message.from_user.first_name)}</b>!\n\n"
        "🔎 Kino kodini yuboring.\n"
        "Masalan: <code>101</code>\n\n"
        f"👑 Status: <b>{status}</b>\n"
        f"⏳ Premium: <b>{esc(premium_line)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Kerakli bo'limni tanlang:",
        reply_markup=user_menu(),
    )


# =========================================================
# START
# =========================================================
@dp.message(CommandStart())
async def start_handler(message: Message):
    is_new = create_or_update_user(message)

    if is_new and not is_admin(message.from_user.id):
        try:
            await bot.send_message(
                ADMIN_ID,
                "🆕 <b>YANGI FOYDALANUVCHI</b>\n\n"
                f"👤 Ism: <b>{esc(message.from_user.first_name)}</b>\n"
                f"🔗 Username: {username_text(message.from_user)}\n"
                f"🆔 ID: <code>{message.from_user.id}</code>\n"
                f"🕐 Vaqt: <code>{now_text()}</code>",
            )
        except Exception as exc:
            logging.exception(
                "New user admin notification failed: %s",
                exc,
            )

    await send_home(message)


# =========================================================
# USER BUTTONS
# =========================================================
@dp.callback_query(F.data == "user_search")
async def user_search(callback: CallbackQuery):
    await callback.message.answer(
        "🔎 <b>KINO QIDIRISH</b>\n\n"
        "Kino kodini yuboring.\n"
        "Masalan: <code>101</code>"
    )

    await callback.answer()


@dp.callback_query(F.data == "user_profile")
async def user_profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id)

    if not user:
        await callback.answer(
            "Profil topilmadi.",
            show_alert=True,
        )
        return

    active = is_premium(callback.from_user.id)

    status = "💎 Premium" if active else "🆓 Free"
    until = fmt_dt(user["premium_until"]) if active else "-"
    left = (
        f"{premium_days_left(callback.from_user.id)} kun"
        if active
        else "0 kun"
    )

    await callback.message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "          👤 <b>PROFIL</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👤 Ism: <b>{esc(user['first_name'])}</b>\n"
        f"🔗 Username: "
        f"{('@' + esc(user['username'])) if user['username'] else 'Username yo‘q'}\n"
        f"🆔 ID: <code>{user['id']}</code>\n\n"
        f"👑 Status: <b>{status}</b>\n"
        f"⏳ Tugash: <code>{until}</code>\n"
        f"📅 Qolgan: <b>{left}</b>\n"
        f"🎬 Ko'rilgan/yuborilgan: "
        f"<b>{user['total_movies']}</b>\n"
        f"🕐 Oxirgi faollik: "
        f"<code>{esc(user['last_seen'])}</code>\n"
        f"📅 Ro'yxatdan o'tgan: "
        f"<code>{esc(user['created_at'])}</code>"
    )

    await callback.answer()


@dp.callback_query(F.data == "user_premium")
async def user_premium(callback: CallbackQuery):
    uid = callback.from_user.id

    if is_premium(uid):
        await callback.message.answer(
            "💎 <b>PREMIUM FAOL</b>\n\n"
            f"⏳ Tugash: "
            f"<code>{fmt_dt(premium_until(uid))}</code>\n"
            f"📅 Qolgan: "
            f"<b>{premium_days_left(uid)} kun</b>"
        )

        await callback.answer()
        return

    await callback.message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       💎 <b>PREMIUM</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "Tarifni tanlang:\n\n"
        "💎 1 kun — <b>3 000 so'm</b>\n"
        "💎 7 kun — <b>15 000 so'm</b>\n"
        "💎 10 kun — <b>20 000 so'm</b>\n"
        "⭐ 30 kun — <b>49 000 so'm</b>\n"
        "👑 1 yil — <b>99 000 so'm</b>\n\n"
        "💳 <b>To'lov:</b>\n"
        f"<code>{esc(PAYMENT_CARD)}</code>\n"
        f"👤 {esc(PAYMENT_OWNER)}\n\n"
        "1️⃣ Tarifni tanlang.\n"
        "2️⃣ To'lov qiling.\n"
        "3️⃣ Chekni shu botga yuboring.\n"
        "4️⃣ Admin tekshiradi.\n"
        "5️⃣ Tasdiqlangach Premium avtomatik ochiladi.",
        reply_markup=premium_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("plan_"))
async def choose_plan(callback: CallbackQuery):
    uid = callback.from_user.id
    key = callback.data.replace("plan_", "", 1)

    if key not in PLANS:
        await callback.answer(
            "Tarif topilmadi.",
            show_alert=True,
        )
        return

    if is_premium(uid):
        await callback.answer(
            "Sizda Premium hali faol.",
            show_alert=True,
        )
        return

    USER_PAYMENT_PLAN[uid] = key

    plan = PLANS[key]

    await callback.message.answer(
        "🧾 <b>TARIF TANLANDI</b>\n\n"
        f"💎 Tarif: <b>{esc(plan['name'])}</b>\n"
        f"💰 Summa: <b>{money(plan['price'])} so'm</b>\n\n"
        "Endi to'lovni amalga oshiring.\n"
        "Keyin chek rasmini shu botga yuboring.\n\n"
        "Bekor qilish: <code>/cancel</code>"
    )

    await callback.answer()


# =========================================================
# RECEIPT DELIVERY
# =========================================================
async def notify_admin_about_receipt(
    payment_id,
    user_id,
    first_name,
    username,
    plan,
    file_id,
    receipt_type,
    message_text="",
):
    caption = (
        "🧾 <b>YANGI TO'LOV</b>\n\n"
        f"🧾 To'lov ID: <code>#{payment_id}</code>\n"
        f"👤 Ism: <b>{esc(first_name)}</b>\n"
        f"🔗 Username: "
        f"{('@' + esc(username)) if username else 'Username yo‘q'}\n"
        f"🆔 User ID: <code>{user_id}</code>\n\n"
        f"💎 Tarif: <b>{esc(plan['name'])}</b>\n"
        f"💰 Summa: <b>{money(plan['price'])} so'm</b>\n"
        f"🕐 Vaqt: <code>{now_text()}</code>\n"
    )

    if message_text:
        caption += (
            f"\n📝 User izohi: "
            f"<i>{esc(message_text[:700])}</i>"
        )

    markup = payment_admin_keyboard(payment_id)

    logging.info(
        "Sending receipt | admin=%s | payment=%s | type=%s",
        ADMIN_ID,
        payment_id,
        receipt_type,
    )

    if receipt_type == "photo":
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=file_id,
            caption=caption,
            reply_markup=markup,
        )
    else:
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=file_id,
            caption=caption,
            reply_markup=markup,
        )


@dp.message(F.photo)
async def receipt_photo_handler(message: Message):
    await handle_receipt(
        message,
        "photo",
        message.photo[-1].file_id,
    )


@dp.message(F.document)
async def receipt_document_handler(message: Message):
    if message.document and message.document.mime_type:
        if not message.document.mime_type.startswith("image/"):
            return

    await handle_receipt(
        message,
        "document",
        message.document.file_id,
    )


async def handle_receipt(
    message: Message,
    receipt_type,
    file_id,
):
    uid = message.from_user.id

    if is_admin(uid):
        return

    create_or_update_user(message)

    plan_key = USER_PAYMENT_PLAN.get(uid)

    if not plan_key:
        await message.answer(
            "⚠️ Avval <b>💎 Premium</b> bo'limidan "
            "tarif tanlang.\n\n"
            "Keyin chekni yuboring."
        )
        return

    if is_premium(uid):
        USER_PAYMENT_PLAN.pop(uid, None)

        await message.answer(
            "ℹ️ Sizda Premium hali faol. "
            "Yangi to'lov qabul qilinmadi."
        )
        return

    plan = PLANS.get(plan_key)

    if not plan:
        USER_PAYMENT_PLAN.pop(uid, None)

        await message.answer(
            "❌ Tarif topilmadi."
        )
        return

    payment_id = create_payment(
        user_id=uid,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        plan_key=plan_key,
        file_id=file_id,
        receipt_type=receipt_type,
    )

    try:
        await notify_admin_about_receipt(
            payment_id=payment_id,
            user_id=uid,
            first_name=message.from_user.first_name,
            username=message.from_user.username,
            plan=plan,
            file_id=file_id,
            receipt_type=receipt_type,
            message_text=message.caption or "",
        )

        USER_PAYMENT_PLAN.pop(uid, None)

        await message.answer(
            "✅ <b>CHEK QABUL QILINDI</b>\n\n"
            f"🧾 ID: <code>#{payment_id}</code>\n"
            f"💎 Tarif: <b>{esc(plan['name'])}</b>\n"
            f"💰 Summa: <b>{money(plan['price'])} so'm</b>\n\n"
            "⏳ Chek admin chatiga yuborildi.\n"
            "Admin tasdiqlagach Premium avtomatik ochiladi."
        )

    except TelegramForbiddenError:
        logging.exception(
            "ADMIN BOT DELIVERY FORBIDDEN"
        )

        await message.answer(
            "❌ Chek bazaga saqlandi, lekin bot admin akkauntiga "
            "xabar yubora olmadi.\n\n"
            "Admin akkaunti botga <b>/start</b> yuborishi shart.\n"
            f"ADMIN_ID: <code>{ADMIN_ID}</code>"
        )

    except TelegramBadRequest:
        logging.exception(
            "ADMIN BOT DELIVERY BAD REQUEST"
        )

        await message.answer(
            "❌ Chek bazaga saqlandi, lekin Telegram admin chatiga "
            "yuborishni rad etdi.\n\n"
            f"Tekshiring: ADMIN_ID = <code>{ADMIN_ID}</code>"
        )

    except Exception:
        logging.exception(
            "UNKNOWN RECEIPT DELIVERY ERROR"
        )

        await message.answer(
            "⚠️ Chek bazaga saqlandi, ammo admin chatiga "
            "yuborishda xatolik bo'ldi.\n"
            "Server konsolidagi ERROR logni tekshiring."
        )


# =========================================================
# APPROVE / REJECT
# =========================================================
@dp.callback_query(F.data.startswith("approve_"))
async def approve_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    try:
        payment_id = int(
            callback.data.replace(
                "approve_",
                "",
                1,
            )
        )
    except ValueError:
        await callback.answer(
            "ID noto'g'ri.",
            show_alert=True,
        )
        return

    result = approve_payment(payment_id)

    if result is None:
        await callback.answer(
            "To'lov topilmadi.",
            show_alert=True,
        )
        return

    if result in ("already", "processed"):
        await callback.answer(
            "Bu to'lov allaqachon ko'rib chiqilgan.",
            show_alert=True,
        )
        return

    plan = PLANS.get(result["plan_key"])

    if not plan:
        await callback.answer(
            "Tarif topilmadi.",
            show_alert=True,
        )
        return

    uid = result["user_id"]

    new_until = activate_premium(
        uid,
        plan["days"],
    )

    if not new_until:
        await callback.answer(
            "User topilmadi.",
            show_alert=True,
        )
        return

    stat_add("approved_payments")

    try:
        await bot.send_message(
            uid,
            "🎉 <b>PREMIUM FAOLLASHDI</b>\n\n"
            f"💎 Tarif: <b>{esc(plan['name'])}</b>\n"
            f"⏳ Tugash: <code>{fmt_dt(new_until)}</code>\n"
            f"📅 Muddat: <b>{plan['days']} kun</b>\n\n"
            "✅ To'lovingiz tasdiqlandi.\n"
            "🍿 Yoqimli tomosha!",
        )
    except Exception:
        logging.exception(
            "Approved-user notification failed: %s",
            uid,
        )

    try:
        await callback.message.edit_caption(
            caption=(
                "✅ <b>TO'LOV TASDIQLANDI</b>\n\n"
                f"🧾 ID: <code>#{payment_id}</code>\n"
                f"🆔 User: <code>{uid}</code>\n"
                f"💎 Tarif: <b>{esc(plan['name'])}</b>\n"
                f"⏳ Tugash: <code>{fmt_dt(new_until)}</code>\n\n"
                "Status: <b>APPROVED</b>"
            )
        )
    except Exception:
        logging.exception(
            "Could not edit receipt caption."
        )

    await callback.answer(
        "Premium ochildi!"
    )


@dp.callback_query(F.data.startswith("reject_"))
async def reject_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    try:
        payment_id = int(
            callback.data.replace(
                "reject_",
                "",
                1,
            )
        )
    except ValueError:
        await callback.answer(
            "ID noto'g'ri.",
            show_alert=True,
        )
        return

    payment = get_payment(payment_id)

    if not payment:
        await callback.answer(
            "To'lov topilmadi.",
            show_alert=True,
        )
        return

    if not reject_payment(payment_id):
        await callback.answer(
            "Bu to'lov allaqachon ko'rib chiqilgan.",
            show_alert=True,
        )
        return

    stat_add("rejected_payments")

    uid = payment["user_id"]

    try:
        await bot.send_message(
            uid,
            "❌ <b>TO'LOV RAD ETILDI</b>\n\n"
            f"🧾 ID: <code>#{payment_id}</code>\n"
            f"💎 Tarif: <b>{esc(payment['plan_name'])}</b>\n\n"
            "Chekni qayta tekshiring va kerak bo'lsa "
            "yangi chek yuboring.",
        )
    except Exception:
        logging.exception(
            "Rejected-user notification failed: %s",
            uid,
        )

    try:
        await callback.message.edit_caption(
            caption=(
                "❌ <b>TO'LOV RAD ETILDI</b>\n\n"
                f"🧾 ID: <code>#{payment_id}</code>\n"
                f"🆔 User: <code>{uid}</code>\n"
                f"💎 Tarif: <b>{esc(payment['plan_name'])}</b>\n\n"
                "Status: <b>REJECTED</b>"
            )
        )
    except Exception:
        logging.exception(
            "Could not edit rejected receipt."
        )

    await callback.answer(
        "Rad etildi."
    )


# =========================================================
# MOVIE SEARCH
# =========================================================
async def search_movie(message: Message, code):
    code = normalize_code(code)

    movie = get_movie(code)

    if not movie:
        await message.answer(
            "❌ <b>KINO TOPILMADI</b>\n\n"
            f"🔢 Kod: <code>{esc(code)}</code>"
        )
        return

    # =====================================================
    # MANDATORY SUBSCRIPTION IS CHECKED BEFORE THE MOVIE
    # =====================================================
    if not await require_subscription(message):
        return

    if (
        movie["category"] == "premium"
        and not is_premium(message.from_user.id)
    ):
        await message.answer(
            "💎 <b>PREMIUM KINO</b>\n\n"
            f"🎬 <b>{esc(movie['name'])}</b>\n\n"
            "🔒 Bu kino Premium uchun.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💎 Premium olish",
                            callback_data="user_premium",
                        )
                    ]
                ]
            ),
        )
        return

    category_text = (
        "💎 Premium"
        if movie["category"] == "premium"
        else "🆓 Oddiy"
    )

    try:
        # protect_content=True:
        # - normal forwarding is blocked
        # - normal saving/download via Telegram UI is restricted
        #
        # It cannot stop screenshots/screen recording outside Telegram.
        await message.answer_video(
            video=movie["file_id"],
            caption=(
                f"🎬 <b>{esc(movie['name'])}</b>\n"
                f"🔢 Kod: <code>{esc(movie['code'])}</code>\n"
                f"📂 {category_text}\n\n"
                "🍿 Yoqimli tomosha!"
            ),
            protect_content=True,
        )

        increment_movie_stat(
            message.from_user.id
        )

    except Exception:
        logging.exception(
            "Movie send failed."
        )

        await message.answer(
            "❌ Videoni yuborishda xatolik. "
            "Admin faylni qayta yuklashi kerak."
        )


# =========================================================
# ADMIN COMMANDS
# =========================================================
@dp.message(Command("admin"))
async def admin_command(message: Message):
    uid = message.from_user.id

    if not is_admin(uid):
        await message.answer(
            "⛔ Sizda admin huquqi yo'q."
        )
        return

    ADMIN_STATE[uid] = {
        "step": "pin",
        "authenticated": False,
    }

    await message.answer(
        "🔐 <b>ADMIN PANEL</b>\n\n"
        "PIN-kodni yuboring."
    )


@dp.message(Command("panel"))
async def panel_command(message: Message):
    uid = message.from_user.id

    if not is_admin(uid):
        await message.answer(
            "⛔ Ruxsat yo'q."
        )
        return

    if admin_ok(uid):
        await show_admin_panel(message)
    else:
        ADMIN_STATE[uid] = {
            "step": "pin",
            "authenticated": False,
        }

        await message.answer(
            "🔐 PIN-kodni yuboring."
        )


async def show_admin_panel(message: Message):
    if not (
        is_admin(message.from_user.id)
        and admin_ok(message.from_user.id)
    ):
        return

    total_users = count_users()
    total_movies, _, _ = movie_counts()
    active_premium = count_active_premium()

    pending, approved, rejected, paid_users, revenue = (
        payment_stats()
    )

    channel_count = len(
        all_required_channels()
    )

    await message.answer(
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🔐 <b>ADMIN PANEL</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👥 Users: <b>{total_users}</b>\n"
        f"💎 Aktiv Premium: <b>{active_premium}</b>\n"
        f"🎬 Kinolar: <b>{total_movies}</b>\n"
        f"📢 Majburiy kanallar: <b>{channel_count}</b>\n"
        f"🧾 Kutilayotgan to'lov: <b>{pending}</b>\n\n"
        f"✅ Tasdiqlangan: <b>{approved}</b>\n"
        f"❌ Rad etilgan: <b>{rejected}</b>\n"
        f"👤 To'lov qilgan user: <b>{paid_users}</b>\n"
        f"💰 Daromad: <b>{money(revenue)} so'm</b>\n\n"
        "👇 Boshqaruv:",
        reply_markup=admin_menu(),
    )


# =========================================================
# ADMIN TEXT ROUTER
# =========================================================
@dp.message(F.text)
async def text_router(message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    if text.startswith("/"):
        return

    if is_admin(uid):
        state = ADMIN_STATE.get(uid)

        if state:
            step = state.get("step")

            # ---------------------------------------------
            # ADMIN PIN
            # ---------------------------------------------
            if step == "pin":
                if text == ADMIN_PIN:
                    ADMIN_STATE[uid] = {
                        "step": "panel",
                        "authenticated": True,
                    }

                    await show_admin_panel(message)
                else:
                    ADMIN_STATE.pop(uid, None)

                    await message.answer(
                        "❌ PIN noto'g'ri."
                    )

                return

            # ---------------------------------------------
            # AUTHENTICATED ADMIN
            # ---------------------------------------------
            if state.get("authenticated"):

                # -----------------------------------------
                # ADD MOVIE: CODE
                # -----------------------------------------
                if step == "movie_code":
                    code = normalize_code(text)

                    if not code:
                        await message.answer(
                            "❌ Kino kodi bo'sh bo'lmasin."
                        )
                        return

                    if movie_exists(code):
                        await message.answer(
                            "❌ Bu kino kodi band."
                        )
                        return

                    state["code"] = code
                    state["step"] = "movie_name"

                    await message.answer(
                        "2️⃣ Kino nomini yuboring:"
                    )
                    return

                # -----------------------------------------
                # ADD MOVIE: NAME
                # -----------------------------------------
                if step == "movie_name":
                    if not text:
                        await message.answer(
                            "❌ Kino nomini kiriting."
                        )
                        return

                    state["name"] = text
                    state["step"] = "movie_category"

                    await message.answer(
                        "3️⃣ Kino turini tanlang:",
                        reply_markup=category_keyboard(),
                    )
                    return

                # -----------------------------------------
                # DELETE MOVIE
                # -----------------------------------------
                if step == "delete_movie":
                    name = delete_movie(text)

                    if not name:
                        await message.answer(
                            "❌ Bunday kino topilmadi."
                        )
                        return

                    ADMIN_STATE[uid] = {
                        "step": "panel",
                        "authenticated": True,
                    }

                    await message.answer(
                        "🗑 <b>O'CHIRILDI</b>\n\n"
                        f"🔢 Kod: <code>{esc(text)}</code>\n"
                        f"🎬 {esc(name)}",
                        reply_markup=admin_menu(),
                    )
                    return

                # -----------------------------------------
                # VIP USER
                # -----------------------------------------
                if step == "vip_user":
                    target = find_user(text)

                    if not target:
                        await message.answer(
                            "❌ User topilmadi."
                        )
                        return

                    state["target_user"] = target
                    state["step"] = "vip_plan"

                    await message.answer(
                        f"👤 User: <code>{target}</code>\n\n"
                        "Muddatni tanlang:",
                        reply_markup=vip_keyboard(),
                    )
                    return

                # -----------------------------------------
                # MESSAGE ONE USER: TARGET
                # -----------------------------------------
                if step == "message_user_target":
                    target = find_user(text)

                    if not target:
                        await message.answer(
                            "❌ User topilmadi."
                        )
                        return

                    state["target_user"] = target
                    state["step"] = "message_user_text"

                    await message.answer(
                        "💬 Userga yuboriladigan xabarni yozing."
                    )
                    return

                # -----------------------------------------
                # MESSAGE ONE USER: TEXT
                # -----------------------------------------
                if step == "message_user_text":
                    target = state["target_user"]

                    try:
                        await bot.send_message(
                            target,
                            "📩 <b>ADMIN XABARI</b>\n\n"
                            + esc(text),
                        )

                        await message.answer(
                            "✅ Xabar yuborildi.",
                            reply_markup=admin_menu(),
                        )

                        stat_add(
                            "admin_user_messages"
                        )

                    except Exception:
                        logging.exception(
                            "One-user message failed."
                        )

                        await message.answer(
                            "❌ Xabar yuborilmadi."
                        )

                    ADMIN_STATE[uid] = {
                        "step": "panel",
                        "authenticated": True,
                    }

                    return

                # -----------------------------------------
                # BROADCAST
                # -----------------------------------------
                if step == "broadcast":
                    await broadcast_text(
                        message,
                        text,
                    )
                    return

                # -----------------------------------------
                # REQUIRED CHANNEL INPUT
                # -----------------------------------------
                if step == "channel_input":
                    raw = text

                    invite_link = None
                    channel_input = raw

                    # Format:
                    # @publicchannel
                    #
                    # or private:
                    # -1001234567890|https://t.me/+invite
                    if "|" in raw:
                        channel_input, invite_link = (
                            raw.split("|", 1)
                        )

                        channel_input = (
                            channel_input.strip()
                        )
                        invite_link = (
                            invite_link.strip()
                        )

                    try:
                        if (
                            channel_input.lstrip("-").isdigit()
                        ):
                            lookup = int(
                                channel_input
                            )
                        else:
                            lookup = channel_input

                        chat = await bot.get_chat(
                            lookup
                        )

                        if chat.type != "channel":
                            await message.answer(
                                "❌ Bu chat kanal emas.\n\n"
                                "Kanal username yoki kanal ID yuboring."
                            )
                            return

                        username = getattr(
                            chat,
                            "username",
                            None,
                        )

                        title = (
                            getattr(chat, "title", None)
                            or "Kanal"
                        )

                        if not username and not invite_link:
                            await message.answer(
                                "❌ Private kanal uchun invite link ham yuboring.\n\n"
                                "Misol:\n"
                                "<code>-1001234567890|https://t.me/+ABCDEF</code>"
                            )
                            return

                        channel_id = add_required_channel(
                            chat.id,
                            username,
                            title,
                            invite_link,
                        )

                        if not channel_id:
                            await message.answer(
                                "❌ Bu kanal allaqachon majburiy "
                                "obunaga qo'shilgan."
                            )
                            return

                        ADMIN_STATE[uid] = {
                            "step": "panel",
                            "authenticated": True,
                        }

                        url = (
                            invite_link
                            or (
                                f"https://t.me/"
                                f"{username.lstrip('@')}"
                                if username
                                else "-"
                            )
                        )

                        await message.answer(
                            "✅ <b>KANAL QO'SHILDI</b>\n\n"
                            f"📢 Nomi: <b>{esc(title)}</b>\n"
                            f"🆔 Chat ID: <code>{chat.id}</code>\n"
                            f"🔗 Username: "
                            f"<code>{esc(username or '-')}</code>\n"
                            f"🌐 Link: <code>{esc(url)}</code>\n\n"
                            "⚠️ Bot shu kanalga qo'shilgan va "
                            "a'zolikni tekshira oladigan huquqqa ega "
                            "bo'lishi kerak.",
                            reply_markup=admin_menu(),
                        )

                    except Exception as exc:
                        logging.exception(
                            "Required channel add failed: %s",
                            exc,
                        )

                        await message.answer(
                            "❌ Kanalni topib bo'lmadi.\n\n"
                            "Tekshiring:\n"
                            "• Public kanal uchun: <code>@kanal</code>\n"
                            "• Private kanal uchun: "
                            "<code>-100...|https://t.me/+...</code>\n"
                            "• Bot kanalga qo'shilgan bo'lsin."
                        )

                    return

    # Normal user message.
    create_or_update_user(message)

    await search_movie(
        message,
        text,
    )


# =========================================================
# ADMIN MOVIE ADD
# =========================================================
@dp.callback_query(F.data == "admin_add_movie")
async def admin_add_movie(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "movie_code",
        "authenticated": True,
    }

    await callback.message.answer(
        "➕ <b>KINO QO'SHISH</b>\n\n"
        "1️⃣ Kino kodini yuboring.\n"
        "Masalan: <code>101</code>"
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_delete_movie")
async def admin_delete_movie(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "delete_movie",
        "authenticated": True,
    }

    await callback.message.answer(
        "🗑 O'chiriladigan kino kodini yuboring."
    )

    await callback.answer()


@dp.callback_query(F.data == "category_free")
async def category_free(callback: CallbackQuery):
    if not require_admin(callback):
        return

    state = ADMIN_STATE.get(ADMIN_ID)

    if (
        not state
        or state.get("step") != "movie_category"
    ):
        await callback.answer(
            "Jarayon topilmadi.",
            show_alert=True,
        )
        return

    state["category"] = "free"
    state["step"] = "movie_video"

    await callback.message.answer(
        "4️⃣ Endi videoni Telegram orqali yuboring."
    )

    await callback.answer()


@dp.callback_query(F.data == "category_premium")
async def category_premium(callback: CallbackQuery):
    if not require_admin(callback):
        return

    state = ADMIN_STATE.get(ADMIN_ID)

    if (
        not state
        or state.get("step") != "movie_category"
    ):
        await callback.answer(
            "Jarayon topilmadi.",
            show_alert=True,
        )
        return

    state["category"] = "premium"
    state["step"] = "movie_video"

    await callback.message.answer(
        "4️⃣ Endi videoni Telegram orqali yuboring."
    )

    await callback.answer()


@dp.message(F.video)
async def admin_video(message: Message):
    uid = message.from_user.id

    if not is_admin(uid) or not admin_ok(uid):
        return

    state = ADMIN_STATE.get(uid)

    if (
        not state
        or state.get("step") != "movie_video"
    ):
        return

    code = state.get("code")
    name = state.get("name")
    category = state.get("category")

    movie_id = add_movie(
        code,
        name,
        message.video.file_id,
        category,
    )

    if not movie_id:
        await message.answer(
            "❌ Kino qo'shilmadi. Kod band bo'lishi mumkin."
        )
        return

    ADMIN_STATE[uid] = {
        "step": "panel",
        "authenticated": True,
    }

    await message.answer(
        "🎉 <b>KINO QO'SHILDI</b>\n\n"
        f"🔢 Kod: <code>{esc(code)}</code>\n"
        f"🎬 Nomi: <b>{esc(name)}</b>\n"
        f"📂 Turi: <b>"
        f"{'💎 Premium' if category == 'premium' else '🆓 Oddiy'}"
        f"</b>",
        reply_markup=admin_menu(),
    )


# =========================================================
# ADMIN MOVIE LIST
# =========================================================
@dp.callback_query(F.data == "admin_movies")
async def admin_movies(callback: CallbackQuery):
    if not require_admin(callback):
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT code,name,category,created_at
        FROM movies
        ORDER BY id DESC
        LIMIT 100
    """)

    rows = cur.fetchall()
    conn.close()

    if not rows:
        await callback.message.answer(
            "📚 Kino yo'q.",
            reply_markup=back_admin_keyboard(),
        )

        await callback.answer()
        return

    text = "📚 <b>KINOLAR</b>\n\n"

    for row in rows:
        line = (
            f"🔢 <code>{esc(row['code'])}</code>\n"
            f"🎬 <b>{esc(row['name'])}</b>\n"
            f"📂 {esc(row['category'])}\n"
            f"🕐 {esc(row['created_at'])}\n\n"
        )

        if len(text) + len(line) > 3500:
            await callback.message.answer(text)
            text = ""

        text += line

    await callback.message.answer(
        text,
        reply_markup=back_admin_keyboard(),
    )

    await callback.answer()


# =========================================================
# ADMIN USERS
# =========================================================
@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not require_admin(callback):
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            first_name,
            username,
            premium_until,
            total_movies
        FROM users
        ORDER BY id DESC
        LIMIT 50
    """)

    rows = cur.fetchall()
    conn.close()

    if not rows:
        await callback.message.answer(
            "👥 Userlar yo'q.",
            reply_markup=back_admin_keyboard(),
        )
        await callback.answer()
        return

    text = "👥 <b>USERLAR — 50 TA</b>\n\n"

    for row in rows:
        status = (
            "💎"
            if is_premium(row["id"])
            else "🆓"
        )

        uname = (
            f"@{esc(row['username'])}"
            if row["username"]
            else "-"
        )

        text += (
            f"{status} <b>{esc(row['first_name'])}</b>\n"
            f"├ ID: <code>{row['id']}</code>\n"
            f"├ Username: {uname}\n"
            f"├ Premium: "
            f"<code>{fmt_dt(row['premium_until'])}</code>\n"
            f"└ Kino: <b>{row['total_movies']}</b>\n\n"
        )

    await callback.message.answer(
        text[:3900],
        reply_markup=back_admin_keyboard(),
    )

    await callback.answer()


# =========================================================
# ADMIN PAYMENTS
# =========================================================
@dp.callback_query(F.data == "admin_payments")
async def admin_payments(callback: CallbackQuery):
    if not require_admin(callback):
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            user_id,
            username,
            first_name,
            plan_name,
            price,
            status,
            created_at
        FROM payments
        ORDER BY id DESC
        LIMIT 50
    """)

    rows = cur.fetchall()
    conn.close()

    if not rows:
        await callback.message.answer(
            "🧾 To'lovlar yo'q.",
            reply_markup=back_admin_keyboard(),
        )

        await callback.answer()
        return

    status_map = {
        "pending": "⏳",
        "approved": "✅",
        "rejected": "❌",
    }

    text = "🧾 <b>TO'LOVLAR</b>\n\n"

    for row in rows:
        uname = (
            f"@{esc(row['username'])}"
            if row["username"]
            else "-"
        )

        text += (
            f"{status_map.get(row['status'], '❔')} "
            f"<b>#{row['id']}</b>\n"
            f"👤 {esc(row['first_name'])} {uname}\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"💎 {esc(row['plan_name'])}\n"
            f"💰 {money(row['price'])} so'm\n"
            f"🕐 {esc(row['created_at'])}\n\n"
        )

        if len(text) > 3500:
            await callback.message.answer(text)
            text = ""

    if text:
        await callback.message.answer(
            text,
            reply_markup=back_admin_keyboard(),
        )

    await callback.answer()


# =========================================================
# ADMIN STATS
# =========================================================
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not require_admin(callback):
        return

    total_users = count_users()
    active_premium = count_active_premium()

    total_movies, free_movies, premium_movies = (
        movie_counts()
    )

    pending, approved, rejected, paid_users, revenue = (
        payment_stats()
    )

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COALESCE(SUM(total_movies),0) s
        FROM users
    """)
    watched = cur.fetchone()["s"]

    cur.execute("""
        SELECT COUNT(*) c
        FROM users
        WHERE last_seen >= ?
    """, (
        (now() - timedelta(days=1)).isoformat(),
    ))
    active_24h = cur.fetchone()["c"]

    cur.execute("""
        SELECT COUNT(*) c
        FROM users
        WHERE created_at >= ?
    """, (
        (now() - timedelta(days=1)).isoformat(),
    ))
    new_24h = cur.fetchone()["c"]

    conn.close()

    await callback.message.answer(
        "📊 <b>STATISTIKA</b>\n\n"
        f"👥 Jami users: <b>{total_users}</b>\n"
        f"🟢 Aktiv 24h: <b>{active_24h}</b>\n"
        f"🆕 Yangi 24h: <b>{new_24h}</b>\n"
        f"💎 Aktiv Premium: <b>{active_premium}</b>\n\n"
        f"🎬 Kinolar: <b>{total_movies}</b>\n"
        f"🆓 Oddiy: <b>{free_movies}</b>\n"
        f"💎 Premium: <b>{premium_movies}</b>\n"
        f"▶️ Yuborishlar: <b>{watched}</b>\n\n"
        f"🧾 Pending: <b>{pending}</b>\n"
        f"✅ Approved: <b>{approved}</b>\n"
        f"❌ Rejected: <b>{rejected}</b>\n"
        f"👤 Paid users: <b>{paid_users}</b>\n"
        f"💰 Daromad: <b>{money(revenue)} so'm</b>\n\n"
        f"🧾 Cheklar: <b>{stat_get('receipts')}</b>\n"
        f"📢 Broadcastlar: <b>{stat_get('broadcasts')}</b>\n"
        f"📢 Majburiy kanallar: "
        f"<b>{len(all_required_channels())}</b>",
        reply_markup=back_admin_keyboard(),
    )

    await callback.answer()


# =========================================================
# REQUIRED CHANNEL ADMIN PANEL
# =========================================================
@dp.callback_query(F.data == "admin_channels")
async def admin_channels(callback: CallbackQuery):
    if not require_admin(callback):
        return

    channels = all_required_channels()

    await callback.message.answer(
        "📢 <b>MAJBURIY OBUNA</b>\n\n"
        f"Jami kanallar: <b>{len(channels)}</b>\n\n"
        "Bu bo'limdan user kino kodini yuborganda "
        "obuna bo'lishi shart bo'lgan kanallarni boshqarasiz.",
        reply_markup=channels_admin_keyboard(),
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "channel_input",
        "authenticated": True,
    }

    await callback.message.answer(
        "➕ <b>MAJBURIY KANAL QO'SHISH</b>\n\n"
        "Public kanal uchun:\n"
        "<code>@kanal_username</code>\n\n"
        "Private kanal uchun:\n"
        "<code>-1001234567890|https://t.me/+INVITE</code>\n\n"
        "⚠️ Botni kanalga oldindan qo'shing. "
        "A'zolikni tekshirish uchun bot kanalga kirish "
        "huquqiga ega bo'lishi kerak.\n\n"
        "Bekor qilish: <code>/cancel</code>"
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_list_channels")
async def admin_list_channels(callback: CallbackQuery):
    if not require_admin(callback):
        return

    channels = all_required_channels()

    if not channels:
        await callback.message.answer(
            "📢 Hozircha majburiy kanal yo'q.",
            reply_markup=channels_admin_keyboard(),
        )
        await callback.answer()
        return

    text = "📢 <b>MAJBURIY KANALLAR</b>\n\n"

    for index, channel in enumerate(
        channels,
        start=1,
    ):
        username = channel["username"] or "-"
        link = channel_join_url(channel) or "-"

        text += (
            f"{index}. <b>{esc(channel['title'])}</b>\n"
            f"🆔 DB ID: <code>{channel['id']}</code>\n"
            f"🆔 Chat ID: <code>{channel['chat_id']}</code>\n"
            f"🔗 Username: <code>{esc(username)}</code>\n"
            f"🌐 Link: <code>{esc(link)}</code>\n"
            f"🕐 Qo'shilgan: "
            f"<code>{esc(channel['created_at'])}</code>\n\n"
        )

        if len(text) > 3500:
            await callback.message.answer(text)
            text = ""

    if text:
        await callback.message.answer(
            text,
            reply_markup=channels_admin_keyboard(),
        )

    await callback.answer()


@dp.callback_query(F.data == "admin_delete_channel")
async def admin_delete_channel(callback: CallbackQuery):
    if not require_admin(callback):
        return

    channels = all_required_channels()

    if not channels:
        await callback.message.answer(
            "🗑 O'chirish uchun kanal yo'q.",
            reply_markup=channels_admin_keyboard(),
        )
        await callback.answer()
        return

    rows = []

    for channel in channels:
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {channel['title']}",
                callback_data=f"admin_delch_{channel['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="⬅️ Orqaga",
            callback_data="admin_channels",
        )
    ])

    await callback.message.answer(
        "🗑 <b>KANAL O'CHIRISH</b>\n\n"
        "O'chirmoqchi bo'lgan kanalni tanlang:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("admin_delch_"))
async def admin_delete_channel_confirm(
    callback: CallbackQuery,
):
    if not require_admin(callback):
        return

    try:
        channel_id = int(
            callback.data.replace(
                "admin_delch_",
                "",
                1,
            )
        )
    except ValueError:
        await callback.answer(
            "ID noto'g'ri.",
            show_alert=True,
        )
        return

    channel = get_required_channel(channel_id)

    if not channel:
        await callback.answer(
            "Kanal topilmadi.",
            show_alert=True,
        )
        return

    deleted = delete_required_channel_by_id(
        channel_id
    )

    if not deleted:
        await callback.answer(
            "Kanal o'chirilmadi.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        "✅ <b>KANAL O'CHIRILDI</b>\n\n"
        f"📢 {esc(channel['title'])}\n"
        f"🆔 Chat ID: <code>{channel['chat_id']}</code>",
        reply_markup=channels_admin_keyboard(),
    )

    await callback.answer(
        "Kanal o'chirildi."
    )


# =========================================================
# MANUAL PREMIUM
# =========================================================
@dp.callback_query(F.data == "admin_vip")
async def admin_vip(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "vip_user",
        "authenticated": True,
    }

    await callback.message.answer(
        "💎 <b>PREMIUM BERISH</b>\n\n"
        "Telegram ID yoki @username yuboring."
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("vip_"))
async def admin_vip_plan(callback: CallbackQuery):
    if not require_admin(callback):
        return

    key = callback.data.replace(
        "vip_",
        "",
        1,
    )

    plan = PLANS.get(key)

    if not plan:
        await callback.answer(
            "Tarif topilmadi.",
            show_alert=True,
        )
        return

    state = ADMIN_STATE.get(ADMIN_ID)

    target = (
        state.get("target_user")
        if state
        else None
    )

    if not target:
        await callback.answer(
            "User tanlanmagan.",
            show_alert=True,
        )
        return

    new_until = activate_premium(
        target,
        plan["days"],
    )

    ADMIN_STATE[ADMIN_ID] = {
        "step": "panel",
        "authenticated": True,
    }

    try:
        await bot.send_message(
            target,
            "🎉 <b>PREMIUM FAOLLASHDI</b>\n\n"
            f"💎 {esc(plan['name'])}\n"
            f"⏳ Tugash: "
            f"<code>{fmt_dt(new_until)}</code>",
        )
    except Exception:
        logging.exception(
            "Manual premium notify failed."
        )

    await callback.message.answer(
        "✅ <b>PREMIUM BERILDI</b>\n\n"
        f"🆔 User: <code>{target}</code>\n"
        f"💎 Tarif: <b>{esc(plan['name'])}</b>\n"
        f"⏳ Tugash: "
        f"<code>{fmt_dt(new_until)}</code>",
        reply_markup=admin_menu(),
    )

    await callback.answer(
        "Premium ochildi!"
    )


# =========================================================
# ADMIN MESSAGE / BROADCAST
# =========================================================
@dp.callback_query(F.data == "admin_message_user")
async def admin_message_user(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "message_user_target",
        "authenticated": True,
    }

    await callback.message.answer(
        "💬 User Telegram ID yoki @username yuboring."
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "broadcast",
        "authenticated": True,
    }

    await callback.message.answer(
        "📢 Barcha userlarga yuboriladigan xabarni yozing."
    )

    await callback.answer()


async def broadcast_text(
    admin_message,
    text,
):
    ids = all_user_ids()

    sent = 0
    failed = 0

    for uid in ids:
        try:
            await bot.send_message(
                uid,
                "📢 <b>ADMIN XABARI</b>\n\n"
                + esc(text),
            )

            sent += 1

        except (
            TelegramForbiddenError,
            TelegramBadRequest,
        ):
            failed += 1

        except Exception:
            failed += 1

            logging.exception(
                "Broadcast error: %s",
                uid,
            )

        await asyncio.sleep(0.05)

    stat_add("broadcasts")

    ADMIN_STATE[ADMIN_ID] = {
        "step": "panel",
        "authenticated": True,
    }

    await admin_message.answer(
        "📢 <b>BROADCAST YAKUNLANDI</b>\n\n"
        f"✅ Yetkazildi: <b>{sent}</b>\n"
        f"❌ Xato/bloklagan: <b>{failed}</b>\n"
        f"👥 Jami: <b>{len(ids)}</b>",
        reply_markup=admin_menu(),
    )


# =========================================================
# RESET / BACK / LOGOUT
# =========================================================
@dp.callback_query(F.data == "admin_reset")
async def admin_reset(callback: CallbackQuery):
    if not require_admin(callback):
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚠️ HA, RESET",
                    callback_data="admin_reset_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Bekor qilish",
                    callback_data="admin_back",
                )
            ],
        ]
    )

    await callback.message.answer(
        "⚠️ <b>RESET</b>\n\n"
        "Premium, to'lovlar va statistika tozalanadi.\n"
        "Kinolar va majburiy kanallar o'chirilmaydi.",
        reply_markup=keyboard,
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_reset_confirm")
async def admin_reset_confirm(callback: CallbackQuery):
    if not require_admin(callback):
        return

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET premium_until=NULL,
            total_movies=0
    """)

    cur.execute(
        "DELETE FROM payments"
    )

    cur.execute(
        "DELETE FROM bot_stats"
    )

    conn.commit()
    conn.close()

    await callback.message.answer(
        "✅ Reset bajarildi.\n\n"
        "Kinolar va majburiy obuna kanallari "
        "saqlanib qoldi.",
        reply_markup=admin_menu(),
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if not require_admin(callback):
        return

    ADMIN_STATE[ADMIN_ID] = {
        "step": "panel",
        "authenticated": True,
    }

    await show_admin_panel(
        callback.message
    )

    await callback.answer()


@dp.callback_query(F.data == "admin_logout")
async def admin_logout(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ADMIN_STATE.pop(
        callback.from_user.id,
        None,
    )

    await callback.message.answer(
        "🔒 Admin panel yopildi.\n"
        "Qayta kirish: <code>/admin</code>"
    )

    await callback.answer()


# =========================================================
# CANCEL
# =========================================================
@dp.message(Command("cancel"))
async def cancel_handler(message: Message):
    uid = message.from_user.id

    USER_PAYMENT_PLAN.pop(
        uid,
        None,
    )

    if is_admin(uid):
        if admin_ok(uid):
            ADMIN_STATE[uid] = {
                "step": "panel",
                "authenticated": True,
            }

            await message.answer(
                "❌ Amal bekor qilindi.",
                reply_markup=admin_menu(),
            )
        else:
            ADMIN_STATE.pop(
                uid,
                None,
            )

            await message.answer(
                "❌ Amal bekor qilindi."
            )

        return

    await message.answer(
        "❌ Amal bekor qilindi.",
        reply_markup=user_menu(),
    )


# =========================================================
# STARTUP DIAGNOSTICS
# =========================================================
async def check_admin_delivery():
    me = await bot.get_me()

    logging.info(
        "BOT OK: @%s | bot_id=%s",
        me.username,
        me.id,
    )

    logging.info(
        "ADMIN_ID=%s",
        ADMIN_ID,
    )

    try:
        chat = await bot.get_chat(
            ADMIN_ID
        )

        logging.info(
            "ADMIN CHAT OK: id=%s | type=%s | username=%s | name=%s",
            chat.id,
            chat.type,
            getattr(chat, "username", None),
            getattr(chat, "first_name", None),
        )

    except Exception as exc:
        logging.exception(
            "ADMIN CHAT CHECK FAILED. "
            "Admin must open the bot and press /start. "
            "Also verify ADMIN_ID. Error: %s",
            exc,
        )


async def check_required_channels_startup():
    channels = all_required_channels()

    if not channels:
        logging.info(
            "Required subscription: no channels configured."
        )
        return

    logging.info(
        "Required subscription: %s channel(s).",
        len(channels),
    )

    for channel in channels:
        try:
            chat = await bot.get_chat(
                channel["chat_id"]
            )

            logging.info(
                "Required channel OK | id=%s | title=%s | username=%s",
                chat.id,
                getattr(chat, "title", None),
                getattr(chat, "username", None),
            )

        except Exception:
            logging.exception(
                "Required channel access failed | id=%s",
                channel["chat_id"],
            )


# =========================================================
# ERROR HANDLER
# =========================================================
@dp.errors()
async def global_error_handler(event):
    logging.exception(
        "Unhandled aiogram error: %s",
        event.exception,
    )

    return True


# =========================================================
# MAIN
# =========================================================
async def main():
    init_db()

    print("=" * 65)
    print("🎬 KINO BOT PRO")
    print("=" * 65)
    print("🗄 Database:", DB_NAME)
    print("🔐 Admin ID:", ADMIN_ID)
    print(
        "💎 Plans:",
        ", ".join(PLANS.keys()),
    )
    print(
        "📢 Required channels:",
        len(all_required_channels()),
    )
    print("=" * 65)

    await check_admin_delivery()
    await check_required_channels_startup()

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except (
        KeyboardInterrupt,
        SystemExit,
    ):
        print("🛑 Bot to'xtatildi.")


requirements = """aiogram>=3.20,<4
"""

out = Path("/mnt/data/kino_bot_fixed.py")
req = Path("/mnt/data/requirements.txt")

out.write_text(code, encoding="utf-8")
req.write_text(requirements, encoding="utf-8")

print(f"Tayyor: {out}")
print(f"Qatorlar soni: {len(code.splitlines())}")
print(f"Requirements: {req}")
