#!/usr/bin/env bash
# Mac only: make Buddy start by itself whenever you log in, and keep running in the
# background (no Terminal window needed).
#   ./autostart.sh on     start now and at every login
#   ./autostart.sh off    stop it and don't start at login
cd "$(dirname "$0")"
DIR="$(pwd)"
PLIST="$HOME/Library/LaunchAgents/com.buddy.server.plist"

case "$1" in
  on)
    mkdir -p "$HOME/Library/LaunchAgents" "$DIR/data"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.buddy.server</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/run.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>BUDDY_NO_BROWSER</key><string>1</string>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$DIR/data/buddy.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/buddy.log</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load -w "$PLIST"
    echo "Buddy now starts by itself when you log in, and is running now."
    echo "Web page: http://localhost:${BUDDY_PORT:-8000}   (log: $DIR/data/buddy.log)"
    ;;
  off)
    launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    echo "Buddy won't start by itself any more (and has been stopped)."
    ;;
  *)
    echo "Usage: ./autostart.sh on   or   ./autostart.sh off"
    ;;
esac
