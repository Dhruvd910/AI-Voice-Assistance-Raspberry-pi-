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
        Parameter("angle", 45.0, 0.0, 90.0, "°", "launch angle above the horizontal"),
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
    PARAMETERS = [Parameter("length", 2.0, 0.1, 10.0, "m", "string length"),
                  Parameter("amplitude", 30.0, 1.0, 170.0, "°", "starting angle from vertical"),
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
