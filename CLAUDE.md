# Buddy: notes for Claude

Buddy is a voice assistant: a FastAPI server on the Mac (`server/`) and an ESP32-S3 desk unit (`firmware/`).
The person building it is experienced with laptop and cloud code (Claude Code, Vercel) but new to small boards.
Explain board constraints in plain words, and warn them early when a change gets close to a limit.

## Before changing anything in `firmware/`
Read `firmware/LIMITS.md`. Then, for every change:
- **Memory:** any buffer of 4 KB or more gets a row in `firmware/buddy/memory_plan.h` and is allocated through
  `firmware/buddy/mem.h` (`memBig` for big things, in PSRAM). No `malloc`, `new[]`, `xStreamBufferCreate` or big
  arrays in `buddy_main.cpp`. New tasks get a `FAST` stack row.
- **CPU:** audio work stays well inside its 20 ms block; `loop()` never waits; nothing spins. Heavy work goes to
  the server on the Mac.
- **Storage:** settings in NVS (rarely written), bulky data on the SD card. Never move the `nvs` partition.
- **Say what it costs.** In your reply, give the change's memory, CPU and storage cost, and how much of each
  budget is left. If any budget passes 75%, say so plainly and suggest what to move.

## House rules
- When a bug is fixed, add a test for it (server: `server/tests/`; board: `server/tests/test_firmware_rules.py`).
- Run the tests: `cd server && python3 -m pytest -q` (all must pass).
- Board changes: bump `FW_VERSION` in `firmware/buddy/buddy_main.cpp`, build with `firmware/build.sh` (never by
  hand: it sets PSRAM, the 16 MB flash and the partitions), and commit `firmware/prebuilt/` with the source.
- After flashing, check the board log (`[mem]`, `[cpu]` lines) and `curl -s localhost:8000/api/board_health`.
- Commit at meaningful points so the history tells the story.
- Explain things to the user in plain, short steps.
