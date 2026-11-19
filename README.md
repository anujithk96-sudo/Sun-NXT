# Sun NXT Telegram Downloader Bot

## Included files
- `bot.py` — Telegram bot source.
- `cookies.txt` — your supplied browser cookies (keep private; do not publish/share them).
- `requirements.txt` — Python dependencies.
- `.env.example` — environment-variable template.
- `run.sh` — Termux setup/run helper.
- `runtime.txt` — Python runtime selection for buildpack deployments.
- `apt.txt` — system packages for Apt-based buildpacks (includes FFmpeg).
- `Dockerfile` — Docker image definition with FFmpeg and Python dependencies.

## Termux setup
1. Install Termux packages:
   ```sh
   pkg update && pkg upgrade -y
   pkg install python ffmpeg -y
   ```
2. Enter this project folder and install Python packages:
   ```sh
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. Create `.env` from the template and fill in your own Telegram credentials:
   ```sh
   cp .env.example .env
   nano .env
   ```
4. Confirm `cookies.txt` is in the same folder as `bot.py` (or set `COOKIE_FILE` in `.env`).
5. Run:
   ```sh
   chmod +x run.sh
   ./run.sh
   ```

You can also start manually after loading `.env` values into the shell:
```sh
export API_ID='your_api_id'
export API_HASH='your_api_hash'
export BOT_TOKEN='your_bot_token'
python bot.py
```

## Docker deployment
Build and run the image from this folder:

```sh
docker build -t sunnxt-bot .
docker run --restart unless-stopped \
  -e API_ID="your_api_id" \
  -e API_HASH="your_api_hash" \
  -e BOT_TOKEN="your_bot_token" \
  -v "$(pwd)/cookies.txt:/app/cookies.txt:ro" \
  -v "$(pwd)/downloads:/app/downloads" \
  sunnxt-bot
```

For Apt-based buildpacks, `apt.txt` installs FFmpeg and `runtime.txt` selects Python.

## Important notes
- Keep `.env`, `cookies.txt`, and Pyrogram session files private. Never post them in a public repository or chat.
- FFmpeg is a system program, not a Python package. Install it with `pkg install ffmpeg` in Termux.
- The quality buttons are format selectors; a quality is available only if the source actually provides a matching format.
- This project does not decrypt DRM-protected streams, bypass Widevine, or defeat access controls. Some Sun NXT content may therefore fail even with valid cookies. Use only content you are authorized to access and download.
- Episode range requests are intentionally not expanded into guessed URLs; provide each episode's actual link.
