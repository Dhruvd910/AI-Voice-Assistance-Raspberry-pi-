"""A plain-JSON list of what the engine can show, for other programs.

Liza reads data/liza_catalog.json to decide whether a model she is asked for
is the engine's to draw -- without importing any engine code (her process has
its own packages and her own `ui` module, which the engine's `ui` package would
shadow). Written whenever the index is rebuilt.
"""

from __future__ import annotations

import json
from pathlib import Path

from database.repository import normalise
from models.registry import ModelRegistry


def export_catalog(registry: ModelRegistry, path: Path) -> int:
    models = {}
    for entry in registry.entries():
        m = entry.manifest
        names = {m["name"], entry.id.rsplit(".", 1)[-1].replace("_", " "), *m.get("aliases", [])}
        models[entry.id] = {
            "name": entry.name,
            "kind": entry.kind,
            "domain": m["domain"],
            "aliases": sorted({normalise(n) for n in names if n}),
            # Formulas keep their case: "CO" is carbon monoxide, "Co" cobalt.
            "formulas": sorted({a for a in m.get("aliases", []) if any(ch.isdigit() for ch in a)}),
            "disclaimer": entry.disclaimer or "",
            "attribution": "" if entry.source["license"] == "LicenseRef-Generated" else entry.source["attribution"],
            "parts": {p: s["name"] for p, s in entry.parts.items() if s.get("available", True)},
            "groups": sorted(entry.groups),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "models": models}, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(models)
