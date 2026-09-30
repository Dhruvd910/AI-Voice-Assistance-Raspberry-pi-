"""Registry entry -> SceneModel.

The manifest's `kind` decides how:

    asset       runtime GLB/GLTF (or STL/OBJ/PLY/VTP) from the model's runtime/ folder
    procedural  a generator named in the manifest, looked up in GENERATORS
    molecule    structure data -> RDKit -> geometry (chemistry.molecules)
    simulation  a physics simulation named in the manifest (physics.registry)

Manifests NAME a generator; they cannot point at arbitrary code. A name that
is not in the whitelist below is an error, not an import.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import numpy as np
import pyvista as pv

from models.cache import AssetCache, ModelLODManager
from models.registry import ModelEntry, ModelRegistry
from visualization.scene import ScenePart, SceneModel

log = logging.getLogger(__name__)

Generator = Callable[[ModelEntry], SceneModel]


def _generators() -> dict[str, Generator]:
    # Imported here, not at the top: each domain pulls in its own libraries
    # (RDKit, SciPy) and the loader should not pay for the ones not in use.
    from biology import cells
    from chemistry import reactions
    return {**cells.GENERATORS, **reactions.GENERATORS}


def attribution_line(entry: ModelEntry) -> str:
    src = entry.source
    if src["license"] == "LicenseRef-Generated":
        return "Generated model"
    return f"{src['attribution']} ({src['license']})"


class ModelLoader:
    def __init__(self, registry: ModelRegistry, cache: AssetCache, lod: ModelLODManager):
        self.registry = registry
        self.cache = cache
        self.lod = lod
        self._generators: dict[str, Generator] | None = None

    def load(self, model_id: str) -> SceneModel:
        entry = self.registry.get(model_id)
        if entry is None:
            raise KeyError(f"unknown model {model_id!r}")
        lod = self.lod.choose(entry.manifest.get("asset", {}).get("lods", {})) or "default"
        cached = self.cache.get(model_id, lod)
        if cached is not None:
            return cached
        if entry.kind == "asset":
            scene = self._load_asset(entry, lod)
        elif entry.kind == "procedural":
            scene = self._generator(entry.manifest["generator"]["name"])(entry)
        elif entry.kind == "molecule":
            from chemistry.molecules import scene_for_entry
            scene = scene_for_entry(entry)
        elif entry.kind == "simulation":
            from physics.registry import scene_for_entry as sim_scene
            scene = sim_scene(entry)
        else:
            raise ValueError(f"{model_id}: unknown kind {entry.kind!r}")
        scene.disclaimer = scene.disclaimer or entry.disclaimer
        scene.attribution = scene.attribution or attribution_line(entry)
        # A simulation's scene is mutated as it runs and is cheap to rebuild,
        # so it is never cached.
        if entry.kind != "simulation":
            self.cache.put(model_id, lod, scene)
        return scene

    def _generator(self, name: str) -> Generator:
        if self._generators is None:
            self._generators = _generators()
        if name not in self._generators:
            raise ValueError(f"generator {name!r} is not registered")
        return self._generators[name]

    # ------------------------------------------------------------ assets
    def _load_asset(self, entry: ModelEntry, lod: str) -> SceneModel:
        asset = entry.manifest["asset"]
        rel = asset.get("lods", {}).get(lod, asset["file"])
        path = entry.model_dir / rel
        meshes = load_mesh_file(path)
        parts: dict[str, ScenePart] = {}
        for part_id, spec in entry.parts.items():
            if not spec.get("available", True):
                continue
            mesh = meshes.get(part_id)
            if mesh is None:
                log.warning("%s: part %s is in the manifest but not in %s", entry.id, part_id, path.name)
                continue
            parts[part_id] = ScenePart(
                id=part_id, name=spec["name"], mesh=mesh,
                color=spec.get("color", "#d9a0a0"), opacity=spec.get("opacity", 1.0),
                description=spec.get("description", ""))
        for name, mesh in meshes.items():
            if name not in parts and name not in entry.parts:
                log.warning("%s: %s has an unnamed part %r; it is not shown", entry.id, path.name, name)
        view = entry.manifest.get("view", {})
        return SceneModel(
            model_id=entry.id, title=entry.name, parts=parts,
            units=asset.get("units", "arbitrary units"),
            view=(view.get("azimuth", 0.0), view.get("elevation", 0.0)),
            animations=_asset_animations(entry),
        )


def load_mesh_file(path: Path) -> dict[str, pv.PolyData]:
    """{part name: PolyData} from a GLB/GLTF (named nodes) or a single mesh file."""
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        import trimesh
        scene = trimesh.load(str(path), force="scene")
        out: dict[str, pv.PolyData] = {}
        for node in scene.graph.nodes_geometry:
            transform, geom_name = scene.graph[node]
            geom = scene.geometry[geom_name]
            vertices = trimesh.transformations.transform_points(geom.vertices, transform)
            faces = np.hstack([np.full((len(geom.faces), 1), 3), geom.faces]).ravel()
            mesh = pv.PolyData(np.asarray(vertices, dtype=np.float32), faces)
            out[node] = mesh.compute_normals(auto_orient_normals=False, split_vertices=False)
        return out
    mesh = pv.read(str(path))
    if isinstance(mesh, pv.MultiBlock):
        return {mesh.get_block_name(i) or f"part_{i}": mesh[i].extract_surface()
                for i in range(mesh.n_blocks) if mesh[i] is not None}
    return {path.stem: mesh.extract_surface() if not isinstance(mesh, pv.PolyData) else mesh}


def _asset_animations(entry: ModelEntry) -> dict:
    names = entry.manifest.get("animations", [])
    out = {}
    if "heartbeat" in names:
        from biology.anatomy import HeartbeatAnimator
        out["heartbeat"] = HeartbeatAnimator
    return out
