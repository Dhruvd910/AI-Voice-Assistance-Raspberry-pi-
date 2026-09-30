"""Speech to text, behind one interface, plus recording from the microphone.

Providers:
    openai_compatible   POST {base}/audio/transcriptions -- Groq, OpenAI, a local
                        whisper server. Fast on a Pi because the Pi does nothing.
    faster_whisper      on-device Whisper (pip install faster-whisper). Works
                        offline; "base" transcribes a short command in ~1-2 s on a Pi 5.
    none                voice input disabled; the text box still works.
"""

from __future__ import annotations

import io
import logging
import wave
from typing import Protocol

import numpy as np
import requests

from app.config import VoiceSettings

log = logging.getLogger(__name__)
SAMPLE_RATE = 16000


class STTError(RuntimeError):
    pass


class STTProvider(Protocol):
    name: str

    def transcribe(self, wav_bytes: bytes, language: str | None = None) -> str: ...


class OpenAICompatibleSTT:
    name = "openai_compatible"

    def __init__(self, settings: VoiceSettings):
        if not settings.stt_api_key:
            raise STTError("STT_API_KEY (or GROQ_API_KEY) is not set")
        self.s = settings

    def transcribe(self, wav_bytes: bytes, language: str | None = None) -> str:
        data = {"model": self.s.stt_model, "response_format": "json", "temperature": "0"}
        if language:
            data["language"] = language
        try:
            resp = requests.post(f"{self.s.stt_base_url}/audio/transcriptions",
                                 headers={"Authorization": f"Bearer {self.s.stt_api_key}"},
                                 files={"file": ("speech.wav", wav_bytes, "audio/wav")}, data=data, timeout=30)
        except requests.RequestException as exc:
            raise STTError(f"speech service unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise STTError(f"speech service returned {resp.status_code}: {resp.text[:200]}")
        return (resp.json().get("text") or "").strip()


class FasterWhisperSTT:
    name = "faster_whisper"

    def __init__(self, settings: VoiceSettings):
        try:
            from faster_whisper import WhisperModel      # optional dependency
        except ImportError as exc:
            raise STTError("faster-whisper is not installed (pip install -r requirements-voice.txt)") from exc
        self.model = WhisperModel(settings.faster_whisper_model, device="cpu", compute_type="int8")

    def transcribe(self, wav_bytes: bytes, language: str | None = None) -> str:
        audio = wav_to_float(wav_bytes)
        segments, _info = self.model.transcribe(audio, language=language, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


def make_stt(settings: VoiceSettings) -> STTProvider | None:
    try:
        if settings.stt_provider == "openai_compatible":
            return OpenAICompatibleSTT(settings)
        if settings.stt_provider == "faster_whisper":
            return FasterWhisperSTT(settings)
    except STTError as exc:
        log.warning("Voice input disabled: %s", exc)
    return None


# ------------------------------------------------------------------ audio helpers
def float_to_wav(audio: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def wav_to_float(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        frames = w.readframes(w.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            audio = audio.reshape(-1, w.getnchannels()).mean(axis=1)
    return audio


def record_until_silence(max_seconds: float = 8.0, silence_seconds: float = 1.0, device=None,
                         start_timeout: float = 5.0) -> bytes:
    """Record from the default (or given) microphone until the speaker stops.

    A simple energy detector: the level is calibrated from the first 0.3 s,
    speech is anything clearly above it, and recording ends after
    `silence_seconds` of quiet following speech.
    """
    import sounddevice as sd
    block = int(SAMPLE_RATE * 0.05)
    chunks: list[np.ndarray] = []
    noise, heard, quiet, elapsed = None, False, 0.0, 0.0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block, device=device) as stream:
        while elapsed < max_seconds:
            data, _ = stream.read(block)
            chunk = data[:, 0].copy()
            chunks.append(chunk)
            elapsed += len(chunk) / SAMPLE_RATE
            level = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)
            if elapsed <= 0.3:
                noise = level if noise is None else max(noise, level)
                continue
            if level > max(3.0 * (noise or 0.0), 0.01):
                heard, quiet = True, 0.0
            elif heard:
                quiet += len(chunk) / SAMPLE_RATE
                if quiet >= silence_seconds:
                    break
            elif elapsed > start_timeout:
                break
    if not heard:
        return b""
    return float_to_wav(np.concatenate(chunks))
