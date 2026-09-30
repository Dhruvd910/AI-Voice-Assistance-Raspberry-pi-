"""Waves and light: travelling and standing waves, interference, diffraction,
reflection and refraction."""

from __future__ import annotations

import numpy as np
import pyvista as pv

from physics.base import Parameter, Simulation, arrow, fmt, polyline_tube
from visualization.scene import ScenePart


class SineWave(Simulation):
    id = "sine_wave"
    title = "Travelling wave"
    disclaimer = "A transverse wave on a string. The dot moves up and down only; the wave shape travels along."
    equations = ["y = A·sin(k·x − ω·t)", "v = f·λ", "k = 2π/λ,  ω = 2π·f"]
    PARAMETERS = [Parameter("amplitude", 0.5, 0.05, 1.5, "m", "amplitude A"),
                  Parameter("wavelength", 2.0, 0.5, 8.0, "m", "wavelength λ"),
                  Parameter("frequency", 0.5, 0.05, 5.0, "Hz", "frequency f")]
    ALIASES = {"a": "amplitude", "lambda": "wavelength", "f": "frequency"}
    loop = True
    duration = 40.0
    LENGTH = 10.0

    def y(self, x, t):
        k, w = 2 * np.pi / self.p("wavelength"), 2 * np.pi * self.p("frequency")
        return self.p("amplitude") * np.sin(k * x - w * t)

    def string(self, t):
        xs = np.linspace(0, self.LENGTH, 300)
        return np.column_stack([xs, np.zeros_like(xs), self.y(xs, t)])

    def build_parts(self):
        return {"axis": ScenePart("axis", "Rest position", polyline_tube([(0, 0, 0), (self.LENGTH, 0, 0)], 0.01), "#8899aa", opacity=0.6),
                "string": ScenePart("string", "Wave", polyline_tube(self.string(0), 0.04), "#4fb0ff"),
                "marker": ScenePart("marker", "One point on the string", pv.Sphere(radius=0.12, center=(self.LENGTH / 2, 0, self.y(self.LENGTH / 2, 0))), "#ff6b4a")}

    def apply(self, engine):
        engine.update_part_mesh("string", polyline_tube(self.string(self.t), 0.04))
        engine.move_part("marker", (0, 0, self.y(self.LENGTH / 2, self.t) - self.y(self.LENGTH / 2, 0)))

    def measurements(self):
        return {"wave_speed": fmt(self.p("frequency") * self.p("wavelength"), "m/s"), "period": fmt(1 / self.p("frequency"), "s")}


class StandingWave(Simulation):
    id = "standing_wave"
    title = "Standing wave on a string"
    disclaimer = "Fixed at both ends. Yellow dots mark nodes (no motion); the loops between them are antinodes."
    equations = ["y = 2A·sin(n·π·x / L)·cos(ω·t)", "λ = 2L / n", "f = n·v / (2L)"]
    PARAMETERS = [Parameter("harmonic", 3, 1, 8, "", "harmonic number n (number of loops)"),
                  Parameter("amplitude", 0.4, 0.05, 1.0, "m", "amplitude of each travelling wave"),
                  Parameter("length", 6.0, 1.0, 10.0, "m", "string length L"),
                  Parameter("wave_speed", 10.0, 1.0, 50.0, "m/s", "wave speed v")]
    ALIASES = {"n": "harmonic", "mode": "harmonic", "l": "length", "v": "wave_speed", "speed": "wave_speed"}
    loop = True
    duration = 60.0

    def set_parameter(self, name, value):
        key = super().set_parameter(name, value)
        if key == "harmonic":
            self.params[key].value = float(round(self.params[key].value))
        return key

    def string(self, t):
        n, L, A, v = int(self.p("harmonic")), self.p("length"), self.p("amplitude"), self.p("wave_speed")
        w = n * np.pi * v / L
        xs = np.linspace(0, L, 300)
        return np.column_stack([xs, np.zeros_like(xs), 2 * A * np.sin(n * np.pi * xs / L) * np.cos(w * t)])

    def build_parts(self):
        n, L = int(self.p("harmonic")), self.p("length")
        nodes = [pv.Sphere(radius=0.08, center=(k * L / n, 0, 0)) for k in range(n + 1)]
        return {"supports": ScenePart("supports", "Fixed ends", pv.merge([pv.Box((-0.1, 0, -0.3, 0.3, -1, 1)), pv.Box((L, L + 0.1, -0.3, 0.3, -1, 1))]), "#707880"),
                "string": ScenePart("string", "String", polyline_tube(self.string(0), 0.03), "#4fb0ff"),
                "nodes": ScenePart("nodes", "Nodes", pv.merge(nodes), "#ffd84d")}

    def apply(self, engine):
        engine.update_part_mesh("string", polyline_tube(self.string(self.t), 0.03))

    def measurements(self):
        n, L, v = int(self.p("harmonic")), self.p("length"), self.p("wave_speed")
        return {"wavelength": fmt(2 * L / n, "m"), "frequency": fmt(n * v / (2 * L), "Hz"), "nodes": str(n + 1), "antinodes": str(n)}


class Interference(Simulation):
    id = "interference"
    title = "Interference of waves from two sources"
    front = "+z"
    disclaimer = "Ripples from two sources in step. Bright bands: constructive interference; flat bands: destructive."
    equations = ["path difference = n·λ  → constructive", "path difference = (n + ½)·λ  → destructive"]
    PARAMETERS = [Parameter("separation", 3.0, 0.5, 8.0, "m", "distance between the sources"),
                  Parameter("wavelength", 1.0, 0.3, 4.0, "m", "wavelength λ"),
                  Parameter("frequency", 1.0, 0.1, 3.0, "Hz", "frequency f")]
    ALIASES = {"d": "separation", "lambda": "wavelength", "f": "frequency"}
    loop = True
    duration = 30.0
    SIZE, RES = 16.0, 110

    def sources(self):
        d = self.p("separation")
        return [np.array([-d / 2, 0.0]), np.array([d / 2, 0.0])]

    def height(self, t):
        xs = np.linspace(-self.SIZE / 2, self.SIZE / 2, self.RES)
        X, Y = np.meshgrid(xs, np.linspace(0, self.SIZE, self.RES))
        k, w = 2 * np.pi / self.p("wavelength"), 2 * np.pi * self.p("frequency")
        z = np.zeros_like(X)
        srcs = self.sources()
        for s in srcs:
            r = np.hypot(X - s[0], Y - s[1])
            z += np.sin(k * r - w * t) / np.sqrt(len(srcs))
        return X, Y, 0.25 * z

    def surface(self, t):
        X, Y, Z = self.height(t)
        grid = pv.StructuredGrid(X, Y, Z).extract_surface(algorithm="dataset_surface")
        grid["height"] = Z.ravel(order="F")
        return grid

    def build_parts(self):
        dots = [pv.Sphere(radius=0.18, center=(s[0], s[1], 0.3)) for s in self.sources()]
        return {"water": ScenePart("water", "Wave surface", self.surface(0), "#4fb0ff",
                                   style={"scalars": "height", "cmap": "coolwarm", "show_scalar_bar": False, "clim": [-0.5, 0.5]}),
                "sources": ScenePart("sources", "Sources", pv.merge(dots), "#ffd84d")}

    def apply(self, engine):
        engine.update_part_mesh("water", self.surface(self.t))

    def measurements(self):
        d, lam = self.p("separation"), self.p("wavelength")
        orders = int(d // lam)
        return {"wave_speed": fmt(lam * self.p("frequency"), "m/s"),
                "bright_directions_each_side": str(orders),
                "first_bright_angle": (fmt(np.degrees(np.arcsin(lam / d)), "°", 1) if lam <= d else "none")}


class Diffraction(Interference):
    id = "diffraction"
    title = "Diffraction through a gap"
    disclaimer = ("Huygens' principle: every point in the gap acts as a source. A gap about the size of the "
                  "wavelength spreads the wave out most.")
    equations = ["first minimum of a single slit: sinθ = λ / a"]
    PARAMETERS = [Parameter("gap_width", 2.0, 0.3, 8.0, "m", "width of the gap a"),
                  Parameter("wavelength", 1.0, 0.3, 4.0, "m", "wavelength λ"),
                  Parameter("frequency", 1.0, 0.1, 3.0, "Hz", "frequency f")]
    ALIASES = {"a": "gap_width", "gap": "gap_width", "slit_width": "gap_width", "lambda": "wavelength"}

    def sources(self):
        a = self.p("gap_width")
        return [np.array([x, 0.0]) for x in np.linspace(-a / 2, a / 2, max(3, int(a / 0.15)))]

    def build_parts(self):
        a = self.p("gap_width")
        barrier = pv.merge([pv.Box((-self.SIZE / 2, -a / 2, -0.15, 0.0, -0.1, 0.6)),
                            pv.Box((a / 2, self.SIZE / 2, -0.15, 0.0, -0.1, 0.6))])
        return {"water": ScenePart("water", "Wave surface", self.surface(0), "#4fb0ff",
                                   style={"scalars": "height", "cmap": "coolwarm", "show_scalar_bar": False, "clim": [-0.5, 0.5]}),
                "barrier": ScenePart("barrier", "Barrier with a gap", barrier, "#707880")}

    def measurements(self):
        a, lam = self.p("gap_width"), self.p("wavelength")
        return {"first_minimum_angle": fmt(np.degrees(np.arcsin(lam / a)), "°", 1) if lam < a else "no minimum: the wave spreads everywhere"}


class Refraction(Simulation):
    id = "refraction"
    title = "Refraction of light"
    front = "-y"
    disclaimer = "A single ray. Part of the light is always reflected; above the critical angle, all of it is."
    equations = ["n1·sinθ1 = n2·sinθ2  (Snell's law)", "critical angle: sinθc = n2 / n1  (when n1 > n2)",
                 "angle of reflection = angle of incidence"]
    PARAMETERS = [Parameter("n1", 1.0, 1.0, 2.5, "", "refractive index of the upper medium (air ≈ 1.00)"),
                  Parameter("n2", 1.5, 1.0, 2.5, "", "refractive index of the lower medium (glass ≈ 1.5, water ≈ 1.33)"),
                  Parameter("angle", 40.0, 0.0, 89.0, "°", "angle of incidence, from the normal")]
    ALIASES = {"incidence": "angle", "angle_of_incidence": "angle", "incident_angle": "angle", "theta": "angle"}

    def refracted_angle(self):
        s = self.p("n1") * np.sin(np.radians(self.p("angle"))) / self.p("n2")
        return None if s > 1 else float(np.degrees(np.arcsin(s)))

    def build_parts(self):
        th = np.radians(self.p("angle"))
        L = 2.0
        inc = np.array([-np.sin(th), 0, np.cos(th)]) * L
        parts = {"upper": ScenePart("upper", f"Medium 1 (n = {self.p('n1'):.2f})", pv.Box((-2.5, 2.5, -0.6, 0.6, 0, 2.2)), "#9fc6e8", opacity=0.08),
                 "lower": ScenePart("lower", f"Medium 2 (n = {self.p('n2'):.2f})", pv.Box((-2.5, 2.5, -0.6, 0.6, -2.2, 0)), "#4f90c0", opacity=0.3),
                 "normal": ScenePart("normal", "Normal", polyline_tube([(0, 0, -2.1), (0, 0, 2.1)], 0.01), "#d0d0d0", opacity=0.7),
                 "incident": ScenePart("incident", "Incident ray", arrow(inc, -inc, 0.025, 0.08), "#ffd84d"),
                 "reflected": ScenePart("reflected", "Reflected ray", arrow((0, 0, 0), np.array([np.sin(th), 0, np.cos(th)]) * L, 0.02, 0.07), "#ff9f43",
                                        opacity=1.0 if self.refracted_angle() is None else 0.45)}
        r = self.refracted_angle()
        if r is not None:
            rr = np.radians(r)
            parts["refracted"] = ScenePart("refracted", "Refracted ray", arrow((0, 0, 0), np.array([np.sin(rr), 0, -np.cos(rr)]) * L, 0.025, 0.08), "#ff6b4a")
        return parts

    def measurements(self):
        r = self.refracted_angle()
        out = {"angle_of_incidence": fmt(self.p("angle"), "°", 1), "angle_of_reflection": fmt(self.p("angle"), "°", 1),
               "angle_of_refraction": fmt(r, "°", 1) if r is not None else "none: total internal reflection"}
        if self.p("n1") > self.p("n2"):
            out["critical_angle"] = fmt(np.degrees(np.arcsin(self.p("n2") / self.p("n1"))), "°", 1)
        return out


class Reflection(Simulation):
    id = "reflection"
    title = "Reflection in a plane mirror"
    front = "-y"
    disclaimer = "Angles are measured from the normal, the line at right angles to the mirror."
    equations = ["angle of reflection = angle of incidence", "incident ray, reflected ray and normal lie in one plane"]
    PARAMETERS = [Parameter("angle", 35.0, 0.0, 89.0, "°", "angle of incidence, from the normal")]
    ALIASES = {"incidence": "angle", "angle_of_incidence": "angle", "theta": "angle"}

    def build_parts(self):
        th = np.radians(self.p("angle"))
        inc = np.array([-np.sin(th), 0, np.cos(th)]) * 2.0
        return {"mirror": ScenePart("mirror", "Mirror", pv.Box((-2.5, 2.5, -0.6, 0.6, -0.12, 0)), "#c8d8e8"),
                "normal": ScenePart("normal", "Normal", polyline_tube([(0, 0, 0), (0, 0, 2.2)], 0.01), "#d0d0d0", opacity=0.7),
                "incident": ScenePart("incident", "Incident ray", arrow(inc, -inc, 0.025, 0.08), "#ffd84d"),
                "reflected": ScenePart("reflected", "Reflected ray", arrow((0, 0, 0), np.array([np.sin(th), 0, np.cos(th)]) * 2.0, 0.025, 0.08), "#ff9f43")}

    def measurements(self):
        return {"angle_of_incidence": fmt(self.p("angle"), "°", 1), "angle_of_reflection": fmt(self.p("angle"), "°", 1)}
