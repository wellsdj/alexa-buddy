# Pocket Buddy: recording lessons at school

The board records lessons onto a micro SD card and sends them to the Buddy server (on the Mac) when it is back on
your home Wi-Fi. The server checks each one is a real lesson (over 2 minutes, with real words), works out the
subject and date, and it shows up in Study Buddy.

## Wiring (micro SD reader, 3.3 V SPI type, e.g. the Adafruit 3V-only breakout)
| Reader pin | Board pin |
|---|---|
| CS | GPIO 10 |
| MOSI (DI) | GPIO 11 |
| SCK (CLK) | GPIO 12 |
| MISO (DO) | GPIO 13 |
| VCC (3V3) | 3V3 |
| GND | GND |

Card: any micro SD, FAT32 (the usual factory format). The board makes a folder called `rec` on it.

## Using it
- **Hold the button 2-4 seconds, then let go**: starts a recording. The light pulses red while it records.
- **Do the same again**: stops it. (A tap does nothing while recording, so it can't be stopped by accident.)
- While you hold, the light shows what letting go will do: **magenta** = record / stop, **red** (after 4 s) = mute.
- Quick red flicker = it couldn't record (no card in, or the card is full).
- Hold **4-10 seconds** and let go: mute / unmute the mic, as before. Hold **10 seconds**: forget Wi-Fi.
- Power it from a USB power bank. It needs no Wi-Fi at school.
- Back home: it joins your Wi-Fi by itself, finds the Mac (Buddy must be running) and sends each recording
  (the light pulses teal). Each is deleted from the card once the Mac has it. Recordings under a minute are deleted.
- If the power dies mid-lesson, the recording is repaired the next time the board starts.
