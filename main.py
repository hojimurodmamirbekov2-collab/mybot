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
from PIL import Image, ImageDraw
from pydub import AudioSegment
from pydub.effects import normalize

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
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

STATS: dict     = defaultdict(lambda: {"dl": 0, "music": 0, "movies": 0, "effects": 0, "circle": 0, "joined": ""})
USER_INFO: dict = {}
URL_STORE: dict = {}
URL_CTR: list   = [0]
BOT_START_TIME  = datetime.now()
BANNED_USERS: set = set()

AUDIO_EFFECTS = {
    "bass":   "🔊 Bass Boost",
    "reverb": "🎵 Reverb",
    "echo":   "🌊 Echo",
    "vocal":  "🎤 Vocal Boost",
    "slow":   "🐢 Sekin (0.75x)",
    "fast":   "⚡ Tez (1.25x)",
    "rock":   "🎸 Rock EQ",
    "lofi":   "🎹 Lo-Fi",
}

PLATFORMS = [
    "▶️ YouTube", "📸 Instagram", "🎵 TikTok", "📌 Pinterest",
    "🐦 Twitter/X", "👥 Facebook", "🎬 Vimeo",
    "🎮 Twitch", "🤖 Reddit", "📹 Dailymotion",
]


def save_url(val: str) -> str:
    URL_CTR[0] += 1
    key = str(URL_CTR[0])
    URL_STORE[key] = val
    if len(URL_STORE) > 3000:
        for k in list(URL_STORE.keys())[:500]:
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


def uptime_str() -> str:
    delta = datetime.now() - BOT_START_TIME
    h, r  = divmod(int(delta.total_seconds()), 3600)
    m, s  = divmod(r, 60)
    return f"{h}s {m}d {s}s"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_banned(uid: int) -> bool:
    return uid in BANNED_USERS or USER_INFO.get(uid, {}).get("banned", False)


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


def ffmpeg_run(args: list) -> tuple[bool, str]:
    r = subprocess.run(["ffmpeg", "-y"] + args, capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def make_circle_video(inp: str) -> str | None:
    out = inp + "_circle.mp4"
    ok, err = ffmpeg_run([
        "-i", inp,
        "-vf",
        "scale=640:640:force_original_aspect_ratio=increase,"
        "crop=640:640,"
        "scale=384:384,"
        "format=yuv420p",
        "-c:v", "libx264", "-preset", "fast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-t", "60",
        out,
    ])
    if ok and os.path.exists(out) and os.path.getsize(out) > 500:
        return out
    return None


def apply_effect(audio: AudioSegment, eid: str) -> AudioSegment:
    if eid == "bass":
        low  = audio.low_pass_filter(250) + 8
        mid  = audio.high_pass_filter(250).low_pass_filter(4000)
        high = audio.high_pass_filter(4000) - 2
        audio = low.overlay(mid).overlay(high)

    elif eid == "reverb":
        delays = [(30, -5), (60, -9), (120, -13), (240, -18), (480, -22)]
        result = audio
        for delay, vol in delays:
            layer = AudioSegment.silent(duration=delay) + (audio + vol)
            pad   = len(result) - len(layer)
            if pad > 0:
                layer += AudioSegment.silent(duration=pad)
            result = result.overlay(layer)
        audio = result

    elif eid == "echo":
        e1 = AudioSegment.silent(350) + (audio - 7)
        e2 = AudioSegment.silent(700) + (audio - 13)
        for e in [e1, e2]:
            pad = len(audio) - len(e)
            if pad > 0:
                e += AudioSegment.silent(pad)
        audio = audio.overlay(e1).overlay(e2)

    elif eid == "vocal":
        audio = audio.high_pass_filter(400).low_pass_filter(7000) + 6

    elif eid == "slow":
        new_rate = int(audio.frame_rate * 0.75)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": new_rate}).set_frame_rate(audio.frame_rate)

    elif eid == "fast":
        new_rate = int(audio.frame_rate * 1.3)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": new_rate}).set_frame_rate(audio.frame_rate)

    elif eid == "rock":
        low  = audio.low_pass_filter(200) + 5
        mid  = audio.high_pass_filter(200).low_pass_filter(3000) - 1
        high = audio.high_pass_filter(3000) + 4
        audio = low.overlay(mid).overlay(high)

    elif eid == "lofi":
        audio = audio.low_pass_filter(3200)
        new_rate = int(audio.frame_rate * 0.97)
        audio = audio._spawn(audio.raw_data,
            overrides={"frame_rate": new_rate}).set_frame_rate(audio.frame_rate)
        audio = audio - 3

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

        ext   = "mp3" if audio_only else "mp4"
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
    return await asyncio.get_event_loop().run_in_executor(
        None, _dl_sync, url, audio_only, quality)


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
    return await asyncio.get_event_loop().run_in_executor(
        None, _yt_search_sync, query, limit)


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
            thumb   = item["album"]["images"][0]["url"] if item["album"]["images"] else ""
            results.append({
                "title":    item["name"],
                "artist":   artists,
                "album":    item["album"]["name"],
                "duration": item["duration_ms"] // 1000,
                "thumb":    thumb,
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
            loc  = r.headers.get("location", "")
            cbir = None
            if loc:
                cbir = "https://yandex.com" + loc if loc.startswith("/") else loc
            else:
                m = re.search(r'(?:url|href)=["\']([^"\']*cbir[^"\']*)["\']', r.text)
                if m:
                    u = m.group(1)
                    cbir = "https://yandex.com" + u if u.startswith("/") else u
            if not cbir:
                return None
            r2     = await c.get(cbir)
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
    await show_main_menu(update.message, user)


async def show_main_menu(message, user):
    name = user.first_name if hasattr(user, "first_name") else user.full_name
    text = (
        f"👋 *Salom, {name}!*\n\n"
        "📥 Havola yuboring — yuklab beraman\n"
        "📸 Kino rasmi yuboring — aniqlaydi\n"
        "⬇️ Yoki quyidagi menyudan tanlang:"
    )
    kb = [
        [InlineKeyboardButton("📥 Video yuklash",  callback_data="M|dl"),
         InlineKeyboardButton("🎵 Musiqa",          callback_data="M|music")],
        [InlineKeyboardButton("🎬 Kino qidirish",  callback_data="M|movie"),
         InlineKeyboardButton("🔥 Trending",        callback_data="M|trend")],
        [InlineKeyboardButton("🎛 Audio effektlar", callback_data="M|effect"),
         InlineKeyboardButton("⭕ Dumaloq video",   callback_data="M|circle")],
        [InlineKeyboardButton("📊 Statistika",      callback_data="M|stats"),
         InlineKeyboardButton("❓ Yordam",           callback_data="M|help")],
    ]
    if is_admin(user.id):
        kb.append([InlineKeyboardButton("👑 ADMIN PANEL", callback_data="ADM|home")])
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup(kb))


async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data.split("|", 1)[1]
    uid    = q.from_user.id
    back   = [[InlineKeyboardButton("🏠 Bosh menyu", callback_data="M|home")]]

    if action == "home":
        await q.message.delete()
        await show_main_menu(q.message, q.from_user)
        return

    elif action == "dl":
        plat_kb = []
        row = []
        for p in PLATFORMS:
            row.append(InlineKeyboardButton(p, callback_data=f"PLT|{p}"))
            if len(row) == 2:
                plat_kb.append(row)
                row = []
        if row:
            plat_kb.append(row)
        plat_kb.extend(back)
        await q.message.edit_text(
            "📥 *Platform tanlang yoki havolani to'g'ridan yuboring:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(plat_kb))

    elif action == "help":
        txt = (
            "❓ *Yordam*\n\n"
            "📥 *Video yuklab olish:*\n"
            "Havolani yuboring → format tanlang\n\n"
            "🎵 *Musiqa:*\n`/music The Weeknd Blinding Lights`\n\n"
            "🎬 *Kino qidirish:*\n`/movie Inception`\n\n"
            "🔥 *Trending kinolar:*\n`/trending`\n\n"
            "🎛 *Audio effekt qo'llash:*\n"
            "`/effect` → effekt tanlang → audio yuboring\n\n"
            "⭕ *Dumaloq video:*\n"
            "`/circle` → video yuboring\n\n"
            "📸 *Kino rasmidan qidirish:*\n"
            "Istalgan kino rasmini yuboring\n\n"
            "📊 *Statistika:* `/stats`"
        )
        await q.message.edit_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "stats":
        s     = STATS[uid]
        total = s["dl"] + s["music"] + s["movies"] + s["effects"] + s["circle"]
        txt = (
            f"📊 *Sizning statistikangiz*\n\n"
            f"📥 Video: `{s['dl']}`\n"
            f"🎵 Musiqa: `{s['music']}`\n"
            f"🎬 Kino: `{s['movies']}`\n"
            f"🎛 Effektlar: `{s['effects']}`\n"
            f"⭕ Dumaloq: `{s['circle']}`\n"
            f"━━━━━━━━━━\n"
            f"📈 Jami: `{total}`\n"
            f"📅 Qo'shilgan: `{s['joined'] or 'N/A'}`"
        )
        await q.message.edit_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "music":
        await q.message.edit_text(
            "🎵 *Musiqa qidirish*\n\n"
            "Quyidagi formatda yozing:\n`/music qo'shiq nomi`\n\n"
            "Misol: `/music Blinding Lights`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "movie":
        await q.message.edit_text(
            "🎬 *Kino qidirish*\n\n"
            "`/movie kino nomi`\n\nMisol: `/movie Inception`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "trend":
        await q.message.edit_text("⏳ Trending yuklanmoqda...",
                                  parse_mode=ParseMode.MARKDOWN)
        if not TMDB_API_KEY:
            await q.message.edit_text("❌ TMDB API kalit sozlanmagan.\n\nQarang: /tmdbhelp",
                                      reply_markup=InlineKeyboardMarkup(back))
            return
        d   = await tmdb_req("/trending/all/week")
        res = (d or {}).get("results", [])[:10]
        if not res:
            await q.message.edit_text("❌ Topilmadi.",
                                      reply_markup=InlineKeyboardMarkup(back))
            return
        txt = "🔥 *Haftalik TOP 10:*\n\n"
        kb  = []
        for i, r in enumerate(res, 1):
            mt    = r.get("media_type", "movie")
            title = r.get("title") or r.get("name") or "?"
            year  = (r.get("release_date") or r.get("first_air_date") or "")[:4]
            rat   = r.get("vote_average") or 0
            icon  = "🎬" if mt == "movie" else "📺"
            txt  += f"{i}. {icon} *{title}* ({year}) ⭐{rat:.1f}\n"
            kb.append([InlineKeyboardButton(f"{icon} {title[:40]}",
                                            callback_data=f"MOV|{r['id']}|{mt}")])
        kb.extend(back)
        await q.message.edit_text(txt, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "effect":
        context.user_data["eff_wait"] = True
        context.user_data["eff_id"]   = None
        kb  = []
        row = []
        for eid, ename in AUDIO_EFFECTS.items():
            row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.extend(back)
        await q.message.edit_text(
            "🎛 *Audio Effektlar*\n\nEffektni tanlang, keyin audio yuboring:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

    elif action == "circle":
        context.user_data["circle_wait"] = True
        await q.message.edit_text(
            "⭕ *Dumaloq Video*\n\n"
            "Video faylni yuboring (max 60 soniya)\n\n"
            "📌 Format: MP4, MOV, AVI",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))


async def platform_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pname = q.data.split("|", 1)[1]
    back  = [[InlineKeyboardButton("⬅️ Orqaga", callback_data="M|dl")]]
    await q.message.edit_text(
        f"📥 *{pname}*\n\n"
        "Havolani shu chatga yuboring — bot avtomatik aniqlaydi!\n\n"
        "Format: `https://...`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(back))


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = (update.message.text or "").strip()
    uid = update.effective_user.id
    if is_banned(uid):
        return
    key = save_url(url)
    kb  = [
        [InlineKeyboardButton("🎬 Best",    callback_data=f"DL|v|b|{key}"),
         InlineKeyboardButton("📺 720p",    callback_data=f"DL|v|7|{key}"),
         InlineKeyboardButton("📱 480p",    callback_data=f"DL|v|4|{key}")],
        [InlineKeyboardButton("🎵 MP3",     callback_data=f"DL|a|b|{key}"),
         InlineKeyboardButton("⭕ Dumaloq", callback_data=f"DL|c|b|{key}"),
         InlineKeyboardButton("ℹ️ Info",    callback_data=f"DL|i|b|{key}")],
    ]
    await update.message.reply_text(
        "⬇️ *Format tanlang:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb))


async def dl_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    if len(parts) < 4:
        return
    mode, qual_code, key = parts[1], parts[2], parts[3]
    url = load_url(key)
    if not url:
        await q.message.reply_text("❌ Havola topilmadi. Qaytadan yuboring.")
        return
    uid = q.from_user.id
    if is_banned(uid):
        return
    quality = {"b": "best", "7": "720", "4": "480"}.get(qual_code, "best")

    if mode == "i":
        msg = await q.message.reply_text("⏳ Ma'lumot olinmoqda...")
        await _show_info(msg, url)
        return

    msg = await q.message.reply_text("⏳ Yuklanmoqda... iltimos kuting")
    try:
        is_audio  = mode == "a"
        is_circle = mode == "c"
        result    = await dl_async(url, audio_only=(is_audio or is_circle), quality=quality)

        if not result.get("data"):
            await msg.edit_text("❌ Yuklab bo'lmadi. Havola noto'g'ri yoki himoyalangan.")
            return

        title    = result["title"] or "video"
        uploader = result["uploader"] or ""
        duration = result["duration"] or 0
        cap = f"🎬 *{title}*"
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
            record(uid, "music")

        elif is_circle:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                tf.write(result["data"])
                inp = tf.name
            cp = None
            try:
                cp = await asyncio.get_event_loop().run_in_executor(
                    None, make_circle_video, inp)
                if cp and os.path.exists(cp):
                    with open(cp, "rb") as f:
                        await q.message.reply_video_note(
                            video_note=f,
                            duration=min(duration, 60),
                            length=384)
                    record(uid, "circle")
                else:
                    await msg.edit_text("❌ Dumaloq video yaratib bo'lmadi.\nffmpeg xato.")
                    return
            finally:
                if os.path.exists(inp): os.unlink(inp)
                if cp and os.path.exists(cp): os.unlink(cp)
        else:
            await q.message.reply_video(
                video=data_io, caption=cap, parse_mode=ParseMode.MARKDOWN,
                thumbnail=thumb_io, duration=duration, supports_streaming=True)
            record(uid, "dl")

        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:300]}")


async def _show_info(msg, url: str):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        def _get():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await asyncio.get_event_loop().run_in_executor(None, _get)
        await msg.edit_text(
            f"ℹ️ *Ma'lumot*\n\n"
            f"📌 {info.get('title','?')}\n"
            f"👤 {info.get('uploader') or info.get('channel','?')}\n"
            f"⏱ {fmt_dur(info.get('duration',0))}\n"
            f"👁 {info.get('view_count') or '?'}\n"
            f"📅 {info.get('upload_date','?')}",
            parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"❌ {str(e)[:200]}")


async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "🎵 Misol: `/music Blinding Lights`",
            parse_mode=ParseMode.MARKDOWN)
        return
    msg    = await update.message.reply_text(f"🔍 *{query}* qidirilmoqda...",
                                             parse_mode=ParseMode.MARKDOWN)
    sp_res = await spotify_search(query)
    yt_res = await yt_search(query, 5)

    if not sp_res and not yt_res:
        await msg.edit_text("❌ Hech narsa topilmadi.")
        return

    await msg.delete()
    text = "🎵 *Natijalar:*\n\n"
    kb   = []

    if sp_res:
        for i, s in enumerate(sp_res[:5], 1):
            text += f"{i}. *{s['title']}* — {s['artist']} `{fmt_dur(s['duration'])}`\n"
            yt_q  = f"{s['title']} {s['artist']} audio"
            k     = save_url(yt_q)
            kb.append([InlineKeyboardButton(
                f"🎵 {s['title'][:25]} — {s['artist'][:12]}",
                callback_data=f"MU|s|{k}")])
    else:
        for i, yt in enumerate(yt_res[:5], 1):
            text += f"{i}. *{yt['title']}* `{fmt_dur(yt['duration'])}`\n"
            k     = save_url(yt["url"])
            kb.append([InlineKeyboardButton(
                f"🎵 {yt['title'][:38]}",
                callback_data=f"MU|u|{k}")])

    thumb_url = (sp_res[0].get("thumb") if sp_res else None) or \
                (yt_res[0].get("thumb") if yt_res else None)

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
    mode, key = parts[1], parts[2]
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

        eff_kb  = []
        row     = []
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
            caption="✅ Yuklandi!\n🎛 Effekt qo'llash uchun tugmani bosing:",
            reply_markup=InlineKeyboardMarkup(eff_kb))
        await msg.delete()
        record(uid, "music")
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:250]}")


async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    q = " ".join(context.args) if context.args else ""
    if not q:
        await update.message.reply_text(
            "🎬 Misol: `/movie Inception`",
            parse_mode=ParseMode.MARKDOWN)
        return
    await _search_movies(update, q)


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    if not TMDB_API_KEY:
        await update.message.reply_text(
            "❌ TMDB API kalit sozlanmagan.\n\nQo'llaniма: /tmdbhelp")
        return
    msg = await update.message.reply_text("⏳ Yuklanmoqda...")
    d   = await tmdb_req("/trending/all/week")
    res = (d or {}).get("results", [])[:10]
    if not res:
        await msg.edit_text("❌ Topilmadi.")
        return
    await msg.delete()
    text = "🔥 *Haftalik TOP 10:*\n\n"
    kb   = []
    for i, r in enumerate(res, 1):
        mt    = r.get("media_type", "movie")
        title = r.get("title") or r.get("name") or "?"
        year  = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        rat   = r.get("vote_average") or 0
        icon  = "🎬" if mt == "movie" else "📺"
        text += f"{i}. {icon} *{title}* ({year}) ⭐{rat:.1f}\n"
        kb.append([InlineKeyboardButton(f"{icon} {title[:40]}",
                                        callback_data=f"MOV|{r['id']}|{mt}")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def _search_movies(update, query_str: str):
    if not TMDB_API_KEY:
        await update.message.reply_text(
            "❌ TMDB API kalit sozlanmagan.\n\nQo'llanma: /tmdbhelp")
        return
    msg = await update.message.reply_text(
        f"🔍 *{query_str}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)
    p   = {"api_key": TMDB_API_KEY, "query": query_str, "include_adult": False}
    d   = await tmdb_req("/search/multi", p)
    res = (d or {}).get("results", [])[:6]
    if not res:
        await msg.edit_text("❌ Topilmadi.")
        return
    await msg.delete()
    text = "🎬 *Natijalar:*\n\n"
    kb   = []
    for r in res:
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
    parts  = q.data.split("|")
    mid    = int(parts[1])
    mtype  = parts[2]
    uid    = q.from_user.id
    msg    = await q.message.reply_text("⏳ Ma'lumot olinmoqda...")

    path   = f"/{'tv' if mtype=='tv' else 'movie'}/{mid}"
    detail = await tmdb_req(path, {"append_to_response": "credits,videos"})
    if not detail:
        await msg.edit_text("❌ Topilmadi.")
        return

    title    = detail.get("title") or detail.get("name") or "?"
    overview = (detail.get("overview") or "Tavsif yo'q")[:600]
    rating   = detail.get("vote_average") or 0
    year     = (detail.get("release_date") or detail.get("first_air_date") or "")[:4]
    runtime  = detail.get("runtime") or 0
    genres   = ", ".join(g["name"] for g in detail.get("genres", [])[:3])
    cast     = ", ".join(a["name"] for a in detail.get("credits", {}).get("cast", [])[:4])
    poster   = detail.get("poster_path", "")
    country  = ", ".join(c.get("name","") for c in detail.get("production_countries",[])[:2])
    lang     = detail.get("original_language","").upper()

    text = f"🎬 *{title}* ({year})\n"
    text += f"⭐ `{rating:.1f}/10`"
    if runtime:
        text += f"  ⏱ `{runtime} daq`"
    text += "\n"
    if genres:
        text += f"🎭 {genres}\n"
    if country:
        text += f"🌍 {country}  🗣 {lang}\n"
    if cast:
        text += f"🌟 {cast}\n"
    text += f"\n📖 {overview}"

    kb = []
    for v in detail.get("videos", {}).get("results", []):
        if v.get("site") == "YouTube" and "Trailer" in v.get("type", ""):
            kb = [[InlineKeyboardButton("▶️ Trailer ko'rish",
                                        url=f"https://youtu.be/{v['key']}")]]
            break

    record(uid, "movies")
    img = await tmdb_poster(poster)
    if img:
        await q.message.reply_photo(
            photo=BytesIO(img), caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    else:
        await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=InlineKeyboardMarkup(kb) if kb else None)
    await msg.delete()


async def cmd_tmdbhelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎬 *TMDB API kalitini qanday olish va sozlash:*\n\n"
        "1️⃣ https://www.themoviedb.org ga kiring\n"
        "2️⃣ Ro'yxatdan o'ting (Sign Up)\n"
        "3️⃣ *Settings* → *API* bo'limiga o'ting\n"
        "4️⃣ *Request an API key* → *Developer* tanlang\n"
        "5️⃣ Formani to'ldiring, API kalitni oling\n\n"
        "🔧 *Render.com da sozlash:*\n"
        "1. `kino-bot` servisingizni oching\n"
        "2. *Environment* bo'limiga kiring\n"
        "3. *Add Environment Variable* bosing:\n"
        "   `Key: TMDB_API_KEY`\n"
        "   `Value: sizning_kalit`\n"
        "4. *Save Changes* → bot qayta ishga tushadi"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    disable_web_page_preview=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid):
        return
    if not TMDB_API_KEY:
        await update.message.reply_text(
            "❌ TMDB API kalit sozlanmagan. Qo'llanma: /tmdbhelp")
        return
    msg   = await update.message.reply_text("🔍 Rasm tahlil qilinmoqda...")
    photo = update.message.photo[-1]
    tgf   = await photo.get_file()
    buf   = BytesIO()
    await tgf.download_to_memory(buf)
    title = await yandex_reverse(buf.getvalue())
    if not title:
        await msg.edit_text("❌ Rasmdan kino aniqlanmadi. Aniqroq rasm yuboring.")
        return
    await msg.edit_text(f"🔍 *{title}* qidirilmoqda...", parse_mode=ParseMode.MARKDOWN)
    p    = {"api_key": TMDB_API_KEY, "query": title, "include_adult": False}
    d    = await tmdb_req("/search/multi", p)
    res  = (d or {}).get("results", [])[:5]
    if not res:
        await msg.edit_text(f"❌ *{title}* — TMDB da topilmadi.", parse_mode=ParseMode.MARKDOWN)
        return
    await msg.delete()
    text = f"🔍 Topildi: *{title}*\n\n"
    kb   = []
    for r in res:
        mt = r.get("media_type", "movie")
        if mt not in ("movie", "tv"):
            continue
        rtitle = r.get("title") or r.get("name") or "?"
        year   = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        icon   = "🎬" if mt == "movie" else "📺"
        kb.append([InlineKeyboardButton(f"{icon} {rtitle[:38]} ({year})",
                                        callback_data=f"MOV|{r['id']}|{mt}")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(kb))


async def eff_select_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    eid   = q.data.split("|", 1)[1]
    ename = AUDIO_EFFECTS.get(eid, eid)
    context.user_data["eff_wait"] = True
    context.user_data["eff_id"]   = eid
    await q.message.edit_text(
        f"✅ *{ename}* tanlandi!\n\nEndi audio faylni yuboring 🎵",
        parse_mode=ParseMode.MARKDOWN)


async def meff_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    eid = q.data.split("|", 1)[1]
    uid = q.from_user.id
    af  = q.message.audio or q.message.voice
    if not af:
        await q.message.reply_text("❌ Audio topilmadi.")
        return
    msg = await q.message.reply_text(f"🎛 *{AUDIO_EFFECTS.get(eid,eid)}* qo'llanmoqda...",
                                     parse_mode=ParseMode.MARKDOWN)
    await _process_effect(q.message, uid, af, eid, msg)


async def _process_effect(orig_msg, uid, af, eid, msg):
    ename = AUDIO_EFFECTS.get(eid, eid)
    try:
        tgf = await af.get_file()
        buf = BytesIO()
        await tgf.download_to_memory(buf)
        buf.seek(0)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            tf.write(buf.read())
            inp = tf.name

        def _apply():
            try:
                audio = AudioSegment.from_file(inp)
            except Exception:
                audio = AudioSegment.from_mp3(inp)
            result = apply_effect(audio, eid)
            out    = inp + "_fx.mp3"
            result.export(out, format="mp3", bitrate="192k",
                          tags={"title": f"{getattr(af,'title','Audio')} [{ename}]"})
            return out

        out   = await asyncio.get_event_loop().run_in_executor(None, _apply)
        title = getattr(af, "title", None) or "Audio"
        dur   = getattr(af, "duration", 0) or 0

        with open(out, "rb") as f:
            await orig_msg.reply_audio(
                audio=f,
                title=f"{title} [{ename}]",
                duration=dur,
                caption=f"🎛 Effekt: *{ename}*",
                parse_mode=ParseMode.MARKDOWN)

        if os.path.exists(inp): os.unlink(inp)
        if os.path.exists(out): os.unlink(out)
        await msg.delete()
        record(uid, "effects")
    except Exception as e:
        await msg.edit_text(f"❌ Effekt xatosi: {str(e)[:250]}")


async def cmd_effect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    context.user_data["eff_wait"] = True
    context.user_data["eff_id"]   = None
    kb  = []
    row = []
    for eid, ename in AUDIO_EFFECTS.items():
        row.append(InlineKeyboardButton(ename, callback_data=f"EFF|{eid}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("🏠 Bosh menyu", callback_data="M|home")])
    await update.message.reply_text(
        "🎛 *Audio Effektlar*\n\nEffekt tanlang, keyin audio yuboring:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb))


async def cmd_circle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        return
    context.user_data["circle_wait"] = True
    await update.message.reply_text(
        "⭕ *Dumaloq Video*\n\nVideo faylni yuboring (max 60 soniya):",
        parse_mode=ParseMode.MARKDOWN)


async def _do_effect(update: Update, context: ContextTypes.DEFAULT_TYPE, af) -> bool:
    if not context.user_data.get("eff_wait"):
        return False
    uid = update.effective_user.id
    eid = context.user_data.get("eff_id")
    if not eid:
        kb  = []
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

    msg = await update.message.reply_text(
        f"🎛 *{AUDIO_EFFECTS.get(eid,eid)}* qo'llanmoqda...",
        parse_mode=ParseMode.MARKDOWN)
    await _process_effect(update.message, uid, af, eid, msg)
    context.user_data["eff_wait"] = False
    context.user_data["eff_id"]   = None
    return True


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
                await update.message.reply_video_note(
                    video_note=f, duration=dur, length=384)
            os.unlink(cp)
            await msg.delete()
            record(uid, "circle")
        else:
            await msg.edit_text(
                "❌ Dumaloq video yaratib bo'lmadi.\n"
                "ffmpeg o'rnatilgan bo'lishi kerak.")
        if os.path.exists(inp):
            os.unlink(inp)
        context.user_data["circle_wait"] = False
    except Exception as e:
        await msg.edit_text(f"❌ Xato: {str(e)[:250]}")
    return True


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid   = update.effective_user.id
    s     = STATS[uid]
    total = s["dl"] + s["music"] + s["movies"] + s["effects"] + s["circle"]
    await update.message.reply_text(
        f"📊 *Statistika*\n\n"
        f"📥 Video: `{s['dl']}`\n"
        f"🎵 Musiqa: `{s['music']}`\n"
        f"🎬 Kino: `{s['movies']}`\n"
        f"🎛 Effektlar: `{s['effects']}`\n"
        f"⭕ Dumaloq: `{s['circle']}`\n"
        f"📈 Jami: `{total}`",
        parse_mode=ParseMode.MARKDOWN)


# ═══════════════════════════════════════════
#            PRO ADMIN PANEL
# ═══════════════════════════════════════════

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Siz admin emassiz.")
        return
    await _adm_home(update.message)


async def _adm_home(message):
    total  = len(USER_INFO)
    banned = len(BANNED_USERS) + sum(1 for u in USER_INFO.values() if u.get("banned"))
    active = total - banned
    dl_all = sum(v["dl"] for v in STATS.values())
    mu_all = sum(v["music"] for v in STATS.values())
    mv_all = sum(v["movies"] for v in STATS.values())
    ef_all = sum(v["effects"] for v in STATS.values())
    ci_all = sum(v["circle"] for v in STATS.values())
    total_act = dl_all + mu_all + mv_all + ef_all + ci_all

    text = (
        "👑 *PRO ADMIN PANEL*\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Foydalanuvchilar: `{total}`\n"
        f"✅ Faol: `{active}`  🚫 Banlangan: `{banned}`\n"
        f"⏳ Uptime: `{uptime_str()}`\n\n"
        "📊 *Umumiy statistika:*\n"
        f"📥 Video: `{dl_all}`\n"
        f"🎵 Musiqa: `{mu_all}`\n"
        f"🎬 Kino: `{mv_all}`\n"
        f"🎛 Effektlar: `{ef_all}`\n"
        f"⭕ Dumaloq: `{ci_all}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Jami amallar: `{total_act}`"
    )
    kb = [
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="ADM|users|1"),
         InlineKeyboardButton("📊 Top 10",           callback_data="ADM|top")],
        [InlineKeyboardButton("🚫 Ban ro'yxati",     callback_data="ADM|banlist"),
         InlineKeyboardButton("🔍 ID qidirish",      callback_data="ADM|search")],
        [InlineKeyboardButton("📢 Hammaga xabar",    callback_data="ADM|bc"),
         InlineKeyboardButton("📌 Pinli xabar",      callback_data="ADM|pin")],
        [InlineKeyboardButton("📣 Kanal/Guruh post", callback_data="ADM|chpost"),
         InlineKeyboardButton("⚙️ Bot sozlamalari",  callback_data="ADM|settings")],
        [InlineKeyboardButton("🗑 Keshni tozala",   callback_data="ADM|clearcache"),
         InlineKeyboardButton("♻️ Statistika reset", callback_data="ADM|resetstats")],
        [InlineKeyboardButton("🏠 Bosh menyu",       callback_data="M|home")],
    ]
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=InlineKeyboardMarkup(kb))


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid   = q.from_user.id
    if not is_admin(uid):
        return
    parts  = q.data.split("|")
    action = parts[1]
    back   = [[InlineKeyboardButton("⬅️ Admin panel", callback_data="ADM|home")]]

    if action == "home":
        await q.message.delete()
        await _adm_home(q.message)
        return

    elif action == "users":
        page  = int(parts[2]) if len(parts) > 2 else 1
        pp    = 8
        users = list(USER_INFO.values())
        tp    = max(1, math.ceil(len(users) / pp))
        page  = max(1, min(page, tp))
        chunk = users[(page - 1) * pp: page * pp]

        text = f"👥 *Foydalanuvchilar* ({len(users)}) — {page}/{tp}\n\n"
        kb   = []
        for u in chunk:
            bid  = u["id"]
            icon = "🚫" if (bid in BANNED_USERS or u.get("banned")) else "✅"
            uname = f"@{u['username']}" if u.get("username") else u["name"]
            text += f"{icon} `{bid}` {uname}\n"
            s     = STATS.get(bid, {})
            total_u = s.get("dl",0) + s.get("music",0) + s.get("movies",0)
            kb.append([
                InlineKeyboardButton(f"{'✅ Unban' if (bid in BANNED_USERS or u.get('banned')) else '🚫 Ban'}",
                                     callback_data=f"ADM|tb|{bid}"),
                InlineKeyboardButton(f"📊 {u['name'][:14]} [{total_u}]",
                                     callback_data=f"ADM|uinfo|{bid}"),
                InlineKeyboardButton("✉️ Xabar",
                                     callback_data=f"ADM|msg|{bid}"),
            ])
        nav = []
        if page > 1:  nav.append(InlineKeyboardButton("⬅️", callback_data=f"ADM|users|{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{tp}", callback_data="ADM|home"))
        if page < tp: nav.append(InlineKeyboardButton("➡️", callback_data=f"ADM|users|{page+1}"))
        if nav: kb.append(nav)
        kb.extend(back)
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "uinfo":
        tid = int(parts[2])
        u   = USER_INFO.get(tid, {})
        s   = STATS.get(tid, {})
        tot = s.get("dl",0) + s.get("music",0) + s.get("movies",0) + s.get("effects",0)
        banned_st = "🚫 Banlangan" if (tid in BANNED_USERS or u.get("banned")) else "✅ Faol"
        text = (
            f"👤 *Foydalanuvchi*\n\n"
            f"🆔 `{tid}`\n"
            f"👤 {u.get('name','?')}\n"
            f"📛 @{u.get('username','—')}\n"
            f"{banned_st}\n"
            f"📅 Qo'shilgan: {s.get('joined','?')}\n\n"
            f"📥 Video: `{s.get('dl',0)}`\n"
            f"🎵 Musiqa: `{s.get('music',0)}`\n"
            f"🎬 Kino: `{s.get('movies',0)}`\n"
            f"🎛 Effektlar: `{s.get('effects',0)}`\n"
            f"📈 Jami: `{tot}`"
        )
        kb = [
            [InlineKeyboardButton(
                "✅ Unban" if (tid in BANNED_USERS or u.get("banned")) else "🚫 Ban",
                callback_data=f"ADM|tb|{tid}"),
             InlineKeyboardButton("✉️ Xabar yuborish", callback_data=f"ADM|msg|{tid}")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data=f"ADM|users|1")],
        ]
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "tb":
        tid = int(parts[2])
        if tid in ADMIN_IDS:
            await q.answer("❌ Adminni banlash mumkin emas!", show_alert=True)
            return
        if tid not in USER_INFO:
            USER_INFO[tid] = {"id": tid, "name": str(tid), "username": "", "banned": False}
        cur = USER_INFO[tid].get("banned", tid in BANNED_USERS)
        if cur:
            USER_INFO[tid]["banned"] = False
            BANNED_USERS.discard(tid)
            await q.answer("✅ Foydalanuvchi blokdan chiqarildi!", show_alert=True)
        else:
            USER_INFO[tid]["banned"] = True
            BANNED_USERS.add(tid)
            await q.answer("🚫 Foydalanuvchi bloklandi!", show_alert=True)

    elif action == "banlist":
        banned_list = [u for u in USER_INFO.values()
                       if u.get("banned") or u["id"] in BANNED_USERS]
        if not banned_list:
            await q.message.edit_text("✅ Ban ro'yxati bo'sh.",
                                      reply_markup=InlineKeyboardMarkup(back))
            return
        text = f"🚫 *Ban ro'yxati ({len(banned_list)} ta):*\n\n"
        kb   = []
        for u in banned_list[:20]:
            text += f"• `{u['id']}` — {u['name']}\n"
            kb.append([InlineKeyboardButton(f"✅ {u['name'][:20]} unban",
                                            callback_data=f"ADM|tb|{u['id']}")])
        kb.extend(back)
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "top":
        top = sorted(STATS.items(),
                     key=lambda x: x[1].get("dl",0)+x[1].get("music",0)+x[1].get("movies",0),
                     reverse=True)[:10]
        text = "📊 *TOP 10 foydalanuvchilar:*\n\n"
        medals = ["🥇","🥈","🥉"]
        for i, (u_id, s) in enumerate(top, 1):
            name = USER_INFO.get(u_id, {}).get("name", str(u_id))
            tot  = s.get("dl",0) + s.get("music",0) + s.get("movies",0)
            med  = medals[i-1] if i <= 3 else f"{i}."
            text += f"{med} *{name}* — `{tot}` amal\n"
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(back))

    elif action == "search":
        context.user_data["adm_search"] = True
        await q.message.edit_text(
            "🔍 *ID yoki username qidirish*\n\nFoydalanuvchi ID yoki @username yozing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "bc":
        context.user_data["adm_bc"] = True
        active = len([u for u in USER_INFO.values() if not u.get("banned")])
        await q.message.edit_text(
            f"📢 *Hammaga xabar yuborish*\n\n"
            f"Faol foydalanuvchilar: *{active}* ta\n\n"
            "Xabar yozing (matn, rasm, video — har qanday):",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "pin":
        context.user_data["adm_pin"] = True
        await q.message.edit_text(
            "📌 *Pinli xabar*\n\nXabar yozing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "chpost":
        context.user_data["adm_chpost"] = True
        await q.message.edit_text(
            "📣 *Kanal/Guruh ga post*\n\n"
            "Avval Chat ID ni yozing:\n"
            "(Misol: `-1001234567890`)\n\n"
            "Chat ID ni topish: @userinfobot ga forward qiling",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "msg":
        tid = int(parts[2])
        context.user_data["adm_msg_to"] = tid
        name = USER_INFO.get(tid, {}).get("name", str(tid))
        await q.message.edit_text(
            f"✉️ *{name}* (`{tid}`) ga xabar yozing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "settings":
        bot_info = await q.get_bot().get_me()
        text = (
            f"⚙️ *Bot sozlamalari*\n\n"
            f"🤖 Bot: @{bot_info.username}\n"
            f"🆔 ID: `{bot_info.id}`\n"
            f"📊 TMDB: {'✅' if TMDB_API_KEY else '❌ sozlanmagan'}\n"
            f"🎵 Spotify: {'✅' if (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET) else '❌ sozlanmagan'}\n"
            f"👥 Adminlar: {len(ADMIN_IDS)} ta\n"
            f"💾 URL kesh: `{len(URL_STORE)}` ta\n"
            f"⏳ Uptime: `{uptime_str()}`\n\n"
            f"*Adminlar:*\n" +
            "\n".join(f"• `{a}`" for a in ADMIN_IDS)
        )
        kb = [
            [InlineKeyboardButton("📖 TMDB qo'llanma", callback_data="ADM|tmdbguide")],
        ]
        kb.extend(back)
        await q.message.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup(kb))

    elif action == "tmdbguide":
        await q.message.edit_text(
            "🎬 *TMDB API sozlash:*\n\n"
            "1. themoviedb.org ga boring\n"
            "2. Ro'yxatdan o'ting\n"
            "3. Settings → API → Request key\n"
            "4. Render → Environment → `TMDB_API_KEY` = kalit",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "clearcache":
        count = len(URL_STORE)
        URL_STORE.clear()
        URL_CTR[0] = 0
        await q.message.edit_text(
            f"✅ Kesh tozalandi! ({count} ta yozuv o'chirildi)",
            reply_markup=InlineKeyboardMarkup(back))

    elif action == "resetstats":
        kb = [[InlineKeyboardButton("✅ Ha, tozala", callback_data="ADM|resetok"),
               InlineKeyboardButton("❌ Bekor",     callback_data="ADM|home")]]
        await q.message.edit_text(
            "⚠️ *Barcha statistikani tozalaysizmi?*\n\nBu amalni qaytarib bo'lmaydi!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb))

    elif action == "resetok":
        for u_id in list(STATS.keys()):
            j = STATS[u_id].get("joined", "")
            STATS[u_id] = {"dl": 0, "music": 0, "movies": 0, "effects": 0, "circle": 0, "joined": j}
        await q.message.edit_text(
            "✅ Barcha statistika tozalandi!",
            reply_markup=InlineKeyboardMarkup(back))


async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid  = update.effective_user.id
    if not is_admin(uid):
        return False
    text = (update.message.text or "").strip()

    if context.user_data.get("adm_search"):
        context.user_data.pop("adm_search")
        found = None
        for u in USER_INFO.values():
            if str(u["id"]) == text or (u.get("username","").lower() == text.lstrip("@").lower()):
                found = u
                break
        if not found:
            await update.message.reply_text("❌ Topilmadi.")
        else:
            tid = found["id"]
            s   = STATS.get(tid, {})
            tot = s.get("dl",0)+s.get("music",0)+s.get("movies",0)+s.get("effects",0)
            banned_st = "🚫 Banlangan" if (tid in BANNED_USERS or found.get("banned")) else "✅ Faol"
            t = (
                f"👤 *{found['name']}*\n"
                f"🆔 `{tid}`\n@{found.get('username','—')}\n"
                f"{banned_st}\n📅 {s.get('joined','?')}\n📈 Jami: `{tot}`"
            )
            kb = [
                [InlineKeyboardButton(
                    "✅ Unban" if (tid in BANNED_USERS or found.get("banned")) else "🚫 Ban",
                    callback_data=f"ADM|tb|{tid}"),
                 InlineKeyboardButton("✉️ Xabar", callback_data=f"ADM|msg|{tid}")],
            ]
            await update.message.reply_text(t, parse_mode=ParseMode.MARKDOWN,
                                            reply_markup=InlineKeyboardMarkup(kb))
        return True

    if context.user_data.get("adm_bc"):
        context.user_data.pop("adm_bc")
        targets = [u_id for u_id, u in USER_INFO.items() if not u.get("banned") and u_id != uid]
        msg = await update.message.reply_text(f"📢 {len(targets)} ta foydalanuvchiga yuborilmoqda...")
        ok = fail = 0
        for t_id in targets:
            try:
                await update.message.copy_to(t_id)
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await msg.edit_text(f"✅ Yuborildi: *{ok}*  ❌ Xato: *{fail}*",
                            parse_mode=ParseMode.MARKDOWN)
        return True

    if context.user_data.get("adm_pin"):
        context.user_data.pop("adm_pin")
        try:
            sent = await update.message.copy_to(update.message.chat_id)
            await update.get_bot().pin_chat_message(update.message.chat_id, sent.message_id)
            await update.message.reply_text("✅ Xabar pin qilindi!")
        except Exception as e:
            await update.message.reply_text(f"❌ Pin qilib bo'lmadi: {e}")
        return True

    if context.user_data.get("adm_chpost"):
        if "adm_chpost_id" not in context.user_data:
            context.user_data["adm_chpost_id"] = text
            await update.message.reply_text(
                f"✅ Chat ID: `{text}`\n\nEndi yubormoqchi bo'lgan xabarni yozing:",
                parse_mode=ParseMode.MARKDOWN)
        else:
            chat_id = context.user_data.pop("adm_chpost_id")
            context.user_data.pop("adm_chpost")
            try:
                await update.message.copy_to(int(chat_id))
                await update.message.reply_text(f"✅ `{chat_id}` ga yuborildi!",
                                                parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await update.message.reply_text(f"❌ Yuborib bo'lmadi: {e}")
        return True

    if context.user_data.get("adm_msg_to"):
        t_id = context.user_data.pop("adm_msg_to")
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
    uid  = user.id
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
            "💡 Nima yuborganingizni bilmadim.\n\n"
            "📥 Video havolasini yuboring\n"
            "📸 Kino rasmini yuboring\n"
            "❓ /help — yordam\n"
            "🏠 /start — bosh menyu")


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
    print(f"Pro Media Bot ishga tushmoqda... PORT={os.getenv('PORT',8000)}")
    if not TMDB_API_KEY:
        print("ESLATMA: TMDB_API_KEY yo'q — kino funksiyasi ishlamaydi")
    if not SPOTIFY_CLIENT_ID:
        print("ESLATMA: Spotify sozlanmagan")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     lambda u, c: u.message.reply_text(
        "📥 /start — bosh menyu\n🎵 /music — musiqa\n🎬 /movie — kino\n"
        "🔥 /trending — top\n🎛 /effect — effektlar\n⭕ /circle — dumaloq\n"
        "📊 /stats — statistika\n🎬 /tmdbhelp — TMDB sozlash")))
    app.add_handler(CommandHandler("music",    cmd_music))
    app.add_handler(CommandHandler("movie",    cmd_movie))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("effect",   cmd_effect))
    app.add_handler(CommandHandler("circle",   cmd_circle))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CommandHandler("tmdbhelp", cmd_tmdbhelp))

    app.add_handler(CallbackQueryHandler(platform_cb, pattern=r"^PLT\|"))
    app.add_handler(CallbackQueryHandler(menu_cb,     pattern=r"^M\|"))
    app.add_handler(CallbackQueryHandler(dl_cb,       pattern=r"^DL\|"))
    app.add_handler(CallbackQueryHandler(music_cb,    pattern=r"^MU\|"))
    app.add_handler(CallbackQueryHandler(eff_select_cb, pattern=r"^EFF\|"))
    app.add_handler(CallbackQueryHandler(meff_cb,     pattern=r"^MEF\|"))
    app.add_handler(CallbackQueryHandler(movie_cb,    pattern=r"^MOV\|"))
    app.add_handler(CallbackQueryHandler(admin_cb,    pattern=r"^ADM\|"))

    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.AUDIO | filters.VOICE |
        filters.VIDEO | filters.Document.ALL,
        handle_message))

    app.add_error_handler(error_handler)

    print("Bot tayyor! Polling boshlandi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
