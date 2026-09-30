"""The Model Registry: every model the engine can show, and nothing else.

Manifests are the source of truth and live in two places:

* next to their files, for anything with stored data:
      assets/<domain>/<name>/metadata/manifest.json
  with original/, processed/ and runtime/ beside metadata/;
* under models/manifests/<domain>/*.json, for models that are generated
  (cells, simulations) and so have no files of their own.

A manifest that fails validation is reported and left out -- it is never
half-loaded -- so a model with missing provenance cannot reach the screen.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from database.repository import Repository, normalise
from models.validator import validate_files, validate_manifest

log = logging.getLogger(__name__)


@dataclass
class ModelEntry:
    manifest: dict
    manifest_path: Path
    model_dir: Path

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def name(self) -> str:
        return self.manifest["name"]

    @property
    def kind(self) -> str:
        return self.manifest["kind"]

    @property
    def parts(self) -> dict[str, dict]:
        return self.manifest.get("parts", {})

    @property
    def groups(self) -> dict[str, list[str]]:
        return self.manifest.get("groups", {})

    @property
    def capabilities(self) -> set[str]:
        return set(self.manifest.get("capabilities", []))

    @property
    def relationships(self) -> dict:
        return self.manifest.get("relationships", {})

    @property
    def disclaimer(self) -> str | None:
        return self.manifest.get("educational", {}).get("disclaimer")

    @property
    def source(self) -> dict:
        return self.manifest["source"]

    def available_parts(self) -> list[str]:
        return [p for p, d in self.parts.items() if d.get("available", True)]


@dataclass
class LoadReport:
    loaded: list[str] = field(default_factory=list)
    rejected: dict[str, list[str]] = field(default_factory=dict)   # path -> problems


class ModelRegistry:
    def __init__(self, manifests_dir: Path, assets_dir: Path, accepted_licenses: set[str] | None = None):
        self.manifests_dir = manifests_dir
        self.assets_dir = assets_dir
        self.accepted_licenses = accepted_licenses
        self._entries: dict[str, ModelEntry] = {}
        self._aliases: dict[str, set[str]] = {}

    # ------------------------------------------------------------ loading
    def manifest_paths(self) -> list[Path]:
        paths = sorted(self.manifests_dir.glob("**/*.json"))
        paths += sorted(self.assets_dir.glob("**/metadata/manifest.json"))
        return paths

    def model_dir_for(self, manifest_path: Path, manifest: dict) -> Path:
        if manifest_path.parent.name == "metadata":
            return manifest_path.parent.parent
        return self.assets_dir / manifest["domain"] / manifest["id"].split(".")[-1]

    def load(self, check_files: bool = True) -> LoadReport:
        report = LoadReport()
        self._entries.clear()
        for path in self.manifest_paths():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                report.rejected[str(path)] = [f"unreadable: {exc}"]
                continue
            problems = validate_manifest(manifest, self.accepted_licenses)
            model_dir = self.model_dir_for(path, manifest) if not problems else path.parent
            if not problems and check_files:
                problems = validate_files(manifest, model_dir)
            if not problems and manifest["id"] in self._entries:
                problems = [f"duplicate id {manifest['id']} (also in "
                            f"{self._entries[manifest['id']].manifest_path})"]
            if problems:
                report.rejected[str(path)] = problems
                log.warning("Rejected %s: %s", path, "; ".join(problems))
                continue
            self._entries[manifest["id"]] = ModelEntry(manifest, path, model_dir)
            report.loaded.append(manifest["id"])
        self._rebuild_aliases()
        return report

    def add(self, manifest: dict, manifest_path: Path) -> ModelEntry:
        """Register a manifest written at run time (e.g. a molecule just fetched)."""
        problems = validate_manifest(manifest, self.accepted_licenses)
        model_dir = self.model_dir_for(manifest_path, manifest)
        problems += validate_files(manifest, model_dir)
        if problems:
            raise ValueError(f"{manifest.get('id')}: " + "; ".join(problems))
        entry = ModelEntry(manifest, manifest_path, model_dir)
        self._entries[entry.id] = entry
        self._rebuild_aliases()
        return entry

    def sync(self, repo: Repository) -> list[str]:
        """Mirror the loaded manifests into SQLite. Returns ids removed as stale."""
        for entry in self._entries.values():
            try:
                rel = str(entry.manifest_path.relative_to(self.assets_dir.parent))
            except ValueError:
                rel = str(entry.manifest_path)
            repo.upsert_model(entry.manifest, rel)
        return repo.delete_models_not_in(set(self._entries))

    def _rebuild_aliases(self) -> None:
        self._aliases = {}
        for entry in self._entries.values():
            m = entry.manifest
            names = {m["name"], m["id"].split(".")[-1], *m.get("aliases", []),
                     *m.get("educational", {}).get("topics", [])}
            for name in names:
                if name:
                    self._aliases.setdefault(normalise(name), set()).add(entry.id)

    # ------------------------------------------------------------ queries
    def get(self, model_id: str) -> ModelEntry | None:
        return self._entries.get(model_id)

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._entries

    def ids(self) -> list[str]:
        return sorted(self._entries)

    def entries(self) -> list[ModelEntry]:
        return [self._entries[i] for i in self.ids()]

    def aliases(self) -> dict[str, set[str]]:
        return self._aliases

    def resolve_part(self, model_id: str, phrase: str) -> str | None:
        """A part id of `model_id` named by `phrase` ("left ventricle", "LV")."""
        entry = self.get(model_id)
        if entry is None:
            return None
        wanted = normalise(phrase)
        if wanted in entry.parts:
            return wanted
        wanted_id = wanted.replace(" ", "_")
        if wanted_id in entry.parts:
            return wanted_id
        for part_id, part in entry.parts.items():
            names = {normalise(part["name"]), normalise(part_id)}
            names.update(normalise(a) for a in part.get("aliases", []))
            if wanted in names:
                return part_id
        return None

    def resolve_group(self, model_id: str, phrase: str) -> str | None:
        entry = self.get(model_id)
        if entry is None:
            return None
        wanted = normalise(phrase).replace(" ", "_")
        for group in entry.groups:
            if wanted in {group, group.rstrip("s"), f"{group}s"}:
                return group
        aliases = entry.manifest.get("group_aliases", {})
        for group, names in aliases.items():
            if normalise(phrase) in {normalise(n) for n in names}:
                return group
        return None
