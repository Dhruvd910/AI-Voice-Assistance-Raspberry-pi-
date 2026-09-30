"""Elements, electron configurations, and the shell ("Bohr") teaching model.

Numbers come from RDKit's periodic table (atomic weight, radii, isotopes), not
from a table typed in here. The electron configuration is worked out with the
Aufbau (Madelung) order, with the two real exceptions below 37 (Cr and Cu).

THE SHELL MODEL IS A TEACHING MODEL. Electrons do not travel round the
nucleus on circular paths; they occupy orbitals, regions where they are
likely to be found. Every scene built here says so on screen.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyvista as pv
from rdkit import Chem

from visualization.scene import SceneModel, ScenePart

SHELL_DISCLAIMER = ("Educational model — not to scale. Electrons do not orbit on fixed circular "
                    "paths; they occupy orbitals (regions of probability). The nucleus is drawn "
                    "thousands of times too big compared with the atom.")

# Jmol / CPK colours, the convention most textbooks and viewers use.
CPK = {"H": "#ffffff", "He": "#d9ffff", "C": "#909090", "N": "#3050f8", "O": "#ff0d0d",
       "F": "#90e050", "Na": "#ab5cf2", "Mg": "#8aff00", "P": "#ff8000", "S": "#ffff30",
       "Cl": "#1ff01f", "K": "#8f40d4", "Ca": "#3dff00", "Fe": "#e06633", "Cu": "#c88033"}
NAMES = {1: "Hydrogen", 2: "Helium", 3: "Lithium", 4: "Beryllium", 5: "Boron", 6: "Carbon",
         7: "Nitrogen", 8: "Oxygen", 9: "Fluorine", 10: "Neon", 11: "Sodium", 12: "Magnesium",
         13: "Aluminium", 14: "Silicon", 15: "Phosphorus", 16: "Sulfur", 17: "Chlorine",
         18: "Argon", 19: "Potassium", 20: "Calcium", 26: "Iron", 29: "Copper", 30: "Zinc"}

MADELUNG = ["1s", "2s", "2p", "3s", "3p", "4s", "3d", "4p", "5s", "4d", "5p", "6s", "4f", "5d", "6p", "7s"]
CAPACITY = {"s": 2, "p": 6, "d": 10, "f": 14}
EXCEPTIONS = {24: {"3d": 5, "4s": 1}, 29: {"3d": 10, "4s": 1}}

_PT = Chem.GetPeriodicTable()


@dataclass(frozen=True)
class Element:
    z: int
    symbol: str
    name: str
    weight: float
    mass_number: int
    covalent_radius: float     # Å
    vdw_radius: float          # Å
    outer_electrons: int
    color: str

    @property
    def neutrons(self) -> int:
        return self.mass_number - self.z


def element(symbol_or_z) -> Element:
    z = symbol_or_z if isinstance(symbol_or_z, int) else _PT.GetAtomicNumber(str(symbol_or_z))
    if z <= 0:
        raise ValueError(f"unknown element {symbol_or_z!r}")
    symbol = _PT.GetElementSymbol(z)
    return Element(z=z, symbol=symbol, name=NAMES.get(z, symbol), weight=_PT.GetAtomicWeight(z),
                   mass_number=_PT.GetMostCommonIsotope(z), covalent_radius=_PT.GetRcovalent(z),
                   vdw_radius=_PT.GetRvdw(z), outer_electrons=_PT.GetNOuterElecs(z),
                   color=CPK.get(symbol, "#ff1493"))


def subshells(z: int) -> dict[str, int]:
    """{'1s': 2, '2s': 2, '2p': 2} for carbon. Supported up to krypton (Z=36)."""
    if not 1 <= z <= 36:
        raise ValueError("electron configurations are provided for Z = 1 to 36")
    left, out = z, {}
    for orbital in MADELUNG:
        if left == 0:
            break
        n = min(left, CAPACITY[orbital[-1]])
        out[orbital] = n
        left -= n
    if z in EXCEPTIONS:
        out.update(EXCEPTIONS[z])
    return {k: v for k, v in out.items() if v}


def configuration(z: int) -> str:
    """'1s2 2s2 2p2', written in order of shell number."""
    subs = subshells(z)
    order = sorted(subs, key=lambda o: (int(o[0]), "spdf".index(o[1])))
    return " ".join(f"{o}{subs[o]}" for o in order)


def shells(z: int) -> list[int]:
    """Electrons per shell (n = 1, 2, 3 ...): carbon [2, 4], sodium [2, 8, 1], iron [2, 8, 14, 2]."""
    counts: dict[int, int] = {}
    for orbital, n in subshells(z).items():
        counts[int(orbital[0])] = counts.get(int(orbital[0]), 0) + n
    return [counts[k] for k in sorted(counts)]


def shell_scene(model_id: str, title: str, symbol: str) -> SceneModel:
    """Nucleus of protons and neutrons, rings for shells, electrons as dots."""
    el = element(symbol)
    rng = np.random.default_rng(el.z)
    nucleons = el.z + el.neutrons
    # Pack the nucleons into a rough ball (a teaching picture, not nuclear physics).
    r_nuc = 0.35 * max(1, nucleons) ** (1 / 3)
    pts = []
    while len(pts) < nucleons:
        p = rng.uniform(-r_nuc, r_nuc, 3)
        if np.linalg.norm(p) <= r_nuc:
            pts.append(p)
    pts = np.array(pts) if pts else np.zeros((0, 3))
    ball = pv.Sphere(radius=0.3, theta_resolution=12, phi_resolution=12)
    protons = pv.PolyData(pts[: el.z]).glyph(geom=ball, orient=False, scale=False)
    parts = [ScenePart("protons", f"Protons ({el.z})", protons, "#e04040")]
    if el.neutrons:
        parts.append(ScenePart("neutrons", f"Neutrons ({el.neutrons})",
                               pv.PolyData(pts[el.z:]).glyph(geom=ball, orient=False, scale=False), "#a0a0a0"))
    electron_ids, shell_ids = [], []
    for n, count in enumerate(shells(el.z), start=1):
        radius = 1.2 + 1.3 * n
        ring = pv.Circle(radius=radius, resolution=96)
        ring = ring.extract_feature_edges(boundary_edges=True, feature_edges=False, manifold_edges=False)
        parts.append(ScenePart(f"shell_{n}", f"Shell {n} ({count} electron{'s' if count != 1 else ''})",
                               ring.tube(radius=0.03), "#6f8fb0", smooth_shading=True))
        angles = np.linspace(0, 2 * np.pi, count, endpoint=False) + (0.3 * n)
        e_pts = np.column_stack([radius * np.cos(angles), radius * np.sin(angles), np.zeros(count)])
        dots = pv.PolyData(e_pts).glyph(geom=pv.Sphere(radius=0.18), orient=False, scale=False)
        parts.append(ScenePart(f"electrons_{n}", f"Electrons in shell {n}", dots, "#4fb0ff"))
        electron_ids.append(f"electrons_{n}")
        shell_ids.append(f"shell_{n}")
    groups = {"electrons": electron_ids, "shells": shell_ids,
              "nucleus": ["protons"] + (["neutrons"] if el.neutrons else []),
              "valence_electrons": [electron_ids[-1]], "outer_shell": [shell_ids[-1], electron_ids[-1]]}
    facts = {"symbol": el.symbol, "atomic_number": el.z, "mass_number": el.mass_number,
             "protons": el.z, "neutrons": el.neutrons, "electrons": el.z,
             "shells": shells(el.z), "configuration": configuration(el.z),
             "valence_electrons": shells(el.z)[-1] if el.z > 2 else el.z,
             "atomic_weight": round(el.weight, 3)}
    return SceneModel(model_id=model_id, title=title, parts={p.id: p for p in parts},
                      units="not to scale", disclaimer=SHELL_DISCLAIMER,
                      extras={"front": "+z", "groups": groups, "atom": facts,
                              "group_aliases": {"nucleus": ["center", "centre", "core"],
                                                "valence_electrons": ["outer electrons", "valence electrons"],
                                                "electrons": ["electron", "electron cloud"]}})
