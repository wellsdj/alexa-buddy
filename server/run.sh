#!/usr/bin/env bash
# Starts the Buddy server (the first run sets everything up). Leave this window open.
#   cd buddy/server && ./run.sh
cd "$(dirname "$0")"
source ./_env.sh
buddy_env || { echo; echo "Setup failed (see above). Check your internet and run this again."; exit 1; }

PORT="${BUDDY_PORT:-8000}"
if "$VENV_PY" -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1', $PORT)) == 0 else 1)"; then
  echo
  echo "  Buddy is already running. Opening its web page: http://localhost:$PORT"
  echo "  (To restart it, close the other Buddy window or press Ctrl+C in it, then start again.)"
  if [ -z "$BUDDY_NO_BROWSER" ]; then
    (open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT") >/dev/null 2>&1
  else
    sleep 30  # started in the background: don't restart in a tight loop
  fi
  exit 0
fi
if [ -z "$BUDDY_NO_BROWSER" ]; then
  ( sleep 4; (open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT") >/dev/null 2>&1 ) &
fi
echo
echo "  Buddy is running. Web page: http://localhost:$PORT"
echo "  Leave this window open (you can minimise it). Press Ctrl+C to stop Buddy."
echo
# On a Mac, stop it dozing off while Buddy runs (the screen can still turn off).
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "$VENV_PY" -m buddy
fi
exec "$VENV_PY" -m buddy
