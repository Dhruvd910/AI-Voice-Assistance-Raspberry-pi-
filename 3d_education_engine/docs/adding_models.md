# Adding a model

Every model has a **manifest**. The registry refuses a manifest that is invalid or lacks
provenance, so a model without a known source and licence can never reach the screen.

## Where manifests live

| kind | files | manifest |
|---|---|---|
| `asset` (meshes) | `assets/<domain>/<name>/{original,processed,runtime}/` | `assets/<domain>/<name>/metadata/manifest.json` |
| `molecule` (structure data) | `assets/chemistry/<name>/original/*.sdf` | `assets/chemistry/<name>/metadata/manifest.json` |
| `procedural` (generated) | none | `models/manifests/<domain>/<name>.json` |
| `simulation` | none | `models/manifests/physics/<name>.json` |

`original/` is exactly what the source served; `processed/` holds cleaned per-part meshes;
`runtime/` holds the GLB files the engine loads. The `files` map in the manifest records a
SHA-256 for each, checked by `scripts/validate_models.py`.

## The manifest

```json
{
  "id": "biology.anatomy.heart",
  "name": "Human Heart",
  "domain": "biology",
  "subject": "anatomy",
  "kind": "asset",
  "asset": {"file": "runtime/heart.glb", "format": "glb", "units": "mm",
            "lods": {"high": "runtime/heart.glb", "low": "runtime/heart_low.glb"}},
  "source": {
    "provider": "BodyParts3D",
    "url": "https://dbarchive.biosciencedbc.jp/data/bodyparts3d/LATEST/partof_BP3D_4.0_obj_99.zip",
    "source_id": "FMA7088 (+25 related FMA concepts)",
    "creator": "The Database Center for Life Science (DBCLS)",
    "license": "CC-BY-4.0",
    "license_url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "BodyParts3D, © The Database Center for Life Science licensed under CC Attribution 4.0 International",
    "download_date": "2026-09-25",
    "modification_status": "extracted+converted+decimated"
  },
  "educational": {"grades": ["7","8","9","10","11","12"], "topics": ["human heart", "circulatory system"],
                  "scale_level": "organ", "disclaimer": "Anatomical model (BodyParts3D). Colours are a teaching convention."},
  "aliases": ["heart", "human heart", "my heart", "cardiac organ"],
  "parts": {
    "left_ventricle": {"name": "Left ventricle", "aliases": ["LV"], "color": "#d65f5a",
                       "description": "Pumps oxygenated blood ...", "available": true},
    "interventricular_septum": {"name": "Interventricular septum", "available": false,
                                "note": "BodyParts3D does not model the septum separately ..."}
  },
  "groups": {"chambers": ["right_atrium", "right_ventricle", "left_atrium", "left_ventricle"]},
  "group_aliases": {"chambers": ["four chambers"]},
  "relationships": {"parent": "biology.anatomy.cardiovascular_system",
                    "scale_down_to": "biology.tissue.cardiac_muscle", "scale_down_focus": "left_ventricle"},
  "capabilities": ["rotate", "zoom", "hide", "show", "highlight", "transparent", "clip", "explode", "focus"],
  "animations": ["heartbeat"],
  "files": {"runtime/heart.glb": "<sha256>", "...": "..."}
}
```

Rules (`models/validator.py`): ids are `domain.subject.name`, lower case; all nine provenance
fields are required; the licence must be one `models/provenance.py` knows **and** the policy
accepts; `license_url` must match it; group members must be parts.

## From an approved source

```bash
python scripts/discover_models.py --query "kidney"                  # search and see licences
python scripts/download_models.py --source BodyParts3D --fma FMA7205 --name "Left Kidney" \
        --model-id biology.anatomy.left_kidney --aliases "kidney"
python scripts/download_models.py --source NIH_3D --entry 3DPX-012345 --confirm-license CC-BY-4.0 \
        --model-id biology.anatomy.skull --name "Skull"
python scripts/download_models.py --source PubChem --query caffeine
python scripts/download_models.py --source RCSB_PDB --query 1GZX --name "Oxyhaemoglobin"
```

Each run fetches the originals, converts them, writes the manifest with provenance, rebuilds the
index and regenerates `ATTRIBUTIONS.md`.

### Multi-part anatomy: write a recipe

A single BodyParts3D concept becomes a one-part model. For named parts, add a `ModelRecipe` to
`biology/anatomy.py` (see `HEART`): each `PartRecipe` lists the FMA concepts whose element meshes
make up the part. **Order matters** — BodyParts3D's concepts overlap and an element belongs to the
first part that claims it. Find FMA ids in `data/sources/bodyparts3d/partof_parts_list_e.txt`.
Then `python scripts/download_models.py --source BodyParts3D --query "<recipe alias>"`.

### Z-Anatomy

Z-Anatomy's atlas is a Blender file; its own content is CC BY-SA 4.0 but parts come from upstream
sources under other licences (its README lists CC BY-NC ones). Confirm the upstream licence of the
structure first, install Blender, then use `models.converter.blender_export_objects`.

## A generated (procedural) model

1. Write a generator `fn(entry) -> SceneModel` (see `biology/cells.py`), mark it
   `"Educational model — not to scale"` and state what it simplifies.
2. Add it to that module's `GENERATORS` dict (the whitelist — manifests cannot name arbitrary code).
3. Write `models/manifests/<domain>/<name>.json` with `"kind": "procedural"`,
   `"generator": {"name": "..."}` and `generated_provenance(...)`-style source fields
   (`license: LicenseRef-Generated`).
4. `python scripts/build_index.py`, then add a document under `knowledge/documents/`.

## Knowledge for the new model

Add `knowledge/documents/<domain>/<name>.md` with frontmatter `concept: <model or part id>` and
`## Definition`, `## Function`, `## Structure`, `## Misconceptions`, `## Examples`, `## Questions`
sections. Curated relations go in `knowledge/documents/graph.json`; parts and scale links are
added from the manifest automatically.
