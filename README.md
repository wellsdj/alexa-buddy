# Buddy: a DIY desk assistant

Say **"hey buddy"** and ask for the weather, set alarms, timers and reminders, or ask
anything. Buddy remembers things about you, and you can ask what it remembers, then change
or delete any of it just by talking.

How it works:
- **The desk unit** (ESP32-S3, mic and speaker) is the ears and mouth. It also keeps a copy of your alarms, so they still ring if the laptop is off.
- **The server** runs on your laptop:
  - **Hearing:** speech-to-text runs on the laptop itself (free, private: your voice never leaves the house)
  - **Thinking:** Claude Haiku, with web search; it asks before thinking hard about big questions
  - **Knowing you're talking to it:** Groq's free models (optional key), or simple phrase rules
  - **Voice:** ElevenLabs, with a free offline voice as backup
  - **Weather:** Open-Meteo, which is free and needs no key
- **The web page** lets you test everything with your laptop's mic before the hardware arrives. It also manages alarms, memory and settings.

```
alexa-buddy/
  server/     Python server + web page (run this on your laptop)
  firmware/   ESP32-S3 code (flash once with the Arduino IDE)
```

---

## 1. Install and start Buddy on your Mac (5 minutes)

1. Open **Terminal**: press Cmd+Space, type `terminal`, press Enter.
2. Copy this whole line, paste it into Terminal and press Enter:
   ```
   curl -fsSL https://raw.githubusercontent.com/wellsdj/alexa-buddy/main/install.sh | bash
   ```
   Wait a few minutes. You don't need to install anything else, not even Python.
3. Your browser opens on Buddy's page. Follow the orange **"A few quick steps"** box to paste in your keys:
   - **Claude** (needed: Buddy's brain). Make it at [console.anthropic.com](https://console.anthropic.com) and add some
     credit under **Billing**. It's pay as you go, separate from a Claude Pro or Max subscription, and costs pennies.
   - **ElevenLabs** (free, for the nice voice).
   - **Groq** (free, optional: helps Buddy tell when you're talking to it).
4. If macOS asks *"Allow Python to accept incoming network connections?"*, click **Allow**.

You now have three buttons on your Desktop:
- **Start Buddy**: double-click it whenever you want Buddy running. Leave its window open.
- **Put Buddy on the board**: sets up the ESP32 (step 3 below).
- **Update Buddy**: gets the latest version. Your memories, alarms and keys are kept.

To try it before the hardware arrives, click **Hands-free** on the web page and say "hey buddy". You can also hold **Space** to talk, or type in the box.

To make Buddy start by itself every time you log in, so you never need the Start button, paste this into Terminal:
```
~/Buddy/server/autostart.sh on
```
To turn that off again, paste `~/Buddy/server/autostart.sh off`.

The weather is set to **Richmond, London** by default. You can change it under Settings → Home location.

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

## 3. Put Buddy on the ESP32-S3 (once)

1. Plug the board into the Mac with a USB cable. Use the board's socket labelled **USB**; if nothing happens, try the other socket. Some cables only charge, so if the board isn't found, try another cable.
2. Double-click **Put Buddy on the board** on your Desktop.
3. Wait about 30 seconds until it says **Done!**

If it says the board didn't answer, it shows you what to do:
1. Hold the **BOOT** button.
2. While holding it, tap **RST**.
3. Let go, then press Enter.

<details><summary>Advanced: build it yourself with the Arduino IDE instead</summary>

1. Install the Arduino IDE.
2. Add ESP32 support:
   - In **Settings → Additional boards manager URLs**, add `https://espressif.github.io/arduino-esp32/package_esp32_index.json`.
   - In **Boards Manager**, install **esp32 by Espressif** 3.x.
3. Install these libraries: **WebSockets** (Markus Sattler), **WiFiManager** (tzapu) and **ArduinoJson** (Benoit Blanchon).
4. Open `firmware/buddy/buddy.ino` and set these in **Tools**:
   - Board: **ESP32S3 Dev Module**
   - Partition Scheme: **Huge APP (3MB No OTA/1MB SPIFFS)**
5. Click Upload.

</details>

## 4. Connect it to your Wi-Fi (once)

1. After the upload, the board creates a Wi-Fi network called **Buddy-Setup** (password: `heybuddy`).
2. Join it on your phone. A setup page opens; if it doesn't, go to <http://192.168.4.1>.
3. Pick your home Wi-Fi and enter its password. Leave *server address* as **auto**, then save.
4. The board joins your Wi-Fi and finds the laptop automatically. It plays a little "hello" tune when it's connected.

If it can't find the laptop:
- Make sure the laptop is on the same Wi-Fi and Buddy is running (double-click **Start Buddy**).
- If it still can't, hold the BOOT button for 10 s to reopen setup. Type in the laptop's address this time. It's shown in the Start Buddy window as "ESP32 server address", e.g. `192.168.1.23`.

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
| Anything else | Answered by Claude Haiku. It searches the web when it needs to |
| A big question (plans, long writing, tricky problems) | Buddy asks "Want me to have a proper think about it?" Say yes for a deeper answer, or no for a quick one. Saying "think it through properly" skips the question |
| "Off" | Buddy stops listening to the room until you say "hey buddy" again |

**How Buddy knows you're talking to it**
- **"Hey buddy"** after a moment of quiet always wakes it.
- **While it's on**, it hears everything near the desk unit and answers what's meant for it, so
  "let me ask you a question" works without saying "hey buddy". Chat between people, the TV, or
  "my buddy Dave" mid-sentence are ignored. The speech is turned into text on your laptop; with a
  Groq key, that text is sent to Groq to judge whether it was meant for Buddy.
- **"Off"** stops that. Then only "hey buddy" wakes it, which also turns it back on.
- To only ever use "hey buddy", untick **Settings → Listening → listen to everything nearby**.

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

- **Claude (pay as you go):** Haiku costs about 10p per million words read and 50p per million written, so everyday use is pennies a month. A web search is about 1p. If the key stops working or the credit runs out, Buddy says so; alarms, timers and weather keep working.
- **Groq (free, optional):** only judges whether room talk is for Buddy. It has per-minute and per-day limits; if it's busy, Buddy uses simple phrase rules until it's free again.
- **ElevenLabs (free):** about 10,000 credits a month. When they run out, Buddy switches to the free offline British voice by itself. The web page shows how much you have left.
- **Weather (Open-Meteo):** free, with no key needed.

## Troubleshooting

- **It doesn't hear "hey buddy".** Check the *Mic level* meter on the web page (set *Meters show* to the desk unit). If it barely moves:
  - Go to **Settings → Listening → Desk unit mic boost** and turn it up.
  - You can also lower **Wake sensitivity**.
- **It wakes up by itself.** Raise **Wake sensitivity**. The **Activity** tab shows every sentence Buddy heard while listening to the room, and whether it thought it was meant for it.
- **It says its ears aren't downloaded.** Double-click **Update Buddy**: the first time it downloads about 240 MB.
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
- Moonshine speech-to-text by Useful Sensors (MIT), run with sherpa-onnx (Apache-2.0)
