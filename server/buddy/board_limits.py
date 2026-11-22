"""How close the desk unit is to the limits of its hardware: memory, CPU and storage.

The board sends these numbers in its hello and every 30 s in its stats (see addHealth() in
firmware/buddy/buddy_main.cpp). check() turns them into plain-English warnings, each with what to do about it.
The hub logs them, puts them on /api/status, and GET /api/board_health shows them on their own.
The thresholds are explained in firmware/LIMITS.md: change them there and here together.
"""
from __future__ import annotations

KB = 1024

# The firmware side of these lives in firmware/buddy/memory_plan.h (MIN_FREE_INTERNAL_KB).
MIN_FREE_INTERNAL_KB = 40     # internal RAM free once connected
MIN_LOWEST_INTERNAL_KB = 16   # lowest it has ever been since start-up
MIN_LARGEST_PIECE_KB = 12     # biggest single block that can still be handed out
MIN_STACK_BYTES = 1024        # least stack any task has had spare
MAX_LOOP_MS = 250             # longest single pass of the main loop (button, network, LEDs all wait on it)
MAX_APP_PERCENT = 80          # program size as a share of its 4 MB flash slot
MIN_SD_FREE_MB = 1024         # space left on the SD card for lessons (about 8 hours of recordings)
MIN_NVS_FREE = 40             # free entries in the settings store
MAX_DROPS_PER_STATS = 25      # audio pieces dropped in one 30 s stats period (mic, speaker or recorder)
MAX_CORE_PERCENT = 85         # how busy either CPU core may be over a stats period
MIC_BLOCK_BUDGET_US = 15_000  # each 20 ms mic block must be done within this (firmware: MIC_BLOCK_BUDGET_US)


def _w(level: str, what: str, fix: str) -> dict:
    return {"level": level, "what": what, "fix": fix}


def check(info: dict, prev: dict | None = None) -> list[dict]:
    """Warnings for one desk unit. `info` is its latest hello/stats; `prev` the stats before (for drop counts)."""
    out: list[dict] = []
    if info.get("heartbeat") == 0:
        out.append(_w("high", "an audio task (mic or speaker) has stopped checking in: the heartbeat light is off",
                      "the hardware watchdog restarts the board if it stays stuck; check the board log"))
    heap = info.get("heap")
    if heap is not None and heap < MIN_FREE_INTERNAL_KB * KB:
        out.append(_w("high" if heap < 16 * KB else "medium",
                      f"internal RAM is low: {heap // KB} KB free (needs {MIN_FREE_INTERNAL_KB} KB)",
                      "move a big buffer to PSRAM (memory_plan.h tier BIG) or shrink one; see firmware/LIMITS.md"))
    low = info.get("min_heap")
    if low is not None and low < MIN_LOWEST_INTERNAL_KB * KB:
        out.append(_w("high", f"internal RAM dipped to {low // KB} KB at some point since start-up",
                      "something briefly needs a lot of internal RAM: check the board log's [mem] lines"))
    largest = info.get("largest")
    if largest is not None and largest < MIN_LARGEST_PIECE_KB * KB and (heap or 0) >= MIN_FREE_INTERNAL_KB * KB:
        out.append(_w("medium", f"internal RAM is broken into small pieces (largest {largest // KB} KB)",
                      "allocate big buffers once at start-up instead of over and over"))
    if info.get("psram") is False:
        out.append(_w("high", "PSRAM isn't working, so big buffers are squeezed into internal RAM",
                      "check the build uses PSRAM=opi (firmware/build.sh) and the board is the 8 MB PSRAM kind"))
    if info.get("mem_fallbacks"):
        out.append(_w("medium", f"{info['mem_fallbacks']} big buffers had to use internal RAM instead of PSRAM",
                      "see the [mem] lines in the board log"))
    stack = info.get("stack_min")
    if stack is not None and stack < MIN_STACK_BYTES:
        out.append(_w("high", f"the {info.get('stack_task') or '?'} task has only {stack} bytes of stack to spare",
                      "give that task a bigger stack in memory_plan.h, or move big local arrays out of it"))
    loop_ms = info.get("loop_max_ms")
    if loop_ms is not None and loop_ms > MAX_LOOP_MS:
        out.append(_w("medium", f"the main loop stalled for {loop_ms} ms (button, network and lights wait on it)",
                      "move slow work (SD card, network, long sums) into its own task; see firmware/LIMITS.md"))
    for core in ("cpu0", "cpu1"):
        busy = info.get(core)
        if busy is not None and busy > MAX_CORE_PERCENT:
            out.append(_w("medium", f"CPU core {core[-1]} is {busy}% busy (busiest task: {info.get('cpu_top') or '?'})",
                          "move work to the other core, run it less often, or hand it to the server on the Mac"))
    mic_us = info.get("mic_max_us")
    if mic_us is not None and mic_us > MIC_BLOCK_BUDGET_US:
        out.append(_w("high", f"a 20 ms block of mic audio took {mic_us / 1000:.1f} ms to process",
                      "the mic task is nearly out of time: lighten the per-block work (echo cancelling is the big one)"))
    if prev and isinstance(info.get("aec_skipped"), int) and info["aec_skipped"] > (prev.get("aec_skipped") or 0):
        out.append(_w("medium", "echo cancelling was switched off for a moment to keep up",
                      "the board was too busy: check the CPU warnings and firmware/LIMITS.md"))
    app, slot = info.get("app_kb"), info.get("app_slot_kb")
    if app and slot and app * 100 > slot * MAX_APP_PERCENT:
        out.append(_w("medium", f"the program is {app} KB, {app * 100 // slot}% of its {slot} KB slot",
                      "drop unused libraries, or move sounds and tables to the flash filesystem"))
    sd_free = info.get("sd_free_mb")
    if info.get("sd") and sd_free is not None and 0 <= sd_free < MIN_SD_FREE_MB:
        out.append(_w("medium", f"the SD card has {sd_free} MB free",
                      "recordings are deleted once the Mac has them: let Buddy upload, or use a bigger card"))
    nvs = info.get("nvs_free")
    if nvs is not None and nvs < MIN_NVS_FREE:
        out.append(_w("medium", f"the settings store is nearly full ({nvs} entries left)",
                      "keep only small settings in Preferences; put lists and files on the SD card"))
    if prev:
        for key, name in (("dropped_mic", "microphone"), ("dropped_spk", "speaker"), ("dropped_rec", "recorder")):
            now, before = info.get(key), prev.get(key)
            if isinstance(now, int) and isinstance(before, int) and now - before > MAX_DROPS_PER_STATS:
                out.append(_w("medium", f"{now - before} pieces of {name} audio were dropped in the last 30 s",
                              "the board is too busy or its buffer too small: check the CPU and memory warnings"))
    return out


def summary(info: dict) -> dict:
    """The numbers that matter, in units people read."""
    def kb(key):
        v = info.get(key)
        return None if v is None else v // KB
    return {"internal_free_kb": kb("heap"), "internal_lowest_kb": kb("min_heap"), "largest_piece_kb": kb("largest"),
            "psram": info.get("psram"), "psram_free_kb": kb("psram_free"),
            "least_stack_spare": f"{info.get('stack_min')} bytes ({info.get('stack_task')})"
            if info.get("stack_min") is not None else None,
            "slowest_loop_ms": info.get("loop_max_ms"), "cpu_core0_pct": info.get("cpu0"),
            "cpu_core1_pct": info.get("cpu1"), "busiest_task": info.get("cpu_top"),
            "slowest_mic_block_ms": None if info.get("mic_max_us") is None else round(info["mic_max_us"] / 1000, 1), "program_kb": info.get("app_kb"),
            "program_slot_kb": info.get("app_slot_kb"), "sd_free_mb": info.get("sd_free_mb"),
            "settings_entries_free": info.get("nvs_free")}
