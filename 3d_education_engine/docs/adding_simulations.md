# Adding a simulation

Physics is generated, never downloaded. A simulation is a class that owns the physics (state as a
function of time) and describes a scene (named parts); the engine draws it and calls it each frame.

## 1. Write the class

```python
# physics/mechanics.py
import numpy as np
import pyvista as pv
from physics.base import Parameter, Simulation, arrow, fmt, polyline_tube
from visualization.scene import ScenePart


class InclinedPlane(Simulation):
    id = "inclined_plane"
    title = "Block on a frictionless slope"
    disclaimer = "No friction."
    equations = ["a = g·sinθ", "s = ½·a·t²"]
    PARAMETERS = [Parameter("angle", 30.0, 1.0, 80.0, "°", "slope angle"),
                  Parameter("gravity", 9.81, 0.1, 30.0, "m/s²", "g")]
    ALIASES = {"slope": "angle", "g": "gravity"}

    def prepare(self):                       # recompute after any parameter change
        self.a = self.p("gravity") * np.sin(np.radians(self.p("angle")))
        self.duration = np.sqrt(2 * 5.0 / self.a)          # time to slide 5 m

    def s(self, t):
        return 0.5 * self.a * min(t, self.duration) ** 2

    def build_parts(self):                   # geometry at t = 0
        th = np.radians(self.p("angle"))
        top = np.array([0, 0, 5 * np.sin(th)])
        return {"slope": ScenePart("slope", "Slope", polyline_tube([top, (5 * np.cos(th), 0, 0)], 0.05), "#707880"),
                "block": ScenePart("block", "Block", pv.Cube(center=top, x_length=0.4, y_length=0.4, z_length=0.4), "#ff6b4a")}

    def apply(self, engine):                 # move parts to time self.t
        th = np.radians(self.p("angle"))
        engine.move_part("block", self.s(self.t) * np.array([np.cos(th), 0, -np.sin(th)]))

    def measurements(self):
        return {"acceleration": fmt(self.a, "m/s²"), "time_to_bottom": fmt(self.duration, "s")}
```

Conventions: SI units; x horizontal, z up; `front = "-y"` looks at the x–z plane (`"+z"` looks down
on x–y). Only move parts through `engine.move_part` / `engine.update_part_mesh`, never through VTK.

## 2. Register it

Add the class to `SIMULATIONS` in `physics/registry.py`. That list is the whitelist a manifest may
name.

## 3. Manifest

`models/manifests/physics/inclined_plane.json`:

```json
{
  "id": "physics.mechanics.inclined_plane", "name": "Inclined Plane", "domain": "physics",
  "subject": "mechanics", "kind": "simulation",
  "simulation": {"name": "inclined_plane", "parameters": {}},
  "source": {"provider": "3D Education Engine (procedural)", "url": "local://generated",
             "source_id": "physics.inclined_plane", "creator": "3D Education Engine",
             "license": "LicenseRef-Generated", "license_url": "docs/provenance.md#generated-content",
             "attribution": "Educational model generated procedurally by 3D Education Engine, based on the standard equations of motion stated in the simulation.",
             "download_date": "2026-09-25", "modification_status": "generated"},
  "educational": {"grades": ["9","10","11","12"], "topics": ["inclined plane", "forces on a slope"],
                  "scale_level": "simulation", "disclaimer": "No friction."},
  "aliases": ["inclined plane", "slope", "ramp"],
  "parts": {}, "capabilities": ["rotate", "zoom", "simulate", "animate", "measure"]
}
```

## 4. Index, document, test

```bash
python scripts/build_index.py
```

Add `knowledge/documents/physics/inclined_plane.md` (concept `physics.mechanics.inclined_plane`)
and a test in `tests/test_physics.py` that checks the numbers against the formula. The agent picks
the simulation up automatically: "show the inclined plane", "change the slope to 45 degrees",
"play", "what is the acceleration?" (parameter names and `ALIASES` feed the offline parser).
