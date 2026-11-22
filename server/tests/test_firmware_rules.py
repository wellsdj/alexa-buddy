"""Checks on the desk unit's program that don't need the board itself.

Each one is here because the board once broke this way:
- the SD card was mounted before Wi-Fi started, and with a card in Wi-Fi ran out of memory
  ("Expected to init 4 rx buffer, actual is 0"), so the board restarted over and over;
- the prebuilt program on GitHub didn't match the source, so "Put Buddy on the board" put an old version on;
- board messages went out of only one USB socket, so Show board log saw nothing.
"""
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


# ---- memory budget (firmware 1.7.7/1.7.8 bug: with the SD card in only 15 KB was free after start-up and 4 KB after
# connecting, so the board sat silent: it never sent its hello and gave up ~39 s later)
BUDGET_KB = 75  # buffers + task stacks together (1.7.8 had 106 and ran dry with the SD card in; 1.7.9 has 72)


def _kb(name: str) -> float:
    """Size in KB of a `#define NAME (16 * 1024)` or `#define NAME 6144` line."""
    m = re.search(rf"#define {name}\s+\(?\s*(\d+)(?:\s*\*\s*(\d+))?", SRC)
    assert m, f"{name} isn't defined in buddy_main.cpp"
    return int(m.group(1)) * int(m.group(2) or 1) / 1024


def test_buffers_and_stacks_fit_the_memory_budget():
    buffers = sum(_kb(n) for n in ("MIC_BUFFER_BYTES", "SPK_BUFFER_BYTES", "REC_BUFFER_BYTES"))
    stacks = sum(_kb(n) for n in ("STACK_MIC", "STACK_PLAY", "STACK_AEC", "STACK_REC", "STACK_UPLOAD"))
    total = buffers + stacks
    assert total <= BUDGET_KB, (
        f"buffers {buffers:.0f} KB + task stacks {stacks:.0f} KB = {total:.0f} KB, over the {BUDGET_KB} KB budget: "
        "the board runs out of memory after connecting to the server")
    assert _kb("SPK_BUFFER_MIN") <= _kb("SPK_BUFFER_BYTES")


def test_task_stacks_and_buffers_use_the_budgeted_sizes():
    # a number typed straight into these calls would get around the budget above
    assert not re.search(r"xTaskCreatePinnedToCore\(\w+, \"\w+\", \d+", SRC), "use the STACK_* sizes"
    assert not re.search(r"xStreamBufferCreate\(\s*\d", SRC), "use the *_BUFFER_BYTES sizes"
    assert not re.search(r"xStreamBufferCreate\([^;]*\d+ \* 1024", SRC), "use the *_BUFFER_BYTES sizes"


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
