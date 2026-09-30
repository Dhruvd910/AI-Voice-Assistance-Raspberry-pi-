"""Search the approved sources for a model, and show what exists and under what licence.

    python scripts/discover_models.py --query "human heart"
    python scripts/discover_models.py --query water --source PubChem
    python scripts/discover_models.py --entry 3DPX-012345          (NIH 3D, by id)

Nothing is downloaded. Use download_models.py to fetch one of the results.
"""

from __future__ import annotations

import argparse
import sys

from _common import setup, table

DOMAIN_SOURCES = {"biology": ["BodyParts3D", "NIH_3D", "Z-Anatomy", "RCSB_PDB"], "chemistry": ["PubChem"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", help="what to look for")
    parser.add_argument("--source", help="one source: BodyParts3D, NIH_3D, Z-Anatomy, PubChem, RCSB_PDB")
    parser.add_argument("--domain", choices=["biology", "chemistry"], help="search the sources for a domain")
    parser.add_argument("--entry", help="an NIH 3D entry id (3DPX-######) to inspect")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    settings = setup(args.verbose)
    from models.downloader import ADAPTERS, NIH3DAdapter, SourceUnavailable, adapter

    cache = settings.data_dir / "sources"
    if args.entry:
        asset = NIH3DAdapter(cache).metadata(args.entry)
        print(f"{asset.source_id}: {asset.title}\n  by {asset.creator}\n  licence label: "
              f"{asset.extra.get('license_label')}  ->  {asset.license or 'NOT DETERMINED'}\n  {asset.notes}\n"
              f"  files: {', '.join(f['name'] for f in asset.extra.get('files', []))}")
        return 0
    if not args.query:
        parser.error("--query or --entry is needed")
    names = [args.source] if args.source else DOMAIN_SOURCES.get(args.domain, list(ADAPTERS))
    status = 0
    for name in names:
        print(f"\n== {name}")
        try:
            hits = adapter(name, cache).search(args.query, args.limit)
        except SourceUnavailable as exc:
            print(f"  {exc}")
            continue
        except Exception as exc:
            print(f"  failed: {exc}")
            status = 1
            continue
        if not hits:
            print("  no matches")
            continue
        print(table([[h.source_id, h.title, h.license or "UNDETERMINED", h.notes] for h in hits],
                    ["id", "title", "licence", "notes"]))
    return status


if __name__ == "__main__":
    sys.exit(main())
