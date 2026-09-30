"""Proteins from RCSB PDB mmCIF files: parse, then draw.

PDB ID -> download (models/acquisition.py) -> parse _atom_site -> geometry.

Representations:
    backbone   a smooth tube through each chain's alpha carbons (a simple
               stand-in for a ribbon; it shows the fold, not the secondary
               structure types)
    atoms      every heavy atom as a small sphere, coloured by element
    surface    a coarse molecular surface from a Gaussian density of the atoms

Ligands such as the haem groups in haemoglobin are drawn as atoms in every
representation, because they are usually the point of the lesson.
"""

from __future__ import annotations

import shlex
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyvista as pv

from chemistry.atoms import CPK
from visualization.scene import SceneModel, ScenePart

CHAIN_COLORS = ["#e06666", "#6fa8dc", "#f6b26b", "#93c47d", "#8e7cc3", "#76a5af", "#c27ba0", "#ffd966"]
REPRESENTATIONS = ("backbone", "atoms", "surface")


def parse_atom_site(path: Path) -> list[dict]:
    """Rows of the _atom_site loop (first model only). Handles quoted values."""
    rows, columns, in_loop, reading = [], [], False, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s == "loop_":
                in_loop, columns, reading = True, [], False
                continue
            if in_loop and s.startswith("_atom_site."):
                columns.append(s.split(".", 1)[1].split()[0])
                continue
            if in_loop and columns and not s.startswith("_"):
                if s.startswith("#") or s == "":
                    if reading:
                        break
                    continue
                reading = True
                values = shlex.split(s, posix=True)
                if len(values) != len(columns):
                    continue
                row = dict(zip(columns, values))
                if row.get("pdbx_PDB_model_num", "1") != "1":
                    break
                rows.append(row)
            elif in_loop and columns and s.startswith("_") and not s.startswith("_atom_site."):
                if reading or rows:
                    break
                in_loop = False
    return rows


def scene_for_protein(entry, representation: str | None = None) -> SceneModel:
    s = entry.manifest["structure"]
    representation = representation or s.get("representation", "backbone")
    rows = parse_atom_site(entry.model_dir / s["mmcif_file"])
    if not rows:
        raise ValueError(f"{entry.id}: no atoms found in {s['mmcif_file']}")
    xyz = lambda r: (float(r["Cartn_x"]), float(r["Cartn_y"]), float(r["Cartn_z"]))
    polymer = [r for r in rows if r["group_PDB"] == "ATOM"]
    hetero = [r for r in rows if r["group_PDB"] == "HETATM" and r["label_comp_id"] != "HOH"]
    chains: dict[str, list] = defaultdict(list)
    for r in polymer:
        chains[r.get("auth_asym_id", r["label_asym_id"])].append(r)
    centre = np.mean([xyz(r) for r in rows], axis=0)
    parts: dict[str, ScenePart] = {}
    for i, (chain, atoms) in enumerate(sorted(chains.items())):
        color = CHAIN_COLORS[i % len(CHAIN_COLORS)]
        pid = f"chain_{chain.lower()}"
        if representation == "backbone":
            ca = np.array([xyz(r) for r in atoms if r["label_atom_id"] == "CA"]) - centre
            if len(ca) < 2:
                continue
            mesh = pv.Spline(ca, max(len(ca) * 4, 8)).tube(radius=0.8, n_sides=10).triangulate()
        elif representation == "atoms":
            pts = np.array([xyz(r) for r in atoms if r["type_symbol"] != "H"]) - centre
            mesh = pv.PolyData(pts).glyph(geom=pv.Sphere(radius=0.7, theta_resolution=8, phi_resolution=8),
                                          orient=False, scale=False)
        else:
            pts = np.array([xyz(r) for r in atoms]) - centre
            cloud = pv.PolyData(pts)
            mesh = cloud.delaunay_3d(alpha=3.0).extract_surface(algorithm="dataset_surface").smooth(n_iter=30)
        parts[pid] = ScenePart(pid, f"Chain {chain} ({len({r['label_seq_id'] for r in atoms})} residues)", mesh, color)
    ligands: dict[str, list] = defaultdict(list)
    for r in hetero:
        ligands[r["label_comp_id"]].append(r)
    for comp, atoms in sorted(ligands.items()):
        pts = np.array([xyz(r) for r in atoms]) - centre
        colors = [CPK.get(r["type_symbol"].capitalize(), "#ff1493") for r in atoms]
        mesh = pv.PolyData(pts).glyph(geom=pv.Sphere(radius=0.55, theta_resolution=8, phi_resolution=8),
                                      orient=False, scale=False)
        main = max(set(colors), key=colors.count)
        pid = f"ligand_{comp.lower()}"
        parts[pid] = ScenePart(pid, f"{comp} ({len(atoms)} atoms)", mesh, main)
    groups = {"chains": [p for p in parts if p.startswith("chain_")],
              "ligands": [p for p in parts if p.startswith("ligand_")]}
    return SceneModel(model_id=entry.id, title=entry.name, parts=parts, units="Å",
                      disclaimer=f"{representation.capitalize()} view of PDB {s['pdb_id']}; hydrogen atoms and water omitted.",
                      extras={"front": "+z", "groups": groups, "representation": representation,
                              "facts": {"pdb_id": s["pdb_id"], "chains": len(chains), "atoms": len(rows)}})
