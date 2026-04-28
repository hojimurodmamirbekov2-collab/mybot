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

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_ENV = [int(x.strip()) for x in os.getenv("ADMIN_ID", "0").split(",") if x.strip().isdigit()]
MAIN_ADMIN_ID = ADMIN_IDS_ENV[0] if ADMIN_IDS_ENV else 0
CHANNEL = os.getenv("CHANNEL", "")
DATABASE_URL = os.getenv("DATABASE_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

bot = telebot.TeleBot(TOKEN, threaded=True)

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
    db_execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            id BIGINT PRIMARY KEY,
            name TEXT,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS movies (
            code TEXT PRIMARY KEY,
            name TEXT,
            file_id TEXT,
            views INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id BIGINT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for aid in ADMIN_IDS_ENV:
        db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (aid,))
    logging.info("Database tayyor ✅")

admin_states = {}

def is_admin(user_id):
    if user_id in ADMIN_IDS_ENV:
        return True
    result = db_execute("SELECT 1 FROM admins WHERE id = %s", (user_id,), fetchone=True)
    return result is not None

def is_main_admin(user_id):
    return user_id == MAIN_ADMIN_ID

def check_sub(user_id):
    if not CHANNEL:
        return True
    try:
        status = bot.get_chat_member(CHANNEL, user_id).status
        return status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Sub check error: {e}")
        return False

def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logging.error(f"Send error: {e}")

def sub_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Kanalga obuna", url=f"https://t.me/{CHANNEL[1:]}"))
    kb.add(types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_sub"))
    return kb

def admin_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Kino qo'shish", "🗑 Kino o'chirish")
    kb.add("📊 Statistika", "📋 Kinolar ro'yxati")
    kb.add("👥 Userlar", "📨 Reklama yuborish")
    return kb

@bot.message_handler(commands=['addadmin'])
def add_admin_cmd(msg):
    if not is_main_admin(msg.from_user.id):
        safe_send(msg.chat.id, "❌ Faqat asosiy admin bu komandadan foydalana oladi")
        return
    try:
        new_admin_id = int(msg.text.split()[1])
        db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (new_admin_id,))
        safe_send(msg.chat.id, f"✅ Yangi admin qo'shildi: <code>{new_admin_id}</code>", parse_mode="HTML")
        try:
            safe_send(new_admin_id, "🎉 Tabriklaymiz! Siz endi botning admin'isiz.\n\n/start bosing — admin paneli ochiladi.")
        except:
            pass
    except (IndexError, ValueError):
        safe_send(msg.chat.id, "❗ Format: <code>/addadmin USER_ID</code>\n\nMisol: <code>/addadmin 123456789</code>", parse_mode="HTML")

@bot.message_handler(commands=['removeadmin'])
def remove_admin_cmd(msg):
    if not is_main_admin(msg.from_user.id):
        safe_send(msg.chat.id, "❌ Faqat asosiy admin bu komandadan foydalana oladi")
        return
    try:
        rm_id = int(msg.text.split()[1])
        if rm_id == MAIN_ADMIN_ID:
            safe_send(msg.chat.id, "❌ Asosiy adminni o'chirib bo'lmaydi")
            return
        result = db_execute("DELETE FROM admins WHERE id = %s RETURNING id", (rm_id,), fetchone=True)
        if result:
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
    if not admins:
        safe_send(msg.chat.id, "📭 Hech qanday admin yo'q")
        return
    text = "👑 <b>Adminlar ro'yxati:</b>\n\n"
    for (aid,) in admins:
        marker = " 👑 (asosiy)" if aid == MAIN_ADMIN_ID else ""
        text += f"• <code>{aid}</code>{marker}\n"
    safe_send(msg.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=['myid'])
def my_id_cmd(msg):
    safe_send(msg.chat.id, f"🆔 Sizning ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")

@bot.message_handler(commands=['start'])
def start(msg):
    user = msg.from_user

    db_execute(
        "INSERT INTO bot_users (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (user.id, user.first_name)
    )

    if is_admin(user.id):
        safe_send(msg.chat.id, "👑 Salom, Admin!", reply_markup=admin_keyboard())
        return

    if not check_sub(user.id):
        safe_send(msg.chat.id, "❗ Botdan foydalanish uchun kanalga obuna bo'ling:", reply_markup=sub_keyboard())
        return

    safe_send(msg.chat.id, "🎬 Salom! Kino kodini yuboring:")
    try:
        safe_send(MAIN_ADMIN_ID, f"👤 Yangi user: {user.id} | {user.first_name}")
    except:
        pass

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_callback(call):
    if check_sub(call.from_user.id):
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass
        safe_send(call.message.chat.id, "✅ Rahmat! Endi kino kodini yuboring:")
    else:
        bot.answer_callback_query(call.id, "❗ Hali obuna bo'lmagansiz!", show_alert=True)

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "➕ Kino qo'shish")
def add_movie_start(msg):
    admin_states[msg.from_user.id] = {"step": "code", "data": {}}
    safe_send(msg.chat.id, "🔢 Kino kodini yuboring (masalan: 123):")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "🗑 Kino o'chirish")
def delete_movie_start(msg):
    admin_states[msg.from_user.id] = {"step": "delete", "data": {}}
    safe_send(msg.chat.id, "🗑 O'chiriladigan kino kodini yuboring:")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📊 Statistika")
def stats(msg):
    users_count = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    movies_count = db_execute("SELECT COUNT(*) FROM movies", fetchone=True)[0]
    total_views = db_execute("SELECT COALESCE(SUM(views), 0) FROM movies", fetchone=True)[0]

    text = (
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users_count}</b>\n"
        f"🎬 Kinolar: <b>{movies_count}</b>\n"
        f"👁 Jami ko'rishlar: <b>{total_views}</b>"
    )
    safe_send(msg.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "📋 Kinolar ro'yxati")
def movie_list(msg):
    movies = db_execute("SELECT code, name, views FROM movies ORDER BY added_at DESC LIMIT 50", fetch=True)
    if not movies:
        safe_send(msg.chat.id, "📭 Hech qanday kino yo'q")
        return

    text = "📋 <b>Kinolar:</b>\n\n"
    for code, name, views in movies:
        text += f"🔢 <code>{code}</code> — {name} (👁 {views})\n"
    safe_send(msg.chat.id, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.text == "👥 Userlar")
def users_menu(msg):
    total = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    today = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours'", fetchone=True)[0]
    week = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '7 days'", fetchone=True)[0]

    text = (
        f"👥 <b>Foydalanuvchilar</b>\n\n"
        f"📊 Jami: <b>{total}</b>\n"
        f"📅 Bugun qo'shilgan: <b>{today}</b>\n"
        f"🗓 Hafta davomida: <b>{week}</b>"
    )

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📋 Ro'yxat (oxirgi 20)", callback_data="users_list_0"),
        types.InlineKeyboardButton("🆕 Yangi userlar", callback_data="users_recent")
    )
    kb.add(types.InlineKeyboardButton("🔍 User qidirish (ID bo'yicha)", callback_data="users_search"))
    safe_send(msg.chat.id, text, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("users_list_"))
def users_list_callback(call):
    if not is_admin(call.from_user.id):
        return
    offset = int(call.data.split("_")[-1])
    limit = 20
    users = db_execute(
        "SELECT id, name, joined_at FROM bot_users ORDER BY joined_at DESC LIMIT %s OFFSET %s",
        (limit, offset),
        fetch=True
    ) or []
    total = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]

    if not users:
        bot.answer_callback_query(call.id, "📭 Boshqa userlar yo'q")
        return

    text = f"👥 <b>Userlar</b> ({offset + 1}–{offset + len(users)} / {total})\n\n"
    for uid, name, joined in users:
        name_safe = (name or "—").replace("<", "&lt;").replace(">", "&gt;")
        date_str = joined.strftime("%d.%m.%Y %H:%M") if joined else "—"
        text += f"🆔 <code>{uid}</code> — {name_safe}\n📅 {date_str}\n\n"

    kb = types.InlineKeyboardMarkup()
    nav = []
    if offset > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Oldingi", callback_data=f"users_list_{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(types.InlineKeyboardButton("Keyingi ➡️", callback_data=f"users_list_{offset + limit}"))
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
        fetch=True
    ) or []

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
    safe_send(call.message.chat.id, "🔍 User ID sini yuboring (masalan: <code>123456789</code>):", parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data == "users_back")
def users_back_callback(call):
    if not is_admin(call.from_user.id):
        return
    total = db_execute("SELECT COUNT(*) FROM bot_users", fetchone=True)[0]
    today = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '24 hours'", fetchone=True)[0]
    week = db_execute("SELECT COUNT(*) FROM bot_users WHERE joined_at >= NOW() - INTERVAL '7 days'", fetchone=True)[0]

    text = (
        f"👥 <b>Foydalanuvchilar</b>\n\n"
        f"📊 Jami: <b>{total}</b>\n"
        f"📅 Bugun qo'shilgan: <b>{today}</b>\n"
        f"🗓 Hafta davomida: <b>{week}</b>"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📋 Ro'yxat (oxirgi 20)", callback_data="users_list_0"),
        types.InlineKeyboardButton("🆕 Yangi userlar", callback_data="users_recent")
    )
    kb.add(types.InlineKeyboardButton("🔍 User qidirish (ID bo'yicha)", callback_data="users_search"))
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
    safe_send(call.message.chat.id, f"✉️ User <code>{target_id}</code> ga yubormoqchi bo'lgan xabaringizni yuboring (matn, rasm, video — istalgan):", parse_mode="HTML")
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("make_admin_"))
def make_admin_callback(call):
    if not is_main_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Faqat asosiy admin", show_alert=True)
        return
    target_id = int(call.data.split("_")[-1])
    db_execute("INSERT INTO admins (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (target_id,))
    bot.answer_callback_query(call.id, "✅ Admin qilindi!", show_alert=True)
    try:
        safe_send(target_id, "🎉 Tabriklaymiz! Siz endi botning admin'isiz.\n\n/start bosing — admin paneli ochiladi.")
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

@bot.message_handler(func=lambda m: is_admin(m.from_user.id) and m.from_user.id in admin_states, content_types=['text', 'video', 'document'])
def admin_steps(msg):
    state = admin_states[msg.from_user.id]
    step = state["step"]

    if step == "code":
        state["data"]["code"] = msg.text.strip()
        state["step"] = "name"
        safe_send(msg.chat.id, "📝 Kino nomini yuboring:")

    elif step == "name":
        state["data"]["name"] = msg.text.strip()
        state["step"] = "file"
        safe_send(msg.chat.id, "🎥 Endi kino faylini (video) yuboring:")

    elif step == "file":
        file_id = None
        if msg.video:
            file_id = msg.video.file_id
        elif msg.document:
            file_id = msg.document.file_id

        if not file_id:
            safe_send(msg.chat.id, "❗ Iltimos, video yuboring")
            return

        code = state["data"]["code"]
        name = state["data"]["name"]
        db_execute(
            "INSERT INTO movies (code, name, file_id) VALUES (%s, %s, %s) ON CONFLICT (code) DO UPDATE SET name=%s, file_id=%s",
            (code, name, file_id, name, file_id)
        )
        safe_send(msg.chat.id, f"✅ Kino qo'shildi!\n🔢 Kod: <code>{code}</code>\n📝 Nom: {name}", parse_mode="HTML")
        del admin_states[msg.from_user.id]

    elif step == "delete":
        code = msg.text.strip()
        result = db_execute("DELETE FROM movies WHERE code = %s RETURNING code", (code,), fetchone=True)
        if result:
            safe_send(msg.chat.id, f"✅ Kino o'chirildi: {code}")
        else:
            safe_send(msg.chat.id, "❌ Bunday kod topilmadi")
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
            safe_send(msg.chat.id, f"❌ ID <code>{uid}</code> bilan user topilmadi", parse_mode="HTML")
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

        is_subscribed = "✅ Ha" if check_sub(u_id) else "❌ Yo'q"
        is_user_admin = "👑 Ha" if is_admin(u_id) else "❌ Yo'q"

        text = (
            f"👤 <b>User ma'lumoti</b>\n\n"
            f"🆔 ID: <code>{u_id}</code>\n"
            f"👨 Ism: {full_name_safe}\n"
            f"📛 Username: {username}\n"
            f"📅 Qo'shilgan: {date_str}\n"
            f"📢 Obuna: {is_subscribed}\n"
            f"👑 Admin: {is_user_admin}"
        )

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✉️ Xabar yuborish", callback_data=f"msg_user_{u_id}"))
        if is_main_admin(msg.from_user.id) and not is_admin(u_id):
            kb.add(types.InlineKeyboardButton("👑 Admin qilish", callback_data=f"make_admin_{u_id}"))
        safe_send(msg.chat.id, text, parse_mode="HTML", reply_markup=kb)
        del admin_states[msg.from_user.id]

    elif step == "send_to_user":
        target_id = state["data"]["target_id"]
        try:
            bot.copy_message(target_id, msg.chat.id, msg.message_id)
            safe_send(msg.chat.id, f"✅ Xabar yuborildi user <code>{target_id}</code> ga", parse_mode="HTML")
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

    if not check_sub(msg.from_user.id):
        safe_send(msg.chat.id, "❗ Avval kanalga obuna bo'ling:", reply_markup=sub_keyboard())
        return

    code = msg.text.strip()
    movie = db_execute(
        "SELECT name, file_id FROM movies WHERE code = %s",
        (code,),
        fetchone=True
    )

    if movie:
        name, file_id = movie
        try:
            bot.send_video(msg.chat.id, file_id, caption=f"🎬 <b>{name}</b>\n\n📢 {CHANNEL}", parse_mode="HTML")
            db_execute("UPDATE movies SET views = views + 1 WHERE code = %s", (code,))
        except Exception as e:
            logging.error(f"Video send error: {e}")
            try:
                bot.send_document(msg.chat.id, file_id, caption=f"🎬 <b>{name}</b>", parse_mode="HTML")
                db_execute("UPDATE movies SET views = views + 1 WHERE code = %s", (code,))
            except:
                safe_send(msg.chat.id, "❌ Kino yuborishda xatolik")
    else:
        safe_send(msg.chat.id, "❌ Bunday kodli kino topilmadi")

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
    run_bot()
