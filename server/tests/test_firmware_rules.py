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
