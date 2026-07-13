import os
import re
import time
import asyncio
import tempfile
import subprocess
import json
import math
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from datetime import datetime
from collections import defaultdict

import httpx
import yt_dlp
from PIL import Image
from pydub import AudioSegment
from pydub.effects import normalize

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

STATS: dict     = defaultdict(lambda: {"dl": 0, "music": 0, "movies": 0, "effects": 0, "joined": ""})
USER_INFO: dict = {}
URL_STORE: dict = {}
URL_CTR: list   = [0]

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


def save_url(url: str) -> str:
    URL_CTR[0] += 1
    key = str(URL_CTR[0])
    URL_STORE[key] = url
    if len(URL_STORE) > 2000:
        oldest = list(URL_STORE.keys())[:500]
        for k in oldest:
            URL_STORE.pop(k, None)
    return key


def load_url(key: str) -> str:
    return URL_STORE.get(key, "")


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


def ffmpeg_run(*args) -> bool:
    r = subprocess.run(["ffmpeg", "-y"] + list(args), capture_output=True)
    return r.returncode == 0


def make_circle_video(inp: str) -> str | None:
    out = inp + "_circle.mp4"
    ok = ffmpeg_run(
        "-i", inp,
        "-vf", "scale=640:640:force_original_aspect_ratio=increase,crop=640:640,scale=384:384",
        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-t", "60",
        out,
    )
    if ok and os.path.exists(out) and os.path.getsize(out) > 1000:
        return out
    return None


def apply_effect(audio: AudioSegment, eid: str) -> AudioSegment:
    if eid == "bass":
        low  = audio.low_pass_filter(200) + 8
        mid  = audio.high_pass_filter(200).low_pass_filter(3000)
        high = audio.high_pass_filter(3000) - 2
        audio = low.overlay(mid).overlay(high)
    elif eid == "reverb":
        result = audio
        for delay, vol in [(30, -6), (60, -10), (120, -15), (240, -20)]:
            layer = AudioSegment.silent(duration=delay) + (audio + vol)
            if len(layer) < len(result):
                layer += AudioSegment.silent(duration=len(result) - len(layer))
            result = result.overlay(layer)
        audio = result
    elif eid == "echo":
        e1 = AudioSegment.silent(300) + (audio - 8)
        e2 = AudioSegment.silent(600) + (audio - 14)
        for e in [e1, e2]:
            if len(e) < len(audio):
                e += AudioSegment.silent(len(audio) - len(e))
        audio = audio.overlay(e1).overlay(e2)
    elif eid == "vocal":
        audio = audio.high_pass_filter(300).low_pass_filter(8000) + 5
    elif eid == "slow":
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": int(audio.frame_rate * 0.75)}).set_frame_rate(audio.frame_rate)
    elif eid == "fast":
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": int(audio.frame_rate * 1.3)}).set_frame_rate(audio.frame_rate)
    elif eid == "rock":
        low  = audio.low_pass_filter(150) + 4
        mid  = audio.high_pass_filter(150).low_pass_filter(2000)
        high = audio.high_pass_filter(2000) + 3
        audio = low.overlay(mid).overlay(high)
    elif eid == "lofi":
        audio = audio.low_pass_filter(3500)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": int(audio.frame_rate * 0.98)}).set_frame_rate(audio.frame_rate)
        audio = audio - 2
    return normalize(audio)


def _dl_sync(url: str, audio_only: bool, quality: str = "best") -> dict:
    if audio_only:
        fmt = "bestaudio/best"
    elif quality == "720":
        fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best"
    elif quality == "480":
        fmt = "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best"
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
        if audio_only:
            opts["postprocessors"].append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        ext = "mp3" if audio_only else "mp4"
        fdata = tdata = None
        for fn in os.listdir(tmp):
            fp = os.path.join(tmp, fn)
            if fn.endswith(f".{ext}") and fdata is None:
                fdata = open(fp, "rb").read()
            elif fn.endswith((".jpg", ".jpeg", ".png", ".webp")) and tdata is None:
                tdata = open(fp, "rb").read()

        return {
            "data":     fdata,
            "thumb":    tdata,
            "title":    info.get("title", "Video"),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration") or 0,
        }


async def dl_async(url: str, audio_only: bool, quality: str = "best") -> dict:
    return await asyncio.get_event_loop().run_in_executor(None, _dl_sync, url, audio_only, quality)


def _yt_search_sync(query: str, limit: int = 5) -> list:
    results = []
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            for e in (info or {}).get("entries", []):
                vid_id = e.get("id", "")
                results.append({
                    "title":    e.get("title", ""),
                    "url":      f"https://www.youtube.com/watch?v={vid_id}",
                    "duration": e.get("duration") or 0,
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "thumb":    e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                })
        except Exception:
            pass
    return results


async def yt_search(query: str, limit: int = 5) -> list:
    return await asyncio.get_event_loop().run_in_executor(None, _yt_search_sync, query, limit)


def _spotify_sync(query: str) -> list:
    if not (SPOTIPY_OK and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return []
    try:
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET))
        r = sp.search(q=query, type="track", limit=5)
        results = []
        for item in r["tracks"]["items"]:
            artists = ", ".join(a["name"] for a in item["artists"])
            thumb = item["album"]["images"][0]["url"] if item["album"]["images"] else ""
            results.append({
                "title":   item["name"],
                "artist":  artists,
                "album":   item["album"]["name"],
                "duration": item["duration_ms"] // 1000,
                "thumb":   thumb,
            })
        return results
    except Exception:
        return []


async def spotify_search(query: str) -> list:
    return await asyncio.get_event_loop().run_in_executor(None, _spotify_sync, query)


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


async def tmdb_poster(poster: str) -> bytes | None:
    if not poster:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(TMDB_IMG + poster)
            return r.content if r.status_code == 200 else None
    except Exception:
        return None


async def yandex_reverse(img_bytes: bytes) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as c:
            r = await c.post(
                "https://yandex.com/images/search",
                params={"rpt": "imageview", "format": "json"},
                files={"upfile": ("photo.jpg", img_bytes, "image/jpeg")},
            )
            cbir = None
            loc = r.headers.get("location", "")
            if loc:
                cbir = "https://yandex.com" + loc if loc.startswith("/") else loc
            else:
                m = re.search(r'(?:url|href)=["\']([^"\']*cbir[^"\']*)["\']', r.text)
                if m:
                    u = m.group(1)
                    cbir = "https://yandex.com" + u if u.startswith("/") else u
            if not cbir:
                return None
            r2 = await c.get(cbir)
            titles = re.findall(r'"snippet":\s*"([^"]{4,80})"', r2.text)
            if not titles:
                titles = re.findall(r'<title[^>]*>([^<]{4,100})</title>', r2.text)
            if titles:
                clean = re.sub(r'\s*[—\-|]\s*Yandex.*', '', titles[0]).strip()
                return clean if len(clean) > 3 else None
    except Exception:
        pass
    return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)
    if is_banned(user.id):
        await update.message.reply_text("🚫 Siz bloklangansiz.")
        return
    await _show_start(update.message, user)


async def _show_start(message, user):
    text = (
        f"👋 *Salom, {user.first_name}!*\n\n"
        "📥 Havola yuboring — yuklab beraman\n"
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
    plat_kb += [
        [InlineKeyboardButton("🎵 Musiqa", callback_data="M|music"),
         InlineKeyboardButton("🎬 Kino",   callback_data="M|movie")],
        [InlineKeyboardButton("🎛 Effektlar", callback_data="M|effect"),
         InlineKeyboardButton("⭕ Dumaloq",   callback_data="M|circle")],
        [InlineKeyboardButton("📊 Statistika", callback_data="M|stats"),
         InlineKeyboardButton("❓ Yordam",      callback_data="M|help")],
    ]
    if is_admin(user.id):
        plat_kb.append([InlineKeyboardButton("👑 Admin", callback_data="A|main")])
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup(plat_kb))


async def platform_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pname = q.data.split("|", 1)[1]
    examples = {
        "youtube":     "youtube.com/watch?v=...",
        "instagram":   "instagram.com/reel/...",
        "tiktok":      "tiktok.com/@user/video/...",
        "pinterest":   "pin.it/...",
        "twitter":     "x.com/i/status/...",
        "facebook":    "fb.watch/...",
        "vimeo":       "vimeo.com/123456",
        "twitch":      "twitch.tv/videos/...",
        "reddit":      "reddit.com/r/.../...",
        "dailymotion": "dailymotion.com/video/...",
    }
    ex = examples.get(pname, "havolani yuboring")
    text = (
        f"📥 *{pname.upper()}*\n\n"
        f"Havola: `{ex}`\n\n"
        f"Shu chatga havolani yuboring — bot o'zi aniqlaydi!\n\n"
        f"• 🎬 Best sifat  • 📺 720p  • 📱 480p\n"
        f"• 🎵 MP3  • ⭕ Dumaloq video"
    )
    await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                              reply_markup=InlineKeyboardMarkup(
                                  [[InlineKeyboardButton("⬅️ Orqaga", callback_data="M|back")]]))


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split("|", 1)[1]
    uid = q.from_user.id
    back = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="M|back")]]

    if action == "back":
        await q.message.delete()
        await _show_start(q.message, q.from_user)
        return

    elif action == "help":
        text = (
            "❓ *Yordam*\n\n"
            "📥 Havola yuboring → format tanlang\n"
            "🎵 `/music nom` — musiqa qidirish\n"
            "🎬 `/movie nom` — kino qidirish\n"
            "🔥 `/trending` — top kinolar\n"
            "📸 Kino rasmi yuboring → bot aniqlaydi\n"
            "🎛 `/effect` → audio yuboring\n"
            "⭕ `/circle` → video yuboring"
        )
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "stats":
        s = STATS[uid]
        total = s["dl"] + s["music"] + s["movies"] + s["effects"]
        text = (
            f"📊 *Statistika*\n\n"
            f"📥 Yuklab olish: `{s['dl']}`\n"
            f"🎵 Musiqa: `{s['music']}`\n"
            f"🎬 Kino: `{s['movies']}`\n"
            f"🎛 Effektlar: `{s['effects']}`\n"
            f"📈 Jami: `{total}`\n"
            f"📅 Qo'shilgan: `{s['joined'] or 'N/A'}`"
        )
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "music":
        await q.message.edit_text(
            "🎵 `/music qo'shiq nomi` yozing\n\nMisol: `/music Blinding Lights`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back))

    elif action == "movie":
        await q.message.edit_text(
            "🎬 `/movie kino nomi` yozing\n\nMisol: `/movie Inception`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back))

    elif action == "effect":
        context.user_data["eff_wait"] = True
        context.user_data["eff_id"] = None
        kb = []
        row = []
        for eid, ename in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="M|back")])
        await q.message.edit_text(
            "🎛 *Effekt tanlang, keyin audio yuboring:*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))

    elif action == "circle":
        context.user_data["circle_wait"] = True
        await q.message.edit_text(
            "⭕ *Video faylni yuboring (max 60 son.):*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(back))


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    uid = update.effective_user.id
    if is_banned(uid):
        return
    key = save_url(url)
    kb = [
        [InlineKeyboardButton("🎬 Best",    callback_data=f"DL|v|b|{key}"),
         InlineKeyboardButton("📺 720p",    callback_data=f"DL|v|7|{key}"),
         InlineKeyboardButton("📱 480p",    callback_data=f"DL|v|4|{key}")],
        [InlineKeyboardButton("🎵 MP3",     callback_data=f"DL|a|b|{key}"),
         InlineKeyboardButton("⭕ Dumaloq", callback_data=f"DL|c|b|{key}"),
         InlineKeyboardButton("ℹ️ Info",    callback_data=f"DL|i|b|{key}")],
    ]
    await update.message.reply_text("⬇️ *Format tanlang:*",
                                    parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def dl_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    if len(parts) < 4:
        return
    _, mode, qual_code, key = parts[0], parts[1], parts[2], parts[3]
    url = load_url(key)
    if not url:
        await q.message.reply_text("❌ Havola topilmadi. Qaytadan yuboring.")
        return
    uid = q.from_user.id
    if is_banned(uid):
        return

    quality = {"b": "best", "7": "720", "4": "480"}.get(qual_code, "best")
    msg = await q.message.reply_text("⏳ Yuklanmoqda...")

    try:
        if mode == "i":
            await _show_info(q, url, msg)
            return

        is_audio  = mode == "a"
        is_circle = mode == "c"
        result = await dl_async(url, audio_only=(is_audio or is_circle), quality=quality)

        if not result.get("data"):
            await msg.edit_text("❌ Yuklab bo'lmadi. Havola noto'g'ri yoki himoyalangan.")
            return

        title    = result["title"] or "video"
        uploader = result["uploader"] or ""
        duration = result["duration"] or 0
        cap      = f"🎬 *{title}*"
        if uploader:
            cap += f"\n👤 {uploader}"
        if duration:
            cap += f"\n⏱ {fmt_dur(duration)}"

        thumb_io = BytesIO(result["thumb"]) if result.get("thumb") else None
        data_io  = BytesIO(result["data"])

        if is_audio:
            await q.message.reply_audio(
                audio=data_io, caption=cap, parse_mode=ParseMode.MARKDOWN,
                thumbnail=thumb_io, duration=duration,
                title=title, performer=uploader)

        elif is_circle:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                tf.write(result["data"])
                inp = tf.name
            try:
                cp = await asyncio.get_event_loop().run_in_executor(None, make_circle_video, inp)
                if cp and os.path.exists(cp):
                    with open(cp, "rb") as f:
                        await q.message.reply_video_note(video_note=f,
                                                         duration=min(duration, 60), length=384)
                    os.unlink(cp)
                else:
                    await msg.edit_text("❌ Dumaloq video yaratib bo'lmadi.")
                    return
            finally:
                if os.path.exists(inp):
                    os.unlink(inp)
        else:
            await q.message.reply_video(
                video=data_io, caption=cap, parse_mode=ParseMode.MARKDOWN,
                thumbnail=thumb_io, duration=duration, supports_streaming=True)

        await msg.delete()
        record(uid, "dl")

    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:250]}")


async def _show_info(q, url: str, msg):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        def _get():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await asyncio.get_event_loop().run_in_executor(None, _get)
        text = (
            f"ℹ️ *Ma'lumot*\n\n"
            f"📌 {info.get('title','?')}\n"
            f"👤 {info.get('uploader') or info.get('channel','?')}\n"
            f"⏱ {fmt_dur(info.get('duration',0))}\n"
            f"👁 {info.get('view_count') or '?'}\n"
            f"📅 {info.get('upload_date','?')}"
        )
        await msg.edit_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("🎵 Misol: `/music Blinding Lights`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    msg = await update.message.reply_text(f"🔍 Qidirilmoqda: *{query}*...",
                                          parse_mode=ParseMode.MARKDOWN)
    sp_res = await spotify_search(query)
    yt_res = await yt_search(query, 5)

    if not sp_res and not yt_res:
        await msg.edit_text("❌ Hech narsa topilmadi.")
        return

    await msg.delete()

    text = "🎵 *Natijalar:*\n\n"
    kb = []

    if sp_res:
        for i, s in enumerate(sp_res[:4], 1):
            text += f"{i}. *{s['title']}* — {s['artist']} ({fmt_dur(s['duration'])})\n"
            yt_q = f"{s['title']} {s['artist']} audio"
            key  = save_url(yt_q)
            kb.append([InlineKeyboardButton(
                f"🎵 {s['title'][:28]} — {s['artist'][:15]}",
                callback_data=f"MU|s|{key}")])
    else:
        for i, yt in enumerate(yt_res[:5], 1):
            text += f"{i}. *{yt['title']}* ({fmt_dur(yt['duration'])})\n"
            key  = save_url(yt["url"])
            kb.append([InlineKeyboardButton(
                f"🎵 {yt['title'][:35]}",
                callback_data=f"MU|u|{key}")])

    thumb_url = sp_res[0]["thumb"] if sp_res and sp_res[0].get("thumb") else (
                yt_res[0]["thumb"] if yt_res and yt_res[0].get("thumb") else "")

    if thumb_url:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(thumb_url)
            if r.status_code == 200:
                await update.message.reply_photo(
                    photo=BytesIO(r.content), caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(kb))
                return
        except Exception:
            pass

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def music_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    if len(parts) < 3:
        return
    _, mode, key = parts[0], parts[1], parts[2]
    uid = q.from_user.id
    if is_banned(uid):
        return

    val = load_url(key)
    if not val:
        await q.message.reply_text("❌ Topilmadi. Qaytadan /music yozing.")
        return

    msg = await q.message.reply_text("⏳ Musiqa yuklanmoqda...")
    try:
        if mode == "s":
            yt_res = await yt_search(val, 1)
            if not yt_res:
                await msg.edit_text("❌ YouTube dan topilmadi.")
                return
            url = yt_res[0]["url"]
        else:
            url = val

        result = await dl_async(url, audio_only=True)
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
            row.append(InlineKeyboardButton(ename, callback_data=f"MEF|{eid}"))
            if len(row) == 2:
                eff_kb.append(row)
                row = []
        if row:
            eff_kb.append(row)

        await q.message.reply_audio(
            audio=BytesIO(result["data"]), title=title, performer=uploader,
            duration=duration, thumbnail=thumb_io,
            caption="✅ Yuklandi! Effekt qo'llash uchun quyidagi tugmani bosing:",
            reply_markup=InlineKeyboardMarkup(eff_kb))

        await msg.delete()
        record(uid, "music")
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")


async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    q = " ".join(context.args) if context.args else ""
    if not q:
        await update.message.reply_text("🎬 Misol: `/movie Inception`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    await _search_movies(update, q)


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ TMDB API kalit sozlanmagan.")
        return
    msg = await update.message.reply_text("⏳ Yuklanmoqda...")
    results = await asyncio.get_event_loop().run_in_executor(
        None, lambda: asyncio.run(asyncio.coroutine(lambda: None)()))
    d = await tmdb_req("/trending/all/week")
    results = (d or {}).get("results", [])[:8]
    if not results:
        await msg.edit_text("❌ Topilmadi.")
        return
    await msg.delete()
    text = "🔥 *Haftalik top kinolar:*\n\n"
    kb = []
    for i, r in enumerate(results, 1):
        mt    = r.get("media_type", "movie")
        title = r.get("title") or r.get("name") or "?"
        year  = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        rat   = r.get("vote_average") or 0
        icon  = "🎬" if mt == "movie" else "📺"
        text += f"{i}. {icon} *{title}* ({year}) ⭐{rat:.1f}\n"
        kb.append([InlineKeyboardButton(f"{icon} {title[:38]}", callback_data=f"MOV|{r['id']}|{mt}")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def _search_movies(update: Update, query_str: str):
    if not TMDB_API_KEY:
        await update.message.reply_text("❌ TMDB API kalit sozlanmagan.")
        return
    msg = await update.message.reply_text(f"🔍 *{query_str}* qidirilmoqda...",
                                          parse_mode=ParseMode.MARKDOWN)
    results = await tmdb_search(query_str)
    if not results:
        await msg.edit_text("❌ Topilmadi.")
        return
    await msg.delete()
    text = "🎬 *Natijalar:*\n\n"
    kb = []
    for r in results[:6]:
        mt = r.get("media_type", "movie")
        if mt not in ("movie", "tv"):
            continue
        title = r.get("title") or r.get("name") or "?"
        year  = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        rat   = r.get("vote_average") or 0
        icon  = "🎬" if mt == "movie" else "📺"
        text += f"{icon} *{title}* ({year}) ⭐{rat:.1f}\n"
        kb.append([InlineKeyboardButton(f"{icon} {title[:40]}",
                                        callback_data=f"MOV|{r['id']}|{mt}")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def movie_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    mid, mtype = int(parts[1]), parts[2]
    uid = q.from_user.id
    msg = await q.message.reply_text("⏳ Ma'lumot olinmoqda...")
    detail = await tmdb_detail(mid, mtype)
    if not detail:
        await msg.edit_text("❌ Topilmadi.")
        return

    title    = detail.get("title") or detail.get("name") or "?"
    overview = detail.get("overview") or "Tavsif yo'q"
    rating   = detail.get("vote_average") or 0
    year     = (detail.get("release_date") or detail.get("first_air_date") or "")[:4]
    runtime  = detail.get("runtime") or 0
    genres   = ", ".join(g["name"] for g in detail.get("genres", [])[:3])
    cast     = ", ".join(a["name"] for a in detail.get("credits", {}).get("cast", [])[:4])
    poster   = detail.get("poster_path", "")

    text = f"🎬 *{title}* ({year})\n⭐ `{rating:.1f}/10`\n"
    if runtime:
        text += f"⏱ `{runtime} daqiqa`\n"
    if genres:
        text += f"🎭 {genres}\n"
    if cast:
        text += f"🌟 {cast}\n"
    text += f"\n{overview[:400]}"

    trailer_kb = []
    for v in detail.get("videos", {}).get("results", []):
        if v.get("site") == "YouTube" and "Trailer" in v.get("type", ""):
            trailer_kb = [[InlineKeyboardButton("▶️ Trailer",
                                                url=f"https://youtu.be/{v['key']}")]]
            break

    record(uid, "movies")
    img = await tmdb_poster(poster)
    if img:
        await q.message.reply_photo(photo=BytesIO(img), caption=text,
                                    parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(trailer_kb) if trailer_kb else None)
    else:
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
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
    tgf = await photo.get_file()
    buf = BytesIO()
    await tgf.download_to_memory(buf)
    title = await yandex_reverse(buf.getvalue())
    if not title:
        await msg.edit_text("❌ Rasmdan kino aniqlanmadi.")
        return
    await msg.edit_text(f"🔍 *{title}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)
    results = await tmdb_search(title)
    if not results:
        await msg.edit_text(f"❌ *{title}* — topilmadi.", parse_mode=ParseMode.MARKDOWN)
        return
    await msg.delete()
    text = f"🔍 Topildi: *{title}*\n\n"
    kb = []
    for r in results[:5]:
        mt = r.get("media_type", "movie")
        if mt not in ("movie", "tv"):
            continue
        rtitle = r.get("title") or r.get("name") or "?"
        year   = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        icon   = "🎬" if mt == "movie" else "📺"
        kb.append([InlineKeyboardButton(f"{icon} {rtitle[:40]} ({year})",
                                        callback_data=f"MOV|{r['id']}|{mt}")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def eff_select_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    eid = q.data.split("|", 1)[1]
    context.user_data["eff_wait"] = True
    context.user_data["eff_id"]   = eid
    ename = AUDIO_EFFECTS.get(eid, eid)
    await q.message.edit_text(f"✅ *{ename}* tanlandi!\n\nEndi audio faylni yuboring:",
                              parse_mode=ParseMode.MARKDOWN)


async def meff_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    eid   = q.data.split("|", 1)[1]
    ename = AUDIO_EFFECTS.get(eid, eid)
    uid   = q.from_user.id
    af = q.message.audio or q.message.voice
    if not af:
        await q.message.reply_text("❌ Audio topilmadi.")
        return
    msg = await q.message.reply_text(f"🎛 *{ename}* effekti qo'llanmoqda...",
                                     parse_mode=ParseMode.MARKDOWN)
    await _process_effect(q.message, uid, af, eid, ename, msg)


async def _process_effect(orig_msg, uid, af, eid, ename, msg):
    try:
        tgf = await af.get_file()
        buf = BytesIO()
        await tgf.download_to_memory(buf)
        buf.seek(0)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(buf.read())
            inp = tf.name

        def _apply():
            audio = AudioSegment.from_file(inp)
            out   = inp + "_fx.mp3"
            apply_effect(audio, eid).export(out, format="mp3", bitrate="192k")
            return out

        out = await asyncio.get_event_loop().run_in_executor(None, _apply)
        title = getattr(af, "title", None) or "Audio"
        with open(out, "rb") as f:
            await orig_msg.reply_audio(audio=f, title=f"{title} [{ename}]",
                                       caption=f"🎛 Effekt: *{ename}*",
                                       parse_mode=ParseMode.MARKDOWN)
        os.unlink(inp)
        os.unlink(out)
        await msg.delete()
        record(uid, "effects")
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")


async def _do_effect(update: Update, context: ContextTypes.DEFAULT_TYPE, af) -> bool:
    if not context.user_data.get("eff_wait"):
        return False
    eid = context.user_data.get("eff_id")
    if not eid:
        kb = []
        row = []
        for e_id, e_name in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(e_name, callback_data=f"EFF|{e_id}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        await update.message.reply_text("🎛 Effektni tanlang:",
                                        reply_markup=InlineKeyboardMarkup(kb))
        return True

    ename = AUDIO_EFFECTS.get(eid, eid)
    uid   = update.effective_user.id
    msg   = await update.message.reply_text(f"🎛 *{ename}* qo'llanmoqda...",
                                            parse_mode=ParseMode.MARKDOWN)
    await _process_effect(update.message, uid, af, eid, ename, msg)
    context.user_data["eff_wait"] = False
    context.user_data["eff_id"]   = None
    return True


async def cmd_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    context.user_data["eff_wait"] = True
    context.user_data["eff_id"]   = None
    kb = []
    row = []
    for eid, ename in AUDIO_EFFECTS.items():
        row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    await update.message.reply_text("🎛 *Effekt tanlang, keyin audio yuboring:*",
                                    parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def cmd_circle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    context.user_data["circle_wait"] = True
    await update.message.reply_text("⭕ *Video faylni yuboring (max 60 soniya):*",
                                    parse_mode=ParseMode.MARKDOWN)


async def _do_circle(update: Update, context: ContextTypes.DEFAULT_TYPE, vf) -> bool:
    if not context.user_data.get("circle_wait"):
        return False
    uid = update.effective_user.id
    msg = await update.message.reply_text("⭕ Dumaloq video tayyorlanmoqda...")
    try:
        tgf = await vf.get_file()
        buf = BytesIO()
        await tgf.download_to_memory(buf)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(buf.getvalue())
            inp = tf.name
        cp = await asyncio.get_event_loop().run_in_executor(None, make_circle_video, inp)
        if cp and os.path.exists(cp):
            dur = min(getattr(vf, "duration", 0) or 0, 60)
            with open(cp, "rb") as f:
                await update.message.reply_video_note(video_note=f, duration=dur, length=384)
            os.unlink(cp)
            await msg.delete()
            record(uid, "dl")
        else:
            await msg.edit_text("❌ Dumaloq video yaratib bo'lmadi.")
        if os.path.exists(inp):
            os.unlink(inp)
        context.user_data["circle_wait"] = False
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:200]}")
    return True


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    s = STATS[uid]
    total = s["dl"] + s["music"] + s["movies"] + s["effects"]
    await update.message.reply_text(
        f"📊 *Statistika*\n\n"
        f"📥 Yuklab: `{s['dl']}`\n"
        f"🎵 Musiqa: `{s['music']}`\n"
        f"🎬 Kino: `{s['movies']}`\n"
        f"🎛 Effektlar: `{s['effects']}`\n"
        f"📈 Jami: `{total}`",
        parse_mode=ParseMode.MARKDOWN)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Siz admin emassiz.")
        return
    await _admin_panel(update.message)


async def _admin_panel(message):
    total  = len(USER_INFO)
    banned = sum(1 for u in USER_INFO.values() if u.get("banned"))
    dl     = sum(v["dl"] for v in STATS.values())
    mu     = sum(v["music"] for v in STATS.values())
    text = (
        f"👑 *ADMIN PANEL*\n\n"
        f"👥 Foydalanuvchilar: `{total}` (🚫 `{banned}`)\n"
        f"📥 Yuklab olish: `{dl}`\n"
        f"🎵 Musiqa: `{mu}`"
    )
    kb = [
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="A|u|1")],
        [InlineKeyboardButton("📢 Hammaga xabar",    callback_data="A|bc")],
        [InlineKeyboardButton("🚫 Ban ro'yxati",     callback_data="A|bl")],
        [InlineKeyboardButton("📊 Top 10",           callback_data="A|top")],
        [InlineKeyboardButton("🗑 Statistikani tozala", callback_data="A|cls")],
    ]
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup(kb))


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not is_admin(uid):
        return
    parts  = q.data.split("|")
    action = parts[1]
    back   = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="A|main")]]

    if action == "main":
        total  = len(USER_INFO)
        banned = sum(1 for u in USER_INFO.values() if u.get("banned"))
        dl     = sum(v["dl"] for v in STATS.values())
        mu     = sum(v["music"] for v in STATS.values())
        text = (
            f"👑 *ADMIN PANEL*\n\n"
            f"👥 Foydalanuvchilar: `{total}` (🚫 `{banned}`)\n"
            f"📥 Yuklab: `{dl}` | 🎵 Musiqa: `{mu}`"
        )
        kb = [
            [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="A|u|1")],
            [InlineKeyboardButton("📢 Hammaga xabar",    callback_data="A|bc")],
            [InlineKeyboardButton("🚫 Ban ro'yxati",     callback_data="A|bl")],
            [InlineKeyboardButton("📊 Top 10",           callback_data="A|top")],
        ]
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "u":
        page = int(parts[2]) if len(parts) > 2 else 1
        pp   = 10
        users = list(USER_INFO.values())
        tp   = max(1, math.ceil(len(users) / pp))
        page = max(1, min(page, tp))
        chunk = users[(page - 1) * pp: page * pp]
        text = f"👥 *Foydalanuvchilar* ({len(users)}) — {page}/{tp}\n\n"
        kb = []
        for u in chunk:
            bid  = u["id"]
            icon = "🚫" if u.get("banned") else "✅"
            text += f"{icon} `{bid}` — {u['name']}\n"
            kb.append([
                InlineKeyboardButton("Unban" if u.get("banned") else "Ban",
                                     callback_data=f"A|tb|{bid}"),
                InlineKeyboardButton(f"📊 {u['name'][:15]}", callback_data=f"A|us|{bid}"),
            ])
        nav = []
        if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"A|u|{page-1}"))
        if page < tp: nav.append(InlineKeyboardButton("➡️", callback_data=f"A|u|{page+1}"))
        if nav: kb.append(nav)
        kb.extend(back)
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "tb":
        tid = int(parts[2])
        if tid in ADMIN_IDS:
            await q.answer("❌ Admin ni banlash mumkin emas!", show_alert=True)
            return
        USER_INFO[tid]["banned"] = not USER_INFO[tid].get("banned", False)
        st = "🚫 Bloklandi" if USER_INFO[tid]["banned"] else "✅ Blokdan chiqarildi"
        await q.answer(f"{st}", show_alert=True)

    elif action == "us":
        tid = int(parts[2])
        u = USER_INFO.get(tid, {})
        s = STATS.get(tid, {})
        total_a = s.get("dl",0) + s.get("music",0) + s.get("movies",0)
        st = "🚫 Bloklangan" if u.get("banned") else "✅ Faol"
        text = (
            f"👤 `{tid}`\n{u.get('name','?')}\n@{u.get('username','—')}\n"
            f"{st}\n📅 {s.get('joined','?')}\n📈 Jami: `{total_a}`"
        )
        kb = [
            [InlineKeyboardButton("Unban" if u.get("banned") else "Ban",
                                  callback_data=f"A|tb|{tid}")],
            [InlineKeyboardButton("📢 Xabar", callback_data=f"A|mu|{tid}")],
        ]
        kb.extend(back)
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "mu":
        tid = int(parts[2])
        context.user_data["msg_to"] = tid
        await q.message.edit_text(f"✉️ `{tid}` ga xabar yozing:",
                                  parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "bc":
        context.user_data["broadcast"] = True
        count = len([u for u in USER_INFO.values() if not u.get("banned")])
        await q.message.edit_text(
            f"📢 *Hammaga xabar*\n\nFaol: *{count}* ta\n\nXabar yozing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "bl":
        banned_list = [u for u in USER_INFO.values() if u.get("banned")]
        if not banned_list:
            await q.message.edit_text("✅ Ban ro'yxati bo'sh.",
                                      reply_markup=InlineKeyboardMarkup(back))
            return
        text = f"🚫 *Ban ro'yxati* ({len(banned_list)}):\n\n"
        kb = []
        for u in banned_list[:20]:
            text += f"• `{u['id']}` — {u['name']}\n"
            kb.append([InlineKeyboardButton(f"✅ {u['name'][:20]} unban",
                                            callback_data=f"A|tb|{u['id']}")])
        kb.extend(back)
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "top":
        top = sorted(STATS.items(),
                     key=lambda x: x[1]["dl"] + x[1]["music"] + x[1]["movies"],
                     reverse=True)[:10]
        text = "📊 *Top 10:*\n\n"
        for i, (u_id, s) in enumerate(top, 1):
            name = USER_INFO.get(u_id, {}).get("name", str(u_id))
            text += f"{i}. *{name}* — `{s['dl']+s['music']+s['movies']}`\n"
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "cls":
        kb = [[InlineKeyboardButton("✅ Ha", callback_data="A|clsok"),
               InlineKeyboardButton("❌ Yo'q", callback_data="A|main")]]
        await q.message.edit_text("⚠️ Barcha statistikani tozalaysizmi?",
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "clsok":
        for u_id in STATS:
            j = STATS[u_id].get("joined", "")
            STATS[u_id] = {"dl": 0, "music": 0, "movies": 0, "effects": 0, "joined": j}
        await q.message.edit_text("✅ Tozalandi.", reply_markup=InlineKeyboardMarkup(back))


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if not is_admin(uid):
        return False

    if context.user_data.get("broadcast"):
        context.user_data.pop("broadcast")
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
        await msg.edit_text(f"✅ Yuborildi: *{ok}* | ❌ Xato: *{fail}*",
                            parse_mode=ParseMode.MARKDOWN)
        return True

    if context.user_data.get("msg_to"):
        t_id = context.user_data.pop("msg_to")
        try:
            await update.message.copy_to(t_id)
            await update.message.reply_text(f"✅ `{t_id}` ga yuborildi.",
                                            parse_mode=ParseMode.MARKDOWN)
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
        if await admin_text_handler(update, context):
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
        if await _do_effect(update, context, af):
            return

    vf = update.message.video
    if not vf and update.message.document:
        mt = update.message.document.mime_type or ""
        if "video" in mt:
            vf = update.message.document
    if vf:
        if await _do_circle(update, context, vf):
            return

    text = (update.message.text or "").strip()
    if is_url(text):
        await handle_url(update, context)
        return

    if text and not text.startswith("/"):
        await update.message.reply_text(
            "💡 Nima qilishni bilmadim.\n\n"
            "📥 Video havolasini yuboring\n"
            "📸 Kino rasmini yuboring\n"
            "❓ /help — yordam")


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (TimedOut, NetworkError)):
        return
    print(f"[ERR] {context.error}")


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass


def _health_server():
    port = int(os.getenv("PORT", 8000))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("BOT_TOKEN sozlanmagan!")
        return

    threading.Thread(target=_health_server, daemon=True).start()
    print(f"Pro Media Bot ishga tushmoqda... PORT={os.getenv('PORT', 8000)}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     lambda u, c: u.message.reply_text(
        "/music — musiqa\n/movie — kino\n/trending — top\n/effect — effektlar\n/circle — dumaloq video\n/stats — statistika")))
    app.add_handler(CommandHandler("music",    cmd_music))
    app.add_handler(CommandHandler("movie",    cmd_movie))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("effect",   cmd_effect))
    app.add_handler(CommandHandler("circle",   cmd_circle))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("admin",    cmd_admin))

    app.add_handler(CallbackQueryHandler(platform_cb,    pattern=r"^PLT\|"))
    app.add_handler(CallbackQueryHandler(menu_cb,        pattern=r"^M\|"))
    app.add_handler(CallbackQueryHandler(dl_cb,          pattern=r"^DL\|"))
    app.add_handler(CallbackQueryHandler(music_cb,       pattern=r"^MU\|"))
    app.add_handler(CallbackQueryHandler(eff_select_cb,  pattern=r"^EFF\|"))
    app.add_handler(CallbackQueryHandler(meff_cb,        pattern=r"^MEF\|"))
    app.add_handler(CallbackQueryHandler(movie_cb,       pattern=r"^MOV\|"))
    app.add_handler(CallbackQueryHandler(admin_cb,       pattern=r"^A\|"))

    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.AUDIO | filters.VOICE |
        filters.VIDEO | filters.Document.ALL,
        handle_message))

    app.add_error_handler(error_handler)

    print("Bot tayyor! Polling boshlandi...\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
