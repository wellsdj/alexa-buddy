"""Voice recognition: knows who is speaking.

A free speaker model (NVIDIA TitaNet small, English, ~40 MB) runs on this computer through
sherpa-onnx, the same library as Buddy's ears. It turns a few seconds of speech into a
"voiceprint": a list of numbers that comes out alike for the same person. Nothing is trained: you give it
some of your speech (a recording, or a few sentences said to the desk unit) and Buddy keeps the
voiceprints in data/voiceprints.json, which never leaves this computer.

Each time someone talks to Buddy, the sentence's voiceprint is compared with everyone saved.
The closest match above the threshold (setting voice_id_threshold) is who spoke.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np

from .config import MODELS_DIR

log = logging.getLogger("buddy.voiceid")

SR = 16000
# Picked by testing: CAM++ and ResNet34 couldn't tell a laptop recording of the user from strangers;
# TitaNet kept them far apart (user >= 0.48, strangers <= 0.16 on 1.5 s clips).
MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             + MODEL_NAME)
MODEL_PATH = MODELS_DIR / "voiceid" / MODEL_NAME
MODEL_MB = 40
PIECE_S = 4.0  # long recordings are cut into pieces this long, one voiceprint each
MIN_PIECE_S = 1.5  # shorter than this says too little about a voice
MAX_SAMPLES = 120  # per person; the oldest go first
MIN_ID_S = 0.8  # shorter sentences aren't checked ("off", "stop")


class VoiceIDError(Exception):
    pass


def _loud_frames(pcm: np.ndarray) -> np.ndarray:
    """One True/False per 20 ms: is someone talking there (rather than a pause or room noise)?"""
    frame = SR // 50
    n = len(pcm) // frame
    if n == 0:
        return np.zeros(0, dtype=bool)
    rms = np.sqrt(np.mean(pcm[: n * frame].astype(np.float64).reshape(n, frame) ** 2, axis=1))
    return rms > max(60.0, float(np.percentile(rms, 95)) * 0.12)


def has_speech(pcm: np.ndarray, share: float = 0.4) -> bool:
    loud = _loud_frames(pcm)
    return bool(len(loud)) and float(loud.mean()) >= share


def pieces(pcm: np.ndarray, piece_s: float = PIECE_S) -> list[np.ndarray]:
    """Cut speech into whole pieces (never stitched together: the joins confuse the model),
    keeping only pieces that are mostly talking."""
    size = int(piece_s * SR)
    out = [pcm[i:i + size] for i in range(0, len(pcm), size)]
    return [p for p in out if len(p) >= MIN_PIECE_S * SR and has_speech(p)]


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class VoiceID:
    def __init__(self, data_dir: Path, model_path: Path | None = None):
        self.path = Path(data_dir) / "voiceprints.json"
        self.model_path = Path(model_path or MODEL_PATH)
        self._lock = threading.Lock()
        self._extractor = None
        self.installing = False
        self.last_error = ""
        self.people: dict[str, dict] = {}
        self._centroids: dict[str, np.ndarray] = {}
        self._load()

    # ---------- storage ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("voiceprints.json unreadable; starting empty")
            return
        self.people = {name: p for name, p in data.get("people", {}).items() if p.get("samples")}
        self._refresh()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"people": self.people}))
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _refresh(self) -> None:
        self._centroids = {name: _unit(np.mean([_unit(s) for s in p["samples"]], axis=0))
                           for name, p in self.people.items() if p.get("samples")}

    # ---------- the model ----------
    def installed(self) -> bool:
        if not self.model_path.exists():
            return False
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return False
        return True

    def install(self) -> None:
        """Download the speaker model (blocking; run it in a thread)."""
        if self.installed():
            return
        import httpx
        self.installing, self.last_error = True, ""
        tmp = self.model_path.with_suffix(".part")
        try:
            self.model_path.parent.mkdir(parents=True, exist_ok=True)
            with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                              headers={"User-Agent": "buddy-setup"}) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            if tmp.stat().st_size < 5_000_000:
                raise VoiceIDError("the download looked incomplete")
            os.replace(tmp, self.model_path)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"Couldn't download the voice model: {e}"
            raise VoiceIDError(self.last_error) from e
        finally:
            tmp.unlink(missing_ok=True)
            self.installing = False

    def _get_extractor(self):
        if self._extractor is None:
            if not self.installed():
                raise VoiceIDError("The voice model isn't downloaded yet.")
            import sherpa_onnx
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self.model_path), num_threads=2)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            log.info("voice model loaded")
        return self._extractor

    def warm(self) -> None:
        """Load the model now, so the first sentence isn't slower."""
        if self.people and self.installed():
            with self._lock:
                self._get_extractor()

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        """int16 16 kHz speech -> unit-length voiceprint."""
        with self._lock:
            ext = self._get_extractor()
            stream = ext.create_stream()
            stream.accept_waveform(SR, pcm.astype(np.float32) / 32768.0)
            stream.input_finished()
            return _unit(ext.compute(stream))

    # ---------- people ----------
    def add_audio(self, name: str, pcm: np.ndarray, source: str = "recording") -> dict:
        """Add someone's speech (any length) to their voiceprint. Returns what was added."""
        name = " ".join(str(name).split())[:40]
        if not name:
            raise VoiceIDError("Type a name first.")
        parts = pieces(np.asarray(pcm, dtype=np.int16))
        if not parts:
            raise VoiceIDError("Not enough speech in that. Talk for at least 2 seconds.")
        prints = [self.embed(p) for p in parts]
        before = self._centroids.get(name)
        with self._lock:
            p = self.people.setdefault(name, {"samples": [], "seconds": 0.0, "added": time.time()})
            p["samples"] = (p["samples"] + [[round(float(x), 5) for x in v] for v in prints])[-MAX_SAMPLES:]
            p["seconds"] = round(p.get("seconds", 0.0) + sum(len(x) for x in parts) / SR, 1)
            p.setdefault("sources", [])
            if source not in p["sources"]:
                p["sources"].append(source)
            self._refresh()
            self._save()
        # How alike the new pieces are to the voice as a whole: low means mixed voices or a bad mic.
        centre = self._centroids[name]
        match = float(np.mean([float(v @ centre) for v in prints]))
        changed = float(_unit(np.mean(prints, axis=0)) @ before) if before is not None else None
        return {"name": name, "pieces": len(prints), "seconds": round(sum(len(x) for x in parts) / SR, 1),
                "total_samples": len(p["samples"]), "total_seconds": p["seconds"],
                "match": round(match, 2), "like_before": round(changed, 2) if changed is not None else None}

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self.people:
                return False
            del self.people[name]
            self._refresh()
            self._save()
        return True

    def identify(self, pcm: np.ndarray, threshold: float = 0.35) -> dict:
        """Who is speaking? {"name": "" if unknown, "score": best match 0..1, "best": closest name}."""
        if not self._centroids or len(pcm) < MIN_ID_S * SR or not self.installed():
            return {"name": "", "score": 0.0, "best": ""}
        voice = pcm[-int(10 * SR):]  # the last 10 s is plenty
        if not has_speech(voice, 0.25):
            return {"name": "", "score": 0.0, "best": ""}
        v = self.embed(voice)
        scores = {name: float(v @ c) for name, c in self._centroids.items()}
        best = max(scores, key=scores.get)
        score = round(scores[best], 2)
        return {"name": best if score >= threshold else "", "score": score, "best": best}

    def status(self) -> dict:
        return {
            "installed": self.installed(),
            "installing": self.installing,
            "model_mb": MODEL_MB,
            "error": self.last_error,
            "people": [{"name": n, "samples": len(p["samples"]), "seconds": p.get("seconds", 0.0),
                        "sources": p.get("sources", [])} for n, p in sorted(self.people.items())],
        }
