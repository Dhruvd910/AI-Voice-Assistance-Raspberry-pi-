"""The whitelist of simulations a manifest may name, and scene building."""

from __future__ import annotations

from physics.astronomy import OrbitalMotion, SolarSystem
from physics.base import Simulation
from physics.circuits import SeriesCircuit
from physics.electromagnetism import ElectricField, MagneticFieldWire
from physics.mechanics import CircularMotion, Collision, FreeFall, Pendulum, ProjectileMotion, Spring
from physics.waves import Diffraction, Interference, Reflection, Refraction, SineWave, StandingWave
from visualization.scene import SceneModel

SIMULATIONS: dict[str, type[Simulation]] = {cls.id: cls for cls in [
    ProjectileMotion, FreeFall, Pendulum, Spring, CircularMotion, Collision,
    OrbitalMotion, SolarSystem, SineWave, StandingWave, Interference, Diffraction,
    Refraction, Reflection, ElectricField, MagneticFieldWire, SeriesCircuit,
]}


def create(name: str, **params) -> Simulation:
    if name not in SIMULATIONS:
        raise ValueError(f"simulation {name!r} is not registered")
    return SIMULATIONS[name](**params)


def scene_for_simulation(sim: Simulation, model_id: str, title: str | None = None) -> SceneModel:
    return SceneModel(model_id=model_id, title=title or sim.title, parts=sim.build_parts(),
                      units=sim.units, disclaimer=sim.disclaimer,
                      extras={"simulation": sim, "front": sim.front})


def scene_for_entry(entry) -> SceneModel:
    spec = entry.manifest["simulation"]
    sim = create(spec["name"], **spec.get("parameters", {}))
    return scene_for_simulation(sim, entry.id, entry.name)
