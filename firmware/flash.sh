#!/usr/bin/env bash
# Puts the Buddy program onto the ESP32-S3 over USB. No Arduino IDE needed.
#   buddy/firmware/flash.sh
cd "$(dirname "$0")"
FW="$(pwd)/prebuilt"
source ../server/_env.sh
buddy_env >/dev/null || { echo "Setup failed. Check your internet and try again."; exit 1; }

find_port() {
  local p
  for p in /dev/cu.usbmodem* /dev/cu.usbserial* /dev/cu.SLAB_USBtoUART* /dev/cu.wchusbserial* /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

PORT="${1:-}"
if [ -z "$PORT" ]; then
  PORT="$(find_port)"
  if [ -z "$PORT" ]; then
    echo "Plug the ESP32-S3 into the Mac now: use the USB socket marked \"USB\" on the board"
    echo "(if nothing happens, try the other socket, and make sure the cable isn't charge-only)."
    printf "Waiting for the board"
    for _ in $(seq 1 90); do
      sleep 1; printf "."
      PORT="$(find_port)" && break
    done
    echo
  fi
fi
if [ -z "$PORT" ]; then
  echo "Couldn't see the board. Try another USB cable (some only charge), then run this again."
  exit 1
fi

echo "Found the board on $PORT. Installing Buddy on it (about 30 seconds)..."
flash() {
  "$VENV_PY" -m esptool --chip esp32s3 --port "$PORT" --baud 460800 \
    --before default-reset --after hard-reset write-flash -z \
    --flash-mode keep --flash-freq keep --flash-size keep \
    0x0 "$FW/bootloader.bin" 0x8000 "$FW/partitions.bin" 0xe000 "$FW/boot_app0.bin" 0x10000 "$FW/buddy.bin"
}
if ! flash; then
  echo
  echo "The board didn't answer. Do this, then press Enter:"
  echo "  1. Hold down the BOOT button on the board"
  echo "  2. While holding it, press and release the RST (or EN) button"
  echo "  3. Let go of BOOT"
  read -r _ </dev/tty || true
  PORT="$(find_port || echo "$PORT")"
  flash || { echo "Still no luck. Try the board's other USB socket or another cable, then run this again."; exit 1; }
fi

cat <<'EOF'

  Done! Buddy is on the board.

  Next, connect it to your Wi-Fi (once):
    1. On your phone, open Wi-Fi settings and join "Buddy-Setup" (password: heybuddy)
    2. A setup page pops up (if not, open 192.168.4.1 in the phone's browser)
    3. Tap "Configure WiFi", pick your home Wi-Fi, type its password, tap Save
  It then finds your laptop by itself and plays a little tune when it's ready.
  (The Buddy server needs to be running on the laptop: "Start Buddy" on your Desktop.)
EOF
