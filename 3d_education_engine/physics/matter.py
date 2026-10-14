"""Matter: the particles in solids, liquids and gases; floating and sinking;
radioactive decay.

These move by stepping a state forward in time (particles bouncing, a block
settling in water) rather than by a formula of t, so each keeps its own state
and `reset` puts it back where it started.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv
from scipy.integrate import solve_ivp

from physics.base import Parameter, Simulation, arrow, beads, fmt, polyline_tube
from visualization.scene import ScenePart

MELT, BOIL = 273.0, 373.0        # water, in kelvin, at normal pressure


class StatesOfMatter(Simulation):
    id = "states_of_matter"
    title = "Particles in a solid, a liquid and a gas"
    disclaimer = ("Water molecules drawn as balls, hugely enlarged and slowed down. Heat them and they move "
                  "faster: they vibrate in place (ice), slide past each other (water), then fly apart (steam).")
    equations = ["average kinetic energy of the particles ∝ temperature (in kelvin)",
                 "water melts at 273 K (0 °C) and boils at 373 K (100 °C)"]
    PARAMETERS = [Parameter("temperature", 250.0, 100.0, 500.0, "K", "temperature (273 K = 0 °C)", 5.0)]
    ALIASES = {"temp": "temperature", "t": "temperature", "heat": "temperature"}
    loop = True
    duration = float("inf")
    BOX = 3.0
    SIDE = 4                     # 4 × 4 × 4 particles
    R = 0.17

    def prepare(self):
        T = self.p("temperature")
        self.state = "solid" if T < MELT else "liquid" if T < BOIL else "gas"
        g = np.arange(self.SIDE) * 0.42
        g = g - g.mean()
        self.sites = np.array([(x, y, z + 0.42 * (self.SIDE - 1) / 2 + self.R) for x in g for y in g for z in g])
        self.reset()

    def reset(self):
        super().reset()
        self.rng = np.random.default_rng(7)
        self.pos = self.sites.copy()
        speed = 0.9 * np.sqrt(self.p("temperature") / 300.0)
        v = self.rng.normal(size=self.pos.shape)
        self.vel = v / np.linalg.norm(v, axis=1, keepdims=True) * speed * (2.2 if self.state == "gas" else 1.0)

    def advance(self, dt):
        super().advance(dt)
        dt = min(dt, 0.1) * self.speed
        T, half = self.p("temperature"), self.BOX / 2
        if self.state == "solid":
            # Each one shakes about its own place in the crystal; harder when warmer.
            amp = 0.05 * np.sqrt(T / MELT)
            self.pos = self.sites + self.rng.normal(scale=amp, size=self.sites.shape)
            return
        if self.state == "liquid":
            # Close together but free to slide: a random walk that keeps to the
            # bottom of the box, where the liquid lies.
            self.vel += self.rng.normal(scale=1.6 * np.sqrt(T / 300.0) * dt, size=self.vel.shape)
            self.vel *= 0.96
            top = 1.55
        else:
            top = self.BOX
        self.pos += self.vel * dt
        lo = np.array([-half + self.R, -half + self.R, self.R])
        hi = np.array([half - self.R, half - self.R, top - self.R])
        for axis in range(3):
            low, high = self.pos[:, axis] < lo[axis], self.pos[:, axis] > hi[axis]
            self.pos[low, axis] = 2 * lo[axis] - self.pos[low, axis]
            self.pos[high, axis] = 2 * hi[axis] - self.pos[high, axis]
            self.vel[low | high, axis] *= -1

    def build_parts(self):
        h = self.BOX / 2
        box = pv.Box((-h, h, -h, h, 0, self.BOX))
        names = {"solid": "Solid (ice)", "liquid": "Liquid (water)", "gas": "Gas (steam)"}
        return {"container": ScenePart("container", "Container", box.extract_feature_edges().tube(radius=0.02), "#c0c8d0"),
                "walls": ScenePart("walls", "Glass", box, "#9fc6e8", opacity=0.08),
                "particles": ScenePart("particles", f"Particles: {names[self.state]}", beads(self.pos, self.R, 10), "#4fb0ff")}

    def apply(self, engine):
        engine.update_part_mesh("particles", beads(self.pos, self.R, 10))

    def measurements(self):
        T = self.p("temperature")
        how = {"solid": "vibrating about fixed places", "liquid": "sliding past one another",
               "gas": "flying freely and hitting the walls"}
        return {"state": {"solid": "solid (ice)", "liquid": "liquid (water)", "gas": "gas (steam)"}[self.state],
                "temperature": f"{T:.0f} K = {T - 273:.0f} °C", "particles_are": how[self.state],
                "relative_speed": fmt(np.sqrt(T / 300.0), "× room temperature", 2)}


class Buoyancy(Simulation):
    id = "buoyancy"
    title = "Floating and sinking (Archimedes' principle)"
    disclaimer = ("A 20 cm cube dropped into a liquid. Arrows: weight (red) and buoyant force, the upthrust "
                  "(green). Water slows it down, so it settles.")
    equations = ["buoyant force F_B = ρ_liquid · V_under · g  (Archimedes)", "it floats if ρ_object < ρ_liquid",
                 "fraction under the surface = ρ_object / ρ_liquid"]
    PARAMETERS = [Parameter("object_density", 600.0, 100.0, 3000.0, "kg/m³", "density of the block (wood ≈ 600, ice ≈ 920, iron ≈ 7800)", 10.0),
                  Parameter("liquid_density", 1000.0, 600.0, 1500.0, "kg/m³", "density of the liquid (water = 1000, sea water ≈ 1025)", 10.0)]
    ALIASES = {"density": "object_density", "block_density": "object_density", "fluid_density": "liquid_density",
               "water_density": "liquid_density"}
    SIDE = 0.2
    DEPTH = 0.8
    G = 9.81

    def under(self, z):
        """Depth of the block below the surface (z = bottom face; surface at 0)."""
        return float(np.clip(-z, 0.0, self.SIDE))

    def prepare(self):
        rho, rho_l, s, g = self.p("object_density"), self.p("liquid_density"), self.SIDE, self.G
        m = rho * s ** 3

        def rhs(_t, y):
            z, v = y
            sub = self.under(z)
            f = -m * g + rho_l * s * s * sub * g - (6.0 * rho_l * s * s * v * abs(v) * (sub / s) + 2.0 * v * (sub > 0))
            if z <= -self.DEPTH and v < 0:
                return [0.0, 0.0]
            return [v, f / m]
        self.duration = 6.0
        self.sol = solve_ivp(rhs, (0, self.duration), [0.25, 0.0], dense_output=True, max_step=0.01)

    def z(self, t):
        return max(float(self.sol.sol(min(t, self.duration))[0]), -self.DEPTH)

    def forces(self, t):
        s = self.SIDE
        weight = self.p("object_density") * s ** 3 * self.G
        lift = self.p("liquid_density") * s * s * self.under(self.z(t)) * self.G
        return weight, lift

    def arrows(self, t):
        z = self.z(t)
        k = 0.25 / (self.p("object_density") * self.SIDE ** 3 * self.G)
        c = np.array([0, -0.12, z + self.SIDE / 2])
        weight, lift = self.forces(t)
        return arrow(c, (0, 0, -weight * k), 0.008, 0.025), arrow(c + (0.04, 0, 0), (0, 0, max(lift * k, 1e-4)), 0.008, 0.025)

    def build_parts(self):
        s, D = self.SIDE, self.DEPTH
        down, up = self.arrows(0)
        tank = pv.Box((-0.5, 0.5, -0.3, 0.3, -D - 0.02, 0.4))
        return {"tank": ScenePart("tank", "Tank", tank.extract_feature_edges().tube(radius=0.006), "#c0c8d0"),
                "liquid": ScenePart("liquid", "Liquid", pv.Box((-0.5, 0.5, -0.3, 0.3, -D - 0.02, 0.0)), "#2f7fd0", opacity=0.35),
                "block": ScenePart("block", "Block", pv.Box((-s / 2, s / 2, -s / 2, s / 2, 0.25, 0.25 + s)), "#d8a060"),
                "weight": ScenePart("weight", "Weight", down, "#ff5a5a"),
                "upthrust": ScenePart("upthrust", "Buoyant force (upthrust)", up, "#6fd08c")}

    def apply(self, engine):
        engine.move_part("block", (0, 0, self.z(self.t) - 0.25))
        down, up = self.arrows(self.t)
        engine.update_part_mesh("weight", down)
        engine.update_part_mesh("upthrust", up)

    def measurements(self):
        rho, rho_l = self.p("object_density"), self.p("liquid_density")
        weight, lift = self.forces(self.t)
        out = {"weight": fmt(weight, "N"), "buoyant_force_now": fmt(lift, "N"),
               "max_buoyant_force": fmt(rho_l * self.SIDE ** 3 * self.G, "N")}
        if rho < rho_l:
            out["result"] = f"floats, with {100 * rho / rho_l:.0f}% under the surface"
        else:
            out["result"] = "sinks: it is denser than the liquid"
        return out


class RadioactiveDecay(Simulation):
    id = "radioactive_decay"
    title = "Radioactive decay and half-life"
    disclaimer = ("400 unstable nuclei (green). Each one decays (grey) at a random moment; nobody can say which "
                  "will go next, but half of them have gone after every half-life. The curve is what is left.")
    equations = ["N = N₀ · (½)^(t / T½)", "after one half-life half are left, after two a quarter, after three an eighth"]
    PARAMETERS = [Parameter("half_life", 3.0, 1.0, 10.0, "s", "half-life T½", 0.5)]
    ALIASES = {"halflife": "half_life", "t_half": "half_life", "half": "half_life"}
    N0, SIDE = 400, 20

    def prepare(self):
        rng = np.random.default_rng(11)
        lam = np.log(2) / self.p("half_life")
        self.decay_at = rng.exponential(1 / lam, self.N0)
        g = (np.arange(self.SIDE) - (self.SIDE - 1) / 2) * 0.22
        self.grid = np.array([(x, 0.0, z + 2.6) for z in g for x in g])
        self.duration = 5 * self.p("half_life")

    def left(self, t):
        return self.decay_at > t

    def curve(self, t):
        ts = np.linspace(0, max(t, 1e-3), max(2, int(100 * t / self.duration) + 2))
        n = np.array([np.count_nonzero(self.decay_at > x) for x in ts])
        return polyline_tube(np.column_stack([3.0 + 4.0 * ts / self.duration, np.zeros_like(ts), 0.6 + 4.0 * n / self.N0]), 0.03, 6)

    def build_parts(self):
        T, D = self.p("half_life"), self.duration
        ts = np.linspace(0, D, 120)
        ideal = np.column_stack([3.0 + 4.0 * ts / D, np.zeros_like(ts) + 0.02, 0.6 + 4.0 * 0.5 ** (ts / T)])
        ticks = [polyline_tube([(3.0 + 4.0 * k * T / D, 0, 0.5), (3.0 + 4.0 * k * T / D, 0, 0.7)], 0.02) for k in range(1, 6)]
        alive = self.left(0)
        return {"undecayed": ScenePart("undecayed", "Undecayed nuclei", beads(self.grid[alive], 0.08, 8), "#6fd08c"),
                "decayed": ScenePart("decayed", "Decayed nuclei", beads(self.grid[~alive], 0.08, 8), "#5a6070"),
                "axes": ScenePart("axes", "Number left against time", pv.merge([polyline_tube([(3.0, 0, 4.8), (3.0, 0, 0.6), (7.2, 0, 0.6)], 0.02)] + ticks), "#c0c8d0"),
                "expected": ScenePart("expected", "Expected: halves every half-life", polyline_tube(ideal, 0.015, 6), "#ffd84d", opacity=0.6),
                "count": ScenePart("count", "Nuclei left", self.curve(0.0), "#6fd08c")}

    def apply(self, engine):
        alive = self.left(self.t)
        engine.update_part_mesh("undecayed", beads(self.grid[alive], 0.08, 8))
        engine.update_part_mesh("decayed", beads(self.grid[~alive], 0.08, 8))
        engine.update_part_mesh("count", self.curve(self.t))

    def measurements(self):
        T = self.p("half_life")
        n = int(np.count_nonzero(self.left(self.t)))
        return {"half_life": fmt(T, "s", 1), "time": fmt(self.t, "s", 1), "nuclei_left": f"{n} of {self.N0}",
                "expected_left": f"{self.N0 * 0.5 ** (self.t / T):.0f}", "half_lives_passed": fmt(self.t / T, "", 1)}
