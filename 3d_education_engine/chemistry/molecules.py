"""Molecules: structure data -> RDKit -> geometry -> PyVista.

No molecule is a stored mesh. A manifest names the structure (a PubChem SDF
kept in original/, or a SMILES), and the geometry is built here each time in
whichever representation is asked for:

    ball_and_stick   atoms at 0.3 x van der Waals radius, bonds as cylinders
                     (double bonds as two, triple as three)
    space_filling    atoms at their van der Waals radii, no bonds
    sticks           bonds only, with small atoms at the joints

Atom sizes in ball-and-stick are shrunk so the bonds show; bond LENGTHS and
ANGLES are from the structure data and are real (in ångströms).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyvista as pv
from rdkit import Chem

from chemistry import rdkit_engine as rk
from chemistry.atoms import element, shell_scene
from visualization.scene import SceneModel, ScenePart

REPRESENTATIONS = ("ball_and_stick", "space_filling", "sticks")
BOND_COLOR = "#b8b8b8"


@dataclass
class Molecule:
    name: str
    mol: Chem.Mol
    geometry_source: str
    reference: dict = field(default_factory=dict)      # measured values from the manifest

    @property
    def symbols(self) -> list[str]:
        return [a.GetSymbol() for a in self.mol.GetAtoms()]

    @property
    def coords(self) -> np.ndarray:
        return rk.coordinates(self.mol)

    def bonds(self) -> list[tuple[int, int, int]]:
        return [(b.GetBeginAtomIdx(), b.GetEndAtomIdx(), int(round(b.GetBondTypeAsDouble())) or 1)
                for b in self.mol.GetBonds()]

    def facts(self) -> dict:
        info = rk.properties(self.mol)
        if self.mol.GetNumAtoms() > 1:
            info["geometry"] = rk.vsepr(self.mol)
            info["bond_angles"] = [{"label": a["label"], "degrees": round(a["degrees"], 1)}
                                   for a in rk.bond_angles(self.mol)]
            info["polarity_estimate"] = rk.polarity(self.mol)
        info["geometry_source"] = self.geometry_source
        info.update({f"reference_{k}": v for k, v in self.reference.items()})
        return info


# ------------------------------------------------------------------ geometry
def _bond_cylinders(a: np.ndarray, b: np.ndarray, order: int, radius: float, up: np.ndarray) -> list[pv.PolyData]:
    axis = b - a
    length = float(np.linalg.norm(axis))
    if length == 0:
        return []
    side = np.cross(axis, up)
    if np.linalg.norm(side) < 1e-6:
        side = np.cross(axis, [1.0, 0.0, 0.0])
        if np.linalg.norm(side) < 1e-6:
            side = np.cross(axis, [0.0, 1.0, 0.0])
    side = side / np.linalg.norm(side)
    spacing = radius * 2.4
    offsets = {1: [0.0], 2: [-0.5, 0.5], 3: [-1.0, 0.0, 1.0]}.get(order, [0.0])
    r = radius if order == 1 else radius * 0.7
    return [pv.Cylinder(center=(a + b) / 2 + side * o * spacing, direction=axis, radius=r,
                        height=length, resolution=14, capping=True).triangulate() for o in offsets]


def build_parts(molecule: Molecule, representation: str = "ball_and_stick",
                coords: np.ndarray | None = None) -> dict[str, ScenePart]:
    if representation not in REPRESENTATIONS:
        raise ValueError(f"representation must be one of {REPRESENTATIONS}")
    pos = molecule.coords if coords is None else coords
    symbols = molecule.symbols
    parts: dict[str, ScenePart] = {}
    scale = {"ball_and_stick": 0.24, "space_filling": 1.0, "sticks": 0.12}[representation]
    for sym in sorted(set(symbols)):
        el = element(sym)
        idx = [i for i, s in enumerate(symbols) if s == sym]
        sphere = pv.Sphere(radius=el.vdw_radius * scale, theta_resolution=24, phi_resolution=24)
        mesh = pv.PolyData(pos[idx]).glyph(geom=sphere, orient=False, scale=False)
        pid = f"{el.name.lower()}_atoms"
        parts[pid] = ScenePart(pid, f"{el.name} atom{'s' if len(idx) > 1 else ''} ({len(idx)})",
                               mesh, el.color)
    if representation != "space_filling" and molecule.mol.GetNumBonds():
        up = np.array([0.0, 0.0, 1.0])
        center = pos.mean(axis=0)
        cyl = []
        for i, j, order in molecule.bonds():
            normal = np.cross(pos[j] - pos[i], center - pos[i])
            if np.linalg.norm(normal) > 1e-6:
                up = normal / np.linalg.norm(normal)
            cyl += _bond_cylinders(pos[i], pos[j], order, 0.11 if representation == "ball_and_stick" else 0.14, up)
        bonds = pv.merge(cyl) if len(cyl) > 1 else cyl[0]
        parts["bonds"] = ScenePart("bonds", f"Covalent bonds ({molecule.mol.GetNumBonds()})", bonds, BOND_COLOR)
    return parts


# ------------------------------------------------------------------ loading
def molecule_for_entry(entry) -> Molecule:
    s = entry.manifest["structure"]
    ref = s.get("reference", {})
    if s.get("sdf_file") and (entry.model_dir / s["sdf_file"]).is_file():
        src = entry.manifest["source"]
        how = ("RDKit 3D from the PubChem 2D record" if "_2d" in s["sdf_file"]
               else f"{src['provider']} CID {src['source_id']} computed 3D conformer")
        return Molecule(entry.name, rk.from_sdf(entry.model_dir / s["sdf_file"]), how, ref)
    if s.get("mol_file") and (entry.model_dir / s["mol_file"]).is_file():
        return Molecule(entry.name, rk.from_molblock((entry.model_dir / s["mol_file"]).read_text()),
                        "MOL file", ref)
    if s.get("smiles"):
        return Molecule(entry.name, rk.from_smiles(s["smiles"]), "generated by RDKit (ETKDG + MMFF)", ref)
    raise rk.StructureError(f"{entry.id}: no usable structure")


def scene_for_entry(entry, representation: str | None = None) -> SceneModel:
    s = entry.manifest["structure"]
    if s.get("element"):
        return shell_scene(entry.id, entry.name, s["element"])
    if s.get("mmcif_file"):
        from chemistry.proteins import scene_for_protein
        return scene_for_protein(entry, representation)
    molecule = molecule_for_entry(entry)
    representation = representation or s.get("representation", "ball_and_stick")
    parts = build_parts(molecule, representation)
    atom_parts = [p for p in parts if p.endswith("_atoms")]
    groups = {"atoms": atom_parts, **({"bonds": ["bonds"]} if "bonds" in parts else {})}
    disclaimer = ("Ball-and-stick model: atoms drawn smaller than real so bonds show; bond lengths "
                  "and angles are from the structure data." if representation == "ball_and_stick"
                  else "Space-filling model: atoms at their van der Waals radii." if representation == "space_filling"
                  else "Stick model: bonds only.")
    return SceneModel(model_id=entry.id, title=entry.name, parts=parts, units="Å",
                      disclaimer=disclaimer,
                      extras={"front": "+z", "molecule": molecule, "representation": representation,
                              "groups": groups, "facts": molecule.facts(), "zoom": 0.8})


def comparison_scene(entries: list, spacing: float = 2.5) -> SceneModel:
    """Two or more molecules side by side, each labelled with its shape and angle."""
    parts: dict[str, ScenePart] = {}
    labels, facts, offset, groups = [], {}, 0.0, {}
    from visualization.scene import SceneLabel
    for entry in entries:
        molecule = molecule_for_entry(entry)
        pos = molecule.coords - molecule.coords.mean(axis=0)
        width = np.ptp(pos[:, 0]) if len(pos) > 1 else 1.0
        pos[:, 0] += offset + width / 2
        stem = entry.id.rsplit(".", 1)[-1]
        sub = build_parts(molecule, "ball_and_stick", pos)
        for pid, part in sub.items():
            part.id = f"{stem}_{pid}"
            part.name = f"{entry.name}: {part.name}"
            parts[part.id] = part
        groups[stem] = [f"{stem}_{p}" for p in sub]
        f = molecule.facts()
        facts[entry.id] = f
        shape = f.get("geometry", {}).get("shape", "")
        angle = f["bond_angles"][0]["degrees"] if f.get("bond_angles") else None
        text = f"{entry.name}: {shape}" + (f", {angle:.1f}°" if angle else "")
        labels.append(SceneLabel(text, (float(pos[:, 0].mean()), float(pos[:, 1].min()) - 1.2, 0.0), f"cmp_{stem}"))
        offset += width + spacing
    title = " vs ".join(e.name for e in entries)
    return SceneModel(model_id="comparison:" + "+".join(e.id for e in entries), title=title, parts=parts,
                      units="Å", labels=labels, disclaimer="Ball-and-stick models from structure data.",
                      extras={"front": "+z", "groups": groups, "facts": facts})
