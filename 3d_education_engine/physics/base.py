"""What every simulation has: parameters, equations, time, geometry, measurements.

A Simulation owns its PHYSICS (state as a function of time, worked out with
NumPy/SciPy) and describes its SCENE (parts with geometry). It never touches
the renderer directly: `apply(engine)` moves parts through the engine's
move_part / update_part_mesh, so a simulation runs identically on screen and
in a test with no display.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pyvista as pv

from visualization.scene import ScenePart


@dataclass
class Parameter:
    name: str
    value: float
    minimum: float
    maximum: float
    unit: str
    description: str
    # The smallest change a slider makes; 0 lets the screen pick a round one.
    step: float = 0.0

    def check(self, value: float) -> float:
        value = float(value)
        if not self.minimum <= value <= self.maximum:
            raise ValueError(f"{self.name} must be between {self.minimum} and {self.maximum} {self.unit}")
        return value


class Simulation:
    id = "simulation"
    title = "Simulation"
    equations: list[str] = []
    disclaimer = "Simplified educational simulation."
    units = "m"
    front = "-y"
    duration = 10.0          # seconds of simulated time before it starts again
    # loop: the motion is periodic and t wraps round seamlessly. Otherwise the
    # run is one story (a throw, a fall, a crash): it holds its last frame for
    # END_PAUSE seconds so the result can be read, then plays again. A student
    # watching a ball land once and then a frozen screen thought it was broken;
    # with `repeat` off it stops at the end as it used to.
    loop = False
    repeat = True
    END_PAUSE = 1.5
    PARAMETERS: list[Parameter] = []
    ALIASES: dict[str, str] = {}      # "launch angle" -> "angle"

    def __init__(self, **overrides):
        self.params = {p.name: replace(p) for p in self.PARAMETERS}
        for key, value in overrides.items():
            self.set_parameter(key, value)
        self.t = 0.0
        self.held = 0.0
        self.playing = False
        self.speed = 1.0
        self.prepare()

    # ------------------------------------------------------------ parameters
    def p(self, name: str) -> float:
        return self.params[name].value

    def resolve_parameter(self, name: str) -> str:
        key = name.strip().lower().replace(" ", "_").replace("-", "_")
        key = self.ALIASES.get(key, self.ALIASES.get(name.strip().lower(), key))
        if key not in self.params:
            raise ValueError(f"{self.title} has no parameter {name!r}; it has {', '.join(self.params)}")
        return key

    def set_parameter(self, name: str, value: float) -> str:
        key = self.resolve_parameter(name)
        self.params[key].value = self.params[key].check(value)
        return key

    def prepare(self) -> None:
        """Recompute anything that depends on the parameters (trajectories...)."""

    # ------------------------------------------------------------ time
    def reset(self) -> None:
        self.t = 0.0
        self.held = 0.0

    def advance(self, dt: float) -> None:
        if self.t >= self.duration and not self.loop:
            # At the end of a run: hold it, then go round again.
            self.held += dt
            if self.repeat and self.held >= self.END_PAUSE:
                self.reset()
            elif not self.repeat:
                self.playing = False
            return
        self.t += dt * self.speed
        if self.t >= self.duration:
            if self.loop:
                self.t %= self.duration
            else:
                self.t = self.duration
                self.held = 0.0

    def step(self, n: int = 1, dt: float = 1 / 30) -> None:
        for _ in range(n):
            self.advance(dt)

    # ------------------------------------------------------------ scene
    def build_parts(self) -> dict[str, ScenePart]:
        raise NotImplementedError

    def apply(self, engine) -> None:
        """Move the dynamic parts to time self.t."""

    def measurements(self) -> dict[str, str]:
        return {}

    def describe(self) -> dict:
        return {"id": self.id, "title": self.title, "time_s": round(self.t, 3), "playing": self.playing,
                "parameters": {k: {"value": v.value, "unit": v.unit, "min": v.minimum, "max": v.maximum,
                                   "step": v.step, "description": v.description} for k, v in self.params.items()},
                "equations": self.equations, "measurements": self.measurements()}


# ------------------------------------------------------------------ geometry helpers
def arrow(start, vector, shaft: float = 0.04, tip: float = 0.12) -> pv.PolyData:
    """An arrow from `start` along `vector`, as long as the vector, with a shaft
    and head of fixed thickness (so a short arrow is not also a thin one)."""
    v = np.asarray(vector, dtype=float)
    length = float(np.linalg.norm(v))
    if length < 1e-9:
        return pv.Sphere(radius=1e-4, center=start)
    return pv.Arrow(start=start, direction=v, scale=length,
                    shaft_radius=min(0.2, shaft / length), tip_radius=min(0.45, tip / length),
                    tip_length=min(0.5, 2.5 * tip / length)).triangulate()


def polyline_tube(points, radius: float, sides: int = 8) -> pv.PolyData:
    pts = np.asarray(points, dtype=float)
    if len(pts) < 2:
        pts = np.vstack([pts, pts + 1e-6]) if len(pts) else np.zeros((2, 3))
    line = pv.lines_from_points(pts)
    return line.tube(radius=radius, n_sides=sides).triangulate()


def ground(xmin: float, xmax: float, depth: float = 2.0, z: float = 0.0) -> pv.PolyData:
    return pv.Box((xmin, xmax, -depth / 2, depth / 2, z - 0.05 * depth, z)).triangulate()


def fmt(value: float, unit: str, digits: int = 2) -> str:
    return f"{value:.{digits}f} {unit}".strip()


def point_along(points, fraction: float) -> np.ndarray:
    """The point `fraction` (0..1) of the way along a polyline, by length."""
    pts = np.asarray(points, dtype=float)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    d = min(max(fraction, 0.0), 1.0) * cum[-1]
    i = int(np.clip(np.searchsorted(cum, d, side="right") - 1, 0, len(seg) - 1))
    f = (d - cum[i]) / seg[i] if seg[i] > 0 else 0.0
    return pts[i] + f * (pts[i + 1] - pts[i])


def beads(points, radius: float, resolution: int = 10) -> pv.PolyData:
    """Small balls at the given points, as one mesh: light pulses, charges."""
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    if len(pts) == 0:
        return pv.Sphere(radius=1e-4)
    ball = pv.Sphere(radius=radius, theta_resolution=resolution, phi_resolution=resolution)
    return pv.PolyData(pts).glyph(geom=ball, scale=False, orient=False)
