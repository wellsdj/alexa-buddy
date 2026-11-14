"""Buddy looking at, and changing, its own settings (the get_settings / change_setting tools).

Only the settings below can be seen or changed this way, each with a safe range. API keys, the
access token, Spotify and location aren't here, so Buddy can never read them out or break them.
"""
from __future__ import annotations

from typing import Any

# name: (kind, range or choices, what it means in plain words)
SETTABLE: dict[str, tuple[str, Any, str]] = {
    "volume": ("int", (0, 10), "speaker volume"),
    "voice_speed": ("float", (0.7, 1.2), "how fast you talk (1.0 is normal)"),
    "end_of_speech_ms": ("int", (300, 2000), "how long a pause means they've finished talking, in ms (longer: "
                                             "fewer cut-offs but slower answers)"),
    "listen_sensitivity": ("float", (0.15, 0.5), "after 'hey buddy', how easily a quiet voice counts as talking "
                                                 "(lower picks up quieter voices, but more noise too)"),
    "device_mic_shift": ("int", (10, 18), "desk unit mic loudness (lower is louder; each step doubles it)"),
    "wake_threshold": ("float", (0.2, 0.9), "how sure you must be you heard 'hey buddy' (lower wakes more easily, "
                                            "but more by mistake)"),
    "max_listen_s": ("int", (5, 60), "longest you'll listen to one sentence, in seconds"),
    "follow_up": ("choice", ("off", "questions", "always"), "keep listening after you speak: never, only after a "
                                                           "question, or always"),
    "sound_effects": ("bool", None, "switch-on, 'hey buddy' and 'didn't hear anything' sounds"),
    "greet_by_voice": ("bool", None, "say hello by name, and ask new voices who they are"),
    "barge_in": ("bool", None, "people you know can talk over you to stop you"),
    "fact_check": ("bool", None, "double-check factual answers"),
    "reword_replies": ("bool", None, "say quick replies (timers, weather) in your own words"),
    "stream_replies": ("bool", None, "start talking while your answer is still being written (faster)"),
    "early_transcribe": ("bool", None, "start writing down their words at the first pause (faster)"),
    "warm_connections": ("bool", None, "connect to Claude and the voice as soon as 'hey buddy' is heard (faster)"),
    "fast_haiku": ("bool", None, "quick everyday answers (no thinking first, short instructions)"),
    "play_hold_ms": ("int", (100, 1000), "sound collected before you start talking, in ms (raise if you stutter)"),
    "strip_brightness": ("int", (0, 255), "light strip brightness"),
    "strip_leds": ("int", (1, 60), "how many lights are on the strip"),
    "room_listening_on": ("bool", None, "also answer room talk that's clearly meant for you, without 'hey buddy'"),
    "morning_briefing": ("bool", None, "weather and the day's reminders after a morning alarm"),
    "snooze_minutes": ("int", (1, 30), "snooze length in minutes"),
    "default_alarm_sound": ("int", (0, 20), "alarm sound number used when none is chosen (0 = beeps)"),
    "temperature_unit": ("choice", ("celsius", "fahrenheit"), "temperature unit"),
    "tts_engine": ("choice", ("auto", "elevenlabs", "piper"), "your voice service (auto: ElevenLabs, offline "
                                                             "voice if that fails)"),
    "user_name": ("text", 40, "the main user's name"),
    "voice_id_on": ("bool", None, "notice who is talking by their voice"),
    "voice_learn_on": ("bool", None, "keep learning voices from everyday talk"),
    "voice_id_threshold": ("float", (0.2, 0.7), "how alike a voice must be to count as someone you know"),
    "voice_trust_seconds": ("int", (20, 300), "seconds of a new voice you need before naming them"),
    "timing_log": ("bool", None, "time every answer (the Speed tab)"),
}

_TRUE = {"true", "yes", "on", "1", "enable", "enabled"}
_FALSE = {"false", "no", "off", "0", "disable", "disabled"}


def describe(settings) -> str:
    lines = []
    for name, (kind, rng, what) in SETTABLE.items():
        v = settings[name]
        allowed = (f"{rng[0]}-{rng[1]}" if kind in ("int", "float") else "/".join(rng) if kind == "choice"
                   else "on/off" if kind == "bool" else "text")
        shown = ("on" if v else "off") if kind == "bool" else v
        lines.append(f"- {name} = {shown} ({allowed}): {what}")
    return "\n".join(lines)


def change(settings, name: str, value) -> tuple[bool, str]:
    """Check and apply one change. Returns (changed, a note for the model)."""
    name = str(name or "").strip()
    if name not in SETTABLE:
        return False, f"'{name}' isn't a setting you can change. Ones you can: {', '.join(SETTABLE)}."
    kind, rng, what = SETTABLE[name]
    old = settings[name]
    try:
        if kind == "bool":
            t = str(value).strip().lower()
            if t not in _TRUE | _FALSE:
                raise ValueError
            new: Any = t in _TRUE
        elif kind in ("int", "float"):
            num = float(value)
            num = min(max(num, rng[0]), rng[1])  # kept inside the safe range
            new = int(round(num)) if kind == "int" else round(num, 2)
        elif kind == "choice":
            new = str(value).strip().lower()
            if new not in rng:
                raise ValueError
        else:
            new = " ".join(str(value).split())[:rng]
            if not new:
                raise ValueError
    except (TypeError, ValueError):
        return False, f"'{value}' doesn't work for {name} ({what})."
    if new == old:
        return False, f"{name} is already {old}."
    settings.update({name: new})
    clamped = kind in ("int", "float") and float(value) != float(new)
    return True, (f"Changed {name} ({what}) from {old} to {new}"
                  + (f" ({value} is outside the safe range)" if clamped else "") + ".")
