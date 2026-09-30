"""Re-run the original/ -> processed/ -> runtime/ conversion from files already downloaded.

    python scripts/convert_models.py                          every model that has originals
    python scripts/convert_models.py --model-id biology.anatomy.heart

Use it after changing a recipe (biology/anatomy.py), the decimation target or
the GLB export; nothing is fetched from the network.
"""

from __future__ import annotations

import argparse
import sys

from _common import setup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-id")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    settings = setup(args.verbose)
    from biology.anatomy import RECIPES
    from models.converter import build_bodyparts3d_model, convert_single_file
    from models.downloader import BodyParts3DAdapter
    from models.registry import ModelRegistry

    registry = ModelRegistry(settings.manifests_dir, settings.assets_dir)
    registry.load(check_files=False)
    done = 0
    for entry in registry.entries():
        if entry.kind != "asset" or (args.model_id and entry.id != args.model_id):
            continue
        if entry.id in RECIPES:
            build_bodyparts3d_model(RECIPES[entry.id], BodyParts3DAdapter(settings.data_dir / "sources"),
                                    entry.model_dir, fetch=False)
            print(f"{entry.id}: rebuilt from {len(list((entry.model_dir / 'original').rglob('*.obj')))} original meshes")
            done += 1
        elif entry.manifest.get("source_detail", {}).get("original_file"):
            original = entry.model_dir / entry.manifest["source_detail"]["original_file"]
            convert_single_file(original, entry.model_dir)
            print(f"{entry.id}: reconverted {original.name} (re-run download_models.py to refresh the manifest hashes)")
            done += 1
        else:
            print(f"{entry.id}: no recipe or original file recorded; skipped")
    print(f"{done} model(s) converted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
