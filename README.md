# Buddy: a DIY desk assistant

Say **"hey buddy"** and ask for the weather, set alarms, timers and reminders, or ask
anything. Buddy remembers things about you, and you can ask what it remembers, then change
or delete any of it just by talking.

How it works:
- **The desk unit** (ESP32-S3, mic and speaker) is the ears and mouth. It also keeps a copy of your alarms, so they still ring if the laptop is off.
- **The server** runs on your laptop and does the thinking:
  - Groq's free models for hearing and thinking
  - ElevenLabs for the voice, with a free offline voice as backup
  - Open-Meteo for the weather, which is free and needs no key
- **The web page** lets you test everything with your laptop's mic before the hardware arrives. It also manages alarms, memory and settings.

```
buddy/
  server/     Python server + web page (run this on your laptop)
  firmware/   ESP32-S3 code (flash once with the Arduino IDE)
```

---

## 1. Start the server (Mac)

1. Install **Python 3.12** from <https://www.python.org/downloads/macos/> if you don't have it.
2. Open Terminal and run:
   ```
   cd path/to/buddy/server
   ./run.sh
   ```
   The first run takes a few minutes. Your browser then opens **http://localhost:8000**.
3. If macOS asks *"Allow Python to accept incoming network connections?"*, click **Allow**. The desk unit can't connect without it.
4. On the web page, go to **Settings → API keys** and paste in your keys:
   - **Groq** (free): <https://console.groq.com/keys>
   - **ElevenLabs** (the free plan is fine): <https://elevenlabs.io/app/settings/api-keys>

To test without the hardware, click **Hands-free** and say "hey buddy". You can also hold
**Space** to talk, or type in the box.

The weather is set to **Richmond, London** by default. You can change it under
Settings → Home location.

## 2. Wire the desk unit

Unplug USB before wiring. Every pin number below is the number printed next to the pin on
the ESP32-S3 board.

| From | Pin | To ESP32-S3 pin |
|---|---|---|
| **INMP441 mic** | VDD | **3V3** (not 5V!) |
| | GND | **GND** |
| | SCK | **4** |
| | WS | **5** |
| | SD | **6** |
| | L/R | **GND** |
| **MAX98357A amp** | VIN | **5V** |
| | GND | **GND** |
| | BCLK | **15** |
| | LRC | **16** |
| | DIN | **7** |
| | SD, GAIN | leave unconnected |
| **Speaker** | + and − | amp's **+** and **−** speaker terminals |

**Tips for alligator clips**
- Make sure no two clips touch, especially the 5V one. Wrap each clip's metal jaw in a bit of tape.
- Keep the mic wires short, and keep them away from the speaker wires.
- The mic hole on the INMP441 is on the side without the chip. Point that side towards you.

## 3. Put the code on the ESP32-S3 (once)

1. Install the **Arduino IDE** from <https://www.arduino.cc/en/software>.
2. Add ESP32 support:
   - Go to **Settings** and, under *Additional boards manager URLs*, add:
     `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
   - Then go to **Tools → Board → Boards Manager**, search for **esp32**, and install **esp32 by Espressif** (version 3.x).
3. Go to **Tools → Manage Libraries** and install:
   - **WebSockets** by Markus Sattler
   - **WiFiManager** by tzapu
   - **ArduinoJson** by Benoit Blanchon
4. Open `firmware/buddy/buddy.ino`. The code is in the `buddy_main.cpp` tab.
5. In **Tools**, set:
   - **Board:** ESP32S3 Dev Module
   - **Partition Scheme:** Huge APP (3MB No OTA/1MB SPIFFS)
   - **Port:** the USB port your board is plugged into. Use the board's USB socket labelled **UART** or **COM**.
6. Click **Upload**. If it won't connect, hold the **BOOT** button while it says "Connecting…".

## 4. Connect it to your Wi-Fi (once)

1. After the upload, the board creates a Wi-Fi network called **Buddy-Setup** (password: `heybuddy`).
2. Join it on your phone. A setup page opens; if it doesn't, go to <http://192.168.4.1>.
3. Pick your home Wi-Fi and enter its password. Leave *server address* as **auto**, then save.
4. The board joins your Wi-Fi and finds the laptop automatically. It plays a little "hello" tune when it's connected.

If it can't find the laptop:
- Make sure the laptop is on the same Wi-Fi and `./run.sh` is running.
- If it still can't, hold the BOOT button for 10 s to reopen setup. Type in the laptop's address this time. The server prints it when it starts, e.g. `192.168.1.23`.

## Using it

| Say | What happens |
|---|---|
| "Hey buddy, what's the weather?" | Temperature now, the high and low, whether it will rain and when |
| "…tomorrow?" / "…this week?" | Tomorrow's forecast, or the week ahead |
| "Wake me up at half seven on weekdays" | A repeating alarm (also "every day", "on Mondays", …) |
| "Set an alarm for 6am tomorrow" | A one-off alarm |
| "Set a timer for 10 minutes" | A timer |
| "Remind me to call Mum at 6" / "in 20 minutes" | Chimes, then says the reminder |
| "What alarms do I have?" / "Cancel my 7:30 alarm" | Lists or removes alarms |
| "Stop" / "Snooze" | Stops or snoozes a ringing alarm; snooze is 9 minutes |
| "Remember that my sister is called Emma" | Saved in memory for good |
| "What's in your memory?" | Reads out what it knows; say "more" for the rest |
| "Forget about my sister" / "Actually she's called Emily" | Deletes or changes a memory |
| "Clear your memory" | Asks you to confirm first; a backup is kept |
| "Turn it up" / "Volume 4" | Volume from 0 to 10 |
| "Say that again" | Repeats the last answer |
| Anything else | Answered by the AI. It searches the web when it needs to. Say "think harder" for tough questions |

**Button, lights and sounds**

The **BOOT button** does three things:
- Tap it to talk, or to stop an alarm.
- Hold it 3 s to mute or unmute the mic.
- Hold it 10 s to reset Wi-Fi.

The on-board light shows what Buddy is doing:

| Light | Meaning |
|---|---|
| Blue | Listening |
| Purple | Thinking |
| Green | Speaking |
| Flashing amber | An alarm is ringing |
| Red | Mic is muted |
| Dim red blink | Can't reach the laptop |

**Memory.** Buddy learns from your chats in the background and tidies its memory once a day. It merges duplicates and drops trivia, but never anything you asked it to remember. You can see and edit everything on the web page's **Memory** tab.

**Alarms without the laptop.** The desk unit stores the next 8 days of alarms, so they still ring if the laptop is asleep or off.

## Free plans and what happens at the limits

- **Groq (free):** there are per-minute and per-day limits. Everyday chat uses a small fast model, and only big jobs use the larger one (tough questions, "think harder", and tidying memory). If a model is busy or at its limit, Buddy switches to another one automatically.
- **ElevenLabs (free):** about 10,000 credits a month. When they run out, Buddy switches to the free offline British voice by itself. The web page shows how much you have left.
- **Weather (Open-Meteo):** free, with no key needed.

## Troubleshooting

- **It doesn't hear "hey buddy".** Check the *Mic level* meter on the web page (set *Meters show* to the desk unit). If it barely moves:
  - Go to **Settings → Listening → Desk unit mic boost** and turn it up.
  - You can also lower **Wake sensitivity**.
- **It wakes up by itself.** Raise **Wake sensitivity**.
- **The sound is quiet or distorted.** Say "volume 8". If it's distorted even at low volume, check the amp's GND and 5V clips.
- **See what the board is doing.** In the Arduino IDE, open **Tools → Serial Monitor** at 115200 baud.

## For developers

Run the tests:
```
cd server
pip install -r requirements.txt pytest pytest-timeout
python -m pytest tests/
```

`tests/test_voice_e2e.py` feeds real speech through the whole pipeline. To run it, generate clips with `tests/make_clips.py`, then set `BUDDY_TEST_CLIPS`.

Model credits (all open licences):
- the "hey buddy" wake word by Benjamin Paine (Apache-2.0)
- feature models from openWakeWord (Apache-2.0)
- Silero VAD (MIT)
- Piper voices (see their model cards)
