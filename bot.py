import asyncio
import html
import logging
import os
import re
import time
import uuid
from pathlib import Path

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


def quality_keyboard(has_audio: bool):
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


async def fetch_link(message, url):
    user_id = message.from_user.id

    if user_id in FETCHING:
        await message.reply_text(
            "⏳ നിങ്ങളുടെ മുമ്പത്തെ ലിങ്ക് പരിശോധിച്ചുകൊണ്ടിരിക്കുന്നു. കാത്തിരിക്കുക."
        )
        return

    if user_id in DOWNLOADING:
        await message.reply_text(
            "⏳ മുമ്പത്തെ ഡൗൺലോഡ് തീരുന്നതുവരെ കാത്തിരിക്കുക."
        )
        return

    FETCHING.add(user_id)

    status = await message.reply_text(
        "⏳ വീഡിയോ വിവരങ്ങൾ പരിശോധിക്കുന്നു... പരമാവധി 2 മിനിറ്റ് കാത്തിരിക്കുക."
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
            timeout=120,
        )

        if not info:
            raise RuntimeError("വീഡിയോ വിവരങ്ങൾ ലഭിച്ചില്ല.")

        if info.get("has_drm"):
            await status.edit_text(
                "🔒 ഈ മീഡിയ DRM സംരക്ഷണമുള്ളതാണ്. "
                "അതുകൊണ്ട് ഡൗൺലോഡ് ക്വാളിറ്റി ബട്ടണുകൾ നൽകാനാവില്ല."
            )
            return

        heights, has_audio = inspect_media(info)

        if not heights and not has_audio:
            await status.edit_text(
                "❌ ഡൗൺലോഡ് ചെയ്യാവുന്ന non-DRM മീഡിയ ഫോർമാറ്റുകൾ ലഭിച്ചില്ല.\n\n"
                "സൈറ്റ് പിന്തുണയില്ലാത്തതോ, login ആവശ്യമുള്ളതോ, "
                "DRM സംരക്ഷണമുള്ളതോ ആകാം."
            )
            return

        PENDING[user_id] = {
            "url": url,
            "title": str(info.get("title") or "OTT Video"),
            "heights": sorted(heights),
            "has_audio": has_audio,
        }

        title = html.escape(PENDING[user_id]["title"][:300])

        await status.edit_text(
            f"🎬 <b>{title}</b>\n\n"
            "👇 <b>ആദ്യം നിങ്ങൾക്ക് വേണ്ട ക്വാളിറ്റി അല്ലെങ്കിൽ Audio MP3 തിരഞ്ഞെടുക്കുക. "
            "അതിനുശേഷം മാത്രമേ ഡൗൺലോഡ് ആരംഭിക്കൂ.</b>",
            reply_markup=quality_keyboard(has_audio),
            parse_mode=ParseMode.HTML,
        )

    except asyncio.TimeoutError:
        await status.edit_text(
            "⌛ 120 സെക്കൻഡിനുള്ളിൽ വീഡിയോ വിവരങ്ങൾ ലഭിച്ചില്ല. പിന്നീട് വീണ്ടും ശ്രമിക്കുക."
        )

    except Exception as exc:
        log.exception("Metadata extraction failed")
        reason = clean_error_text(exc, 700)

        await status.edit_text(
            f"❌ Could not fetch video info.\n\n<code>{reason}</code>",
            parse_mode=ParseMode.HTML,
        )

    finally:
        FETCHING.discard(user_id)


@app.on_message(filters.private & filters.command("start"))
async def start_handler(client, message):
    await message.reply_text(
        "🎬 OTT Video Bot\n\n"
        "വീഡിയോ ലിങ്ക് അയക്കുക. ആദ്യം Quality / Audio MP3 ബട്ടണുകൾ കാണിക്കും.\n"
        "നിങ്ങൾ ഒരു ബട്ടൺ തിരഞ്ഞെടുക്കുമ്പോൾ മാത്രമേ ഡൗൺലോഡ് തുടങ്ങൂ.\n\n"
        "/help - സഹായം\n"
        "/status - നിലവിലെ ഡൗൺലോഡ്\n"
        "/cancel - ഡൗൺലോഡ് റദ്ദാക്കുക"
    )


@app.on_message(filters.private & filters.command("help"))
async def help_handler(client, message):
    await message.reply_text(
        "ലിങ്ക് നേരിട്ട് അയക്കുക അല്ലെങ്കിൽ /rip URL ഉപയോഗിക്കുക.\n\n"
        "1. ബോട്ട് വീഡിയോ വിവരങ്ങൾ പരിശോധിക്കും.\n"
        "2. ക്വാളിറ്റി ബട്ടണുകളും Audio MP3 ബട്ടണും കാണിക്കും.\n"
        "3. ബട്ടൺ അമർത്തിയാൽ മാത്രമേ ഡൗൺലോഡ് തുടങ്ങൂ.\n\n"
        "DRM സംരക്ഷണമോ login/access നിയന്ത്രണമോ മറികടക്കില്ല."
    )


@app.on_message(filters.private & filters.command("status"))
async def status_handler(client, message):
    uid = message.from_user.id

    if uid in DOWNLOADING:
        await message.reply_text(
            "⬇️ നിങ്ങളുടെ ഡൗൺലോഡ് ഇപ്പോൾ പ്രവർത്തിക്കുന്നു."
        )
    elif uid in FETCHING:
        await message.reply_text(
            "⏳ വീഡിയോ വിവരങ്ങൾ പരിശോധിക്കുന്നു."
        )
    else:
        await message.reply_text(
            "📭 ഇപ്പോൾ സജീവമായ ഡൗൺലോഡ് ഇല്ല."
        )


@app.on_message(filters.private & filters.command("cancel"))
async def cancel_handler(client, message):
    uid = message.from_user.id

    if uid in DOWNLOADING:
        CANCELLED.add(uid)
        await message.reply_text(
            "🛑 ഡൗൺലോഡ് റദ്ദാക്കാനുള്ള അഭ്യർത്ഥന നൽകി."
        )
    else:
        await message.reply_text(
            "ഇപ്പോൾ റദ്ദാക്കാനുള്ള ഡൗൺലോഡ് ഇല്ല."
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
            "ഉപയോഗം: /rip https://example.com/video-link"
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
            "❌ ദയവായി സാധുവായ വീഡിയോ ലിങ്ക് അയക്കുക."
        )
        return

    await fetch_link(
        message,
        match.group(0).rstrip(".,);]>"),
    )


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
            "ലിങ്ക് കാലഹരണപ്പെട്ടു. വീണ്ടും ലിങ്ക് അയക്കുക.",
            show_alert=True,
        )
        return

    if uid in DOWNLOADING:
        await callback.answer(
            "ഒരു ഡൗൺലോഡ് ഇതിനകം നടക്കുന്നു.",
            show_alert=True,
        )
        return

    choice = callback.data.split(":", 1)[1]
    is_audio = choice == "mp3"

    if is_audio and not pending.get("has_audio"):
        await callback.answer(
            "ഈ വീഡിയോയിൽ ഓഡിയോ ലഭ്യമല്ല.",
            show_alert=True,
        )
        return

    label = "Audio MP3" if is_audio else f"{int(choice)}p"
    await callback.answer(
        f"{label} തിരഞ്ഞെടുത്തു. ഡൗൺലോഡ് ആരംഭിക്കുന്നു..."
    )

    DOWNLOADING.add(uid)
    CANCELLED.discard(uid)

    status = await callback.message.reply_text(
        f"⏳ {label} ഡൗൺലോഡ് തയ്യാറാക്കുന്നു..."
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
                "ഡൗൺലോഡ് ഫയൽ കണ്ടെത്താനായില്ല."
            )

        await status.edit_text(
            f"📤 {label} Telegram-ലേക്ക് അപ്‌ലോഡ് ചെയ്യുന്നു..."
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
            text = "❌ ഡൗൺലോഡ് റദ്ദാക്കി."
        else:
            text = (
                "❌ ഡൗൺലോഡ് പരാജയപ്പെട്ടു.\n\n"
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

        DOWNLOADING.discard(uid)
        CANCELLED.discard(uid)


if __name__ == "__main__":
    print(
        "🤖 Bot running. Send a video URL; "
        "quality / MP3 selection comes before download."
    )
    app.run()
