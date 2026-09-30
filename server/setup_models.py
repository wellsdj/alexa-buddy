"""One-off setup: downloads a free British offline voice (Piper) for when ElevenLabs is
unavailable or out of credit. The wake-word and speech-detection models already ship in models/.

    python setup_models.py
"""
import io
import sys
import tarfile
from pathlib import Path

import httpx  # uses its own certificates, so it also works with python.org builds on macOS

ROOT = Path(__file__).resolve().parent
VOICES = ROOT / "data" / "voices"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/"
FALLBACK = "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-en-gb-alan-low.tar.gz"


def fetch(url: str) -> bytes:
    r = httpx.get(url, follow_redirects=True, timeout=180, headers={"User-Agent": "buddy-setup"})
    r.raise_for_status()
    return r.content


def main() -> int:
    for name in ("hey-buddy.onnx", "melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx"):
        if not (ROOT / "models" / name).exists():
            print(f"Missing models/{name}. Re-download the project folder.")
            return 1
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
