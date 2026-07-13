import os
import re
import time
import asyncio
import tempfile
import subprocess
import json
import math
from io import BytesIO
from datetime import datetime
from collections import defaultdict

import httpx
import yt_dlp
from PIL import Image
from pydub import AudioSegment
from pydub.effects import normalize

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TimedOut, NetworkError, BadRequest

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
    SPOTIPY_OK = True
except ImportError:
    SPOTIPY_OK = False

try:
    from youtubesearchpython import VideosSearch
    YTSEARCH_OK = True
except ImportError:
    YTSEARCH_OK = False

BOT_TOKEN             = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TMDB_API_KEY          = os.getenv("TMDB_API_KEY", "")
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
ADMIN_IDS_RAW         = os.getenv("ADMIN_IDS", "")

ADMIN_IDS: set = set()
for _a in ADMIN_IDS_RAW.split(","):
    if _a.strip().isdigit():
        ADMIN_IDS.add(int(_a.strip()))

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"

STATS: dict    = defaultdict(lambda: {"dl": 0, "music": 0, "movies": 0, "effects": 0, "joined": ""})
USER_INFO: dict = {}

AUDIO_EFFECTS = {
    "bass":   "🔊 Bass Boost",
    "reverb": "🎵 Reverb",
    "echo":   "🌊 Echo",
    "vocal":  "🎤 Vocal Boost",
    "slow":   "🐢 Slow (0.75x)",
    "fast":   "🐇 Fast (1.25x)",
    "rock":   "🎸 Rock EQ",
    "lofi":   "🎹 Lo-Fi",
}

PLATFORMS = [
    ("▶️ YouTube",    "youtube"),
    ("📸 Instagram",  "instagram"),
    ("🎵 TikTok",     "tiktok"),
    ("📌 Pinterest",  "pinterest"),
    ("🐦 Twitter/X",  "twitter"),
    ("👥 Facebook",   "facebook"),
    ("🎬 Vimeo",      "vimeo"),
    ("🎮 Twitch",     "twitch"),
    ("🤖 Reddit",     "reddit"),
    ("📹 Dailymotion","dailymotion"),
]


def is_url(t: str) -> bool:
    return bool(re.match(r"https?://", t.strip()))


def fmt_dur(sec) -> str:
    if not sec:
        return "0:00"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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


def ffmpeg(*args) -> tuple:
    cmd = ["ffmpeg", "-y"] + list(args)
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0, r.stderr.decode(errors="ignore")


def make_circle_video(inp: str) -> str | None:
    out = inp + "_circle.mp4"
    ok, err = ffmpeg(
        "-i", inp,
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,"
               "crop=640:640,"
               "scale=384:384",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "28",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", "60",
        out
    )
    if ok and os.path.exists(out) and os.path.getsize(out) > 1000:
        return out
    return None


def apply_effect(audio: AudioSegment, eid: str) -> AudioSegment:
    if eid == "bass":
        low = audio.low_pass_filter(200)
        mid = audio.high_pass_filter(200).low_pass_filter(3000)
        high = audio.high_pass_filter(3000)
        audio = low + 8 + mid + high - 2

    elif eid == "reverb":
        delays = [30, 60, 120, 240]
        vols   = [-6, -10, -15, -20]
        result = audio
        for d, v in zip(delays, vols):
            echo_layer = AudioSegment.silent(duration=d) + (audio + v)
            if len(echo_layer) < len(result):
                echo_layer = echo_layer + AudioSegment.silent(duration=len(result) - len(echo_layer))
            result = result.overlay(echo_layer)
        audio = result

    elif eid == "echo":
        echo1 = AudioSegment.silent(duration=300) + (audio - 8)
        echo2 = AudioSegment.silent(duration=600) + (audio - 14)
        if len(echo1) < len(audio):
            echo1 += AudioSegment.silent(duration=len(audio) - len(echo1))
        if len(echo2) < len(audio):
            echo2 += AudioSegment.silent(duration=len(audio) - len(echo2))
        audio = audio.overlay(echo1).overlay(echo2)

    elif eid == "vocal":
        audio = audio.high_pass_filter(300).low_pass_filter(8000) + 5

    elif eid == "slow":
        new_rate = int(audio.frame_rate * 0.75)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": new_rate}).set_frame_rate(audio.frame_rate)

    elif eid == "fast":
        new_rate = int(audio.frame_rate * 1.3)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": new_rate}).set_frame_rate(audio.frame_rate)

    elif eid == "rock":
        low  = audio.low_pass_filter(150) + 4
        mid  = audio.high_pass_filter(150).low_pass_filter(2000)
        high = audio.high_pass_filter(2000) + 3
        audio = low.overlay(mid).overlay(high)

    elif eid == "lofi":
        audio = audio.low_pass_filter(3500)
        new_rate = int(audio.frame_rate * 0.98)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": new_rate}).set_frame_rate(audio.frame_rate)
        audio = audio - 2

    return normalize(audio)


def _dl_video_sync(url: str, audio_only: bool, quality: str = "best") -> dict:
    is_tiktok = "tiktok.com" in url.lower()
    is_twitter = "twitter.com" in url.lower() or "x.com" in url.lower()

    if audio_only:
        fmt = "bestaudio/best"
    elif quality == "720":
        fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best"
    elif quality == "480":
        fmt = "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]/best"
    else:
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": fmt,
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "writethumbnail": True,
            "postprocessors": [],
        }

        if is_tiktok:
            opts["extractor_args"] = {"tiktok": {"embed_url": ["1"]}}

        if audio_only:
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })

        with yt_dlp.YoutubeDL(opts) as ydl_inst:
            info = ydl_inst.extract_info(url, download=True)

        ext = "mp3" if audio_only else "mp4"
        fdata = tdata = None

        for fn in os.listdir(tmp):
            fp = os.path.join(tmp, fn)
            if fn.endswith(f".{ext}") and fdata is None:
                with open(fp, "rb") as f:
                    fdata = f.read()
            elif fn.endswith((".jpg", ".jpeg", ".png", ".webp")) and tdata is None:
                with open(fp, "rb") as f:
                    tdata = f.read()

        return {
            "data":     fdata,
            "thumb":    tdata,
            "title":    info.get("title", "Video"),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration") or 0,
            "ext":      ext,
        }


async def dl_video(url: str, audio_only: bool, quality: str = "best") -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _dl_video_sync, url, audio_only, quality)


async def tmdb_req(path: str, params: dict | None = None) -> dict | None:
    if not TMDB_API_KEY:
        return None
    p = {"api_key": TMDB_API_KEY, "language": "en-US", **(params or {})}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{TMDB_BASE}{path}", params=p)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


async def tmdb_search(q: str) -> list:
    d = await tmdb_req("/search/multi", {"query": q, "include_adult": False})
    return (d or {}).get("results", [])[:6]


async def tmdb_detail(mid: int, mtype: str) -> dict | None:
    path = f"/{'tv' if mtype == 'tv' else 'movie'}/{mid}"
    return await tmdb_req(path, {"append_to_response": "credits,videos"})


async def tmdb_fetch_poster(poster: str) -> bytes | None:
    if not poster:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(TMDB_IMG + poster)
            return r.content if r.status_code == 200 else None
    except Exception:
        return None


async def tmdb_trending() -> list:
    d = await tmdb_req("/trending/all/week")
    return (d or {}).get("results", [])[:8]


async def yandex_reverse_search(img_bytes: bytes) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as c:
            r = await c.post(
                "https://yandex.com/images/search",
                params={"rpt": "imageview", "format": "json", "request": '{"blocks":[{"block":"cbir-uploader__get-cbir-id"}]}'},
                files={"upfile": ("photo.jpg", img_bytes, "image/jpeg")},
            )
            cbir_url = None
            loc = r.headers.get("location", "")
            if loc:
                cbir_url = "https://yandex.com" + loc if loc.startswith("/") else loc
            else:
                m = re.search(r'(?:url|href)=["\']([^"\']*cbir[^"\']*)["\']', r.text)
                if m:
                    cbir_url = m.group(1)
                    if cbir_url.startswith("/"):
                        cbir_url = "https://yandex.com" + cbir_url

            if not cbir_url:
                return None

            r2 = await c.get(cbir_url)
            titles = re.findall(r'"snippet":\s*"([^"]{4,80})"', r2.text)
            if not titles:
                titles = re.findall(r'<title[^>]*>([^<]{4,100})</title>', r2.text)
            if titles:
                clean = re.sub(r'\s*[—\-|]\s*Yandex.*', '', titles[0]).strip()
                return clean if len(clean) > 3 else None
    except Exception:
        pass
    return None


def _yt_search_sync(query: str, limit: int = 5) -> list:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": True,
        "default_search": f"ytsearch{limit}",
        "format": "bestaudio/best",
    }
    results = []
    with yt_dlp.YoutubeDL(opts) as ydl_inst:
        try:
            info = ydl_inst.extract_info(f"ytsearch{limit}:{query}", download=False)
            entries = info.get("entries", [])
            for e in entries:
                results.append({
                    "title":    e.get("title", ""),
                    "url":      e.get("url") or f"https://www.youtube.com/watch?v={e.get('id','')}",
                    "duration": e.get("duration") or 0,
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "thumb":    e.get("thumbnail") or "",
                })
        except Exception:
            pass
    return results


async def yt_search(query: str, limit: int = 5) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _yt_search_sync, query, limit)


def _spotify_search_sync(query: str) -> list:
    if not (SPOTIPY_OK and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return []
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        ))
        r = sp.search(q=query, type="track", limit=5)
        results = []
        for item in r["tracks"]["items"]:
            artists = ", ".join(a["name"] for a in item["artists"])
            thumb = ""
            if item["album"]["images"]:
                thumb = item["album"]["images"][0]["url"]
            results.append({
                "title":    item["name"],
                "artist":   artists,
                "album":    item["album"]["name"],
                "duration": item["duration_ms"] // 1000,
                "thumb":    thumb,
                "preview":  item.get("preview_url") or "",
                "spotify_url": item["external_urls"].get("spotify", ""),
            })
        return results
    except Exception:
        return []


async def spotify_search(query: str) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _spotify_search_sync, query)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    if is_banned(user.id):
        await update.message.reply_text("🚫 Siz bloklangansiz.")
        return

    text = (
        f"👋 *Salom, {user.first_name}!*\n\n"
        "🤖 *Pro Media Bot* — platformalardan video yuklab oling,\n"
        "musiqa toping, kino qidiring va ko'p narsalar!\n\n"
        "📥 Havola yuboring — bot o'zi aniqlaydi\n"
        "🔽 Yoki quyidan tanlang:"
    )

    plat_kb = []
    row = []
    for name, _ in PLATFORMS:
        row.append(InlineKeyboardButton(name, callback_data=f"PLT|{name}"))
        if len(row) == 2:
            plat_kb.append(row)
            row = []
    if row:
        plat_kb.append(row)

    plat_kb.append([
        InlineKeyboardButton("🎵 Musiqa qidirish", callback_data="MENU|music"),
        InlineKeyboardButton("🎬 Kino qidirish",   callback_data="MENU|movie"),
    ])
    plat_kb.append([
        InlineKeyboardButton("🎛 Audio Effektlar",  callback_data="MENU|effect"),
        InlineKeyboardButton("⭕ Dumaloq Video",     callback_data="MENU|circle"),
    ])
    plat_kb.append([
        InlineKeyboardButton("📊 Statistikam",      callback_data="MENU|stats"),
        InlineKeyboardButton("❓ Yordam",             callback_data="MENU|help"),
    ])
    if is_admin(user.id):
        plat_kb.append([InlineKeyboardButton("👑 Admin Panel", callback_data="ADMIN|main")])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(plat_kb),
    )


async def platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pname = query.data.split("|", 1)[1]

    examples = {
        "youtube":    "https://youtube.com/watch?v=...\nhttps://youtu.be/...",
        "instagram":  "https://instagram.com/reel/...\nhttps://instagram.com/p/...",
        "tiktok":     "https://tiktok.com/@user/video/...",
        "pinterest":  "https://pin.it/...\nhttps://pinterest.com/pin/...",
        "twitter":    "https://twitter.com/i/status/...\nhttps://x.com/i/status/...",
        "facebook":   "https://fb.watch/...\nhttps://facebook.com/videos/...",
        "vimeo":      "https://vimeo.com/123456789",
        "twitch":     "https://twitch.tv/videos/...",
        "reddit":     "https://reddit.com/r/sub/comments/...",
        "dailymotion":"https://dailymotion.com/video/...",
    }
    ex = examples.get(pname, "havolani yuboring")
    text = (
        f"📥 *{pname.upper()} dan yuklab olish*\n\n"
        f"Havola namunasi:\n`{ex}`\n\n"
        f"Havolani shu chatga yuboring — bot avtomatik aniqlaydi!\n\n"
        f"📌 Mavjud formatlar:\n"
        f"• 🎬 Eng yaxshi sifat\n"
        f"• 📺 720p HD\n"
        f"• 📱 480p\n"
        f"• 🎵 MP3 audio\n"
        f"• ⭕ Dumaloq video"
    )
    kb = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="MENU|back_start")]]
    await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split("|", 1)[1]
    uid = query.from_user.id

    back = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="MENU|back_start")]]

    if action == "back_start":
        await query.message.delete()
        fake = type("FU", (), {"effective_user": query.from_user, "message": query.message})()
        await _send_start(query.message, query.from_user)
        return

    elif action == "help":
        text = (
            "❓ *Qo'llanma*\n\n"
            "📥 *Video yuklab olish:*\n"
            "Havola yuboring → format tanlang → yuklab oling\n"
            "10+ platformadan ishlaydi (suvsiz belgisiz)\n\n"
            "🎵 *Musiqa:*\n"
            "`/music nom` — qo'shiq qidirish\n\n"
            "🎬 *Kino:*\n"
            "`/movie nom` — TMDB dan kino topish\n"
            "`/trending` — haftalik toplar\n"
            "Kino rasmi yuboring → bot aniqlaydi!\n\n"
            "🎛 *Audio effektlar:*\n"
            "`/effect` → audio yuboring → effekt tanlang\n\n"
            "⭕ *Dumaloq video:*\n"
            "`/circle` → video yuboring"
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == "stats":
        s = STATS[uid]
        total = s["dl"] + s["music"] + s["movies"] + s["effects"]
        text = (
            f"📊 *Sizning statistikangiz*\n\n"
            f"📥 Yuklab olish: `{s['dl']}`\n"
            f"🎵 Musiqa: `{s['music']}`\n"
            f"🎬 Kino: `{s['movies']}`\n"
            f"🎛 Effektlar: `{s['effects']}`\n"
            f"━━━━━━━━━\n"
            f"📈 Jami: `{total}`\n"
            f"📅 Ro'yxatdan: `{s['joined'] or 'N/A'}`"
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == "music":
        text = (
            "🎵 *Musiqa qidirish*\n\n"
            "Buyruqdan foydalaning:\n"
            "`/music qo'shiq nomi`\n\n"
            "Misol:\n"
            "`/music Doja Cat Paint The Town Red`\n"
            "`/music Kendrick Lamar HUMBLE`\n\n"
            "Bot YouTube dan topadi, sifatli mp3 yuboradi."
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == "movie":
        text = (
            "🎬 *Kino qidirish*\n\n"
            "• `/movie kino nomi` — nom bilan qidirish\n"
            "• `/trending` — haftalik top 8\n"
            "• Kino posteri rasmini yuboring — bot aniqlaydi!"
        )
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == "effect":
        context.user_data["effect_waiting"] = True
        context.user_data["effect_id"] = None
        eff_kb = []
        row = []
        for eid, ename in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
            if len(row) == 2:
                eff_kb.append(row)
                row = []
        if row:
            eff_kb.append(row)
        eff_kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="MENU|back_start")])
        await query.message.edit_text(
            "🎛 *Audio Effektlar*\n\nEffektni tanlang, so'ng audio faylni yuboring:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(eff_kb),
        )

    elif action == "circle":
        context.user_data["circle_waiting"] = True
        await query.message.edit_text(
            "⭕ *Dumaloq Video*\n\nVideo faylni yuboring (max 60 soniya):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back),
        )


async def _send_start(message, user):
    text = (
        f"👋 *Salom, {user.first_name}!*\n\n"
        "📥 Havola yuboring — bot o'zi aniqlaydi\n"
        "🔽 Yoki quyidan tanlang:"
    )
    plat_kb = []
    row = []
    for name, _ in PLATFORMS:
        row.append(InlineKeyboardButton(name, callback_data=f"PLT|{name}"))
        if len(row) == 2:
            plat_kb.append(row)
            row = []
    if row:
        plat_kb.append(row)
    plat_kb.append([
        InlineKeyboardButton("🎵 Musiqa qidirish", callback_data="MENU|music"),
        InlineKeyboardButton("🎬 Kino qidirish",   callback_data="MENU|movie"),
    ])
    plat_kb.append([
        InlineKeyboardButton("🎛 Audio Effektlar",  callback_data="MENU|effect"),
        InlineKeyboardButton("⭕ Dumaloq Video",     callback_data="MENU|circle"),
    ])
    plat_kb.append([
        InlineKeyboardButton("📊 Statistikam",      callback_data="MENU|stats"),
        InlineKeyboardButton("❓ Yordam",             callback_data="MENU|help"),
    ])
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                 reply_markup=InlineKeyboardMarkup(plat_kb))
    except Exception:
        pass


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    uid = update.effective_user.id
    if is_banned(uid):
        return

    kb = [
        [
            InlineKeyboardButton("🎬 Best", callback_data=f"DL|video|best|{url}"),
            InlineKeyboardButton("📺 720p", callback_data=f"DL|video|720|{url}"),
            InlineKeyboardButton("📱 480p", callback_data=f"DL|video|480|{url}"),
        ],
        [
            InlineKeyboardButton("🎵 MP3",    callback_data=f"DL|audio|best|{url}"),
            InlineKeyboardButton("⭕ Dumaloq", callback_data=f"DL|circle|best|{url}"),
            InlineKeyboardButton("ℹ️ Info",   callback_data=f"DL|info|best|{url}"),
        ],
    ]
    await update.message.reply_text(
        "⬇️ *Qanday yuklab olasiz?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 3)
    if len(parts) < 4:
        return
    _, mode, quality, url = parts
    uid = query.from_user.id

    if is_banned(uid):
        await query.message.reply_text("🚫 Siz bloklangansiz.")
        return

    msg = await query.message.reply_text("⏳ Yuklanmoqda, iltimos kuting...")

    try:
        if mode == "info":
            await _show_video_info(query, url, msg)
            return

        is_audio  = mode == "audio"
        is_circle = mode == "circle"

        result = await dl_video(url, audio_only=(is_audio or is_circle), quality=quality)

        if not result.get("data"):
            await msg.edit_text("❌ Yuklab bo'lmadi. Havola noto'g'ri yoki himoyalangan bo'lishi mumkin.")
            return

        title    = result["title"] or "video"
        uploader = result["uploader"] or ""
        duration = result["duration"] or 0
        caption  = f"🎬 *{title}*"
        if uploader:
            caption += f"\n👤 {uploader}"
        if duration:
            caption += f"\n⏱ {fmt_dur(duration)}"

        thumb_io = BytesIO(result["thumb"]) if result.get("thumb") else None
        data_io  = BytesIO(result["data"])

        save_kb = [[
            InlineKeyboardButton("📤 Ulashish", switch_inline_query=title),
        ]]
        markup = InlineKeyboardMarkup(save_kb)

        if is_audio:
            await query.message.reply_audio(
                audio=data_io,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                thumbnail=thumb_io,
                duration=duration,
                title=title,
                performer=uploader,
                reply_markup=markup,
            )

        elif is_circle:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                tf.write(result["data"])
                inp_path = tf.name
            try:
                circle_path = await asyncio.get_event_loop().run_in_executor(
                    None, make_circle_video, inp_path
                )
                if circle_path and os.path.exists(circle_path):
                    circle_dur = min(duration, 60)
                    with open(circle_path, "rb") as f:
                        await query.message.reply_video_note(
                            video_note=f,
                            duration=circle_dur,
                            length=384,
                        )
                    os.unlink(circle_path)
                else:
                    await msg.edit_text("❌ Dumaloq video yaratib bo'lmadi. ffmpeg o'rnatilganligini tekshiring.")
                    return
            finally:
                if os.path.exists(inp_path):
                    os.unlink(inp_path)

        else:
            await query.message.reply_video(
                video=data_io,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                thumbnail=thumb_io,
                duration=duration,
                supports_streaming=True,
                reply_markup=markup,
            )

        await msg.delete()
        record(uid, "dl")

    except Exception as e:
        err = str(e)[:300]
        await msg.edit_text(f"❌ Xato: {err}")


async def _show_video_info(query, url: str, msg):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    try:
        loop = asyncio.get_event_loop()
        def _info():
            with yt_dlp.YoutubeDL(opts) as ydl_inst:
                return ydl_inst.extract_info(url, download=False)
        info = await loop.run_in_executor(None, _info)
        text = (
            f"ℹ️ *Video Ma'lumoti*\n\n"
            f"📌 *Sarlavha:* {info.get('title','?')}\n"
            f"👤 *Muallif:* {info.get('uploader') or info.get('channel','?')}\n"
            f"⏱ *Davomiylik:* {fmt_dur(info.get('duration',0))}\n"
            f"👁 *Ko'rishlar:* {info.get('view_count') or '?'}\n"
            f"❤️ *Layklar:* {info.get('like_count') or '?'}\n"
            f"📅 *Yuklangan:* {info.get('upload_date','?')}\n"
        )
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"❌ Ma'lumot olib bo'lmadi: {str(e)[:200]}")


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(update.effective_user)
    if is_banned(uid):
        return

    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "🎵 Qo'shiq nomini yozing:\n`/music The Weeknd Blinding Lights`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.message.reply_text(f"🔍 *{query}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)

    spotify_results = await spotify_search(query)
    yt_results      = await yt_search(query, limit=5)

    if not spotify_results and not yt_results:
        await msg.edit_text("❌ Hech narsa topilmadi.")
        return

    kb = []
    shown = {}

    if spotify_results:
        for i, s in enumerate(spotify_results[:3]):
            title   = s["title"]
            artist  = s["artist"]
            dur_str = fmt_dur(s["duration"])
            label   = f"🎵 {title[:22]} — {artist[:15]} ({dur_str})"
            yt_q    = f"{title} {artist} official audio"
            cb_data = f"MUSIC|dl|{yt_q}"
            if len(cb_data) > 62:
                cb_data = f"MUSIC|dl|{yt_q[:55]}"
            kb.append([InlineKeyboardButton(label, callback_data=cb_data)])
            shown[i] = s

    elif yt_results:
        for i, yt in enumerate(yt_results[:5]):
            title   = yt["title"]
            dur_str = fmt_dur(yt["duration"])
            label   = f"🎵 {title[:30]} ({dur_str})"
            url     = yt["url"]
            cb_data = f"MUSIC|url|{url}"
            if len(cb_data) > 64:
                cb_data = f"MUSIC|url|{url[:58]}"
            kb.append([InlineKeyboardButton(label, callback_data=cb_data)])

    await msg.delete()

    text_lines = ["🎵 *Qidirish natijalari:*\n"]
    if spotify_results:
        for i, s in enumerate(spotify_results[:3], 1):
            text_lines.append(
                f"{i}. *{s['title']}* — {s['artist']}\n"
                f"   💿 {s['album']} • ⏱ {fmt_dur(s['duration'])}"
            )
    elif yt_results:
        for i, yt in enumerate(yt_results[:5], 1):
            text_lines.append(f"{i}. *{yt['title']}* ({fmt_dur(yt['duration'])})")

    text = "\n".join(text_lines)

    thumb_url = ""
    if spotify_results and spotify_results[0].get("thumb"):
        thumb_url = spotify_results[0]["thumb"]
    elif yt_results and yt_results[0].get("thumb"):
        thumb_url = yt_results[0]["thumb"]

    if thumb_url:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(thumb_url)
            if r.status_code == 200:
                await update.message.reply_photo(
                    photo=BytesIO(r.content),
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(kb),
                )
                return
        except Exception:
            pass

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def music_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 2)
    if len(parts) < 3:
        return
    _, mode, val = parts
    uid = query.from_user.id

    if is_banned(uid):
        return

    msg = await query.message.reply_text("⏳ Musiqa yuklanmoqda...")

    try:
        if mode == "dl":
            yt_results = await yt_search(val, limit=1)
            if not yt_results:
                await msg.edit_text("❌ YouTube dan topilmadi.")
                return
            url = yt_results[0]["url"]
        else:
            url = val

        result = await dl_video(url, audio_only=True, quality="best")

        if not result.get("data"):
            await msg.edit_text("❌ Yuklab bo'lmadi.")
            return

        title    = result["title"] or "audio"
        uploader = result["uploader"] or ""
        duration = result["duration"] or 0
        thumb_io = BytesIO(result["thumb"]) if result.get("thumb") else None

        eff_kb = []
        row = []
        for eid, ename in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(ename, callback_data=f"MEFF|{eid}"))
            if len(row) == 2:
                eff_kb.append(row)
                row = []
        if row:
            eff_kb.append(row)

        sent = await query.message.reply_audio(
            audio=BytesIO(result["data"]),
            title=title,
            performer=uploader,
            duration=duration,
            thumbnail=thumb_io,
            caption="🎵 Yuklandi! Quyida effekt qo'llashingiz mumkin:",
            reply_markup=InlineKeyboardMarkup(eff_kb),
        )

        await msg.delete()
        record(uid, "music")

    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")


async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(update.effective_user)
    if is_banned(uid):
        return

    q = " ".join(context.args) if context.args else ""
    if not q:
        await update.message.reply_text(
            "🎬 Kino nomini yozing:\n`/movie Inception`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _search_and_show_movies(update, q)


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(update.effective_user)
    if is_banned(uid):
        return

    if not TMDB_API_KEY:
        await update.message.reply_text("❌ TMDB API kalit sozlanmagan.")
        return

    msg = await update.message.reply_text("⏳ Trend kinolar yuklanmoqda...")
    results = await tmdb_trending()
    if not results:
        await msg.edit_text("❌ Trend kinolar olib bo'lmadi.")
        return

    await msg.delete()
    text = "🔥 *Haftalik trend kinolar:*\n\n"
    kb = []
    for i, r in enumerate(results, 1):
        mtype = r.get("media_type", "movie")
        title = r.get("title") or r.get("name") or "?"
        year  = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        rating = r.get("vote_average") or 0
        text += f"{i}. *{title}* ({year}) ⭐ {rating:.1f}\n"
        kb.append([InlineKeyboardButton(
            f"{'🎬' if mtype=='movie' else '📺'} {title[:35]}",
            callback_data=f"MOV|{r['id']}|{mtype}",
        )])

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def _search_and_show_movies(update: Update, query_str: str):
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ TMDB API kalit sozlanmagan.")
        return

    msg = await update.message.reply_text(f"🔍 *{query_str}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)
    results = await tmdb_search(query_str)

    if not results:
        await msg.edit_text("❌ Hech narsa topilmadi.")
        return

    await msg.delete()
    text = "🎬 *Qidirish natijalari:*\n\n"
    kb = []
    for r in results[:6]:
        mtype = r.get("media_type", "movie")
        if mtype not in ("movie", "tv"):
            continue
        title = r.get("title") or r.get("name") or "?"
        year  = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        rating = r.get("vote_average") or 0
        text += f"{'🎬' if mtype=='movie' else '📺'} *{title}* ({year}) ⭐{rating:.1f}\n"
        kb.append([InlineKeyboardButton(
            f"{'🎬' if mtype=='movie' else '📺'} {title[:40]}",
            callback_data=f"MOV|{r['id']}|{mtype}",
        )])

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|")
    mid   = int(parts[1])
    mtype = parts[2]
    uid   = query.from_user.id

    msg = await query.message.reply_text("⏳ Ma'lumot olinmoqda...")

    detail = await tmdb_detail(mid, mtype)
    if not detail:
        await msg.edit_text("❌ Ma'lumot olib bo'lmadi.")
        return

    title    = detail.get("title") or detail.get("name") or "?"
    overview = detail.get("overview") or "Tavsif yo'q"
    rating   = detail.get("vote_average") or 0
    year     = (detail.get("release_date") or detail.get("first_air_date") or "")[:4]
    runtime  = detail.get("runtime") or 0
    genres   = ", ".join(g["name"] for g in detail.get("genres", [])[:3])
    cast_list = detail.get("credits", {}).get("cast", [])[:4]
    cast     = ", ".join(a["name"] for a in cast_list)
    poster   = detail.get("poster_path", "")

    text = (
        f"🎬 *{title}* ({year})\n\n"
        f"⭐ Reyting: `{rating:.1f}/10`\n"
    )
    if runtime:
        text += f"⏱ Davomiylik: `{runtime} daqiqa`\n"
    if genres:
        text += f"🎭 Janr: `{genres}`\n"
    if cast:
        text += f"🌟 Aktvorlar: {cast}\n"
    text += f"\n📝 {overview[:400]}"

    trailer_kb = []
    videos = detail.get("videos", {}).get("results", [])
    for v in videos:
        if v.get("site") == "YouTube" and "Trailer" in v.get("type", ""):
            trailer_kb.append([InlineKeyboardButton(
                "▶️ Trailer", url=f"https://youtu.be/{v['key']}"
            )])
            break

    record(uid, "movies")

    poster_img = await tmdb_fetch_poster(poster)
    if poster_img:
        await query.message.reply_photo(
            photo=BytesIO(poster_img),
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(trailer_kb) if trailer_kb else None,
        )
    else:
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=InlineKeyboardMarkup(trailer_kb) if trailer_kb else None)
    await msg.delete()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid):
        return

    if not TMDB_API_KEY:
        await update.message.reply_text("❌ TMDB API kalit sozlanmagan.")
        return

    msg = await update.message.reply_text("🔍 Rasm tahlil qilinmoqda...")

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    buf = BytesIO()
    await tg_file.download_to_memory(buf)
    img_bytes = buf.getvalue()

    title = await yandex_reverse_search(img_bytes)

    if not title:
        await msg.edit_text("❌ Kinoni rasmdan aniqlash imkoni bo'lmadi.")
        return

    await msg.edit_text(f"🔍 *{title}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)
    results = await tmdb_search(title)

    if not results:
        await msg.edit_text(f"❌ *{title}* — TMDB dan topilmadi.", parse_mode=ParseMode.MARKDOWN)
        return

    await msg.delete()
    text = f"🔍 Topildi: *{title}*\n\n"
    kb = []
    for r in results[:5]:
        mtype = r.get("media_type", "movie")
        if mtype not in ("movie", "tv"):
            continue
        rtitle = r.get("title") or r.get("name") or "?"
        year   = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        kb.append([InlineKeyboardButton(
            f"{'🎬' if mtype=='movie' else '📺'} {rtitle[:40]} ({year})",
            callback_data=f"MOV|{r['id']}|{mtype}",
        )])

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def eff_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eid = query.data.split("|", 1)[1]
    context.user_data["effect_waiting"] = True
    context.user_data["effect_id"] = eid
    ename = AUDIO_EFFECTS.get(eid, eid)
    await query.message.edit_text(
        f"✅ *{ename}* effekti tanlandi!\n\nEndi audio faylni yuboring:",
        parse_mode=ParseMode.MARKDOWN,
    )


async def meff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eid   = query.data.split("|", 1)[1]
    ename = AUDIO_EFFECTS.get(eid, eid)
    audio_msg = query.message
    uid = query.from_user.id

    msg = await query.message.reply_text(f"🎛 *{ename}* effekti qo'llanmoqda...", parse_mode=ParseMode.MARKDOWN)

    af = audio_msg.audio or audio_msg.voice
    if not af:
        await msg.edit_text("❌ Audio fayl topilmadi.")
        return

    try:
        tg_file = await af.get_file()
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(buf.read())
            inp_path = tf.name

        def _apply():
            audio = AudioSegment.from_file(inp_path)
            processed = apply_effect(audio, eid)
            out_path = inp_path + "_fx.mp3"
            processed.export(out_path, format="mp3", bitrate="192k")
            return out_path

        out_path = await asyncio.get_event_loop().run_in_executor(None, _apply)

        with open(out_path, "rb") as f:
            await query.message.reply_audio(
                audio=f,
                title=f"{getattr(af, 'title', 'Audio')} [{ename}]",
                caption=f"🎛 Effekt: *{ename}*",
                parse_mode=ParseMode.MARKDOWN,
            )

        os.unlink(inp_path)
        os.unlink(out_path)
        await msg.delete()
        record(uid, "effects")

    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")


async def _do_effect(update: Update, context: ContextTypes.DEFAULT_TYPE, af) -> bool:
    if not context.user_data.get("effect_waiting"):
        return False

    eid = context.user_data.get("effect_id")
    if not eid:
        eff_kb = []
        row = []
        for e_id, e_name in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(e_name, callback_data=f"EFF|{e_id}"))
            if len(row) == 2:
                eff_kb.append(row)
                row = []
        if row:
            eff_kb.append(row)
        await update.message.reply_text(
            "🎛 Effektni tanlang:",
            reply_markup=InlineKeyboardMarkup(eff_kb),
        )
        return True

    ename = AUDIO_EFFECTS.get(eid, eid)
    uid   = update.effective_user.id
    msg   = await update.message.reply_text(f"🎛 *{ename}* effekti qo'llanmoqda...", parse_mode=ParseMode.MARKDOWN)

    try:
        tg_file = await af.get_file()
        buf = BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(buf.read())
            inp_path = tf.name

        def _apply():
            audio = AudioSegment.from_file(inp_path)
            processed = apply_effect(audio, eid)
            out_path = inp_path + "_fx.mp3"
            processed.export(out_path, format="mp3", bitrate="192k")
            return out_path

        out_path = await asyncio.get_event_loop().run_in_executor(None, _apply)

        title = getattr(af, "title", None) or "Audio"
        with open(out_path, "rb") as f:
            await update.message.reply_audio(
                audio=f,
                title=f"{title} [{ename}]",
                caption=f"🎛 Effekt: *{ename}*",
                parse_mode=ParseMode.MARKDOWN,
            )

        os.unlink(inp_path)
        os.unlink(out_path)
        await msg.delete()
        record(uid, "effects")
        context.user_data["effect_waiting"] = False
        context.user_data["effect_id"] = None

    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")

    return True


async def cmd_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    context.user_data["effect_waiting"] = True
    context.user_data["effect_id"] = None

    eff_kb = []
    row = []
    for eid, ename in AUDIO_EFFECTS.items():
        row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
        if len(row) == 2:
            eff_kb.append(row)
            row = []
    if row:
        eff_kb.append(row)

    await update.message.reply_text(
        "🎛 *Audio Effektlar*\n\nEffektni tanlang, so'ng audio faylni yuboring:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(eff_kb),
    )


async def cmd_circle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    context.user_data["circle_waiting"] = True
    await update.message.reply_text(
        "⭕ *Dumaloq Video*\n\nVideo faylni yuboring (max 60 soniya):",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _do_circle(update: Update, context: ContextTypes.DEFAULT_TYPE, vf) -> bool:
    if not context.user_data.get("circle_waiting"):
        return False

    uid = update.effective_user.id
    msg = await update.message.reply_text("⭕ Dumaloq video tayyorlanmoqda...")

    try:
        tg_file = await vf.get_file()
        buf = BytesIO()
        await tg_file.download_to_memory(buf)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(buf.getvalue())
            inp_path = tf.name

        circle_path = await asyncio.get_event_loop().run_in_executor(
            None, make_circle_video, inp_path
        )

        if circle_path and os.path.exists(circle_path):
            dur = min(getattr(vf, "duration", 0) or 0, 60)
            with open(circle_path, "rb") as f:
                await update.message.reply_video_note(
                    video_note=f,
                    duration=dur,
                    length=384,
                )
            os.unlink(circle_path)
            await msg.delete()
            record(uid, "dl")
        else:
            await msg.edit_text("❌ Dumaloq video yaratib bo'lmadi.\nffmpeg o'rnatilganligini tekshiring.")

        if os.path.exists(inp_path):
            os.unlink(inp_path)

        context.user_data["circle_waiting"] = False

    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")

    return True


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    s = STATS[uid]
    total = s["dl"] + s["music"] + s["movies"] + s["effects"]
    await update.message.reply_text(
        f"📊 *Sizning statistikangiz*\n\n"
        f"📥 Yuklab olish: `{s['dl']}`\n"
        f"🎵 Musiqa: `{s['music']}`\n"
        f"🎬 Kino: `{s['movies']}`\n"
        f"🎛 Effektlar: `{s['effects']}`\n"
        f"━━━━━━━━━\n"
        f"📈 Jami: `{total}`\n"
        f"📅 Ro'yxatdan: `{s['joined'] or 'N/A'}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Siz admin emassiz.")
        return
    await _show_admin_panel(update.message)


async def _show_admin_panel(message):
    total  = len(USER_INFO)
    banned = sum(1 for u in USER_INFO.values() if u.get("banned"))
    total_dl = sum(v["dl"] for v in STATS.values())
    total_mu = sum(v["music"] for v in STATS.values())
    total_mo = sum(v["movies"] for v in STATS.values())

    text = (
        f"👑 *ADMIN PANEL*\n\n"
        f"👥 Foydalanuvchilar: `{total}`\n"
        f"🚫 Bloklangan: `{banned}`\n"
        f"📥 Yuklab olish: `{total_dl}`\n"
        f"🎵 Musiqa: `{total_mu}`\n"
        f"🎬 Kino: `{total_mo}`"
    )
    kb = [
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="ADMIN|users|1")],
        [InlineKeyboardButton("📢 Hammaga xabar",    callback_data="ADMIN|broadcast")],
        [InlineKeyboardButton("🚫 Ban ro'yxati",     callback_data="ADMIN|banlist")],
        [InlineKeyboardButton("📊 Top 10",           callback_data="ADMIN|top")],
        [InlineKeyboardButton("🗑 Statistikani tozala", callback_data="ADMIN|clearstats")],
    ]
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup(kb))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    if not is_admin(uid):
        await query.message.edit_text("❌ Ruxsat yo'q.")
        return

    parts  = query.data.split("|")
    action = parts[1]
    back   = [[InlineKeyboardButton("⬅️ Admin panel", callback_data="ADMIN|main")]]

    if action == "main":
        total  = len(USER_INFO)
        banned = sum(1 for u in USER_INFO.values() if u.get("banned"))
        total_dl = sum(v["dl"] for v in STATS.values())
        total_mu = sum(v["music"] for v in STATS.values())
        text = (
            f"👑 *ADMIN PANEL*\n\n"
            f"👥 Foydalanuvchilar: `{total}` (🚫 `{banned}`)\n"
            f"📥 Yuklab olish: `{total_dl}`\n"
            f"🎵 Musiqa: `{total_mu}`"
        )
        kb = [
            [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="ADMIN|users|1")],
            [InlineKeyboardButton("📢 Hammaga xabar",    callback_data="ADMIN|broadcast")],
            [InlineKeyboardButton("🚫 Ban ro'yxati",     callback_data="ADMIN|banlist")],
            [InlineKeyboardButton("📊 Top 10",           callback_data="ADMIN|top")],
            [InlineKeyboardButton("🗑 Statistikani tozala", callback_data="ADMIN|clearstats")],
        ]
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(kb))

    elif action == "users":
        page     = int(parts[2]) if len(parts) > 2 else 1
        per_page = 10
        users    = list(USER_INFO.values())
        total_p  = max(1, math.ceil(len(users) / per_page))
        page     = max(1, min(page, total_p))
        chunk    = users[(page - 1) * per_page: page * per_page]

        text = f"👥 *Foydalanuvchilar* ({len(users)} ta) — {page}/{total_p}\n\n"
        kb = []
        for u in chunk:
            bid  = u["id"]
            icon = "🚫 " if u.get("banned") else ""
            uname = f"@{u['username']}" if u.get("username") else ""
            text += f"{icon}`{bid}` — {u['name']} {uname}\n"
            kb.append([
                InlineKeyboardButton(
                    "✅ Unban" if u.get("banned") else "🚫 Ban",
                    callback_data=f"ADMIN|toggleban|{bid}",
                ),
                InlineKeyboardButton(
                    f"📊 {u['name'][:15]}",
                    callback_data=f"ADMIN|userstat|{bid}",
                ),
            ])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"ADMIN|users|{page - 1}"))
        if page < total_p:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"ADMIN|users|{page + 1}"))
        if nav:
            kb.append(nav)
        kb.extend(back)
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(kb))

    elif action == "toggleban":
        tid = int(parts[2])
        if tid in ADMIN_IDS:
            await query.answer("❌ Adminni banlash mumkin emas!", show_alert=True)
            return
        if tid not in USER_INFO:
            await query.answer("❌ Foydalanuvchi topilmadi.", show_alert=True)
            return
        USER_INFO[tid]["banned"] = not USER_INFO[tid].get("banned", False)
        status = "🚫 Bloklandi" if USER_INFO[tid]["banned"] else "✅ Blokdan chiqarildi"
        await query.answer(f"{status}: {USER_INFO[tid]['name']}", show_alert=True)
        query.data = "ADMIN|users|1"
        parts = ["ADMIN", "users", "1"]
        action = "users"
        page = 1
        per_page = 10
        users = list(USER_INFO.values())
        total_p = max(1, math.ceil(len(users) / per_page))
        chunk = users[:per_page]
        text = f"👥 *Foydalanuvchilar* ({len(users)} ta) — 1/{total_p}\n\n"
        kb = []
        for u in chunk:
            bid = u["id"]
            icon = "🚫 " if u.get("banned") else ""
            uname = f"@{u['username']}" if u.get("username") else ""
            text += f"{icon}`{bid}` — {u['name']} {uname}\n"
            kb.append([
                InlineKeyboardButton(
                    "✅ Unban" if u.get("banned") else "🚫 Ban",
                    callback_data=f"ADMIN|toggleban|{bid}",
                ),
                InlineKeyboardButton(
                    f"📊 {u['name'][:15]}",
                    callback_data=f"ADMIN|userstat|{bid}",
                ),
            ])
        kb.extend(back)
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(kb))

    elif action == "userstat":
        tid = int(parts[2])
        u = USER_INFO.get(tid, {})
        s = STATS.get(tid, {})
        total_act = s.get("dl", 0) + s.get("music", 0) + s.get("movies", 0) + s.get("effects", 0)
        ban_status = "🚫 Bloklangan" if u.get("banned") else "✅ Faol"
        text = (
            f"👤 *Foydalanuvchi*\n\n"
            f"🆔 `{tid}`\n"
            f"👤 {u.get('name', '?')}\n"
            f"📛 @{u.get('username', '—')}\n"
            f"🔒 {ban_status}\n"
            f"📅 {s.get('joined', '?')}\n\n"
            f"📥 Yuklab olish: `{s.get('dl', 0)}`\n"
            f"🎵 Musiqa: `{s.get('music', 0)}`\n"
            f"🎬 Kino: `{s.get('movies', 0)}`\n"
            f"📈 Jami: `{total_act}`"
        )
        kb = [
            [InlineKeyboardButton(
                "✅ Blokdan chiqarish" if u.get("banned") else "🚫 Bloklash",
                callback_data=f"ADMIN|toggleban|{tid}",
            )],
            [InlineKeyboardButton("📢 Xabar yuborish", callback_data=f"ADMIN|msguser|{tid}")],
        ]
        kb.extend(back)
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(kb))

    elif action == "msguser":
        tid = int(parts[2])
        context.user_data["msg_to_user"] = tid
        await query.message.edit_text(
            f"✉️ `{tid}` ga xabar yozing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back),
        )

    elif action == "broadcast":
        context.user_data["broadcast_mode"] = True
        count = len([u for u in USER_INFO.values() if not u.get("banned")])
        await query.message.edit_text(
            f"📢 *Hammaga xabar*\n\nFaol foydalanuvchilar: *{count}* ta\n\nXabar yozing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back),
        )

    elif action == "banlist":
        banned_users = [u for u in USER_INFO.values() if u.get("banned")]
        if not banned_users:
            await query.message.edit_text("✅ Bloklangan foydalanuvchi yo'q.",
                                          reply_markup=InlineKeyboardMarkup(back))
            return
        text = f"🚫 *Ban ro'yxati* ({len(banned_users)} ta):\n\n"
        kb = []
        for u in banned_users[:20]:
            text += f"• `{u['id']}` — {u['name']}\n"
            kb.append([InlineKeyboardButton(
                f"✅ {u['name'][:20]} unban",
                callback_data=f"ADMIN|toggleban|{u['id']}",
            )])
        kb.extend(back)
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(kb))

    elif action == "top":
        top = sorted(
            STATS.items(),
            key=lambda x: x[1]["dl"] + x[1]["music"] + x[1]["movies"],
            reverse=True,
        )[:10]
        text = "📊 *Top 10 faol foydalanuvchi:*\n\n"
        for i, (u_id, s) in enumerate(top, 1):
            name = USER_INFO.get(u_id, {}).get("name", str(u_id))
            total_act = s["dl"] + s["music"] + s["movies"]
            text += f"{i}. *{name}* — `{total_act}` ta harakatlar\n"
        await query.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(back))

    elif action == "clearstats":
        kb = [
            [
                InlineKeyboardButton("✅ Ha, tozala", callback_data="ADMIN|clearconfirm"),
                InlineKeyboardButton("❌ Bekor",      callback_data="ADMIN|main"),
            ]
        ]
        await query.message.edit_text(
            "⚠️ Barcha statistikani tozalashni tasdiqlaysizmi?",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    elif action == "clearconfirm":
        for u_id in list(STATS.keys()):
            joined = STATS[u_id].get("joined", "")
            STATS[u_id] = {"dl": 0, "music": 0, "movies": 0, "effects": 0, "joined": joined}
        await query.message.edit_text("✅ Barcha statistika tozalandi.",
                                      reply_markup=InlineKeyboardMarkup(back))


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if not is_admin(uid):
        return False

    if context.user_data.get("broadcast_mode"):
        context.user_data.pop("broadcast_mode")
        targets = [u_id for u_id, u in USER_INFO.items()
                   if not u.get("banned") and u_id != uid]
        msg = await update.message.reply_text(f"📢 {len(targets)} ta foydalanuvchiga yuborilmoqda...")
        ok = fail = 0
        for t_id in targets:
            try:
                await update.message.copy_to(t_id)
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await msg.edit_text(
            f"✅ Yuborildi: *{ok}*\n❌ Xato: *{fail}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    if context.user_data.get("msg_to_user"):
        t_id = context.user_data.pop("msg_to_user")
        try:
            await update.message.copy_to(t_id)
            await update.message.reply_text(f"✅ `{t_id}` ga yuborildi.", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            await update.message.reply_text(f"❌ Yuborib bo'lmadi: {e}")
        return True

    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    register_user(user)
    uid = user.id

    if is_banned(uid):
        await update.message.reply_text("🚫 Siz bloklangansiz.")
        return

    if is_admin(uid):
        handled = await admin_text_handler(update, context)
        if handled:
            return

    if update.message.photo:
        await handle_photo(update, context)
        return

    af = update.message.audio or update.message.voice
    if not af and update.message.document:
        mt = update.message.document.mime_type or ""
        if "audio" in mt:
            af = update.message.document
    if af:
        handled = await _do_effect(update, context, af)
        if handled:
            return

    vf = update.message.video or update.message.document
    if vf:
        mt = getattr(vf, "mime_type", "") or ""
        if update.message.video or "video" in mt:
            handled = await _do_circle(update, context, vf)
            if handled:
                return

    text = (update.message.text or "").strip()
    if is_url(text):
        await handle_url(update, context)
        return

    if text and not text.startswith("/"):
        await update.message.reply_text(
            "💡 Nima yuborishni bilmadim.\n\n"
            "📥 Video havolasini yuboring\n"
            "📸 Kino rasmini yuboring\n"
            "❓ /help — yordam",
        )


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (TimedOut, NetworkError)):
        return
    print(f"[ERR] {context.error}")


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("BOT_TOKEN sozlanmagan! .env faylini to'ldiring.")
        return

    print("Pro Media Bot ishga tushmoqda...")
    if not TMDB_API_KEY:
        print("Eslatma: TMDB_API_KEY yo'q — kino funksiyasi ishlamaydi")
    if not SPOTIPY_OK or not SPOTIFY_CLIENT_ID:
        print("Eslatma: Spotify sozlanmagan — YouTube qidirish ishlatiladi")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     lambda u, c: u.message.reply_text(
        "📋 Buyruqlar:\n/music — musiqa\n/movie — kino\n/trending — top kinolar\n"
        "/effect — audio effektlar\n/circle — dumaloq video\n/stats — statistika"
    )))
    app.add_handler(CommandHandler("music",    cmd_music))
    app.add_handler(CommandHandler("movie",    cmd_movie))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("effect",   cmd_effect))
    app.add_handler(CommandHandler("circle",   cmd_circle))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("admin",    cmd_admin))

    app.add_handler(CallbackQueryHandler(platform_callback,  pattern=r"^PLT\|"))
    app.add_handler(CallbackQueryHandler(menu_callback,      pattern=r"^MENU\|"))
    app.add_handler(CallbackQueryHandler(dl_callback,        pattern=r"^DL\|"))
    app.add_handler(CallbackQueryHandler(music_callback,     pattern=r"^MUSIC\|"))
    app.add_handler(CallbackQueryHandler(eff_select_callback,pattern=r"^EFF\|"))
    app.add_handler(CallbackQueryHandler(meff_callback,      pattern=r"^MEFF\|"))
    app.add_handler(CallbackQueryHandler(movie_callback,     pattern=r"^MOV\|"))
    app.add_handler(CallbackQueryHandler(admin_callback,     pattern=r"^ADMIN\|"))

    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.AUDIO | filters.VOICE |
        filters.VIDEO | filters.Document.ALL,
        handle_message,
    ))

    app.add_error_handler(error_handler)

    print("Bot tayyor! Polling boshlandi...\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
