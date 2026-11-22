"""Setting up the desk unit through its USB cable (no "Buddy-Setup" hotspot needed).

Buddy sends the board your Wi-Fi name and password and this Mac's address, as text lines down the
cable. Opening the port restarts the board, and it listens for these for its first few seconds.
"""
from __future__ import annotations

import glob
import socket
import time

PORT_PATTERNS = ["/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
                 "/dev/ttyACM*", "/dev/ttyUSB*"]


def my_address() -> str:
    """This Mac's address on the home network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.0.1", 9))  # no packet is sent; it just picks the network interface
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def board_ports() -> list[str]:
    return sorted(p for pat in PORT_PATTERNS for p in glob.glob(pat))


def send_setup(ssid: str, password: str, server_port: int = 8000, wait_s: float = 30.0) -> dict:
    """Restart the board, send it the Wi-Fi and this Mac's address, and report what it said back."""
    import serial  # comes with esptool (already installed for "Put Buddy on the board")
    ports = board_ports()
    if not ports:
        return {"ok": False, "message": "I can't find the board. Is it plugged into this Mac with a USB cable?"}
    host = my_address()
    lines = [f"BUDDY host {host} {server_port}"] if host else []
    if ssid:
        lines.append(f"BUDDY wifi {ssid}\t{password}")
    heard: list[str] = []
    for path in ports:
        try:
            ser = serial.Serial()
            ser.port, ser.baudrate, ser.timeout = path, 115200, 0.2
            ser.dtr = ser.rts = False  # don't drop it into "programming mode"
            ser.open()
        except Exception as e:  # noqa: BLE001 - busy or not the board
            heard.append(f"{path}: {e}")
            continue
        try:
            ser.rts = True  # a normal restart, so it's listening
            time.sleep(0.1)
            ser.rts = False
            end = time.time() + wait_s
            last_send = 0.0
            while time.time() < end:
                if time.time() - last_send > 0.5:
                    for line in lines:
                        ser.write((line + "\n").encode())
                    last_send = time.time()
                out = ser.readline().decode(errors="ignore").strip()
                if out:
                    heard.append(out)
                    if "[usb] joined" in out or ("[usb] server set" in out and not ssid):
                        return {"ok": True, "host": host, "message": f"Done: the board is on {ssid or 'your Wi-Fi'} "
                                f"and knows this Mac is at {host}. Its light should stop flashing red in a moment."}
                    if "[usb] couldn't join" in out:
                        return {"ok": False, "message": f"The board couldn't join {ssid}. Check the password, "
                                "and that it's a 2.4 GHz network (not one ending in 5G)."}
        finally:
            ser.close()
    if any("[usb] ok" in h for h in heard):
        return {"ok": True, "host": host, "message": f"Sent. The board now knows this Mac is at {host}."}
    return {"ok": False, "message": "The board didn't answer. Click 'Put Buddy on the board' first (it needs the "
            "newest board software), then try again.", "heard": heard[-6:]}
