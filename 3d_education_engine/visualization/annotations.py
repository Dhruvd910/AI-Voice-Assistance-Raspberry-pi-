"""Labels in the scene, and the text overlays around it.

Two kinds of text:
* labels pinned to 3D points ("Left ventricle"), each with an id so it can be
  removed by name;
* fixed overlays: the model title, the "Educational model — not to scale"
  badge, and the attribution line that every third-party asset must carry.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pyvista as pv

NOT_TO_SCALE = "Educational model — not to scale"


@dataclass
class Label:
    id: str
    text: str
    position: tuple[float, float, float]
    actor: object | None = None
    # Textbook callouts and riding labels are drawn as separate actors (the
    # words, and a pointer line); `extra` holds the ones beyond `actor`.
    extra: list = field(default_factory=list)
    follows: str | None = None           # a part id: the label rides on it


class Annotations:
    def __init__(self, plotter, font_size: int = 11):
        self.plotter = plotter
        self.font_size = font_size
        self.labels: dict[str, Label] = {}
        self.measurements: dict[str, list] = {}
        self._ids = itertools.count(1)
        self._overlay_names: list[str] = []

    # ------------------------------------------------------------ labels
    def add_label(self, text: str, position, label_id: str | None = None) -> Label:
        label_id = label_id or f"label_{next(self._ids)}"
        if label_id in self.labels:
            self.remove_label(label_id)
        pos = tuple(float(v) for v in position)
        actor = self.plotter.add_point_labels(
            np.array([pos]), [text], font_size=self.font_size, point_size=6,
            point_color="#ffd84d", text_color="white", shape_color="#202830",
            shape_opacity=0.75, always_visible=True, name=f"lbl:{label_id}",
            render_points_as_spheres=True, reset_camera=False)
        label = Label(label_id, text, pos, actor)
        self.labels[label_id] = label
        return label

    def remove_label(self, label_id: str) -> bool:
        label = self.labels.pop(label_id, None)
        if label is None:
            return False
        self.plotter.remove_actor(f"lbl:{label_id}", reset_camera=False, render=False)
        for actor in label.extra:
            self.plotter.remove_actor(actor, reset_camera=False, render=False)
        return True

    # ------------------------------------------------------------ textbook labels
    # The words to the side, a thin line back to the part: the way a textbook
    # labels a cell. Point labels at each part's centre piled up in the middle
    # of a cell, where every organelle's centre is, and VTK's label placer
    # quietly dropped whichever ones overlapped. These are one billboard actor
    # each, always facing the camera and never dropped.
    def _words(self, text: str, position, align: str = "center", size: int | None = None):
        from vtkmodules.vtkRenderingCore import vtkBillboardTextActor3D
        actor = vtkBillboardTextActor3D()
        actor.SetInput(_vtk_safe(text))
        actor.SetPosition(*[float(v) for v in position])
        prop = actor.GetTextProperty()
        prop.SetFontSize(size or self.font_size + 2)
        prop.SetBold(True)
        prop.SetColor(1.0, 1.0, 1.0)
        prop.SetShadow(True)
        prop.SetVerticalJustificationToCentered()
        {"left": prop.SetJustificationToLeft, "right": prop.SetJustificationToRight,
         "center": prop.SetJustificationToCentered}[align]()
        actor.SetVisibility(self.visible)
        self.plotter.renderer.AddActor(actor)
        return actor

    def add_callout(self, text: str, anchor, at, label_id: str, align: str) -> Label:
        if label_id in self.labels:
            self.remove_label(label_id)
        anchor, at = np.asarray(anchor, float), np.asarray(at, float)
        words = self._words(text, at, align)
        # The line stops short of the words, so it points AT them.
        line = self.plotter.add_mesh(pv.Line(anchor, anchor + (at - anchor) * 0.94), color="#9fb0d8",
                                     line_width=2, lighting=False, reset_camera=False, render=False,
                                     name=f"lbl:{label_id}")
        dot = self.plotter.add_mesh(pv.PolyData(anchor[None, :]), color="#ffd84d", point_size=7,
                                    render_points_as_spheres=True, reset_camera=False, render=False,
                                    name=f"lbl:{label_id}:dot")
        for actor in (line, dot):
            actor.SetVisibility(self.visible)
        label = Label(label_id, text, tuple(at), line, extra=[words, dot])
        self.labels[label_id] = label
        return label

    def add_follower(self, text: str, part_id: str, position, label_id: str) -> Label:
        """Words that ride above a moving part (a planet, a pendulum bob)."""
        if label_id in self.labels:
            self.remove_label(label_id)
        words = self._words(text, position, "center", size=self.font_size + 1)
        label = Label(label_id, text, tuple(float(v) for v in position), None, extra=[words],
                      follows=part_id)
        self.labels[label_id] = label
        return label

    def move_follower(self, label_id: str, position) -> None:
        label = self.labels.get(label_id)
        if label is not None and label.extra:
            label.extra[0].SetPosition(*[float(v) for v in position])

    # Below this width the view is Liza's board thumbnail, where a dozen names
    # are a smudge; they come back when it is opened full screen.
    visible = True

    def set_visible(self, visible: bool) -> None:
        self.visible = bool(visible)
        for label in self.labels.values():
            for actor in [label.actor, *label.extra]:
                if actor is not None and hasattr(actor, "SetVisibility"):
                    actor.SetVisibility(self.visible)

    def clear_labels(self) -> None:
        for label_id in list(self.labels):
            self.remove_label(label_id)
        for mid in list(self.measurements):
            self.remove_measurement(mid)

    # ------------------------------------------------------------ measurements
    def add_measurement(self, a, b, text: str) -> str:
        mid = f"measure_{next(self._ids)}"
        line = pv.Line(a, b)
        self.plotter.add_mesh(line, color="#ffd84d", line_width=3, name=f"msr:{mid}",
                              reset_camera=False, render_lines_as_tubes=False)
        midpoint = (np.asarray(a) + np.asarray(b)) / 2
        self.add_label(text, midpoint, label_id=f"{mid}_text")
        self.measurements[mid] = [a, b]
        return mid

    def remove_measurement(self, mid: str) -> None:
        self.measurements.pop(mid, None)
        self.plotter.remove_actor(f"msr:{mid}", reset_camera=False, render=False)
        self.remove_label(f"{mid}_text")

    # ------------------------------------------------------------ overlays
    def set_overlays(self, title: str, disclaimer: str | None, attribution: str | None) -> None:
        for name in self._overlay_names:
            self.plotter.remove_actor(name, reset_camera=False, render=False)
        self._overlay_names = []
        width = self.plotter.window_size[0]
        if title:
            self.plotter.add_text(_wrap(_vtk_safe(title), max(24, width // 16)), position="upper_left",
                                  font_size=self.font_size + 1, color="white", name="ovl:title")
            self._overlay_names.append("ovl:title")
        # Disclaimer bottom-left and attribution bottom-right, each wrapped to
        # its own share of the width so they never run into each other.
        if disclaimer:
            self.plotter.add_text(_wrap(_vtk_safe(disclaimer), max(30, width // 10)), position="lower_left",
                                  font_size=self.font_size - 3, color="#ffc44d", name="ovl:disclaimer")
            self._overlay_names.append("ovl:disclaimer")
        if attribution:
            self.plotter.add_text(_wrap(_vtk_safe(attribution), max(24, width // 15)), position="lower_right",
                                  font_size=self.font_size - 4, color="#9aa4ad", name="ovl:attribution")
            self._overlay_names.append("ovl:attribution")


def _vtk_safe(text: str) -> str:
    """VTK's built-in font has no arrows; everything else we use renders."""
    return text.replace("→", "->").replace("⟶", "->")


def _wrap(text: str, width: int) -> str:
    words, lines, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "\n".join(lines)
