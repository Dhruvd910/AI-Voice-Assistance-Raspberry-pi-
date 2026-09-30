"""One line at the bottom: where you are, what kind of model it is, whose it is.

The attribution is always visible for third-party assets -- CC BY requires it,
and it is the student's first lesson in citing sources.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """A label that shortens its text with "…" instead of widening the window.

    On an 800-pixel screen a long attribution line would otherwise push the
    whole layout off the edge; the full text stays in the tooltip."""

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(10)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.ElideRight, self.width())
        painter.drawText(self.rect(), int(self.alignment() | Qt.AlignVCenter), text)


class StatusBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = ElidedLabel("")
        self.path.setObjectName("statusPath")
        self.disclaimer = ElidedLabel("")
        self.disclaimer.setObjectName("statusDisclaimer")
        self.attribution = ElidedLabel("")
        self.attribution.setObjectName("statusAttribution")
        self.attribution.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mode = QLabel("")
        self.mode.setObjectName("statusMode")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(10)
        layout.addWidget(self.path, 3)
        layout.addWidget(self.disclaimer, 3)
        layout.addWidget(self.attribution, 3)
        layout.addWidget(self.mode, 0)

    def show_scene(self, path: str, disclaimer: str | None, attribution: str | None) -> None:
        self.path.setText(path)
        self.disclaimer.setText(disclaimer or "")
        self.disclaimer.setToolTip(disclaimer or "")
        self.attribution.setText(attribution or "")
        self.attribution.setToolTip(attribution or "")

    def set_mode(self, text: str) -> None:
        self.mode.setText(text)
