"""Gravity and orbits.

Units are astronomical units and years, in which G·M(Sun) = 4π², so the
numbers a student sees are the familiar ones (Earth: 1 AU, 1 year).
"""

from __future__ import annotations

import numpy as np
import pyvista as pv
from scipy.integrate import solve_ivp

from physics.base import Parameter, Simulation, arrow, fmt, polyline_tube
from visualization.scene import ScenePart

GM_SUN = 4 * np.pi ** 2          # AU³ / year²


class OrbitalMotion(Simulation):
    id = "orbital_motion"
    title = "Orbital motion under gravity"
    front = "+z"
    units = "AU"
    disclaimer = ("Two bodies, star fixed. Sizes not to scale. Blue: velocity; orange: the pull of gravity, "
                  "always towards the star.")
    equations = ["F = G·M·m / r²  (Newton's law of gravitation)",
                 "circular orbit speed v = √(G·M / r)",
                 "Kepler's third law: T² = a³ / M  (T in years, a in AU, M in solar masses)"]
    PARAMETERS = [Parameter("star_mass", 1.0, 0.1, 10.0, "solar masses", "mass of the central star"),
                  Parameter("distance", 1.0, 0.3, 5.0, "AU", "starting distance from the star"),
                  Parameter("speed_factor", 1.0, 0.5, 1.4, "× circular speed", "starting speed; 1 gives a circle, "
                            "above √2 ≈ 1.41 the planet escapes")]
    ALIASES = {"mass": "star_mass", "m": "star_mass", "r": "distance", "radius": "distance", "speed": "speed_factor",
               "velocity": "speed_factor"}
    loop = True

    def prepare(self):
        gm = GM_SUN * self.p("star_mass")
        r0 = self.p("distance")
        v0 = self.p("speed_factor") * np.sqrt(gm / r0)
        energy = 0.5 * v0 ** 2 - gm / r0
        self.a = -gm / (2 * energy)                       # semi-major axis (vis-viva)
        h = r0 * v0
        self.e = float(np.sqrt(max(0.0, 1 + 2 * energy * h ** 2 / gm ** 2)))
        self.T = float(np.sqrt(self.a ** 3 / self.p("star_mass")))
        self.duration = 2 * self.T

        def rhs(t, y):
            r = np.linalg.norm(y[:2])
            return [y[2], y[3], -gm * y[0] / r ** 3, -gm * y[1] / r ** 3]

        self.sol = solve_ivp(rhs, (0, self.duration), [r0, 0.0, 0.0, v0], dense_output=True,
                             rtol=1e-10, atol=1e-12, max_step=self.T / 400)
        self.gm = gm

    def state(self, t):
        y = self.sol.sol(t % self.duration)
        return np.array([y[0], y[1], 0.0]), np.array([y[2], y[3], 0.0])

    def build_parts(self):
        ts = np.linspace(0, self.T, 400)
        path = np.array([self.state(t)[0] for t in ts])
        scale = self.a * (1 + self.e)
        pos, _vel = self.state(0)
        return {"star": ScenePart("star", "Star", pv.Sphere(radius=0.08 * scale), "#ffd84d"),
                "orbit": ScenePart("orbit", "Orbit", polyline_tube(path, 0.006 * scale), "#8899aa", opacity=0.6),
                "planet": ScenePart("planet", "Planet", pv.Sphere(radius=0.04 * scale, center=pos), "#4fb0ff"),
                "velocity": ScenePart("velocity", "Velocity", pv.Sphere(radius=1e-4), "#6fa8ff"),
                "gravity_force": ScenePart("gravity_force", "Gravitational pull", pv.Sphere(radius=1e-4), "#ff9f43")}

    def apply(self, engine):
        pos, vel = self.state(self.t)
        scale = self.a * (1 + self.e)
        engine.move_part("planet", pos - self.state(0)[0])
        engine.update_part_mesh("velocity", arrow(pos, vel / np.linalg.norm(vel) * 0.3 * scale, 0.008 * scale, 0.025 * scale))
        engine.update_part_mesh("gravity_force", arrow(pos, -pos / np.linalg.norm(pos) * 0.3 * scale, 0.008 * scale, 0.025 * scale))

    def measurements(self):
        pos, vel = self.state(self.t)
        return {"period": fmt(self.T, "years"), "semi_major_axis": fmt(self.a, "AU"),
                "eccentricity": fmt(self.e, "", 3), "distance_now": fmt(float(np.linalg.norm(pos)), "AU"),
                "speed_now": fmt(float(np.linalg.norm(vel)) * 4.74, "km/s", 1)}


# Semi-major axis (AU) and orbital period (years): NASA planetary fact sheet values.
PLANETS = [("mercury", "Mercury", 0.387, 0.241, "#b0a090"), ("venus", "Venus", 0.723, 0.615, "#e8c070"),
           ("earth", "Earth", 1.000, 1.000, "#4f90ff"), ("mars", "Mars", 1.524, 1.881, "#d06040"),
           ("jupiter", "Jupiter", 5.203, 11.86, "#d8b080"), ("saturn", "Saturn", 9.537, 29.45, "#e8d090"),
           ("uranus", "Uranus", 19.19, 84.02, "#90d8e8"), ("neptune", "Neptune", 30.07, 164.8, "#5070e0")]


class SolarSystem(Simulation):
    id = "solar_system"
    title = "The Solar System"
    front = "+z"
    units = "AU (square-root scale)"
    disclaimer = ("Not to scale: distances compressed (square-root scale) and planets hugely enlarged. "
                  "Orbits drawn as circles; real ones are slightly elliptical. Periods are real.")
    equations = ["Kepler's third law: T² = a³  (T in years, a in AU)"]
    PARAMETERS = [Parameter("years_per_second", 1.0, 0.05, 20.0, "years/s", "how fast time runs")]
    ALIASES = {"speed": "years_per_second", "time_speed": "years_per_second"}
    loop = True
    duration = 165.0

    def advance(self, dt):
        super().advance(dt * self.p("years_per_second"))

    # Where each planet starts on its orbit: golden-angle steps, so they begin
    # spread round the Sun rather than in a line on one side of it, where
    # their names sat on top of each other.
    START = {pid: np.radians(137.5 * i) for i, (pid, *_rest) in enumerate(PLANETS)}

    def place(self, a, period, t, pid=None):
        r = 4.0 * np.sqrt(a)
        ang = 2 * np.pi * t / period + self.START.get(pid, 0.0)
        return np.array([r * np.cos(ang), r * np.sin(ang), 0.0])

    def build_parts(self):
        parts = {"sun": ScenePart("sun", "Sun", pv.Sphere(radius=1.2), "#ffd84d")}
        orbits = []
        for pid, name, a, period, color in PLANETS:
            r = 4.0 * np.sqrt(a)
            orbits.append(polyline_tube([(r * np.cos(u), r * np.sin(u), 0) for u in np.linspace(0, 2 * np.pi, 160)], 0.03))
            size = 0.35 if pid in ("jupiter", "saturn") else 0.22 if pid in ("uranus", "neptune") else 0.15
            parts[pid] = ScenePart(pid, name, pv.Sphere(radius=size, center=self.place(a, period, 0, pid)), color)
        parts["orbits"] = ScenePart("orbits", "Orbits", pv.merge(orbits), "#8899aa", opacity=0.5)
        return parts

    def apply(self, engine):
        for pid, _n, a, period, _c in PLANETS:
            engine.move_part(pid, self.place(a, period, self.t, pid) - self.place(a, period, 0, pid))

    def measurements(self):
        return {"years_elapsed": fmt(self.t, "years", 1),
                **{f"{pid}_period": fmt(period, "years", 3) for pid, _n, _a, period, _c in PLANETS}}
