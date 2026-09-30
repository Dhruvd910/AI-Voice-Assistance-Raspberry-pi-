"""The 3D view: shows the engine's off-screen frames and turns touch into tools.

Drag to rotate, pinch or scroll to zoom, double-tap to reset. Every gesture is
a tool call through the ToolRegistry, exactly like a spoken command, so the
touch screen and the voice can never disagree about what the scene is.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QPinchGesture, QWidget


class Viewport(QWidget):
    resized = Signal(int, int)
    gesture = Signal(str, dict)          # tool name, arguments

    DEG_PER_PIXEL = 0.35

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.grabGesture(Qt.PinchGesture)
        self._image: QImage | None = None
        self._last: QPointF | None = None
        self._pending = [0.0, 0.0]          # accumulated drag, sent at most every 40 ms
        self._flush = QTimer(self, interval=40, timeout=self._send_drag)
        self._flush.start()

    # ------------------------------------------------------------ frames
    def set_frame(self, frame: np.ndarray) -> None:
        h, w, _ = frame.shape
        data = np.ascontiguousarray(frame)
        self._image = QImage(data.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if self._image is not None:
            img = self._image
            if img.width() != self.width() or img.height() != self.height():
                img = img.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawImage((self.width() - img.width()) // 2, (self.height() - img.height()) // 2, img)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.resized.emit(self.width(), self.height())

    # ------------------------------------------------------------ input
    def mousePressEvent(self, event) -> None:
        self._last = event.position()

    def mouseMoveEvent(self, event) -> None:
        if self._last is None:
            return
        pos = event.position()
        self._pending[0] += (pos.x() - self._last.x()) * self.DEG_PER_PIXEL
        self._pending[1] += (pos.y() - self._last.y()) * self.DEG_PER_PIXEL
        self._last = pos

    def mouseReleaseEvent(self, _event) -> None:
        self._send_drag()
        self._last = None

    def _send_drag(self) -> None:
        dx, dy = self._pending
        if abs(dx) + abs(dy) >= 0.5:
            self._pending = [0.0, 0.0]
            # Dragging right turns the model right; dragging down tips it towards you.
            self.gesture.emit("rotate", {"y": round(-dx, 2), "x": round(-dy, 2)})

    def mouseDoubleClickEvent(self, _event) -> None:
        self.gesture.emit("reset_camera", {})

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.gesture.emit("zoom", {"amount": round(1.12 ** steps, 3)})

    def event(self, event) -> bool:
        if event.type() == QEvent.Gesture:
            pinch = event.gesture(Qt.PinchGesture)
            if isinstance(pinch, QPinchGesture):
                factor = pinch.scaleFactor()
                if factor and abs(factor - 1.0) > 0.01:
                    self.gesture.emit("zoom", {"amount": round(max(0.5, min(2.0, factor)), 3)})
                return True
        return super().event(event)
