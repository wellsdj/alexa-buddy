# Buddy: the plan

What we're doing, phase by phase. Tick things off as they're done.

**Where Buddy lives at the end:** everything on **Vercel** (free, no card), so the laptop
doesn't need to be on. Oracle was dropped because it wants a credit card.

```
Desk unit (on your desk)  ──internet, encrypted──►  Buddy on Vercel
  wake word, mic, speaker,                             brain, memory, Diary, calendar,
  alarms and timers                                    settings, web page (phone too)
```

---

## Phase 1: Claude Haiku as Buddy's brain

Swap Groq's free models for Claude Haiku, using an Anthropic API key.

**What you do**
- [ ] Get an API key at [console.anthropic.com](https://console.anthropic.com) and add some credit.
  - This is separate from a Claude Pro/Max subscription, which can't be used by apps like Buddy.
  - Anthropic needs a card to buy API credit. No card = stay on Groq (free) and skip this phase.
- [ ] Double-click **Update Buddy**, then **Start Buddy**.
- [ ] Paste the key into Buddy's web page: Settings → API keys → **Claude** → Save.

**What gets built**
- [x] A **Claude key** box on the web page, which says straight away whether the key works.
- [x] A **Thinks with** choice in Settings: Claude when its key is set (Groq as backup), or Groq only.
- [x] Claude Haiku for everyday questions (quick, low effort) and for "think harder" and big jobs
  (thinks harder). Either can be switched to Sonnet or Opus in Settings.
- [x] Groq stays as the ears (speech to text) and ElevenLabs stays as the voice.
- [x] Memory, alarms, reminders and weather keep working exactly as now. Web searches use Claude's search.
- [x] Safety net: if the Claude key fails or runs out of credit, Buddy switches back to Groq by itself.
- [x] README updated with the new steps.

### Phase 1b: Haiku only, ears on the Mac, smarter listening (done)

- [x] Claude Haiku does all the answering, thinking and web research (no Groq answers or fallback).
- [x] Deep thinking only after Buddy asks ("Want me to have a proper think about it?") and you say yes.
- [x] Ears run on the Mac (Moonshine speech-to-text, free and private); no Groq for hearing.
- [x] While on, Buddy hears everything nearby and answers what's meant for it ("let me ask you a
  question" works without the wake word). Groq's free model (optional key) judges each sentence;
  without it, simple phrase rules do.
- [x] "Hey buddy" after quiet always wakes it; "buddy" mid-sentence is judged in context instead.
- [x] "Off" stops room listening until the next "hey buddy".
- Note for phase 2: the ears run on the laptop, so moving everything to Vercel needs a plan for
  hearing (e.g. ears on a small home computer, or a speech-to-text service).

---

## Phase 2: Move everything to Vercel (no laptop needed)

**What gets built**
- [ ] Buddy's brain reshaped to run on Vercel: answer a question, then sleep.
- [ ] Settings, memory and keys stored in Vercel's free storage.
- [ ] The web page served by Vercel, so it works from your phone anywhere.
- [ ] A password (access token) so only you and your desk unit can use it.
- [ ] New desk unit software:
  - listens for the wake word itself
  - records the question and sends it to Vercel, **encrypted**
  - plays the answer Vercel sends back
  - keeps alarms and timers on the board, so they ring even if the internet drops
- [ ] Wake word: "Hey Buddy" needs a small custom model trained for the board.
  A ready-made word (e.g. "Hey Jarvis") is used until it's ready.

**What you do**
- [ ] In Vercel → alexa-buddy → Settings → Build and Deployment → **Ignored Build Step**,
  switch back from "Don't build anything" to **Automatic** (it was turned off to stop red errors).
- [ ] Put the new software on the board with the **Put Buddy on the board** Desktop button.
- [ ] Re-do the Buddy-Setup Wi-Fi page once, with the new server address and access token.

Notes: the Vercel project **alexa-buddy** already exists and is linked to GitHub
(root directory: `server`). Claude can read its deployments and logs but can't change its settings.

---

## Phase 3: The new memory system

Three layers, like a filing system:

1. **Diary (cold storage).** Every question and answer, with date and time, one file per day.
   Never deleted. Buddy doesn't read it unless it needs to look something up.
2. **Summaries.** A short summary of each day; weekly and monthly roll-ups. Makes searching quick.
3. **Core memory (front of mind).** About a page of the important facts about you.
   The only layer Buddy reads before every answer.

- [ ] Diary, summaries and core memory stored on Vercel.
- [ ] **Recall:** Buddy can search the Diary when asked, e.g. "what did I ask you yesterday?",
  "what was the weather last Monday?", "what did you tell me about that film?".
- [ ] **Nightly review**, a Vercel scheduled job some time between 8 and 9pm, running as long as it needs:
  1. Back up everything.
  2. Read the whole day.
  3. Write the day summary (plus the week on Sundays, the month at month end).
  4. Add new facts to core memory.
  5. Update facts that changed.
  6. Merge duplicates.
  7. Move out what's no longer needed (e.g. "dentist on Tuesday" after Tuesday). It stays in the Diary.
  8. Never touch things you asked it to remember.
- [ ] Anything said after the review goes into the next night's review.
- [ ] The web page shows the Diary, summaries and core memory, and lets you edit them.

---

## Phase 4: Calendar

- [ ] Connect Buddy to your existing calendar so events show on your phone too.
  **To decide: Google Calendar or Apple Calendar.**
- [ ] Add, move and cancel events by voice.
- [ ] Heads-up before events.
- [ ] "What's on today?" in the morning briefing.

---

## Phase 5: Voice check

- [ ] Buddy learns your voice (WeSpeaker) so it knows when it's you talking.
- [ ] Enrol from your recording ("New ai training.m4a" in the Desktop "Buddy voice" folder).
  The voice recording never goes on GitHub.

---

## Sound: Buddy plays through the VQ Hepburn Mk II radio

The radio has 2 full-range speakers + 2 tweeters, its own amp and tuning: better than any small
speaker we'd add. Buddy's voice goes into its **AUX IN**, so the radio doesn't need opening.

**What to buy**
- [ ] A **PCM5102A** DAC board (Amazon UK, about £4–7). Pick one whose photos show the pins fitted.
- [ ] A 3.5mm to 3.5mm audio cable (if the radio doesn't have one).
- [ ] Only if there's a hum: a "ground loop isolator" (about £5).

**Wiring (needs the iron)**

| PCM5102A pin | ESP32 pin |
|---|---|
| VIN | 3V3 (or 5V once IN-OUT is bridged) |
| GND | GND |
| BCK | 15 (same as the amp's BCLK) |
| LCK | 16 (same as the amp's LRC) |
| DIN | 7 (same as the amp's DIN) |
| SCK | GND |

- [ ] Back pads: 1 → L, 2 → L, **3 → H**, 4 → L (otherwise it stays silent).
- [ ] The little amp and speaker can stay connected or be removed.
- [ ] Cable from the PCM5102A's headphone socket into the radio's AUX IN. Radio on AUX, volume about 3/4;
  Buddy controls the volume from there.

**To check**
- [ ] Does the radio switch itself off after a while of silence on AUX? If yes, look for an
  auto-standby setting to turn off; otherwise Buddy needs a small software fix (keeps it awake).
- [ ] Keep Buddy's mic away from the radio's speakers, so it doesn't hear itself.

### Buddy lives inside the radio (decided)

- [ ] Open the radio carefully (photos at every step, screws in labelled groups, watch the wires).
- [ ] Find a spot inside for the ESP32 + PCM5102A, away from the speaker magnets and any metal
  (so Wi-Fi still works).
- [ ] Sound: wire the PCM5102A's output straight onto the AUX IN socket's legs inside
  (left, right, ground), so no cable sticks out. Select AUX with the radio's SOURCE button.
- [ ] Power: the radio has a **USB charging socket** on the back (5V) and runs from a **9V 2A** adapter.
  - First test: plug Buddy's USB cable into the radio's USB socket. Does Buddy stay on when the
    radio is switched to standby? If yes, power Buddy from that socket's 5V and GND inside: one plug for both.
  - If the USB socket switches off in standby: a small 9V to 5V converter board (about £3)
    fed from the radio's 9V input instead.
- [ ] Opening: screws are most likely under the 4 round feet in the bottom corners.
- [ ] Mic: a 2–3 mm hole in the case, on the top or back, away from the speakers, with a bit of foam
  around the INMP441 so it doesn't pick up vibration.
- [ ] LED ring behind the front grille or around the display (later, with the phase 2 board software).
- [ ] The radio still works as a normal radio.

---

## Hardware notes

- The amp runs from **3V3** for now, because the board's 5V pin is dead on the USB socket.
  For full volume later: bridge the board's **IN-OUT** solder pads, then move the amp's Vin back to **5V**.
- Optional: change the hello tune and the wake chime.
