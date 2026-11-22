# Handoff: working on Buddy from a terminal on the Mac

Notes for a Claude session running on the Mac, picking up from the cloud session that built most of this.

## Where things are
- Installed copy (what actually runs): `~/buddy` or wherever `install.sh` put it. Check with `ls ~/Desktop/*.command` and read "Start Buddy.command".
- Server: `server/` (FastAPI). Start: `cd server && ./run.sh`. Web page: http://localhost:8000
- Board program: `firmware/buddy/` (ESP32-S3, Arduino). Ready-built: `firmware/prebuilt/buddy.bin`.
- Tests: `cd server && python3 -m pytest -q` (all should pass; `tests/test_firmware_rules.py` checks the board code).

## Everyday commands (instead of the Desktop buttons)
- Update everything: `curl -fsSL https://raw.githubusercontent.com/wellsdj/alexa-buddy/main/install.sh | bash`
- Put Buddy on the board (USB): `firmware/flash.sh`
- Read the board's log (restarts it): `curl -s -X POST localhost:8000/api/usb_log | python3 -m json.tool`
- SD card test (board must be connected, "Desk unit" green): `curl -s -X POST localhost:8000/api/sd_test | python3 -m json.tool`
- Re-send Wi-Fi + this Mac's address over USB: `curl -s -X POST localhost:8000/api/usb_setup -H 'content-type: application/json' -d '{"ssid":"NAME","password":"PASS"}'`
- Or read the serial port directly: `python3 -m serial.tools.miniterm /dev/cu.usbmodem* 115200`

## Board wiring (ESP32-S3)
- Mic (INMP441): SCK 4, WS 5, SD 6, L/R to GND
- DAC (PCM5102, purple): BCK 15, LCK 16, DIN 7, SCK to GND, VIN 3V
- SD reader (Adafruit): CS 10, SI(MOSI) 11, CLK 12, SO(MISO) 13, power to 3V/5V pin, GND
- Button GPIO 0 (yellow/green), LED strip GPIO 2, status LED 48/38
- Lights: fast red flicker = can't record (no SD); solid red = muted (hold button 5 s); slow red blink = no server

## Current status (22 Nov 2026)
- Firmware 1.7.7. The SD card now mounts (30 GB, 20 MHz). It used to take Wi-Fi's memory and the board
  restarted in a loop ("wifi:Expected to init 4 rx buffer, actual is 0"): fixed by starting Wi-Fi first.
- Next step: flash 1.7.7, check "Desk unit" goes green, run the SD test, then try recording a lesson
  (hold the button 2 s). If anything's off, read the board log first.

## House rules
- When a bug is fixed, add a test for it (server: `tests/`; board: `tests/test_firmware_rules.py`).
- Bump `FW_VERSION` in `firmware/buddy/buddy_main.cpp` and rebuild `firmware/prebuilt/buddy.bin` together
  (a test checks they match).
- Explain things to the user in plain, short steps.
