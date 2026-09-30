"""The touch-screen app (PySide6), laid out for an 800x480 display.

    ┌──────────────────────────────┬───────────────┐
    │                              │ conversation  │
    │        3D viewport           │               │
    │                              ├───────────────┤
    │                              │ parts         │
    ├──────────────────────────────┴───────────────┤
    │ ⟲ ⟳ ＋ － Reset See Cut Apart Labels ◀ ▼ ▶ Exit │
    │ [ ask or command…                  ] 🎤  Send │
    │ Human Heart › …   disclaimer      attribution │
    └──────────────────────────────────────────────┘

Threads: the engine (VTK) lives on the UI thread. The agent runs on a worker
thread so an LLM call never freezes the screen; its tool calls come back to
the UI thread through MainThreadExecutor.
"""

from __future__ import annotations

import html
import logging
import queue
import threading
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLineEdit, QMainWindow, QPushButton, QSplitter,
                               QTextBrowser, QVBoxLayout, QWidget)

from app.config import Settings
from agent.tool_registry import ToolCall
from ui.controls import Controls, PartsList
from ui.status_bar import StatusBar
from ui.viewport import Viewport

log = logging.getLogger(__name__)

STYLE = """
QWidget { background: #101418; color: #e6e9ec; font-size: 13px; }
QPushButton { background: #1f2a33; border: 1px solid #2f3d49; border-radius: 6px; padding: 4px 6px; font-size: 13px; }
QPushButton:pressed { background: #35506a; }
QPushButton:disabled { color: #5a6570; }
QLineEdit { background: #182028; border: 1px solid #2f3d49; border-radius: 6px; padding: 6px; font-size: 14px; }
QTextBrowser, QListWidget { background: #141b21; border: 1px solid #25313b; border-radius: 6px; }
QListWidget::item { padding: 3px; }
QListWidget::item:selected { background: #35506a; }
#statusPath { color: #9fc6e8; font-size: 11px; }
#statusDisclaimer { color: #ffc44d; font-size: 11px; }
#statusAttribution { color: #8a96a0; font-size: 10px; }
#statusMode { color: #6fd08c; font-size: 11px; }
"""

HELLO = ("Hi! Try: <i>show me the heart</i>, <i>rotate it</i>, <i>show the four chambers</i>, "
         "<i>cut it in half</i>, <i>go one level deeper</i>, <i>create a water molecule</i>, "
         "<i>show projectile motion</i>.")


class MainThreadExecutor(QObject):
    """Runs a callable on the UI thread and hands back its result (or exception)."""

    _run = Signal(object)

    def __init__(self):
        super().__init__()
        self._run.connect(self._execute, Qt.BlockingQueuedConnection)

    def __call__(self, fn):
        if QThread.currentThread() is self.thread():
            return fn()
        box: dict = {}
        self._run.emit((fn, box))
        if "error" in box:
            raise box["error"]
        return box.get("result")

    @Slot(object)
    def _execute(self, payload) -> None:
        fn, box = payload
        try:
            box["result"] = fn()
        except BaseException as exc:          # re-raised on the calling thread
            box["error"] = exc


class AgentWorker(QThread):
    replied = Signal(str, str, bool)          # request, reply, used_llm
    busy = Signal(bool)

    def __init__(self, agent, tts=None):
        super().__init__()
        self.agent, self.tts = agent, tts
        self.requests: queue.Queue[str | None] = queue.Queue()

    def submit(self, text: str) -> None:
        self.requests.put(text)

    def run(self) -> None:
        while True:
            text = self.requests.get()
            if text is None:
                return
            self.busy.emit(True)
            try:
                reply = self.agent.handle(text)
                self.replied.emit(text, reply.text, reply.used_llm)
                if self.tts is not None and reply.text:
                    from voice.tts import language_of
                    threading.Thread(target=self._speak, args=(reply.text, language_of(reply.text)), daemon=True).start()
            except Exception as exc:
                log.exception("agent failed")
                self.replied.emit(text, f"Something went wrong: {exc}", False)
            finally:
                self.busy.emit(False)

    def _speak(self, text: str, language: str) -> None:
        try:
            self.tts.speak(text, language)
        except Exception as exc:
            log.warning("TTS failed: %s", exc)


class VoiceWorker(QThread):
    heard = Signal(str)
    status = Signal(str)

    def __init__(self, stt, settings: Settings):
        super().__init__()
        self.stt, self.settings = stt, settings

    def run(self) -> None:
        from voice.stt import record_until_silence
        try:
            self.status.emit("Listening…")
            wav = record_until_silence(self.settings.voice.record_seconds_max)
            if not wav:
                self.status.emit("I didn't hear anything.")
                return
            self.status.emit("Transcribing…")
            text = self.stt.transcribe(wav)
            self.status.emit("")
            if text:
                self.heard.emit(text)
        except Exception as exc:
            log.warning("voice input failed: %s", exc)
            self.status.emit(f"Microphone problem: {exc}")


class MainWindow(QMainWindow):
    wake = Signal()

    def __init__(self, services, stt=None, tts=None):
        super().__init__()
        self.sv = services
        self.stt, self.tts = stt, tts
        self.setWindowTitle("3D Education Engine")
        self.resize(800, 480)

        self.viewport = Viewport()
        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(False)
        self.parts = PartsList()
        side = QSplitter(Qt.Vertical)
        side.addWidget(self.chat)
        side.addWidget(self.parts)
        side.setSizes([200, 130])
        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.viewport)
        top.addWidget(side)
        top.setSizes([550, 250])
        top.setStretchFactor(0, 1)

        self.controls = Controls()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask or command… e.g. 'show the left ventricle'")
        self.mic = QPushButton("🎤")
        self.mic.setMinimumSize(48, 38)
        self.send = QPushButton("Send")
        self.send.setMinimumSize(64, 38)
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.addWidget(self.input, 1)
        row.addWidget(self.mic)
        row.addWidget(self.send)
        self.status = StatusBar()

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 2)
        layout.setSpacing(3)
        layout.addWidget(top, 1)
        layout.addWidget(self.controls)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.setStyleSheet(STYLE)

        # ---- threads and wiring
        self.executor = MainThreadExecutor()
        self.sv.on_main = self.executor
        self.sv.tools.executor = self.executor
        self.worker = AgentWorker(self.sv.agent, tts)
        self.worker.replied.connect(self._on_reply)
        self.worker.busy.connect(lambda b: self.status.set_mode("thinking…" if b else self._mode_text()))
        self.worker.start()
        self.voice_worker: VoiceWorker | None = None

        self.send.clicked.connect(self._submit)
        self.input.returnPressed.connect(self._submit)
        self.mic.clicked.connect(self._listen)
        self.mic.setEnabled(stt is not None)
        self.mic.setToolTip("Speak a command" if stt else "Voice input is off: set STT_PROVIDER in .env")
        self.viewport.gesture.connect(self._run_tool)
        self.viewport.resized.connect(self._on_resize)
        self.controls.tool.connect(self._run_tool)
        self.controls.exit_requested.connect(self.close)
        self.parts.tool.connect(self._run_tool)
        self.wake.connect(self._listen)
        self.sv.engine.listeners.append(self._on_engine_event)

        self._last_tick = time.monotonic()
        self.timer = QTimer(self, interval=33, timeout=self._tick)
        self.timer.start()
        self._say_system(HELLO)
        self.status.set_mode(self._mode_text())

    # ------------------------------------------------------------ rendering
    def _tick(self) -> None:
        now = time.monotonic()
        dt, self._last_tick = min(0.1, now - self._last_tick), now
        engine = self.sv.engine
        if engine.tick(dt) or engine.dirty:
            if engine.scene is not None:
                self.viewport.set_frame(engine.render())

    def _on_resize(self, w: int, h: int) -> None:
        self.sv.engine.resize(w, h)
        if self.sv.engine.scene is not None:
            self.sv.engine.annotations.set_overlays(self.sv.engine.scene.title, self.sv.engine.scene.disclaimer,
                                                    self.sv.engine.scene.attribution)

    def _on_engine_event(self, event: str, data: dict) -> None:
        engine = self.sv.engine
        if event in {"model_loaded", "parts_changed", "model_unloaded"}:
            summary = engine.summary()
            self.parts.fill(summary.get("parts", {}) if summary.get("model") else {})
        if event == "model_loaded" and engine.scene is not None:
            self.controls.reset_toggles()
            self.sv.scale.visited(engine.model_id)
            self.status.show_scene(self.sv.scale.path_label() or engine.scene.title,
                                   engine.scene.disclaimer, engine.scene.attribution)

    # ------------------------------------------------------------ actions
    @Slot(str, dict)
    def _run_tool(self, name: str, args: dict) -> None:
        result = self.sv.tools.execute(ToolCall(name, args))
        if not result.ok:
            self._say_system(html.escape(result.message))
        elif name in {"go_back", "go_deeper"}:
            self._say_system(html.escape(result.message))

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self._say_user(text)
        self.worker.submit(text)

    def _listen(self) -> None:
        if self.stt is None or (self.voice_worker and self.voice_worker.isRunning()):
            return
        if self.tts is not None:
            self.tts.stop()
        self.voice_worker = VoiceWorker(self.stt, self.sv.settings)
        self.voice_worker.status.connect(lambda s: self.status.set_mode(s or self._mode_text()))
        self.voice_worker.heard.connect(self._heard)
        self.voice_worker.start()

    def _heard(self, text: str) -> None:
        self._say_user(text)
        self.worker.submit(text)
        wake = getattr(self, "wakeword", None)
        if wake is not None:
            wake.paused.clear()

    def _on_reply(self, request: str, reply: str, used_llm: bool) -> None:
        self._say_system(html.escape(reply))

    # ------------------------------------------------------------ chat
    def _say_user(self, text: str) -> None:
        self.chat.append(f"<p style='color:#9fc6e8;margin:2px'><b>You:</b> {html.escape(text)}</p>")

    def _say_system(self, text: str) -> None:
        self.chat.append(f"<p style='margin:2px'>{text}</p>")

    def _mode_text(self) -> str:
        return "LLM" if self.sv.agent.llm_available else "offline"

    def closeEvent(self, event) -> None:
        self.worker.submit(None)
        self.worker.wait(2000)
        wake = getattr(self, "wakeword", None)
        if wake is not None:
            wake.stop()
        super().closeEvent(event)


def run_app(settings: Settings, rebuild_index: bool = False) -> int:
    app = QApplication.instance() or QApplication([])
    from app.dependency_container import build
    from voice.stt import make_stt
    from voice.tts import make_tts
    sv = build(settings, interactive=True, rebuild_index=True if rebuild_index else None)
    window = MainWindow(sv, make_stt(settings.voice), make_tts(settings.voice))
    if settings.voice.wakeword_enabled and window.stt is not None:
        from voice.wakeword import WakeWordListener
        window.wakeword = WakeWordListener(settings.voice.wakeword_model, window.wake.emit)
        window.wakeword.start()
    if settings.fullscreen:
        window.showFullScreen()
    else:
        # On an 800x480 panel the taskbar and title bar leave less than 480
        # pixels: fill what is actually available instead of running off the edge.
        avail = app.primaryScreen().availableGeometry()
        if avail.width() <= 1024 or avail.height() <= 600:
            window.showMaximized()
        else:
            window.show()
    QTimer.singleShot(200, lambda: window._run_tool("load_model", {"model_id": "biology.anatomy.heart"}))
    return app.exec()
