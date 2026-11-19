import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# -------------------- Configuration --------------------
API_ID = int(os.environ.get("API_ID", "0").strip() or "0")
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "./downloads").strip() or "./downloads")
THUMB_DIR = DOWNLOAD_DIR / "thumbnails"
COOKIE_FILE = Path(os.environ.get("COOKIE_FILE", "cookies.txt").strip() or "cookies.txt")
SETTINGS_FILE = Path(os.environ.get("SETTINGS_FILE", "user_settings.json"))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH, BOT_TOKEN എന്നിവ Environment Variables ആയി സെറ്റ് ചെയ്യുക!")

app = Client("sunnxt_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

USER_SHOWS: dict[int, dict] = {}
USER_SETTINGS: dict[int, dict] = {}
PENDING_JOBS: dict[str, dict] = {}
ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}
CANCEL_EVENTS: dict[str, threading.Event] = {}

RANGE_PATTERN = re.compile(r"^/(\d+):(\d+)(?:-(\d+))?$")

QUALITY_OPTIONS = {
    "144p": ("144p", "bestvideo[height<=144]+bestaudio/best[height<=144]/best"),
    "240p": ("240p", "bestvideo[height<=240]+bestaudio/best[height<=240]/best"),
    "360p": ("360p", "bestvideo[height<=360]+bestaudio/best[height<=360]/best"),
    "480p": ("480p", "bestvideo[height<=480]+bestaudio/best[height<=480]/best"),
    "576p": ("576p", "bestvideo[height<=576]+bestaudio/best[height<=576]/best"),
    "720p": ("720p", "bestvideo[height<=720]+bestaudio/best[height<=720]/best"),
    "1080p": ("1080p", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"),
    "1440p": ("1440p (2K)", "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best"),
    "2160p": ("2160p (4K)", "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best"),
    "mp3": ("MP3 Audio", "bestaudio/best"),
}

DEFAULT_SETTINGS = {
    "auto_thumb": True,
    "custom_thumb": None,
    "custom_name": None,
    "mp3_quality": "192K",
}


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for uid, values in data.items():
                if str(uid).isdigit() and isinstance(values, dict):
                    USER_SETTINGS[int(uid)] = {**DEFAULT_SETTINGS, **values}
    except (OSError, json.JSONDecodeError):
        pass


def save_settings():
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps({str(k): v for k, v in USER_SETTINGS.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def get_user_settings(user_id: int) -> dict:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()
    else:
        USER_SETTINGS[user_id] = {**DEFAULT_SETTINGS, **USER_SETTINGS[user_id]}
    return USER_SETTINGS[user_id]


def build_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    s = get_user_settings(user_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Auto Thumb: ON" if s["auto_thumb"] else "❌ Auto Thumb: OFF", callback_data="set|toggle_autothumb")],
        [InlineKeyboardButton("🖼️ Custom Thumb: Set" if s["custom_thumb"] else "🖼️ Custom Thumb: None", callback_data="set|clear_thumb")],
        [InlineKeyboardButton(f"✏️ Name: {s['custom_name']}" if s["custom_name"] else "✏️ Custom Name: Default", callback_data="set|clear_name")],
        [InlineKeyboardButton(f"🎵 MP3 Bitrate: {s['mp3_quality']}", callback_data="set|toggle_mp3")],
        [InlineKeyboardButton("🔍 Check cookies.txt", callback_data="set|cookies")],
    ])


def format_settings_text(user_id: int) -> str:
    s = get_user_settings(user_id)
    show = USER_SHOWS.get(user_id)
    return (
        "⚙️ **Sun NXT Video Downloading Bot Settings**\n\n"
        f"• **Active Link:** `{show['slug'] if show else 'None'}`\n"
        f"• **Auto Thumbnail:** `{'ON' if s['auto_thumb'] else 'OFF'}`\n"
        f"• **Custom Thumbnail:** `{'Set' if s['custom_thumb'] else 'Not set'}`\n"
        f"• **Custom Name:** `{s['custom_name'] or 'Default'}`\n"
        f"• **MP3 Bitrate:** `{s['mp3_quality']}`\n"
        f"• **Cookies:** `{COOKIE_FILE}`\n"
        f"• **Download Folder:** `{DOWNLOAD_DIR}`\n\n"
        "ഫോട്ടോ അയച്ചാൽ Custom Thumbnail ആയി സേവ് ചെയ്യും. `-n പേര്` ഉപയോഗിച്ച് ഫയൽ പേര് നൽകാം."
    )


def build_quality_keyboard(job_id: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("144p", callback_data=f"dl|{job_id}|144p"), InlineKeyboardButton("240p", callback_data=f"dl|{job_id}|240p"), InlineKeyboardButton("360p", callback_data=f"dl|{job_id}|360p")],
        [InlineKeyboardButton("480p", callback_data=f"dl|{job_id}|480p"), InlineKeyboardButton("576p", callback_data=f"dl|{job_id}|576p"), InlineKeyboardButton("720p", callback_data=f"dl|{job_id}|720p")],
        [InlineKeyboardButton("1080p", callback_data=f"dl|{job_id}|1080p"), InlineKeyboardButton("1440p (2K)", callback_data=f"dl|{job_id}|1440p")],
        [InlineKeyboardButton("2160p (4K)", callback_data=f"dl|{job_id}|2160p"), InlineKeyboardButton("🎵 MP3 Audio", callback_data=f"dl|{job_id}|mp3")],
        [InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel_job|{job_id}|{user_id}")],
    ])


def check_cookies_file(cookie_path=COOKIE_FILE) -> tuple[bool, str]:
    path = Path(cookie_path)
    if not str(path).strip():
        return False, "COOKIE_FILE പാത്ത് നൽകിയിട്ടില്ല."
    if path.is_dir():
        return False, f"`{path}` ഒരു ഫോൾഡറാണ്; cookies.txt ഒരു ഫയലായിരിക്കണം."
    if not path.is_file():
        return False, f"`{path}` കണ്ടെത്തിയില്ല. bot.py ഉള്ള ഫോൾഡറിൽ cookies.txt ഇടുക."
    size = path.stat().st_size
    if size == 0:
        return False, f"`{path}` കാലിയാണ് (0 bytes)."
    return True, f"`{path}` ലഭ്യമാണ് ({size} bytes)."


def extract_sunnxt_info(url: str) -> dict | None:
    cleaned = url.strip().rstrip(".,)")
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (host == "sunnxt.com" or host.endswith(".sunnxt.com")):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    # Example: /<slug>/detail/<detail-id>/<content-id>[/<season>-<episode>]
    try:
        detail_index = next(i for i, part in enumerate(parts) if part.lower() == "detail")
    except StopIteration:
        return None
    if len(parts) < detail_index + 3:
        return None
    season_episode = None
    if len(parts) > detail_index + 3 and re.fullmatch(r"\d+-\d+", parts[detail_index + 3]):
        season_episode = parts[detail_index + 3]
    detail_id = parts[detail_index + 1]
    content_id = parts[detail_index + 2]
    slug_part = parts[0] if detail_index > 0 else "sunnxt_video"
    slug = re.sub(r"-nov-\d{1,2}-\d{4}.*$", "", slug_part, flags=re.I)
    return {
        "url": cleaned,
        "slug": slug or slug_part,
        "content_id": content_id,
        "detail_id": detail_id,
        "season_episode": season_episode,
    }


def parse_episode_command(text: str) -> dict | None:
    match = RANGE_PATTERN.fullmatch(text.strip())
    if not match:
        return None
    season, first = int(match.group(1)), int(match.group(2))
    last = int(match.group(3)) if match.group(3) else first
    if season < 1 or first < 1 or last < first or last - first > 49:
        return None
    return {"season": season, "episodes": list(range(first, last + 1))}


def safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip().strip(".")
    return value[:120] or "sunnxt_video"


def generate_thumbnail(video_path: str, user_id: int) -> str | None:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMB_DIR / f"auto_{user_id}.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-ss", "00:00:05", "-i", str(video_path), "-vframes", "1", "-q:v", "2", str(thumb_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
        return str(thumb_path) if thumb_path.is_file() and thumb_path.stat().st_size else None
    except (OSError, subprocess.SubprocessError):
        return None


def run_yt_dlp(url: str, out_file: str, quality_key: str, mp3_bitrate: str, cancel_event: threading.Event, job_id: str) -> tuple[bool, str, str | None]:
    """Runs yt-dlp for formats the source makes available without DRM circumvention."""
    if not shutil.which("ffmpeg") and quality_key != "mp3":
        return False, "Video merge/thumbnail പ്രവർത്തനങ്ങൾക്ക് FFmpeg ഇൻസ്റ്റാൾ ചെയ്യണം.", None
    if not shutil.which("ffmpeg") and quality_key == "mp3":
        return False, "MP3 conversion-ന് ffmpeg ഇൻസ്റ്റാൾ ചെയ്യണം.", None

    cookie_ok, cookie_msg = check_cookies_file()
    if not cookie_ok:
        return False, f"Cookies പരിശോധിക്കൽ പരാജയപ്പെട്ടു: {cookie_msg}", None

    _, selector = QUALITY_OPTIONS[quality_key]
    output = str(Path(out_file).with_suffix(".%(ext)s"))
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", "--newline", "-f", selector,
           "--cookies", str(COOKIE_FILE), "-o", output,
           "--add-header", "Referer: https://www.sunnxt.com/",
           "--add-header", "User-Agent: Mozilla/5.0"]
    if quality_key == "mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", mp3_bitrate]
    else:
        cmd += ["--merge-output-format", "mp4"]
    cmd.append(url)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        ACTIVE_PROCESSES[job_id] = proc
        lines = []
        while proc.poll() is None:
            if cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return False, "ഉപയോക്താവ് ഡൗൺലോഡ് ക്യാൻസൽ ചെയ്തു.", None
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                lines.append(line.strip())
        if proc.stdout:
            lines.extend(x.strip() for x in proc.stdout.readlines())
        if proc.returncode != 0:
            error = "\n".join(x for x in lines if x)[-700:] or "yt-dlp ഡൗൺലോഡ് പരാജയപ്പെടുത്തി."
            # Do not label every extractor/format error as DRM. Only report DRM when
            # yt-dlp explicitly identifies DRM/encryption; preserve other diagnostics.
            if re.search(r"\b(drm|widevine|encrypted|encryption)\b", error, re.I):
                error = (
                    "സ്ട്രീം DRM/encryption സംരക്ഷണമുള്ളതായി yt-dlp റിപ്പോർട്ട് ചെയ്തു. "
                    "ബോട്ട് DRM മറികടക്കുകയോ ഡീക്രിപ്റ്റ് ചെയ്യുകയോ ചെയ്യില്ല. "
                    "ആപ്പ് playback ലഭ്യമാണെന്നത് downloadable format ലഭ്യമാണെന്ന് ഉറപ്പാക്കുന്നില്ല.\n\n"
                    f"yt-dlp വിശദാംശം: {error[-450:]}"
                )
            elif re.search(r"no formats found|requested format is not available", error, re.I):
                error = (
                    "yt-dlp-ന് ഈ ലിങ്കിൽ പിന്തുണയ്ക്കുന്ന downloadable format കണ്ടെത്താനായില്ല. "
                    "ഇത് മാത്രം DRM ആണെന്ന് തെളിയിക്കുന്നില്ല; Sun NXT extractor/format support "
                    "അല്ലെങ്കിൽ login/cookie പ്രശ്നമായിരിക്കാം.\n\n"
                    f"yt-dlp വിശദാംശം: {error[-450:]}"
                )
            elif re.search(r"unsupported url", error, re.I):
                error = (
                    "ഈ Sun NXT URL നിലവിലെ yt-dlp പതിപ്പ് പിന്തുണയ്ക്കുന്നില്ല. "
                    "ഇത് DRM ആണെന്ന് മാത്രം അർത്ഥമാക്കുന്നില്ല.\n\n"
                    f"yt-dlp വിശദാംശം: {error[-450:]}"
                )
            return False, error, None
        parent = Path(out_file).parent
        stem = Path(out_file).stem
        candidates = sorted(parent.glob(stem + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
        final = str(candidates[0]) if candidates else None
        return True, "ഡൗൺലോഡ് പൂർത്തിയായി.", final
    except FileNotFoundError:
        return False, "Python yt-dlp package കണ്ടെത്തിയില്ല. requirements.txt ഇൻസ്റ്റാൾ ചെയ്യുക.", None
    except Exception as exc:
        return False, str(exc)[:500], None
    finally:
        ACTIVE_PROCESSES.pop(job_id, None)


async def create_and_send_quality_menu(message: Message, user_id: int, show: dict, season: int = 1, episodes=None):
    episodes = episodes or [1]
    job_id = f"{user_id}_{message.id}_{int(time.time())}"
    PENDING_JOBS[job_id] = {"user_id": user_id, "show": show, "season": season, "episodes": episodes}
    ep_label = f"E{episodes[0]}" if len(episodes) == 1 else f"E{episodes[0]}–E{episodes[-1]}"
    await message.reply_text(
        f"🎬 **Sun NXT Link Detected**\n📺 `{show['slug']}`\n🆔 Content ID: `{show['content_id']}`\n📺 Season {season} ({ep_label})\n\nQuality തിരഞ്ഞെടുക്കുക:",
        reply_markup=build_quality_keyboard(job_id, user_id),
    )


@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message: Message):
    await message.reply_text(
        "🎬 **Sun NXT Video Downloading Bot**\n\n"
        "Sun NXT ലിങ്ക് അയക്കുക. പിന്തുണയ്ക്കുന്ന ലിങ്കാണെങ്കിൽ quality options കാണിക്കും.\n\n"
        "**Commands**\n"
        "• `/start` — Start bot\n• `/settings` — Settings\n• `/checkcookies` — cookies.txt പരിശോധിക്കുക\n• `/restart` — Restart bot\n\n"
        "**Episode format**\n`/1:5` — Season 1, Episode 5\n`/1:1-10` — Season 1, Episodes 1–10\n\n"
        "ലിങ്കിനൊപ്പം `-n പേര്` ചേർത്ത് ഫയൽ പേര് നൽകാം. DRM സംരക്ഷിത സ്ട്രീമുകൾ ഡീക്രിപ്റ്റ് ചെയ്യുകയോ മറികടക്കുകയോ ചെയ്യില്ല."
    )


@app.on_message(filters.command("settings") & filters.private)
async def settings_cmd(_, message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.reply_text(format_settings_text(uid), reply_markup=build_settings_keyboard(uid))


@app.on_message(filters.command("restart") & filters.private)
async def restart_cmd(_, message: Message):
    await message.reply_text("🔄 ബോട്ട് റീസ്റ്റാർട്ട് ചെയ്യുന്നു...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


@app.on_message(filters.command("checkcookies") & filters.private)
async def check_cookies_cmd(_, message: Message):
    ok, msg = check_cookies_file()
    await message.reply_text(("✅ " if ok else "❌ ") + msg)


@app.on_message(filters.photo & filters.private)
async def photo_thumb_handler(_, message: Message):
    uid = message.from_user.id if message.from_user else 0
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    path = THUMB_DIR / f"custom_{uid}.jpg"
    await message.download(file_name=str(path))
    get_user_settings(uid)["custom_thumb"] = str(path)
    save_settings()
    await message.reply_text("✅ Custom Thumbnail സേവ് ചെയ്തു!")


@app.on_message(filters.text & filters.private)
async def text_handler(_, message: Message):
    text = (message.text or "").strip()
    uid = message.from_user.id if message.from_user else 0
    settings = get_user_settings(uid)

    url_match = re.search(r"https?://[^\s]+", text)
    if url_match:
        info = extract_sunnxt_info(url_match.group(0))
        if not info:
            await message.reply_text("❌ സാധുവായ Sun NXT `/detail/` ലിങ്ക് അയക്കുക.")
            return
        name_match = re.search(r"(?:^|\s)-n(?:\s+|=)(.+?)\s*$", text, flags=re.I)
        if name_match:
            settings["custom_name"] = safe_name(name_match.group(1))
            save_settings()
        USER_SHOWS[uid] = info
        season, episode = 1, 1
        if info.get("season_episode"):
            season, episode = (int(x) for x in info["season_episode"].split("-", 1))
        await create_and_send_quality_menu(message, uid, info, season=season, episodes=[episode])
        return

    parsed = parse_episode_command(text)
    if parsed:
        show = USER_SHOWS.get(uid)
        if not show:
            await message.reply_text("ആദ്യം Sun NXT ലിങ്ക് അയക്കുക.")
            return
        if len(parsed["episodes"]) > 1:
            await message.reply_text("⚠️ ഈ URL-ൽ ഒരു content ID മാത്രമാണ് ലഭിച്ചത്. മറ്റു എപ്പിസോഡുകളുടെ യഥാർത്ഥ URL/ID ഇല്ലാതെ ഒരേ വീഡിയോ പലതവണ ഡൗൺലോഡ് ചെയ്യാതിരിക്കാൻ range download തടഞ്ഞിരിക്കുന്നു. ഓരോ എപ്പിസോഡിന്റെയും ലിങ്ക് അയക്കുക.")
            return
        await create_and_send_quality_menu(message, uid, show, parsed["season"], parsed["episodes"])
        return

    if text.startswith("/setname "):
        settings["custom_name"] = safe_name(text.split(" ", 1)[1])
        save_settings()
        await message.reply_text(f"✅ Custom name set: `{settings['custom_name']}`")
        return

    await message.reply_text("Sun NXT link അയക്കുക, അല്ലെങ്കിൽ `/1:5` പോലുള്ള episode command ഉപയോഗിക്കുക.")


@app.on_callback_query(filters.regex(r"^set\|"))
async def settings_callback(_, callback: CallbackQuery):
    uid = callback.from_user.id if callback.from_user else 0
    s = get_user_settings(uid)
    action = callback.data.split("|", 1)[1]
    if action == "toggle_autothumb":
        s["auto_thumb"] = not s["auto_thumb"]
        answer = f"Auto Thumbnail: {'ON' if s['auto_thumb'] else 'OFF'}"
    elif action == "clear_thumb":
        old = s.get("custom_thumb")
        if old and Path(old).is_file():
            try:
                Path(old).unlink()
            except OSError:
                pass
        s["custom_thumb"] = None
        answer = "Custom Thumbnail നീക്കം ചെയ്തു. പുതിയ ഫോട്ടോ അയച്ച് സെറ്റ് ചെയ്യാം."
    elif action == "clear_name":
        s["custom_name"] = None
        answer = "Custom Name reset ചെയ്തു."
    elif action == "toggle_mp3":
        s["mp3_quality"] = "320K" if s["mp3_quality"] == "192K" else "192K"
        answer = f"MP3 Bitrate: {s['mp3_quality']}"
    elif action == "cookies":
        ok, msg = check_cookies_file()
        await callback.answer(("✅ " if ok else "❌ ") + msg, show_alert=True)
        return
    else:
        await callback.answer("Unknown setting", show_alert=True)
        return
    save_settings()
    await callback.answer(answer)
    await callback.message.edit_text(format_settings_text(uid), reply_markup=build_settings_keyboard(uid))


@app.on_callback_query(filters.regex(r"^cancel_job\|"))
async def cancel_job_callback(_, callback: CallbackQuery):
    _, job_id, target = callback.data.split("|", 2)
    uid = callback.from_user.id if callback.from_user else 0
    if not target.isdigit() or uid != int(target):
        await callback.answer("ഇത് നിങ്ങളുടെ റിക്വസ്റ്റ് അല്ല.", show_alert=True)
        return
    event = CANCEL_EVENTS.get(job_id)
    if event:
        event.set()
    proc = ACTIVE_PROCESSES.get(job_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    PENDING_JOBS.pop(job_id, None)
    await callback.answer("Cancel request അയച്ചു.")
    try:
        await callback.message.edit_text("❌ Download cancel ചെയ്തു.")
    except Exception:
        pass


@app.on_callback_query(filters.regex(r"^dl\|"))
async def quality_callback(_, callback: CallbackQuery):
    try:
        _, job_id, quality_key = callback.data.split("|", 2)
    except ValueError:
        await callback.answer("Invalid selection", show_alert=True)
        return
    job = PENDING_JOBS.get(job_id)
    if not job:
        await callback.answer("ഈ ഓപ്ഷൻ കാലഹരണപ്പെട്ടു. ലിങ്ക് വീണ്ടും അയക്കുക.", show_alert=True)
        return
    uid = callback.from_user.id if callback.from_user else 0
    if uid != job["user_id"]:
        await callback.answer("ഇത് നിങ്ങളുടെ റിക്വസ്റ്റ് അല്ല.", show_alert=True)
        return
    if quality_key not in QUALITY_OPTIONS:
        await callback.answer("Unknown quality", show_alert=True)
        return

    await callback.answer(f"{QUALITY_OPTIONS[quality_key][0]} തിരഞ്ഞെടുത്തു.")
    settings = get_user_settings(uid)
    show = job["show"]
    season = job["season"]
    episode = job["episodes"][0]
    base = safe_name(settings.get("custom_name") or show["slug"])
    folder = DOWNLOAD_DIR / base / f"S{season:02d}" / ("MP3" if quality_key == "mp3" else quality_key)
    folder.mkdir(parents=True, exist_ok=True)
    extension = ".mp3" if quality_key == "mp3" else ".mp4"
    output = folder / f"{base}_S{season:02d}E{episode:02d}{extension}"

    status = await callback.message.edit_text(f"⏳ **{base}**\nQuality: `{QUALITY_OPTIONS[quality_key][0]}`\nDownload ആരംഭിക്കുന്നു...")
    cancel_event = threading.Event()
    CANCEL_EVENTS[job_id] = cancel_event
    ok, result, final_path = await asyncio.to_thread(
        run_yt_dlp, show["url"], str(output), quality_key, settings["mp3_quality"], cancel_event, job_id
    )
    CANCEL_EVENTS.pop(job_id, None)
    PENDING_JOBS.pop(job_id, None)

    if not ok:
        try:
            await status.edit_text(f"❌ **Download failed**\n{result}")
        except Exception:
            pass
        return

    if not final_path or not Path(final_path).is_file():
        await status.edit_text("❌ Download പൂർത്തിയായി എന്ന് yt-dlp അറിയിച്ചു, പക്ഷേ output file കണ്ടെത്തിയില്ല.")
        return

    thumb = settings.get("custom_thumb") if settings.get("custom_thumb") and Path(settings["custom_thumb"]).is_file() else None
    if quality_key != "mp3" and not thumb and settings["auto_thumb"]:
        thumb = await asyncio.to_thread(generate_thumbnail, final_path, uid)

    try:
        await status.edit_text("✅ Download പൂർത്തിയായി. Telegram-ലേക്ക് അയക്കുന്നു...")
        caption = f"**{base}**\nQuality: `{QUALITY_OPTIONS[quality_key][0]}`"
        if quality_key == "mp3":
            await status.reply_audio(final_path, caption=caption, title=base)
        else:
            await status.reply_video(final_path, caption=caption, thumb=thumb, supports_streaming=True)
        await status.edit_text("✅ Download പൂർത്തിയായി!")
    except Exception as exc:
        await status.edit_text(f"✅ ഫയൽ ഡൗൺലോഡ് ചെയ്തു: `{final_path}`\nTelegram upload error: `{str(exc)[:250]}`")


if __name__ == "__main__":
    load_settings()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.run()
