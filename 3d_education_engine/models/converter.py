"""original/ -> processed/ -> runtime/: cleaning, decimation, GLB export, thumbnails.

The three folders are kept apart on purpose:

    original/   exactly what the source served, never modified
    processed/  one cleaned mesh per named part (VTK .vtp), full resolution
    runtime/    GLB files the engine loads: <name>.glb (high) and <name>_low.glb

and every file written is hashed into the manifest's "files" map, so
scripts/validate_models.py can tell when something on disk has changed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pyvista as pv

from biology.anatomy import ModelRecipe, all_concepts, assign_elements
from models.downloader import BodyParts3DAdapter
from models.validator import sha256_of, validate_manifest

log = logging.getLogger(__name__)

LOW_LOD_TARGET_CELLS = 30_000       # the whole model, on a Pi, with room to spare


# ------------------------------------------------------------------ meshes
def read_mesh(path: Path) -> pv.PolyData:
    mesh = pv.read(str(path))
    if isinstance(mesh, pv.MultiBlock):
        mesh = mesh.combine()
    if not isinstance(mesh, pv.PolyData):
        mesh = mesh.extract_surface(algorithm="dataset_surface")
    return mesh


def merge_meshes(paths: list[Path]) -> pv.PolyData:
    meshes = [read_mesh(p) for p in paths]
    merged = meshes[0] if len(meshes) == 1 else pv.merge(meshes)
    if not isinstance(merged, pv.PolyData):
        merged = merged.extract_surface(algorithm="dataset_surface")
    return clean(merged)


def clean(mesh: pv.PolyData) -> pv.PolyData:
    mesh = mesh.triangulate().clean(tolerance=1e-6)
    return mesh.compute_normals(auto_orient_normals=False, split_vertices=False)


def trim_below(mesh: pv.PolyData, z: float) -> pv.PolyData:
    """Keep only the geometry at or above height z."""
    return clean(mesh.clip(normal="z", origin=(0.0, 0.0, z), invert=False))


def decimate(mesh: pv.PolyData, fraction_kept: float) -> pv.PolyData:
    if fraction_kept >= 0.999 or mesh.n_cells < 200:
        return mesh
    # decimate_pro with preserve_topology: thin valve leaflets and small
    # vessels survive, where plain quadric decimation can punch holes in them.
    out = mesh.decimate_pro(1.0 - fraction_kept, preserve_topology=True)
    return clean(out) if out.n_cells else mesh


# ------------------------------------------------------------------ GLB
def export_glb(parts: dict[str, pv.PolyData], colors: dict[str, str], path: Path) -> None:
    """One GLB, one named node per part, so part names survive the round trip."""
    import trimesh
    scene = trimesh.Scene()
    for part_id, mesh in parts.items():
        tri = mesh.triangulate()
        faces = tri.faces.reshape(-1, 4)[:, 1:]
        rgba = _rgba(colors.get(part_id, "#cccccc"))
        tm = trimesh.Trimesh(vertices=np.asarray(tri.points, dtype=np.float32), faces=faces,
                             process=False)
        tm.visual = trimesh.visual.ColorVisuals(tm, face_colors=np.tile(rgba, (len(faces), 1)))
        scene.add_geometry(tm, node_name=part_id, geom_name=part_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(scene.export(file_type="glb"))


def _rgba(hex_color: str) -> np.ndarray:
    h = hex_color.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)] + [255], dtype=np.uint8)


def thumbnail(parts: dict[str, pv.PolyData], colors: dict[str, str], path: Path,
              azimuth: float = 0.0, elevation: float = 0.0, size=(320, 240)) -> None:
    plotter = pv.Plotter(off_screen=True, window_size=list(size))
    plotter.set_background("#101418")
    for part_id, mesh in parts.items():
        plotter.add_mesh(mesh, color=colors.get(part_id, "#cccccc"), smooth_shading=True)
    plotter.view_xz()                 # anterior view for BodyParts3D: looking along +y
    plotter.camera.azimuth = azimuth
    plotter.camera.elevation = elevation
    plotter.reset_camera()
    path.parent.mkdir(parents=True, exist_ok=True)
    plotter.screenshot(str(path))
    plotter.close()


# ------------------------------------------------------------------ BodyParts3D pipeline
def build_bodyparts3d_model(recipe: ModelRecipe, source: BodyParts3DAdapter, model_dir: Path,
                            fetch: bool = True) -> dict:
    """Recipe -> originals, processed parts, runtime GLBs, thumbnail and manifest.

    Returns the manifest (also written to metadata/manifest.json). Every step
    is repeatable: files already present are not fetched again.
    """
    elements = source.elements()
    concepts = source.concepts()
    assignment = assign_elements(recipe, elements)
    original_dir = model_dir / "original" / "bodyparts3d"
    if fetch:
        source.download_elements(sorted({e for ids in assignment.values() for e in ids}), original_dir)

    parts: dict[str, pv.PolyData] = {}
    for part in recipe.parts:
        parts[part.id] = merge_meshes([original_dir / f"{e}.obj" for e in assignment[part.id]])
    # Heights for trimming come from the heart itself, not a hard-coded number.
    chamber_ids = [p.id for p in recipe.parts if not p.keep_z_above]
    heart_zmin = min(parts[p].bounds[4] for p in chamber_ids)
    for part in recipe.parts:
        if part.keep_z_above is not None:
            parts[part.id] = trim_below(parts[part.id], heart_zmin - part.keep_z_above)

    # Centre on the origin; keep the offset so source coordinates can be recovered.
    everything = pv.merge(list(parts.values()))
    offset = np.array(everything.center)
    for pid in parts:
        parts[pid] = parts[pid].translate(-offset, inplace=False)

    colors = {p.id: p.color for p in recipe.parts}
    stem = recipe.model_id.rsplit(".", 1)[-1]
    processed_dir, runtime_dir, meta_dir = (model_dir / d for d in ("processed", "runtime", "metadata"))
    processed_dir.mkdir(parents=True, exist_ok=True)
    for pid, mesh in parts.items():
        mesh.save(str(processed_dir / f"{pid}.vtp"))
    export_glb(parts, colors, runtime_dir / f"{stem}.glb")
    total = sum(m.n_cells for m in parts.values())
    keep = min(1.0, (recipe.low_lod_cells or LOW_LOD_TARGET_CELLS) / max(total, 1))
    low = {pid: decimate(mesh, keep) for pid, mesh in parts.items()}
    export_glb(low, colors, runtime_dir / f"{stem}_low.glb")
    thumbnail(parts, colors, meta_dir / "thumbnail.png")

    source_asset = source.metadata(recipe.root_concept)
    provenance = source_asset.provenance(source.attribution, "extracted+converted+decimated").to_manifest()
    extra = len(all_concepts(recipe)) - 1
    provenance["source_id"] = recipe.root_concept + (f" (+{extra} related FMA concepts)" if extra else "")
    manifest = {
        "id": recipe.model_id,
        "name": recipe.name,
        "domain": recipe.model_id.split(".")[0],
        "subject": recipe.model_id.split(".")[1],
        "kind": "asset",
        "asset": {"file": f"runtime/{stem}.glb", "format": "glb", "units": "mm",
                  "lods": {"high": f"runtime/{stem}.glb", "low": f"runtime/{stem}_low.glb"},
                  "origin_offset_mm": [round(float(v), 3) for v in offset],
                  "cells": {"high": int(total), "low": int(sum(m.n_cells for m in low.values()))},
                  "thumbnail": "metadata/thumbnail.png"},
        "source": provenance,
        "source_detail": {
            "database_license_page": BodyParts3DAdapter.license_page,
            "archive": f"{BodyParts3DAdapter.base}/{BodyParts3DAdapter.archive}",
            "concepts": {c: concepts.get(c, "") for c in all_concepts(recipe)},
            "processing": ["element meshes merged per part (first claim wins; see biology/anatomy.py)",
                           "triangulated, cleaned, normals computed",
                           "centred on the origin (origin_offset_mm recovers source coordinates)",
                           f"low LOD: decimate_pro keeping {keep:.0%} of triangles",
                           *[f"{p.id}: {p.note}" for p in recipe.parts if p.note]],
        },
        "educational": {"grades": recipe.grades, "topics": recipe.topics, "scale_level": recipe.scale_level,
                        "disclaimer": "Anatomical model (BodyParts3D). Colours are a teaching convention."},
        "aliases": recipe.aliases,
        "parts": {},
        "groups": recipe.groups,
        "group_aliases": recipe.group_aliases,
        "relationships": recipe.relationships,
        "capabilities": ["rotate", "zoom", "hide", "show", "highlight", "transparent", "clip",
                         "explode", "focus", "label", "measure", "animate"],
        "animations": recipe.animations,
        "view": {"azimuth": 0.0, "elevation": 0.0, "up": "z", "front": recipe.front},
        "notes": recipe.notes,
        **({"default_labels": recipe.default_labels} if recipe.default_labels else {}),
        "generated_on": date.today().isoformat(),
        "files": {},
    }
    for part in recipe.parts:
        manifest["parts"][part.id] = {
            "name": part.name, "description": part.description, "color": part.color,
            "aliases": part.aliases, "available": True,
            "source_concepts": part.concepts or [f"remainder of {part.remainder_of}"],
            "source_elements": assignment[part.id],
            **({"note": part.note} if part.note else {}),
        }
    for part_id, spec in recipe.unavailable.items():
        manifest["parts"][part_id] = {**spec, "available": False}
    for path in sorted(p for p in model_dir.rglob("*") if p.is_file() and p.name != "manifest.json"):
        manifest["files"][str(path.relative_to(model_dir))] = sha256_of(path)

    problems = validate_manifest(manifest)
    if problems:
        raise ValueError("generated manifest is invalid: " + "; ".join(problems))
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


# ------------------------------------------------------------------ generic single files
def convert_single_file(src: Path, model_dir: Path, part_id: str = "model", color: str = "#d0b090") -> dict:
    """An STL/OBJ/PLY/GLB from a source like NIH 3D -> runtime GLBs + thumbnail.

    Returns {"file", "lods", "cells", "offset"} for the manifest's asset block.
    """
    if src.suffix.lower() in {".glb", ".gltf"}:
        from models.loader import load_mesh_file
        parts = {k: clean(v) for k, v in load_mesh_file(src).items()}
    else:
        parts = {part_id: clean(read_mesh(src))}
    merged = pv.merge(list(parts.values())) if len(parts) > 1 else next(iter(parts.values()))
    offset = np.array(merged.center)
    parts = {k: v.translate(-offset, inplace=False) for k, v in parts.items()}
    colors = {k: color for k in parts}
    stem = src.stem.lower().replace(" ", "_")
    export_glb(parts, colors, model_dir / "runtime" / f"{stem}.glb")
    total = sum(m.n_cells for m in parts.values())
    keep = min(1.0, LOW_LOD_TARGET_CELLS / max(total, 1))
    export_glb({k: decimate(v, keep) for k, v in parts.items()}, colors, model_dir / "runtime" / f"{stem}_low.glb")
    thumbnail(parts, colors, model_dir / "metadata" / "thumbnail.png")
    return {"file": f"runtime/{stem}.glb", "lods": {"high": f"runtime/{stem}.glb", "low": f"runtime/{stem}_low.glb"},
            "cells": int(total), "offset": [float(v) for v in offset], "parts": list(parts)}


# ------------------------------------------------------------------ Blender
BLENDER_EXPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "blender_export_objects.py"


def blender_export_objects(blend_file: Path, object_names: list[str], out_dir: Path) -> list[Path]:
    """Export named objects from a .blend (e.g. the Z-Anatomy atlas) as OBJ files.

    Needs the `blender` executable (`sudo apt install blender` on Raspberry Pi
    OS, ~350 MB). NOTE: not exercised on the development Pi, which has no
    Blender installed; the Z-Anatomy path is otherwise ready.
    """
    exe = shutil.which("blender")
    if exe is None:
        raise RuntimeError("Blender is not installed; it is needed to extract objects from .blend files")
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([exe, "--background", str(blend_file), "--python", str(BLENDER_EXPORT_SCRIPT),
                    "--", str(out_dir), *object_names], check=True, timeout=1800)
    return sorted(out_dir.glob("*.obj"))
