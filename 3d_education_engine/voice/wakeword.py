"""Hands-free start: listen for a wake word, then record a command.

Uses OpenWakeWord (pip install openwakeword) with one of its pretrained
models ("hey_jarvis" by default). Without the package the listener reports
itself unavailable and the app uses its microphone button instead -- nothing
else changes.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)
SAMPLE_RATE = 16000
FRAME = 1280        # 80 ms, what OpenWakeWord expects


class WakeWordListener(threading.Thread):
    def __init__(self, model_name: str, on_wake: Callable[[], None], threshold: float = 0.5, device=None):
        super().__init__(daemon=True, name="wakeword")
        self.model_name, self.on_wake, self.threshold, self.device = model_name, on_wake, threshold, device
        self._stop = threading.Event()
        self.paused = threading.Event()        # set while a command is being recorded
        self.available, self.reason = self._load()

    def _load(self) -> tuple[bool, str]:
        try:
            import openwakeword
            from openwakeword.model import Model
        except ImportError:
            return False, "openwakeword is not installed"
        try:
            openwakeword.utils.download_models([self.model_name])
            self.model = Model(wakeword_models=[self.model_name], inference_framework="onnx")
            return True, ""
        except Exception as exc:                  # model download or runtime missing
            return False, f"wake word model unavailable: {exc}"

    def run(self) -> None:
        if not self.available:
            log.info("Wake word disabled: %s", self.reason)
            return
        import sounddevice as sd
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME, device=self.device) as stream:
            while not self._stop.is_set():
                data, _ = stream.read(FRAME)
                if self.paused.is_set():
                    continue
                scores = self.model.predict(np.asarray(data[:, 0], dtype=np.int16))
                if max(scores.values(), default=0.0) >= self.threshold:
                    self.model.reset()
                    self.paused.set()
                    self.on_wake()

    def stop(self) -> None:
        self._stop.set()
