"""Moving between scales: heart -> cardiac muscle -> cardiomyocyte -> mitochondrion.

Not an optical zoom -- the next model is a different model. The transition
sells the change of scale instead: the camera dives towards the point of
interest while the current model fades, the new model is swapped in already
"close", and the camera pulls back while it fades in. About a second in total
at the default 12 frames per half.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from visualization.engine import VisualizationEngine


def ease(x: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, x)))


class Transition:
    def __init__(self, engine: "VisualizationEngine", target_model_id: str, frames: int = 12,
                 focus_point=None, on_swap: Callable[[], None] | None = None):
        self.engine = engine
        self.target = target_model_id
        self.frames = max(2, frames)
        self.focus_point = focus_point
        self.on_swap = on_swap
        self.frame = 0
        self.phase = "out"
        self.error: Exception | None = None

    @property
    def done(self) -> bool:
        return self.phase == "done"

    def step(self) -> bool:
        """Advance one frame. True while the transition is still running."""
        eng = self.engine
        if self.phase == "out":
            self.frame += 1
            x = ease(self.frame / self.frames)
            eng.fade = 1.0 - x
            if self.focus_point is not None:
                eng.camera.dolly_towards(self.focus_point, 0.12)
            else:
                eng.camera.zoom(1.06)
            eng.restyle_all()
            if self.frame >= self.frames:
                try:
                    eng.load_model(self.target, _keep_fade=True)
                    if self.on_swap:
                        self.on_swap()
                except Exception as exc:        # fall back to the old scene, visibly
                    self.error = exc
                    eng.fade = 1.0
                    eng.restyle_all()
                    self.phase = "done"
                    return False
                eng.fade = 0.0
                eng.camera.zoom(2.2)
                eng.restyle_all()
                self.phase, self.frame = "in", 0
            return True
        if self.phase == "in":
            self.frame += 1
            x = ease(self.frame / self.frames)
            eng.fade = x
            eng.camera.zoom((1.0 / 2.2) ** (1.0 / self.frames))
            eng.restyle_all()
            if self.frame >= self.frames:
                eng.fade = 1.0
                eng.reset_camera()
                eng.restyle_all()
                self.phase = "done"
                return False
            return True
        return False

    def finish(self) -> None:
        while self.step():
            pass
