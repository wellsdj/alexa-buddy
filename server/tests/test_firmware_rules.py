"""Checks on the desk unit's program that don't need the board itself.

Each one is here because the board once broke this way:
- the SD card was mounted before Wi-Fi started, and with a card in Wi-Fi ran out of memory
  ("Expected to init 4 rx buffer, actual is 0"), so the board restarted over and over;
- the prebuilt program on GitHub didn't match the source, so "Put Buddy on the board" put an old version on;
- board messages went out of only one USB socket, so Show board log saw nothing;
- internal RAM ran out with the SD card in (1.7.7-1.7.10), so the board went silent after connecting. Since 1.8.0
  big buffers live in PSRAM and every one is listed in firmware/buddy/memory_plan.h (see firmware/LIMITS.md).
"""
import json
import re
from pathlib import Path

FW = Path(__file__).resolve().parents[2] / "firmware"
SRC = (FW / "buddy" / "buddy_main.cpp").read_text()


def _setup_body() -> str:
    start = SRC.index("void setup()")
    return SRC[start:SRC.index("\nvoid loop()", start)]


def test_wifi_starts_before_anything_big_takes_memory():
    body = _setup_body()
    wifi = body.index("WiFi.mode(WIFI_STA)")
    for big in ("sdBegin()", "startRecTasks()"):
        assert big in body, f"{big} should be called in setup()"
        assert body.index(big) > wifi, f"{big} runs before Wi-Fi starts: Wi-Fi can run out of memory"


def test_no_big_allocations_between_boot_and_wifi():
    body = _setup_body()
    before_wifi = body[:body.index("WiFi.mode(WIFI_STA)")]
    for call in ("SD.begin", "xTaskCreatePinnedToCore(recTask", "xTaskCreatePinnedToCore(uploadTask"):
        assert call not in before_wifi, f"{call} before Wi-Fi starts"


def test_prebuilt_matches_source_version():
    version = re.search(r'#define FW_VERSION "([\d.]+)"', SRC).group(1)
    binary = (FW / "prebuilt" / "buddy.bin").read_bytes()
    assert f"firmware {version}".encode() in binary, (
        f"firmware/prebuilt/buddy.bin isn't version {version}: rebuild it and copy it in")


def test_log_goes_to_both_usb_sockets():
    assert not re.search(r"\bSerial\.(print|printf|println)\(", SRC), "use logOut, not Serial, for messages"
    assert "usbJtag.write" in SRC


def test_every_server_command_has_a_handler():
    hub = (Path(__file__).resolve().parents[1] / "buddy" / "hub.py").read_text()
    sent = set(re.findall(r'send_json\(\{"type": "(\w+)"', hub))
    handled = set(re.findall(r'!strcmp\(type, "(\w+)"\)', SRC))
    for_web_page = {"event", "alarm_fire", "say"}  # these go to the web page (say: when there's no voice)
    missing = sent - handled - for_web_page
    assert not missing, f"the server sends {missing} but the board doesn't handle it"


# ---- the memory plan (firmware/LIMITS.md). Firmware 1.7.7-1.7.10: with the SD card in, internal RAM ran out
# (15 KB free after start-up, 1-4 KB after connecting), so the board couldn't send its hello or answer the SD test.
# Since 1.8.0 every buffer of 4 KB or more is listed in memory_plan.h, and these tests add the table up.
BUDDY = FW / "buddy"
PLAN = (BUDDY / "memory_plan.h").read_text()
HEADERS = "\n".join((BUDDY / n).read_text() for n in ("config.h", "aec.h", "memory_plan.h")) + SRC
ROW = re.compile(r'X\((\w+),\s*(BIG|FAST|STATIC),\s*([^,]+?),\s*([^,]+?),\s*"([^"]*)"\)')
TYPE_BYTES = {"int8_t": 1, "uint8_t": 1, "char": 1, "int16_t": 2, "uint16_t": 2, "int32_t": 4, "uint32_t": 4,
              "float": 4, "rmt_data_t": 4, "double": 8, "int64_t": 8, "uint64_t": 8}


def _defines() -> dict:
    return dict(re.findall(r"#define (\w+)\s+\(?([\d\s*+()]+?)\)?\s*(?://.*)?$", HEADERS, re.M))


def _eval(expr: str) -> int:
    """Work out a size like `AEC_REF_LEN * 2` from the #defines in the firmware."""
    defs = _defines()
    for _ in range(5):
        expr = re.sub(r"[A-Z_][A-Z0-9_]*", lambda m: f"({defs[m.group(0)]})" if m.group(0) in defs else m.group(0), expr)
    assert re.fullmatch(r"[\d\s*+()/-]+", expr), f"can't work out the size {expr!r}"
    return int(eval(expr))  # digits and + - * / only (checked above)


def _plan() -> list[dict]:
    rows = [{"name": n, "tier": t, "bytes": _eval(b), "fallback": _eval(f), "why": w} for n, t, b, f, w in ROW.findall(PLAN)]
    assert len(rows) >= 10, "couldn't read the table in memory_plan.h"
    return rows


def _budget(name: str) -> int:
    return int(re.search(rf"#define {name} (\d+)", PLAN).group(1))


def test_memory_plan_fits_its_budgets():
    rows = _plan()
    fast = sum(r["bytes"] for r in rows if r["tier"] in ("FAST", "STATIC")) / 1024
    big = sum(r["bytes"] for r in rows if r["tier"] == "BIG") / 1024
    assert fast <= _budget("BUDGET_FAST_KB"), (
        f"internal RAM in the plan is {fast:.0f} KB, over BUDGET_FAST_KB ({_budget('BUDGET_FAST_KB')}): move something "
        "to PSRAM (tier BIG) or shrink it. See firmware/LIMITS.md")
    assert big <= _budget("BUDGET_BIG_KB"), f"PSRAM in the plan is {big:.0f} KB, over BUDGET_BIG_KB"
    for r in rows:
        assert r["why"].strip(), f"{r['name']}: say what it's for"
        if r["tier"] != "BIG":
            assert r["fallback"] == 0, f"{r['name']}: only BIG things have a fallback size"


def test_every_big_fixed_array_is_in_the_plan():
    names = {r["name"] for r in _plan() if r["tier"] == "STATIC"}
    for src_name in ("buddy_main.cpp", "aec.h"):
        text = (BUDDY / src_name).read_text()
        for line in text.splitlines():
            m = re.match(r"\s*static (\w+) (\w+)\[([^\]]+)\]", line)
            if not m or m.group(1) not in TYPE_BYTES:
                continue
            size = TYPE_BYTES[m.group(1)] * _eval(m.group(3))
            if size >= 4096:
                tag = re.search(r"// plan: (\w+)", line)
                assert tag and tag.group(1) in names, (
                    f"{src_name}: '{m.group(2)}' is {size} bytes of internal RAM. Arrays of 4 KB or more must be a "
                    "STATIC row in memory_plan.h with '// plan: NAME' on the line, or (better) come from memBig()")


def test_no_big_memory_except_through_mem_h():
    code = re.sub(r"//.*", "", SRC)
    for bad in ("malloc(", "ps_malloc(", "heap_caps_malloc(", "xStreamBufferCreate(", "new uint8_t[", "new int16_t[",
                "new float["):
        assert bad not in code, f"buddy_main.cpp uses {bad}: get memory through mem.h (memBig/memFast/memStream)"
    aec = re.sub(r"//.*", "", (BUDDY / "aec.h").read_text())
    assert aec.count("malloc(") == 1 and "#define AEC_MALLOC(bytes) malloc(bytes)" in aec, (
        "aec.h must allocate only through AEC_MALLOC (PSRAM on the board)")


def test_task_stacks_come_from_the_plan():
    calls = re.findall(r'xTaskCreatePinnedToCore\(\w+, "\w+", (\w+)', SRC)
    assert calls, "no tasks found"
    rows = {f"MEM_{r['name']}" for r in _plan() if r["tier"] == "FAST"}
    for size in calls:
        assert size in rows, f"task stack {size} isn't a FAST row in memory_plan.h"


def test_big_buffers_ask_for_psram():
    for name in ("SPEAKER", "MIC", "REC", "AEC_REF", "AEC_HIST"):
        assert f"MEM_{name}" in SRC, f"{name} is in the plan but buddy_main.cpp doesn't use MEM_{name}"
    assert 'memStream("speaker", MEM_SPEAKER' in SRC and 'memStream("mic", MEM_MIC' in SRC


def test_build_uses_psram_and_the_16mb_layout():
    build = (FW / "build.sh").read_text()
    for opt in ("PSRAM=opi", "FlashSize=16M", "PartitionScheme=custom"):
        assert opt in build, f"firmware/build.sh must build with {opt}"
    info = json.loads((FW / "prebuilt" / "build_info.json").read_text())
    assert all(opt in info["fqbn"] for opt in ("PSRAM=opi", "FlashSize=16M")), "prebuilt wasn't made by build.sh"


def _partitions() -> dict:
    rows = {}
    for line in (BUDDY / "partitions.csv").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, _t, _s, off, size = [c.strip() for c in line.split(",")[:5]]
            rows[name] = (int(off, 16), int(size, 16))
    return rows


def test_partitions_keep_settings_and_fit_the_chip():
    p = _partitions()
    # Wi-Fi details, settings and alarms live in nvs: moving it would wipe them on the next update.
    assert p["nvs"] == (0x9000, 0x5000) and p["otadata"] == (0xE000, 0x2000)
    assert p["app0"][1] == p["app1"][1], "both program slots must be the same size (for updates over Wi-Fi)"
    spans = sorted(p.values())
    for (a, sa), (b, _sb) in zip(spans, spans[1:]):
        assert a + sa <= b, "partitions overlap"
    assert spans[-1][0] + spans[-1][1] <= 16 * 1024 * 1024, "past the end of the 16 MB flash"


def test_program_and_fixed_ram_have_room_to_grow():
    info = json.loads((FW / "prebuilt" / "build_info.json").read_text())
    version = re.search(r'#define FW_VERSION "([\d.]+)"', SRC).group(1)
    assert info["version"] == version, "prebuilt/build_info.json is stale: run firmware/build.sh"
    slot = _partitions()["app0"][1]
    assert info["app_bytes"] <= slot * 0.8, (
        f"the program is {info['app_bytes'] // 1024} KB, over 80% of its {slot // 1024} KB slot. See firmware/LIMITS.md")
    assert info["static_ram_bytes"] <= _budget("BUDGET_STATIC_RAM_KB") * 1024, (
        f"fixed internal RAM is {info['static_ram_bytes'] // 1024} KB, over BUDGET_STATIC_RAM_KB: big arrays belong "
        "in PSRAM (memBig)")


def test_board_reports_every_number_the_server_checks():
    start = SRC.index("static void addHealth")
    sent = set(re.findall(r'doc\["(\w+)"\]', SRC[start:SRC.index("\n}", start)]))
    limits = (Path(__file__).resolve().parents[1] / "buddy" / "board_limits.py").read_text()
    used = set(re.findall(r'info\.get\("(\w+)"\)', limits)) | set(re.findall(r'info\["(\w+)"\]', limits))
    elsewhere = {"sd", "dropped_mic", "dropped_spk", "dropped_rec", "stack_task"}  # sent outside addHealth
    missing = used - sent - elsewhere
    assert not missing, f"board_limits.py checks {missing} but the board never sends them (addHealth)"
    assert "stack_task" in sent and "dropped_rec" in SRC


def test_memory_is_logged_after_connecting_to_the_server():
    start = SRC.index("case WStype_CONNECTED:")
    connected = SRC[start:SRC.index("case WStype_DISCONNECTED:", start)]
    assert "logMem(" in connected, "log the free memory when the web socket connects"
    assert "sendHello()" in connected
    assert "[mem] free heap" in SRC and "couldn't send a message" in SRC


def test_messages_that_cant_be_read_are_logged_not_dropped():
    start = SRC.index("static void handleJson")
    body = SRC[start:SRC.index("const char *type", start)]
    assert "deserializeJson" in body and "logOut.printf" in body, (
        "handleJson must say when it can't read a message: a silent drop hid the memory shortage")
    assert "[sd] test requested" in SRC


# ---- heartbeat light (1.8.2): a dim blink while idle shows the board is working; it must never "lie".
def _body(signature: str) -> str:
    start = SRC.index(signature)
    return SRC[start:SRC.index("\n}\n", start)]


def test_heartbeat_is_driven_by_the_program_not_a_timer():
    # A timer or PWM blink keeps going after the program hangs, so it would say "fine" when it isn't.
    for timer in ("esp_timer_create", "timerBegin", "timerAttachInterrupt", "ledcAttach", "ledcWrite", "xTimerCreate"):
        assert timer not in SRC, f"{timer}: the heartbeat must come from loop(), not a timer"
    loop = _body("void loop()")
    assert "updateHeartbeat();" in loop and "updateLed();" in loop
    assert "heartbeatAt(" in _body("static void updateLed()") and "heartbeatAt(" in _body("static void stripUpdate()")


def test_audio_tasks_check_in_and_are_watched():
    for task, beat in (("static void micTask", "beatMic = millis();"), ("static void playTask", "beatPlay = millis();")):
        body = _body(task)
        assert "esp_task_wdt_add(nullptr);" in body, f"{task}: register with the hardware watchdog"
        loop_part = body[body.index("for (;;)"):]
        assert "esp_task_wdt_reset();" in loop_part and beat in loop_part, f"{task}: check in on every pass"
    hb = _body("static void updateHeartbeat()")
    assert "beatMic" in hb and "beatPlay" in hb and "hbLevel = 0" in hb


def test_heartbeat_limits_match_the_server():
    limits = (Path(__file__).resolve().parents[1] / "buddy" / "board_limits.py").read_text()
    server = {k: int(v.replace("_", "")) for k, v in re.findall(r"^(\w+) = ([\d_]+)", limits, re.M)}
    board = {k: int(v) for k, v in re.findall(r"#define (\w+) (\d+)", SRC + PLAN)}
    pairs = {"MIN_FREE_INTERNAL_KB": "MIN_FREE_INTERNAL_KB", "HEALTH_MIN_LOWEST_KB": "MIN_LOWEST_INTERNAL_KB",
             "HEALTH_MIN_STACK": "MIN_STACK_BYTES", "HEALTH_MAX_CPU": "MAX_CORE_PERCENT",
             "HEALTH_MAX_LOOP_MS": "MAX_LOOP_MS", "MIC_BLOCK_BUDGET_US": "MIC_BLOCK_BUDGET_US"}
    for fw, py in pairs.items():
        assert board[fw] == server[py], f"{fw} ({board[fw]}) on the board but {py} ({server[py]}) on the server"


def test_heartbeat_setting_reaches_the_board_and_is_saved_only_on_change():
    assert '!strcmp(type, "heartbeat")' in SRC and 'doc["heartbeat"]' in _body("static void handleJson")
    sethb = _body("static void setHeartbeat")
    assert sethb.index("if (on == heartbeatOn) return;") < sethb.index("prefs.putBool"), "don't rewrite flash each time"


def test_every_light_colour_is_explained_in_lights_md():
    # LIGHTS.md is where people look up what a light means: every colour the code shows must be in it.
    lights = (FW / "LIGHTS.md").read_text().lower()
    words = {"green", "amber", "red", "magenta", "purple", "teal", "blue", "pink", "orange", "white", "heartbeat"}
    led_code = _body("static void updateLed()") + _body("static void stripUpdate()")
    comments = " ".join(re.findall(r"//([^\n]*)", led_code)).lower()
    for w in words:
        if w in comments:
            assert w in lights, f"the lights code mentions {w!r} but firmware/LIGHTS.md doesn't explain it"
    assert "LIGHTS.md" in (FW.parent / "CLAUDE.md").read_text(), "CLAUDE.md must point at firmware/LIGHTS.md"
