"""Chemistry actions on the scene: bond angles, representations, comparisons, shells.

Called by the tool registry. Every number reported comes from the structure
on screen; a reference (measured) value from the manifest is quoted beside it
and named as such, so "103.9° in this model, 104.5° measured" is never
blurred into one figure.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from chemistry import rdkit_engine as rk
from chemistry.molecules import REPRESENTATIONS, build_parts, comparison_scene
from visualization.engine import EngineError, VisualizationEngine
from visualization.scene import ScenePart


def _molecule(engine: VisualizationEngine):
    scene = engine._require_scene()
    molecule = scene.extras.get("molecule")
    if molecule is None:
        raise EngineError(f"{scene.title} is not a molecule; load one first (e.g. 'create a water molecule')")
    return scene, molecule


def show_bond_angle(engine: VisualizationEngine) -> dict:
    scene, molecule = _molecule(engine)
    mol = molecule.mol
    if mol.GetNumAtoms() < 3:
        return {"message": f"{scene.title} has only {mol.GetNumAtoms()} atoms, so there is no bond angle; "
                           "a two-atom molecule is always linear."}
    geo = rk.vsepr(mol)
    angles = rk.bond_angles(mol, geo["atom_index"])
    if not angles:
        raise EngineError("no bond angle at the central atom")
    first = angles[0]
    i, c, j = first["atoms"]
    pos = molecule.coords
    v1, v2 = pos[i] - pos[c], pos[j] - pos[c]
    # Outside the central atom's sphere, so the arc is never hidden inside it.
    r = 0.75 * min(np.linalg.norm(v1), np.linalg.norm(v2))
    u1, u2 = v1 / np.linalg.norm(v1), v2 / np.linalg.norm(v2)
    arc_pts = []
    theta = np.arccos(np.clip(np.dot(u1, u2), -1, 1))
    for t in np.linspace(0, 1, 24):
        # Spherical interpolation from u1 to u2.
        w = np.sin((1 - t) * theta) / np.sin(theta) * u1 + np.sin(t * theta) / np.sin(theta) * u2 \
            if theta > 1e-6 else u1
        arc_pts.append(pos[c] + r * w)
    arc = pv.Spline(np.array(arc_pts), 48).tube(radius=0.035)
    parts = dict(engine.scene.parts)
    parts["angle_arc"] = ScenePart("angle_arc", "Bond angle", arc, "#ffd84d")
    engine.replace_parts(parts)
    mid = pos[c] + r * 1.25 * (u1 + u2) / (np.linalg.norm(u1 + u2) or 1.0)
    engine.annotations.add_label(f"{first['degrees']:.1f}°", mid, label_id="bond_angle")
    distinct = sorted({round(a["degrees"], 1) for a in angles})
    ref = molecule.reference
    msg = (f"The {first['label']} angle in this model is {first['degrees']:.1f}°. "
           f"{geo['element']} has {geo['bonded_atoms']} bonded atom(s) and {geo['lone_pairs']} lone pair(s), "
           f"so the shape is {geo['shape']}.")
    if ref.get("degrees"):
        msg += f" The measured value is {ref['degrees']}° ({ref.get('source', 'reference')})."
    return {"angle_degrees": round(first["degrees"], 2), "all_angles": distinct, "shape": geo["shape"],
            "lone_pairs": geo["lone_pairs"], "geometry_source": molecule.geometry_source,
            "reference": ref or None, "message": msg}


def set_representation(engine: VisualizationEngine, style: str) -> dict:
    scene = engine._require_scene()
    style = style.lower().replace("-", "_").replace(" ", "_")
    aliases = {"ball": "ball_and_stick", "ball_and_stick": "ball_and_stick", "space_filling": "space_filling",
               "spacefill": "space_filling", "cpk": "space_filling", "stick": "sticks", "sticks": "sticks",
               "backbone": "backbone", "ribbon": "backbone", "cartoon": "backbone", "atoms": "atoms",
               "surface": "surface"}
    style = aliases.get(style, style)
    if scene.extras.get("molecule") is not None:
        if style not in REPRESENTATIONS:
            raise EngineError(f"molecules can be shown as {', '.join(REPRESENTATIONS)}")
        engine.replace_parts(build_parts(scene.extras["molecule"], style))
        scene.extras["representation"] = style
        engine.annotations.remove_label("bond_angle")
        return {"representation": style, "message": f"Showing {scene.title} as a {style.replace('_', '-')} model."}
    entry = engine.registry.get(scene.model_id)
    if entry and entry.manifest.get("structure", {}).get("mmcif_file"):
        from chemistry.proteins import REPRESENTATIONS as PROT, scene_for_protein
        if style not in PROT:
            raise EngineError(f"proteins can be shown as {', '.join(PROT)}")
        engine.replace_parts(scene_for_protein(entry, style).parts)
        return {"representation": style, "message": f"Showing {scene.title} as {style}."}
    raise EngineError(f"{scene.title} has no alternative representations")


def compare(engine: VisualizationEngine, model_ids: list[str]) -> dict:
    entries = []
    for mid in model_ids:
        entry = engine.registry.get(mid)
        if entry is None or entry.kind != "molecule" or not entry.manifest["structure"].get(
                "sdf_file") and not entry.manifest["structure"].get("smiles"):
            raise EngineError(f"{mid} is not a molecule that can be compared")
        entries.append(entry)
    if len(entries) < 2:
        raise EngineError("name at least two molecules to compare")
    scene = comparison_scene(entries)
    engine.show_scene(scene)
    rows = []
    for entry in entries:
        f = scene.extras["facts"][entry.id]
        geo = f.get("geometry", {})
        angle = f["bond_angles"][0]["degrees"] if f.get("bond_angles") else None
        rows.append(f"{entry.name} ({f['formula']}): {geo.get('shape', 'diatomic')}"
                    + (f", {angle:.1f}°" if angle else "") + f", {f.get('polarity_estimate', '')}")
    return {"compared": model_ids, "facts": scene.extras["facts"], "message": "; ".join(rows) + "."}


def electron_configuration(engine: VisualizationEngine) -> dict:
    scene = engine._require_scene()
    atom = scene.extras.get("atom")
    if atom is None:
        raise EngineError("load an atom first (e.g. 'show me a carbon atom')")
    engine.highlight("valence_electrons")
    return {**atom, "message": (f"{scene.title}: {atom['protons']} protons, {atom['neutrons']} neutrons "
                                f"(in the most common isotope) and {atom['electrons']} electrons, arranged "
                                f"{', '.join(map(str, atom['shells']))} in shells; configuration "
                                f"{atom['configuration']}. The outer shell holds {atom['valence_electrons']}.")}


def molecule_facts(engine: VisualizationEngine) -> dict:
    scene = engine._require_scene()
    facts = scene.extras.get("facts") or scene.extras.get("atom")
    if not facts:
        raise EngineError(f"{scene.title} has no chemistry facts")
    return {"facts": facts, "message": f"Facts for {scene.title}."}
