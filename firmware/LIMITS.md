# The desk unit's limits, and where things go

Buddy's desk unit is an ESP32-S3 board. Compared with a laptop or a Vercel function it is tiny, and when it runs
out of something it doesn't slow down politely: it goes silent, drops audio, or restarts. This page says what the
board has, where each kind of data belongs, and how you get warned before you hit a limit.

**Short version:** big things go in PSRAM (`memBig`), small fast things in internal RAM, fixed data in flash, bulky
files on the SD card. Every buffer of 4 KB or more is listed in `buddy/memory_plan.h`. Audio work must finish well
inside its 20 ms. `loop()` must never wait. The tests, the board log and `/api/board_health` warn you when you get close.

## What the board has

Measured on this board (ESP32-S3, revision v0.2, with 16 MB flash and 8 MB PSRAM):

| | Size | Speed | Like on a laptop |
|---|---|---|---|
| **Internal RAM** | 320 KB in total; about 155 KB free once running | fastest | CPU cache: tiny and precious |
| **PSRAM** | 8 MB (octal) | slower (about 5-10x on a cache miss), cached in a 32 KB cache | normal RAM |
| **Flash** | 16 MB | read like PSRAM; writing is slow and wears it out | the SSD the app is installed on |
| **SD card** | 30 GB | slow (milliseconds per write) | an external USB drive |
| **CPU** | 2 cores at 240 MHz | | 2 slow cores, no spare |

Things that surprise people from the laptop world:
- **Internal RAM is shared with Wi-Fi.** Wi-Fi, the network stack, the SD card driver and every task's stack all
  come out of it. In 1.7.7-1.7.10 Buddy's own buffers left 15 KB, and the board went silent after connecting.
- **There is no swap and no "out of memory" error you'll notice.** A failed allocation usually shows up as a
  silent failure somewhere else (a message not sent, a dropped connection).
- **Each task has a fixed stack** (set when it's made). A big local array (`int16_t buf[4096]` inside a function)
  eats it; overflow crashes the board.
- **Writing to flash pauses almost everything** for milliseconds (the program itself runs from flash). Saving
  settings (`Preferences`) is fine now and then, never in a loop.
- **TLS (HTTPS from the board) costs about 40 KB of internal RAM per connection** in this build. Let the Mac
  server do internet calls; the board talks plain WebSocket to the Mac.

## Where data goes: five questions, in order

1. **Does hardware or an interrupt touch it** (I2S audio DMA, the LED strip's RMT, an ISR), or is it a task
   stack or FreeRTOS object? → **internal RAM**: `memFast()`, or a `STATIC` row if it's a fixed array.
2. **Is it read-only and known when you build?** (sounds, tune tables, lookup tables, text) → **flash**: make it
   `const` / `static const`; it stays in flash and costs no RAM.
3. **Must it survive a restart?** Small settings (numbers, short strings) → **NVS** (`Preferences`), written
   rarely. Files → the flash filesystem (the 8 MB `spiffs` partition, not used yet) or the **SD card**. Anything
   bulky or growing (recordings, logs) → **SD card**.
4. **Is it 4 KB or more?** → **PSRAM**: `memBig()` / `memStream()`. Read through it in order where you can.
5. **Otherwise** (small, hot) → internal RAM, normally just a variable.

Two patterns that make PSRAM work for real-time audio:
- **Reservoir + small window.** Keep the big buffer in PSRAM, and copy what the hot loop needs into a small
  internal array first. The speaker is a 96 KB PSRAM reservoir that the play task copies 256 samples at a time
  into an internal array before handing it to the hardware.
- **Hot working set in internal RAM.** The echo canceller keeps its 1.5 s of history (48 KB) in PSRAM but its
  filter and working arrays (7 KB, used every 20 ms) in internal RAM.

## The memory plan

`buddy/memory_plan.h` is a table of everything of 4 KB or more: name, tier (`FAST`, `STATIC`, `BIG`), size,
fallback size (what a `BIG` thing gets in internal RAM if PSRAM is missing) and what it's for. The code gets the
sizes from it (`MEM_SPEAKER`, `MEM_STACK_MIC`, ...) and allocates through `buddy/mem.h`:

| Call | Gives you |
|---|---|
| `memBig(name, bytes)` | PSRAM, or `nullptr` |
| `memBigOr(name, bytes, fallback)` | PSRAM, else `fallback` bytes of internal RAM (logged) |
| `memFast(name, bytes)` | internal RAM |
| `memStream(name, bytes, fallback, trigger)` | a FreeRTOS stream buffer with its storage in PSRAM |
| `memTemp(bytes)` | short-lived scratch space (PSRAM if any); free it soon |

Budgets (in `memory_plan.h`, checked by the tests and on the board):

| Budget | Limit | Now (1.8.1) |
|---|---|---|
| `FAST` + `STATIC` rows | 72 KB | 50 KB |
| All fixed internal RAM the build reports | 100 KB | 83 KB |
| `BIG` rows (PSRAM) | 6 MB of the 8 MB | 269 KB |
| Internal RAM free once connected (`MIN_FREE_INTERNAL_KB`) | at least 40 KB | 152 KB |
| Program size | 80% of its 4 MB slot | 1.3 MB (33%) |

## CPU rules

- **Two cores.** Core 0 runs Wi-Fi, the network stack, the mic task and the echo-delay task. Core 1 runs `loop()`,
  the speaker task, the lesson recorder and the uploader. Put new heavy work on the quieter core.
- **Audio has a deadline.** The mic delivers a block every 20 ms, and the mic task must finish each block in under
  15 ms (`MIC_BLOCK_BUDGET_US`). If a block takes longer, the mic task switches echo cancelling off for 2 s rather
  than fall behind. If it fell behind for 5 s, CPU 0's idle task would starve and the watchdog would restart the
  board. That happened once in 1.8.0, during echo cancelling; it hasn't happened since 1.8.1 added these guards.
- **`loop()` must never wait.** Buttons, the WebSocket, the lights and alarms all run from it. Anything that can
  take more than a few milliseconds (SD card, HTTP, long sums) belongs in its own task. Warning above 250 ms.
- **Never spin.** A `while` loop that waits without `vTaskDelay()` or a blocking call starves everything below it.
- **Heavy thinking belongs on the Mac.** The board is the ears, mouth and alarm clock; speech recognition,
  Claude, music and the web all run in `server/`. If a feature needs real computing, send the data to the server.

## Storage rules

- **Flash layout** (`buddy/partitions.csv`, 16 MB): settings (`nvs`, 20 KB, never move it: Wi-Fi details live
  there), two 4 MB program slots (for updates over Wi-Fi later), an 8 MB filesystem (`spiffs`, free for sounds or
  models), and a crash-dump area. Changing it needs a USB flash.
- **NVS** is for small settings only. It has a fixed number of entries; the server warns under 40 left.
- **SD card** holds lessons. They're deleted once the Mac has them. The server warns under 1 GB free.

## How you're warned (from earliest to latest)

1. **Tests** (`cd server && python3 -m pytest tests/test_firmware_rules.py tests/test_board_limits.py`). They fail
   if a buffer of 4 KB or more isn't in the plan, if memory is allocated outside `mem.h`, if a budget is exceeded,
   if the program outgrows 80% of its slot, if the flash layout would move the settings, or if the board stops
   sending a number the server checks.
2. **The build** (`firmware/build.sh`) prints the program size and fixed internal RAM and writes
   `prebuilt/build_info.json`, which the tests check.
3. **The board log at start-up** lists every big buffer and where it landed, whether PSRAM was found, and free
   internal RAM after each step. `[mem] LOW` and `[cpu] busy` lines appear when something is close.
4. **The server**, every 30 s, checks the board's numbers (`server/buddy/board_limits.py`). It logs a warning,
   adds a `board_limits` entry to the Activity log, and shows everything at
   `curl -s localhost:8000/api/board_health | python3 -m json.tool`: each number, and each warning with what to do.

## When you get a warning

| Warning | Usually means | Do this |
|---|---|---|
| internal RAM is low / dipped | a new buffer or library took internal RAM | move it to `memBig`; check the `[mem]` lines for what grew |
| PSRAM isn't working | built without `PSRAM=opi` | build with `firmware/build.sh` only |
| big buffers had to use internal RAM | PSRAM missing or full | as above; check `BUDGET_BIG_KB` |
| a task has little stack to spare | big local arrays, deep calls, `String` building | raise its row in the plan, or move the array to `memBig` |
| main loop stalled | slow work in `loop()` | move it into a task |
| mic block too slow / core busy / echo cancelling switched off | too much per-block work | lighten it, move it to the other core, or do it on the Mac |
| program near its slot size | big libraries or embedded data | drop libraries; put data in the flash filesystem |
| SD card / settings store nearly full | uploads failing; too much in `Preferences` | let it upload; move lists to the SD card |
| audio pieces dropped | buffers too small or the CPU too busy | check the CPU and memory warnings first |

## Adding a feature: checklist

1. Write down what it needs: memory (how big, how long it lives), CPU (how often, how long each time), storage.
2. Anything of 4 KB or more: add a row to `memory_plan.h`, allocate it with `mem.h`. Tasks: a `FAST` stack row.
3. Run the tests. Build with `firmware/build.sh`. Bump `FW_VERSION` in the same change.
4. Flash with `firmware/flash.sh`, then read the board log (`[mem]`, `[cpu]`) and `/api/board_health`.
5. If any budget is more than 75% used, say so in the commit message and decide what moves next.

## Building and flashing

One-off setup on a Mac: `brew install arduino-cli`, then `arduino-cli core install esp32:esp32` (about 1.8 GB;
GitHub limits each download to about 0.5 MB/s, so it can take a while) and
`arduino-cli lib install WebSockets WiFiManager ArduinoJson`.

- Build: `firmware/build.sh` (puts `buddy.bin`, `bootloader.bin`, `partitions.bin` and `build_info.json` in
  `prebuilt/`). Always use it: it sets PSRAM, the 16 MB flash and the partition table.
- Flash over USB: `firmware/flash.sh`.
- Read the board log: `curl -s -X POST localhost:8000/api/usb_log | python3 -m json.tool` (restarts the board).
