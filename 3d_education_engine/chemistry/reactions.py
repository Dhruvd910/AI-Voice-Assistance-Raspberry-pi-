"""Chemical reactions as atoms that move, with every atom accounted for.

A reaction is written as ATOM-MAPPED SMILES: each atom carries a number, and
the same number on the product side is the same atom. That is what lets the
animation show bonds breaking, atoms rearranging and new bonds forming --
and it is checked: an equation that creates or destroys an atom is refused.

    CH4 + 2 O2 -> CO2 + 2 H2O

    reactants -> bond breaking -> atom rearrangement -> bond formation -> products
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np
import pyvista as pv
from rdkit import Chem

from chemistry import rdkit_engine as rk
from chemistry.atoms import element
from visualization.scene import SceneLabel, SceneModel, ScenePart

STAGES = [(0.0, "Reactants"), (0.18, "Bond breaking"), (0.38, "Atom rearrangement"),
          (0.72, "Bond formation"), (0.9, "Products")]


@dataclass
class Reaction:
    id: str
    name: str
    equation: str
    mapped_smiles: str        # "reactant.reactant>>product.product", every atom mapped
    energy_note: str = ""


METHANE_COMBUSTION = Reaction(
    id="methane_combustion",
    name="Combustion of methane",
    equation="CH4 + 2O2 → CO2 + 2H2O",
    mapped_smiles=("[C:1]([H:2])([H:3])([H:4])[H:5].[O:6]=[O:7].[O:8]=[O:9]"
                   ">>[O:6]=[C:1]=[O:8].[H:2][O:7][H:3].[H:4][O:9][H:5]"),
    energy_note=("Exothermic: about 890 kJ of energy is released per mole of methane "
                 "(with the water formed as liquid)."),
)
REACTIONS = {r.id: r for r in [METHANE_COMBUSTION]}


def _side(smiles: str, spacing: float = 4.0) -> tuple[dict[int, np.ndarray], dict[int, str], set[tuple[int, int, int]]]:
    """Map number -> position / element, and bonds as (map_a, map_b, order)."""
    positions: dict[int, np.ndarray] = {}
    elements: dict[int, str] = {}
    bonds: set[tuple[int, int, int]] = set()
    offset = 0.0
    for part in smiles.split("."):
        mol = rk.from_smiles(part, keep_explicit_h=True)
        maps = [a.GetAtomMapNum() for a in mol.GetAtoms()]
        if 0 in maps:
            raise ValueError(f"every atom must be mapped: {part}")
        pos = rk.coordinates(mol)
        pos -= pos.mean(axis=0)
        width = float(np.ptp(pos[:, 0])) if len(pos) > 1 else 0.0
        pos[:, 0] += offset + width / 2
        offset += width + spacing
        for atom, p in zip(mol.GetAtoms(), pos):
            positions[atom.GetAtomMapNum()] = p
            elements[atom.GetAtomMapNum()] = atom.GetSymbol()
        for b in mol.GetBonds():
            a, c = sorted((b.GetBeginAtom().GetAtomMapNum(), b.GetEndAtom().GetAtomMapNum()))
            bonds.add((a, c, int(round(b.GetBondTypeAsDouble()))))
    centre = np.mean(list(positions.values()), axis=0)
    return {k: v - centre for k, v in positions.items()}, elements, bonds


def check_conservation(reaction: Reaction) -> None:
    left, right = reaction.mapped_smiles.split(">>")
    l_el = {a.GetAtomMapNum(): a.GetSymbol() for s in left.split(".") for a in Chem.MolFromSmiles(s, _keep_h()).GetAtoms()}
    r_el = {a.GetAtomMapNum(): a.GetSymbol() for s in right.split(".") for a in Chem.MolFromSmiles(s, _keep_h()).GetAtoms()}
    if set(l_el) != set(r_el):
        raise ValueError(f"{reaction.id}: atoms {sorted(set(l_el) ^ set(r_el))} appear on one side only")
    if any(l_el[k] != r_el[k] for k in l_el):
        raise ValueError(f"{reaction.id}: an atom changes element")
    if Counter(l_el.values()) != Counter(r_el.values()):
        raise ValueError(f"{reaction.id}: equation is not balanced")


def _keep_h():
    p = Chem.SmilesParserParams()
    p.removeHs = False
    return p


class ReactionModel:
    def __init__(self, reaction: Reaction):
        check_conservation(reaction)
        self.reaction = reaction
        left, right = reaction.mapped_smiles.split(">>")
        self.start, self.elements, self.bonds_before = _side(left)
        self.end, _, self.bonds_after = _side(right)
        pair = lambda bonds: {(a, b): o for a, b, o in bonds}
        self.before, self.after = pair(self.bonds_before), pair(self.bonds_after)
        self.kept = {k for k in self.before if k in self.after and self.before[k] == self.after[k]}
        self.breaking = set(self.before) - self.kept
        self.forming = set(self.after) - self.kept

    def stage(self, t: float) -> str:
        return [name for start, name in STAGES if t >= start][-1]

    def positions(self, t: float) -> dict[int, np.ndarray]:
        x = np.clip((t - 0.38) / (0.72 - 0.38), 0.0, 1.0)
        x = 0.5 - 0.5 * np.cos(np.pi * x)
        return {k: (1 - x) * self.start[k] + x * self.end[k] for k in self.start}

    def parts(self, t: float) -> dict[str, ScenePart]:
        pos = self.positions(t)
        parts: dict[str, ScenePart] = {}
        for sym in sorted(set(self.elements.values())):
            el = element(sym)
            ids = [k for k, s in self.elements.items() if s == sym]
            sphere = pv.Sphere(radius=el.vdw_radius * 0.3, theta_resolution=20, phi_resolution=20)
            mesh = pv.PolyData(np.array([pos[k] for k in ids])).glyph(geom=sphere, orient=False, scale=False)
            pid = f"{el.name.lower()}_atoms"
            parts[pid] = ScenePart(pid, f"{el.name} atoms ({len(ids)})", mesh, el.color)
        breaking = 1.0 - np.clip((t - 0.18) / 0.2, 0.0, 1.0)      # shrink away
        forming = np.clip((t - 0.72) / 0.18, 0.0, 1.0)            # grow in
        for pid, keys, table, factor, color in (
                ("bonds_kept", self.kept, self.before, 1.0, "#b8b8b8"),
                ("bonds_breaking", self.breaking, self.before, breaking, "#ff9f43"),
                ("bonds_forming", self.forming, self.after, forming, "#4fd1c5")):
            cyl = [_bond(pos[a], pos[b], 0.11 * max(factor, 0.02)) for a, b in keys]
            mesh = pv.merge(cyl) if len(cyl) > 1 else (cyl[0] if cyl else pv.Sphere(radius=1e-4))
            name = {"bonds_kept": "Bonds that stay", "bonds_breaking": "Bonds breaking",
                    "bonds_forming": "Bonds forming"}[pid]
            parts[pid] = ScenePart(pid, name, mesh, color)
        return parts


def _bond(a, b, radius) -> pv.PolyData:
    axis = np.asarray(b) - np.asarray(a)
    return pv.Cylinder(center=(np.asarray(a) + np.asarray(b)) / 2, direction=axis, radius=radius,
                       height=float(np.linalg.norm(axis)), resolution=12).triangulate()


class ReactionAnimator:
    """Plays the reaction over `duration` seconds, then holds on the products."""

    name = "play"

    def __init__(self, model: ReactionModel, duration: float = 8.0):
        self.model, self.duration, self.t = model, duration, 0.0

    def tick(self, dt: float, engine) -> None:
        self.t = min(self.duration, self.t + dt)
        self._apply(engine)

    def _apply(self, engine) -> None:
        x = self.t / self.duration
        for pid, part in self.model.parts(x).items():
            engine.update_part_mesh(pid, part.mesh)
        engine.annotations.add_label(self.model.stage(x), (0.0, 4.5, 0.0), label_id="stage")
        engine.dirty = True

    def reset(self, engine) -> None:
        self.t = 0.0
        self._apply(engine)


def reaction_scene(entry) -> SceneModel:
    reaction = REACTIONS[entry.manifest["generator"]["params"]["reaction"]]
    model = ReactionModel(reaction)
    parts = model.parts(0.0)
    return SceneModel(
        model_id=entry.id, title=f"{reaction.name}: {reaction.equation}", parts=parts, units="Å",
        disclaimer=("Educational animation — real molecules collide and react in many steps; "
                    "atom identities are tracked exactly."),
        labels=[SceneLabel("Reactants", (0.0, 4.5, 0.0), "stage")], view=(0.0, 20.0),
        animations={"play": lambda: ReactionAnimator(model)},
        extras={"front": "+z", "zoom": 1.3, "reaction": reaction, "reaction_model": model,
                "groups": {"atoms": [p for p in parts if p.endswith("_atoms")],
                           "bonds": ["bonds_kept", "bonds_breaking", "bonds_forming"]},
                "facts": {"equation": reaction.equation, "energy": reaction.energy_note,
                          "atoms_conserved": dict(Counter(model.elements.values()))}})


GENERATORS = {"chemistry.reactions.reaction": reaction_scene}
