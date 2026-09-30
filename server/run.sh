#!/usr/bin/env bash
# Starts the Buddy server. First run sets everything up (takes a few minutes).
#   cd buddy/server && ./run.sh
set -e
cd "$(dirname "$0")"

PY=""
for p in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Buddy needs Python 3.10 or newer. Install Python 3.12 from https://www.python.org/downloads/macos/ then run this again."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up (a few minutes)..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.installed
fi
.venv/bin/python setup_models.py

PORT="${BUDDY_PORT:-8000}"
( sleep 3; (open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT") >/dev/null 2>&1 ) &
echo "Buddy is starting. Web page: http://localhost:$PORT  (Ctrl+C to stop)"
exec .venv/bin/python -m buddy
