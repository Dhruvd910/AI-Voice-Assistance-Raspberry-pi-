"""Big touch buttons for what students do most, and the parts list.

Each button is a tool call. The toggles (see-through, cut, apart, labels)
remember their state so one tap does and the next tap undoes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QWidget


class Controls(QWidget):
    tool = Signal(str, dict)
    exit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        self.toggles: dict[str, bool] = {}

        def button(text: str, tip: str, handler) -> QPushButton:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setMinimumHeight(38)
            b.setMinimumWidth(30)
            # Share the row equally, whatever the label length: 13 buttons
            # have to fit across an 800-pixel screen.
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(handler)
            layout.addWidget(b)
            return b

        button("⟲", "Rotate left", lambda: self.tool.emit("rotate", {"y": -30.0}))
        button("⟳", "Rotate right", lambda: self.tool.emit("rotate", {"y": 30.0}))
        button("＋", "Zoom in", lambda: self.tool.emit("zoom", {"amount": 1.25}))
        button("－", "Zoom out", lambda: self.tool.emit("zoom", {"amount": 0.8}))
        button("Reset", "Reset the view", lambda: self.tool.emit("reset_camera", {}))
        self.see = button("See", "Make the model see-through", lambda: self._toggle(
            "see", ("set_transparency", {"part_id": "all", "value": 0.3}), ("set_transparency", {"part_id": "all", "value": 1.0})))
        self.cut = button("Cut", "Cut the model in half", lambda: self._toggle(
            "cut", ("clip", {"plane": "y", "position": 0.5, "keep": "positive"}), ("clear_clip", {})))
        self.apart = button("Apart", "Pull the parts apart", lambda: self._toggle(
            "apart", ("explode", {"amount": 0.8}), ("explode", {"amount": 0.0})))
        self.labels = button("Labels", "Name the parts", lambda: self._toggle(
            "labels", ("label_parts", {"part_id": "all"}), ("remove_label", {"label_id": "all"})))
        button("◀", "Go back to the previous model", lambda: self.tool.emit("go_back", {}))
        button("▼", "Go one level deeper (smaller scale)", lambda: self.tool.emit("go_deeper", {}))
        self.play = button("▶", "Play / pause", lambda: self._toggle("play", ("play", {}), ("pause", {})))
        self.exit = button("Exit", "Close the app (tap twice)", self._exit_tap)
        self._exit_armed = False
        self._exit_timer = QTimer(self, singleShot=True, interval=3000, timeout=self._disarm_exit)

    def _toggle(self, key: str, on: tuple[str, dict], off: tuple[str, dict]) -> None:
        state = not self.toggles.get(key, False)
        self.toggles[key] = state
        name, args = on if state else off
        self.tool.emit(name, args)
        if key == "play":
            self.play.setText("⏸" if state else "▶")

    def reset_toggles(self) -> None:
        self.toggles.clear()
        self.play.setText("▶")

    def _exit_tap(self) -> None:
        # Two taps, so a stray touch never closes the app in the middle of a lesson.
        if self._exit_armed:
            self.exit_requested.emit()
            return
        self._exit_armed = True
        self.exit.setText("Tap again")
        self._exit_timer.start()

    def _disarm_exit(self) -> None:
        self._exit_armed = False
        self.exit.setText("Exit")


class PartsList(QListWidget):
    """Tick to show/hide; tap a name to highlight it."""

    tool = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filling = False
        self.itemChanged.connect(self._changed)
        self.itemClicked.connect(self._clicked)

    def fill(self, parts: dict[str, dict]) -> None:
        self._filling = True
        self.clear()
        for pid, info in parts.items():
            item = QListWidgetItem(info["name"])
            item.setData(Qt.UserRole, pid)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if info.get("visible", True) else Qt.Unchecked)
            self.addItem(item)
        self._filling = False

    def _changed(self, item: QListWidgetItem) -> None:
        if self._filling:
            return
        pid = item.data(Qt.UserRole)
        self.tool.emit("set_visibility", {"part_id": pid, "visible": item.checkState() == Qt.Checked})

    def _clicked(self, item: QListWidgetItem) -> None:
        self.tool.emit("highlight", {"part_id": item.data(Qt.UserRole)})
