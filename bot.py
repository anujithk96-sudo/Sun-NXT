import asyncio
import html
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("ott_bot")

API_ID = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
COOKIE_FILE = os.getenv("COOKIE_FILE", "cookies.txt")

if not API_ID.isdigit() or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Set API_ID, API_HASH and BOT_TOKEN in your .env file or environment.")

app = Client(
    "ott_quality_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

PENDING = {}
FETCHING = set()
DOWNLOADING = set()
CANCELLED = set()

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
THUMB_DIR = Path("thumbnails")
THUMB_DIR.mkdir(exist_ok=True)

TARGET_QUALITIES = [1080, 720, 476, 360, 144, 140, 120]
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m")


def clean_error_text(err, limit: int = 700) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", str(err)).strip()
    return html.escape(cleaned[:limit])


def sanitize_filename(name_str: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(name_str or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "OTT_Video"


def get_video_duration(file_path: str) -> int:
    try:
        parser = createParser(file_path)
        if parser:
            metadata = extractMetadata(parser)
            if metadata and metadata.has("duration"):
                return int(metadata.get("duration").seconds)
    except Exception:
        pass
    return 0


def generate_thumbnail(video_path: str, user_id: int):
    thumb_path = THUMB_DIR / f"auto_{user_id}_{uuid.uuid4().hex[:6]}.jpg"
    try:
        cmd = [
            "ffmpeg",
            "-ss",
            "00:00:05",
            "-i",
            video_path,
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(thumb_path),
            "-y",
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if thumb_path.is_file() and thumb_path.stat().st_size > 0:
            return str(thumb_path)
    except Exception as exc:
        log.error("Error generating thumbnail: %s", exc)
    return None


def get_progress_bar(percentage: str) -> str:
    clean_pct_str = ANSI_ESCAPE_RE.sub("", percentage).replace("%", "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)", clean_pct_str)
    try:
        pct = float(match.group(1)) if match else 0.0
    except Exception:
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    completed = 10 if pct >= 100.0 else int(pct // 10)
    return ("█" * completed) + ("░" * (10 - completed))


def ydl_base_options():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocolor": True,
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
        "fragment_retries": 10,
        "file_access_retries": 5,
    }
    if os.path.isfile(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts


def inspect_media(info):
    """Inspect non-DRM video heights and check if audio stream exists."""
    heights = set()
    has_audio = False

    formats = info.get("formats") or []
    if not formats and info.get("format_id"):
        formats = [info]

    for fmt in formats:
        if fmt.get("has_drm"):
            continue
        if not (fmt.get("url") or fmt.get("manifest_url") or fmt.get("protocol")):
            continue

        acodec = fmt.get("acodec")
        if acodec and acodec != "none":
            has_audio = True

        vcodec = fmt.get("vcodec")
        if not fmt.get("format_id") or vcodec == "none":
            continue

        height = fmt.get("height")
        if not height:
            resolution = str(fmt.get("resolution") or "")
            if "x" in resolution:
                height = resolution.rsplit("x", 1)[-1]

        try:
            height = int(height)
            if height > 0:
                heights.add(height)
        except (TypeError, ValueError):
            continue

    return heights, has_audio


def quality_keyboard(has_audio: bool = True):
    rows = []
    for height in TARGET_QUALITIES:
        rows.append([
            InlineKeyboardButton(
                f"📹 {height}p",
                callback_data=f"quality:{height}",
            )
        ])
    if has_audio:
        rows.append([
            InlineKeyboardButton(
                "🎵 Audio MP3",
                callback_data="quality:mp3",
            )
        ])
    return InlineKeyboardMarkup(rows)


def progress_hook(user_id, status_message, client, title="Downloading"):
    last_edit = [0.0]

    def hook(data):
        if user_id in CANCELLED:
            raise Exception("Task cancelled by user.")

        status = data.get("status")
        if status not in ("downloading", "finished"):
            return

        now = time.time()
        if status != "finished" and (now - last_edit[0] < 2.5):
            return

        last_edit[0] = now

        raw_pct = "100.0%" if status == "finished" else str(data.get("_percent_str", "0%"))
        percent = ANSI_ESCAPE_RE.sub("", raw_pct).strip() or "0%"
        speed = ANSI_ESCAPE_RE.sub("", str(data.get("_speed_str", "N/A"))).strip() or "N/A"
        eta = ANSI_ESCAPE_RE.sub("", str(data.get("_eta_str", "00:00" if status == "finished" else "N/A"))).strip() or "N/A"
        bar = get_progress_bar(percent)

        text = (
            f"📥 <b>{html.escape(title)}...</b>\n\n"
            f"<code>[{bar}]</code> <b>{html.escape(percent)}</b>\n"
            f"🚀 <b>Speed:</b> <code>{html.escape(speed)}</code>\n"
            f"⏱️ <b>ETA:</b> <code>{html.escape(eta)}</code>"
        )

        try:
            asyncio.run_coroutine_threadsafe(
                status_message.edit_text(text, parse_mode=ParseMode.HTML),
                client.loop,
            )
        except Exception:
            pass

    return hook


async def show_quality_buttons(
    status_msg,
    user_id: int,
    url: str,
    title: str,
    has_audio: bool = True,
    note: str = "",
):
    PENDING[user_id] = {
        "url": url,
        "title": title,
        "has_audio": has_audio,
    }
    safe_title = html.escape(title[:300])
    extra_note = f"\n⚠️ <i>{html.escape(note[:450])}</i>" if note else ""

    await status_msg.edit_text(
        f"🎬 <b>{safe_title}</b>{extra_note}\n\n"
        "👇 <b>Select a quality or Audio MP3 first. "
        "Downloading will start only after you click a button.</b>",
        reply_markup=quality_keyboard(has_audio),
        parse_mode=ParseMode.HTML,
    )


async def fetch_link(message, url):
    user_id = message.from_user.id

    if user_id in FETCHING:
        await message.reply_text(
            "⏳ Your previous link is still being checked. Please wait."
        )
        return

    if user_id in DOWNLOADING:
        await message.reply_text(
            "⏳ Please wait until the current download finishes."
        )
        return

    FETCHING.add(user_id)

    status = await message.reply_text(
        "⏳ Preparing quality options..."
    )

    try:
        opts = ydl_base_options()
        opts.update({
            "skip_download": True,
            "extract_flat": False,
        })

        def extract():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        info = await asyncio.wait_for(
            asyncio.to_thread(extract),
            timeout=15,
        )

        if info and info.get("has_drm"):
            await status.edit_text(
                "🔒 This media is DRM-protected, "
                "so download quality buttons cannot be provided."
            )
            return

        title = str((info or {}).get("title") or "Video Link")
        _, has_audio = inspect_media(info or {})
        await show_quality_buttons(
            status,
            user_id,
            url,
            title=title,
            has_audio=True if not info else has_audio,
        )

    except Exception as exc:
        err_clean = ANSI_ESCAPE_RE.sub("", str(exc)).strip()
        log.warning("Metadata pre-check note for %s: %s", url, err_clean)
        await show_quality_buttons(
            status,
            user_id,
            url,
            title="Video Link",
            has_audio=True,
            note=err_clean,
        )

    finally:
        FETCHING.discard(user_id)


@app.on_message(filters.private & filters.command("start"))
async def start_handler(client, message):
    await message.reply_text(
        "🎬 OTT Video Bot\n\n"
        "Send a video link to see quality and Audio MP3 buttons first.\n"
        "Downloading starts only after you select an option.\n\n"
        "/help - Help\n"
        "/status - Current status\n"
        "/cancel - Cancel download"
    )


@app.on_message(filters.private & filters.command("help"))
async def help_handler(client, message):
    await message.reply_text(
        "Send a link directly or use /rip URL.\n\n"
        "1. Quality buttons and Audio MP3 button are shown first.\n"
        "2. Downloading starts only after you click a button.\n\n"
        "DRM protection or login/access restrictions are not bypassed."
    )


@app.on_message(filters.private & filters.command("status"))
async def status_handler(client, message):
    uid = message.from_user.id

    if uid in DOWNLOADING:
        await message.reply_text(
            "⬇️ Your download is currently in progress."
        )
    elif uid in FETCHING:
        await message.reply_text(
            "⏳ Preparing quality options."
        )
    else:
        await message.reply_text(
            "📭 No active download right now."
        )


@app.on_message(filters.private & filters.command("cancel"))
async def cancel_handler(client, message):
    uid = message.from_user.id

    if uid in DOWNLOADING:
        CANCELLED.add(uid)
        await message.reply_text(
            "🛑 Cancellation requested for your current download."
        )
    else:
        await message.reply_text(
            "There is no active download to cancel."
        )


@app.on_message(filters.private & filters.command("rip"))
async def rip_handler(client, message):
    args = message.command[1:]

    url = next(
        (
            x for x in args
            if x.startswith(("https://", "http://"))
        ),
        None,
    )

    if not url:
        await message.reply_text(
            "Usage: /rip https://example.com/video-link"
        )
        return

    await fetch_link(message, url.rstrip(".,);]>"))


@app.on_message(
    filters.private
    & filters.text
    & ~filters.command(["start", "help", "status", "cancel", "rip"])
)
async def link_handler(client, message):
    match = re.search(
        r"https?://[^\s]+",
        message.text or "",
    )

    if not match:
        await message.reply_text(
            "❌ Please send a valid video link."
        )
        return

    await fetch_link(
        message,
        match.group(0).rstrip(".,);]>"),
    )


def find_downloaded_file(ydl, info, uid: int, expected_exts: tuple, fallback_path: str = ""):
    candidates = []

    for item in info.get("requested_downloads") or []:
        path = item.get("filepath") or item.get("_filename")
        if path:
            candidates.append(path)

    if info.get("_filename"):
        candidates.append(info["_filename"])

    try:
        candidates.append(ydl.prepare_filename(info))
    except Exception:
        pass

    if fallback_path:
        candidates.append(fallback_path)

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

        stem = os.path.splitext(candidate)[0]
        for ext in expected_exts:
            if os.path.isfile(stem + ext):
                return stem + ext

    matches = sorted(
        DOWNLOAD_DIR.glob(f"*_{uid}_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(matches[0]) if matches else None


def build_clean_output_path(downloaded_file: str, title: str, label: str, uid: int) -> str:
    ext = os.path.splitext(downloaded_file)[1] or ".mp4"
    base = sanitize_filename(title)
    tag = sanitize_filename(label).replace(" ", "_")
    target = DOWNLOAD_DIR / f"{base}.{tag}_{uid}_{uuid.uuid4().hex[:6]}{ext}"
    if os.path.abspath(downloaded_file) != os.path.abspath(target):
        if target.exists():
            target.unlink()
        os.rename(downloaded_file, str(target))
    return str(target)


@app.on_callback_query(filters.regex(r"^quality:(?:\d+|mp3)$"))
async def quality_callback(client, callback: CallbackQuery):
    uid = callback.from_user.id
    pending = PENDING.get(uid)

    if not pending:
        await callback.answer(
            "Link expired. Please send the link again.",
            show_alert=True,
        )
        return

    if uid in DOWNLOADING:
        await callback.answer(
            "A download is already in progress.",
            show_alert=True,
        )
        return

    choice = callback.data.split(":", 1)[1]
    is_audio = choice == "mp3"

    label = "Audio MP3" if is_audio else f"{int(choice)}p"
    await callback.answer(
        f"Selected {label}. Starting download..."
    )

    DOWNLOADING.add(uid)
    CANCELLED.discard(uid)

    status = await callback.message.reply_text(
        f"⏳ Initializing {label} download..."
    )

    filename = None
    thumb_path = None

    try:
        output_template = str(
            DOWNLOAD_DIR
            / f"%(title).120B_{uid}_{uuid.uuid4().hex[:8]}.%(ext)s"
        )

        opts = ydl_base_options()
        opts.update({
            "outtmpl": output_template,
            "continuedl": True,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [
                progress_hook(uid, status, client, title=f"Downloading {label}")
            ],
        })

        if is_audio:
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            })
            expected_exts = (".mp3", ".m4a", ".opus", ".ogg", ".wav")
        else:
            height = int(choice)
            opts.update({
                "format": (
                    f"bestvideo[height={height}]+bestaudio/"
                    f"bestvideo[height<={height}]+bestaudio/"
                    f"best[height<={height}]/best"
                ),
                "merge_output_format": "mp4",
            })
            expected_exts = (".mp4", ".mkv", ".webm", ".mov")

        def run_download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                if uid in CANCELLED:
                    raise Exception("Task cancelled by user.")
                info = ydl.extract_info(
                    pending["url"],
                    download=True,
                )
                resolved = find_downloaded_file(
                    ydl,
                    info,
                    uid,
                    expected_exts,
                    fallback_path=output_template,
                )
                return resolved, info

        raw_file, dl_info = await asyncio.to_thread(run_download)

        if uid in CANCELLED:
            raise RuntimeError("Task cancelled by user.")

        if not raw_file or not os.path.isfile(raw_file):
            raise RuntimeError(
                "Downloaded file could not be found."
            )

        resolved_title = str(
            (dl_info or {}).get("series")
            or (dl_info or {}).get("show")
            or (dl_info or {}).get("title")
            or pending["title"]
            or "OTT_Video"
        )
        filename = build_clean_output_path(raw_file, resolved_title, label, uid)

        await status.edit_text(
            f"📤 Uploading {label} to Telegram..."
        )

        safe_title = html.escape(resolved_title[:200])
        file_name = os.path.basename(filename)

        if is_audio:
            await client.send_audio(
                chat_id=callback.message.chat.id,
                audio=filename,
                title=resolved_title[:120],
                file_name=file_name,
                caption=(
                    f"🎵 <b>{safe_title}</b>\n"
                    f"🎧 Format: <b>MP3</b>"
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            thumb_path = generate_thumbnail(filename, uid)
            duration = get_video_duration(filename)
            await client.send_video(
                chat_id=callback.message.chat.id,
                video=filename,
                file_name=file_name,
                duration=duration,
                thumb=thumb_path,
                caption=(
                    f"📹 <b>{safe_title}</b>\n"
                    f"🎯 Quality: <b>{label}</b>"
                ),
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
            )

        await status.delete()

    except Exception as exc:
        log.exception("Download failed")

        if uid in CANCELLED or "cancel" in str(exc).lower():
            text = "❌ Task cancelled."
        else:
            text = (
                "❌ Download failed.\n\n"
                + f"<code>{clean_error_text(exc, 800)}</code>"
            )

        try:
            await status.edit_text(
                text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await callback.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
            )

    finally:
        if filename and os.path.isfile(filename):
            try:
                os.remove(filename)
            except OSError:
                pass
        if thumb_path and os.path.isfile(thumb_path):
            try:
                os.remove(thumb_path)
            except OSError:
                pass

        DOWNLOADING.discard(uid)
        CANCELLED.discard(uid)


if __name__ == "__main__":
    print(
        "🤖 Bot running. Send a video URL; "
        "quality / MP3 selection comes before download."
    )
    app.run()
