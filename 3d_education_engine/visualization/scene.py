"""What a model IS once loaded: named parts with geometry, and nothing about
how they are currently being shown.

Every source of models -- a GLB from the registry, a cell generator, RDKit, a
physics simulation -- produces a SceneModel, so the visualization engine has
one kind of thing to draw. Display STATE (visible, highlighted, clipped...)
belongs to the engine, which is why a cached SceneModel can be shown again
without being reloaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np
import pyvista as pv


@dataclass
class ScenePart:
    id: str
    name: str
    mesh: pv.DataSet
    color: str = "#cccccc"
    opacity: float = 1.0
    smooth_shading: bool = True
    visible: bool = True
    description: str = ""
    # Extra per-part rendering hints: {"point_size": 8} or {"render_lines_as_tubes": True}
    style: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> np.ndarray:
        return np.asarray(self.mesh.center, dtype=float)

    @property
    def bounds(self) -> tuple[float, ...]:
        return tuple(self.mesh.bounds)


@dataclass
class SceneLabel:
    text: str
    position: tuple[float, float, float]
    id: str = ""


class Animator(Protocol):
    """Something that moves a scene over time: a heartbeat, a simulation."""

    name: str

    def tick(self, dt: float, engine: Any) -> None: ...

    def reset(self, engine: Any) -> None: ...


@dataclass
class SceneModel:
    model_id: str
    title: str
    parts: dict[str, ScenePart]
    units: str = "arbitrary units"
    disclaimer: str | None = None
    attribution: str | None = None
    labels: list[SceneLabel] = field(default_factory=list)
    # Named animations this model offers; the engine plays one at a time.
    animations: dict[str, Callable[[], Animator]] = field(default_factory=dict)
    # Domain objects the tools may need: the Molecule, the Simulation...
    extras: dict[str, Any] = field(default_factory=dict)
    # Where the camera starts: (azimuth, elevation) in degrees.
    view: tuple[float, float] = (0.0, 0.0)

    def bounds(self, part_ids: list[str] | None = None) -> tuple[float, ...]:
        ids = part_ids or [p for p, part in self.parts.items() if part.visible]
        boxes = np.array([self.parts[p].bounds for p in ids if p in self.parts])
        if boxes.size == 0:
            return (-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)
        return (boxes[:, 0].min(), boxes[:, 1].max(), boxes[:, 2].min(),
                boxes[:, 3].max(), boxes[:, 4].min(), boxes[:, 5].max())

    def center(self, part_ids: list[str] | None = None) -> np.ndarray:
        b = self.bounds(part_ids)
        return np.array([(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2])

    def size(self) -> float:
        b = self.bounds()
        return float(np.linalg.norm([b[1] - b[0], b[3] - b[2], b[5] - b[4]]))

    def memory_bytes(self) -> int:
        total = 0
        for part in self.parts.values():
            try:
                total += int(part.mesh.actual_memory_size) * 1024
            except Exception:
                total += part.mesh.n_points * 64
        return total
