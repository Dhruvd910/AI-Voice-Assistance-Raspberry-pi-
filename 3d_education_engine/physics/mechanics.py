"""Mechanics: projectile, free fall, pendulum, spring, circular motion, collisions.

Coordinates: x horizontal, z up, looking from -y. SI units throughout. Air
resistance is ignored everywhere and every scene says so.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv
from scipy.integrate import solve_ivp

from physics.base import Parameter, Simulation, arrow, fmt, ground, polyline_tube
from visualization.scene import ScenePart

NO_DRAG = "No air resistance. "


# ======================================================================
class ProjectileMotion(Simulation):
    id = "projectile_motion"
    title = "Projectile motion"
    disclaimer = NO_DRAG + "Arrows: velocity (blue) and its horizontal and vertical components."
    equations = ["x = v0·cosθ·t", "z = h + v0·sinθ·t − ½·g·t²",
                 "time of flight T = (v0·sinθ + √((v0·sinθ)² + 2gh)) / g",
                 "range R = v0·cosθ·T  (= v0²·sin2θ / g when h = 0)",
                 "maximum height H = h + (v0·sinθ)² / (2g)"]
    PARAMETERS = [
        Parameter("initial_velocity", 20.0, 1.0, 100.0, "m/s", "launch speed"),
        Parameter("angle", 45.0, 0.0, 90.0, "°", "launch angle above the horizontal", 1.0),
        Parameter("gravity", 9.81, 0.1, 30.0, "m/s²", "gravitational field strength"),
        Parameter("height", 0.0, 0.0, 50.0, "m", "launch height"),
    ]
    ALIASES = {"launch_angle": "angle", "theta": "angle", "speed": "initial_velocity", "velocity": "initial_velocity",
               "v0": "initial_velocity", "launch_speed": "initial_velocity", "g": "gravity", "launch_height": "height"}

    def prepare(self) -> None:
        v, th, g, h = self.p("initial_velocity"), np.radians(self.p("angle")), self.p("gravity"), self.p("height")
        self.vx, self.vz0 = v * np.cos(th), v * np.sin(th)
        self.T = (self.vz0 + np.sqrt(self.vz0 ** 2 + 2 * g * h)) / g
        self.R = self.vx * self.T
        self.H = h + self.vz0 ** 2 / (2 * g)
        self.duration = self.T
        self.size = max(self.R, self.H, 1.0)
        # Arrow length per m/s: the launch velocity is drawn as 20% of the scene.
        self.k = 0.2 * self.size / max(v, 1e-6)

    def position(self, t: float) -> np.ndarray:
        t = min(t, self.T)
        return np.array([self.vx * t, 0.0, self.p("height") + self.vz0 * t - 0.5 * self.p("gravity") * t * t])

    def velocity(self, t: float) -> np.ndarray:
        return np.array([self.vx, 0.0, self.vz0 - self.p("gravity") * min(t, self.T)])

    def build_parts(self):
        ts = np.linspace(0, self.T, 120)
        path = np.array([self.position(t) for t in ts])
        r = 0.015 * self.size
        parts = {
            "ground": ScenePart("ground", "Ground", ground(-0.1 * self.size, self.R + 0.1 * self.size, 0.3 * self.size), "#3c5a3c"),
            "trajectory": ScenePart("trajectory", "Predicted path", polyline_tube(path, r * 0.25), "#8899aa", opacity=0.6),
            "trail": ScenePart("trail", "Path so far", polyline_tube(path[:2], r * 0.5), "#ffd84d"),
            "projectile": ScenePart("projectile", "Projectile", pv.Sphere(radius=r * 2, center=self.position(0)), "#ff6b4a"),
            "velocity": ScenePart("velocity", "Velocity", arrow(self.position(0), self.velocity(0) * self.k, r * 0.5, r * 1.3), "#4fb0ff"),
            "vx": ScenePart("vx", "Horizontal velocity (constant)", arrow(self.position(0), [self.vx * self.k, 0, 0], r * 0.35, r), "#6fd08c"),
            "vz": ScenePart("vz", "Vertical velocity (changes)", arrow(self.position(0), [0, 0, self.vz0 * self.k], r * 0.35, r), "#c080ff"),
        }
        if self.p("height") > 0:
            parts["launch_platform"] = ScenePart("launch_platform", "Launch platform",
                                                 pv.Box((-0.04 * self.size, 0.0, -0.05 * self.size, 0.05 * self.size, 0.0, self.p("height"))),
                                                 "#707880")
        return parts

    def apply(self, engine) -> None:
        pos, vel = self.position(self.t), self.velocity(self.t)
        k = self.k
        r = 0.015 * self.size
        engine.move_part("projectile", pos - self.position(0))
        ts = np.linspace(0, min(self.t, self.T), max(2, int(60 * self.t / max(self.T, 1e-6)) + 2))
        engine.update_part_mesh("trail", polyline_tube(np.array([self.position(t) for t in ts]), r * 0.5))
        engine.update_part_mesh("velocity", arrow(pos, vel * k, r * 0.5, r * 1.3))
        engine.update_part_mesh("vx", arrow(pos, [vel[0] * k, 0, 0], r * 0.35, r))
        engine.update_part_mesh("vz", arrow(pos, [0, 0, vel[2] * k], r * 0.35, r))

    def measurements(self):
        pos, vel = self.position(self.t), self.velocity(self.t)
        return {"time_of_flight": fmt(self.T, "s"), "range": fmt(self.R, "m"), "maximum_height": fmt(self.H, "m"),
                "time": fmt(min(self.t, self.T), "s"), "x": fmt(pos[0], "m"), "height_now": fmt(pos[2], "m"),
                "speed_now": fmt(float(np.linalg.norm(vel)), "m/s")}


# ======================================================================
class FreeFall(Simulation):
    id = "free_fall"
    title = "Free fall"
    disclaimer = NO_DRAG + "Ghost balls show the position every 0.2 s: the gaps grow because the ball speeds up."
    equations = ["z = h − ½·g·t²", "v = g·t", "time to fall t = √(2h / g)", "impact speed v = √(2gh)"]
    PARAMETERS = [Parameter("height", 20.0, 1.0, 100.0, "m", "drop height"),
                  Parameter("gravity", 9.81, 0.1, 30.0, "m/s²", "gravitational field strength")]
    ALIASES = {"g": "gravity", "drop_height": "height", "h": "height"}

    def prepare(self):
        self.T = np.sqrt(2 * self.p("height") / self.p("gravity"))
        self.duration = self.T

    def z(self, t):
        return self.p("height") - 0.5 * self.p("gravity") * min(t, self.T) ** 2

    def build_parts(self):
        h = self.p("height")
        r = 0.03 * h
        ruler = [pv.Box((-0.12 * h, -0.1 * h, -0.01 * h, 0.01 * h, 0, h))]
        for m in np.arange(0, h + 1e-9, 5.0 if h >= 20 else 1.0):
            ruler.append(pv.Box((-0.16 * h, -0.1 * h, -0.01 * h, 0.01 * h, m - 0.002 * h, m + 0.002 * h)))
        ghosts = [pv.Sphere(radius=r * 0.7, center=(0.12 * h, 0, self.z(t))) for t in np.arange(0, self.T, 0.2)]
        return {
            "ground": ScenePart("ground", "Ground", ground(-0.4 * h, 0.4 * h, 0.4 * h), "#3c5a3c"),
            "ruler": ScenePart("ruler", "Height scale", pv.merge(ruler), "#c0c8d0"),
            "strobe": ScenePart("strobe", "Positions every 0.2 s", pv.merge(ghosts) if len(ghosts) > 1 else ghosts[0], "#ffd84d", opacity=0.45),
            "ball": ScenePart("ball", "Ball", pv.Sphere(radius=r, center=(0, 0, h)), "#ff6b4a"),
        }

    def apply(self, engine):
        engine.move_part("ball", (0, 0, self.z(self.t) - self.p("height")))

    def measurements(self):
        t = min(self.t, self.T)
        return {"time_to_fall": fmt(self.T, "s"), "impact_speed": fmt(np.sqrt(2 * self.p("gravity") * self.p("height")), "m/s"),
                "time": fmt(t, "s"), "height_now": fmt(self.z(t), "m"), "speed_now": fmt(self.p("gravity") * t, "m/s")}


# ======================================================================
class Pendulum(Simulation):
    id = "pendulum"
    title = "Simple pendulum"
    disclaimer = "No friction. Solved exactly (not the small-angle approximation), so large swings are slower."
    equations = ["d²θ/dt² = −(g/L)·sinθ", "small-angle period T ≈ 2π·√(L/g)"]
    PARAMETERS = [Parameter("length", 2.0, 0.1, 10.0, "m", "string length", 0.1),
                  Parameter("amplitude", 30.0, 1.0, 170.0, "°", "starting angle from vertical", 1.0),
                  Parameter("gravity", 9.81, 0.1, 30.0, "m/s²", "gravitational field strength")]
    ALIASES = {"angle": "amplitude", "starting_angle": "amplitude", "g": "gravity", "l": "length", "string_length": "length"}
    loop = True

    def prepare(self):
        L, g, th0 = self.p("length"), self.p("gravity"), np.radians(self.p("amplitude"))
        self.T_small = 2 * np.pi * np.sqrt(L / g)
        self.duration = 4 * self.T_small * (1 + th0 ** 2 / 16) + 1.0
        sol = solve_ivp(lambda t, y: [y[1], -(g / L) * np.sin(y[0])], (0, self.duration), [th0, 0.0],
                        dense_output=True, rtol=1e-9, atol=1e-9, max_step=self.T_small / 50)
        self.sol = sol
        ts = np.linspace(0, self.duration, 4000)
        th = sol.sol(ts)[0]
        crossings = ts[1:][(np.sign(th[:-1]) != np.sign(th[1:]))]
        self.T_actual = 2 * float(np.mean(np.diff(crossings))) if len(crossings) >= 2 else self.T_small
        # Wrap round on a whole number of real swings, so going round again
        # does not jerk the bob back to where it started mid-swing.
        swings = max(1, int((self.duration - 0.5) // self.T_actual))
        self.duration = swings * self.T_actual

    def angle(self, t):
        return float(self.sol.sol(t % self.duration)[0])

    def bob(self, t):
        L, th = self.p("length"), self.angle(t)
        return np.array([L * np.sin(th), 0.0, -L * np.cos(th)])

    def build_parts(self):
        L = self.p("length")
        th0 = np.radians(self.p("amplitude"))
        arc = [np.array([L * np.sin(a), 0, -L * np.cos(a)]) for a in np.linspace(-th0, th0, 60)]
        return {
            "support": ScenePart("support", "Support", pv.Box((-0.3 * L, 0.3 * L, -0.05 * L, 0.05 * L, 0, 0.04 * L)), "#707880"),
            "swing_arc": ScenePart("swing_arc", "Swing", polyline_tube(arc, 0.004 * L), "#8899aa", opacity=0.6),
            "string": ScenePart("string", "String", polyline_tube([(0, 0, 0), self.bob(0)], 0.006 * L), "#d0d0d0"),
            "bob": ScenePart("bob", "Bob", pv.Sphere(radius=0.06 * L, center=self.bob(0)), "#ff6b4a"),
        }

    def apply(self, engine):
        engine.move_part("bob", self.bob(self.t) - self.bob(0))
        engine.update_part_mesh("string", polyline_tube([(0, 0, 0), self.bob(self.t)], 0.006 * self.p("length")))

    def measurements(self):
        return {"period_small_angle": fmt(self.T_small, "s"), "period_actual": fmt(self.T_actual, "s"),
                "angle_now": fmt(np.degrees(self.angle(self.t)), "°", 1), "time": fmt(self.t, "s")}


# ======================================================================
class Spring(Simulation):
    id = "spring"
    title = "Mass on a spring"
    disclaimer = "No friction unless damping is set. The coil is drawn, not simulated."
    equations = ["F = −k·x  (Hooke's law)", "x = A·cos(ω·t),  ω = √(k/m)", "period T = 2π·√(m/k)"]
    PARAMETERS = [Parameter("mass", 1.0, 0.1, 10.0, "kg", "mass of the block"),
                  Parameter("spring_constant", 20.0, 1.0, 500.0, "N/m", "stiffness k"),
                  Parameter("amplitude", 0.3, 0.05, 1.0, "m", "starting stretch"),
                  Parameter("damping", 0.0, 0.0, 5.0, "kg/s", "damping coefficient b")]
    ALIASES = {"k": "spring_constant", "stiffness": "spring_constant", "m": "mass", "a": "amplitude"}
    loop = True
    REST = 1.0

    def prepare(self):
        m, k, b, A = self.p("mass"), self.p("spring_constant"), self.p("damping"), self.p("amplitude")
        self.omega = np.sqrt(k / m)
        self.T = 2 * np.pi / self.omega
        self.duration = 6 * self.T
        self.sol = solve_ivp(lambda t, y: [y[1], (-k * y[0] - b * y[1]) / m], (0, self.duration), [A, 0.0],
                             dense_output=True, rtol=1e-8, atol=1e-10, max_step=self.T / 60)

    def x(self, t):
        return float(self.sol.sol(t % self.duration)[0])

    def coil(self, x):
        end = self.REST + x
        s = np.linspace(0, 1, 300)
        return np.column_stack([s * end, 0.08 * np.sin(2 * np.pi * 12 * s) * (s > 0.05) * (s < 0.95),
                                0.08 * np.cos(2 * np.pi * 12 * s) * (s > 0.05) * (s < 0.95)])

    def build_parts(self):
        return {
            "wall": ScenePart("wall", "Wall", pv.Box((-0.06, 0.0, -0.3, 0.3, -0.3, 0.3)), "#707880"),
            "floor": ScenePart("floor", "Frictionless surface", pv.Box((-0.06, 2.6, -0.3, 0.3, -0.36, -0.3)), "#3c4a5a"),
            "spring": ScenePart("spring", "Spring", polyline_tube(self.coil(self.x(0)), 0.012), "#c0c8d0"),
            "block": ScenePart("block", "Mass", pv.Box((self.REST + self.x(0), self.REST + self.x(0) + 0.3, -0.15, 0.15, -0.15, 0.15)), "#ff6b4a"),
            "equilibrium": ScenePart("equilibrium", "Equilibrium position", polyline_tube([(self.REST, 0, -0.3), (self.REST, 0, 0.35)], 0.005), "#ffd84d"),
        }

    def apply(self, engine):
        engine.move_part("block", (self.x(self.t) - self.x(0), 0, 0))
        engine.update_part_mesh("spring", polyline_tube(self.coil(self.x(self.t)), 0.012))

    def measurements(self):
        return {"period": fmt(self.T, "s"), "angular_frequency": fmt(self.omega, "rad/s"),
                "max_speed_undamped": fmt(self.p("amplitude") * self.omega, "m/s"),
                "displacement_now": fmt(self.x(self.t), "m", 3),
                "force_now": fmt(-self.p("spring_constant") * self.x(self.t), "N")}


# ======================================================================
class CircularMotion(Simulation):
    id = "circular_motion"
    title = "Uniform circular motion"
    front = "+z"
    disclaimer = "The velocity (blue) is along the circle; the acceleration (orange) points to the centre."
    equations = ["v = ω·r", "a = v² / r  (towards the centre)", "F = m·v² / r", "period T = 2π·r / v"]
    PARAMETERS = [Parameter("radius", 3.0, 0.5, 10.0, "m", "radius of the circle"),
                  Parameter("speed", 5.0, 0.1, 30.0, "m/s", "constant speed"),
                  Parameter("mass", 1.0, 0.1, 10.0, "kg", "mass of the object")]
    ALIASES = {"r": "radius", "v": "speed", "velocity": "speed", "m": "mass"}
    loop = True

    def prepare(self):
        self.omega = self.p("speed") / self.p("radius")
        self.T = 2 * np.pi / self.omega
        self.duration = self.T * 4

    def pos(self, t):
        a, r = self.omega * t, self.p("radius")
        return np.array([r * np.cos(a), r * np.sin(a), 0.0])

    def build_parts(self):
        r = self.p("radius")
        circle = [(r * np.cos(a), r * np.sin(a), 0) for a in np.linspace(0, 2 * np.pi, 120)]
        return {"path": ScenePart("path", "Circular path", polyline_tube(circle, 0.01 * r), "#8899aa", opacity=0.6),
                "centre": ScenePart("centre", "Centre", pv.Sphere(radius=0.04 * r), "#ffffff"),
                "object": ScenePart("object", "Object", pv.Sphere(radius=0.08 * r, center=self.pos(0)), "#ff6b4a"),
                "velocity": ScenePart("velocity", "Velocity", arrow(self.pos(0), [0, 0.4 * r, 0], 0.012 * r, 0.035 * r), "#4fb0ff"),
                "acceleration": ScenePart("acceleration", "Centripetal acceleration", arrow(self.pos(0), [-0.4 * r, 0, 0], 0.012 * r, 0.035 * r), "#ff9f43")}

    def apply(self, engine):
        r, p = self.p("radius"), self.pos(self.t)
        tangent = np.array([-p[1], p[0], 0]) / r
        engine.move_part("object", p - self.pos(0))
        engine.update_part_mesh("velocity", arrow(p, tangent * 0.4 * r, 0.012 * r, 0.035 * r))
        engine.update_part_mesh("acceleration", arrow(p, -p / r * 0.4 * r, 0.012 * r, 0.035 * r))

    def measurements(self):
        v, r, m = self.p("speed"), self.p("radius"), self.p("mass")
        return {"angular_velocity": fmt(self.omega, "rad/s"), "period": fmt(self.T, "s"),
                "centripetal_acceleration": fmt(v * v / r, "m/s²"), "centripetal_force": fmt(m * v * v / r, "N")}


# ======================================================================
class Collision(Simulation):
    id = "collision"
    title = "Collision on a track (conservation of momentum)"
    disclaimer = "Frictionless track. Elasticity 1 = elastic (kinetic energy kept), 0 = the carts stick together."
    equations = ["total momentum m1·u1 + m2·u2 = m1·v1 + m2·v2",
                 "v1 = (m1·u1 + m2·u2 + m2·e·(u2 − u1)) / (m1 + m2)",
                 "v2 = (m1·u1 + m2·u2 + m1·e·(u1 − u2)) / (m1 + m2)"]
    PARAMETERS = [Parameter("mass1", 1.0, 0.1, 10.0, "kg", "mass of cart 1 (left)"),
                  Parameter("mass2", 1.0, 0.1, 10.0, "kg", "mass of cart 2 (right)"),
                  Parameter("velocity1", 2.0, -5.0, 5.0, "m/s", "starting velocity of cart 1"),
                  Parameter("velocity2", 0.0, -5.0, 5.0, "m/s", "starting velocity of cart 2"),
                  Parameter("elasticity", 1.0, 0.0, 1.0, "", "coefficient of restitution e")]
    ALIASES = {"m1": "mass1", "m2": "mass2", "u1": "velocity1", "u2": "velocity2", "e": "elasticity",
               "coefficient_of_restitution": "elasticity"}
    W = 0.4

    def prepare(self):
        m1, m2, u1, u2, e = (self.p(k) for k in ("mass1", "mass2", "velocity1", "velocity2", "elasticity"))
        self.x1, self.x2 = -2.0, 2.0
        closing = u1 - u2
        self.t_hit = (self.x2 - self.x1 - self.W) / closing if closing > 0 else None
        p = m1 * u1 + m2 * u2
        self.v1 = (p + m2 * e * (u2 - u1)) / (m1 + m2)
        self.v2 = (p + m1 * e * (u1 - u2)) / (m1 + m2)
        self.duration = (self.t_hit or 3.0) + 3.0

    def positions(self, t):
        u1, u2 = self.p("velocity1"), self.p("velocity2")
        if self.t_hit is None or t <= self.t_hit:
            return self.x1 + u1 * t, self.x2 + u2 * t
        h = self.t_hit
        return (self.x1 + u1 * h + self.v1 * (t - h), self.x2 + u2 * h + self.v2 * (t - h))

    def build_parts(self):
        s1, s2 = (0.2 + 0.08 * self.p("mass1")), (0.2 + 0.08 * self.p("mass2"))
        return {"track": ScenePart("track", "Track", pv.Box((-6, 6, -0.3, 0.3, -0.05, 0.0)), "#3c4a5a"),
                "cart1": ScenePart("cart1", "Cart 1", pv.Box((self.x1 - self.W / 2, self.x1 + self.W / 2, -0.2, 0.2, 0.0, s1)), "#4fb0ff"),
                "cart2": ScenePart("cart2", "Cart 2", pv.Box((self.x2 - self.W / 2, self.x2 + self.W / 2, -0.2, 0.2, 0.0, s2)), "#ff6b4a")}

    def apply(self, engine):
        a, b = self.positions(self.t)
        engine.move_part("cart1", (a - self.x1, 0, 0))
        engine.move_part("cart2", (b - self.x2, 0, 0))

    def measurements(self):
        m1, m2, u1, u2 = (self.p(k) for k in ("mass1", "mass2", "velocity1", "velocity2"))
        ke0 = 0.5 * m1 * u1 ** 2 + 0.5 * m2 * u2 ** 2
        ke1 = 0.5 * m1 * self.v1 ** 2 + 0.5 * m2 * self.v2 ** 2
        out = {"momentum_before": fmt(m1 * u1 + m2 * u2, "kg·m/s"), "kinetic_energy_before": fmt(ke0, "J")}
        if self.t_hit is None:
            out["collision"] = "the carts never meet (cart 1 is not catching cart 2)"
        else:
            out.update({"velocity1_after": fmt(self.v1, "m/s"), "velocity2_after": fmt(self.v2, "m/s"),
                        "momentum_after": fmt(m1 * self.v1 + m2 * self.v2, "kg·m/s"),
                        "kinetic_energy_after": fmt(ke1, "J")})
        return out


# ======================================================================
def _wedge(run: float, rise: float, half_width: float) -> pv.PolyData:
    """A ramp: a right-angled triangle (top at x = 0, foot at x = run) given depth."""
    w = half_width
    pts = np.array([(0, -w, 0), (run, -w, 0), (0, -w, rise), (0, w, 0), (run, w, 0), (0, w, rise)], float)
    faces = np.hstack([[3, 0, 1, 2], [3, 3, 5, 4], [4, 0, 3, 4, 1], [4, 1, 4, 5, 2], [4, 0, 2, 5, 3]])
    return pv.PolyData(pts, faces).triangulate()


class InclinedPlane(Simulation):
    id = "inclined_plane"
    title = "Block on an inclined plane"
    disclaimer = ("Arrows: weight (red), normal force (green), friction (orange), net force (white). "
                  "One friction coefficient for starting and sliding, to keep it simple.")
    equations = ["along the slope: a = g·(sinθ − μ·cosθ)", "normal force N = m·g·cosθ",
                 "friction f = μ·N", "it slides only if tanθ > μ"]
    PARAMETERS = [Parameter("angle", 30.0, 5.0, 60.0, "°", "angle of the slope", 1.0),
                  Parameter("friction", 0.2, 0.0, 1.0, "", "coefficient of friction μ", 0.05),
                  Parameter("mass", 2.0, 0.5, 10.0, "kg", "mass of the block", 0.5),
                  Parameter("gravity", 9.81, 1.0, 25.0, "m/s²", "gravitational field strength", 0.1)]
    ALIASES = {"theta": "angle", "slope": "angle", "incline": "angle", "mu": "friction",
               "coefficient_of_friction": "friction", "m": "mass", "g": "gravity"}
    LENGTH = 5.0
    BLOCK = 0.5

    def prepare(self):
        th, mu, g = np.radians(self.p("angle")), self.p("friction"), self.p("gravity")
        self.th = th
        self.slides = np.tan(th) > mu
        self.a = g * (np.sin(th) - mu * np.cos(th)) if self.slides else 0.0
        travel = self.LENGTH - self.BLOCK * 1.2
        self.travel = travel
        self.T = np.sqrt(2 * travel / self.a) if self.slides else 0.0
        self.duration = self.T if self.slides else 3.0
        self.down = np.array([np.cos(th), 0.0, -np.sin(th)])     # down the slope
        self.out = np.array([np.sin(th), 0.0, np.cos(th)])       # away from the surface
        self.top = np.array([0.0, 0.0, self.LENGTH * np.sin(th)])

    def s(self, t):
        return 0.5 * self.a * min(t, self.T) ** 2 if self.slides else 0.0

    def centre(self, t):
        return self.top + self.down * (0.15 + self.s(t) + self.BLOCK / 2) + self.out * (self.BLOCK / 2)

    def forces(self):
        m, g, mu = self.p("mass"), self.p("gravity"), self.p("friction")
        weight = np.array([0, 0, -m * g])
        normal = self.out * m * g * np.cos(self.th)
        along = m * g * np.sin(self.th)
        friction = -self.down * (mu * m * g * np.cos(self.th) if self.slides else along)
        return weight, normal, friction, weight + normal + friction

    def arrows(self, t):
        c = self.centre(t)
        k = 1.4 / (self.p("mass") * self.p("gravity"))
        weight, normal, friction, net = self.forces()
        return {"weight": arrow(c, weight * k, 0.03, 0.09), "normal": arrow(c, normal * k, 0.03, 0.09),
                "friction": arrow(c + self.out * 0.0, friction * k, 0.03, 0.09),
                "net": arrow(c, net * k, 0.03, 0.09)}

    def build_parts(self):
        run, rise = self.LENGTH * np.cos(self.th), self.LENGTH * np.sin(self.th)
        b = self.BLOCK / 2
        block = pv.Box((-b, b, -0.35, 0.35, -b, b)).rotate_y(self.p("angle"), inplace=False).translate(self.centre(0), inplace=False)
        a = self.arrows(0)
        return {"ground": ScenePart("ground", "Ground", ground(-0.5, run + 1.5, 1.6), "#3c5a3c"),
                "ramp": ScenePart("ramp", "Inclined plane", _wedge(run, rise, 0.5), "#8a7356"),
                "block": ScenePart("block", "Block", block, "#4fb0ff"),
                "weight": ScenePart("weight", "Weight m·g", a["weight"], "#ff5a5a"),
                "normal": ScenePart("normal", "Normal force", a["normal"], "#6fd08c"),
                "friction": ScenePart("friction", "Friction", a["friction"], "#ff9f43"),
                "net": ScenePart("net", "Net force", a["net"], "#ffffff")}

    def apply(self, engine):
        engine.move_part("block", self.centre(self.t) - self.centre(0))
        for pid, mesh in self.arrows(self.t).items():
            engine.update_part_mesh(pid, mesh)

    def measurements(self):
        m, g = self.p("mass"), self.p("gravity")
        weight, normal, friction, net = self.forces()
        out = {"normal_force": fmt(float(np.linalg.norm(normal)), "N"),
               "friction_force": fmt(float(np.linalg.norm(friction)), "N")}
        if self.slides:
            out.update({"acceleration": fmt(self.a, "m/s²"), "time_to_bottom": fmt(self.T, "s"),
                        "speed_at_bottom": fmt(self.a * self.T, "m/s")})
        else:
            out["result"] = "it does not slide: friction holds it (tanθ ≤ μ)"
        return out


# ======================================================================
class EnergyTrack(Simulation):
    id = "energy_track"
    title = "Conservation of energy on a track"
    disclaimer = ("No friction: the ball always comes back to the height it started from. Bars: kinetic "
                  "energy (red), potential energy (blue); together they never change.")
    equations = ["potential energy PE = m·g·h", "kinetic energy KE = ½·m·v²",
                 "KE + PE = constant (no friction)", "speed at the bottom v = √(2·g·h)"]
    PARAMETERS = [Parameter("height", 5.0, 0.5, 9.0, "m", "height the ball is let go from", 0.5),
                  Parameter("mass", 1.0, 0.1, 10.0, "kg", "mass of the ball", 0.1),
                  Parameter("gravity", 9.81, 1.0, 25.0, "m/s²", "gravitational field strength", 0.1)]
    ALIASES = {"h": "height", "release_height": "height", "m": "mass", "g": "gravity"}
    loop = True
    WIDTH = 4.0        # the track is z = x² / WIDTH

    def track_z(self, x):
        return x * x / self.WIDTH

    def prepare(self):
        g, W, h = self.p("gravity"), self.WIDTH, self.p("height")
        self.x0 = -np.sqrt(h * W)

        def rhs(_t, y):
            x, v = y
            f1, f2 = 2 * x / W, 2 / W
            return [v, -(g + f2 * v * v) * f1 / (1 + f1 * f1)]
        guess = 2 * np.pi * np.sqrt(W / (2 * g)) * 2.5
        sol = solve_ivp(rhs, (0, guess * 3), [self.x0, 0.0], dense_output=True, rtol=1e-9, atol=1e-9,
                        max_step=guess / 200)
        ts = np.linspace(0, guess * 3, 6000)
        xs = sol.sol(ts)[0]
        ups = ts[1:][(xs[:-1] < 0) & (xs[1:] >= 0)]
        self.period = float(np.mean(np.diff(ups))) if len(ups) >= 2 else guess
        self.sol = sol
        self.duration = self.period * max(1, int(guess * 3 * 0.9 // self.period))

    def x(self, t):
        return float(self.sol.sol(t % self.duration)[0])

    def ball(self, t):
        x = self.x(t)
        return np.array([x, 0.0, self.track_z(x) + 0.25])

    def energies(self, t):
        m, g = self.p("mass"), self.p("gravity")
        total = m * g * self.p("height")
        pe = m * g * self.track_z(self.x(t))
        return min(pe, total), max(0.0, total - pe), total

    def bars(self, t):
        pe, ke, total = self.energies(t)
        k = 9.0 / total             # the full bar is the total energy, whatever it is
        ke_bar = pv.Box((7.0, 7.8, -0.3, 0.3, 0.0, max(ke * k, 1e-3)))
        pe_bar = pv.Box((8.2, 9.0, -0.3, 0.3, 0.0, max(pe * k, 1e-3)))
        return ke_bar, pe_bar

    def build_parts(self):
        xs = np.linspace(-6.2, 6.2, 200)
        track = np.column_stack([xs, np.zeros_like(xs), self.track_z(xs)])
        ke, pe = self.bars(0)
        return {"track": ScenePart("track", "Track", polyline_tube(track, 0.08), "#c0c8d0"),
                "start": ScenePart("start", "Release height", polyline_tube([(-6.5, 0, self.p("height") + 0.25), (6.5, 0, self.p("height") + 0.25)], 0.02), "#ffd84d", opacity=0.5),
                "ball": ScenePart("ball", "Ball", pv.Sphere(radius=0.25, center=self.ball(0)), "#ff6b4a"),
                "frame": ScenePart("frame", "Total energy", pv.Box((6.9, 9.1, -0.32, 0.32, 0, 9.0)).extract_feature_edges().tube(radius=0.03), "#ffffff", opacity=0.6),
                "ke_bar": ScenePart("ke_bar", "Kinetic energy", ke, "#ff5a5a"),
                "pe_bar": ScenePart("pe_bar", "Potential energy", pe, "#4f90ff")}

    def apply(self, engine):
        engine.move_part("ball", self.ball(self.t) - self.ball(0))
        ke, pe = self.bars(self.t)
        engine.update_part_mesh("ke_bar", ke)
        engine.update_part_mesh("pe_bar", pe)

    def measurements(self):
        m, g, h = self.p("mass"), self.p("gravity"), self.p("height")
        pe, ke, total = self.energies(self.t)
        return {"total_energy": fmt(total, "J"), "speed_at_bottom": fmt(np.sqrt(2 * g * h), "m/s"),
                "period": fmt(self.period, "s"), "kinetic_energy_now": fmt(ke, "J"), "potential_energy_now": fmt(pe, "J")}
