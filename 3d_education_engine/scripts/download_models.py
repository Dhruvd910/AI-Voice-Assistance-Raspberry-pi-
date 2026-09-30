"""Fetch a model from an approved source, convert it, record its provenance, register it.

    python scripts/download_models.py --source BodyParts3D --query "human heart"
    python scripts/download_models.py --source BodyParts3D --fma FMA7197 --name "Liver"
    python scripts/download_models.py --source PubChem --query caffeine
    python scripts/download_models.py --source RCSB_PDB --query 4HHB --name Haemoglobin
    python scripts/download_models.py --source NIH_3D --entry 3DPX-012345 --confirm-license CC-BY-4.0 \\
            --model-id biology.anatomy.kidney --name "Kidney"

The licence is checked BEFORE anything is downloaded; an asset whose licence
cannot be determined is refused. Afterwards the index and ATTRIBUTIONS.md are
rebuilt.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date

from _common import setup


def bodyparts3d(args, settings, cache) -> str:
    from biology.anatomy import RECIPES, ModelRecipe, PartRecipe
    from models.converter import build_bodyparts3d_model
    from models.downloader import BodyParts3DAdapter
    src = BodyParts3DAdapter(cache)
    wanted = (args.query or "").lower()
    recipe = next((r for r in RECIPES.values() if wanted and (wanted in r.aliases or wanted == r.model_id)), None)
    if recipe is None:
        fma = args.fma
        if not fma:
            hits = src.search(args.query, limit=1)
            if not hits:
                raise SystemExit(f"BodyParts3D has nothing matching {args.query!r}")
            fma = hits[0].source_id
        name = args.name or src.concepts()[fma].title()
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        recipe = ModelRecipe(
            model_id=args.model_id or f"biology.anatomy.{slug}", name=name, root_concept=fma,
            parts=[PartRecipe(slug, name, [fma], "#d9a0a0", f"{name} (BodyParts3D {fma}).")],
            unavailable={}, groups={}, group_aliases={}, aliases=[name.lower()], topics=[name.lower()],
            grades=["8", "9", "10", "11", "12"], relationships={}, notes=[])
        print(f"No hand-written recipe for this structure: building '{name}' as a single part from {fma}.")
    recipe.aliases = sorted({*recipe.aliases, *args.alias_list})
    model_dir = settings.assets_dir / "biology" / recipe.model_id.rsplit(".", 1)[-1]
    manifest = build_bodyparts3d_model(recipe, src, model_dir)
    return manifest["id"]


def nih3d(args, settings, cache) -> str:
    from models.converter import convert_single_file
    from models.acquisition import record_files, write_manifest
    from models.downloader import NIH3DAdapter
    if not args.entry or not args.model_id or not args.name:
        raise SystemExit("NIH 3D needs --entry, --model-id and --name")
    src = NIH3DAdapter(cache)
    asset = src.metadata(args.entry, confirmed_license=args.confirm_license)
    print(f"{asset.source_id}: {asset.title} by {asset.creator}; licence {asset.license or 'NOT DETERMINED'}")
    if asset.license is None:
        raise SystemExit(f"Refused: {asset.notes}")
    model_dir = settings.assets_dir / args.model_id.split(".")[0] / args.model_id.rsplit(".", 1)[-1]
    files = src.download(asset, model_dir / "original")
    converted = convert_single_file(files[0], model_dir)
    attribution = asset.extra.get("attribution_instructions") or f"{asset.title} by {asset.creator}, NIH 3D {asset.source_id}"
    manifest = {
        "id": args.model_id, "name": args.name, "domain": args.model_id.split(".")[0],
        "subject": args.model_id.split(".")[1], "kind": "asset",
        "asset": {"file": converted["file"], "format": "glb", "units": "source units", "lods": converted["lods"],
                  "cells": converted["cells"], "origin_offset": converted["offset"], "thumbnail": "metadata/thumbnail.png"},
        "source": asset.provenance(attribution, "converted+decimated").to_manifest(),
        "source_detail": {"license_label_on_entry_page": asset.extra.get("license_label"), "notes": asset.notes,
                          "original_file": str(files[0].relative_to(model_dir))},
        "educational": {"grades": ["8", "9", "10", "11", "12"], "topics": [args.name.lower()]},
        "aliases": sorted({args.name.lower(), *args.alias_list}),
        "parts": {p: {"name": args.name if p == "model" else p.replace("_", " ").title()} for p in converted["parts"]},
        "capabilities": ["rotate", "zoom", "hide", "show", "highlight", "transparent", "clip", "focus", "label", "measure"],
        "generated_on": date.today().isoformat(),
    }
    record_files(manifest, model_dir)
    write_manifest(manifest, model_dir)
    return args.model_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, choices=["BodyParts3D", "NIH_3D", "Z-Anatomy", "PubChem", "RCSB_PDB"])
    parser.add_argument("--query")
    parser.add_argument("--fma", help="BodyParts3D concept id, e.g. FMA7197")
    parser.add_argument("--entry", help="NIH 3D entry id, e.g. 3DPX-012345")
    parser.add_argument("--model-id")
    parser.add_argument("--name")
    parser.add_argument("--aliases", default="", help="comma-separated extra names")
    parser.add_argument("--confirm-license", help="licence you have checked on the source page (NIH 3D)")
    parser.add_argument("--no-index", action="store_true", help="skip rebuilding the index")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    settings = setup(args.verbose)
    cache = settings.data_dir / "sources"
    aliases = [a.strip() for a in args.aliases.split(",") if a.strip()]
    args.alias_list = aliases

    if args.source == "BodyParts3D":
        model_id = bodyparts3d(args, settings, cache)
    elif args.source == "PubChem":
        from models.acquisition import acquire_pubchem_molecule
        manifest, _ = acquire_pubchem_molecule(args.query, args.model_id, settings.assets_dir, cache,
                                               aliases=aliases or [args.query], name=args.name)
        model_id = manifest["id"]
    elif args.source == "RCSB_PDB":
        from models.acquisition import acquire_rcsb_structure
        manifest, path = acquire_rcsb_structure(args.query, settings.assets_dir, cache, args.name)
        if aliases:
            manifest["aliases"] += aliases
            path.write_text(json.dumps(manifest, indent=2) + "\n")
        model_id = manifest["id"]
    elif args.source == "NIH_3D":
        model_id = nih3d(args, settings, cache)
    else:
        print("Z-Anatomy structures come out of a Blender file:\n"
              "  1. choose the structure and CONFIRM its upstream licence (Z-Anatomy's README lists several);\n"
              "  2. install Blender (sudo apt install blender);\n"
              "  3. models.converter.blender_export_objects(<Z-Anatomy .blend>, [object names], <dir>)\n"
              "     then build a recipe as for BodyParts3D.\n"
              "Refusing to download the 87 MB atlas without a confirmed upstream licence.")
        return 2
    print(f"Registered {model_id}.")
    if not args.no_index:
        from build_index import rebuild
        rebuild(settings, quiet=True)
        print("Index and ATTRIBUTIONS.md rebuilt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
