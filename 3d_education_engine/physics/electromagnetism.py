"""Electric fields of point charges, and the magnetic field around a wire.

Field lines are traced by integrating the field direction (RK4) from seed
points around each charge, in the plane of the charges. They start on
positive charges and end on negative ones or leave the view; where lines
crowd together the field is stronger.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from physics.base import Parameter, Simulation, arrow, beads, fmt, point_along, polyline_tube
from visualization.scene import ScenePart

K = 8.9875517923e9          # Coulomb constant, N·m²/C²
MU0 = 4e-7 * np.pi          # permeability of free space (to 9 s.f.), T·m/A


class ElectricField(Simulation):
    id = "electric_field"
    title = "Electric field of point charges"
    front = "+z"
    disclaimer = "Field lines in the plane of the charges. Red: positive charge; blue: negative."
    equations = ["E = k·q / r²  (point charge)", "k = 8.99 × 10⁹ N·m²/C²",
                 "field lines start on + charges and end on − charges; they never cross"]
    PARAMETERS = [Parameter("charge1", 1.0, -5.0, 5.0, "µC", "charge at the left (0 removes it)"),
                  Parameter("charge2", 0.0, -5.0, 5.0, "µC", "charge at the right (0 removes it)"),
                  Parameter("separation", 2.0, 0.5, 5.0, "m", "distance between the charges")]
    ALIASES = {"q1": "charge1", "q2": "charge2", "charge": "charge1", "d": "separation", "distance": "separation"}
    loop = True
    duration = 4.0
    # Small + test charges drifting along the lines, the way a free positive
    # charge would be pushed: out of + charges, into − ones.
    _lines: list = []

    def flow(self, t):
        pts = []
        for line, sign in self._lines:
            for k in range(2):
                f = ((t / self.duration) + k / 2) % 1.0
                pts.append(point_along(line, f if sign > 0 else 1.0 - f))
        return beads(pts, 0.045, 8)

    def apply(self, engine):
        if self._lines:
            engine.update_part_mesh("test_charges", self.flow(self.t))

    def charges(self):
        d = self.p("separation")
        out = []
        for key, x in (("charge1", -d / 2), ("charge2", d / 2)):
            q = self.p(key) * 1e-6
            if q != 0:
                out.append((q, np.array([x, 0.0, 0.0])))
        if len(out) == 1:                        # a lone charge sits at the centre
            out = [(out[0][0], np.zeros(3))]
        return out

    def field(self, p):
        e = np.zeros(3)
        for q, c in self.charges():
            r = p - c
            dist = np.linalg.norm(r)
            if dist > 1e-6:
                e += K * q * r / dist ** 3
        return e

    def _trace(self, start, sign, extent, charges):
        pts, p, h = [start], start.copy(), 0.03 * extent
        for _ in range(900):
            def f(x):
                e = self.field(x)
                n = np.linalg.norm(e)
                return sign * e / n if n else np.zeros(3)
            k1 = f(p); k2 = f(p + h / 2 * k1); k3 = f(p + h / 2 * k2); k4 = f(p + h * k3)
            p = p + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
            pts.append(p.copy())
            if np.abs(p).max() > extent or any(np.linalg.norm(p - c) < 0.06 * extent / 3 for _, c in charges):
                break
        return np.array(pts)

    def build_parts(self):
        charges = self.charges()
        extent = max(3.0, 1.6 * self.p("separation"))
        lines = []
        self._lines = []
        positive = [(q, c) for q, c in charges if q > 0]
        sources, sign = (positive, 1.0) if positive else (charges, -1.0)
        for q, c in sources:
            n = int(8 + 4 * abs(q) * 1e6)
            for a in np.linspace(0, 2 * np.pi, n, endpoint=False):
                start = c + 0.12 * np.array([np.cos(a), np.sin(a), 0.0])
                line = self._trace(start, sign, extent, charges)
                if len(line) > 2:
                    lines.append(polyline_tube(line, 0.012, 6))
                    self._lines.append((line, sign))
        parts = {}
        for i, (q, c) in enumerate(charges, 1):
            parts[f"charge_{i}"] = ScenePart(f"charge_{i}", f"Charge {q * 1e6:+.1f} µC",
                                             pv.Sphere(radius=0.12, center=c), "#ff4a4a" if q > 0 else "#4a7aff")
        if lines:
            parts["field_lines"] = ScenePart("field_lines", "Field lines", pv.merge(lines), "#ffd84d")
            parts["test_charges"] = ScenePart("test_charges", "Test charges (+)", self.flow(0.0), "#ff8a8a")
        grid = [np.array([x, y, 0.0]) for x in np.linspace(-extent * 0.9, extent * 0.9, 13)
                for y in np.linspace(-extent * 0.9, extent * 0.9, 13)
                if all(np.linalg.norm(np.array([x, y, 0]) - c) > 0.3 for _, c in charges)]
        arrows = [arrow(p, self.field(p) / (np.linalg.norm(self.field(p)) or 1) * 0.18 * extent / 3, 0.01, 0.03) for p in grid]
        parts["field_arrows"] = ScenePart("field_arrows", "Field direction", pv.merge(arrows), "#9fc6e8", opacity=0.7)
        return parts

    def measurements(self):
        charges = self.charges()
        probe = charges[0][1] + np.array([1.0, 0.0, 0.0]) if charges else np.array([1.0, 0, 0])
        out = {"field_1m_right_of_first_charge": fmt(float(np.linalg.norm(self.field(probe))), "N/C", 0)}
        for i, (q, _c) in enumerate(charges, 1):
            out[f"charge_{i}_field_at_1m_alone"] = fmt(K * abs(q), "N/C", 0)
        if len(charges) == 2:
            (q1, c1), (q2, c2) = charges
            out["force_between_charges"] = fmt(K * q1 * q2 / np.linalg.norm(c1 - c2) ** 2, "N (+ repel, − attract)", 4)
        return out


class MagneticFieldWire(Simulation):
    id = "magnetic_field_wire"
    title = "Magnetic field around a straight wire"
    front = "iso"
    disclaimer = ("Conventional current flows upwards (arrow). Right-hand grip rule: thumb along the current, "
                  "fingers curl the way the field circles.")
    equations = ["B = μ₀·I / (2π·r)", "μ₀ = 4π × 10⁻⁷ T·m/A"]
    PARAMETERS = [Parameter("current", 5.0, -20.0, 20.0, "A", "current in the wire (negative flows down)", 0.5)]
    ALIASES = {"i": "current", "amps": "current"}
    loop = True
    duration = 8.0

    def compass(self, t):
        """Beads running round the field lines, the way the field points;
        faster for a bigger current, still when there is none."""
        turn = 2 * np.pi * t / self.duration * (self.p("current") / 5.0)
        pts = []
        for z in (-1.0, 0.0, 1.0):
            for r in (0.4, 0.8, 1.2):
                for k in range(3):
                    a = turn * (0.4 / r) + k * 2 * np.pi / 3 + r
                    pts.append((r * np.cos(a), r * np.sin(a), z))
        return beads(pts, 0.04, 8)

    def apply(self, engine):
        engine.update_part_mesh("field_beads", self.compass(self.t))

    def build_parts(self):
        rings, arrows = [], []
        up = 1.0 if self.p("current") >= 0 else -1.0
        for z in (-1.0, 0.0, 1.0):
            for r in (0.4, 0.8, 1.2):
                ring = [(r * np.cos(a), r * np.sin(a), z) for a in np.linspace(0, 2 * np.pi, 80)]
                rings.append(polyline_tube(ring, 0.008 + 0.004 * abs(self.p("current")) / 20 / r))
                for a in (0.0, np.pi):
                    p = np.array([r * np.cos(a), r * np.sin(a), z])
                    tangent = up * np.array([-np.sin(a), np.cos(a), 0.0])     # anticlockwise from above for upward current
                    arrows.append(arrow(p, tangent * 0.2, 0.012, 0.04))
        wire_dir = np.array([0, 0, up])
        return {"wire": ScenePart("wire", "Wire", pv.Cylinder(radius=0.05, height=3.0, direction=(0, 0, 1)), "#c88033"),
                "current": ScenePart("current", "Current direction", arrow(-wire_dir * 0.3 + [0.12, 0, 0], wire_dir * 0.8, 0.02, 0.06), "#ff6b4a"),
                "field_lines": ScenePart("field_lines", "Magnetic field lines", pv.merge(rings), "#4fb0ff"),
                "field_arrows": ScenePart("field_arrows", "Field direction", pv.merge(arrows), "#9fc6e8"),
                "field_beads": ScenePart("field_beads", "Moving with the field", self.compass(0.0), "#ffffff")}

    def measurements(self):
        i = abs(self.p("current"))
        return {f"B_at_{int(r * 100)}cm": fmt(MU0 * i / (2 * np.pi * r) * 1e6, "µT", 2) for r in (0.01, 0.05, 0.4, 1.0)}


def _helix(radius, length, turns, centre_x=0.0, points=12):
    s = np.linspace(0, 1, max(60, int(turns * points)))
    a = 2 * np.pi * turns * s
    return np.column_stack([centre_x - length / 2 + length * s, radius * np.cos(a), radius * np.sin(a)])


def _bar_magnet(centre_x, length=1.6, half=0.22):
    """(north box, south box) with north on the right, facing the coil."""
    return (pv.Box((centre_x, centre_x + length / 2, -half, half, -half, half)),
            pv.Box((centre_x - length / 2, centre_x, -half, half, -half, half)))


class ElectromagneticInduction(Simulation):
    id = "electromagnetic_induction"
    title = "Electromagnetic induction (magnet and coil)"
    front = "-y"
    disclaimer = ("Faraday's experiment: the needle moves only while the magnet moves. Pushed in it swings one way, "
                  "pulled out the other. Needle in arbitrary units.")
    equations = ["induced emf ε = −N·dΦ/dt  (Faraday's law)", "faster motion, more turns or a stronger magnet → bigger current",
                 "the current opposes the change that makes it (Lenz's law)"]
    PARAMETERS = [Parameter("speed", 0.5, 0.1, 2.0, "pushes/s", "how often the magnet is pushed in and pulled out", 0.1),
                  Parameter("turns", 50.0, 10.0, 200.0, "", "number of turns in the coil", 10.0),
                  Parameter("magnet_strength", 1.0, 0.2, 3.0, "×", "strength of the magnet", 0.1)]
    ALIASES = {"frequency": "speed", "n": "turns", "coils": "turns", "strength": "magnet_strength", "magnet": "magnet_strength"}
    loop = True
    COIL_X, REACH = 1.5, 3.2

    def prepare(self):
        self.duration = 1 / self.p("speed")
        ts = np.linspace(0, self.duration, 400)
        self.peak = max(abs(self.emf(t)) for t in ts) or 1.0

    def magnet_x(self, t):
        # Out at the left, in to the middle of the coil, out again: smoothly.
        return self.COIL_X - 0.8 - self.REACH * 0.5 * (1 + np.cos(2 * np.pi * self.p("speed") * t))

    def flux(self, x):
        d = (x + 0.8) - self.COIL_X          # north pole to the middle of the coil
        return self.p("magnet_strength") / (1 + (d / 0.9) ** 2) ** 1.5

    def emf(self, t, h=1e-4):
        return -self.p("turns") * (self.flux(self.magnet_x(t + h)) - self.flux(self.magnet_x(t - h))) / (2 * h)

    def needle(self, t):
        # Full scale: the default settings' biggest kick; stronger settings pin it.
        swing = np.clip(self.emf(t) / 220.0, -1, 1) * np.radians(70)
        pivot = np.array([self.COIL_X, 0.0, -2.4])
        return arrow(pivot, 1.0 * np.array([np.sin(swing), 0, np.cos(swing)]), 0.03, 0.08)

    def build_parts(self):
        n_draw = int(np.clip(self.p("turns") / 8, 5, 20))
        north, south = _bar_magnet(self.magnet_x(0))
        dial = pv.Disc(center=(self.COIL_X, 0.05, -2.4), inner=0.0, outer=1.25, normal=(0, 1, 0), c_res=40).triangulate()
        return {"coil": ScenePart("coil", f"Coil ({int(self.p('turns'))} turns)", polyline_tube(_helix(0.7, 2.0, n_draw, self.COIL_X), 0.04, 6), "#c88033"),
                "north": ScenePart("north", "North pole", north, "#ff5a5a"),
                "south": ScenePart("south", "South pole", south, "#4f90ff"),
                "wires": ScenePart("wires", "Wires to the galvanometer", polyline_tube([(self.COIL_X - 1.0, 0, -0.7), (self.COIL_X - 1.0, 0, -1.6), (self.COIL_X + 1.0, 0, -1.6), (self.COIL_X + 1.0, 0, -0.7)], 0.02), "#c88033"),
                "galvanometer": ScenePart("galvanometer", "Galvanometer", dial, "#e8e8e8"),
                "needle": ScenePart("needle", "Needle", self.needle(0.0), "#ff3030")}

    def apply(self, engine):
        x = self.magnet_x(self.t) - self.magnet_x(0)
        engine.move_part("north", (x, 0, 0))
        engine.move_part("south", (x, 0, 0))
        engine.update_part_mesh("needle", self.needle(self.t))

    def measurements(self):
        e = self.emf(self.t)
        return {"needle_now": "still: the magnet is not moving" if abs(e) < 2 else ("to the right" if e > 0 else "to the left"),
                "biggest_kick": fmt(self.peak, "units", 0),
                "rule": "faster, more turns, stronger magnet → bigger kick"}


class ACGenerator(Simulation):
    id = "ac_generator"
    title = "AC generator"
    front = "iso"
    disclaimer = ("A coil turning between the poles of a magnet. The graph underneath draws the emf as it "
                  "turns: it reverses every half turn, which is why the current is alternating.")
    equations = ["emf ε = N·B·A·ω·sin(ω·t)", "peak emf ε₀ = N·B·A·ω", "ω = 2π·f", "rms value = ε₀ / √2"]
    PARAMETERS = [Parameter("frequency", 0.5, 0.1, 2.0, "turns/s", "how fast the coil turns", 0.1),
                  Parameter("turns", 100.0, 1.0, 500.0, "", "number of turns in the coil", 10.0),
                  Parameter("field", 0.5, 0.05, 1.5, "T", "magnetic field strength B", 0.05)]
    ALIASES = {"f": "frequency", "speed": "frequency", "rpm": "frequency", "n": "turns", "b": "field",
               "magnetic_field": "field"}
    loop = True
    AREA = 0.02               # m², the coil's area
    A, B = 1.0, 0.8           # half-width and half-height of the drawn coil

    def prepare(self):
        self.duration = 1 / self.p("frequency")
        self.w = 2 * np.pi * self.p("frequency")
        self.peak = self.p("turns") * self.p("field") * self.AREA * self.w

    def emf(self, t):
        return self.peak * np.sin(self.w * t)

    def coil(self, t):
        a = self.w * t
        u = np.array([np.cos(a + np.pi / 2), np.sin(a + np.pi / 2), 0.0])   # in the coil's plane, across
        z = np.array([0, 0, 1.0])
        c = [self.A * u * sx + self.B * z * sz for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1))]
        return polyline_tube(np.array(c), 0.05, 8)

    def graph(self, t):
        ts = np.linspace(0, t, max(2, int(120 * t / self.duration) + 2))
        x = -2.2 + 4.4 * ts / self.duration
        return polyline_tube(np.column_stack([x, np.zeros_like(x) - 2.0, -2.3 + 0.8 * np.sin(self.w * ts)]), 0.035, 6)

    def build_parts(self):
        axis = np.linspace(-2.2, 2.2, 2)
        return {"north": ScenePart("north", "North pole", pv.Box((-2.6, -1.8, -0.8, 0.8, -1.1, 1.1)), "#ff5a5a"),
                "south": ScenePart("south", "South pole", pv.Box((1.8, 2.6, -0.8, 0.8, -1.1, 1.1)), "#4f90ff"),
                "field_lines": ScenePart("field_lines", "Magnetic field (N to S)", pv.merge([arrow((-1.7, y, z), (3.3, 0, 0), 0.01, 0.05) for y in (-0.5, 0.5) for z in (-0.7, 0.0, 0.7)]), "#8899aa", opacity=0.6),
                "coil": ScenePart("coil", "Coil", self.coil(0.0), "#c88033"),
                "shaft": ScenePart("shaft", "Axle", polyline_tube([(0, 0, -1.4), (0, 0, 1.4)], 0.03), "#c0c8d0"),
                "graph_axis": ScenePart("graph_axis", "Time axis", polyline_tube(np.column_stack([axis, [-2.0, -2.0], [-2.3, -2.3]]), 0.015), "#c0c8d0"),
                "graph": ScenePart("graph", "emf as it turns", self.graph(0.01), "#ffd84d")}

    def apply(self, engine):
        engine.update_part_mesh("coil", self.coil(self.t))
        engine.update_part_mesh("graph", self.graph(max(self.t, 0.01)))

    def measurements(self):
        return {"peak_emf": fmt(self.peak, "V"), "rms_emf": fmt(self.peak / np.sqrt(2), "V"),
                "emf_now": fmt(self.emf(self.t), "V"), "frequency": fmt(self.p("frequency"), "Hz")}
