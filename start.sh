#!/usr/bin/env bash
# Mac/Linux: runs the paper trader locally (your own IP => Bybit/MEXC reachable) and prints the phone URL.
set -e
cd "$(dirname "$0")/papertrader"
python3 -m venv .venv 2>/dev/null || true
. .venv/bin/activate
pip install -q -r requirements.txt
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
echo; echo "Open on your phone (same Wi-Fi):  http://$IP:8000"; echo
exec uvicorn app:app --host 0.0.0.0 --port 8000
