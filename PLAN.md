# Buddy: the plan

What we're doing next, in order. Tick things off as they're done.

## 1. Claude Haiku as Buddy's brain (next up)

Swap Groq's free models for Claude Haiku, using an Anthropic API key.

**What you do**
- [ ] Get an API key at [console.anthropic.com](https://console.anthropic.com) and add some credit.
  This is separate from a Claude Pro/Max subscription, which can't be used by apps like Buddy.
- [ ] Paste the key into Buddy's web page when the new key box is there.

**What gets built**
- [ ] A **Claude key** box on the web page, which says straight away whether the key works.
- [ ] A **Brain** choice in Settings: Groq (free) or Claude.
- [ ] Claude Haiku for everyday questions; a smarter Claude model for "think harder" and big jobs.
- [ ] Groq stays as the ears (speech to text) and ElevenLabs stays as the voice.
- [ ] Memory, alarms, reminders and weather keep working exactly as now.
- [ ] Safety net: if the Claude key fails or runs out of credit, Buddy switches back to Groq by itself.
- [ ] README updated with the new steps.

## Already agreed for later

- **Voice check:** Buddy learns your voice (WeSpeaker) so it knows when it's you talking.
  The voice recording never goes on GitHub.
- **Hosting:** run Buddy's brain on Oracle Always Free (London) so the laptop doesn't need to be on.
  Use Vercel for the web page.
- **Wake word on the board:** the desk unit listens for "hey buddy" by itself.

## Hardware notes

- The amp runs from **3V3** for now, because the board's 5V pin is dead on the USB socket.
  For full volume later: bridge the board's **IN-OUT** solder pads, then move the amp's Vin back to **5V**.
- Optional: change the hello tune and the wake chime.
