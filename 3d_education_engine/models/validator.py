"""Manifest validation. Returns a list of problems; empty means valid.

Hand-written rather than JSON Schema so the messages say what is wrong in
terms a person adding a model can act on, and so there is no extra dependency
on the Pi.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from models.provenance import (KNOWN_LICENSES, MODIFICATION_STATUSES, PROVENANCE_FIELDS,
                               LicenseError, check_license)

DOMAINS = {"biology", "chemistry", "physics"}
KINDS = {"asset", "procedural", "molecule", "simulation"}
ASSET_FORMATS = {"glb", "gltf", "stl", "obj", "ply", "vtp"}
CAPABILITIES = {"rotate", "zoom", "hide", "show", "highlight", "transparent", "clip",
                "explode", "focus", "label", "measure", "animate", "simulate", "bond_angle",
                "electron_shells", "representation"}
RE_ID = re.compile(r"^(biology|chemistry|physics)(\.[a-z0-9_]+){2,}$")
RE_PART = re.compile(r"^[a-z0-9_]+$")


def validate_manifest(m: dict, accepted_licenses: set[str] | None = None) -> list[str]:
    errors: list[str] = []

    def need(key: str, kind: type, where: dict = m, prefix: str = "") -> bool:
        if key not in where:
            errors.append(f"{prefix}{key}: missing")
            return False
        if not isinstance(where[key], kind):
            errors.append(f"{prefix}{key}: expected {kind.__name__}")
            return False
        return True

    for key in ("id", "name", "domain", "subject", "kind"):
        need(key, str)
    if errors:
        return errors
    if not RE_ID.match(m["id"]):
        errors.append(f"id: {m['id']!r} must look like domain.subject.name (lower case, dotted)")
    elif m["id"].split(".")[0] != m["domain"]:
        errors.append(f"id: must start with its domain {m['domain']!r}")
    if m["domain"] not in DOMAINS:
        errors.append(f"domain: {m['domain']!r} not one of {sorted(DOMAINS)}")
    if m["kind"] not in KINDS:
        errors.append(f"kind: {m['kind']!r} not one of {sorted(KINDS)}")

    # -- provenance: every field, every time
    if need("source", dict):
        errors += _provenance_errors(m["source"], "source.", accepted_licenses)
    for i, extra in enumerate(m.get("additional_sources", [])):
        errors += _provenance_errors(extra, f"additional_sources[{i}].", accepted_licenses)

    # -- educational metadata
    if need("educational", dict):
        edu = m["educational"]
        if need("grades", list, edu, "educational.") and not all(isinstance(g, str) for g in edu["grades"]):
            errors.append("educational.grades: must be strings")
        if need("topics", list, edu, "educational.") and not edu["topics"]:
            errors.append("educational.topics: at least one topic is needed for search")

    # -- what the model is made of
    kind = m.get("kind")
    if kind == "asset" and need("asset", dict):
        asset = m["asset"]
        if need("file", str, asset, "asset."):
            fmt = asset.get("format", Path(asset["file"]).suffix.lstrip("."))
            if fmt not in ASSET_FORMATS:
                errors.append(f"asset.format: {fmt!r} not one of {sorted(ASSET_FORMATS)}")
        for lod, path in asset.get("lods", {}).items():
            if not isinstance(path, str):
                errors.append(f"asset.lods.{lod}: must be a path")
    if kind == "procedural" and need("generator", dict):
        need("name", str, m["generator"], "generator.")
    if kind == "molecule" and need("structure", dict):
        s = m["structure"]
        if not any(s.get(k) for k in ("smiles", "sdf_file", "mol_file", "pubchem_cid", "element", "mmcif_file")):
            errors.append("structure: needs smiles, sdf_file, mol_file, mmcif_file, pubchem_cid or element")
    if kind == "simulation" and need("simulation", dict):
        need("name", str, m["simulation"], "simulation.")

    parts = m.get("parts", {})
    if not isinstance(parts, dict):
        errors.append("parts: expected an object keyed by part id")
        parts = {}
    for part_id, part in parts.items():
        if not RE_PART.match(part_id):
            errors.append(f"parts.{part_id}: ids are lower_snake_case")
        if not isinstance(part, dict) or not isinstance(part.get("name"), str):
            errors.append(f"parts.{part_id}: needs a name")
    for group, members in m.get("groups", {}).items():
        for p in members:
            if p not in parts:
                errors.append(f"groups.{group}: unknown part {p!r}")
    for cap in m.get("capabilities", []):
        if cap not in CAPABILITIES:
            errors.append(f"capabilities: unknown capability {cap!r}")
    rel = m.get("relationships", {})
    for key in ("parent", "scale_up_to", "scale_down_to"):
        if key in rel and not isinstance(rel[key], (str, type(None))):
            errors.append(f"relationships.{key}: expected a model id")
    for key in ("inside", "related"):
        if key in rel and not isinstance(rel[key], list):
            errors.append(f"relationships.{key}: expected a list")
    # "Zoom into the nucleus": a part of this model that opens into a model of
    # its own. The part must be one of this model's; the target is checked
    # against the registry when it is used, since it may be registered later.
    part_models = rel.get("part_models", {})
    if not isinstance(part_models, dict):
        errors.append("relationships.part_models: expected {part id: model id}")
    else:
        for part, target in part_models.items():
            if not isinstance(target, str):
                errors.append(f"relationships.part_models.{part}: expected a model id")
            elif m.get("parts") and part not in m["parts"]:
                errors.append(f"relationships.part_models: {part!r} is not a part of this model")
    # The parts named when the model appears, and those whose names ride on
    # them rather than sitting in a column. A simulation builds its parts when
    # it loads, so they can only be checked against a manifest that lists them.
    for key in ("default_labels", "riding_labels"):
        labels = m.get(key)
        if labels is not None and (not isinstance(labels, list) or not all(isinstance(p, str) for p in labels)
                                   or (m.get("parts") and any(p not in m["parts"] for p in labels))):
            errors.append(f"{key}: expected a list of this model's part ids")
    return errors


def _provenance_errors(src: dict, prefix: str, accepted: set[str] | None) -> list[str]:
    errors = []
    for field in PROVENANCE_FIELDS:
        value = src.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}{field}: missing (provenance is mandatory)")
    if errors:
        return errors
    try:
        lic = check_license(src["license"], accepted)
        if src["license_url"] != lic.url and src["license"] in KNOWN_LICENSES:
            errors.append(f"{prefix}license_url: {src['license_url']!r} does not match "
                          f"{src['license']} ({lic.url})")
    except LicenseError as exc:
        errors.append(f"{prefix}license: {exc}")
    try:
        date.fromisoformat(src["download_date"])
    except ValueError:
        errors.append(f"{prefix}download_date: not an ISO date")
    if src["modification_status"] not in MODIFICATION_STATUSES:
        errors.append(f"{prefix}modification_status: {src['modification_status']!r} not one of "
                      f"{sorted(MODIFICATION_STATUSES)}")
    return errors


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def validate_files(m: dict, model_dir: Path) -> list[str]:
    """Files named by the manifest must exist and match their recorded hashes."""
    errors = []
    if m.get("kind") == "asset":
        asset = m.get("asset", {})
        paths = [asset.get("file")] + list(asset.get("lods", {}).values())
        for rel in filter(None, paths):
            if not (model_dir / rel).is_file():
                errors.append(f"asset file missing: {model_dir / rel}")
    if m.get("kind") == "molecule":
        for key in ("sdf_file", "mol_file", "mmcif_file"):
            rel = m.get("structure", {}).get(key)
            if rel and not (model_dir / rel).is_file():
                errors.append(f"structure file missing: {model_dir / rel}")
    for rel, digest in m.get("files", {}).items():
        path = model_dir / rel
        if not path.is_file():
            errors.append(f"recorded file missing: {path}")
        elif sha256_of(path) != digest:
            errors.append(f"checksum mismatch: {path}")
    return errors
