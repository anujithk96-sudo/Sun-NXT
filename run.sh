#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg is missing. In Termux run: pkg install ffmpeg"
  exit 1
fi
if [ ! -f .env ]; then
  echo "Create .env from .env.example and fill API_ID, API_HASH and BOT_TOKEN."
  exit 1
fi
set -a
. ./.env
set +a
python bot.py
