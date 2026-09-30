"""A simple series circuit: a cell and two resistors, with current shown moving.

The moving dots show CONVENTIONAL current (from + to −) and their speed is
exaggerated for visibility -- the real drift speed of electrons in a wire is
around a millimetre per second, and electrons move the other way.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from physics.base import Parameter, Simulation, fmt, polyline_tube
from visualization.scene import ScenePart


class SeriesCircuit(Simulation):
    id = "series_circuit"
    title = "Series circuit (Ohm's law)"
    front = "+z"
    disclaimer = ("Dots show conventional current (+ to −); speed exaggerated. Real electrons drift "
                  "the other way at about a millimetre per second.")
    equations = ["V = I·R  (Ohm's law)", "series: R_total = R1 + R2", "the same current flows through every component",
                 "V = V1 + V2,  V1 = I·R1", "power P = V·I"]
    PARAMETERS = [Parameter("voltage", 9.0, 1.0, 24.0, "V", "cell/battery voltage"),
                  Parameter("r1", 100.0, 1.0, 1000.0, "Ω", "resistor 1"),
                  Parameter("r2", 200.0, 0.0, 1000.0, "Ω", "resistor 2 (0 removes it)")]
    ALIASES = {"v": "voltage", "battery": "voltage", "resistance": "r1", "resistor": "r1", "resistor1": "r1",
               "resistor2": "r2", "resistance2": "r2"}
    loop = True
    duration = 60.0
    W, H = 6.0, 4.0

    def current(self):
        return self.p("voltage") / (self.p("r1") + self.p("r2"))

    def loop_points(self):
        w, h = self.W / 2, self.H / 2
        corners = [(-w, -h), (w, -h), (w, h), (-w, h), (-w, -h)]
        pts = []
        for (x0, y0), (x1, y1) in zip(corners, corners[1:]):
            for s in np.linspace(0, 1, 60, endpoint=False):
                pts.append((x0 + (x1 - x0) * s, y0 + (y1 - y0) * s, 0.0))
        return np.array(pts)

    def build_parts(self):
        w, h = self.W / 2, self.H / 2
        pts = self.loop_points()
        parts = {"wires": ScenePart("wires", "Wires", polyline_tube(np.vstack([pts, pts[:1]]), 0.04), "#c88033"),
                 "cell": ScenePart("cell", f"Cell ({self.p('voltage'):.1f} V)",
                                   pv.merge([pv.Box((-w - 0.35, -w + 0.35, -0.5, -0.35, -0.3, 0.3)),
                                             pv.Box((-w - 0.2, -w + 0.2, 0.35, 0.45, -0.3, 0.3))]), "#e0e0e0"),
                 "r1": ScenePart("r1", f"Resistor 1 ({self.p('r1'):.0f} Ω)", pv.Box((-0.8, 0.8, -h - 0.25, -h + 0.25, -0.25, 0.25)), "#d8b080")}
        if self.p("r2") > 0:
            parts["r2"] = ScenePart("r2", f"Resistor 2 ({self.p('r2'):.0f} Ω)", pv.Box((-0.8, 0.8, h - 0.25, h + 0.25, -0.25, 0.25)), "#d8b080")
        parts["charges"] = ScenePart("charges", "Current (conventional)", self._dots(0.0), "#ffd84d")
        return parts

    def _dots(self, t):
        pts = self.loop_points()
        n = len(pts)
        # Out of the + terminal (the longer plate, lower on the left side),
        # along the bottom, up through the resistors and back into the −
        # terminal: the order loop_points() walks the loop in.
        shift = int(t * self.current() * 400) % n
        idx = (np.arange(0, n, 12) + shift) % n
        return pv.PolyData(pts[idx]).glyph(geom=pv.Sphere(radius=0.09), orient=False, scale=False)

    def apply(self, engine):
        engine.update_part_mesh("charges", self._dots(self.t))

    def measurements(self):
        i = self.current()
        out = {"total_resistance": fmt(self.p("r1") + self.p("r2"), "Ω", 0), "current": fmt(i * 1000, "mA", 1),
               "voltage_across_r1": fmt(i * self.p("r1"), "V"), "power": fmt(self.p("voltage") * i, "W", 3)}
        if self.p("r2") > 0:
            out["voltage_across_r2"] = fmt(i * self.p("r2"), "V")
        return out
