#!/usr/bin/env bash
# Buddy installer / updater. Paste this into Terminal:
#   curl -fsSL https://raw.githubusercontent.com/wellsdj/Serum-Plugin-Creator/claude/alexa-desk-robot-hardware-txxae5/buddy/install.sh | bash
# It puts Buddy in ~/Buddy, adds buttons to your Desktop, and starts it.
# Running it again updates Buddy and keeps your keys, memories, alarms and settings.
set -e
BRANCH="claude/alexa-desk-robot-hardware-txxae5"
ZIP_URL="${BUDDY_ZIP_URL:-https://codeload.github.com/wellsdj/Serum-Plugin-Creator/zip/refs/heads/$BRANCH}"
DEST="${BUDDY_HOME:-$HOME/Buddy}"
INSTALL_CMD="curl -fsSL https://raw.githubusercontent.com/wellsdj/Serum-Plugin-Creator/$BRANCH/buddy/install.sh | bash"

echo "Downloading Buddy..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fL --progress-bar -o "$TMP/buddy.zip" "$ZIP_URL"
unzip -q "$TMP/buddy.zip" -d "$TMP"
SRC="$(ls -d "$TMP"/*/buddy | head -1)"
[ -d "$SRC/server" ] || { echo "Download looked wrong; try again in a minute."; exit 1; }

mkdir -p "$DEST"
cp -R "$SRC/." "$DEST/"   # your data (server/data) and installed parts (server/.venv) are left alone
chmod +x "$DEST/install.sh" "$DEST/server/run.sh" "$DEST/server/autostart.sh" "$DEST/firmware/flash.sh"
echo "Buddy is in $DEST"

# Double-clickable buttons on the Desktop (Mac).
if [ "$(uname)" = "Darwin" ] && [ -d "$HOME/Desktop" ]; then
  make_button() {  # name, command
    printf '#!/bin/bash\n%s\n' "$2" > "$HOME/Desktop/$1.command"
    chmod +x "$HOME/Desktop/$1.command"
  }
  make_button "Start Buddy" "cd \"$DEST/server\" && ./run.sh"
  make_button "Put Buddy on the board" "\"$DEST/firmware/flash.sh\"; echo; read -n 1 -s -r -p 'Press any key to close.'"
  make_button "Update Buddy" "$INSTALL_CMD"
  echo "Added 3 buttons to your Desktop: Start Buddy, Put Buddy on the board, Update Buddy."
fi

source "$DEST/server/_env.sh"
buddy_env || { echo "Setup failed (see above). Check your internet and paste the install line again."; exit 1; }

# If Buddy is set to start by itself, restart it so the update takes effect.
if [ -f "$HOME/Library/LaunchAgents/com.buddy.server.plist" ]; then
  "$DEST/server/autostart.sh" on
  echo "Buddy has been updated and restarted in the background."
  exit 0
fi

PORT="${BUDDY_PORT:-8000}"
if "$VENV_PY" -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1', $PORT)) == 0 else 1)"; then
  echo
  echo "Buddy is updated. It's still running the old version in another window:"
  echo "close that Terminal window (or press Ctrl+C in it), then double-click \"Start Buddy\" on your Desktop."
  exit 0
fi

echo
echo "All set. Starting Buddy now..."
cd "$DEST/server"
exec ./run.sh </dev/null
