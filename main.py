  BOT_TOKEN             = 
  TMDB_API_KEY          = themoviedb.org/settings/api (kino uchun)
  SPOTIFY_CLIENT_ID     = developer.spotify.com (ixtiyoriy)
  SPOTIFY_CLIENT_SECRET = (ixtiyoriy)
  GENIUS_TOKEN          = genius.com/api-clients (ixtiyoriy)
  ADMIN_IDS             = 123456789,987654321   (vergul bilan)
"""

import os, re, time, asyncio, tempfile, subprocess, json
from io import BytesIO
from datetime import datetime
from collections import defaultdict

import httpx
import yt_dlp
from PIL import Image
from pydub import AudioSegment
from pydub.effects import normalize

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TimedOut, NetworkError

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIPY_OK = True
except ImportError:
    SPOTIPY_OK = False

try:
    import lyricsgenius
    GENIUS_OK = True
except ImportError:
    GENIUS_OK = False

try:
    from youtubesearchpython import VideosSearch
    YTSEARCH_OK = True
except ImportError:
    YTSEARCH_OK = False

# ═══════════════════════════════════════════════════════════
# KONFIGURATSIYA
# ═══════════════════════════════════════════════════════════

BOT_TOKEN             = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TMDB_API_KEY          = os.getenv("TMDB_API_KEY", "")
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
GENIUS_TOKEN          = os.getenv("GENIUS_TOKEN", "")
ADMIN_IDS_RAW         = os.getenv("ADMIN_IDS", "")

ADMIN_IDS: set[int] = set()
for _a in ADMIN_IDS_RAW.split(","):
    if _a.strip().isdigit():
        ADMIN_IDS.add(int(_a.strip()))

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"

# ─── In-memory storage ────────────────────────────────────
STATS: dict     = defaultdict(lambda: {"dl": 0, "music": 0, "movies": 0, "effects": 0, "joined": ""})
USER_INFO: dict = {}   # uid → {"name": str, "username": str, "banned": bool}
BROADCAST_MSG   = {}   # temp storage

AUDIO_EFFECTS = {
    "bass":   "🔊 Bass Boost",
    "reverb": "🎵 Reverb",
    "echo":   "🌊 Echo",
    "vocal":  "🎤 Vocal Boost",
    "slow":   "🐢 Slow (0.75x)",
    "fast":   "🐇 Fast (1.25x)",
    "rock":   "🎸 Rock",
    "lofi":   "🎹 Lo-Fi",
}

PLATFORMS = [
    ("▶️ YouTube",   "https://youtube.com"),
    ("📸 Instagram", "https://instagram.com"),
    ("🎵 TikTok",    "https://tiktok.com"),
    ("📌 Pinterest", "https://pinterest.com"),
    ("🐦 Twitter/X", "https://x.com"),
    ("👥 Facebook",  "https://facebook.com"),
    ("🎬 Vimeo",     "https://vimeo.com"),
    ("🎮 Twitch",    "https://twitch.tv"),
    ("🤖 Reddit",    "https://reddit.com"),
    ("🎬 Dailymot.", "https://dailymotion.com"),
]

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def is_url(t: str) -> bool:
    return bool(re.match(r"https?://", t.strip()))

def fmt_dur(sec) -> str:
    if not sec: return "0:00"
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def fmt_num(n) -> str:
    try: return f"{int(n):,}"
    except: return str(n)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_banned(uid: int) -> bool:
    return USER_INFO.get(uid, {}).get("banned", False)

def register_user(user):
    uid = user.id
    if uid not in USER_INFO:
        USER_INFO[uid] = {
            "name": user.full_name,
            "username": user.username or "",
            "banned": False,
            "id": uid,
        }
        STATS[uid]["joined"] = datetime.now().strftime("%d.%m.%Y %H:%M")
    else:
        USER_INFO[uid]["name"] = user.full_name
        USER_INFO[uid]["username"] = user.username or ""

def record(uid: int, key: str):
    STATS[uid][key] += 1

async def typing(update: Update):
    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except: pass

def sp():
    if SPOTIPY_OK and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            return spotipy.Spotify(auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET))
        except: pass
    return None

def gn():
    if GENIUS_OK and GENIUS_TOKEN:
        try:
            return lyricsgenius.Genius(GENIUS_TOKEN, verbose=False,
                skip_non_songs=True, excluded_terms=["(Remix)", "(Live)"])
        except: pass
    return None

# ─── ffmpeg ───────────────────────────────────────────────

def run_ff(args: list) -> bool:
    return subprocess.run(["ffmpeg", "-y"] + args, capture_output=True).returncode == 0

def make_circle(inp: str) -> str | None:
    out = inp.replace(".mp4", "_c.webm")
    ok = run_ff(["-i", inp,
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=512:512,"
               "format=yuva420p,"
               "geq=lum='p(X,Y)':a='if(lte(hypot(X-W/2,Y-H/2),W/2),255,0)'",
        "-c:v", "libvpx-vp9", "-c:a", "libopus", "-b:v", "1M", out])
    if ok and os.path.exists(out) and os.path.getsize(out) > 500:
        return out
    out2 = inp.replace(".mp4", "_c.mp4")
    ok2 = run_ff(["-i", inp, "-vf",
        "crop='min(iw,ih)':'min(iw,ih)',scale=512:512",
        "-c:v", "libx264", "-c:a", "aac", out2])
    return out2 if ok2 and os.path.exists(out2) else None

def fx(audio: AudioSegment, eid: str) -> AudioSegment:
    if eid == "bass":   audio = audio.low_pass_filter(300) + 6
    elif eid == "reverb":
        d = (audio - 12).overlay(audio - 18, position=60)
        audio = audio.overlay(d, position=30)
    elif eid == "echo":
        audio = audio.overlay(audio - 8, position=300).overlay(audio - 16, position=600)
    elif eid == "vocal": audio = audio.high_pass_filter(600) + 5
    elif eid == "slow":
        audio = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * 0.75)}).set_frame_rate(audio.frame_rate)
    elif eid == "fast":
        audio = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * 1.25)}).set_frame_rate(audio.frame_rate)
    elif eid == "rock":  audio = audio.high_pass_filter(200).low_pass_filter(8000) + 3
    elif eid == "lofi":
        audio = audio.low_pass_filter(3000) - 3
        audio = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * 0.95)}).set_frame_rate(audio.frame_rate)
    return normalize(audio)

# ─── yt-dlp ───────────────────────────────────────────────

def _dl_sync(url: str, audio_only: bool, quality: str = "best") -> dict:
    fmt = "bestaudio/best"
    if not audio_only:
        q = {"720": "height<=720", "480": "height<=480"}.get(quality, "")
        fmt = (f"bestvideo[{q}][ext=mp4]+bestaudio[ext=m4a]/best[{q}][ext=mp4]/best"
               if q else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": fmt,
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "postprocessors": [],
        }
        if audio_only:
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3", "preferredquality": "320"})

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        ext = "mp3" if audio_only else "mp4"
        fdata = tdata = None
        for fn in os.listdir(tmp):
            fp = os.path.join(tmp, fn)
            if fn.endswith(ext) and not fdata:
                fdata = open(fp, "rb").read()
            elif fn.endswith((".jpg", ".png", ".webp")) and not tdata:
                tdata = open(fp, "rb").read()

        return {
            "data": fdata, "thumb": tdata,
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader", ""),
            "duration": info.get("duration", 0),
            "ext": ext,
        }

async def ydl(url: str, audio_only: bool, quality: str = "best") -> dict:
    return await asyncio.get_event_loop().run_in_executor(
        None, _dl_sync, url, audio_only, quality)

# ─── TMDB ─────────────────────────────────────────────────

async def tmdb(path: str, params: dict | None = None) -> dict | None:
    if not TMDB_API_KEY: return None
    p = {"api_key": TMDB_API_KEY, "language": "en-US", **(params or {})}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{TMDB_BASE}{path}", params=p)
            if r.status_code == 200: return r.json()
    except: pass
    return None

async def tmdb_search(q: str) -> list:
    d = await tmdb("/search/multi", {"query": q, "include_adult": False})
    return (d or {}).get("results", [])[:6]

async def tmdb_detail(mid: int, mt: str) -> dict | None:
    path = f"/{'tv' if mt == 'tv' else 'movie'}/{mid}"
    return await tmdb(path, {"append_to_response": "credits,videos"})

async def tmdb_img(poster: str) -> bytes | None:
    if not poster: return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(TMDB_IMG + poster)
            return r.content if r.status_code == 200 else None
    except: return None

async def tmdb_trending() -> list:
    d = await tmdb("/trending/all/week")
    return (d or {}).get("results", [])[:8]

# ─── Reverse image search ─────────────────────────────────

async def yandex_reverse(img: bytes) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as c:
            r = await c.post("https://yandex.com/images/search",
                params={"rpt": "imageview", "format": "json"},
                files={"upfile": ("img.jpg", img, "image/jpeg")})
            cbir = None
            if r.headers.get("location"):
                cbir = "https://yandex.com" + r.headers["location"]
            else:
                m = re.search(r'(?:url|href)=["\']([^"\']*cbir[^"\']*)["\']', r.text)
                if m:
                    cbir = m.group(1)
                    if not cbir.startswith("http"):
                        cbir = "https://yandex.com" + cbir
            if not cbir: return None
            r2 = await c.get(cbir)
            ts = re.findall(r'"snippet":\s*"([^"]{5,80})"', r2.text)
            if not ts:
                ts = re.findall(r'<title[^>]*>([^<]{5,100})</title>', r2.text)
            if ts:
                clean = re.sub(r'\s*[—\-|]\s*Yandex.*', '', ts[0]).strip()
                return clean if len(clean) > 3 else None
    except: pass
    return None

async def google_lens(img: bytes) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.post("https://lens.google.com/upload",
                params={"re": "df", "st": str(int(time.time()*1000)), "ep": "gsbubb"},
                files={"encoded_image": ("img.jpg", img, "image/jpeg")})
            ts = re.findall(r'"([A-Z][a-zA-Z\s\(\)0-9:]{5,60})"', r.text)
            movie = [t for t in ts if any(w in t.lower() for w in
                ["movie","film","series","season","show","2019","2020","2021","2022","2023","2024"])]
            return movie[0] if movie else (ts[0] if ts else None)
    except: return None

# ═══════════════════════════════════════════════════════════
# ★ START — Chiroyli tugmachalar
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)

    if is_banned(user.id):
        await update.message.reply_text("🚫 Siz bloklangansiz.")
        return

    text = (
        f"👋 *Salom, {user.first_name}!*\n\n"
        "🤖 *Pro Media Bot* — quyidagi platformalardan\n"
        "video va rasmlarni yuklab oling!\n\n"
        "🔽 Kerakli platformani tanlang:"
    )

    # Platform tugmalari (2 dona qatoriga)
    plat_kb = []
    row = []
    for name, _ in PLATFORMS:
        row.append(InlineKeyboardButton(name, callback_data=f"PLT|{name}"))
        if len(row) == 2:
            plat_kb.append(row); row = []
    if row: plat_kb.append(row)

    # Asosiy funksiyalar
    plat_kb.append([
        InlineKeyboardButton("🎵 Musiqa yuklab olish", callback_data="MENU|music"),
        InlineKeyboardButton("🎬 Kino qidirish",       callback_data="MENU|movie"),
    ])
    plat_kb.append([
        InlineKeyboardButton("🎛 Audio Effektlar",     callback_data="MENU|effect"),
        InlineKeyboardButton("⭕ Aylana Video",         callback_data="MENU|circle"),
    ])
    plat_kb.append([
        InlineKeyboardButton("📊 Statistikam",         callback_data="MENU|stats"),
        InlineKeyboardButton("❓ Yordam",               callback_data="MENU|help"),
    ])

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(plat_kb))

async def platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split("|", 1)[1]

    platform_info = {
        "▶️ YouTube":   ("YouTube", "youtube.com/watch?v=...\nyoutu.be/..."),
        "📸 Instagram": ("Instagram", "instagram.com/p/...\ninstagram.com/reel/..."),
        "🎵 TikTok":    ("TikTok", "tiktok.com/@user/video/..."),
        "📌 Pinterest": ("Pinterest", "pin.it/...\npinterest.com/pin/..."),
        "🐦 Twitter/X": ("Twitter/X", "twitter.com/i/status/...\nx.com/i/status/..."),
        "👥 Facebook":  ("Facebook", "fb.watch/...\nfacebook.com/watch/..."),
        "🎬 Vimeo":     ("Vimeo", "vimeo.com/123456"),
        "🎮 Twitch":    ("Twitch", "twitch.tv/videos/..."),
        "🤖 Reddit":    ("Reddit", "reddit.com/r/.../..."),
        "🎬 Dailymot.": ("Dailymotion", "dailymotion.com/video/..."),
    }
    pname, example = platform_info.get(name, (name, "havolani yuboring"))

    text = (
        f"{name} *{pname}*\n\n"
        f"Havola formatiga misol:\n"
        f"`{example}`\n\n"
        f"📋 Havolani bu chatga yuboring — bot avtomatik aniqlaydi!\n\n"
        f"📥 Sifat tanlash imkoniyati:\n"
        f"• 🎬 Best sifat\n"
        f"• 📺 720p HD\n"
        f"• 📱 480p\n"
        f"• 🎵 MP3 (faqat ovoz)\n"
        f"• ⭕ Aylana video (video_note)"
    )
    kb = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="BACK|start")]]
    await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb))

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split("|", 1)[1]
    uid = query.from_user.id

    back_btn = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="BACK|start")]]

    if action == "help":
        text = (
            "❓ *Qo'llanma*\n\n"
            "📥 *Video/Rasm yuklab olish:*\n"
            "Havolani yuboring → sifat tanlang → tayyor!\n"
            "10+ platformadan ishlaydi\n\n"
            "🎵 *Musiqa:*\n"
            "`/music <nom>` — Spotify+YouTube qidirish\n"
            "`/lyrics <nom>` — qo'shiq matni\n\n"
            "🎬 *Kino:*\n"
            "`/movie <nom>` — TMDB dan kino topish\n"
            "`/trending` — haftalik top kinolar\n"
            "📸 Rasm yuboring → kinoni rasmdan topadi!\n\n"
            "🎛 *Effektlar:*\n"
            "`/effect` → audio yuboring → effekt tanlang\n\n"
            "⭕ *Aylana video:*\n"
            "`/circle` → video yuboring"
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn))

    elif action == "stats":
        s = STATS[uid]
        total = s["dl"] + s["music"] + s["movies"] + s["effects"]
        text = (
            f"📊 *Sizning statistikangiz*\n\n"
            f"📥 Video/Audio: `{s['dl']}`\n"
            f"🎵 Musiqa: `{s['music']}`\n"
            f"🎬 Kino qidirish: `{s['movies']}`\n"
            f"🎛 Effektlar: `{s['effects']}`\n"
            f"─────────────\n"
            f"📈 Jami: `{total}`\n"
            f"📅 Qo'shilgan: `{s['joined'] or 'N/A'}`"
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn))

    elif action == "music":
        text = (
            "🎵 *Musiqa yuklab olish*\n\n"
            "Buyruqdan foydalaning:\n\n"
            "`/music <qo'shiq nomi>`\n\n"
            "Misol:\n"
            "`/music Doja Cat Paint The Town Red`\n"
            "`/music The Weeknd Blinding Lights`\n\n"
            "Bot Spotify + YouTube orqali topadi,\n"
            "rasm + davomiylik bilan ko'rsatadi."
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn))

    elif action == "movie":
        text = (
            "🎬 *Kino qidirish*\n\n"
            "1️⃣ Buyruq bilan:\n"
            "`/movie <kino nomi>`\n\n"
            "2️⃣ Rasm bilan:\n"
            "Kino posteri yoki aktyor rasmini yuboring!\n"
            "Bot Yandex orqali aniqlaydi.\n\n"
            "3️⃣ Trend kinolar:\n"
            "`/trending` — haftanin top 8 kinosi"
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn))

    elif action == "effect":
        eff_kb = []
        row = []
        for eid, ename in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
            if len(row) == 2:
                eff_kb.append(row); row = []
        if row: eff_kb.append(row)
        eff_kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="BACK|start")])
        context.user_data["effect_waiting"] = True
        context.user_data["effect_id"] = None
        await query.message.edit_text(
            "🎛 *Audio Effektlar*\n\nEffektni tanlang, keyin audio yuboring:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(eff_kb))

    elif action == "circle":
        context.user_data["circle_waiting"] = True
        await query.message.edit_text(
            "⭕ *Aylana Video*\n\nVideo faylni yuboring:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_btn))

async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()
    # /start ni qayta yuborish
    await cmd_start.__wrapped__(update, context) if hasattr(cmd_start, "__wrapped__") else None

# ═══════════════════════════════════════════════════════════
# VIDEO DOWNLOAD
# ═══════════════════════════════════════════════════════════

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    uid = update.effective_user.id
    if is_banned(uid):
        return

    kb = [
        [InlineKeyboardButton("🎬 Best sifat", callback_data=f"DL|video|best|{url}"),
         InlineKeyboardButton("📺 720p HD",    callback_data=f"DL|video|720|{url}")],
        [InlineKeyboardButton("📱 480p",       callback_data=f"DL|video|480|{url}"),
         InlineKeyboardButton("🎵 MP3 320k",   callback_data=f"DL|audio|best|{url}")],
        [InlineKeyboardButton("⭕ Aylana",      callback_data=f"DL|circle|best|{url}"),
         InlineKeyboardButton("ℹ️ Ma'lumot",   callback_data=f"DL|info|best|{url}")],
    ]
    await update.message.reply_text("⬇️ *Qanday yuklab olasiz?*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 3)
    if len(parts) < 4: return
    _, mode, quality, url = parts
    uid = query.from_user.id

    msg = await query.message.reply_text("⏳ Yuklab olinmoqda...")
    try:
        if mode == "info":
            await _video_info(query, url, msg)
            return

        is_audio  = mode == "audio"
        is_circle = mode == "circle"
        result = await ydl(url, audio_only=(is_audio or is_circle), quality=quality)

        if not result["data"]:
            await msg.edit_text("❌ Yuklab bo'lmadi.")
            return

        cap   = f"🎬 *{result['title']}*\n👤 {result['uploader']}\n⏱ {fmt_dur(result['duration'])}"
        thumb = BytesIO(result["thumb"]) if result.get("thumb") else None

        if is_audio:
            await query.message.reply_audio(audio=BytesIO(result["data"]),
                caption=cap, parse_mode=ParseMode.MARKDOWN,
                title=result["title"], performer=result["uploader"],
                thumbnail=thumb)
            record(uid, "music")

        elif is_circle:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                f.write(result["data"]); inp = f.name
            tmp_mp4 = inp.replace(".mp3", "_tmp.mp4")
            run_ff(["-i", inp, "-c:v", "libx264", "-c:a", "aac", tmp_mp4])
            src = tmp_mp4 if os.path.exists(tmp_mp4) else inp
            final = make_circle(src)
            if final and os.path.exists(final):
                await query.message.reply_video_note(
                    video_note=BytesIO(open(final, "rb").read()),
                    duration=result["duration"] or 0, length=512)
                os.unlink(final)
            else:
                await query.message.reply_audio(audio=BytesIO(result["data"]),
                    caption=cap, parse_mode=ParseMode.MARKDOWN)
            for p in [inp, tmp_mp4]:
                try: os.unlink(p)
                except: pass
            record(uid, "dl")

        else:
            await query.message.reply_video(video=BytesIO(result["data"]),
                caption=cap, parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True, thumbnail=thumb)
            record(uid, "dl")

        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ Xato: `{str(e)[:250]}`", parse_mode=ParseMode.MARKDOWN)

async def _video_info(query, url: str, msg):
    def _get():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl_:
            return ydl_.extract_info(url, download=False)
    try:
        info = await asyncio.get_event_loop().run_in_executor(None, _get)
        text = (
            f"ℹ️ *Video ma'lumoti*\n\n"
            f"📌 *{info.get('title','?')}*\n"
            f"👤 {info.get('uploader','?')}\n"
            f"⏱ {fmt_dur(info.get('duration',0))}\n"
            f"👁 {fmt_num(info.get('view_count',0))} ko'rish\n"
            f"👍 {fmt_num(info.get('like_count',0))} layk\n"
            f"🎞 {len(info.get('formats',[]))} format mavjud\n\n"
            f"📝 {(info.get('description') or '')[:300]}"
        )
        kb = [[
            InlineKeyboardButton("🎬 Yuklab olish", callback_data=f"DL|video|best|{url}"),
            InlineKeyboardButton("🎵 MP3", callback_data=f"DL|audio|best|{url}"),
        ]]
        thumb = info.get("thumbnail", "")
        if thumb:
            async with httpx.AsyncClient(timeout=15) as c:
                ir = await c.get(thumb)
            await msg.delete()
            await query.message.reply_photo(photo=BytesIO(ir.content),
                caption=text[:1024], parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(kb))
        else:
            await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")

# ═══════════════════════════════════════════════════════════
# MUSIQA
# ═══════════════════════════════════════════════════════════

async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid): return
    if not context.args:
        await update.message.reply_text("🎵 `/music <qo'shiq nomi>`", parse_mode=ParseMode.MARKDOWN)
        return
    q = " ".join(context.args)
    await typing(update)
    msg = await update.message.reply_text(f"🔍 *{q}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)

    spotify = sp()
    text = f"🎵 *{q}* natijalari:\n\n"
    kb = []
    thumb_url = None

    if spotify:
        try:
            res = spotify.search(q=q, type="track", limit=6)
            for i, t in enumerate(res["tracks"]["items"]):
                name   = t["name"]
                artist = t["artists"][0]["name"]
                m, s   = divmod(t["duration_ms"] // 1000, 60)
                if t["album"]["images"] and not thumb_url:
                    thumb_url = t["album"]["images"][0]["url"]
                text += f"{i+1}. 🎵 *{name}*\n   👤 {artist} | ⏱ {m}:{s:02d}\n\n"
                kb.append([InlineKeyboardButton(
                    f"⬇️ {name[:28]} — {artist[:18]}",
                    callback_data=f"MUSIC|sp|{name}|{artist}")])
        except: pass

    if not kb and YTSEARCH_OK:
        try:
            for item in VideosSearch(q + " official audio", limit=5).result()["result"]:
                text += f"• *{item['title']}*\n  👤 {item['channel']['name']}\n\n"
                kb.append([InlineKeyboardButton(f"⬇️ {item['title'][:40]}",
                    callback_data=f"MUSIC|yt|{item['link']}|_")])
        except: pass

    if not kb:
        await msg.edit_text("❌ Topilmadi."); return

    rm = InlineKeyboardMarkup(kb)
    if thumb_url:
        async with httpx.AsyncClient(timeout=15) as c:
            ir = await c.get(thumb_url)
        await msg.delete()
        await update.message.reply_photo(photo=BytesIO(ir.content),
            caption=text[:1024], reply_markup=rm, parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.edit_text(text[:3000], reply_markup=rm, parse_mode=ParseMode.MARKDOWN)

async def music_dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, src, a, b = query.data.split("|", 3)
    uid = query.from_user.id
    msg = await query.message.reply_text("⏳ Musiqa yuklab olinmoqda...")
    try:
        if src == "yt":
            url = a
        elif YTSEARCH_OK:
            items = VideosSearch(f"{a} {b} audio", limit=1).result()["result"]
            if not items:
                await msg.edit_text("❌ Topilmadi."); return
            url = items[0]["link"]
        else:
            await msg.edit_text("❌ YouTube search yo'q."); return

        result = await ydl(url, audio_only=True)
        if not result["data"]:
            await msg.edit_text("❌ Yuklab bo'lmadi."); return

        thumb = BytesIO(result["thumb"]) if result.get("thumb") else None
        await query.message.reply_audio(audio=BytesIO(result["data"]),
            title=result["title"], performer=result["uploader"],
            thumbnail=thumb,
            caption=f"🎵 *{result['title']}*", parse_mode=ParseMode.MARKDOWN)
        record(uid, "music")
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")

# ═══════════════════════════════════════════════════════════
# LYRICS
# ═══════════════════════════════════════════════════════════

async def cmd_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("📝 `/lyrics <qo'shiq nomi>`", parse_mode=ParseMode.MARKDOWN)
        return
    q = " ".join(context.args)
    await typing(update)
    msg = await update.message.reply_text(f"🔍 *{q}* matni...", parse_mode=ParseMode.MARKDOWN)
    genius = gn()
    if not genius:
        await msg.edit_text("❌ `GENIUS_TOKEN` sozlanmagan.", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        song = await asyncio.get_event_loop().run_in_executor(None, lambda: genius.search_song(q))
        if not song:
            await msg.edit_text("❌ Topilmadi."); return
        lyr  = song.lyrics[:3500] + "..." if len(song.lyrics) > 3500 else song.lyrics
        text = f"🎵 *{song.title}*\n👤 _{song.artist}_\n\n{lyr}"
        if song.song_art_image_url:
            async with httpx.AsyncClient(timeout=15) as c:
                ir = await c.get(song.song_art_image_url)
            await msg.delete()
            await update.message.reply_photo(photo=BytesIO(ir.content),
                caption=text[:1024], parse_mode=ParseMode.MARKDOWN)
            for i in range(1024, len(text), 4000):
                await update.message.reply_text(text[i:i+4000], parse_mode=ParseMode.MARKDOWN)
        else:
            for i, start in enumerate(range(0, len(text), 4000)):
                chunk = text[start:start+4000]
                if i == 0: await msg.edit_text(chunk, parse_mode=ParseMode.MARKDOWN)
                else: await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")

# ═══════════════════════════════════════════════════════════
# KINO
# ═══════════════════════════════════════════════════════════

def _movie_text(d: dict) -> str:
    title    = d.get("title") or d.get("name") or "?"
    orig     = d.get("original_title") or d.get("original_name") or ""
    year     = (d.get("release_date") or d.get("first_air_date") or "")[:4]
    rating   = d.get("vote_average", 0)
    votes    = d.get("vote_count", 0)
    genres   = ", ".join(g["name"] for g in d.get("genres", []))
    overview = (d.get("overview") or "")[:500]
    runtime  = d.get("runtime") or (d.get("episode_run_time") or [None])[0]
    cast     = ", ".join(a["name"] for a in d.get("credits", {}).get("cast", [])[:5])
    dirs     = ", ".join(p["name"] for p in d.get("credits", {}).get("crew", [])
                         if p.get("job") == "Director")[:2]
    trailer  = next((v["key"] for v in d.get("videos", {}).get("results", [])
                     if v.get("type") == "Trailer" and v.get("site") == "YouTube"), None)

    return (
        f"🎬 *{title}*" + (f" _({orig})_" if orig and orig != title else "")
        + (f" ({year})" if year else "") + "\n\n"
        + (f"⭐ *{rating:.1f}/10*  ({fmt_num(votes)} ovoz)\n" if rating else "")
        + (f"🎭 {genres}\n" if genres else "")
        + (f"⏱ {runtime} min\n" if runtime else "")
        + (f"🎬 Rejissyor: {dirs}\n" if dirs else "")
        + (f"👥 {cast}\n" if cast else "")
        + (f"\n📖 {overview}" if overview else "")
        + (f"\n\n🎞 [Treyler izlash](https://youtu.be/{trailer})" if trailer else "")
    )

async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("🎬 `/movie <kino nomi>`", parse_mode=ParseMode.MARKDOWN)
        return
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ `TMDB_API_KEY` sozlanmagan.", parse_mode=ParseMode.MARKDOWN)
        return
    q = " ".join(context.args)
    await typing(update)
    msg = await update.message.reply_text(f"🔍 *{q}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)
    results = await tmdb_search(q)
    if not results:
        await msg.edit_text("❌ Topilmadi."); return

    if len(results) == 1:
        await _send_movie(update.message, msg, results[0]); return

    text = f"🎬 *{q}* natijalari:\n\n"
    kb = []
    for r in results[:6]:
        t  = r.get("title") or r.get("name") or "?"
        yr = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        mt = r.get("media_type", "movie")
        rt = r.get("vote_average", 0)
        text += f"• *{t}* ({yr}) ⭐{rt:.1f}\n"
        kb.append([InlineKeyboardButton(
            f"{'🎬' if mt=='movie' else '📺'} {t[:35]} ({yr})",
            callback_data=f"MOV|{r['id']}|{mt}")])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def movie_sel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, mid, mt = query.data.split("|")
    msg = await query.message.reply_text("⏳...")
    d = await tmdb_detail(int(mid), mt)
    if not d:
        await msg.edit_text("❌ Topilmadi."); return
    await _send_movie_detail(query.message, msg, d)

async def _send_movie(message, msg, r: dict):
    d = await tmdb_detail(r["id"], r.get("media_type", "movie")) or r
    await _send_movie_detail(message, msg, d)

async def _send_movie_detail(message, msg, d: dict):
    text   = _movie_text(d)
    poster = d.get("poster_path") or d.get("backdrop_path")
    title  = d.get("title") or d.get("name") or ""
    kb = [[InlineKeyboardButton("🔍 YouTube treyler",
        url=f"https://www.youtube.com/results?search_query={title.replace(' ','+')}+trailer")]]
    if poster:
        img = await tmdb_img(poster)
        if img:
            await msg.delete()
            await message.reply_photo(photo=BytesIO(img), caption=text[:1024],
                parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
            if len(text) > 1024:
                await message.reply_text(text[1024:4000], parse_mode=ParseMode.MARKDOWN)
            return
    await msg.edit_text(text[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ `TMDB_API_KEY` sozlanmagan.", parse_mode=ParseMode.MARKDOWN)
        return
    await typing(update)
    msg = await update.message.reply_text("📊 Trend kinolar yuklanmoqda...")
    items = await tmdb_trending()
    if not items:
        await msg.edit_text("❌ Topilmadi."); return
    text = "📊 *Bu haftaning eng zo'r kinolari:*\n\n"
    kb = []
    for i, r in enumerate(items, 1):
        t  = r.get("title") or r.get("name") or "?"
        yr = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        mt = r.get("media_type", "movie")
        rt = r.get("vote_average", 0)
        icon = "🎬" if mt == "movie" else "📺"
        text += f"{i}. {icon} *{t}* ({yr}) ⭐{rt:.1f}\n"
        kb.append([InlineKeyboardButton(f"{icon} {t[:38]} ({yr})",
            callback_data=f"MOV|{r['id']}|{mt}")])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ─── Rasmdan kino topish ──────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid): return

    photo = update.message.photo[-1]
    msg = await update.message.reply_text(
        "🔍 *Rasm tahlil qilinmoqda...*\n_(Yandex Reverse Image Search)_",
        parse_mode=ParseMode.MARKDOWN)

    try:
        tg_file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            await tg_file.download_to_drive(f.name); img_path = f.name
        img_bytes = Image.open(img_path).convert("RGB")
        buf = BytesIO()
        img_bytes.save(buf, "JPEG", quality=85)
        img_data = buf.getvalue()
        os.unlink(img_path)

        keyword = await yandex_reverse(img_data)
        if not keyword:
            await msg.edit_text("🔍 *Google Lens sinab ko'rilmoqda...*", parse_mode=ParseMode.MARKDOWN)
            keyword = await google_lens(img_data)

        if not keyword:
            await msg.edit_text(
                "❌ Rasmdan topib bo'lmadi.\n\n"
                "💡 Yaxshiroq kino posteri yuboring\n"
                "yoki: `/movie <kino nomi>`", parse_mode=ParseMode.MARKDOWN)
            return

        await msg.edit_text(f"✅ *{keyword}* — TMDB da qidirilmoqda...",
            parse_mode=ParseMode.MARKDOWN)

        if not TMDB_API_KEY:
            await msg.edit_text(
                f"🔍 Topildi: *{keyword}*\n\n❌ TMDB kerak: `/movie {keyword}`",
                parse_mode=ParseMode.MARKDOWN)
            return

        results = await tmdb_search(keyword)
        if not results:
            results = await tmdb_search(" ".join(keyword.split()[:3]))

        if not results:
            await msg.edit_text(
                f"🔍 Rasm: *{keyword}*\n\n❌ TMDB da topilmadi.\n`/movie {keyword}`",
                parse_mode=ParseMode.MARKDOWN)
            return

        record(uid, "movies")

        if len(results) == 1:
            await _send_movie(update.message, msg, results[0])
        else:
            text = f"🔍 Rasm: *{keyword}*\n\nQaysi kinoni tanlaysiz?\n\n"
            kb = []
            for r in results[:5]:
                t  = r.get("title") or r.get("name") or "?"
                yr = (r.get("release_date") or r.get("first_air_date") or "")[:4]
                mt = r.get("media_type", "movie")
                rt = r.get("vote_average", 0)
                text += f"• *{t}* ({yr}) ⭐{rt:.1f}\n"
                kb.append([InlineKeyboardButton(
                    f"{'🎬' if mt=='movie' else '📺'} {t[:35]} ({yr})",
                    callback_data=f"MOV|{r['id']}|{mt}")])
            await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")

# ═══════════════════════════════════════════════════════════
# AUDIO EFFECTS
# ═══════════════════════════════════════════════════════════

async def cmd_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    context.user_data["effect_waiting"] = True
    context.user_data["effect_id"] = None
    kb = []
    row = []
    for eid, ename in AUDIO_EFFECTS.items():
        row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
        if len(row) == 2: kb.append(row); row = []
    if row: kb.append(row)
    await update.message.reply_text(
        "🎛 *Effektni tanlang, keyin audio yuboring:*",
        parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

async def eff_sel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, eid = query.data.split("|")
    context.user_data["effect_waiting"] = True
    context.user_data["effect_id"] = eid
    await query.message.reply_text(
        f"✅ *{AUDIO_EFFECTS[eid]}* tanlandi.\n\n🎵 Audio faylni yuboring:",
        parse_mode=ParseMode.MARKDOWN)

async def _do_effect(update: Update, context: ContextTypes.DEFAULT_TYPE, af) -> bool:
    if not context.user_data.get("effect_waiting"): return False
    eid = context.user_data.get("effect_id")
    if not eid:
        kb = []
        row = []
        for e, n in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(n, callback_data=f"EFF|{e}"))
            if len(row) == 2: kb.append(row); row = []
        if row: kb.append(row)
        await update.message.reply_text("🎛 Avval effektni tanlang:",
            reply_markup=InlineKeyboardMarkup(kb))
        return True

    ename = AUDIO_EFFECTS[eid]
    msg = await update.message.reply_text(f"⏳ *{ename}* qo'shilmoqda...", parse_mode=ParseMode.MARKDOWN)
    uid = update.effective_user.id
    try:
        tgf = await af.get_file()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            await tgf.download_to_drive(f.name); inp = f.name

        def proc():
            a = AudioSegment.from_file(inp)
            out_a = fx(a, eid)
            out = inp.replace(".mp3", f"_{eid}.mp3")
            out_a.export(out, format="mp3", bitrate="320k")
            data = open(out, "rb").read()
            os.unlink(inp); os.unlink(out)
            return data

        data = await asyncio.get_event_loop().run_in_executor(None, proc)
        await update.message.reply_audio(audio=BytesIO(data), title=ename,
            caption=f"✅ *{ename}* effekti qo'shildi!", parse_mode=ParseMode.MARKDOWN)
        await msg.delete()
        record(uid, "effects")
        context.user_data["effect_waiting"] = False
        context.user_data["effect_id"] = None
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")
    return True

# ═══════════════════════════════════════════════════════════
# CIRCLE VIDEO
# ═══════════════════════════════════════════════════════════

async def cmd_circle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id): return
    context.user_data["circle_waiting"] = True
    await update.message.reply_text("⭕ *Aylana Video*\n\nVideo faylni yuboring:",
        parse_mode=ParseMode.MARKDOWN)

async def _do_circle(update: Update, context: ContextTypes.DEFAULT_TYPE, vf) -> bool:
    if not context.user_data.get("circle_waiting"): return False
    msg = await update.message.reply_text("⏳ Aylana video tayyorlanmoqda...")
    uid = update.effective_user.id
    try:
        tgf = await vf.get_file()
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            await tgf.download_to_drive(f.name); inp = f.name

        final = await asyncio.get_event_loop().run_in_executor(None, make_circle, inp)
        if final and os.path.exists(final):
            dur = getattr(vf, "duration", 0) or 0
            await update.message.reply_video_note(
                video_note=BytesIO(open(final, "rb").read()), duration=dur, length=512)
            os.unlink(final)
        else:
            await msg.edit_text("❌ Aylana video yaratib bo'lmadi. ffmpeg o'rnatilganmi?")
            return True

        os.unlink(inp)
        await msg.delete()
        record(uid, "dl")
        context.user_data["circle_waiting"] = False
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")
    return True

# ═══════════════════════════════════════════════════════════
# ★ ADMIN PANEL — To'liq kuchli
# ═══════════════════════════════════════════════════════════

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Siz admin emassiz.")
        return

    total = len(USER_INFO)
    banned = sum(1 for u in USER_INFO.values() if u.get("banned"))
    total_dl = sum(v["dl"] for v in STATS.values())
    total_music = sum(v["music"] for v in STATS.values())
    total_movies = sum(v["movies"] for v in STATS.values())
    total_fx = sum(v["effects"] for v in STATS.values())

    text = (
        f"👑 *ADMIN PANEL*\n\n"
        f"━━━━━ 📊 Statistika ━━━━━\n"
        f"👥 Jami foydalanuvchilar: `{total}`\n"
        f"🚫 Bloklangan: `{banned}`\n"
        f"📥 Jami yuklab olish: `{total_dl}`\n"
        f"🎵 Jami musiqa: `{total_music}`\n"
        f"🎬 Jami kino qidirish: `{total_movies}`\n"
        f"🎛 Jami effektlar: `{total_fx}`\n"
    )
    kb = [
        [InlineKeyboardButton("👥 Foydalanuvchilar ro'yxati", callback_data="ADM|users|1")],
        [InlineKeyboardButton("📢 Hammaga xabar yuborish",    callback_data="ADM|broadcast|0")],
        [InlineKeyboardButton("🔍 Foydalanuvchini topish",    callback_data="ADM|search|0")],
        [InlineKeyboardButton("🚫 Ban ro'yxati",              callback_data="ADM|banlist|0")],
        [InlineKeyboardButton("📊 Top foydalanuvchilar",      callback_data="ADM|top|0")],
        [InlineKeyboardButton("🗑 Statistikani tozalash",     callback_data="ADM|clearstats|0")],
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        await query.message.edit_text("❌ Ruxsat yo'q.")
        return

    parts = query.data.split("|")
    action = parts[1]
    back_kb = [[InlineKeyboardButton("⬅️ Admin panel", callback_data="ADM|back|0")]]

    if action == "back":
        # Admin panelni qayta ko'rsatish
        total = len(USER_INFO)
        banned = sum(1 for u in USER_INFO.values() if u.get("banned"))
        total_dl = sum(v["dl"] for v in STATS.values())
        total_music = sum(v["music"] for v in STATS.values())
        text = (
            f"👑 *ADMIN PANEL*\n\n"
            f"👥 Foydalanuvchilar: `{total}` (🚫 ban: `{banned}`)\n"
            f"📥 Yuklab olish: `{total_dl}`\n"
            f"🎵 Musiqa: `{total_music}`\n"
        )
        kb = [
            [InlineKeyboardButton("👥 Foydalanuvchilar ro'yxati", callback_data="ADM|users|1")],
            [InlineKeyboardButton("📢 Hammaga xabar yuborish",    callback_data="ADM|broadcast|0")],
            [InlineKeyboardButton("🔍 Foydalanuvchini topish",    callback_data="ADM|search|0")],
            [InlineKeyboardButton("🚫 Ban ro'yxati",              callback_data="ADM|banlist|0")],
            [InlineKeyboardButton("📊 Top foydalanuvchilar",      callback_data="ADM|top|0")],
        ]
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

    elif action == "users":
        page = int(parts[2])
        per_page = 10
        users = list(USER_INFO.values())
        total_pages = max(1, (len(users) + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        chunk = users[(page-1)*per_page : page*per_page]

        text = f"👥 *Foydalanuvchilar* ({len(users)} ta) — {page}/{total_pages} sahifa\n\n"
        kb = []
        for u in chunk:
            bid = u["id"]
            ban_icon = "🚫 " if u.get("banned") else ""
            uname = f"@{u['username']}" if u.get("username") else ""
            text += f"{ban_icon}`{bid}` — {u['name']} {uname}\n"
            kb.append([
                InlineKeyboardButton(f"{'✅ Ban ochish' if u.get('banned') else '🚫 Ban'}", callback_data=f"ADM|toggleban|{bid}"),
                InlineKeyboardButton(f"📊 {u['name'][:15]}", callback_data=f"ADM|userstat|{bid}"),
            ])

        nav = []
        if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"ADM|users|{page-1}"))
        if page < total_pages: nav.append(InlineKeyboardButton("➡️", callback_data=f"ADM|users|{page+1}"))
        if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADM|back|0")])
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

    elif action == "toggleban":
        target_id = int(parts[2])
        if target_id in ADMIN_IDS:
            await query.answer("❌ Adminni banlash mumkin emas!", show_alert=True)
            return
        if target_id not in USER_INFO:
            await query.answer("❌ Foydalanuvchi topilmadi.", show_alert=True)
            return
        USER_INFO[target_id]["banned"] = not USER_INFO[target_id].get("banned", False)
        status = "🚫 Bloklandi" if USER_INFO[target_id]["banned"] else "✅ Blokdan chiqarildi"
        uname = USER_INFO[target_id].get("name", str(target_id))
        await query.answer(f"{status}: {uname}", show_alert=True)
        # Sahifani yangilash
        await admin_callback(update, context)  # users ni qayta ko'rsatish
        # Back to users page 1
        parts[2] = "1"
        query.data = "ADM|users|1"
        await admin_callback(update, context)

    elif action == "userstat":
        target_id = int(parts[2])
        u = USER_INFO.get(target_id, {})
        s = STATS.get(target_id, {})
        total_act = s.get("dl",0) + s.get("music",0) + s.get("movies",0) + s.get("effects",0)
        ban_status = "🚫 Bloklangan" if u.get("banned") else "✅ Faol"
        uname = f"@{u['username']}" if u.get("username") else "—"
        text = (
            f"👤 *Foydalanuvchi ma'lumoti*\n\n"
            f"🆔 ID: `{target_id}`\n"
            f"👤 Ism: {u.get('name','?')}\n"
            f"📛 Username: {uname}\n"
            f"📅 Qo'shilgan: {s.get('joined','?')}\n"
            f"🔒 Status: {ban_status}\n\n"
            f"📊 *Faoliyat:*\n"
            f"📥 Yuklab olish: `{s.get('dl',0)}`\n"
            f"🎵 Musiqa: `{s.get('music',0)}`\n"
            f"🎬 Kino: `{s.get('movies',0)}`\n"
            f"🎛 Effektlar: `{s.get('effects',0)}`\n"
            f"📈 Jami: `{total_act}`"
        )
        ban_txt = "✅ Blokdan chiqarish" if u.get("banned") else "🚫 Bloklash"
        kb = [
            [InlineKeyboardButton(ban_txt, callback_data=f"ADM|toggleban|{target_id}")],
            [InlineKeyboardButton("📢 Xabar yuborish", callback_data=f"ADM|msguser|{target_id}")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="ADM|users|1")],
        ]
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

    elif action == "msguser":
        target_id = int(parts[2])
        context.user_data["msg_to_user"] = target_id
        await query.message.edit_text(
            f"✉️ `{target_id}` ga xabar yozing:\n_(keyingi xabaringiz o'sha foydalanuvchiga yuboriladi)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_kb))

    elif action == "broadcast":
        context.user_data["broadcast_mode"] = True
        count = len([u for u in USER_INFO.values() if not u.get("banned")])
        await query.message.edit_text(
            f"📢 *Hammaga xabar yuborish*\n\n"
            f"👥 Qabul qiluvchilar: *{count}* ta faol foydalanuvchi\n\n"
            f"Xabaringizni yozing (matn, rasm, video — barchasi qabul qilinadi):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_kb))

    elif action == "search":
        context.user_data["admin_search"] = True
        await query.message.edit_text(
            "🔍 Foydalanuvchi ID yoki username ni yozing:",
            reply_markup=InlineKeyboardMarkup(back_kb))

    elif action == "banlist":
        banned_users = [u for u in USER_INFO.values() if u.get("banned")]
        if not banned_users:
            await query.message.edit_text("✅ Bloklangan foydalanuvchi yo'q.",
                reply_markup=InlineKeyboardMarkup(back_kb))
            return
        text = f"🚫 *Ban ro'yxati* ({len(banned_users)} ta):\n\n"
        kb = []
        for u in banned_users[:20]:
            text += f"• `{u['id']}` — {u['name']}\n"
            kb.append([InlineKeyboardButton(
                f"✅ {u['name'][:20]} unban",
                callback_data=f"ADM|toggleban|{u['id']}")])
        kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADM|back|0")])
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

    elif action == "top":
        top = sorted(STATS.items(),
            key=lambda x: x[1]["dl"] + x[1]["music"] + x[1]["movies"],
            reverse=True)[:10]
        text = "📊 *Top 10 faol foydalanuvchilar:*\n\n"
        for i, (uid_, s) in enumerate(top, 1):
            name = USER_INFO.get(uid_, {}).get("name", str(uid_))
            total_act = s["dl"] + s["music"] + s["movies"]
            text += f"{i}. *{name}* — `{total_act}` ta harakatlar\n"
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back_kb))

    elif action == "clearstats":
        kb = [
            [InlineKeyboardButton("✅ Ha, tozala", callback_data="ADM|clearconfirm|0"),
             InlineKeyboardButton("❌ Bekor", callback_data="ADM|back|0")],
        ]
        await query.message.edit_text(
            "⚠️ *Haqiqatan ham barcha statistikani tozalashni xohlaysizmi?*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

    elif action == "clearconfirm":
        for uid_ in list(STATS.keys()):
            STATS[uid_] = {"dl": 0, "music": 0, "movies": 0, "effects": 0,
                           "joined": STATS[uid_].get("joined", "")}
        await query.message.edit_text("✅ Barcha statistika tozalandi.",
            reply_markup=InlineKeyboardMarkup(back_kb))

# ═══════════════════════════════════════════════════════════
# ADMIN — Broadcast va User message handling
# ═══════════════════════════════════════════════════════════

async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if not is_admin(uid): return False

    # Broadcast mode
    if context.user_data.get("broadcast_mode"):
        context.user_data["broadcast_mode"] = False
        targets = [u_id for u_id, u in USER_INFO.items()
                   if not u.get("banned") and u_id != uid]
        msg = await update.message.reply_text(f"📢 Yuborilmoqda {len(targets)} ta foydalanuvchiga...")
        ok = fail = 0
        for t_id in targets:
            try:
                await update.message.copy_to(t_id)
                ok += 1
                await asyncio.sleep(0.05)
            except:
                fail += 1
        await msg.edit_text(
            f"✅ Yuborildi: *{ok}* ta\n❌ Xato: *{fail}* ta",
            parse_mode=ParseMode.MARKDOWN)
        return True

    # Individual user message
    if context.user_data.get("msg_to_user"):
        t_id = context.user_data.pop("msg_to_user")
        try:
            await update.message.copy_to(t_id)
            await update.message.reply_text(f"✅ `{t_id}` ga xabar yuborildi.",
                parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await update.message.reply_text(f"❌ Yuborib bo'lmadi: {e}")
        return True

    # Admin search
    if context.user_data.get("admin_search"):
        context.user_data["admin_search"] = False
        q = (update.message.text or "").strip()
        found = []
        for u_id, u in USER_INFO.items():
            if str(u_id) == q or u.get("username","").lower() == q.lstrip("@").lower():
                found.append((u_id, u))
        if not found:
            await update.message.reply_text("❌ Topilmadi.")
        else:
            for u_id, u in found[:3]:
                s = STATS.get(u_id, {})
                ban_txt = "🚫 Bloklangan" if u.get("banned") else "✅ Faol"
                text = (
                    f"🔍 *Topildi*\n\n"
                    f"🆔 `{u_id}`\n"
                    f"👤 {u['name']}\n"
                    f"📛 @{u.get('username','—')}\n"
                    f"🔒 {ban_txt}\n"
                    f"📅 {s.get('joined','?')}\n"
                    f"📈 Jami: `{s.get('dl',0)+s.get('music',0)+s.get('movies',0)}`"
                )
                kb = [[
                    InlineKeyboardButton(
                        "✅ Blokdan chiqarish" if u.get("banned") else "🚫 Bloklash",
                        callback_data=f"ADM|toggleban|{u_id}"),
                    InlineKeyboardButton("📢 Xabar", callback_data=f"ADM|msguser|{u_id}"),
                ]]
                await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(kb))
        return True

    return False

# ═══════════════════════════════════════════════════════════
# UNIVERSAL MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user = update.effective_user
    register_user(user)
    uid = user.id

    if is_banned(uid):
        await update.message.reply_text("🚫 Siz bloklangansiz.")
        return

    # Admin actions (broadcast, search, msg_to_user)
    if is_admin(uid):
        handled = await admin_text_handler(update, context)
        if handled: return

    # Photo → kino topish
    if update.message.photo:
        await handle_photo(update, context)
        return

    # Audio → effekt
    af = update.message.audio or update.message.voice
    if not af and update.message.document:
        mt = update.message.document.mime_type or ""
        if "audio" in mt: af = update.message.document
    if af:
        handled = await _do_effect(update, context, af)
        if handled: return

    # Video → aylana
    vf = update.message.video
    if not vf and update.message.document:
        mt = update.message.document.mime_type or ""
        if "video" in mt: vf = update.message.document
    if vf:
        handled = await _do_circle(update, context, vf)
        if handled: return

    # URL → yuklab olish
    text = (update.message.text or "").strip()
    if is_url(text):
        await handle_url(update, context)
        return

    # Boshqa matn
    if text and not text.startswith("/"):
        await update.message.reply_text(
            "💡 Nima yuborishni bilmadim.\n\n"
            "📥 Video havolasini yuboring\n"
            "📸 Kino rasmini yuboring\n"
            "❓ /help — yordam")

async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (TimedOut, NetworkError)): return
    print(f"[ERR] {context.error}")

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ BOT_TOKEN sozlanmagan! .env faylini to'ldiring.")
        return

    print("🤖 Pro Media Bot v3.0 ishga tushmoqda...")
    if ADMIN_IDS:
        print(f"👑 Adminlar: {ADMIN_IDS}")
    if not TMDB_API_KEY:
        print("⚠️  TMDB_API_KEY yo'q — kino ishlamaydi")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     lambda u, c: u.message.reply_text(
        "❓ /music • /lyrics • /movie • /trending • /effect • /circle • /stats • /admin",
        parse_mode=ParseMode.MARKDOWN)))
    app.add_handler(CommandHandler("music",    cmd_music))
    app.add_handler(CommandHandler("lyrics",   cmd_lyrics))
    app.add_handler(CommandHandler("movie",    cmd_movie))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("effect",   cmd_effect))
    app.add_handler(CommandHandler("circle",   cmd_circle))
    app.add_handler(CommandHandler("stats",    lambda u, c: menu_callback(
        type("FakeQuery", (), {"data": "MENU|stats", "from_user": u.effective_user,
        "message": u.message, "answer": asyncio.coroutine(lambda: None)})(), c)
    ))
    app.add_handler(CommandHandler("admin",    cmd_admin))

    app.add_handler(CallbackQueryHandler(platform_callback, pattern=r"^PLT\|"))
    app.add_handler(CallbackQueryHandler(menu_callback,     pattern=r"^MENU\|"))
    app.add_handler(CallbackQueryHandler(back_callback,     pattern=r"^BACK\|"))
    app.add_handler(CallbackQueryHandler(dl_callback,       pattern=r"^DL\|"))
    app.add_handler(CallbackQueryHandler(music_dl_callback, pattern=r"^MUSIC\|"))
    app.add_handler(CallbackQueryHandler(movie_sel_callback,pattern=r"^MOV\|"))
    app.add_handler(CallbackQueryHandler(eff_sel_callback,  pattern=r"^EFF\|"))
    app.add_handler(CallbackQueryHandler(admin_callback,    pattern=r"^ADM\|"))

    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.AUDIO | filters.VOICE |
        filters.VIDEO | filters.Document.ALL,
        handle_message))

    app.add_error_handler(error_handler)

    print("✅ Bot tayyor! Polling boshlandi...\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
