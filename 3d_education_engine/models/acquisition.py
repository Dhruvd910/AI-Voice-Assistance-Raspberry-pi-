"""The acquisition pipeline, and the rule for when a requested model does not exist.

    search approved sources -> report what exists -> check the licence ->
    download originals -> convert -> write manifest -> register -> index ->
    update ATTRIBUTIONS.md

Used by the scripts in scripts/ and, for chemistry only, at run time: a
molecule the student names that is not in the library can be fetched from
PubChem on the spot, because PubChem's licence position is known in advance
and the geometry is computed locally from its data. Anatomy is never fetched
silently at run time -- a person runs the pipeline and checks the result.

When nothing suitable exists (rule 28 of the spec), `plan_for_missing`
returns what was searched and what can be done instead; it never invents a
download URL.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from database.repository import Repository
from models.downloader import (PubChemAdapter, RCSBAdapter, SourceAsset, SourceUnavailable,
                               adapter)
from models.provenance import write_attributions
from models.registry import ModelEntry, ModelRegistry
from models.validator import sha256_of, validate_manifest

log = logging.getLogger(__name__)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "item"


def record_files(manifest: dict, model_dir: Path) -> None:
    manifest["files"] = {str(p.relative_to(model_dir)): sha256_of(p)
                         for p in sorted(model_dir.rglob("*"))
                         if p.is_file() and p.name != "manifest.json"}


def write_manifest(manifest: dict, model_dir: Path) -> Path:
    problems = validate_manifest(manifest)
    if problems:
        raise ValueError(f"{manifest.get('id')}: " + "; ".join(problems))
    path = model_dir / "metadata" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def refresh_attributions(repo: Repository, registry: ModelRegistry, path: Path) -> None:
    registry.sync(repo)
    write_attributions(repo.provenance(), path)


# ------------------------------------------------------------------ chemistry
def molecule_manifest(asset: SourceAsset, model_id: str, sdf_rel: str, aliases: list[str],
                      topics: list[str], grades: list[str], reference: dict | None,
                      attribution: str, name: str | None = None) -> dict:
    formula = asset.extra.get("formula") or ""
    display = name or asset.title
    if display.islower():
        display = display.title()
    return {
        "id": model_id,
        "name": display,
        "domain": "chemistry", "subject": model_id.split(".")[1], "kind": "molecule",
        "structure": {"sdf_file": sdf_rel, "smiles": asset.extra.get("smiles"),
                      "pubchem_cid": int(asset.source_id), "formula": formula,
                      "representation": "ball_and_stick", **({"reference": reference} if reference else {})},
        "source": asset.provenance(attribution, "computed-from-data").to_manifest(),
        "educational": {"grades": grades, "topics": topics or [asset.title.lower()], "scale_level": "molecule",
                        "disclaimer": None},
        "aliases": sorted({*aliases, asset.title.lower(), *([formula] if formula else [])}),
        "parts": {},
        "capabilities": ["rotate", "zoom", "hide", "show", "highlight", "transparent", "clip", "focus",
                         "label", "measure", "bond_angle", "representation"],
    }


def acquire_pubchem_molecule(query: str, model_id: str | None, assets_dir: Path, cache_dir: Path,
                             aliases: list[str] | None = None, topics: list[str] | None = None,
                             grades: list[str] | None = None, reference: dict | None = None,
                             name: str | None = None) -> tuple[dict, Path]:
    """Look up `query` on PubChem, store its SDF and write a manifest. -> (manifest, path)."""
    pubchem = PubChemAdapter(cache_dir)
    hits = pubchem.search(query, limit=1)
    if not hits:
        raise LookupError(f"PubChem has no compound called {query!r}")
    asset = hits[0]
    stem = slug(name or query)
    model_id = model_id or f"chemistry.molecule.{stem}"
    model_dir = assets_dir / "chemistry" / model_id.rsplit(".", 1)[-1]
    files = pubchem.download(asset, model_dir / "original")
    manifest = molecule_manifest(asset, model_id, str(files[0].relative_to(model_dir)),
                                 aliases or [query], topics or [], grades or ["8", "9", "10", "11", "12"],
                                 reference, pubchem.attribution_for(asset), name)
    record_files(manifest, model_dir)
    manifest["generated_on"] = date.today().isoformat()
    return manifest, write_manifest(manifest, model_dir)


def acquire_rcsb_structure(pdb_id: str, assets_dir: Path, cache_dir: Path, name: str | None = None) -> tuple[dict, Path]:
    """A protein structure from RCSB PDB, kept as mmCIF (visualised by chemistry tools later)."""
    rcsb = RCSBAdapter(cache_dir)
    asset = rcsb.metadata(pdb_id)
    model_id = f"biology.protein.{slug(name or asset.source_id)}"
    model_dir = assets_dir / "biology" / "proteins" / slug(asset.source_id)
    files = rcsb.download(asset, model_dir / "original")
    manifest = {
        "id": model_id, "name": name or asset.title.title(), "domain": "biology", "subject": "protein",
        "kind": "molecule",
        "structure": {"mmcif_file": str(files[0].relative_to(model_dir)), "pdb_id": asset.source_id,
                      "representation": "backbone"},
        "source": asset.provenance(rcsb.attribution_for(asset), "original").to_manifest(),
        "educational": {"grades": ["11", "12"], "topics": [asset.title.lower()], "scale_level": "molecule"},
        "aliases": [asset.source_id.lower(), *( [name.lower()] if name else [])], "parts": {},
        "capabilities": ["rotate", "zoom", "hide", "show", "highlight", "transparent", "focus", "representation"],
    }
    record_files(manifest, model_dir)
    return manifest, write_manifest(manifest, model_dir)


# ------------------------------------------------------------------ the missing-model rule
@dataclass
class MissingPlan:
    query: str
    searched: dict[str, str] = field(default_factory=dict)       # source -> outcome
    candidates: list[SourceAsset] = field(default_factory=list)
    procedural: str | None = None                                # a generator that could stand in
    acquired: ModelEntry | None = None

    def message(self) -> str:
        if self.acquired:
            return f"Fetched {self.acquired.name} from {self.acquired.source['provider']} and added it to the library."
        lines = [f"I don't have a 3D model of '{self.query}' yet."]
        if self.candidates:
            best = self.candidates[0]
            lines.append(f"{best.source} has '{best.title}' ({best.license or 'licence to be confirmed'}); "
                         f"it can be added with: python scripts/download_models.py --source {best.source} "
                         f"--query \"{self.query}\"")
        if self.procedural:
            lines.append(f"I can generate a simplified educational model instead ({self.procedural}).")
        if "network" in self.searched:
            lines.append("I'm offline, so I couldn't search the approved sources.")
        elif not self.candidates and not self.procedural:
            lines.append("None of the approved sources has one, so a source model is required.")
        return " ".join(lines)


def plan_for_missing(query: str, domain_hint: str | None, registry: ModelRegistry, repo: Repository,
                     assets_dir: Path, cache_dir: Path, attributions_path: Path,
                     allow_fetch_chemistry: bool = True, offline: bool = False) -> MissingPlan:
    plan = MissingPlan(query)
    if offline:
        plan.searched["network"] = "offline: no sources searched"
        return plan
    looks_chemical = domain_hint == "chemistry" or bool(re.fullmatch(r"(?:[A-Z][a-z]?\d*){1,10}", query.strip()))
    if domain_hint in (None, "chemistry") or looks_chemical:
        try:
            hits = PubChemAdapter(cache_dir).search(query, limit=1)
            plan.searched["PubChem"] = f"{len(hits)} match(es)"
            if hits and allow_fetch_chemistry:
                manifest, path = acquire_pubchem_molecule(query, None, assets_dir, cache_dir)
                plan.acquired = registry.add(manifest, path)
                refresh_attributions(repo, registry, attributions_path)
                return plan
            plan.candidates += hits
        except (SourceUnavailable, LookupError, OSError) as exc:
            plan.searched["PubChem"] = f"unavailable: {exc}"
    if domain_hint in (None, "biology"):
        try:
            hits = adapter("BodyParts3D", cache_dir).search(query, limit=3)
            plan.searched["BodyParts3D"] = f"{len(hits)} match(es)"
            plan.candidates += hits
        except (SourceUnavailable, OSError) as exc:
            plan.searched["BodyParts3D"] = f"unavailable: {exc}"
        plan.searched["NIH_3D"] = "no public search API; look it up on 3d.nih.gov/discover"
    return plan
