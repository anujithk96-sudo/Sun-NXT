import asyncio
import html
import logging
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
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

TARGET_QUALITIES = [1080, 720, 476, 360, 144, 140, 120]
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m|\[\d+(?:;\d+)*m")

# Example Sun NXT URLs for reference and domain recognition
SUNNXT_EXAMPLE_URLS = [
    "https://www.sunnxt.com/suntv-malayalam-serial-kanyadaanam-nov-01-2026-episodeno1835-Nov01-2026/detail/122855/281312",
    "https://www.sunnxt.com",
    "sunnxt.com",
]


def is_sunnxt_url(url: str) -> bool:
    normalized = url.strip().lower()
    if normalized == "sunnxt.com" or normalized.startswith(("https://sunnxt.com", "http://sunnxt.com", "https://www.sunnxt.com", "http://www.sunnxt.com")):
        return True
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = (parsed.netloc or "").lower()
    return host == "sunnxt.com" or host.endswith(".sunnxt.com")


def sunnxt_support_requirements(url: str) -> str:
    """Explain what is needed for Sun NXT links to be supported/accessible."""
    has_cookies = os.path.isfile(COOKIE_FILE)
    cookie_status = f"Found ({COOKIE_FILE})" if has_cookies else f"Missing ({COOKIE_FILE})"
    return (
        f"Sun NXT Link Check ({url}):\n"
        f"- Extractor Support: yt-dlp does not have a built-in extractor for Sun NXT web URLs, so 'Unsupported URL' can occur.\n"
        f"- Auth / Session Cookies: {cookie_status}. Sun NXT typically requires valid account login/session cookies.\n"
        f"- Stream & DRM Limits: Many OTT streams require authorized stream tokens and may use DRM protection, which this bot does not bypass."
    )


def clean_error_text(err, limit: int = 700) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", str(err)).strip()
    return html.escape(cleaned[:limit])


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


def progress_hook(user_id, status_message, client):
    last_edit = [0.0]

    def hook(data):
        if user_id in CANCELLED:
            raise Exception("Download cancelled by user")

        if data.get("status") != "downloading":
            return

        now = time.time()
        if now - last_edit[0] < 3:
            return

        last_edit[0] = now

        percent = ANSI_ESCAPE_RE.sub("", str(data.get("_percent_str", "0%"))).strip()
        speed = ANSI_ESCAPE_RE.sub("", str(data.get("_speed_str", "N/A"))).strip()
        eta = ANSI_ESCAPE_RE.sub("", str(data.get("_eta_str", "N/A"))).strip()

        text = (
            f"⬇️ Downloading...\n\n"
            f"📊 {percent}\n"
            f"🚀 Speed: {speed}\n"
            f"⏱ ETA: {eta}"
        )

        try:
            asyncio.run_coroutine_threadsafe(
                status_message.edit_text(text),
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

    sunnxt_note = ""
    if is_sunnxt_url(url):
        sunnxt_note = sunnxt_support_requirements(url)
        log.info(sunnxt_note)

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

        title = str((info or {}).get("title") or ("Sun NXT Link" if is_sunnxt_url(url) else "Video Link"))
        await show_quality_buttons(
            status,
            user_id,
            url,
            title=title,
            has_audio=True,
            note=sunnxt_note,
        )

    except Exception as exc:
        err_clean = ANSI_ESCAPE_RE.sub("", str(exc)).strip()
        log.warning("Metadata pre-check note for %s: %s", url, err_clean)
        combined_note = (
            f"{sunnxt_note}\n{err_clean}".strip()
            if sunnxt_note
            else err_clean
        )
        await show_quality_buttons(
            status,
            user_id,
            url,
            title="Sun NXT Link" if is_sunnxt_url(url) else "Video Link",
            has_audio=True,
            note=combined_note,
        )

    finally:
        FETCHING.discard(user_id)


@app.on_message(filters.private & filters.command("start"))
async def start_handler(client, message):
    examples_text = "\n".join(f"• {u}" for u in SUNNXT_EXAMPLE_URLS)
    await message.reply_text(
        "🎬 OTT Video Bot\n\n"
        "Send a video link to see quality and Audio MP3 buttons first.\n"
        "Downloading starts only after you select an option.\n\n"
        f"Example Sun NXT URLs:\n{examples_text}\n\n"
        "/help - Help\n"
        "/status - Current status\n"
        "/cancel - Cancel download"
    )


@app.on_message(filters.private & filters.command("help"))
async def help_handler(client, message):
    examples_text = "\n".join(f"• {u}" for u in SUNNXT_EXAMPLE_URLS)
    await message.reply_text(
        "Send a link directly or use /rip URL.\n\n"
        "1. Quality buttons and Audio MP3 button are shown first.\n"
        "2. Downloading starts only after you click a button.\n\n"
        f"Examples:\n{examples_text}\n\n"
        "Note: Sun NXT web URLs require yt-dlp support, valid session/cookies, "
        "and non-DRM streams; DRM or login/access restrictions are not bypassed."
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
            if x.startswith(("https://", "http://")) or "sunnxt.com" in x.lower()
        ),
        None,
    )

    if not url:
        await message.reply_text(
            "Usage: /rip https://www.sunnxt.com/suntv-malayalam-serial-kanyadaanam-nov-01-2026-episodeno1835-Nov01-2026/detail/122855/281312"
        )
        return

    cleaned_url = url.rstrip(".,);]>")
    if not cleaned_url.startswith(("https://", "http://")):
        cleaned_url = f"https://{cleaned_url}"

    await fetch_link(message, cleaned_url)


@app.on_message(
    filters.private
    & filters.text
    & ~filters.command(["start", "help", "status", "cancel", "rip"])
)
async def link_handler(client, message):
    text = message.text or ""
    match = re.search(r"https?://[^\s]+", text)

    if match:
        url = match.group(0).rstrip(".,);]>")
    elif "sunnxt.com" in text.lower():
        sun_match = re.search(r"(?:www\.)?sunnxt\.com[^\s]*", text, flags=re.IGNORECASE)
        url = f"https://{sun_match.group(0).rstrip('.,);]>')}" if sun_match else "https://www.sunnxt.com"
    else:
        await message.reply_text(
            "❌ Please send a valid video link."
        )
        return

    await fetch_link(message, url)


def find_downloaded_file(ydl, info, uid: int, expected_exts: tuple):
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
        f"⏳ Starting {label} download..."
    )

    filename = None

    try:
        output_template = str(
            DOWNLOAD_DIR
            / f"%(title).150B_{uid}_{uuid.uuid4().hex[:8]}.%(ext)s"
        )

        opts = ydl_base_options()
        opts.update({
            "outtmpl": output_template,
            "continuedl": True,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [
                progress_hook(uid, status, client)
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

        def download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    pending["url"],
                    download=True,
                )
                return find_downloaded_file(ydl, info, uid, expected_exts)

        filename = await asyncio.to_thread(download)

        if uid in CANCELLED:
            raise RuntimeError("Download cancelled")

        if not filename or not os.path.isfile(filename):
            raise RuntimeError(
                "Downloaded file could not be found."
            )

        await status.edit_text(
            f"📤 Uploading {label} to Telegram..."
        )

        safe_title = html.escape(pending["title"][:200])

        if is_audio:
            await client.send_audio(
                chat_id=callback.message.chat.id,
                audio=filename,
                title=pending["title"][:120],
                caption=(
                    f"🎵 {safe_title}\n"
                    f"Format: MP3"
                ),
            )
        else:
            await client.send_video(
                chat_id=callback.message.chat.id,
                video=filename,
                caption=(
                    f"📹 {safe_title}\n"
                    f"Quality: {label}"
                ),
                supports_streaming=True,
            )

        await status.delete()

    except Exception as exc:
        log.exception("Download failed")

        if uid in CANCELLED or "cancel" in str(exc).lower():
            text = "❌ Download cancelled."
        else:
            extra_info = ""
            if is_sunnxt_url(pending["url"]):
                extra_info = "\n\n" + html.escape(sunnxt_support_requirements(pending["url"]))
            text = (
                "❌ Download failed.\n\n"
                + f"<code>{clean_error_text(exc, 800)}</code>"
                + extra_info
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

        DOWNLOADING.discard(uid)
        CANCELLED.discard(uid)


if __name__ == "__main__":
    print(
        "🤖 Bot running. Send a video URL; "
        "quality / MP3 selection comes before download."
    )
    app.run()
