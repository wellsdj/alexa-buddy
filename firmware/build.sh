#!/usr/bin/env bash
# Builds the board program from firmware/buddy and puts it in firmware/prebuilt, with build_info.json (size and
# memory use, checked by server/tests/test_firmware_rules.py). Then flash it with firmware/flash.sh.
# Needs arduino-cli with the esp32 core and three libraries (see firmware/LIMITS.md, "Building").
set -euo pipefail
cd "$(dirname "$0")"
FQBN="esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,FlashMode=qio,PartitionScheme=custom"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
LOG="$OUT/compile.log"
if ! arduino-cli compile --fqbn "$FQBN" --output-dir "$OUT" buddy >"$LOG" 2>&1; then
  cat "$LOG"; echo; echo "Build failed (above)."; exit 1
fi
grep -E "^Sketch uses|^Global variables" "$LOG"
cp "$OUT/buddy.ino.bin" prebuilt/buddy.bin
cp "$OUT/buddy.ino.bootloader.bin" prebuilt/bootloader.bin
cp "$OUT/buddy.ino.partitions.bin" prebuilt/partitions.bin
python3 - "$LOG" <<'PY'
import json, re, sys
log = open(sys.argv[1]).read()
app = int(re.search(r"Sketch uses (\d+) bytes", log).group(1))
ram = int(re.search(r"Global variables use (\d+) bytes", log).group(1))
ver = re.search(r'#define FW_VERSION "([^"]+)"', open("buddy/buddy_main.cpp").read()).group(1)
json.dump({"version": ver, "app_bytes": app, "static_ram_bytes": ram,
           "fqbn": "esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,FlashMode=qio,PartitionScheme=custom"},
          open("prebuilt/build_info.json", "w"), indent=2)
print(f"firmware {ver}: program {app // 1024} KB, fixed internal RAM {ram // 1024} KB -> prebuilt/")
PY
