"""Text to speech, behind one interface.

    cartesia   Cartesia's REST API (POST /tts/bytes), WAV back, played with aplay
    piper      the Piper CLI with a local .onnx voice: fully offline
    none       silent; replies are still shown on screen
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Protocol

import requests

from app.config import VoiceSettings

log = logging.getLogger(__name__)
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_VERSION = "2025-11-04"


class TTSProvider(Protocol):
    name: str

    def speak(self, text: str, language: str = "en") -> None: ...

    def stop(self) -> None: ...


class _Player:
    def __init__(self, device: str):
        self.device = device
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def play_wav(self, wav: bytes) -> None:
        if not shutil.which("aplay"):
            log.warning("aplay not found; cannot play speech")
            return
        with self._lock:
            self.stop()
            cmd = ["aplay", "-q"] + (["-D", self.device] if self.device and self.device != "default" else []) + ["-"]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            self._proc.communicate(wav, timeout=120)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            self.stop()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()


class CartesiaTTS:
    name = "cartesia"

    def __init__(self, settings: VoiceSettings):
        if not (settings.cartesia_api_key and settings.cartesia_voice_id):
            raise RuntimeError("CARTESIA_API_KEY and CARTESIA_VOICE_ID are needed")
        self.s = settings
        self.player = _Player(settings.audio_output_device)

    def synthesize(self, text: str, language: str = "en") -> bytes:
        resp = requests.post(CARTESIA_URL, timeout=30, headers={
            "Authorization": f"Bearer {self.s.cartesia_api_key}", "cartesia-version": CARTESIA_VERSION},
            json={"model_id": self.s.cartesia_model, "transcript": text, "language": language,
                  "voice": {"mode": "id", "id": self.s.cartesia_voice_id},
                  "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": 22050}})
        if resp.status_code >= 400:
            raise RuntimeError(f"Cartesia returned {resp.status_code}: {resp.text[:200]}")
        return resp.content

    def speak(self, text: str, language: str = "en") -> None:
        self.player.play_wav(self.synthesize(text, language))

    def stop(self) -> None:
        self.player.stop()


class PiperTTS:
    name = "piper"

    def __init__(self, settings: VoiceSettings):
        if not shutil.which("piper") or not settings.piper_model:
            raise RuntimeError("the piper CLI and PIPER_MODEL (.onnx voice) are needed")
        self.s = settings
        self.player = _Player(settings.audio_output_device)

    def speak(self, text: str, language: str = "en") -> None:
        out = subprocess.run(["piper", "--model", self.s.piper_model, "--output_file", "-"],
                             input=text.encode("utf-8"), capture_output=True, timeout=60)
        if out.returncode == 0:
            self.player.play_wav(out.stdout)

    def stop(self) -> None:
        self.player.stop()


def make_tts(settings: VoiceSettings) -> TTSProvider | None:
    try:
        if settings.tts_provider == "cartesia":
            return CartesiaTTS(settings)
        if settings.tts_provider == "piper":
            return PiperTTS(settings)
    except RuntimeError as exc:
        log.warning("Speech output disabled: %s", exc)
    return None


def language_of(text: str) -> str:
    return "hi" if any("ऀ" <= ch <= "ॿ" for ch in text) else "en"
