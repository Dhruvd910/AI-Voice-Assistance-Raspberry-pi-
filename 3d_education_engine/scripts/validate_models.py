"""Check every manifest: schema, provenance, licence policy, files and checksums.

    python scripts/validate_models.py            exit code 1 if anything is wrong

Run it before committing a new model, and in CI.
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import setup, table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    settings = setup(args.verbose)
    from models.provenance import KNOWN_LICENSES
    from models.registry import ModelRegistry

    registry = ModelRegistry(settings.manifests_dir, settings.assets_dir)
    report = registry.load(check_files=True)
    rows = []
    for entry in registry.entries():
        src = entry.source
        lic = KNOWN_LICENSES[src["license"]]
        rows.append([entry.id, entry.kind, src["provider"], src["license"],
                     "share-alike" if lic.share_alike else ("attribution" if lic.attribution_required else "-")])
    print(table(rows, ["model", "kind", "source", "licence", "obligation"]))
    print(f"\n{len(report.loaded)} valid, {len(report.rejected)} rejected")
    for path, problems in report.rejected.items():
        print(f"\nREJECTED {path}")
        for p in problems:
            print(f"  - {p}")
    # ATTRIBUTIONS.md must list every third-party asset.
    attributions = settings.attributions_path.read_text(encoding="utf-8") if settings.attributions_path.is_file() else ""
    missing = [e.id for e in registry.entries()
               if e.source["license"] != "LicenseRef-Generated" and f"`{e.id}`" not in attributions]
    if missing:
        print(f"\nATTRIBUTIONS.md is missing {missing}: run scripts/build_index.py")
    unknown = [p for p in settings.manifests_dir.rglob("*.json") if not _is_json(p)]
    return 1 if report.rejected or missing or unknown else 0


def _is_json(path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, json.JSONDecodeError):
        return False


if __name__ == "__main__":
    sys.exit(main())
