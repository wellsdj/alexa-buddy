# Plan: make Buddy answer faster

Goal: the time from the end of your speech to Buddy's first sound goes from 1.5–2 s to **800 ms or less**, with no change you can hear in the answers.

Status: **not started.** I have only read the code so far. Nothing is changed yet.

---

## How it works now

Every stage waits for the stage before it to finish completely.

| # | Stage | Where in the code | Waits for |
|---|---|---|---|
| 1 | **End-of-speech wait.** Buddy waits for 800 ms of silence before it decides you've finished. | `vad.py` `Endpointer`, setting `end_of_speech_ms` = 800 | — |
| 2 | **Transcribe** the whole clip on the Mac (Parakeet, or Moonshine as backup). | `stt.py` `LocalSTT.transcribe` | stage 1 |
| 3 | **Quick commands** (timers, weather and so on) are matched without Haiku. Everything else goes to Haiku. | `brain.py` `handle` → `intents.match` | stage 2 |
| 4 | **Build the system prompt.** This may fetch the weather if the saved copy is more than 10 minutes old. | `brain.py` `system_prompt` → `weather.context_line` | stage 3 |
| 5 | **Haiku answer.** Not streamed: Buddy waits for the whole reply. Thinking is on (effort "low"), with `max_tokens` of at least 4096. A tool call adds another full round. | `claude.py` `chat`, `brain.py` `_tool_loop` | stage 4 |
| 6 | **Fact-check.** A *second, separate* Haiku call checks the answer before Buddy says it. It's on by default. | `brain.py` `_make_sure` / `_check`, setting `fact_check` | stage 5 |
| 7 | **Reword / missed alarm.** Another Haiku call for some quick replies. | `brain.py` `_reword`, `_mention_missed` | stage 6 |
| 8 | **ElevenLabs.** The whole answer goes in one HTTP request. Audio streams back as PCM 16 kHz. A new connection is opened each time. | `tts.py` `ElevenLabsTTS.stream` | stage 7 |
| 9 | **Half-second hold.** The Mac collects 0.5 s of audio before sending any of it to the ESP32, to stop stutter. | `hub.py` `Session._speak` (`held`) | stage 8 |
| 10 | **ESP32 plays** as soon as audio arrives. It has no buffer of its own before it starts playing. | `buddy_main.cpp` speaker task | stage 9 |

### My guess at where the time goes (to be checked with real numbers)

- **800 ms:** the end-of-speech wait. This alone uses up the whole 800 ms target.
- **500 ms:** the half-second hold.
- **One full Haiku round:** the fact-check.
- The rest: Haiku waiting for its full answer (thinking included), and a new ElevenLabs connection every time.

---

## Step 1: Measure (no behaviour change)

1. Add a small `timing.py` that keeps one timer for each turn. It is carried through the async tasks, so no function signatures change.
2. Record these moments:
   - you stopped talking (worked out as the endpoint time minus the trailing silence)
   - endpoint fired
   - transcript done
   - Haiku request sent
   - first Haiku token (the same as "reply done" until streaming is added)
   - first sentence complete
   - fact-check start and end
   - TTS request sent
   - first audio byte received
   - first audio sent to the ESP32
3. Print one line per turn, for example:
   `TURN 3 | silence-wait 800 | stt 140 | haiku 620 | fact-check 540 | tts first byte 310 | hold 500 | TOTAL 2910 ms`
4. Also save each turn to `data/timings.jsonl`. A small command, `python -m buddy.timing_report`, will show the average and middle value of the last 10 turns.
5. Add a setting, `timing_log` (on by default).
6. Baseline: ask 10 normal questions ("what's the capital of Peru", "tell me a fun fact"…) with no timers or weather. Then send me the report.

"First audio played" will be measured on the Mac, as the moment audio is sent to the ESP32. The ESP32 starts playing about 20–50 ms later over Wi-Fi. Measuring it on the ESP32 itself would mean flashing new firmware. I'll ask before doing that.

---

## Step 2: Changes, one commit each, each with an on/off setting

| | Change | Setting (default) | What to test / what the log should show |
|---|---|---|---|
| a | **Stream Haiku into ElevenLabs.** Use the streaming Messages API. Send text to the ElevenLabs WebSocket `stream-input` as it arrives. Play audio chunks as soon as they come. Fact-check can't run on a streamed answer, so it becomes check-after-speaking or is skipped for chat. | `stream_reply` (on) | Haiku and TTS times overlap. "First audio byte" comes soon after "first sentence complete". |
| b | **Lowest-latency ElevenLabs model**, checked against the current ElevenLabs docs (it's `eleven_flash_v2_5` now). Same voice. Keep `pcm_16000`, which the ESP32 plays with no decoding. | `elevenlabs_model` | The TTS first byte gets smaller. |
| c | **Warm connections.** Open the ElevenLabs WebSocket and the Anthropic connection when the wake word fires. Reuse them, and reconnect if they time out. | `warm_connections` (on) | The first-turn TTS and Haiku times match later turns. |
| d | **Shorter end-of-speech wait**, for example 800 → 450 ms. Parakeet and Moonshine (the speech models) are not streaming models. The options are below this table. | `end_of_speech_ms` | The silence wait drops. **Risk:** it may cut you off mid-sentence. |
| e | **Faster Haiku.** Thinking off. A shorter system prompt with prompt caching. Answers of one or two spoken sentences. A low `max_tokens`. | `fast_haiku` (on) | Faster Haiku first token. Check the answers still sound as good. |
| f | **Interruption.** If you talk while Buddy is speaking: stop playback, cancel the Haiku stream and clear the TTS buffer. | `barge_in` (on) | Buddy goes quiet within about 200 ms. **Risk:** its own voice could set it off (no echo cancelling yet). |

**No gap after the first sentence (part of a).** Buddy shouldn't say sentence 1, go quiet, then say sentence 2. Here's how it will start speaking:
1. Haiku writes much faster than Buddy talks: about 2 seconds of speech arrive every 0.1–0.2 s.
2. Buddy starts speaking once two things are true:
   - the first sentence's audio has arrived;
   - the 2nd sentence has started arriving, or the answer is finished. One-sentence answers don't wait.
3. While Buddy speaks, the Mac keeps at least about 0.4 s of voice queued ahead. If it ever runs short, the next part is held back until there's enough again, rather than playing in bits.
4. The log line shows "gaps" (any moment Buddy ran out of audio mid-answer). This should stay at 0.

Settings: `start_after_sentences` (default 1, plus the start of the next one) and `min_audio_ahead_ms` (400). If there are still gaps, raise either setting. That costs about 100–200 ms more wait at the start.

Also: the **half-second hold** can shrink to about 150 ms once audio is streaming steadily (setting `play_hold_ms`). **Risk:** stutter on weak Wi-Fi. I'll tell you if it happens.

### Options for d (transcribing while you speak)

1. **Re-transcribe the clip so far** every 0.5 s while you talk, so the final pass only covers the last bit. Free, but uses more Mac CPU.
2. **Switch to a streaming model** (for example sherpa-onnx streaming Zipformer). Free, but may be less accurate than Parakeet.
3. **Cloud streaming speech-to-text.** This is a paid service, so I won't use it unless you say yes.

---

## Rules

- One commit per change, and each change has its own on/off setting.
- The wake word, the voice and the hardware stay the same.
- No new paid services without asking.
- If audio stutters or the ends of your sentences get cut off, I'll say so and explain the trade-off.

## Found while reading (not fixed yet)

- **Bug:** `hub.py` is missing the line `async def _accept(...)` (around line 858). A soft "hey buddy" that gets checked, and room talk meant for Buddy, will crash instead of being answered.
- **Tests:** 9 tests already fail before any change (listening, memory paging, weather parsing, speaking).
- I couldn't open the x.com link from here, because x.com is blocked in this session.

## Step 3: Report

When all the changes are done, the report will include:
- a before-and-after table of each stage's time
- every setting and its default
- every problem that is still open

---

# Later: tidy up Buddy and Study Buddy

Status: **not started.**

Covers both web pages: Buddy (`server/web/index.html`, `app.js`, `style.css`) and Study Buddy (`study.html`, `study.js`, `study.css`, plus the online copy on Vercel).

1. **Remove what isn't needed**
   - Text: long explanations, repeated hints and labels that say the obvious.
   - Information: settings and status details you never use. Move rarely used items under an "Advanced" section rather than delete them.
   - Code: unused functions, old settings, dead CSS and leftover code in the server and the pages.
     - Example: the broken `_accept` code in `hub.py` gets fixed or removed.
2. **Make important things stand out**
   - Make important things bigger and **bold**: Buddy's state, the last thing heard and said, errors, and today's lesson or questions in Study Buddy.
   - Make less important things smaller and lighter.
3. **Make buttons feel like real buttons**
   - Make them bigger and easier to press on a phone, with a clear button shape and colour.
   - When pressed, they visibly push down, and they change straight away. A spinner shows if something takes longer than a moment.
   - The same button style is used everywhere.
4. **Check**
   - Take before-and-after screenshots of every page, on a phone and on the Mac.
   - Make sure nothing you use was removed.
   - Run the tests.
5. **One commit per page.** You check each one before I move on.

---

# Later: more ideas

Status: **not started.**

1. **Fix the broken bits first**, before the speed work.
   - Put back the missing `async def _accept(...)` line in `hub.py`. Without it, a quiet "hey buddy" that gets checked silently crashes, and Buddy can get stuck and stop waking up.
   - Fix the 9 tests that already fail.
2. **Find the "light comes on, then goes off" bug.** You say a quiet "hey buddy" and the light comes on, but Buddy doesn't take in what you say and the light goes off again.
   - **Confirmed:** the light stays on for about 6 s before it goes off, so this is a timeout.
   - Cause: the wake word heard you, but your voice was too quiet for the speech detector (it needs a speech score of 0.5). So Buddy thinks nobody is talking and gives up after 6 s.
   - Check it in the Activity tab and in the timing logs, then fix it. Options: a lower speech threshold just after a wake, or more mic gain.
3. **Echo cancelling.** The Mac removes Buddy's own voice from the mic sound. Then interruption (change f) only reacts to *you*, not to Buddy hearing itself.
4. **A speed panel on the Buddy page.** It shows each turn's timings (from Step 1), with the average of the last 10.
5. **Updating the ESP32 over Wi-Fi.** Put new firmware on Buddy from the Mac, with no USB cable, which helps once it's in the box. It falls back to the old firmware if the update fails.
6. **Ready-made audio for common replies** such as "Done", "Timer set" and "Okay". They are made once and saved, so they play instantly and save ElevenLabs credits.

---

# Later: voice recognition (knows who is speaking)

Status: **not started.** I've checked that the free model works, but no code is written.

- **Model:** CAM++. It's free, runs on the Mac and is about 29 MB. It uses the same speech library Buddy already has, so there are no new paid services.
- **No training needed.** You only record yourself once:
  1. Read about 8 short sentences to the desk unit, from where you usually sit. That's about 30 seconds.
  2. Buddy saves a "voiceprint" in its private data folder. It never goes to GitHub.
  3. Other people can do the same, if you want Buddy to know them by name.
- **Then each turn,** Buddy checks the voice at the same time as it transcribes, so it adds no delay. It notes who spoke: you, another known person, or someone unknown.
- **Set up on the Buddy page:** a new "Your voice" tab, with a name box, the sentences to read and a progress bar. Recordings can be deleted. The Mic test shows "sounds like you (0.72)" on each line, so you can check it works.
- **What Buddy does with it** is to be decided later. Options:
  - only answer you
  - only you can change alarms
  - personal memories for each person
