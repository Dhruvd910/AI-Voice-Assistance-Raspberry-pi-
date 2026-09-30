"""RDKit: structures in, 3D coordinates and chemistry facts out.

SMILES / SDF / MOL / a PubChem 3D conformer -> an RDKit Mol with hydrogens and
3D coordinates. Geometry from PubChem's computed conformer is used as-is;
geometry generated here (ETKDG + MMFF) is labelled as generated.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem, Descriptors, rdMolDescriptors

RDLogger.DisableLog("rdApp.warning")

SHAPES = {
    (1, 0): "linear", (1, 1): "linear", (1, 2): "linear", (1, 3): "linear",
    (2, 0): "linear", (2, 1): "bent", (2, 2): "bent", (2, 3): "linear",
    (3, 0): "trigonal planar", (3, 1): "trigonal pyramidal", (3, 2): "T-shaped",
    (4, 0): "tetrahedral", (4, 1): "seesaw", (4, 2): "square planar",
    (5, 0): "trigonal bipyramidal", (6, 0): "octahedral",
}
IDEAL_ANGLES = {"linear": 180.0, "trigonal planar": 120.0, "tetrahedral": 109.5,
                "trigonal bipyramidal": 90.0, "octahedral": 90.0}


class StructureError(ValueError):
    pass


def from_smiles(smiles: str, seed: int = 42, keep_explicit_h: bool = False) -> Chem.Mol:
    if keep_explicit_h:
        params = Chem.SmilesParserParams()
        params.removeHs = False
        mol = Chem.MolFromSmiles(smiles, params)
    else:
        mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise StructureError(f"RDKit could not parse SMILES {smiles!r}")
    mol = Chem.AddHs(mol)
    return embed(mol, seed)


def embed(mol: Chem.Mol, seed: int = 42) -> Chem.Mol:
    if mol.GetNumAtoms() == 1:
        conf = Chem.Conformer(1)
        conf.SetAtomPosition(0, (0.0, 0.0, 0.0))
        mol.AddConformer(conf, assignId=True)
        return mol
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        raise StructureError("RDKit could not generate 3D coordinates")
    if AllChem.MMFFHasAllMoleculeParams(mol):
        AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
    else:
        AllChem.UFFOptimizeMolecule(mol, maxIters=500)
    return mol


def from_sdf(path: Path) -> Chem.Mol:
    supplier = Chem.SDMolSupplier(str(path), removeHs=False)
    mol = next(iter(supplier), None)
    if mol is None:
        raise StructureError(f"{path}: no molecule in the SDF")
    if mol.GetNumConformers() == 0 or not mol.GetConformer().Is3D():
        mol = embed(Chem.AddHs(mol))
    return mol


def from_molblock(block: str) -> Chem.Mol:
    mol = Chem.MolFromMolBlock(block, removeHs=False)
    if mol is None:
        raise StructureError("RDKit could not parse the MOL block")
    return mol if mol.GetConformer().Is3D() else embed(Chem.AddHs(mol))


def coordinates(mol: Chem.Mol) -> np.ndarray:
    return np.array(mol.GetConformer().GetPositions(), dtype=float)


def properties(mol: Chem.Mol) -> dict:
    return {"formula": rdMolDescriptors.CalcMolFormula(mol),
            "molecular_weight": round(Descriptors.MolWt(mol), 3),
            "atoms": mol.GetNumAtoms(), "bonds": mol.GetNumBonds(),
            "smiles": Chem.MolToSmiles(Chem.RemoveHs(mol))}


def central_atom(mol: Chem.Mol) -> int:
    """The atom with the most neighbours (heaviest on ties)."""
    return max(range(mol.GetNumAtoms()),
               key=lambda i: (mol.GetAtomWithIdx(i).GetDegree(), mol.GetAtomWithIdx(i).GetAtomicNum()))


def lone_pairs(mol: Chem.Mol, idx: int) -> int:
    atom = mol.GetAtomWithIdx(idx)
    valence = Chem.GetPeriodicTable().GetNOuterElecs(atom.GetAtomicNum())
    bonding = sum(int(round(b.GetBondTypeAsDouble())) for b in atom.GetBonds())
    return max(0, (valence - bonding - atom.GetFormalCharge()) // 2)


def vsepr(mol: Chem.Mol, idx: int | None = None) -> dict:
    idx = central_atom(mol) if idx is None else idx
    atom = mol.GetAtomWithIdx(idx)
    bonded, lp = atom.GetDegree(), lone_pairs(mol, idx)
    shape = SHAPES.get((bonded, lp), "complex")
    return {"atom_index": idx, "element": atom.GetSymbol(), "bonded_atoms": bonded, "lone_pairs": lp,
            "electron_domains": bonded + lp, "shape": shape}


def bond_angles(mol: Chem.Mol, idx: int | None = None) -> list[dict]:
    idx = central_atom(mol) if idx is None else idx
    pos = coordinates(mol)
    nbrs = [n.GetIdx() for n in mol.GetAtomWithIdx(idx).GetNeighbors()]
    out = []
    for a in range(len(nbrs)):
        for b in range(a + 1, len(nbrs)):
            i, j = nbrs[a], nbrs[b]
            v1, v2 = pos[i] - pos[idx], pos[j] - pos[idx]
            cos = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
            out.append({"atoms": (i, idx, j),
                        "label": f"{mol.GetAtomWithIdx(i).GetSymbol()}-{mol.GetAtomWithIdx(idx).GetSymbol()}-"
                                 f"{mol.GetAtomWithIdx(j).GetSymbol()}",
                        "degrees": float(np.degrees(np.arccos(np.clip(cos, -1, 1))))})
    return out


def polarity(mol: Chem.Mol) -> str:
    """'polar' / 'non-polar' from Gasteiger charges and geometry: a dipole that
    does not cancel. A teaching-level estimate, stated as such by callers."""
    m = Chem.Mol(mol)
    AllChem.ComputeGasteigerCharges(m)
    q = np.array([float(a.GetProp("_GasteigerCharge")) for a in m.GetAtoms()])
    if not np.all(np.isfinite(q)):
        return "unknown"
    dipole = np.linalg.norm((q[:, None] * coordinates(m)).sum(axis=0))
    return "polar" if dipole > 0.1 else "non-polar"
