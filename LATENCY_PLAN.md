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
