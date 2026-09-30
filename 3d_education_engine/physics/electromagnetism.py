"""Electric fields of point charges, and the magnetic field around a wire.

Field lines are traced by integrating the field direction (RK4) from seed
points around each charge, in the plane of the charges. They start on
positive charges and end on negative ones or leave the view; where lines
crowd together the field is stronger.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from physics.base import Parameter, Simulation, arrow, fmt, polyline_tube
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
        positive = [(q, c) for q, c in charges if q > 0]
        sources, sign = (positive, 1.0) if positive else (charges, -1.0)
        for q, c in sources:
            n = int(8 + 4 * abs(q) * 1e6)
            for a in np.linspace(0, 2 * np.pi, n, endpoint=False):
                start = c + 0.12 * np.array([np.cos(a), np.sin(a), 0.0])
                line = self._trace(start, sign, extent, charges)
                if len(line) > 2:
                    lines.append(polyline_tube(line, 0.012, 6))
        parts = {}
        for i, (q, c) in enumerate(charges, 1):
            parts[f"charge_{i}"] = ScenePart(f"charge_{i}", f"Charge {q * 1e6:+.1f} µC",
                                             pv.Sphere(radius=0.12, center=c), "#ff4a4a" if q > 0 else "#4a7aff")
        if lines:
            parts["field_lines"] = ScenePart("field_lines", "Field lines", pv.merge(lines), "#ffd84d")
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
    PARAMETERS = [Parameter("current", 5.0, -20.0, 20.0, "A", "current in the wire (negative flows down)")]
    ALIASES = {"i": "current", "amps": "current"}

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
                "field_arrows": ScenePart("field_arrows", "Field direction", pv.merge(arrows), "#9fc6e8")}

    def measurements(self):
        i = abs(self.p("current"))
        return {f"B_at_{int(r * 100)}cm": fmt(MU0 * i / (2 * np.pi * r) * 1e6, "µT", 2) for r in (0.01, 0.05, 0.4, 1.0)}
