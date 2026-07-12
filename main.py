import os
import re
import glob
import time
import logging
import threading
import subprocess
import urllib.request
import requests
import psycopg2
from psycopg2 import pool
from flask import Flask
from concurrent.futures import ThreadPoolExecutor
import telebot
from telebot import types
import yt_dlp

TOKEN        = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CHANNEL      = os.getenv("CHANNEL", "")
_admin_env   = os.getenv("ADMIN_ID", "0")
ADMIN_IDS    = [int(x.strip()) for x in _admin_env.split(",") if x.strip().isdigit()]
MAIN_ADMIN   = ADMIN_IDS[0] if ADMIN_IDS else 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

_cache: dict = {}

def kesh_ol(k):
    e = _cache.get(k)
    if e and time.time() < e["exp"]:
        return e["val"], True
    return None, False

def kesh_set(k, v, ttl=60):
    _cache[k] = {"val": v, "exp": time.time() + ttl}

def kesh_del(k):
    _cache.pop(k, None)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot ishlayapti"

@app.route("/health")
def health():
    return "OK"

def flask_start():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def db(sql, params=None, *, fetch=False, one=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if one:    result = cur.fetchone()
            elif fetch: result = cur.fetchall()
            else:       result = None
            conn.commit()
            return result
    except Exception as err:
        conn.rollback()
        log.error(f"DB: {err}")
        return None
    finally:
        db_pool.putconn(conn)

def init_db():
    db("CREATE TABLE IF NOT EXISTS bot_users (id BIGINT PRIMARY KEY, name TEXT, qoshildi TIMESTAMP DEFAULT NOW())")
    db("CREATE TABLE IF NOT EXISTS kinolar (kod TEXT PRIMARY KEY, nom TEXT, poster_id TEXT, korishlar INTEGER DEFAULT 0, qoshildi TIMESTAMP DEFAULT NOW())")
    db("ALTER TABLE kinolar ADD COLUMN IF NOT EXISTS poster_id TEXT")
    db("CREATE TABLE IF NOT EXISTS kino_qismlar (id SERIAL PRIMARY KEY, kod TEXT NOT NULL, qism_num INTEGER NOT NULL, fayl_id TEXT NOT NULL, UNIQUE(kod, qism_num))")
    db("CREATE TABLE IF NOT EXISTS adminlar (id BIGINT PRIMARY KEY, qoshildi TIMESTAMP DEFAULT NOW())")
    db("CREATE TABLE IF NOT EXISTS sozlamalar (kalit TEXT PRIMARY KEY, qiymat TEXT)")
    for aid in ADMIN_IDS:
        db("INSERT INTO adminlar (id) VALUES (%s) ON CONFLICT DO NOTHING", (aid,))
    if CHANNEL:
        db("INSERT INTO sozlamalar (kalit, qiymat) VALUES ('kanallar', %s) ON CONFLICT DO NOTHING", (CHANNEL,))

def kanallar_ol():
    v, ok = kesh_ol("kanallar")
    if ok: return v
    row = db("SELECT qiymat FROM sozlamalar WHERE kalit='kanallar'", one=True)
    r = [k.strip() for k in row[0].split(",") if k.strip()] if row and row[0] else ([CHANNEL] if CHANNEL else [])
    kesh_set("kanallar", r, ttl=300)
    return r

def kanallar_saqlash(lst):
    v = ",".join(lst)
    db("INSERT INTO sozlamalar (kalit, qiymat) VALUES ('kanallar',%s) ON CONFLICT (kalit) DO UPDATE SET qiymat=%s", (v, v))
    kesh_del("kanallar")

def admin_mi(uid):
    if uid in ADMIN_IDS: return True
    v, ok = kesh_ol(f"adm_{uid}")
    if ok: return v
    r = db("SELECT 1 FROM adminlar WHERE id=%s", (uid,), one=True)
    kesh_set(f"adm_{uid}", r is not None, ttl=120)
    return r is not None

def bosh_admin(uid):
    return uid == MAIN_ADMIN

def obuna_tekshir(uid):
    kanallar = kanallar_ol()
    if not kanallar: return True, []
    v, ok = kesh_ol(f"sub_{uid}")
    if ok: return v
    ulanmagan = []
    for k in kanallar:
        try:
            if bot.get_chat_member(k, uid).status not in ("member","administrator","creator"):
                ulanmagan.append(k)
        except: pass
    r = (len(ulanmagan) == 0, ulanmagan)
    kesh_set(f"sub_{uid}", r, ttl=60)
    return r

def yuborish(chat_id, matn, **kw):
    try: return bot.send_message(chat_id, matn, **kw)
    except Exception as e: log.error(f"Send: {e}")

def user_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📥 Video yuklab olish", "🎵 Musiqa yuklab olish")
    kb.add("🔍 Musiqa qidirish",    "🔵 Dumaloq video")
    kb.add("🖼 Rasm orqali kino topish")
    return kb

def admin_kb(uid=None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Kino qo'shish",   "🗑 Kino o'chirish")
    kb.add("📊 Statistika",       "📋 Kinolar ro'yxati")
    kb.add("👥 Foydalanuvchilar", "📨 Reklama yuborish")
    kb.add("👑 Adminlar",         "⚙️ Sozlamalar")
    if uid and bosh_admin(uid):
        kb.add("➕ Admin qo'shish", "❌ Admin o'chirish")
    return kb

def obuna_kb(lst):
    kb = types.InlineKeyboardMarkup()
    for k in lst:
        kb.add(types.InlineKeyboardButton(f"📢 {k} ga obuna bo'lish", url=f"https://t.me/{k.lstrip('@')}"))
    kb.add(types.InlineKeyboardButton("✅ Obuna bo'ldim, tekshir", callback_data="obuna_tekshir"))
    return kb

def effektlar_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(n, callback_data=k) for k,(n,_) in EFFEKTLAR.items()]
    for i in range(0, len(btns), 2): kb.row(*btns[i:i+2])
    return kb

def qism_kb(kod):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Yana qism qo'shish", callback_data=f"qism_{kod}"))
    kb.add(types.InlineKeyboardButton("✅ Tayyor, saqlash",    callback_data="qism_tayyor"))
    return kb

def soz_kb():
    kanallar = kanallar_ol()
    matn = "⚙️ <b>Sozlamalar</b>\n\n"
    matn += f"📢 Kanallar ({len(kanallar)}/5):\n" if kanallar else "📢 Kanal ulanmagan\n"
    for i,k in enumerate(kanallar,1): matn += f"  {i}. <b>{k}</b>\n"
    kb = types.InlineKeyboardMarkup()
    if len(kanallar) < 5:
        kb.add(types.InlineKeyboardButton("➕ Kanal/Guruh qo'shish", callback_data="kanal_qosh"))
    for i,k in enumerate(kanallar):
        kb.add(types.InlineKeyboardButton(f"❌ {k} ni o'chirish", callback_data=f"kanal_del_{i}"))
    return matn, kb

def adminlar_paneli(chat_id, bosh):
    lst  = db("SELECT id FROM adminlar ORDER BY qoshildi", fetch=True) or []
    matn = "👑 <b>Adminlar:</b>\n\n"
    kb   = types.InlineKeyboardMarkup()
    for (aid,) in lst:
        matn += f"• <code>{aid}</code>{' 👑 (asosiy)' if aid==MAIN_ADMIN else ''}\n"
        if bosh and aid != MAIN_ADMIN:
            kb.add(types.InlineKeyboardButton(f"❌ {aid}", callback_data=f"admin_del_{aid}"))
    if bosh:
        kb.add(types.InlineKeyboardButton("➕ Admin qo'shish", callback_data="admin_qosh"))
    yuborish(chat_id, matn, parse_mode="HTML", reply_markup=kb)

EFFEKTLAR = {
    "eff_bass":      ("🔊 Bass Boost",     "bass=g=10"),
    "eff_treble":    ("🎵 Treble Boost",   "treble=g=8"),
    "eff_echo":      ("🌊 Echo",           "aecho=0.8:0.88:60:0.4"),
    "eff_slrev":     ("🌙 Slowed+Reverb",  "atempo=0.85,aecho=0.8:0.9:1000:0.3"),
    "eff_speed":     ("⚡ Tezlashtirish",  "atempo=1.5"),
    "eff_slow":      ("🐌 Sekinlashtirish","atempo=0.75"),
    "eff_nightcore": ("🎤 Nightcore",      "aresample=48000,asetrate=60000,atempo=0.8"),
    "eff_volume":    ("📢 Volume+",        "volume=2"),
}

def effekt_qollan(kirish, filtr):
    chiq = f"/tmp/eff_{int(time.time()*1000)}.mp3"
    try:
        r = subprocess.run(["ffmpeg","-y","-i",kirish,"-af",filtr,"-codec:a","libmp3lame","-q:a","3",chiq], capture_output=True, timeout=120)
        if os.path.exists(chiq) and os.path.getsize(chiq) > 0: return chiq
    except Exception as e: log.error(e)
    return None

def dumaloq_video(kirish):
    chiq = f"/tmp/doira_{int(time.time()*1000)}.mp4"
    vf = "crop=min(iw\\,ih):min(iw\\,ih),scale=384:384"
    try:
        r = subprocess.run(["ffmpeg","-y","-i",kirish,"-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","28","-c:a","aac","-b:a","96k","-t","60","-movflags","+faststart","-pix_fmt","yuv420p",chiq], capture_output=True, timeout=180)
        if os.path.exists(chiq) and os.path.getsize(chiq) > 0: return chiq
    except Exception as e: log.error(e)
    return None

PLATFORMALAR = ["youtube.com","youtu.be","tiktok.com","instagram.com","pinterest.com","twitter.com","x.com","facebook.com","vk.com","dailymotion.com","vimeo.com","twitch.tv","soundcloud.com","reddit.com","ok.ru","rumble.com"]

def url_mi(t):
    t = t.strip().lower()
    return any(p in t for p in PLATFORMALAR) or (t.startswith("http") and "." in t)

def media_yukla(url, audio=False, max_mb=49):
    ts   = int(time.time()*1000)
    tmpl = f"/tmp/ytdl_{ts}.%(ext)s"
    umumiy = {"outtmpl":tmpl,"quiet":True,"no_warnings":True,"noplaylist":True}
    if audio:
        opts = {**umumiy,"format":"bestaudio/best","postprocessors":[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]}
    else:
        opts = {**umumiy,"format":f"best[filesize<{max_mb}M][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]/best","merge_output_format":"mp4"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            nom, thumb, dur, muallif = info.get("title","Video"), info.get("thumbnail",""), info.get("duration",0), info.get("uploader","")
        fls = glob.glob(f"/tmp/ytdl_{ts}*")
        if not fls: return None, nom, thumb, dur, muallif, "topilmadi"
        f = fls[0]
        if os.path.getsize(f)/1024/1024 > max_mb:
            [os.remove(x) for x in fls]
            return None, nom, thumb, dur, muallif, "hajm"
        return f, nom, thumb, dur, muallif, None
    except Exception as e:
        m = str(e).lower()
        xato = "yopiq" if "private" in m else "yosh" if "age" in m else "mavjud_emas" if "unavailab" in m else str(e)[:100]
        return None, None, None, 0, None, xato

def musiqa_qidirish(sorov, n=4):
    opts = {"quiet":True,"no_warnings":True,"extract_flat":True,"noplaylist":True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"ytsearch{n}:{sorov}", download=False)
            lst = []
            for e in res.get("entries",[]):
                if not e: continue
                dur = e.get("duration",0) or 0
                m,s = divmod(int(dur),60)
                lst.append({"nom":e.get("title","Nomsiz"),"url":f"https://youtube.com/watch?v={e.get('id','')}","davomiy":f"{m}:{s:02d}","thumb":e.get("thumbnail",""),"id":e.get("id",""),"muallif":e.get("uploader","")})
            return lst
    except: return []

def rasmdan_aniqla(fayl):
    try:
        h = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36","Accept-Language":"ru-RU,ru;q=0.9,en;q=0.8"}
        with open(fayl,"rb") as f:
            r = requests.post("https://lens.google.com/upload?ep=gsbubb&hl=ru&re=df", files={"encoded_image":("photo.jpg",f,"image/jpeg")}, headers=h, allow_redirects=True, timeout=20)
        t = r.text
        topilgan = []
        for pat in [r'"knowledgeGraphEntities"[^}]*?"title"\s*:\s*"([^"]{2,80})"',r'"title"\s*:\s*"([^"]{3,80})"',r'"text"\s*:\s*"([A-Za-z0-9\u0400-\u04FF\s\-\:\'\"\!]{4,60})"']:
            for m in re.findall(pat, t):
                m = m.strip()
                tashla = ["Google","Search","Images","Lens","http","www","com","null","true","false"]
                if m and len(m)>3 and m not in topilgan and not any(x.lower() in m.lower() for x in tashla):
                    topilgan.append(m)
        return topilgan[:8]
    except Exception as e:
        log.error(e); return []

def bazadan_qidirish(sorov):
    return db("SELECT kod,nom,poster_id,korishlar FROM kinolar WHERE LOWER(nom) LIKE %s ORDER BY korishlar DESC LIMIT 5", (f"%{sorov.lower()}%",), fetch=True) or []

def davomiylik(s):
    if not s: return ""
    m,s = divmod(int(s),60); h,m = divmod(m,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def yuklab_xato(chat_id, xato):
    xabarlar = {"hajm":"❌ Video 49 MB dan katta","yopiq":"❌ Bu video yopiq (private)","yosh":"❌ Yosh chekloviga ega","mavjud_emas":"❌ Video mavjud emas"}
    yuborish(chat_id, xabarlar.get(xato, f"❌ Yuklab bo'lmadi: {xato[:100]}" if xato else "❌ Yuklab bo'lmadi"))

def video_yuborish(chat_id, fayl, nom, dur, muallif):
    cap = f"🎬 <b>{nom}</b>"
    if muallif: cap += f"\n👤 {muallif}"
    d = davomiylik(dur)
    if d: cap += f"\n⏱ {d}"
    try:
        with open(fayl,"rb") as f: bot.send_video(chat_id, f, caption=cap, parse_mode="HTML", supports_streaming=True)
    except:
        try:
            with open(fayl,"rb") as f: bot.send_document(chat_id, f, caption=cap, parse_mode="HTML")
        except: yuborish(chat_id,"❌ Yuborishda xatolik")
    finally:
        try: os.remove(fayl)
        except: pass

def audio_yuborish(chat_id, fayl, nom, dur, muallif):
    cap = f"🎵 <b>{nom}</b>"
    if muallif: cap += f"\n👤 {muallif}"
    d = davomiylik(dur)
    if d: cap += f"\n⏱ {d}"
    cap += "\n\n🎛 Effekt tanlang:"
    try:
        with open(fayl,"rb") as f: bot.send_audio(chat_id, f, title=nom, caption=cap, parse_mode="HTML", reply_markup=effektlar_kb())
    except: yuborish(chat_id,"❌ Yuborishda xatolik")

admin_holat: dict = {}
user_holat:  dict = {}
audio_kesh:  dict = {}

@bot.message_handler(commands=["start"])
def start(msg):
    uid = msg.from_user.id
    db("INSERT INTO bot_users (id,name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (uid, msg.from_user.first_name or ""))
    if admin_mi(uid):
        yuborish(msg.chat.id, f"👑 Salom, Admin!", reply_markup=admin_kb(uid)); return
    ok, ul = obuna_tekshir(uid)
    if not ok:
        yuborish(msg.chat.id,"❗ Botdan foydalanish uchun kanallarga obuna bo'ling:", reply_markup=obuna_kb(ul)); return
    yuborish(msg.chat.id, "🎬 <b>Salom!</b>\n\n🔢 Kino kodini yuboring\n📸 Kino rasmi yuboring — bot topadi!\n📥 Yoki quyidagi tugmalar:", parse_mode="HTML", reply_markup=user_kb())

@bot.message_handler(commands=["myid"])
def myid(msg):
    yuborish(msg.chat.id, f"🆔 Sizning ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data=="obuna_tekshir")
def obuna_cb(call):
    uid = call.from_user.id
    kesh_del(f"sub_{uid}")
    ok, ul = obuna_tekshir(uid)
    if ok:
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        yuborish(call.message.chat.id,"✅ Rahmat! Kino kodini yuboring:", reply_markup=user_kb())
    else:
        bot.answer_callback_query(call.id,"❗ Hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and m.text=="📥 Video yuklab olish")
def video_btn(msg):
    user_holat[msg.from_user.id] = {"rejim":"video"}
    yuborish(msg.chat.id,"📥 <b>Video yuklab olish</b>\n\nYouTube, TikTok, Instagram, Pinterest, Twitter, Facebook, VK, Dailymotion, Vimeo, Twitch, SoundCloud, Reddit...\n\n🔗 Havola yuboring:\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and m.text=="🎵 Musiqa yuklab olish")
def musiqa_btn(msg):
    user_holat[msg.from_user.id] = {"rejim":"musiqa"}
    yuborish(msg.chat.id,"🎵 <b>Musiqa yuklab olish</b>\n\nYouTube yoki SoundCloud havolasini yuboring:\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and m.text=="🔍 Musiqa qidirish")
def qidiruv_btn(msg):
    user_holat[msg.from_user.id] = {"rejim":"qidiruv"}
    yuborish(msg.chat.id,"🔍 <b>Musiqa qidirish</b>\n\nQo'shiq nomini yuboring:\nMisol: <code>Dildora Niyozova Alvido</code>\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and m.text=="🔵 Dumaloq video")
def doira_btn(msg):
    user_holat[msg.from_user.id] = {"rejim":"doira"}
    yuborish(msg.chat.id,"🔵 <b>Dumaloq video</b>\n\nVideo yuboring — bot doira shaklga o'tkazadi!\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and m.text=="🖼 Rasm orqali kino topish")
def rasm_btn(msg):
    user_holat[msg.from_user.id] = {"rejim":"rasm"}
    yuborish(msg.chat.id,"🖼 <b>Rasm orqali kino topish</b>\n\nKino posteri yoki kadrini yuboring!\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data in EFFEKTLAR)
def effekt_cb(call):
    uid = call.from_user.id
    if uid not in audio_kesh:
        bot.answer_callback_query(call.id,"❌ Audio topilmadi, qaytadan yuklab oling", show_alert=True); return
    nom_e, filtr = EFFEKTLAR[call.data]
    bot.answer_callback_query(call.id, f"⏳ {nom_e} qo'llanmoqda...")
    kirish = audio_kesh[uid].get("fayl")
    url    = audio_kesh[uid].get("url")
    nom    = audio_kesh[uid].get("nom","Audio")
    if not kirish or not os.path.exists(kirish):
        if not url: yuborish(call.message.chat.id,"❌ Audio topilmadi"); return
        k = yuborish(call.message.chat.id,"⏬ Qayta yuklanmoqda...")
        fayl,_,_,_,_,xato = media_yukla(url, audio=True)
        try: bot.delete_message(call.message.chat.id, k.message_id)
        except: pass
        if xato or not fayl: yuklab_xato(call.message.chat.id, xato); return
        kirish = fayl; audio_kesh[uid]["fayl"] = fayl
    k = yuborish(call.message.chat.id, f"⚙️ {nom_e} qo'llanmoqda...")
    chiq = effekt_qollan(kirish, filtr)
    try: bot.delete_message(call.message.chat.id, k.message_id)
    except: pass
    if chiq:
        try:
            with open(chiq,"rb") as f: bot.send_audio(call.message.chat.id, f, title=f"{nom_e} — {nom}", caption=f"🎵 <b>{nom_e}</b>\n🎶 {nom}", parse_mode="HTML", reply_markup=effektlar_kb())
        finally:
            try: os.remove(chiq)
            except: pass
    else:
        yuborish(call.message.chat.id,"❌ Effekt qo'llashda xatolik")

@bot.callback_query_handler(func=lambda c: c.data.startswith("yukla_musiqa_"))
def yukla_musiqa_cb(call):
    vid_id = call.data[len("yukla_musiqa_"):]
    url = f"https://youtube.com/watch?v={vid_id}"
    bot.answer_callback_query(call.id,"⏬ Yuklanmoqda...")
    k = yuborish(call.message.chat.id,"⏬ Yuklanmoqda...")
    fayl,nom,_,dur,muallif,xato = media_yukla(url, audio=True)
    try: bot.delete_message(call.message.chat.id, k.message_id)
    except: pass
    if xato or not fayl: yuklab_xato(call.message.chat.id, xato); return
    audio_kesh[call.from_user.id] = {"url":url,"fayl":fayl,"nom":nom}
    audio_yuborish(call.message.chat.id, fayl, nom, dur, muallif)

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id), content_types=["photo"])
def foto_handler(msg):
    uid = msg.from_user.id
    ok, ul = obuna_tekshir(uid)
    if not ok: yuborish(msg.chat.id,"❗ Avval kanallarga obuna bo'ling:", reply_markup=obuna_kb(ul)); return
    k = yuborish(msg.chat.id,"🔍 Rasm tahlil qilinmoqda... ⏳")
    try:
        fi  = bot.get_file(msg.photo[-1].file_id)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{fi.file_path}"
        ts  = int(time.time()*1000)
        img = f"/tmp/rasm_{ts}.jpg"
        urllib.request.urlretrieve(url, img)
    except:
        try: bot.delete_message(msg.chat.id, k.message_id)
        except: pass
        yuborish(msg.chat.id,"❌ Rasm yuklab bo'lmadi"); return
    topilgan = rasmdan_aniqla(img)
    try: os.remove(img)
    except: pass
    try: bot.delete_message(msg.chat.id, k.message_id)
    except: pass
    if not topilgan:
        yuborish(msg.chat.id,"❌ Rasmdan kino aniqlanmadi.\n💡 Kino posteri yoki aniq kadr yuboring."); return
    baza = []
    for t in topilgan:
        for r in bazadan_qidirish(t):
            if r not in baza: baza.append(r)
    matn = f"🔍 <b>Natija:</b>\n\n🌐 Aniqlangan: <b>{topilgan[0]}</b>\n\n"
    if baza:
        matn += "🎬 <b>Botdagi kinolar:</b>\n"
        for kod,nom,pid,_ in baza[:5]: matn += f"• Kod: <code>{kod}</code> — {nom}\n"
        matn += "\n📩 Kodni yuboring → kino keladi!"
        if baza[0][2]:
            try: bot.send_photo(msg.chat.id, baza[0][2], caption=matn, parse_mode="HTML"); return
            except: pass
    else:
        matn += f"😔 Botda bu kino yo'q.\n💡 Nomi: <b>{topilgan[0]}</b>"
    yuborish(msg.chat.id, matn, parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id), content_types=["text","video","document"])
def asosiy(msg):
    uid  = msg.from_user.id
    ok, ul = obuna_tekshir(uid)
    if not ok: yuborish(msg.chat.id,"❗ Avval kanallarga obuna bo'ling:", reply_markup=obuna_kb(ul)); return
    holat = user_holat.get(uid,{})
    rejim = holat.get("rejim","")
    matn  = (msg.text or "").strip()

    if rejim == "video":
        user_holat.pop(uid,None)
        if not url_mi(matn): yuborish(msg.chat.id,"❌ Havola noto'g'ri"); return
        k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda... ⏳")
        fayl,nom,_,dur,muallif,xato = media_yukla(matn)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except: pass
        if xato or not fayl: yuklab_xato(msg.chat.id, xato); return
        video_yuborish(msg.chat.id, fayl, nom, dur, muallif)

    elif rejim == "musiqa":
        user_holat.pop(uid,None)
        if not url_mi(matn): yuborish(msg.chat.id,"❌ Havola noto'g'ri"); return
        k = yuborish(msg.chat.id,"⏬ Musiqa yuklanmoqda... 🎵")
        fayl,nom,_,dur,muallif,xato = media_yukla(matn, audio=True)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except: pass
        if xato or not fayl: yuklab_xato(msg.chat.id, xato); return
        audio_kesh[uid] = {"url":matn,"fayl":fayl,"nom":nom}
        audio_yuborish(msg.chat.id, fayl, nom, dur, muallif)

    elif rejim == "qidiruv":
        user_holat.pop(uid,None)
        if not matn: return
        k = yuborish(msg.chat.id, f"🔍 <b>{matn}</b> qidirilmoqda...", parse_mode="HTML")
        natija = musiqa_qidirish(matn)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except: pass
        if not natija: yuborish(msg.chat.id,"❌ Topilmadi. Boshqacha yozing."); return
        yuborish(msg.chat.id, f"🎵 <b>Natijalar: «{matn}»</b>", parse_mode="HTML")
        for item in natija:
            cap = f"🎵 <b>{item['nom']}</b>\n👤 {item['muallif']}\n⏱ {item['davomiy']}"
            kb  = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⬇️ Yuklab olish", callback_data=f"yukla_musiqa_{item['id']}"))
            try:
                if item["thumb"]: bot.send_photo(msg.chat.id, item["thumb"], caption=cap, parse_mode="HTML", reply_markup=kb)
                else: yuborish(msg.chat.id, cap, parse_mode="HTML", reply_markup=kb)
            except: yuborish(msg.chat.id, cap, parse_mode="HTML", reply_markup=kb)

    elif rejim == "doira":
        user_holat.pop(uid,None)
        kirish = None
        if msg.video:
            k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda...")
            try:
                fi = bot.get_file(msg.video.file_id)
                dl = f"https://api.telegram.org/file/bot{TOKEN}/{fi.file_path}"
                kirish = f"/tmp/doira_{int(time.time()*1000)}.mp4"
                urllib.request.urlretrieve(dl, kirish)
            except: yuborish(msg.chat.id,"❌ Video yuklab bo'lmadi"); return
            finally:
                try: bot.delete_message(msg.chat.id, k.message_id)
                except: pass
        elif msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
            k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda...")
            try:
                fi = bot.get_file(msg.document.file_id)
                dl = f"https://api.telegram.org/file/bot{TOKEN}/{fi.file_path}"
                kirish = f"/tmp/doira_{int(time.time()*1000)}.mp4"
                urllib.request.urlretrieve(dl, kirish)
            except: yuborish(msg.chat.id,"❌ Video yuklab bo'lmadi"); return
            finally:
                try: bot.delete_message(msg.chat.id, k.message_id)
                except: pass
        elif matn and url_mi(matn):
            k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda... ⏳")
            kirish,_,_,_,_,xato = media_yukla(matn)
            try: bot.delete_message(msg.chat.id, k.message_id)
            except: pass
            if xato or not kirish: yuklab_xato(msg.chat.id, xato); return
        else:
            yuborish(msg.chat.id,"❌ Video yuboring"); return
        k = yuborish(msg.chat.id,"🔵 Dumaloq videoga aylantirilmoqda... ⏳")
        chiq = dumaloq_video(kirish)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except: pass
        try: os.remove(kirish)
        except: pass
        if chiq:
            try:
                with open(chiq,"rb") as f: bot.send_video_note(msg.chat.id, f, length=384)
            except: yuborish(msg.chat.id,"❌ Dumaloq video yuborishda xatolik")
            finally:
                try: os.remove(chiq)
                except: pass
        else:
            yuborish(msg.chat.id,"❌ Aylantirshda xatolik — video formati mos kelmadi")

    else:
        if matn and url_mi(matn):
            k = yuborish(msg.chat.id,"⏬ Yuklanmoqda... ⏳")
            fayl,nom,_,dur,muallif,xato = media_yukla(matn)
            try: bot.delete_message(msg.chat.id, k.message_id)
            except: pass
            if xato or not fayl: yuklab_xato(msg.chat.id, xato); return
            video_yuborish(msg.chat.id, fayl, nom, dur, muallif); return
        if not matn: return
        kino = db("SELECT nom,poster_id FROM kinolar WHERE kod=%s", (matn,), one=True)
        if not kino: yuborish(msg.chat.id,"❌ Bunday kodli kino topilmadi"); return
        nom, poster_id = kino
        qismlar = db("SELECT qism_num,fayl_id FROM kino_qismlar WHERE kod=%s ORDER BY qism_num", (matn,), fetch=True) or []
        if not qismlar: yuborish(msg.chat.id,"❌ Kino fayli topilmadi"); return
        db("UPDATE kinolar SET korishlar=korishlar+1 WHERE kod=%s", (matn,))
        kanal = kanallar_ol()
        kanal_text = kanal[0] if kanal else ""
        if poster_id:
            try: bot.send_photo(msg.chat.id, poster_id, caption=f"🎬 <b>{nom}</b>", parse_mode="HTML")
            except: pass
        for qnum, fid in qismlar:
            cap = f"🎬 <b>{nom}</b>" + (f" — {qnum}-qism" if len(qismlar)>1 else "")
            if kanal_text: cap += f"\n\n📢 {kanal_text}"
            try: bot.send_video(msg.chat.id, fid, caption=cap, parse_mode="HTML")
            except:
                try: bot.send_document(msg.chat.id, fid, caption=cap, parse_mode="HTML")
                except: yuborish(msg.chat.id,f"❌ {qnum}-qism yuborishda xatolik")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="📊 Statistika")
def statistika(msg):
    u = db("SELECT COUNT(*) FROM bot_users",one=True)[0]
    k = db("SELECT COUNT(*) FROM kinolar",one=True)[0]
    q = db("SELECT COUNT(*) FROM kino_qismlar",one=True)[0]
    v = db("SELECT COALESCE(SUM(korishlar),0) FROM kinolar",one=True)[0]
    yuborish(msg.chat.id,f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: <b>{u}</b>\n🎬 Kinolar: <b>{k}</b>\n🎞 Qismlar: <b>{q}</b>\n👁 Ko'rishlar: <b>{v}</b>", parse_mode="HTML")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="📋 Kinolar ro'yxati")
def kinolar_list(msg):
    lst = db("SELECT k.kod,k.nom,k.korishlar,(SELECT COUNT(*) FROM kino_qismlar WHERE kod=k.kod) FROM kinolar k ORDER BY k.qoshildi DESC LIMIT 50", fetch=True)
    if not lst: yuborish(msg.chat.id,"📭 Hech qanday kino yo'q"); return
    matn = "📋 <b>Kinolar:</b>\n\n"
    for kod,nom,ko,q in lst:
        matn += f"🔢 <code>{kod}</code> — {nom} (👁{ko}" + (f" | 🎞{q}qism" if q>1 else "") + ")\n"
    yuborish(msg.chat.id, matn, parse_mode="HTML")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="👥 Foydalanuvchilar")
def foydalanuvchilar(msg):
    u = db("SELECT COUNT(*) FROM bot_users",one=True)[0]
    b = db("SELECT COUNT(*) FROM bot_users WHERE qoshildi>=NOW()-INTERVAL '24 hours'",one=True)[0]
    h = db("SELECT COUNT(*) FROM bot_users WHERE qoshildi>=NOW()-INTERVAL '7 days'",one=True)[0]
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📋 Ro'yxat", callback_data="user_list_0"))
    yuborish(msg.chat.id,f"👥 <b>Foydalanuvchilar</b>\n\n📊 Jami: <b>{u}</b>\n📅 Bugun: <b>{b}</b>\n🗓 Hafta: <b>{h}</b>", parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_list_"))
def user_list_cb(call):
    if not admin_mi(call.from_user.id): return
    offset = int(call.data.split("_")[-1])
    limit  = 20
    lst    = db("SELECT id,name,qoshildi FROM bot_users ORDER BY qoshildi DESC LIMIT %s OFFSET %s",(limit,offset),fetch=True) or []
    jami   = db("SELECT COUNT(*) FROM bot_users",one=True)[0]
    if not lst: bot.answer_callback_query(call.id,"📭 Boshqa yo'q"); return
    matn = f"👥 <b>Userlar</b> ({offset+1}–{offset+len(lst)}/{jami})\n\n"
    for uid,ism,v in lst:
        matn += f"🆔 <code>{uid}</code> — {(ism or '—').replace('<','&lt;')} | {v.strftime('%d.%m.%Y %H:%M') if v else '—'}\n"
    kb = types.InlineKeyboardMarkup()
    nav = []
    if offset>0: nav.append(types.InlineKeyboardButton("⬅️",callback_data=f"user_list_{max(0,offset-limit)}"))
    if offset+limit<jami: nav.append(types.InlineKeyboardButton("➡️",callback_data=f"user_list_{offset+limit}"))
    if nav: kb.row(*nav)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="👑 Adminlar")
def adminlar_menu(msg):
    adminlar_paneli(msg.chat.id, bosh_admin(msg.from_user.id))

@bot.message_handler(func=lambda m: bosh_admin(m.from_user.id) and m.text=="➕ Admin qo'shish")
def admin_qosh_btn(msg):
    admin_holat[msg.from_user.id] = {"qadam":"admin_id"}
    yuborish(msg.chat.id,"➕ Yangi admin ID sini yuboring:\n💡 Admin <code>/myid</code> yozib bilsin\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: bosh_admin(m.from_user.id) and m.text=="❌ Admin o'chirish")
def admin_ochir_btn(msg):
    lst = db("SELECT id FROM adminlar ORDER BY qoshildi",fetch=True) or []
    kb  = types.InlineKeyboardMarkup()
    bor = False
    for (aid,) in lst:
        if aid!=MAIN_ADMIN: kb.add(types.InlineKeyboardButton(f"❌ {aid}",callback_data=f"admin_del_{aid}")); bor=True
    if not bor: yuborish(msg.chat.id,"📭 O'chiriladigan admin yo'q"); return
    yuborish(msg.chat.id,"Qaysi adminni o'chirmoqchisiz?", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_del_"))
def admin_del_cb(call):
    if not bosh_admin(call.from_user.id): bot.answer_callback_query(call.id,"❌ Faqat asosiy admin",show_alert=True); return
    aid = int(call.data.split("_")[-1])
    if aid==MAIN_ADMIN: bot.answer_callback_query(call.id,"❌ Asosiy adminni o'chirib bo'lmaydi!",show_alert=True); return
    r = db("DELETE FROM adminlar WHERE id=%s RETURNING id",(aid,),one=True)
    if r:
        kesh_del(f"adm_{aid}")
        bot.answer_callback_query(call.id,f"✅ O'chirildi: {aid}",show_alert=True)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        adminlar_paneli(call.message.chat.id, True)
    else: bot.answer_callback_query(call.id,"❌ Topilmadi",show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data=="admin_qosh")
def admin_qosh_cb(call):
    if not bosh_admin(call.from_user.id): return
    admin_holat[call.from_user.id] = {"qadam":"admin_id"}
    bot.answer_callback_query(call.id)
    yuborish(call.message.chat.id,"➕ Yangi admin ID sini yuboring:\n💡 Admin <code>/myid</code> yozib bilsin\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="⚙️ Sozlamalar")
def sozlamalar_menu(msg):
    matn, kb = soz_kb()
    yuborish(msg.chat.id, matn, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data=="kanal_qosh")
def kanal_qosh_cb(call):
    if not admin_mi(call.from_user.id): return
    if len(kanallar_ol())>=5: bot.answer_callback_query(call.id,"❌ Maksimum 5 ta!",show_alert=True); return
    admin_holat[call.from_user.id] = {"qadam":"kanal_qosh"}
    bot.answer_callback_query(call.id)
    yuborish(call.message.chat.id,"📢 Kanal/guruh username yuboring:\nMisol: <code>@mening_kanalim</code>\n\n❌ Bekor: /start", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("kanal_del_"))
def kanal_del_cb(call):
    if not admin_mi(call.from_user.id): return
    idx = int(call.data.split("_")[-1])
    lst = kanallar_ol()
    if idx<len(lst):
        o = lst.pop(idx); kanallar_saqlash(lst)
        bot.answer_callback_query(call.id,f"✅ {o} o'chirildi!",show_alert=True)
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except: pass
        matn,kb = soz_kb(); yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)
    else: bot.answer_callback_query(call.id,"❌ Topilmadi",show_alert=True)

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="➕ Kino qo'shish")
def kino_qosh_btn(msg):
    admin_holat[msg.from_user.id] = {"qadam":"kod","malumot":{}}
    yuborish(msg.chat.id,"🔢 Kino kodini yuboring:\nMisol: <code>101</code> yoki <code>serial1</code>", parse_mode="HTML")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="🗑 Kino o'chirish")
def kino_ochir_btn(msg):
    admin_holat[msg.from_user.id] = {"qadam":"kino_ochir"}
    yuborish(msg.chat.id,"🗑 O'chiriladigan kino kodini yuboring:")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.text=="📨 Reklama yuborish")
def reklama_btn(msg):
    admin_holat[msg.from_user.id] = {"qadam":"reklama"}
    yuborish(msg.chat.id,"📨 Yubormoqchi bo'lgan xabaringizni yuboring:")

@bot.callback_query_handler(func=lambda c: c.data.startswith("qism_") and c.data!="qism_tayyor")
def qism_qosh_cb(call):
    if not admin_mi(call.from_user.id): return
    kod = call.data[len("qism_"):]
    q   = db("SELECT COUNT(*) FROM kino_qismlar WHERE kod=%s",(kod,),one=True)[0]
    admin_holat[call.from_user.id] = {"qadam":"qism_video","malumot":{"kod":kod,"qism_num":q+1}}
    bot.answer_callback_query(call.id)
    yuborish(call.message.chat.id,f"🎥 <b>{q+1}-qism</b> videosini yuboring:", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data=="qism_tayyor")
def qism_tayyor_cb(call):
    if not admin_mi(call.from_user.id): return
    bot.answer_callback_query(call.id,"✅ Kino saqlandi!")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except: pass

@bot.callback_query_handler(func=lambda c: c.data=="poster_skip")
def poster_skip_cb(call):
    if not admin_mi(call.from_user.id): return
    uid = call.from_user.id
    if uid in admin_holat and admin_holat[uid]["qadam"]=="poster":
        admin_holat[uid]["qadam"] = "video"
        bot.answer_callback_query(call.id)
        yuborish(call.message.chat.id,"🎥 <b>1-qism</b> videosini yuboring:", parse_mode="HTML")

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and m.from_user.id in admin_holat,
                     content_types=["text","video","document","photo"])
def admin_handler(msg):
    uid   = msg.from_user.id
    holat = admin_holat[uid]
    qadam = holat["qadam"]

    if qadam=="kod":
        holat["malumot"]["kod"] = (msg.text or "").strip()
        holat["qadam"] = "nom"
        yuborish(msg.chat.id,"📝 Kino/Serial nomini yuboring:")

    elif qadam=="nom":
        holat["malumot"]["nom"] = (msg.text or "").strip()
        holat["qadam"] = "poster"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⏭ O'tkazib yuborish",callback_data="poster_skip"))
        yuborish(msg.chat.id,"🖼 Kino posterini yuboring:\n\nYoki o'tkazib yuboring 👇", reply_markup=kb)

    elif qadam=="poster":
        if msg.photo: holat["malumot"]["poster"] = msg.photo[-1].file_id
        holat["qadam"] = "video"
        yuborish(msg.chat.id,"🎥 <b>1-qism</b> videosini yuboring:", parse_mode="HTML")

    elif qadam=="video":
        fid = (msg.video and msg.video.file_id) or (msg.document and msg.document.file_id)
        if not fid: yuborish(msg.chat.id,"❗ Video yuboring"); return
        kod    = holat["malumot"]["kod"]
        nom    = holat["malumot"]["nom"]
        poster = holat["malumot"].get("poster")
        db("INSERT INTO kinolar (kod,nom,poster_id) VALUES (%s,%s,%s) ON CONFLICT (kod) DO UPDATE SET nom=%s,poster_id=%s",(kod,nom,poster,nom,poster))
        db("INSERT INTO kino_qismlar (kod,qism_num,fayl_id) VALUES (%s,1,%s) ON CONFLICT (kod,qism_num) DO UPDATE SET fayl_id=%s",(kod,fid,fid))
        del admin_holat[uid]
        yuborish(msg.chat.id,f"✅ <b>{nom}</b> qo'shildi!\n🔢 Kod: <code>{kod}</code>\n\nYana qism qo'shishni xohlaysizmi?", parse_mode="HTML", reply_markup=qism_kb(kod))

    elif qadam=="qism_video":
        fid = (msg.video and msg.video.file_id) or (msg.document and msg.document.file_id)
        if not fid: yuborish(msg.chat.id,"❗ Video yuboring"); return
        kod  = holat["malumot"]["kod"]
        qnum = holat["malumot"]["qism_num"]
        db("INSERT INTO kino_qismlar (kod,qism_num,fayl_id) VALUES (%s,%s,%s) ON CONFLICT (kod,qism_num) DO UPDATE SET fayl_id=%s",(kod,qnum,fid,fid))
        del admin_holat[uid]
        jami = db("SELECT COUNT(*) FROM kino_qismlar WHERE kod=%s",(kod,),one=True)[0]
        yuborish(msg.chat.id,f"✅ <b>{qnum}-qism</b> qo'shildi! Jami: {jami} qism\n\nYana qism?", parse_mode="HTML", reply_markup=qism_kb(kod))

    elif qadam=="kino_ochir":
        kod = (msg.text or "").strip()
        db("DELETE FROM kino_qismlar WHERE kod=%s",(kod,))
        r = db("DELETE FROM kinolar WHERE kod=%s RETURNING kod",(kod,),one=True)
        yuborish(msg.chat.id,f"✅ O'chirildi: <code>{kod}</code>" if r else "❌ Bunday kod topilmadi", parse_mode="HTML")
        del admin_holat[uid]

    elif qadam=="admin_id":
        try: new_id = int((msg.text or "").strip())
        except: yuborish(msg.chat.id,"❗ ID raqam bo'lishi kerak"); del admin_holat[uid]; return
        db("INSERT INTO adminlar (id) VALUES (%s) ON CONFLICT DO NOTHING",(new_id,))
        kesh_del(f"adm_{new_id}")
        yuborish(msg.chat.id,f"✅ Admin qo'shildi: <code>{new_id}</code>", parse_mode="HTML")
        try: yuborish(new_id,"🎉 Siz admin bo'ldingiz! /start bosing.")
        except: pass
        del admin_holat[uid]
        adminlar_paneli(msg.chat.id, True)

    elif qadam=="kanal_qosh":
        u = (msg.text or "").strip()
        if not u.startswith("@"): u = "@"+u
        try:
            bot.get_chat(u)
            lst = kanallar_ol()
            if u in lst: yuborish(msg.chat.id,f"⚠️ {u} allaqachon qo'shilgan!")
            elif len(lst)>=5: yuborish(msg.chat.id,"❌ Maksimum 5 ta kanal!")
            else:
                lst.append(u); kanallar_saqlash(lst)
                yuborish(msg.chat.id,f"✅ {u} qo'shildi! Jami: {len(lst)}/5", parse_mode="HTML")
        except: yuborish(msg.chat.id,"❌ Kanal topilmadi!\n⚠️ Bot kanalga admin bo'lishi shart.")
        del admin_holat[uid]

    elif qadam=="reklama":
        lst  = db("SELECT id FROM bot_users",fetch=True) or []
        jami = len(lst)
        yuborish(msg.chat.id,f"📨 Yuborish boshlandi... ({jami} ta user)")
        def yuboruvchi(u):
            try: bot.copy_message(u[0], msg.chat.id, msg.message_id); return True
            except Exception as e:
                if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                    db("DELETE FROM bot_users WHERE id=%s",(u[0],))
                return False
        y = f = 0
        with ThreadPoolExecutor(max_workers=20) as ex:
            for r in ex.map(yuboruvchi, lst):
                if r: y+=1
                else: f+=1
        yuborish(msg.chat.id,f"✅ Yuborildi: {y}\n❌ Yuborilmadi: {f}\n📊 Jami: {jami}")
        del admin_holat[uid]

def keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL","")
    if not url: return
    while True:
        try:
            time.sleep(14*60)
            urllib.request.urlopen(url+"/health", timeout=10)
        except: pass

def run_bot():
    while True:
        try: bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e: log.error(e); time.sleep(5)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=flask_start, daemon=True).start()
    threading.Thread(target=keep_alive,  daemon=True).start()
    run_bot()
