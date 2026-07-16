import os, re, glob, time, logging, threading, subprocess, urllib.request, tempfile
import requests, psycopg2
from psycopg2 import pool
from flask import Flask
from concurrent.futures import ThreadPoolExecutor
import telebot
from telebot import types
import yt_dlp

try:
    import instaloader
    INSTA_OK = True
except ImportError:
    INSTA_OK = False

# ──────────────────────────────────────────────
#  KONFIGURATSIYA (barchasi ENV orqali)
# ──────────────────────────────────────────────

TOKEN         = os.getenv("BOT_TOKEN", "")
DATABASE_URL  = os.getenv("DATABASE_URL", "")
CHANNEL       = os.getenv("CHANNEL", "")
MAIN_ADMIN    = int(os.getenv("MAIN_ADMIN_ID", "7092119152"))
BOT_TAG       = "@" + (os.getenv("BOT_USERNAME", "kino_bot").lstrip("@"))
MAX_MB        = int(os.getenv("MAX_MB", "49"))
POOL_MIN      = int(os.getenv("DB_POOL_MIN", "2"))
POOL_MAX      = int(os.getenv("DB_POOL_MAX", "20"))
# Instagram/TikTok/Pinterest ko'p hollarda faqat cookie bilan ishonchli ishlaydi.
# Brauzerdan eksport qilingan cookies.txt faylini shu yo'lga qo'ying (Netscape formatida).
COOKIES_FILE  = os.getenv("COOKIES_FILE", "").strip()

if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN ENV o'rnatilmagan!")
if not DATABASE_URL:
    raise SystemExit("❌ DATABASE_URL ENV o'rnatilmagan!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=10)

# ──────────────────────────────────────────────
#  THREAD-XAVFSIZ KESH / HOLAT / FLOOD-CONTROL
# ──────────────────────────────────────────────

_cache: dict = {}
_cache_lock = threading.RLock()

def kesh_ol(k):
    with _cache_lock:
        e = _cache.get(k)
        if e and time.time() < e["exp"]:
            return e["val"], True
        return None, False

def kesh_set(k, v, ttl=60):
    with _cache_lock:
        _cache[k] = {"val": v, "exp": time.time() + ttl}

def kesh_del(k):
    with _cache_lock:
        _cache.pop(k, None)

admin_holat: dict = {}
user_holat:  dict = {}
audio_kesh:  dict = {}
_holat_lock = threading.RLock()

def holat_ol(d, uid):
    with _holat_lock:
        return d.get(uid)

def holat_set(d, uid, val):
    with _holat_lock:
        d[uid] = val

def holat_del(d, uid):
    with _holat_lock:
        d.pop(uid, None)

# Flood/DoS himoyasi — foydalanuvchi belgilangan vaqt oynasida cheklangan sondan
# ortiq og'ir amal (yuklab olish, qidiruv, rasm tahlili) yubora olmaydi.
_flood: dict = {}
_flood_lock = threading.RLock()

def flood_ok(uid, limit=5, oyna=6):
    now = time.time()
    with _flood_lock:
        arr = _flood.setdefault(uid, [])
        arr[:] = [t for t in arr if now - t < oyna]
        if len(arr) >= limit:
            return False
        arr.append(now)
        return True

app = Flask(__name__)
@app.route("/")
def home(): return "Bot ishlayapti"
@app.route("/health")
def health(): return "OK"
def flask_start():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)

# ──────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────

db_pool = None
def db(sql, params=None, *, fetch=False, one=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if one:     result = cur.fetchone()
            elif fetch: result = cur.fetchall()
            else:       result = None
            conn.commit()
            return result
    except Exception as err:
        conn.rollback(); log.error(f"DB xato: {err}"); return None
    finally:
        db_pool.putconn(conn)

def init_db():
    global db_pool
    db_pool = psycopg2.pool.SimpleConnectionPool(POOL_MIN, POOL_MAX, DATABASE_URL)
    db("CREATE TABLE IF NOT EXISTS bot_users (id BIGINT PRIMARY KEY, name TEXT, qoshildi TIMESTAMP DEFAULT NOW())")
    db("CREATE TABLE IF NOT EXISTS kinolar (kod TEXT PRIMARY KEY, nom TEXT, poster_id TEXT, korishlar INTEGER DEFAULT 0, qoshildi TIMESTAMP DEFAULT NOW())")
    db("CREATE TABLE IF NOT EXISTS kino_qismlar (id SERIAL PRIMARY KEY, kod TEXT NOT NULL, qism_num INTEGER NOT NULL, fayl_id TEXT NOT NULL, UNIQUE(kod,qism_num))")
    db("CREATE TABLE IF NOT EXISTS adminlar (id BIGINT PRIMARY KEY, qoshildi TIMESTAMP DEFAULT NOW())")
    db("CREATE TABLE IF NOT EXISTS sozlamalar (kalit TEXT PRIMARY KEY, qiymat TEXT)")
    # Xavfsizlik/kuzatuv uchun qo'shimcha ustunlar va jadvallar
    db("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE")
    db("ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS faollik TIMESTAMP DEFAULT NOW()")
    db("CREATE TABLE IF NOT EXISTS yuklamalar_log (id SERIAL PRIMARY KEY, turi TEXT, uid BIGINT, sana TIMESTAMP DEFAULT NOW())")
    db("CREATE TABLE IF NOT EXISTS admin_loglar (id SERIAL PRIMARY KEY, admin_id BIGINT, amal TEXT, nishon BIGINT, tafsilot TEXT, sana TIMESTAMP DEFAULT NOW())")
    db("INSERT INTO adminlar (id) VALUES (%s) ON CONFLICT DO NOTHING", (MAIN_ADMIN,))
    if CHANNEL:
        db("INSERT INTO sozlamalar (kalit,qiymat) VALUES ('kanallar',%s) ON CONFLICT DO NOTHING", (CHANNEL,))
    log.info("Database tayyor ✅")

def admin_log(admin_id, amal, nishon=None, tafsilot=""):
    db("INSERT INTO admin_loglar (admin_id,amal,nishon,tafsilot) VALUES (%s,%s,%s,%s)",
       (admin_id, amal, nishon, tafsilot[:200]))

def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def kanallar_ol():
    v, ok = kesh_ol("kanallar")
    if ok: return v
    row = db("SELECT qiymat FROM sozlamalar WHERE kalit='kanallar'", one=True)
    r = [k.strip() for k in row[0].split(",") if k.strip()] if row and row[0] else ([CHANNEL] if CHANNEL else [])
    kesh_set("kanallar", r, ttl=300); return r

def kanallar_saqlash(lst):
    v = ",".join(lst)
    db("INSERT INTO sozlamalar (kalit,qiymat) VALUES ('kanallar',%s) ON CONFLICT (kalit) DO UPDATE SET qiymat=%s", (v, v))
    kesh_del("kanallar")

def admin_mi(uid):
    if uid == MAIN_ADMIN: return True
    v, ok = kesh_ol(f"adm_{uid}")
    if ok: return v
    r = db("SELECT 1 FROM adminlar WHERE id=%s", (uid,), one=True)
    kesh_set(f"adm_{uid}", r is not None, ttl=120)
    return r is not None

def ban_mi(uid):
    v, ok = kesh_ol(f"ban_{uid}")
    if ok: return v
    r = db("SELECT banned FROM bot_users WHERE id=%s", (uid,), one=True)
    banned = bool(r[0]) if r else False
    kesh_set(f"ban_{uid}", banned, ttl=60)
    return banned

def obuna_tekshir(uid):
    kanallar = kanallar_ol()
    if not kanallar: return True, []
    v, ok = kesh_ol(f"sub_{uid}")
    if ok: return v
    ulanmagan = []
    for k in kanallar:
        try:
            st = bot.get_chat_member(k, uid).status
            if st not in ("member","administrator","creator"):
                ulanmagan.append(k)
        except Exception as e:
            log.error(f"Obuna tekshirish xato ({k}): {e}")
    r = (len(ulanmagan)==0, ulanmagan)
    kesh_set(f"sub_{uid}", r, ttl=60)
    return r

def faollik_yangila(uid):
    db("UPDATE bot_users SET faollik=NOW() WHERE id=%s", (uid,))

def yuklama_log(turi, uid):
    db("INSERT INTO yuklamalar_log (turi,uid) VALUES (%s,%s)", (turi, uid))

def yuborish(chat_id, matn, **kw):
    try: return bot.send_message(chat_id, matn, **kw)
    except Exception as e: log.error(f"Send xato: {e}")

def html_esc(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def obuna_kb(lst):
    kb = types.InlineKeyboardMarkup()
    for k in lst:
        kb.add(types.InlineKeyboardButton(f"📢 {k}", url=f"https://t.me/{k.lstrip('@')}"))
    kb.add(types.InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="obuna_tekshir"))
    return kb

def user_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📥 Video yuklab olish",      "🎵 Musiqa yuklab olish")
    kb.add("🔍 Musiqa qidirish",          "🔵 Dumaloq video")
    kb.add("🖼 Rasm orqali kino topish",  "🎞 Video orqali kino topish")
    return kb

# ──────────────────────────────────────────────
#  DOWNLOAD ENGINE — kuchaytirilgan (IG / TikTok / Pinterest tuzatildi)
# ──────────────────────────────────────────────

UA_MOBILE   = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
UA_DESKTOP  = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

def _base_opts(tmpl):
    o = {
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "geo_bypass": True,
        "overwrites": True,
        "http_headers": {"User-Agent": UA_MOBILE, "Accept-Language": "en-US,en;q=0.9"},
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    return o

def _ydl_opts_video(tmpl, max_mb=49):
    o = _base_opts(tmpl)
    o.update({
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "max_filesize": max_mb * 1024 * 1024,
    })
    return o

def _ydl_opts_audio(tmpl):
    o = _base_opts(tmpl)
    o.update({
        "format": "bestaudio/best",
        "merge_output_format": "mp3",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    return o

# TikTok tez-tez ichki API formatini o'zgartiradi — bir nechta variant bilan urinamiz
_TIKTOK_VERSIYALAR = [
    {"app_name": ["musical_ly"], "app_version": ["34.1.2"], "manifest_app_version": ["2023405020"]},
    {"app_name": ["musical_ly"], "app_version": ["35.1.3"], "manifest_app_version": ["2023506030"]},
    {"app_name": ["trill"],      "app_version": ["34.1.2"], "manifest_app_version": ["2023405020"]},
]

def _tiktok_opts(base, versiya):
    o = dict(base)
    o["http_headers"] = {**o.get("http_headers", {}), "Referer": "https://www.tiktok.com/"}
    o["format"] = "download_addr-2/download_addr/play_addr-2/play_addr/bestvideo+bestaudio/best"
    o["extractor_args"] = {"tiktok": versiya}
    return o

def _instagram_opts(base):
    o = dict(base)
    o["http_headers"] = {**o.get("http_headers", {}), "Referer": "https://www.instagram.com/"}
    return o

def _pinterest_opts(base):
    o = dict(base)
    o["http_headers"] = {**o.get("http_headers", {}), "Referer": "https://www.pinterest.com/"}
    return o

def _fayl_topish(ts):
    fls = sorted(glob.glob(f"/tmp/dl_{ts}*"), key=os.path.getsize, reverse=True)
    return [f for f in fls if os.path.getsize(f) > 0]

def _tozala(ts):
    for x in glob.glob(f"/tmp/dl_{ts}*"):
        try: os.remove(x)
        except Exception: pass

def _xato_kodi(e):
    s = str(e).lower()
    if "filesize" in s:                                   return "hajm"
    if "private" in s:                                     return "yopiq"
    if "age" in s:                                          return "yosh"
    if "unavailab" in s or "not found" in s or "404" in s:  return "mavjud_emas"
    if "login" in s or "cookie" in s or "rate-limit" in s or "429" in s: return "login"
    return str(e)[:120]

def _ytdlp_urin(url, opts):
    """Bitta yt-dlp urinishi. Muvaffaqiyatli bo'lsa (nom,dur,muallif) qaytaradi, aks holda exception ko'taradi."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        nom     = (info.get("title") or "Video")[:120]
        dur     = info.get("duration") or 0
        muallif = info.get("uploader") or ""
        return nom, dur, muallif

def insta_yukla(url, max_mb=49):
    """1) yt-dlp (cookie bo'lsa eng ishonchli)  2) instaloader — zaxira"""
    ts   = int(time.time() * 1000)
    tmpl = f"/tmp/dl_{ts}.%(ext)s"
    try:
        opts = _instagram_opts(_ydl_opts_video(tmpl, max_mb))
        nom, dur, muallif = _ytdlp_urin(url, opts)
        fls = _fayl_topish(ts)
        if fls and os.path.getsize(fls[0]) > 100:
            fayl = fls[0]
            for x in fls[1:]:
                try: os.remove(x)
                except Exception: pass
            return fayl, nom, dur, muallif, None
        _tozala(ts)
    except Exception as e:
        _tozala(ts)
        log.info(f"IG yt-dlp muvaffaqiyatsiz, instaloader urinilmoqda: {e}")

    if not INSTA_OK:
        return None, None, 0, None, "login"
    m = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    if not m: return None, None, 0, None, "url_noto'g'ri"
    sc = m.group(1)
    try:
        L = instaloader.Instaloader(
            download_videos=True, download_video_thumbnails=False,
            download_geotags=False, download_comments=False,
            save_metadata=False, compress_json=False, quiet=True,
        )
        post    = instaloader.Post.from_shortcode(L.context, sc)
        nom     = (post.caption or "Instagram video")[:80]
        dur     = post.video_duration or 0
        muallif = post.owner_username or ""
        if not post.is_video: return None, nom, 0, muallif, "video_emas"
        chiq = f"/tmp/insta_{int(time.time()*1000)}.mp4"
        h = {"User-Agent": UA_MOBILE}
        r = requests.get(post.video_url, headers=h, stream=True, timeout=60)
        r.raise_for_status()
        hajm = 0
        with open(chiq, "wb") as f:
            for chunk in r.iter_content(65536):
                hajm += len(chunk)
                if hajm/1024/1024 > max_mb:
                    f.close(); os.remove(chiq)
                    return None, nom, dur, muallif, "hajm"
                f.write(chunk)
        if os.path.exists(chiq) and os.path.getsize(chiq) > 0:
            return chiq, nom, dur, muallif, None
    except Exception as e:
        log.error(f"Instaloader: {e}")
        s = str(e).lower()
        xato = ("yopiq" if "private" in s else
                "mavjud_emas" if "not found" in s else
                "login" if "login" in s or "401" in s else
                str(e)[:80])
        return None, None, 0, None, xato
    return None, None, 0, None, "login"

def tiktok_yukla(url, max_mb=49):
    ts   = int(time.time() * 1000)
    tmpl = f"/tmp/dl_{ts}.%(ext)s"
    base = _ydl_opts_video(tmpl, max_mb)
    oxirgi_xato = None
    for versiya in _TIKTOK_VERSIYALAR:
        try:
            opts = _tiktok_opts(base, versiya)
            nom, dur, muallif = _ytdlp_urin(url, opts)
            fls = _fayl_topish(ts)
            if fls and os.path.getsize(fls[0]) > 100:
                fayl = fls[0]
                for x in fls[1:]:
                    try: os.remove(x)
                    except Exception: pass
                return fayl, nom, dur, muallif, None
            _tozala(ts)
        except Exception as e:
            oxirgi_xato = e
            _tozala(ts)
            continue
    return None, None, 0, None, (_xato_kodi(oxirgi_xato) if oxirgi_xato else "topilmadi")

def pinterest_yukla(url, max_mb=49):
    """Pinterest pinlari ko'pincha video emas, rasm bo'ladi — ikkalasini ham qo'llab-quvvatlaymiz."""
    ts   = int(time.time() * 1000)
    tmpl = f"/tmp/dl_{ts}.%(ext)s"
    try:
        opts = _pinterest_opts(_ydl_opts_video(tmpl, max_mb))
        nom, dur, muallif = _ytdlp_urin(url, opts)
        fls = _fayl_topish(ts)
        if fls and os.path.getsize(fls[0]) > 100:
            fayl = fls[0]
            for x in fls[1:]:
                try: os.remove(x)
                except Exception: pass
            return fayl, nom, dur, muallif, None, "video"
        _tozala(ts)
    except Exception as e:
        _tozala(ts)
        log.info(f"Pinterest video emas, rasm sifatida urinilmoqda: {e}")

    # Rasm-pin fallback: sahifadan og:image ni olib yuklaymiz
    try:
        h = {"User-Agent": UA_DESKTOP}
        r = requests.get(url, headers=h, timeout=20)
        r.raise_for_status()
        m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text) or \
            re.search(r'<meta name="og:image" content="([^"]+)"', r.text)
        if not m:
            return None, None, 0, None, "mavjud_emas", None
        img_url = m.group(1)
        tm = re.search(r"<title>([^<]{3,120})</title>", r.text)
        nom = html_esc(tm.group(1)) if tm else "Pinterest rasm"
        rr = requests.get(img_url, headers=h, stream=True, timeout=30)
        rr.raise_for_status()
        chiq = f"/tmp/pin_{ts}.jpg"
        hajm = 0
        with open(chiq, "wb") as f:
            for chunk in rr.iter_content(65536):
                hajm += len(chunk)
                if hajm/1024/1024 > max_mb:
                    f.close(); os.remove(chiq)
                    return None, nom, 0, None, "hajm", None
                f.write(chunk)
        if os.path.exists(chiq) and os.path.getsize(chiq) > 0:
            return chiq, nom, 0, None, None, "rasm"
    except Exception as e:
        log.error(f"Pinterest rasm xato: {e}")
    return None, None, 0, None, "mavjud_emas", None

def media_yukla(url, audio=False, max_mb=49):
    """Qaytaradi: (fayl, nom, davomiylik, muallif, xato, turi)
       turi: 'video' | 'audio' | 'rasm' """
    url   = url.strip()
    ts    = int(time.time() * 1000)
    tmpl  = f"/tmp/dl_{ts}.%(ext)s"
    is_tt = any(x in url.lower() for x in ["tiktok.com", "vm.tiktok", "vt.tiktok"])
    is_ig = "instagram.com" in url.lower()
    is_pin = any(x in url.lower() for x in ["pinterest.com", "pin.it"])

    if audio:
        opts = _ydl_opts_audio(tmpl)
        try:
            nom, dur, muallif = _ytdlp_urin(url, opts)
            fls = _fayl_topish(ts)
            if not fls:
                return None, nom, dur, muallif, "topilmadi", "audio"
            fayl = fls[0]
            if os.path.getsize(fayl) < 100:
                _tozala(ts); return None, nom, dur, muallif, "kichik", "audio"
            for x in fls[1:]:
                try: os.remove(x)
                except Exception: pass
            return fayl, nom, dur, muallif, None, "audio"
        except Exception as e:
            _tozala(ts)
            xato = _xato_kodi(e)
            log.error(f"Audio yuklab olish xato ({url[:60]}): {xato}")
            return None, None, 0, None, xato, "audio"

    if is_ig:
        fayl, nom, dur, muallif, xato = insta_yukla(url, max_mb)
        return fayl, nom, dur, muallif, xato, "video"

    if is_tt:
        fayl, nom, dur, muallif, xato = tiktok_yukla(url, max_mb)
        return fayl, nom, dur, muallif, xato, "video"

    if is_pin:
        fayl, nom, dur, muallif, xato, turi = pinterest_yukla(url, max_mb)
        return fayl, nom, dur, muallif, xato, (turi or "video")

    opts = _ydl_opts_video(tmpl, max_mb)
    try:
        nom, dur, muallif = _ytdlp_urin(url, opts)
        fls = _fayl_topish(ts)
        if not fls:
            return None, nom, dur, muallif, "topilmadi", "video"
        fayl = fls[0]
        if os.path.getsize(fayl) < 100:
            _tozala(ts); return None, nom, dur, muallif, "kichik", "video"
        for x in fls[1:]:
            try: os.remove(x)
            except Exception: pass
        return fayl, nom, dur, muallif, None, "video"
    except Exception as e:
        _tozala(ts)
        xato = _xato_kodi(e)
        log.error(f"yt-dlp xato ({url[:60]}): {xato}")
        return None, None, 0, None, xato, "video"

def musiqa_qidirish(sorov, n=5):
    opts = {"quiet":True,"no_warnings":True,"extract_flat":True,"noplaylist":True}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"ytsearch{n}:{sorov}", download=False)
            lst = []
            for e in (res.get("entries") or []):
                if not e: continue
                dur = e.get("duration") or 0
                m,s = divmod(int(dur), 60)
                lst.append({"nom":e.get("title","Nomsiz"),
                            "url":f"https://youtu.be/{e.get('id','')}",
                            "davomiy":f"{m}:{s:02d}",
                            "thumb":e.get("thumbnail",""),
                            "id":e.get("id",""),
                            "muallif":e.get("uploader","")})
            return lst
    except Exception as e:
        log.error(f"Qidirish: {e}"); return []

# ──────────────────────────────────────────────
#  AUDIO EFFEKTLAR
# ──────────────────────────────────────────────

EFFEKTLAR = {
    "eff_bass":      ("🔊 Bass Boost",      "bass=g=15"),
    "eff_treble":    ("🎵 Treble Boost",    "treble=g=10"),
    "eff_echo":      ("🌊 Echo",            "aecho=0.8:0.88:60:0.4"),
    "eff_slrev":     ("🌙 Slowed+Reverb",   "atempo=0.85,aecho=0.8:0.9:1000:0.3"),
    "eff_speed":     ("⚡ Tezlashtirish",   "atempo=1.5"),
    "eff_slow":      ("🐌 Sekinlashtirish", "atempo=0.75"),
    "eff_nightcore": ("🎤 Nightcore",       "asetrate=44100*1.25,atempo=0.8,aresample=44100"),
    "eff_volume":    ("📢 Volume +",        "volume=2.0"),
}

def effektlar_kb():
    kb   = types.InlineKeyboardMarkup(row_width=2)
    btns = [types.InlineKeyboardButton(n, callback_data=k) for k,(n,_) in EFFEKTLAR.items()]
    for i in range(0, len(btns), 2): kb.row(*btns[i:i+2])
    return kb

def effekt_qollan(url, src_fayl, filtr):
    ts   = int(time.time()*1000)
    chiq = f"/tmp/eff_{ts}.mp3"
    tmp  = None
    try:
        if src_fayl and os.path.exists(src_fayl):
            src = src_fayl
        elif url:
            tmp = f"/tmp/effsrc_{ts}"
            opts = _ydl_opts_audio(f"{tmp}.%(ext)s")
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            fls = glob.glob(f"{tmp}*")
            if not fls: return None
            src = fls[0]
        else:
            return None
        r = subprocess.run(
            ["ffmpeg","-y","-i",src,"-af",filtr,"-codec:a","libmp3lame","-q:a","3",chiq],
            capture_output=True, timeout=120
        )
        if r.returncode == 0 and os.path.exists(chiq) and os.path.getsize(chiq) > 500:
            return chiq
        log.error(f"ffmpeg stderr: {r.stderr.decode()[:400]}")
    except Exception as e:
        log.error(f"Effekt xato: {e}")
    finally:
        if tmp:
            for f in glob.glob(f"{tmp}*"):
                try: os.remove(f)
                except Exception: pass
    return None

# ──────────────────────────────────────────────
#  VIDEO YORDAMCHI FUNKSIYALAR
# ──────────────────────────────────────────────

def video_kadr_ol(video_fayl):
    chiq = f"/tmp/kadr_{int(time.time()*1000)}.jpg"
    try:
        for ss in ["00:00:03","00:00:01","00:00:00"]:
            r = subprocess.run(
                ["ffmpeg","-y","-i",video_fayl,"-ss",ss,"-frames:v","1","-q:v","2",chiq],
                capture_output=True, timeout=30
            )
            if r.returncode==0 and os.path.exists(chiq) and os.path.getsize(chiq)>0:
                return chiq
    except Exception as e:
        log.error(f"Kadr: {e}")
    return None

def dumaloq_video(kirish):
    chiq = f"/tmp/doira_{int(time.time()*1000)}.mp4"
    vf   = "crop=min(iw\\,ih):min(iw\\,ih),scale=384:384"
    try:
        r = subprocess.run(
            ["ffmpeg","-y","-i",kirish,"-vf",vf,"-c:v","libx264","-preset","veryfast",
             "-crf","28","-c:a","aac","-b:a","96k","-t","60","-movflags","+faststart","-pix_fmt","yuv420p",chiq],
            capture_output=True, timeout=180
        )
        if r.returncode==0 and os.path.exists(chiq) and os.path.getsize(chiq)>0:
            return chiq
    except Exception as e:
        log.error(f"Doira: {e}")
    return None

def _tg_yukla(file_id, ext="mp4"):
    fi  = bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{TOKEN}/{fi.file_path}"
    dst = f"/tmp/tg_{int(time.time()*1000)}.{ext}"
    urllib.request.urlretrieve(url, dst)
    return dst

def google_lens_qidirish(img_fayl):
    try:
        h = {
            "User-Agent": UA_DESKTOP,
            "Accept-Language": "ru,en;q=0.9",
        }
        with open(img_fayl, "rb") as f:
            r = requests.post(
                "https://lens.google.com/upload?ep=gsbubb&hl=ru&re=df",
                files={"encoded_image": ("img.jpg", f, "image/jpeg")},
                headers=h, allow_redirects=True, timeout=25
            )
        topilgan = []
        for pat in [r'"title"\s*:\s*"([^"]{3,80})"', r'"text"\s*:\s*"([A-Za-z0-9\u0400-\u04FF\s\-\:\'\"\!]{4,60})"']:
            for mm in re.findall(pat, r.text):
                mm = mm.strip()
                skip = ["Google","Search","Images","Lens","http","www",".com","null","true","false"]
                if mm and len(mm)>3 and mm not in topilgan and not any(x.lower() in mm.lower() for x in skip):
                    topilgan.append(mm)
        return topilgan[:10]
    except Exception as e:
        log.error(f"Lens: {e}"); return []

def bazadan_qidirish(sorov):
    return db("SELECT kod,nom,poster_id,korishlar FROM kinolar WHERE LOWER(nom) LIKE %s ESCAPE '\\' ORDER BY korishlar DESC LIMIT 5",
              (f"%{like_escape(sorov.lower())}%",), fetch=True) or []

def kino_qidirish_natija(chat_id, img_fayl, kut_id=None):
    topilgan = google_lens_qidirish(img_fayl)
    try: os.remove(img_fayl)
    except Exception: pass
    if kut_id:
        try: bot.delete_message(chat_id, kut_id)
        except Exception: pass
    if not topilgan:
        yuborish(chat_id,"❌ Kino aniqlanmadi\n💡 Aniqroq poster yoki kadr yuboring"); return
    baza = []
    for t in topilgan:
        for r in bazadan_qidirish(t):
            if r not in baza: baza.append(r)
    matn = f"🔍 <b>Aniqlangan:</b> <i>{html_esc(topilgan[0])}</i>\n\n"
    if baza:
        matn += "🎬 <b>Botdagi kinolar:</b>\n\n"
        for kod,nom,pid,ko in baza[:5]:
            matn += f"🔢 <code>{html_esc(kod)}</code> — <b>{html_esc(nom)}</b> 👁{ko}\n"
        matn += "\n📩 <b>Kodni yuboring → kino keladi!</b>"
        pid = baza[0][2]
        if pid:
            try: bot.send_photo(chat_id, pid, caption=matn, parse_mode="HTML"); return
            except Exception: pass
    else:
        matn += f"😔 Botda bu kino hali yo'q\n🏷 Nomi: <b>{html_esc(topilgan[0])}</b>"
    yuborish(chat_id, matn, parse_mode="HTML")

def davomiylik(s):
    if not s: return ""
    m,s = divmod(int(s),60); h,m = divmod(m,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

XATO_MATN = {
    "hajm":        f"❌ Fayl {MAX_MB} MB dan katta — bot yuklab bera olmaydi",
    "yopiq":       "❌ Bu profil/video yopiq (private)",
    "yosh":        "❌ Yosh chekloviga ega video",
    "mavjud_emas": "❌ Video/rasm mavjud emas yoki o'chirilgan",
    "video_emas":  "❌ Bu rasm post — video postlarni yuboring",
    "login":       "❌ Bu havola login/cookie talab qiladi — hozircha ochiq (public) postlarni yuborishingiz kerak",
    "topilmadi":   "❌ Fayl yuklab olinmadi",
    "kichik":      "❌ Fayl bo'sh keldi — boshqa havola sinab ko'ring",
}
def yuklab_xato(chat_id, xato):
    yuborish(chat_id, XATO_MATN.get(xato, f"❌ Yuklab bo'lmadi:\n<code>{html_esc((xato or '')[:120])}</code>"), parse_mode="HTML")

def url_mi(t):
    t = t.strip().lower()
    return t.startswith("http") and "." in t

def video_yuborish(chat_id, fayl, nom, dur, muallif, uid=None, turi="video"):
    d   = davomiylik(dur)
    cap = f"🎬 <b>{html_esc(nom)}</b>"
    if muallif: cap += f"\n👤 {html_esc(muallif)}"
    if d:       cap += f"\n⏱ {d}"
    cap += f"\n\n📥 {BOT_TAG}"
    try:
        if turi == "rasm":
            with open(fayl,"rb") as f:
                bot.send_photo(chat_id, f, caption=cap, parse_mode="HTML")
        else:
            with open(fayl,"rb") as f:
                bot.send_video(chat_id, f, caption=cap, parse_mode="HTML", supports_streaming=True)
        if uid: yuklama_log(turi, uid)
    except Exception as e:
        log.error(f"Video yuborish xato: {e}")
        try:
            with open(fayl,"rb") as f:
                bot.send_document(chat_id, f, caption=cap, parse_mode="HTML")
            if uid: yuklama_log(turi, uid)
        except Exception as e2:
            log.error(f"Document yuborish xato: {e2}")
            yuborish(chat_id,"❌ Yuborishda xatolik")
    finally:
        try: os.remove(fayl)
        except Exception: pass

def audio_yuborish(chat_id, fayl, nom, dur, muallif, uid=None):
    d   = davomiylik(dur)
    cap = f"🎵 <b>{html_esc(nom)}</b>"
    if muallif: cap += f"\n👤 {html_esc(muallif)}"
    if d:       cap += f"\n⏱ {d}"
    cap += f"\n\n🎛 Effekt tanlash uchun quyidagi tugmalardan bosing 👇\n📥 {BOT_TAG}"
    try:
        with open(fayl,"rb") as f:
            msg = bot.send_audio(chat_id, f,
                                 title=nom[:64],
                                 performer=(muallif or "")[:32],
                                 caption=cap,
                                 parse_mode="HTML",
                                 reply_markup=effektlar_kb())
        if uid: yuklama_log("audio", uid)
        return msg
    except Exception as e:
        log.error(f"Audio yuborish: {e}")
    return None

# ──────────────────────────────────────────────
#  SUPER ULTRA ADMIN PANEL
# ──────────────────────────────────────────────

def bosh_admin_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Kino qo'shish",    callback_data="adm_kino_qosh"),
        types.InlineKeyboardButton("🗑 Kino o'chirish",   callback_data="adm_kino_ochir"),
    )
    kb.add(
        types.InlineKeyboardButton("📊 Statistika",        callback_data="adm_stat"),
        types.InlineKeyboardButton("📋 Kinolar ro'yxati",  callback_data="adm_kinolar_0"),
    )
    kb.add(
        types.InlineKeyboardButton("👥 Foydalanuvchilar",  callback_data="adm_users_0"),
        types.InlineKeyboardButton("🔎 User qidirish",     callback_data="adm_user_qidir"),
    )
    kb.add(
        types.InlineKeyboardButton("📨 Reklama",           callback_data="adm_reklama"),
        types.InlineKeyboardButton("📥 Yuklamalar",        callback_data="adm_yuklamalar"),
    )
    kb.add(
        types.InlineKeyboardButton("👑 Adminlar",          callback_data="adm_adminlar"),
        types.InlineKeyboardButton("⚙️ Sozlamalar",        callback_data="adm_sozlamalar"),
    )
    kb.add(types.InlineKeyboardButton("🛡 Xavfsizlik jurnali", callback_data="adm_loglar_0"))
    return kb

def admin_panel_yuborish(chat_id, tahrirlash_id=None):
    u  = db("SELECT COUNT(*) FROM bot_users",one=True)
    k  = db("SELECT COUNT(*) FROM kinolar",one=True)
    v  = db("SELECT COALESCE(SUM(korishlar),0) FROM kinolar",one=True)
    ban= db("SELECT COUNT(*) FROM bot_users WHERE banned=TRUE",one=True)
    b  = db("SELECT COUNT(*) FROM bot_users WHERE qoshildi>=NOW()-INTERVAL '24 hours'",one=True)
    matn = (
        "🚀 <b>SUPER ADMIN PANEL</b> 🚀\n\n"
        f"👥 Foydalanuvchilar: <b>{u[0] if u else 0}</b>  (bugun +{b[0] if b else 0})\n"
        f"🎬 Kinolar: <b>{k[0] if k else 0}</b>\n"
        f"👁 Ko'rishlar: <b>{v[0] if v else 0}</b>\n"
        f"🚫 Bloklangan: <b>{ban[0] if ban else 0}</b>\n\n"
        "👇 Kerakli bo'limni tanlang:"
    )
    if tahrirlash_id:
        try:
            bot.edit_message_text(matn, chat_id, tahrirlash_id, parse_mode="HTML", reply_markup=bosh_admin_kb())
            return
        except Exception: pass
    yuborish(chat_id, matn, parse_mode="HTML", reply_markup=bosh_admin_kb())

def orqaga_kb(data="adm_bosh"):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data=data))
    return kb

@bot.message_handler(commands=["start"])
def start(msg):
    uid = msg.from_user.id
    if ban_mi(uid):
        yuborish(msg.chat.id, "🚫 Siz botdan foydalanishdan bloklangansiz."); return
    db("INSERT INTO bot_users (id,name) VALUES (%s,%s) ON CONFLICT (id) DO UPDATE SET name=%s",
       (uid, msg.from_user.first_name or "", msg.from_user.first_name or ""))
    kesh_del(f"sub_{uid}"); kesh_del(f"ban_{uid}")
    faollik_yangila(uid)
    if admin_mi(uid):
        admin_panel_yuborish(msg.chat.id)
        return
    ok, ul = obuna_tekshir(uid)
    if not ok:
        yuborish(msg.chat.id, "❗ Botdan foydalanish uchun kanallarga obuna bo'ling:", reply_markup=obuna_kb(ul))
        return
    yuborish(msg.chat.id,
        "🎬 <b>Kino botga xush kelibsiz!</b>\n\n"
        "🔢 Kino kodini yuboring → kino keladi\n"
        "🖼 Poster/kadr yuboring → kino topiladi\n\n"
        "📥 Pastdagi tugmalardan foydalaning 👇",
        parse_mode="HTML", reply_markup=user_kb())

@bot.message_handler(commands=["admin"])
def admin_cmd(msg):
    if admin_mi(msg.from_user.id):
        admin_panel_yuborish(msg.chat.id)

@bot.message_handler(commands=["myid"])
def myid(msg):
    yuborish(msg.chat.id, f"🆔 ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")

# ── Statistika (kengaytirilgan) ──

@bot.callback_query_handler(func=lambda c: c.data=="adm_bosh")
def cb_adm_bosh(call):
    if not admin_mi(call.from_user.id): return
    bot.answer_callback_query(call.id)
    admin_panel_yuborish(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data=="adm_stat")
def cb_stat(call):
    if not admin_mi(call.from_user.id): return
    u   = db("SELECT COUNT(*) FROM bot_users",one=True)
    k   = db("SELECT COUNT(*) FROM kinolar",one=True)
    q   = db("SELECT COUNT(*) FROM kino_qismlar",one=True)
    v   = db("SELECT COALESCE(SUM(korishlar),0) FROM kinolar",one=True)
    b   = db("SELECT COUNT(*) FROM bot_users WHERE qoshildi>=NOW()-INTERVAL '24 hours'",one=True)
    h   = db("SELECT COUNT(*) FROM bot_users WHERE qoshildi>=NOW()-INTERVAL '7 days'",one=True)
    ban = db("SELECT COUNT(*) FROM bot_users WHERE banned=TRUE",one=True)
    faol= db("SELECT COUNT(*) FROM bot_users WHERE faollik>=NOW()-INTERVAL '24 hours'",one=True)
    top = db("SELECT nom,korishlar FROM kinolar ORDER BY korishlar DESC LIMIT 3",fetch=True) or []
    matn = (
        "📊 <b>STATISTIKA</b>\n\n"
        f"👥 Jami foydalanuvchi: <b>{u[0] if u else 0}</b>\n"
        f"  📅 Bugun qo'shildi: <b>{b[0] if b else 0}</b>\n"
        f"  🗓 Hafta ichida: <b>{h[0] if h else 0}</b>\n"
        f"  🟢 So'nggi 24soat faol: <b>{faol[0] if faol else 0}</b>\n"
        f"  🚫 Bloklangan: <b>{ban[0] if ban else 0}</b>\n\n"
        f"🎬 Kinolar: <b>{k[0] if k else 0}</b>\n"
        f"🎞 Qismlar: <b>{q[0] if q else 0}</b>\n"
        f"👁 Ko'rishlar: <b>{v[0] if v else 0}</b>\n"
    )
    if top:
        matn += "\n🏆 <b>Top kinolar:</b>\n"
        for nom,ko in top:
            matn += f"  • {html_esc(nom)} — 👁{ko}\n"
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=orqaga_kb())
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=orqaga_kb())

@bot.callback_query_handler(func=lambda c: c.data=="adm_yuklamalar")
def cb_yuklamalar(call):
    if not admin_mi(call.from_user.id): return
    jami  = db("SELECT COUNT(*) FROM yuklamalar_log",one=True)
    bugun = db("SELECT COUNT(*) FROM yuklamalar_log WHERE sana>=NOW()-INTERVAL '24 hours'",one=True)
    turlar= db("SELECT turi,COUNT(*) FROM yuklamalar_log GROUP BY turi ORDER BY COUNT(*) DESC",fetch=True) or []
    matn = (
        "📥 <b>Yuklamalar statistikasi</b>\n\n"
        f"📦 Jami yuklamalar: <b>{jami[0] if jami else 0}</b>\n"
        f"📅 So'nggi 24 soat: <b>{bugun[0] if bugun else 0}</b>\n\n"
    )
    if turlar:
        matn += "📂 <b>Turlar bo'yicha:</b>\n"
        for t,c in turlar:
            matn += f"  • {t}: <b>{c}</b>\n"
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=orqaga_kb())
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=orqaga_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_loglar_"))
def cb_loglar(call):
    if not admin_mi(call.from_user.id): return
    offset = int(call.data.split("_")[-1])
    limit  = 10
    lst = db("SELECT admin_id,amal,nishon,tafsilot,sana FROM admin_loglar ORDER BY sana DESC LIMIT %s OFFSET %s",
             (limit, offset), fetch=True) or []
    jami = (db("SELECT COUNT(*) FROM admin_loglar",one=True) or (0,))[0]
    matn = f"🛡 <b>Xavfsizlik jurnali</b> ({offset+1}–{offset+len(lst)}/{jami})\n\n"
    if not lst:
        matn += "📭 Hozircha yozuv yo'q"
    for aid,amal,nishon,taf,sana in lst:
        sana_s = sana.strftime('%d.%m %H:%M') if sana else '—'
        matn += f"• <code>{aid}</code> — <b>{html_esc(amal)}</b>"
        if nishon: matn += f" → <code>{nishon}</code>"
        matn += f" ({sana_s})\n"
    kb = types.InlineKeyboardMarkup()
    nav = []
    if offset>0: nav.append(types.InlineKeyboardButton("⬅️",callback_data=f"adm_loglar_{max(0,offset-limit)}"))
    if offset+limit<jami: nav.append(types.InlineKeyboardButton("➡️",callback_data=f"adm_loglar_{offset+limit}"))
    if nav: kb.row(*nav)
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_bosh"))
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_kinolar_"))
def cb_kinolar(call):
    if not admin_mi(call.from_user.id): return
    offset = int(call.data.split("_")[-1])
    limit  = 10
    lst    = db("SELECT k.kod,k.nom,k.korishlar,(SELECT COUNT(*) FROM kino_qismlar WHERE kod=k.kod) FROM kinolar k ORDER BY k.qoshildi DESC LIMIT %s OFFSET %s",(limit,offset),fetch=True) or []
    jami   = (db("SELECT COUNT(*) FROM kinolar",one=True) or (0,))[0]
    if not lst:
        bot.answer_callback_query(call.id,"📭 Kino yo'q"); return
    matn = f"📋 <b>Kinolar ro'yxati</b> ({offset+1}–{offset+len(lst)}/{jami})\n\n"
    for kod,nom,ko,q in lst:
        matn += f"🔢 <code>{html_esc(kod)}</code> — {html_esc(nom)} 👁{ko}" + (f" 🎞{q}ta" if q>1 else "")+"\n"
    kb = types.InlineKeyboardMarkup()
    nav = []
    if offset>0: nav.append(types.InlineKeyboardButton("⬅️",callback_data=f"adm_kinolar_{max(0,offset-limit)}"))
    if offset+limit<jami: nav.append(types.InlineKeyboardButton("➡️",callback_data=f"adm_kinolar_{offset+limit}"))
    if nav: kb.row(*nav)
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_bosh"))
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_users_"))
def cb_users(call):
    if not admin_mi(call.from_user.id): return
    offset = int(call.data.split("_")[-1])
    limit  = 10
    lst    = db("SELECT id,name,qoshildi,banned FROM bot_users ORDER BY qoshildi DESC LIMIT %s OFFSET %s",(limit,offset),fetch=True) or []
    jami   = (db("SELECT COUNT(*) FROM bot_users",one=True) or (0,))[0]
    if not lst:
        bot.answer_callback_query(call.id,"📭 Foydalanuvchi yo'q"); return
    matn = f"👥 <b>Foydalanuvchilar</b> ({offset+1}–{offset+len(lst)}/{jami})\n\n"
    for uid,ism,v,banned in lst:
        sana = v.strftime('%d.%m.%y') if v else '—'
        belg = "🚫" if banned else "🟢"
        matn += f"{belg} <code>{uid}</code> — <b>{html_esc((ism or '—'))[:20]}</b> | {sana}\n"
    kb = types.InlineKeyboardMarkup(row_width=2)
    for uid,ism,v,banned in lst:
        lbl = f"{'🚫' if banned else '👤'} {(ism or str(uid))[:20]}"
        kb.add(types.InlineKeyboardButton(lbl, callback_data=f"user_prof_{uid}"))
    nav = []
    if offset>0: nav.append(types.InlineKeyboardButton("⬅️",callback_data=f"adm_users_{max(0,offset-limit)}"))
    if offset+limit<jami: nav.append(types.InlineKeyboardButton("➡️",callback_data=f"adm_users_{offset+limit}"))
    if nav: kb.row(*nav)
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_bosh"))
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data=="adm_user_qidir")
def cb_user_qidir(call):
    if not admin_mi(call.from_user.id): return
    holat_set(admin_holat, call.from_user.id, {"qadam":"user_qidir"})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    yuborish(call.message.chat.id,
        "🔎 <b>Foydalanuvchi qidirish</b>\n\n"
        "ID yoki ismning bir qismini yuboring:\n"
        "Misol: <code>123456789</code> yoki <code>Ali</code>\n\n"
        "❌ Bekor: /admin", parse_mode="HTML")

def _user_profil_matn_kb(uid_v, ism, sana, banned):
    matn = (
        f"👤 <b>Foydalanuvchi profili</b>\n\n"
        f"🆔 ID: <code>{uid_v}</code>\n"
        f"📛 Ism: <b>{html_esc(ism or '—')}</b>\n"
        f"📅 Qo'shilgan: <b>{sana.strftime('%d.%m.%Y %H:%M') if sana else '—'}</b>\n"
        f"📊 Holat: {'🚫 <b>Bloklangan</b>' if banned else '🟢 <b>Faol</b>'}\n"
        f"🔗 Telegram: <a href='tg://user?id={uid_v}'>Profil ochish</a>"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✉️ Xabar yuborish", callback_data=f"user_msg_{uid_v}"))
    if banned:
        kb.add(types.InlineKeyboardButton("✅ Blokdan chiqarish", callback_data=f"user_unban_{uid_v}"))
    else:
        kb.add(types.InlineKeyboardButton("🚫 Bloklash", callback_data=f"user_ban_{uid_v}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_users_0"))
    return matn, kb

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_prof_"))
def cb_user_prof(call):
    if not admin_mi(call.from_user.id): return
    try: t_uid = int(call.data[len("user_prof_"):])
    except Exception: bot.answer_callback_query(call.id,"❌ Xato ID"); return
    row = db("SELECT id,name,qoshildi,banned FROM bot_users WHERE id=%s",(t_uid,),one=True)
    if not row:
        bot.answer_callback_query(call.id,"❌ Foydalanuvchi topilmadi",show_alert=True); return
    uid_v, ism, sana, banned = row
    matn, kb = _user_profil_matn_kb(uid_v, ism, sana, banned)
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id,
                               parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb,
                     disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_msg_"))
def cb_user_msg(call):
    if not admin_mi(call.from_user.id): return
    try: t_uid = int(call.data[len("user_msg_"):])
    except Exception: bot.answer_callback_query(call.id,"❌ Xato"); return
    row = db("SELECT name FROM bot_users WHERE id=%s",(t_uid,),one=True)
    ism = (row[0] if row else "") or str(t_uid)
    holat_set(admin_holat, call.from_user.id, {"qadam":"xabar_yuborish","malumot":{"uid":t_uid,"ism":ism}})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    yuborish(call.message.chat.id,
        f"✉️ <b>{html_esc(ism)}</b> ga xabar yozmoqdasiz\n"
        f"🆔 ID: <code>{t_uid}</code>\n\n"
        "Xabar yuboring (matn, rasm, video — istalgan):\n\n"
        "❌ Bekor: /admin",
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_ban_"))
def cb_user_ban(call):
    if not admin_mi(call.from_user.id): return
    try: t_uid = int(call.data[len("user_ban_"):])
    except Exception: bot.answer_callback_query(call.id,"❌ Xato"); return
    if t_uid == MAIN_ADMIN:
        bot.answer_callback_query(call.id,"❌ Asosiy adminni bloklab bo'lmaydi!",show_alert=True); return
    db("UPDATE bot_users SET banned=TRUE WHERE id=%s",(t_uid,))
    kesh_del(f"ban_{t_uid}")
    admin_log(call.from_user.id, "ban", t_uid)
    bot.answer_callback_query(call.id,f"🚫 {t_uid} bloklandi",show_alert=True)
    row = db("SELECT id,name,qoshildi,banned FROM bot_users WHERE id=%s",(t_uid,),one=True)
    if row:
        matn, kb = _user_profil_matn_kb(*row)
        try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id,
                                   parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("user_unban_"))
def cb_user_unban(call):
    if not admin_mi(call.from_user.id): return
    try: t_uid = int(call.data[len("user_unban_"):])
    except Exception: bot.answer_callback_query(call.id,"❌ Xato"); return
    db("UPDATE bot_users SET banned=FALSE WHERE id=%s",(t_uid,))
    kesh_del(f"ban_{t_uid}")
    admin_log(call.from_user.id, "unban", t_uid)
    bot.answer_callback_query(call.id,f"✅ {t_uid} blokdan chiqarildi",show_alert=True)
    row = db("SELECT id,name,qoshildi,banned FROM bot_users WHERE id=%s",(t_uid,),one=True)
    if row:
        matn, kb = _user_profil_matn_kb(*row)
        try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id,
                                   parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data=="adm_adminlar")
def cb_adminlar(call):
    if not admin_mi(call.from_user.id): return
    lst  = db("SELECT id FROM adminlar ORDER BY qoshildi",fetch=True) or []
    matn = "👑 <b>Adminlar ro'yxati:</b>\n\n"
    kb   = types.InlineKeyboardMarkup()
    for (aid,) in lst:
        matn += f"• <code>{aid}</code>{' 👑' if aid==MAIN_ADMIN else ''}\n"
        if call.from_user.id==MAIN_ADMIN and aid!=MAIN_ADMIN:
            kb.add(types.InlineKeyboardButton(f"❌ {aid} o'chirish", callback_data=f"admin_del_{aid}"))
    if call.from_user.id==MAIN_ADMIN:
        kb.add(types.InlineKeyboardButton("➕ Admin qo'shish", callback_data="admin_qosh"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_bosh"))
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data=="adm_sozlamalar")
def cb_sozlamalar(call):
    if not admin_mi(call.from_user.id): return
    kanallar = kanallar_ol()
    matn = "⚙️ <b>Sozlamalar — Kanallar</b>\n\n"
    if kanallar:
        matn += f"📢 Ulangan kanallar ({len(kanallar)}/5):\n"
        for i,k in enumerate(kanallar,1): matn += f"  {i}. <b>{html_esc(k)}</b>\n"
    else:
        matn += "📢 Kanal ulanmagan\n"
    matn += "\n💡 Kanal qo'shsangiz foydalanuvchilar majburiy obuna bo'ladi"
    kb = types.InlineKeyboardMarkup()
    if len(kanallar)<5:
        kb.add(types.InlineKeyboardButton("➕ Kanal qo'shish", callback_data="kanal_qosh"))
    for i,k in enumerate(kanallar):
        kb.add(types.InlineKeyboardButton(f"❌ {k} o'chirish", callback_data=f"kanal_del_{i}"))
    kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_bosh"))
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(matn, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception: yuborish(call.message.chat.id, matn, parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data=="adm_kino_qosh")
def cb_kino_qosh(call):
    if not admin_mi(call.from_user.id): return
    holat_set(admin_holat, call.from_user.id, {"qadam":"kod","malumot":{}})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    yuborish(call.message.chat.id,
        "➕ <b>Kino qo'shish</b>\n\n"
        "🔢 Kino kodini yuboring:\n"
        "Misol: <code>101</code> yoki <code>serial5</code>\n\n"
        "❌ Bekor: /admin",
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data=="adm_kino_ochir")
def cb_kino_ochir(call):
    if not admin_mi(call.from_user.id): return
    holat_set(admin_holat, call.from_user.id, {"qadam":"kino_ochir"})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    yuborish(call.message.chat.id,
        "🗑 <b>Kino o'chirish</b>\n\n"
        "O'chiriladigan kino kodini yuboring:\n\n"
        "❌ Bekor: /admin",
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data=="adm_reklama")
def cb_reklama(call):
    if not admin_mi(call.from_user.id): return
    holat_set(admin_holat, call.from_user.id, {"qadam":"reklama"})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    u = db("SELECT COUNT(*) FROM bot_users WHERE banned=FALSE",one=True)
    yuborish(call.message.chat.id,
        f"📨 <b>Reklama yuborish</b>\n\n"
        f"👥 Jami: <b>{u[0] if u else 0}</b> foydalanuvchiga yuboriladi\n\n"
        "Reklama xabarini yuboring (matn, rasm, video):\n\n"
        "❌ Bekor: /admin",
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data=="kanal_qosh")
def cb_kanal_qosh(call):
    if not admin_mi(call.from_user.id): return
    if len(kanallar_ol())>=5: bot.answer_callback_query(call.id,"❌ Maksimum 5 ta!",show_alert=True); return
    holat_set(admin_holat, call.from_user.id, {"qadam":"kanal_qosh"})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    yuborish(call.message.chat.id,
        "📢 <b>Kanal qo'shish</b>\n\n"
        "Kanal username ini yuboring:\n"
        "Misol: <code>@mening_kanalim</code>\n\n"
        "⚠️ Bot kanalga admin bo'lishi shart!\n❌ Bekor: /admin",
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("kanal_del_"))
def cb_kanal_del(call):
    if not admin_mi(call.from_user.id): return
    idx = int(call.data.split("_")[-1])
    lst = kanallar_ol()
    if idx < len(lst):
        o = lst.pop(idx); kanallar_saqlash(lst)
        admin_log(call.from_user.id, "kanal_ochirish", tafsilot=o)
        bot.answer_callback_query(call.id,f"✅ {o} o'chirildi",show_alert=True)
        cb_sozlamalar(call)
    else:
        bot.answer_callback_query(call.id,"❌ Topilmadi",show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_del_"))
def cb_admin_del(call):
    if call.from_user.id!=MAIN_ADMIN: bot.answer_callback_query(call.id,"❌ Faqat asosiy admin!",show_alert=True); return
    aid = int(call.data.split("_")[-1])
    if aid==MAIN_ADMIN: bot.answer_callback_query(call.id,"❌ Asosiy adminni o'chirib bo'lmaydi!",show_alert=True); return
    r = db("DELETE FROM adminlar WHERE id=%s RETURNING id",(aid,),one=True)
    if r:
        kesh_del(f"adm_{aid}")
        admin_log(call.from_user.id, "admin_ochirish", aid)
        bot.answer_callback_query(call.id,f"✅ {aid} o'chirildi",show_alert=True)
        cb_adminlar(call)
    else:
        bot.answer_callback_query(call.id,"❌ Topilmadi",show_alert=True)

@bot.callback_query_handler(func=lambda c: c.data=="admin_qosh")
def cb_admin_qosh(call):
    if call.from_user.id!=MAIN_ADMIN: return
    holat_set(admin_holat, call.from_user.id, {"qadam":"admin_id"})
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    yuborish(call.message.chat.id,
        "➕ <b>Admin qo'shish</b>\n\n"
        "Yangi admin Telegram ID sini yuboring:\n"
        "💡 Admin <code>/myid</code> yozib bilsin\n\n"
        "❌ Bekor: /admin",
        parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("qism_") and c.data!="qism_tayyor")
def cb_qism_qosh(call):
    if not admin_mi(call.from_user.id): return
    kod = call.data[5:]
    q   = db("SELECT COUNT(*) FROM kino_qismlar WHERE kod=%s",(kod,),one=True)[0]
    holat_set(admin_holat, call.from_user.id, {"qadam":"qism_video","malumot":{"kod":kod,"qism_num":q+1}})
    bot.answer_callback_query(call.id)
    yuborish(call.message.chat.id,f"🎥 <b>{q+1}-qism</b> videosini yuboring:", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data=="qism_tayyor")
def cb_qism_tayyor(call):
    if not admin_mi(call.from_user.id): return
    bot.answer_callback_query(call.id,"✅ Kino saqlandi!")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data=="poster_skip")
def cb_poster_skip(call):
    if not admin_mi(call.from_user.id): return
    uid = call.from_user.id
    holat = holat_ol(admin_holat, uid)
    if holat and holat["qadam"]=="poster":
        holat["qadam"] = "video"
        bot.answer_callback_query(call.id)
        yuborish(call.message.chat.id,"🎥 <b>1-qism</b> videosini yuboring:", parse_mode="HTML")

# ──────────────────────────────────────────────
#  OBUNA TEKSHIRISH
# ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data=="obuna_tekshir")
def cb_obuna(call):
    uid = call.from_user.id
    kesh_del(f"sub_{uid}")
    ok, ul = obuna_tekshir(uid)
    if ok:
        try: bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception: pass
        yuborish(call.message.chat.id,"✅ Rahmat! Botdan foydalanishingiz mumkin 👇", reply_markup=user_kb())
    else:
        bot.answer_callback_query(call.id,"❗ Hali barcha kanallarga obuna bo'lmagansiz!",show_alert=True)

# ──────────────────────────────────────────────
#  AUDIO EFFEKT CALLBACK
# ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data in EFFEKTLAR)
def cb_effekt(call):
    uid = call.from_user.id
    if ban_mi(uid): bot.answer_callback_query(call.id,"🚫 Siz bloklangansiz",show_alert=True); return
    info = holat_ol(audio_kesh, uid)
    if not info:
        bot.answer_callback_query(call.id,"❌ Audio topilmadi, qaytadan yuklab oling",show_alert=True); return
    if not flood_ok(uid, limit=4, oyna=8):
        bot.answer_callback_query(call.id,"⏳ Juda tez-tez so'rov, biroz kuting",show_alert=True); return
    nom_e, filtr = EFFEKTLAR[call.data]
    bot.answer_callback_query(call.id,f"⏳ {nom_e} qo'llanmoqda...")
    k = yuborish(call.message.chat.id,f"⚙️ <b>{nom_e}</b> qo'llanmoqda...",parse_mode="HTML")
    chiq = effekt_qollan(info.get("url"), info.get("fayl"), filtr)
    try: bot.delete_message(call.message.chat.id, k.message_id)
    except Exception: pass
    if chiq:
        cap = f"🎵 <b>{nom_e}</b> — {html_esc(info.get('nom','Audio'))}\n🎛 Boshqa effekt tanlang 👇\n📥 {BOT_TAG}"
        try:
            with open(chiq,"rb") as f:
                bot.send_audio(call.message.chat.id, f,
                               title=f"{nom_e} — {info.get('nom','')}"[:64],
                               caption=cap, parse_mode="HTML",
                               reply_markup=effektlar_kb())
        finally:
            try: os.remove(chiq)
            except Exception: pass
    else:
        yuborish(call.message.chat.id,"❌ Effekt qo'llashda xatolik")

@bot.callback_query_handler(func=lambda c: c.data.startswith("yukla_musiqa_"))
def cb_yukla_musiqa(call):
    uid = call.from_user.id
    if ban_mi(uid): bot.answer_callback_query(call.id,"🚫 Siz bloklangansiz",show_alert=True); return
    if not flood_ok(uid, limit=4, oyna=8):
        bot.answer_callback_query(call.id,"⏳ Juda tez-tez so'rov, biroz kuting",show_alert=True); return
    vid_id = call.data[len("yukla_musiqa_"):]
    url    = f"https://youtu.be/{vid_id}"
    bot.answer_callback_query(call.id,"⏬ Yuklanmoqda...")
    k = yuborish(call.message.chat.id,"⏬ Musiqa yuklanmoqda... 🎵")
    fayl,nom,dur,muallif,xato,_ = media_yukla(url, audio=True)
    try: bot.delete_message(call.message.chat.id, k.message_id)
    except Exception: pass
    if xato or not fayl: yuklab_xato(call.message.chat.id, xato); return
    holat_set(audio_kesh, uid, {"url":url,"fayl":fayl,"nom":nom})
    audio_yuborish(call.message.chat.id, fayl, nom, dur, muallif, uid)

# ──────────────────────────────────────────────
#  FOYDALANUVCHI TUGMALARI
# ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and not ban_mi(m.from_user.id) and m.text=="📥 Video yuklab olish")
def btn_video(msg):
    holat_set(user_holat, msg.from_user.id, {"rejim":"video"})
    yuborish(msg.chat.id,
        "📥 <b>Video yuklab olish</b>\n\n"
        "✅ Qo'llab-quvvatlanadi:\n"
        "▫️ YouTube  ▫️ TikTok  ▫️ Instagram\n"
        "▫️ Pinterest  ▫️ Twitter/X  ▫️ Facebook\n"
        "▫️ VK  ▫️ Vimeo  ▫️ Dailymotion  ▫️ Twitch\n"
        "▫️ Rumble  ▫️ Bilibili  ▫️ OK.ru  ▫️ Reddit\n"
        "▫️ SoundCloud  ▫️ Streamable  ▫️ Coub\n"
        "▫️ Likee  ▫️ Kwai  ▫️ Odysee va +100 ta\n\n"
        "🔗 Havola yuboring:\n\n❌ Bekor: /start",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and not ban_mi(m.from_user.id) and m.text=="🎵 Musiqa yuklab olish")
def btn_musiqa(msg):
    holat_set(user_holat, msg.from_user.id, {"rejim":"musiqa"})
    yuborish(msg.chat.id,
        "🎵 <b>Musiqa yuklab olish</b>\n\n"
        "YouTube, SoundCloud, Deezer, Bandcamp...\n\n"
        "🔗 Musiqa havolasini yuboring:\n\n❌ Bekor: /start",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and not ban_mi(m.from_user.id) and m.text=="🔍 Musiqa qidirish")
def btn_qidiruv(msg):
    holat_set(user_holat, msg.from_user.id, {"rejim":"qidiruv"})
    yuborish(msg.chat.id,
        "🔍 <b>Musiqa qidirish</b>\n\n"
        "Qo'shiq nomini yuboring:\n"
        "Misol: <code>Ozodbek Nazarov Muhabbat</code>\n\n❌ Bekor: /start",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and not ban_mi(m.from_user.id) and m.text=="🔵 Dumaloq video")
def btn_doira(msg):
    holat_set(user_holat, msg.from_user.id, {"rejim":"doira"})
    yuborish(msg.chat.id,
        "🔵 <b>Dumaloq video</b>\n\n"
        "Video yuboring yoki havola yuboring!\n"
        "Bot uni doira shakliga o'tkazadi 🔄\n\n❌ Bekor: /start",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and not ban_mi(m.from_user.id) and m.text=="🖼 Rasm orqali kino topish")
def btn_rasm(msg):
    holat_set(user_holat, msg.from_user.id, {"rejim":"rasm"})
    yuborish(msg.chat.id,
        "🖼 <b>Rasm orqali kino topish</b>\n\n"
        "Kino posteri yoki kadrini yuboring!\n"
        "Bot Google AI bilan tahlil qiladi 🤖\n\n❌ Bekor: /start",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id) and not ban_mi(m.from_user.id) and m.text=="🎞 Video orqali kino topish")
def btn_kino_video(msg):
    holat_set(user_holat, msg.from_user.id, {"rejim":"kino_video"})
    yuborish(msg.chat.id,
        "🎞 <b>Video orqali kino topish</b>\n\n"
        "Kinoning qisqa klipini yuboring!\n"
        "Bot kadrni olib kino topadi 🤖\n\n❌ Bekor: /start",
        parse_mode="HTML")

# ──────────────────────────────────────────────
#  RASM HANDLER (user)
# ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id), content_types=["photo"])
def foto_handler(msg):
    uid = msg.from_user.id
    if ban_mi(uid): return
    ok, ul = obuna_tekshir(uid)
    if not ok: yuborish(msg.chat.id,"❗ Avval kanallarga obuna bo'ling:",reply_markup=obuna_kb(ul)); return
    if not flood_ok(uid, limit=4, oyna=8):
        yuborish(msg.chat.id,"⏳ Juda tez-tez so'rov yuboryapsiz, biroz kuting"); return
    holat_del(user_holat, uid)
    faollik_yangila(uid)
    k = yuborish(msg.chat.id,"🔍 Rasm tahlil qilinmoqda... ⏳")
    try:
        img = _tg_yukla(msg.photo[-1].file_id,"jpg")
    except Exception:
        try: bot.delete_message(msg.chat.id, k.message_id)
        except Exception: pass
        yuborish(msg.chat.id,"❌ Rasm yuklab bo'lmadi"); return
    kino_qidirish_natija(msg.chat.id, img, k.message_id)

# ──────────────────────────────────────────────
#  ASOSIY HANDLER (user)
# ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: not admin_mi(m.from_user.id),
                     content_types=["text","video","document","video_note"])
def asosiy(msg):
    uid  = msg.from_user.id
    if ban_mi(uid): return
    ok, ul = obuna_tekshir(uid)
    if not ok: yuborish(msg.chat.id,"❗ Avval kanallarga obuna bo'ling:",reply_markup=obuna_kb(ul)); return
    faollik_yangila(uid)
    holat = holat_ol(user_holat, uid) or {}
    rejim = holat.get("rejim","")
    matn  = (msg.text or "").strip()

    og_ish = rejim in ("video","musiqa","kino_video","doira") or (not rejim and matn and url_mi(matn))
    if og_ish and not flood_ok(uid, limit=4, oyna=10):
        yuborish(msg.chat.id,"⏳ Juda tez-tez so'rov yuboryapsiz, biroz kuting"); return

    # ── VIDEO YUKLAB OLISH ──
    if rejim=="video":
        holat_del(user_holat, uid)
        if not url_mi(matn):
            yuborish(msg.chat.id,"❌ To'g'ri havola yuboring!\nMisol: https://youtube.com/watch?v=xxx"); return
        k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda... ⏳\n(1-2 daqiqa ketishi mumkin)")
        fayl,nom,dur,muallif,xato,turi = media_yukla(matn)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except Exception: pass
        if xato or not fayl: yuklab_xato(msg.chat.id, xato); return
        video_yuborish(msg.chat.id, fayl, nom, dur, muallif, uid, turi)

    # ── MUSIQA YUKLAB OLISH ──
    elif rejim=="musiqa":
        holat_del(user_holat, uid)
        if not url_mi(matn):
            yuborish(msg.chat.id,"❌ To'g'ri havola yuboring!"); return
        k = yuborish(msg.chat.id,"⏬ Musiqa yuklanmoqda... 🎵")
        fayl,nom,dur,muallif,xato,_ = media_yukla(matn, audio=True)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except Exception: pass
        if xato or not fayl: yuklab_xato(msg.chat.id, xato); return
        holat_set(audio_kesh, uid, {"url":matn,"fayl":fayl,"nom":nom})
        audio_yuborish(msg.chat.id, fayl, nom, dur, muallif, uid)

    # ── MUSIQA QIDIRISH ──
    elif rejim=="qidiruv":
        holat_del(user_holat, uid)
        if not matn: return
        if not flood_ok(uid, limit=4, oyna=10):
            yuborish(msg.chat.id,"⏳ Juda tez-tez so'rov, biroz kuting"); return
        k = yuborish(msg.chat.id,f"🔍 <b>«{html_esc(matn)}»</b> qidirilmoqda...",parse_mode="HTML")
        natija = musiqa_qidirish(matn)
        try: bot.delete_message(msg.chat.id, k.message_id)
        except Exception: pass
        if not natija:
            yuborish(msg.chat.id,"❌ Topilmadi. Boshqacha yozing."); return
        yuborish(msg.chat.id,f"🎵 <b>Natijalar: «{html_esc(matn)}»</b>",parse_mode="HTML")
        for item in natija:
            cap = f"🎵 <b>{html_esc(item['nom'])}</b>\n👤 {html_esc(item['muallif'])}\n⏱ {item['davomiy']}"
            kb  = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⬇️ Yuklab olish",callback_data=f"yukla_musiqa_{item['id']}"))
            try:
                if item["thumb"]:
                    bot.send_photo(msg.chat.id, item["thumb"], caption=cap, parse_mode="HTML", reply_markup=kb)
                else:
                    yuborish(msg.chat.id, cap, parse_mode="HTML", reply_markup=kb)
            except Exception:
                yuborish(msg.chat.id, cap, parse_mode="HTML", reply_markup=kb)

    # ── VIDEO ORQALI KINO TOPISH ──
    elif rejim=="kino_video":
        holat_del(user_holat, uid)
        fid = None
        if msg.video:      fid = msg.video.file_id
        elif msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
            fid = msg.document.file_id
        elif msg.video_note: fid = msg.video_note.file_id
        if not fid: yuborish(msg.chat.id,"❌ Video yuboring"); return
        k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda... ⏳")
        try:
            vid_fayl = _tg_yukla(fid,"mp4")
        except Exception:
            try: bot.delete_message(msg.chat.id, k.message_id)
            except Exception: pass
            yuborish(msg.chat.id,"❌ Video yuklab bo'lmadi"); return
        try: bot.edit_message_text("🎞 Kadr tahlil qilinmoqda... ⏳",msg.chat.id,k.message_id)
        except Exception: pass
        kadr = video_kadr_ol(vid_fayl)
        try: os.remove(vid_fayl)
        except Exception: pass
        if not kadr:
            try: bot.delete_message(msg.chat.id, k.message_id)
            except Exception: pass
            yuborish(msg.chat.id,"❌ Kadr olib bo'lmadi"); return
        kino_qidirish_natija(msg.chat.id, kadr, k.message_id)

    # ── DUMALOQ VIDEO ──
    elif rejim=="doira":
        holat_del(user_holat, uid)
        fid = None
        if msg.video:      fid = msg.video.file_id
        elif msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
            fid = msg.document.file_id
        if fid:
            k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda...")
            try:
                kirish = _tg_yukla(fid,"mp4")
                try: bot.delete_message(msg.chat.id, k.message_id)
                except Exception: pass
            except Exception:
                try: bot.delete_message(msg.chat.id, k.message_id)
                except Exception: pass
                yuborish(msg.chat.id,"❌ Video yuklab bo'lmadi"); return
        elif matn and url_mi(matn):
            k = yuborish(msg.chat.id,"⏬ Video yuklanmoqda... ⏳")
            kirish,_,_,_,xato,_ = media_yukla(matn)
            try: bot.delete_message(msg.chat.id, k.message_id)
            except Exception: pass
            if xato or not kirish: yuklab_xato(msg.chat.id, xato); return
        else:
            yuborish(msg.chat.id,"❌ Video yuboring"); return
        k2 = yuborish(msg.chat.id,"🔵 Dumaloq videoga aylantirilmoqda... ⏳")
        chiq = dumaloq_video(kirish)
        try: bot.delete_message(msg.chat.id, k2.message_id)
        except Exception: pass
        try: os.remove(kirish)
        except Exception: pass
        if chiq:
            try:
                with open(chiq,"rb") as f: bot.send_video_note(msg.chat.id, f, length=384)
                yuklama_log("doira", uid)
            except Exception: yuborish(msg.chat.id,"❌ Yuborishda xatolik")
            finally:
                try: os.remove(chiq)
                except Exception: pass
        else:
            yuborish(msg.chat.id,"❌ Aylantirshda xatolik")

    # ── UMUMIY: URL yoki Kino kodi ──
    else:
        if matn and url_mi(matn):
            k = yuborish(msg.chat.id,"⏬ Yuklanmoqda... ⏳\n(1-2 daqiqa ketishi mumkin)")
            fayl,nom,dur,muallif,xato,turi = media_yukla(matn)
            try: bot.delete_message(msg.chat.id, k.message_id)
            except Exception: pass
            if xato or not fayl: yuklab_xato(msg.chat.id, xato); return
            video_yuborish(msg.chat.id, fayl, nom, dur, muallif, uid, turi)
            return
        if not matn: return
        kino = db("SELECT nom,poster_id FROM kinolar WHERE kod=%s",(matn,),one=True)
        if not kino:
            yuborish(msg.chat.id,
                "❌ Bunday kodli kino topilmadi\n\n"
                "🖼 Kino posteri yuborsangiz ham topish mumkin!"); return
        nom, poster_id = kino
        qismlar = db("SELECT qism_num,fayl_id FROM kino_qismlar WHERE kod=%s ORDER BY qism_num",(matn,),fetch=True) or []
        if not qismlar: yuborish(msg.chat.id,"❌ Kino fayli hali qo'shilmagan"); return
        db("UPDATE kinolar SET korishlar=korishlar+1 WHERE kod=%s",(matn,))
        kanal_text = (kanallar_ol() or [""])[0]
        if poster_id:
            try: bot.send_photo(msg.chat.id, poster_id, caption=f"🎬 <b>{html_esc(nom)}</b>",parse_mode="HTML",protect_content=True)
            except Exception: pass
        for qnum, fid in qismlar:
            cap = f"🎬 <b>{html_esc(nom)}</b>" + (f"\n🎞 {qnum}-qism" if len(qismlar)>1 else "")
            if kanal_text: cap += f"\n\n📢 {html_esc(kanal_text)}"
            try: bot.send_video(msg.chat.id,fid,caption=cap,parse_mode="HTML",protect_content=True)
            except Exception:
                try: bot.send_document(msg.chat.id,fid,caption=cap,parse_mode="HTML",protect_content=True)
                except Exception: yuborish(msg.chat.id,f"❌ {qnum}-qism yuborishda xatolik")

# ──────────────────────────────────────────────
#  ADMIN HOLAT HANDLER — matn/video/rasm/hujjat
# ──────────────────────────────────────────────

@bot.message_handler(func=lambda m: admin_mi(m.from_user.id) and holat_ol(admin_holat, m.from_user.id) is not None,
                     content_types=["text","video","document","photo"])
def admin_handler(msg):
    uid   = msg.from_user.id
    holat = holat_ol(admin_holat, uid)
    if not holat: return
    qadam = holat["qadam"]

    if qadam=="kod":
        holat["malumot"]["kod"] = (msg.text or "").strip()[:64]
        holat["qadam"] = "nom"
        yuborish(msg.chat.id,"📝 Kino/Serial nomini yuboring:")

    elif qadam=="nom":
        holat["malumot"]["nom"] = (msg.text or "").strip()[:200]
        holat["qadam"] = "poster"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⏭ O'tkazib yuborish", callback_data="poster_skip"))
        yuborish(msg.chat.id,"🖼 Kino posterini yuboring (yoki o'tkazib yuboring):", reply_markup=kb)

    elif qadam=="poster":
        if msg.photo: holat["malumot"]["poster"] = msg.photo[-1].file_id
        holat["qadam"] = "video"
        yuborish(msg.chat.id,"🎥 <b>1-qism</b> videosini yuboring:",parse_mode="HTML")

    elif qadam=="video":
        fid = (msg.video and msg.video.file_id) or (msg.document and msg.document.file_id)
        if not fid: yuborish(msg.chat.id,"❗ Video yuboring"); return
        kod    = holat["malumot"]["kod"]
        nom    = holat["malumot"]["nom"]
        poster = holat["malumot"].get("poster")
        db("INSERT INTO kinolar (kod,nom,poster_id) VALUES (%s,%s,%s) ON CONFLICT (kod) DO UPDATE SET nom=%s,poster_id=%s",
           (kod,nom,poster,nom,poster))
        db("INSERT INTO kino_qismlar (kod,qism_num,fayl_id) VALUES (%s,1,%s) ON CONFLICT (kod,qism_num) DO UPDATE SET fayl_id=%s",
           (kod,fid,fid))
        admin_log(uid, "kino_qoshish", tafsilot=kod)
        holat_del(admin_holat, uid)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➕ Yana qism qo'shish",callback_data=f"qism_{kod}"),
               types.InlineKeyboardButton("✅ Tayyor",callback_data="qism_tayyor"))
        yuborish(msg.chat.id,
            f"✅ <b>{html_esc(nom)}</b> saqlandi!\n"
            f"🔢 Kod: <code>{html_esc(kod)}</code>\n\n"
            "Yana qism qo'shasizmi?",
            parse_mode="HTML", reply_markup=kb)

    elif qadam=="qism_video":
        fid = (msg.video and msg.video.file_id) or (msg.document and msg.document.file_id)
        if not fid: yuborish(msg.chat.id,"❗ Video yuboring"); return
        kod  = holat["malumot"]["kod"]
        qnum = holat["malumot"]["qism_num"]
        db("INSERT INTO kino_qismlar (kod,qism_num,fayl_id) VALUES (%s,%s,%s) ON CONFLICT (kod,qism_num) DO UPDATE SET fayl_id=%s",
           (kod,qnum,fid,fid))
        holat_del(admin_holat, uid)
        jami = db("SELECT COUNT(*) FROM kino_qismlar WHERE kod=%s",(kod,),one=True)[0]
        kb   = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("➕ Yana qism qo'shish",callback_data=f"qism_{kod}"),
               types.InlineKeyboardButton("✅ Tayyor",callback_data="qism_tayyor"))
        yuborish(msg.chat.id,f"✅ <b>{qnum}-qism</b> qo'shildi! Jami: {jami}",parse_mode="HTML",reply_markup=kb)

    elif qadam=="kino_ochir":
        kod = (msg.text or "").strip()[:64]
        db("DELETE FROM kino_qismlar WHERE kod=%s",(kod,))
        r = db("DELETE FROM kinolar WHERE kod=%s RETURNING kod",(kod,),one=True)
        if r: admin_log(uid, "kino_ochirish", tafsilot=kod)
        yuborish(msg.chat.id,
            f"✅ <code>{html_esc(kod)}</code> o'chirildi!" if r else "❌ Bunday kod topilmadi",
            parse_mode="HTML")
        holat_del(admin_holat, uid)
        admin_panel_yuborish(msg.chat.id)

    elif qadam=="user_qidir":
        sorov = (msg.text or "").strip()[:64]
        holat_del(admin_holat, uid)
        if sorov.isdigit():
            lst = db("SELECT id,name,qoshildi,banned FROM bot_users WHERE id=%s",(int(sorov),),fetch=True) or []
        else:
            lst = db("SELECT id,name,qoshildi,banned FROM bot_users WHERE name ILIKE %s ESCAPE '\\' LIMIT 15",
                      (f"%{like_escape(sorov)}%",), fetch=True) or []
        if not lst:
            yuborish(msg.chat.id,"❌ Foydalanuvchi topilmadi"); admin_panel_yuborish(msg.chat.id); return
        if len(lst)==1:
            matn, kb = _user_profil_matn_kb(*lst[0])
            yuborish(msg.chat.id, matn, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        else:
            kb = types.InlineKeyboardMarkup()
            for uid_v,ism,_,banned in lst:
                lbl = f"{'🚫' if banned else '👤'} {(ism or str(uid_v))[:24]} ({uid_v})"
                kb.add(types.InlineKeyboardButton(lbl, callback_data=f"user_prof_{uid_v}"))
            kb.add(types.InlineKeyboardButton("⬅️ Orqaga", callback_data="adm_bosh"))
            yuborish(msg.chat.id, f"🔎 <b>{len(lst)} ta natija topildi:</b>", parse_mode="HTML", reply_markup=kb)

    elif qadam=="admin_id":
        try: new_id = int((msg.text or "").strip())
        except Exception:
            yuborish(msg.chat.id,"❗ Faqat raqam kiriting"); holat_del(admin_holat, uid); return
        db("INSERT INTO adminlar (id) VALUES (%s) ON CONFLICT DO NOTHING",(new_id,))
        kesh_del(f"adm_{new_id}")
        admin_log(uid, "admin_qoshish", new_id)
        yuborish(msg.chat.id,f"✅ Admin qo'shildi: <code>{new_id}</code>",parse_mode="HTML")
        try: yuborish(new_id,"🎉 Siz admin bo'ldingiz! /start yoki /admin bosing.")
        except Exception: pass
        holat_del(admin_holat, uid)
        admin_panel_yuborish(msg.chat.id)

    elif qadam=="kanal_qosh":
        u = (msg.text or "").strip()
        if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", u):
            yuborish(msg.chat.id,"❌ Noto'g'ri username formati!")
            holat_del(admin_holat, uid); admin_panel_yuborish(msg.chat.id); return
        if not u.startswith("@"): u = "@"+u
        try:
            bot.get_chat(u)
            lst = kanallar_ol()
            if u in lst:
                yuborish(msg.chat.id,f"⚠️ {u} allaqachon qo'shilgan!")
            elif len(lst)>=5:
                yuborish(msg.chat.id,"❌ Maksimum 5 ta kanal!")
            else:
                lst.append(u); kanallar_saqlash(lst)
                admin_log(uid, "kanal_qoshish", tafsilot=u)
                yuborish(msg.chat.id,f"✅ {u} qo'shildi! ({len(lst)}/5)")
        except Exception:
            yuborish(msg.chat.id,"❌ Kanal topilmadi!\n⚠️ Bot kanalga admin bo'lishi kerak.")
        holat_del(admin_holat, uid)
        admin_panel_yuborish(msg.chat.id)

    elif qadam=="xabar_yuborish":
        t_uid = holat["malumot"]["uid"]
        ism   = holat["malumot"]["ism"]
        holat_del(admin_holat, uid)
        try:
            bot.copy_message(t_uid, msg.chat.id, msg.message_id)
            admin_log(uid, "xabar_yuborish", t_uid)
            yuborish(msg.chat.id, f"✅ <b>{html_esc(ism)}</b> ga xabar yuborildi!", parse_mode="HTML")
        except Exception as e:
            s = str(e).lower()
            if any(x in s for x in ["blocked","deactivated","not found"]):
                yuborish(msg.chat.id, f"❌ <b>{html_esc(ism)}</b> boti bloklagan yoki o'chirilgan", parse_mode="HTML")
            else:
                yuborish(msg.chat.id, f"❌ Yuborishda xatolik: {html_esc(str(e)[:80])}")
        admin_panel_yuborish(msg.chat.id)

    elif qadam=="reklama":
        lst  = db("SELECT id FROM bot_users WHERE banned=FALSE",fetch=True) or []
        jami = len(lst)
        yuborish(msg.chat.id,f"📨 Yuborilmoqda... ({jami} ta foydalanuvchi)")
        def yuboruvchi(u):
            try: bot.copy_message(u[0],msg.chat.id,msg.message_id); return True
            except Exception as e:
                if any(x in str(e).lower() for x in ["blocked","deactivated","not found"]):
                    db("DELETE FROM bot_users WHERE id=%s",(u[0],))
                return False
        y=f=0
        with ThreadPoolExecutor(max_workers=20) as ex:
            for ok in ex.map(yuboruvchi, lst):
                if ok: y+=1
                else:  f+=1
        admin_log(uid, "reklama", tafsilot=f"yuborildi={y} xato={f}")
        yuborish(msg.chat.id,f"✅ Yuborildi: {y}\n❌ Xato: {f}\n📊 Jami: {jami}")
        holat_del(admin_holat, uid)
        admin_panel_yuborish(msg.chat.id)

# ──────────────────────────────────────────────
#  ISHGA TUSHIRISH
# ──────────────────────────────────────────────

def keep_alive():
    url = os.getenv("RENDER_EXTERNAL_URL","")
    if not url: return
    while True:
        try: time.sleep(14*60); urllib.request.urlopen(url+"/health",timeout=10)
        except Exception: pass

def run_bot():
    log.info("Bot ishga tushdi ✅")
    while True:
        try: bot.infinity_polling(timeout=10,long_polling_timeout=5)
        except Exception as e: log.error(f"Polling: {e}"); time.sleep(5)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=flask_start, daemon=True).start()
    threading.Thread(target=keep_alive,  daemon=True).start()
    run_bot()
