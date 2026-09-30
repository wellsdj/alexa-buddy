"""Generate spoken 16 kHz test clips with Piper for test_voice_e2e.py and wake tuning.

    pip install piper-tts
    python tests/make_clips.py /path/to/voices /path/to/clips

Voices: any Piper .onnx voices, e.g. from
https://github.com/rhasspy/piper/releases/tag/v0.0.2 (voice-en-us-lessac-medium.tar.gz,
voice-en-gb-alan-low.tar.gz). Clip names follow  <phrase>__<voice>__<speed>.wav
"""
import sys
import wave
from pathlib import Path

import numpy as np
from piper import PiperVoice
from piper.config import SynthesisConfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from buddy.audio import resample  # noqa: E402

PHRASES = {
    "pos_hey_buddy": "Hey buddy.",
    "pos_hey_buddy_q": "Hey buddy, what's the weather like?",
    "pos_hey_buddy_alarm": "Hey buddy, set an alarm for seven.",
    "neg_hello": "Hello there, how are you doing today?",
    "neg_hey_bobby": "Hey Bobby, pass me the salt.",
    "neg_buddy_holly": "I was listening to Buddy Holly yesterday.",
    "neg_weather": "The weather in Richmond is quite nice this afternoon.",
    "neg_hey_body": "Hey, nobody told me about the meeting.",
}


def main(voices: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for model in sorted(voices.glob("*.onnx")):
        voice = PiperVoice.load(str(model))
        for key, text in PHRASES.items():
            for speed in (0.9, 1.15):
                chunks = voice.synthesize(text, syn_config=SynthesisConfig(length_scale=speed))
                pcm = resample(np.concatenate([c.audio_int16_array for c in chunks]), voice.config.sample_rate)
                with wave.open(str(out / f"{key}__{model.stem}__{speed}.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes(pcm.tobytes())
    print("clips written to", out)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
