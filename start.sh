#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
# (re)install when the venv is new, a previous install failed, or requirements.txt changed
[ -x .venv/bin/python ] || python3 -m venv .venv
if ! cmp -s requirements.txt .venv/installed.txt; then
  echo "Installing libraries..."
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  cp requirements.txt .venv/installed.txt
fi
[ -f .env ] || cp .env.example .env
# keep the bots running: restart automatically after a crash or a network failure
set +e
while true; do
  .venv/bin/python -m app
  code=$?
  [ "$code" = 2 ] && exit 2          # no free port - restarting would not help
  echo "The app stopped (exit $code). Restarting in 15 seconds - Ctrl+C to stop."
  sleep 15
done
