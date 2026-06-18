import telebot
from telebot import types
import psycopg2
from psycopg2 import pool
import time
import logging
import os
from flask import Flask
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.request

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_ENV = [int(x.strip()) for x in os.getenv("ADMIN_ID", "0").split(",") if x.strip().isdigit()]
MAIN_ADMIN_ID = ADMIN_IDS_ENV[0] if ADMIN_IDS_ENV else 0
CHANNEL = os.getenv("CHANNEL", "")
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

_cache = {}

def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() < entry["exp"]:
        return entry["val"], True
    return None, False

def cache_set(key, val, ttl=60):
    _cache[key] = {"val": val, "exp": time.time() + ttl}

def cache_del(key):
    _cache.pop(key, None)

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot ishlayapti ✅"

@app.route('/health')
def health():
    return "OK"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def db_execute(query, params=None, fetch=False, fetchone=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone:
                result = cur.fetchone()
            elif fetch:
                result = cur.fetchall()
            else:
                result = None
            conn.commit()
            return result
    except Exception as e:
        conn.rollback()
        logging.error(f"DB error: {e}")
        return None
    finally:
        db_pool.putconn(conn)

def init_db():
    db_execute("""CREATE TABLE IF NOT EXISTS bot_users (
        id BIGINT PRIMARY KEY, name TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    db_execute("""CREATE TABLE IF NOT EXISTS movies (
        code TEXT PRIMARY KEY, name TEXT,
        views INTEGER DEFAULT 0,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    db_execute("""CREATE TABLE IF NOT EXISTS movie_parts (
        id SERIAL PRIMARY KEY,
        code TEXT NOT NULL,
        part_num INTEGER NOT NULL,
        file_id TEXT NOT NULL,
        UNIQUE(code, part_num))""")
    db_execute("""CREATE TABLE IF NOT EXISTS admins (
        id BIGINT PRIMARY KEY, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    db_execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    for aid in ADMIN_IDS_ENV:
        db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (aid,))
    if CHANNEL:
        existing = db_execute("SELECT value FROM settings WHERE key='channels'", fetchone=True)
        if not existing:
            db_execute("INSERT INTO settings (key, value) VALUES ('channels', %s) ON CONFLICT (key) DO NOTHING", (CHANNEL,))
    logging.info("Database tayyor ✅")

def get_channels():
    val, hit = cache_get("channels")
    if hit:
        return val
    row = db_execute("SELECT value FROM settings WHERE key = 'channels'", fetchone=True)
    if row and row[0]:
        result = [ch.strip() for ch in row[0].split(",") if ch.strip()]
    else:
        result = [CHANNEL] if CHANNEL else []
    cache_set("channels", result, ttl=300)
    return result

def save_channels(channels):
    value = ",".join(channels)
    db_execute("INSERT INTO settings (key, value) VALUES ('channels', %s) ON CONFLICT (key) DO UPDATE SET value=%s",
               (value, value))
    cache_del("channels")

admin_states = {}

def is_admin(user_id):
    if user_id in ADMIN_IDS_ENV:
        return True
    val, hit = cache_get(f"admin_{user_id}")
    if hit:
        return val
    result = db_execute("SELECT 1 FROM admins WHERE id = %s", (user_id,), fetchone=True)
    is_adm = result is not None
    cache_set(f"admin_{user_id}", is_adm, ttl=120)
    return is_adm

def is_main_admin(user_id):
    return user_id == MAIN_ADMIN_ID

def check_sub(user_id):
    channels = get_channels()
    if not channels:
        return True, []
    val, hit = cache_get(f"sub_{user_id}")
    if hit:
        return val
    not_subbed = []
    for ch in channels:
        try:
            status = bot.get_chat_member(ch, user_id).status
            if status not in ["member", "administrator", "creator"]:
                not_subbed.append(ch)
        except Exception as e:
            logging.error(f"Sub check error {ch}: {e}")
    result = (len(not_subbed) == 0, not_subbed)
    cache_set(f"sub_{user_id}", result, ttl=60)
    return result

def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logging.error(f"Send error: {e}")

def sub_keyboard(not_subbed):
    kb = types.InlineKeyboardMarkup()
    for ch in not_subbed:
        name = ch.replace("@", "")
        kb.add(types.InlineKeyboardButton(f"📢 {ch} ga obuna", url=f"https://t.me/{name}"))
    kb.add(types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub"))
    return kb

def admin_keyboard(user_id=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Kino qo'shish", "🗑 Kino o'chirish")
    kb.add("📊 Statistika", "📋 Kinolar ro'yxati")
    kb.add("👥 Userlar", "📨 Reklama yuborish")
    kb.add("👑 Adminlar", "⚙️ Sozlamalar")
    if user_id and is_main_admin(user_id):
        kb.add("➕ Admin qo'shish", "❌ Admin o'chirish")
    return kb

def show_admins_panel(chat_id, is_main):
    admins = db_execute("SELECT id FROM admins ORDER BY added_at", fetch=True) or []
    text = "👑 <b>Adminlar ro'yxati:</b>\n\n"
    kb = types.InlineKeyboardMarkup()
    for row in admins:
        aid = row[0]
        marker = " 👑 (asosiy)" if aid == MAIN_ADMIN_ID else ""
        text += f"• <code>{aid}</code>{marker}\n"
        if is_main and aid != MAIN_ADMIN_ID:
            kb.add(types.InlineKeyboardButton(f"❌ {aid} ni o'chirish", callback_data=f"remove_admin_{aid}"))
    if is_main:
        kb.add(types.InlineKeyboardButton("➕ Yangi admin qo'shish", callback_data="add_admin_start"))
    safe_send(chat_id, text, parse_mode="HTML", reply_markup=kb)

@bot.message_handler(commands=['addadmin'])
def add_admin_cmd(msg):
    if not is_main_admin(msg.from_user.id):
        safe_send(msg.chat.id, "❌ Faqat asosiy admin")
        return
    try:
        new_admin_id = int(msg.text.split()[1])
        db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (new_admin_id,))
        cache_del(f"admin_{new_admin_id}")
        safe_send(msg.chat.id, f"✅ Admin qo'shildi: <code>{new_admin_id}</code>", parse_mode="HTML")
        try:
            safe_send(new_admin_id, "🎉 Siz admin bo'ldingiz! /start bosing.")
        except:
            pass
    except (IndexError, ValueError):
        safe_send(msg.chat.id, "❗ Format: <code>/addadmin USER_ID</code>", parse_mode="HTML")

@bot.message_handler(commands=['removeadmin'])
def remove_admin_cmd(msg):
    if not is_main_admin(msg.from_user.id):
        safe_send(msg.chat.id, "❌ Faqat asosiy admin")
        return
    try:
        rm_id = int(msg.text.split()[1])
        if rm_id == MAIN_ADMIN_ID:
            safe_send(msg.chat.id, "❌ Asosiy adminni o'chirib bo'lmaydi")
            return
        result = db_execute("DELETE FROM admins WHERE id = %s RETURNING id", (rm_id,), fetchone=True)
        if result:
            cache_del(f"admin_{rm_id}")
            safe_send(msg.chat.id, f"✅ Admin o'chirildi: <code>{rm_id}</code>", parse_mode="HTML")
        else:
            safe_send(msg.chat.id, "❌ Bunday admin topilmadi")
    except (IndexError, ValueError):
        safe_send(msg.chat.id, "❗ Format: <code>/removeadmin USER_ID</code>", parse_mode="HTML")

@bot.message_handler(commands=['admins'])
def list_admins_cmd(msg):
    if not is_main_admin(msg.from_user.id):
        return
    admins = db_execute("SELECT id FROM admins ORDER BY added_at", fetch=True) or []
    text = "👑 <b>Adminlar:</b>\n\n"
    for (aid,) in admins:
        marker = " 👑" if aid == MAIN_ADMIN_ID else ""
        text += f"• <code>{aid}</code>{marker}\n"
    safe_send(msg.chat.id, text or "📭 Admin yo'q", parse_mode="HTML")

@bot.message_handler(commands=['myid'])
def my_id_cmd(msg):
    safe_send(msg.chat.id, f"🆔 Sizning ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "👑 Adminlar")
def admins_menu(msg):
    show_admins_panel(msg.chat.id, is_main_admin(msg.from_user.id))

@bot.message_handler(func=lambda m: is_main_admin(m.from_user.id) and m.text == "➕ Admin qo'shish")
def add_admin_btn(msg):
    admin_states[msg.from_user.id] = {"step": "add_admin_id"}
    safe_send(msg.chat.id,
        "➕ Yangi admin ID sini yuboring:\n\n"
        "💡 Admin <code>/myid</code> yozib ID sini bilsin\n\n"
        "❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: is_main_admin(m.from_user.id) and m.text == "❌ Admin o'chirish")
def remove_admin_btn(msg):
    admins = db_execute("SELECT id FROM admins ORDER BY added_at", fetch=True) or []
    kb = types.InlineKeyboardMarkup()
    found = False
    for row in admins:
        aid = row[0]
        if aid != MAIN_ADMIN_ID:
            kb.add(types.InlineKeyboardButton(f"❌ {aid} ni o'chirish", callback_data=f"remove_admin_{aid}"))
            found = True
    if not found:
        safe_send(msg.chat.id, "📭 O'chiriladigan admin yo'q")
        return
    safe_send(msg.chat.id, "👑 Qaysi adminni o'chirmoqchisiz?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("remove_admin_"))
def remove_admin_callback(call):
    if not is_main_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Faqat asosiy admin", show_alert=True)
        return
    target_id = int(call.data.split("_")[-1])
    if target_id == MAIN_ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Asosiy adminni o'chirib bo'lmaydi!", show_alert=True)
        return
    result = db_execute("DELETE FROM admins WHERE id = %s RETURNING id", (target_id,), fetchone=True)
    if result:
        cache_del(f"admin_{target_id}")
        bot.answer_callback_query(call.id, f"✅ O'chirildi: {target_id}", show_alert=True)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        show_admins_panel(call.message.chat.id, True)
    else:
        bot.answer_callback_query(call.id, "❌ Topilmadi", show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data == "add_admin_start")
def add_admin_start_callback(call):
    if not is_main_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Faqat asosiy admin", show_alert=True)
        return
    admin_states[call.from_user.id] = {"step": "add_admin_id"}
    bot.answer_callback_query(call.id)
    safe_send(call.message.chat.id,
        "➕ Yangi admin ID sini yuboring:\n\n"
        "💡 Admin <code>/myid</code> yozib ID sini bilsin\n\n"
        "❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "⚙️ Sozlamalar")
def settings_menu(msg):
    channels = get_channels()
    if channels:
        ch_text = "\n".join([f"  {i+1}. <b>{ch}</b>" for i, ch in enumerate(channels)])
        text = f"⚙️ <b>Sozlamalar</b>\n\n📢 Kanallar ({len(channels)}/5):\n{ch_text}"
    else:
        text = "⚙️ <b>Sozlamalar</b>\n\n📢 Kanallar: <b>Ulanmagan</b>"
    kb = types.InlineKeyboardMarkup()
    if len(channels) < 5:
        kb.add(types.InlineKeyboardButton("➕ Kanal/Guruh qo'shish", callback_data="kanal_add"))
    for i, ch in enumerate(channels):
        kb.add(types.InlineKeyboardButton(f"❌ {ch} ni o'chirish", callback_data=f"kanal_del_{i}"))
    safe_send(msg.chat.id, text, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "kanal_add")
def kanal_add_callback(call):
    if not is_admin(call.from_user.id):
        return
    channels = get_channels()
    if len(channels) >= 5:
        bot.answer_callback_query(call.id, "❌ Maksimum 5 ta kanal!", show_alert=True)
        return
    admin_states[call.from_user.id] = {"step": "kanal_set"}
    bot.answer_callback_query(call.id)
    safe_send(call.message.chat.id,
        "📢 Kanal yoki guruh username ni yuboring:\n\n"
        "Misol: <code>@mening_kanalim</code>\n\n"
        "❌ Bekor: /start", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("kanal_del_"))
def kanal_del_callback(call):
    if not is_admin(call.from_user.id):
        return
    idx = int(call.data.split("_")[-1])
    channels = get_channels()
    if idx < len(channels):
        removed = channels.pop(idx)
        save_channels(channels)
        bot.answer_callback_query(call.id, f"✅ {removed} o'chirildi!", show_alert=True)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        settings_menu_by_id(call.message.chat.id)
    else:
        bot.answer_callback_query(call.id, "❌ Topilmadi", show_alert=True)

def settings_menu_by_id(chat_id):
    channels = get_channels()
    if channels:
        ch_text = "\n".join([f"  {i+1}. <b>{ch}</b>" for i, ch in enumerate(channels)])
        text = f"⚙️ <b>Sozlamalar</b>\n\n📢 Kanallar ({len(channels)}/5):\n{ch_text}"
    else:
        text = "⚙️ <b>Sozlamalar</b>\n\n📢 Kanallar: <b>Ulanmagan</b>"
    kb = types.InlineKeyboardMarkup()
    if len(channels) < 5:
        kb.add(types.InlineKeyboardButton("➕ Kanal/Guruh qo'shish", callback_data="kanal_add"))
    for i, ch in enumerate(channels):
        kb.add(types.InlineKeyboardButton(f"❌ {ch} ni o'chirish", callback_data=f"kanal_del_{i}"))
    safe_send(chat_id, text, parse_mode="HTML", reply_markup=kb)

@bot.message_handler(commands=['start'])
def start(msg):
    user = msg.from_user
    db_execute("INSERT INTO bot_users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
               (user.id, user.first_name))
    if is_admin(user.id):
        safe_send(msg.chat.id, "👑 Salom, Admin!", reply_markup=admin_keyboard(user.id))
        return
    is_subbed, not_subbed = check_sub(user.id)
    if not is_subbed:
        safe_send(msg.chat.id,
            "❗ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:",
            reply_markup=sub_keyboard(not_subbed))
        return
    safe_send(msg.chat.id, "🎬 Salom! Kino kodini yuboring:")
    try:
        safe_send(MAIN_ADMIN_ID, f"👤 Yangi user: {user.id} | {user.first_name}")
    except:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_callback(call):
    cache_del(f"sub_{call.from_user.id}")
    is_subbed, not_subbed = check_sub(call.from_user.id)
    if is_subbed:
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        safe_send(call.message.chat.id, "✅ Rahmat! Kino kodini yuboring:")
    else:
        bot.answer_callback_query(call.id, "❗ Hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "➕ Kino qo'shish")
def add_movie_start(msg):
    admin_states[msg.from_user.id] = {"step": "code", "data": {}}
    safe_send(msg.chat.id, "🔢 Kino kodini yuboring:\n\nMisol: <code>101</code> yoki <code>serial1</code>", parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "🗑 Kino o'chirish")
def delete_movie_start(msg):
    admin_states[msg.from_user.id] = {"step": "delete", "data": {}}
    safe_send(msg.chat.id, "🗑 O'chiriladigan kino kodini yuboring:")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📊 Statistika")
def stats(msg):
    users_count = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    movies_count = db_execute("SELECT COUNT(*) FROM movies", fetchone=True)[0]
    parts_count = db_execute("SELECT COUNT(*) FROM movie_parts", fetchone=True)[0]
    total_views = db_execute("SELECT COALESCE(SUM(views), 0) FROM movies", fetchone=True)[0]
    text = (f"📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{users_count}</b>\n"
            f"🎬 Kinolar/Seriallar: <b>{movies_count}</b>\n"
            f"🎞 Jami qismlar: <b>{parts_count}</b>\n"
            f"👁 Jami ko'rishlar: <b>{total_views}</b>")
    safe_send(msg.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📋 Kinolar ro'yxati")
def movie_list(msg):
    movies = db_execute("""
        SELECT m.code, m.name, m.views,
               (SELECT COUNT(*) FROM movie_parts WHERE code = m.code) as parts
        FROM movies m ORDER BY m.added_at DESC LIMIT 50""", fetch=True)
    if not movies:
        safe_send(msg.chat.id, "📭 Hech qanday kino yo'q")
        return
    text = "📋 <b>Kinolar:</b>\n\n"
    for code, name, views, parts in movies:
        parts_info = f" | 🎞 {parts} qism" if parts > 1 else ""
        text += f"🔢 <code>{code}</code> — {name} (👁 {views}{parts_info})\n"
    safe_send(msg.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "👥 Userlar")
def users_menu(msg):
    total = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    today = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours'", fetchone=True)[0]
    week = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '7 days'", fetchone=True)[0]
    text = (f"👥 <b>Foydalanuvchilar</b>\n\n"
            f"📊 Jami: <b>{total}</b>\n"
            f"📅 Bugun: <b>{today}</b>\n"
            f"🗓 Hafta: <b>{week}</b>")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📋 Ro'yxat (oxirgi 20)", callback_data="users_list_0"),
           types.InlineKeyboardButton("🆕 Yangi userlar", callback_data="users_recent"))
    kb.add(types.InlineKeyboardButton("🔍 User qidirish", callback_data="users_search"))
    safe_send(msg.chat.id, text, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("users_list_"))
def users_list_callback(call):
    if not is_admin(call.from_user.id):
        return
    offset = int(call.data.split("_")[-1])
    limit = 20
    users = db_execute("SELECT id, name, joined_at FROM bot_users ORDER BY joined_at DESC LIMIT %s OFFSET %s",
                       (limit, offset), fetch=True) or []
    total = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    if not users:
        bot.answer_callback_query(call.id, "📭 Boshqa userlar yo'q")
        return
    text = f"👥 <b>Userlar</b> ({offset+1}–{offset+len(users)} / {total})\n\n"
    for uid, name, joined in users:
        name_safe = (name or "—").replace("<", "&lt;").replace(">", "&gt;")
        date_str = joined.strftime("%d.%m.%Y %H:%M") if joined else "—"
        text += f"🆔 <code>{uid}</code> — {name_safe}\n📅 {date_str}\n\n"
    kb = types.InlineKeyboardMarkup()
    nav = []
    if offset > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Oldingi", callback_data=f"users_list_{max(0,offset-limit)}"))
    if offset + limit < total:
        nav.append(types.InlineKeyboardButton("Keyingi ➡️", callback_data=f"users_list_{offset+limit}"))
    if nav:
        kb.row(*nav)
    kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="users_back"))
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except:
        safe_send(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "users_recent")
def users_recent_callback(call):
    if not is_admin(call.from_user.id):
        return
    users = db_execute(
        "SELECT id, name, joined_at FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours' ORDER BY joined_at DESC LIMIT 30",
        fetch=True) or []
    if not users:
        bot.answer_callback_query(call.id, "📭 Bugun yangi user yo'q", show_alert=True)
        return
    text = f"🆕 <b>Bugungi yangi userlar ({len(users)} ta)</b>\n\n"
    for uid, name, joined in users:
        name_safe = (name or "—").replace("<", "&lt;").replace(">", "&gt;")
        date_str = joined.strftime("%H:%M") if joined else "—"
        text += f"🆔 <code>{uid}</code> — {name_safe} ({date_str})\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Orqaga", callback_data="users_back"))
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except:
        safe_send(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "users_search")
def users_search_callback(call):
    if not is_admin(call.from_user.id):
        return
    admin_states[call.from_user.id] = {"step": "search_user", "data": {}}
    safe_send(call.message.chat.id, "🔍 User ID sini yuboring:")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "users_back")
def users_back_callback(call):
    if not is_admin(call.from_user.id):
        return
    total = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    today = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours'", fetchone=True)[0]
    week = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '7 days'", fetchone=True)[0]
    text = (f"👥 <b>Foydalanuvchilar</b>\n\n"
            f"📊 Jami: <b>{total}</b>\n"
            f"📅 Bugun: <b>{today}</b>\n"
            f"🗓 Hafta: <b>{week}</b>")
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📋 Ro'yxat (oxirgi 20)", callback_data="users_list_0"),
           types.InlineKeyboardButton("🆕 Yangi userlar", callback_data="users_recent"))
    kb.add(types.InlineKeyboardButton("🔍 User qidirish", callback_data="users_search"))
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except:
        safe_send(call.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("msg_user_"))
def msg_user_callback(call):
    if not is_admin(call.from_user.id):
        return
    target_id = int(call.data.split("_")[-1])
    admin_states[call.from_user.id] = {"step": "send_to_user", "data": {"target_id": target_id}}
    safe_send(call.message.chat.id, f"✉️ User <code>{target_id}</code> ga xabar yuboring:", parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("make_admin_"))
def make_admin_callback(call):
    if not is_main_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Faqat asosiy admin", show_alert=True)
        return
    target_id = int(call.data.split("_")[-1])
    db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (target_id,))
    cache_del(f"admin_{target_id}")
    bot.answer_callback_query(call.id, "✅ Admin qilindi!", show_alert=True)
    try:
        safe_send(target_id, "🎉 Siz admin bo'ldingiz! /start bosing.")
    except:
        pass

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📨 Reklama yuborish")
def broadcast_start(msg):
    admin_states[msg.from_user.id] = {"step": "broadcast", "data": {}}
    safe_send(msg.chat.id, "📨 Yubormoqchi bo'lgan xabaringizni yuboring:")

def send_to_user(uid, msg):
    try:
        bot.copy_message(uid, msg.chat.id, msg.message_id)
        return True
    except Exception as e:
        if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
            db_execute("DELETE FROM bot_users WHERE id = %s", (uid,))
        return False

def add_part_keyboard(code):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Yana qism qo'shish", callback_data=f"addpart_{code}"))
    kb.add(types.InlineKeyboardButton("✅ Tayyor", callback_data="addpart_done"))
    return kb

@bot.callback_query_handler(func=lambda c: c.data.startswith("addpart_") and c.data != "addpart_done")
def addpart_callback(call):
    if not is_admin(call.from_user.id):
        return
    code = call.data[len("addpart_"):]
    parts_count = db_execute("SELECT COUNT(*) FROM movie_parts WHERE code=%s", (code,), fetchone=True)[0]
    admin_states[call.from_user.id] = {"step": "add_part", "data": {"code": code, "part_num": parts_count + 1}}
    bot.answer_callback_query(call.id)
    safe_send(call.message.chat.id,
        f"🎥 <b>{parts_count + 1}-qism</b> videosini yuboring:", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data == "addpart_done")
def addpart_done_callback(call):
    if not is_admin(call.from_user.id):
        return
    bot.answer_callback_query(call.id, "✅ Kino saqlandi!")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.from_user.id in admin_states,
                     content_types=['text', 'video', 'document'])
def admin_steps(msg):
    state = admin_states[msg.from_user.id]
    step = state["step"]

    if step == "code":
        code = msg.text.strip()
        state["data"]["code"] = code
        state["step"] = "name"
        safe_send(msg.chat.id, "📝 Kino/Serial nomini yuboring:")

    elif step == "name":
        state["data"]["name"] = msg.text.strip()
        state["step"] = "file"
        safe_send(msg.chat.id, "🎥 <b>1-qism</b> videosini yuboring:", parse_mode="HTML")

    elif step == "file":
        file_id = None
        if msg.video:
            file_id = msg.video.file_id
        elif msg.document:
            file_id = msg.document.file_id
        if not file_id:
            safe_send(msg.chat.id, "❗ Video yuboring")
            return
        code = state["data"]["code"]
        name = state["data"]["name"]
        db_execute("INSERT INTO movies (code, name) VALUES (%s, %s) ON CONFLICT (code) DO UPDATE SET name=%s",
                   (code, name, name))
        db_execute("INSERT INTO movie_parts (code, part_num, file_id) VALUES (%s, 1, %s) ON CONFLICT (code, part_num) DO UPDATE SET file_id=%s",
                   (code, file_id, file_id))
        del admin_states[msg.from_user.id]
        safe_send(msg.chat.id,
            f"✅ <b>{name}</b> qo'shildi!\n🔢 Kod: <code>{code}</code>\n\nYana qism qo'shishni xohlaysizmi?",
            parse_mode="HTML", reply_markup=add_part_keyboard(code))

    elif step == "add_part":
        file_id = None
        if msg.video:
            file_id = msg.video.file_id
        elif msg.document:
            file_id = msg.document.file_id
        if not file_id:
            safe_send(msg.chat.id, "❗ Video yuboring")
            return
        code = state["data"]["code"]
        part_num = state["data"]["part_num"]
        db_execute("INSERT INTO movie_parts (code, part_num, file_id) VALUES (%s, %s, %s) ON CONFLICT (code, part_num) DO UPDATE SET file_id=%s",
                   (code, part_num, file_id, file_id))
        del admin_states[msg.from_user.id]
        total_parts = db_execute("SELECT COUNT(*) FROM movie_parts WHERE code=%s", (code,), fetchone=True)[0]
        safe_send(msg.chat.id,
            f"✅ <b>{part_num}-qism</b> qo'shildi! Jami: {total_parts} qism\n\nYana qism qo'shishni xohlaysizmi?",
            parse_mode="HTML", reply_markup=add_part_keyboard(code))

    elif step == "delete":
        code = msg.text.strip()
        r1 = db_execute("DELETE FROM movie_parts WHERE code = %s", (code,))
        r2 = db_execute("DELETE FROM movies WHERE code = %s RETURNING code", (code,), fetchone=True)
        safe_send(msg.chat.id, f"✅ O'chirildi: {code}" if r2 else "❌ Topilmadi")
        del admin_states[msg.from_user.id]

    elif step == "search_user":
        try:
            uid = int(msg.text.strip())
        except ValueError:
            safe_send(msg.chat.id, "❗ ID raqam bo'lishi kerak")
            del admin_states[msg.from_user.id]
            return
        user = db_execute("SELECT id, name, joined_at FROM bot_users WHERE id = %s", (uid,), fetchone=True)
        if not user:
            safe_send(msg.chat.id, f"❌ ID <code>{uid}</code> topilmadi", parse_mode="HTML")
            del admin_states[msg.from_user.id]
            return
        u_id, u_name, u_joined = user
        date_str = u_joined.strftime("%d.%m.%Y %H:%M") if u_joined else "—"
        name_safe = (u_name or "—").replace("<", "&lt;").replace(">", "&gt;")
        try:
            chat = bot.get_chat(u_id)
            username = f"@{chat.username}" if chat.username else "yo'q"
            full_name = f"{chat.first_name or ''} {chat.last_name or ''}".strip() or "—"
            full_name_safe = full_name.replace("<", "&lt;").replace(">", "&gt;")
        except:
            username = "yo'q"
            full_name_safe = name_safe
        is_subbed, _ = check_sub(u_id)
        is_subscribed = "✅ Ha" if is_subbed else "❌ Yo'q"
        is_user_admin = "👑 Ha" if is_admin(u_id) else "❌ Yo'q"
        text = (f"👤 <b>User ma'lumoti</b>\n\n"
                f"🆔 ID: <code>{u_id}</code>\n"
                f"👨 Ism: {full_name_safe}\n"
                f"📛 Username: {username}\n"
                f"📅 Qo'shilgan: {date_str}\n"
                f"📢 Obuna: {is_subscribed}\n"
                f"👑 Admin: {is_user_admin}")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✉️ Xabar yuborish", callback_data=f"msg_user_{u_id}"))
        if is_main_admin(msg.from_user.id) and not is_admin(u_id):
            kb.add(types.InlineKeyboardButton("👑 Admin qilish", callback_data=f"make_admin_{u_id}"))
        safe_send(msg.chat.id, text, parse_mode="HTML", reply_markup=kb)
        del admin_states[msg.from_user.id]

    elif step == "add_admin_id":
        try:
            new_id = int(msg.text.strip())
        except ValueError:
            safe_send(msg.chat.id, "❗ ID raqam bo'lishi kerak")
            del admin_states[msg.from_user.id]
            return
        db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (new_id,))
        cache_del(f"admin_{new_id}")
        safe_send(msg.chat.id, f"✅ Admin qo'shildi: <code>{new_id}</code>", parse_mode="HTML")
        try:
            safe_send(new_id, "🎉 Siz admin bo'ldingiz! /start bosing.")
        except:
            pass
        del admin_states[msg.from_user.id]
        show_admins_panel(msg.chat.id, True)

    elif step == "kanal_set":
        username = msg.text.strip()
        if not username.startswith("@"):
            username = "@" + username
        try:
            bot.get_chat(username)
            channels = get_channels()
            if username in channels:
                safe_send(msg.chat.id, f"⚠️ {username} allaqachon qo'shilgan!")
                del admin_states[msg.from_user.id]
                return
            if len(channels) >= 5:
                safe_send(msg.chat.id, "❌ Maksimum 5 ta kanal qo'shish mumkin!")
                del admin_states[msg.from_user.id]
                return
            channels.append(username)
            save_channels(channels)
            safe_send(msg.chat.id,
                f"✅ Kanal qo'shildi: <b>{username}</b>\nJami: {len(channels)}/5 ta kanal",
                parse_mode="HTML", reply_markup=admin_keyboard(msg.from_user.id))
        except:
            safe_send(msg.chat.id,
                "❌ Kanal topilmadi!\n\n⚠️ Bot kanalga admin bo'lishi kerak.", parse_mode="HTML")
        del admin_states[msg.from_user.id]

    elif step == "send_to_user":
        target_id = state["data"]["target_id"]
        try:
            bot.copy_message(target_id, msg.chat.id, msg.message_id)
            safe_send(msg.chat.id, f"✅ Xabar yuborildi <code>{target_id}</code> ga", parse_mode="HTML")
        except Exception as e:
            safe_send(msg.chat.id, f"❌ Xato: {str(e)[:200]}")
        del admin_states[msg.from_user.id]

    elif step == "broadcast":
        users = db_execute("SELECT id FROM bot_users", fetch=True) or []
        total = len(users)
        safe_send(msg.chat.id, f"📨 Yuborish boshlandi... ({total} ta user)")
        sent = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = executor.map(lambda uid: send_to_user(uid[0], msg), users)
            for result in results:
                if result:
                    sent += 1
                else:
                    failed += 1
        safe_send(msg.chat.id, f"✅ Yuborildi: {sent}\n❌ Yuborilmadi: {failed}\n📊 Jami: {total}")
        del admin_states[msg.from_user.id]

@bot.message_handler(func=lambda m: True)
def get_movie(msg):
    if is_admin(msg.from_user.id):
        return
    is_subbed, not_subbed = check_sub(msg.from_user.id)
    if not is_subbed:
        safe_send(msg.chat.id,
            "❗ Avval quyidagi kanal(lar)ga obuna bo'ling:",
            reply_markup=sub_keyboard(not_subbed))
        return
    code = msg.text.strip()
    movie = db_execute("SELECT name FROM movies WHERE code = %s", (code,), fetchone=True)
    if not movie:
        safe_send(msg.chat.id, "❌ Bunday kodli kino topilmadi")
        return
    name = movie[0]
    parts = db_execute("SELECT part_num, file_id FROM movie_parts WHERE code=%s ORDER BY part_num",
                       (code,), fetch=True) or []
    if not parts:
        safe_send(msg.chat.id, "❌ Kino fayli topilmadi")
        return
    channels = get_channels()
    ch_text = channels[0] if channels else ""
    db_execute("UPDATE movies SET views = views + 1 WHERE code = %s", (code,))
    for part_num, file_id in parts:
        if len(parts) == 1:
            caption = f"🎬 <b>{name}</b>\n\n📢 {ch_text}"
        else:
            caption = f"🎬 <b>{name}</b> — {part_num}-qism\n\n📢 {ch_text}"
        try:
            bot.send_video(msg.chat.id, file_id, caption=caption, parse_mode="HTML", protect_content=True)
        except Exception as e:
            logging.error(f"Video send error: {e}")
            try:
                bot.send_document(msg.chat.id, file_id, caption=caption, parse_mode="HTML", protect_content=True)
            except:
                safe_send(msg.chat.id, f"❌ {part_num}-qism yuborishda xatolik")

def keep_alive():
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not render_url:
        return
    while True:
        try:
            time.sleep(14 * 60)
            urllib.request.urlopen(render_url + "/health", timeout=10)
            logging.info("Keep-alive ping ✅")
        except Exception as e:
            logging.warning(f"Keep-alive xato: {e}")

def run_bot():
    while True:
        try:
            logging.info("Bot ishga tushdi...")
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            logging.error(f"Bot error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()
    run_bot()
