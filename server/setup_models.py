"""One-off setup: downloads Buddy's ears (speech-to-text that runs on this computer) and a
free British offline voice (Piper) for when ElevenLabs is unavailable or out of credit.
The wake-word and speech-detection models already ship in models/.

    python setup_models.py
"""
import fcntl
import io
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx  # uses its own certificates, so it also works with python.org builds on macOS

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "data" / "voices"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/"
FALLBACK = "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-en-gb-alan-low.tar.gz"
# Buddy's ears. Parakeet (NVIDIA, English): very accurate, about 480 MB. Moonshine: the older,
# smaller backup (kept if already downloaded).
EARS_MODELS = {
    "parakeet": (ROOT / "models" / "ears" / "parakeet-tdt-0.6b-v2",
                 ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"),
                 "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                 "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2", 480),
    "moonshine": (ROOT / "models" / "ears" / "moonshine-base-en",
                  ("preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx",
                   "tokens.txt"),
                  "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
                  "sherpa-onnx-moonshine-base-en-int8.tar.bz2", 240),
}


def fetch(url: str) -> bytes:
    r = httpx.get(url, follow_redirects=True, timeout=180, headers={"User-Agent": "buddy-setup"})
    r.raise_for_status()
    return r.content


def install_ears(which: str = "parakeet") -> bool:
    """Download one of Buddy's ears models. Streams to a temp file, then keeps only what's needed."""
    EARS, EARS_FILES, EARS_URL, size_mb = EARS_MODELS[which]
    if all((EARS / f).exists() for f in EARS_FILES):
        print("Ears already installed.")
        return True
    EARS.mkdir(parents=True, exist_ok=True)
    # Only one download at a time, even if several Buddy buttons were double-clicked together.
    lock = open(EARS / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another Buddy window is already downloading the ears; waiting for it to finish...")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if all((EARS / f).exists() for f in EARS_FILES):
            print("Ears installed.")
            return True
    print(f"Downloading Buddy's new ears: speech-to-text that runs on this computer (about {size_mb} MB, one-off)...")
    try:
        with tempfile.TemporaryFile() as tmp:
            with httpx.stream("GET", EARS_URL, follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                              headers={"User-Agent": "buddy-setup"}) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                got = 0
                for chunk in r.iter_bytes(1 << 20):
                    tmp.write(chunk)
                    got += len(chunk)
                    shown = f"{got * 100 // total}% " if total else ""
                    print(f"\r  {shown}({got >> 20} of {total >> 20 or size_mb} MB)   ", end="", flush=True)
                print()
            print("  Unpacking...")
            tmp.seek(0)
            with tarfile.open(fileobj=tmp, mode="r:bz2") as tar:
                for m in tar.getmembers():
                    name = Path(m.name).name
                    if m.isfile() and name in EARS_FILES:
                        part = EARS / (name + ".part")
                        part.write_bytes(tar.extractfile(m).read())
                        part.replace(EARS / name)
    except Exception as e:  # noqa: BLE001
        print(f"  Couldn't download the ears ({e}). Buddy uses its old ears until then; "
              "double-click Update Buddy again later.")
        return False
    ok = all((EARS / f).exists() for f in EARS_FILES)
    print("Ears installed." if ok else "  The download looked incomplete; run Update Buddy again.")
    return ok


def main() -> int:
    for name in ("hey-buddy.onnx", "melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx"):
        if not (ROOT / "models" / name).exists():
            print(f"Missing models/{name}. Re-download the project folder.")
            return 1
    if not install_ears("parakeet"):
        install_ears("moonshine")  # at least the small backup, so Buddy can hear
    VOICES.mkdir(parents=True, exist_ok=True)
    if list(VOICES.glob("*.onnx")):
        print("Offline voice already installed:", ", ".join(p.name for p in VOICES.glob("*.onnx")))
        return 0
    print("Downloading the offline British voice (about 60 MB)...")
    try:
        for f in ("en_GB-alan-medium.onnx.json", "en_GB-alan-medium.onnx"):
            (VOICES / f).write_bytes(fetch(HF + f))
    except Exception as e:  # noqa: BLE001
        print(f"  Hugging Face didn't work ({e}); trying GitHub...")
        for f in VOICES.glob("en_GB-alan-medium*"):
            f.unlink()
        try:
            with tarfile.open(fileobj=io.BytesIO(fetch(FALLBACK)), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if m.name.endswith((".onnx", ".onnx.json")):
                        (VOICES / Path(m.name).name).write_bytes(tar.extractfile(m).read())
        except Exception as e2:  # noqa: BLE001
            print(f"  Couldn't download a voice ({e2}). Buddy still works with ElevenLabs "
                  "or your browser's voice; run this again later for the offline voice.")
            return 0
    print("Done:", ", ".join(p.name for p in VOICES.glob("*.onnx")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
