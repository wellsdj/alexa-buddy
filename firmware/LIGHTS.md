# What Buddy's lights mean

Buddy's desk unit has two lights that show the same things: the **small light on the board** and the
**LED strip behind the grille**. Look up what you see below. If two things are happening at once, the one
higher in the table wins (for example, a button hold shows over an alarm).

When the lights change in `firmware/buddy/buddy_main.cpp` (`updateLed()` and `stripUpdate()`), update this page
in the same commit (a test checks every light colour in the code is listed here).

## At a glance

Pattern key: ▬ steady · ∿ breathing or pulsing · ● one short blip every 3 s · ●● two blips every 3 s ·
⚡ fast flashing.

| | You see | It means | What to do |
|---|---|---|---|
| 🟢 ● | **Dim green blip every 3 s** | Idle and healthy: the heartbeat | Nothing |
| 🟡 ●● | **Dim amber double blip every 3 s** | Idle, but close to a memory, CPU or storage limit | `curl -s localhost:8000/api/board_health` says which, and what to do |
| ⚫ | **Nothing at all, for more than 3 s, while idle** | Something is stuck (or the heartbeat is switched off) | Wait 5 s: if an audio task is stuck, the board restarts itself. If not, read the board log |
| 🔴 ● | **Slow dim red blink** (on for 1 s, every 3 s) | Can't reach the Buddy server on the Mac (Wi-Fi down, or Buddy isn't running) | Start Buddy on the Mac; check Wi-Fi |
| 🔴 ▬ | **Solid red** | Microphone muted | Hold the button 4 s and let go to unmute |
| 🔴 ⚡ | **Fast red flicker** (for a few seconds) | Couldn't record: no SD card, or it's full | Check the card |
| 🔴 ▬ 3 s | **Bright red** (3 s) | A recording was too short (deleted), or couldn't be sent home (it tries again later) | Nothing, usually |
| 🟢 ▬ 3 s | **Bright green** (3 s) | A recording was saved, or sent home | Nothing |
| 🟣 ∿ | **Slow purple pulse** | Recording a lesson | Hold 2 s and let go to stop |
| 🩵 ∿ | **Teal, breathing** | Sending a lesson to the Mac | Leave it on Wi-Fi until it finishes |
| 🔵 ▬ | **Blue** | Listening (after "hey Buddy" or a button tap) | Talk |
| 🟣 ∿ | **Purple, breathing** (strip: a bright dot sweeping) | Thinking about an answer | Wait |
| 🟢 ∿ | **Green-cyan, moving with the voice** | Speaking | Tap the button to stop it |
| 🩷 ∿ | **Pink, fading slowly in and out** | Doing a job in the background (e.g. on a website) | Wait, or ask what it's doing |
| 🟠 ⚡ | **Orange, flashing twice a second** | An alarm or timer is ringing | Tap the button, or say "stop" |
| 🟣 ▬ | **Magenta, while holding the button** (after 2 s) | Let go now to start or stop recording a lesson | |
| 🔴 ▬ | **Red, while holding the button** (after 4 s) | Let go now to mute or unmute | Keep holding to 10 s to forget Wi-Fi |
| ⚪ ▬ | **Dim white** (just after switching on) | Starting up | A few seconds |
| 🩵 ▬ | **Steady teal** (for up to 20 s) | Joining a Wi-Fi network (set up over USB, or after you asked to change network) | Wait |
| 🩵 ▬ | **Bright teal, staying on** (up to 5 minutes) | The "Buddy-Setup" Wi-Fi hotspot is open (no saved network) | On your phone, join "Buddy-Setup" (password: heybuddy) |

## The heartbeat in more detail

While Buddy is idle and connected, the small light gives a 60 ms blip every 3 seconds and the middle light of
the strip a slightly longer one (the strip fades in and out). It comes from the main program loop, never from a
timer, so it **stops if the program stops**. It also only blinks if the microphone and speaker tasks have both
checked in within the last second.

- **Green single blip:** everything is within its limits.
- **Amber double blip:** at least one limit is close. These are the same limits the server warns about (see
  `firmware/LIMITS.md`):
  - internal RAM under 40 KB free, or it dipped under 16 KB
  - PSRAM missing
  - a CPU core over 85% busy
  - a 20 ms mic block taking over 15 ms
  - the main loop stalling over 250 ms
  - a task with under 1 KB of stack spare
- **No blip:** the microphone or speaker task stopped checking in. Both are on the hardware watchdog, so if
  either stays stuck for 5 seconds the board restarts and you see the dim white start-up light.

Switch it off or on:
- On the web page: Settings, Light, "Heartbeat".
- By voice: ask Buddy to turn the heartbeat light off.
- From the terminal: `curl -s -X POST localhost:8000/api/settings -H 'content-type: application/json' -d '{"heartbeat_light": false}'`.

## Asking Buddy's Claude about a light

In a Claude Code session in this repository, describe what you see ("the light is blinking amber twice"). Claude
reads this page, and can check `/api/board_health` and the board log for the reason.
