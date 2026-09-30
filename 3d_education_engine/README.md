# 3D Education Engine

A voice- and touch-driven 3D viewer for school Biology, Chemistry and Physics, built for a
Raspberry Pi 5 with an 800×480 touch screen (and equally at home on a desktop PC).

> "Show me a human heart." → "Show the four chambers." → "Cut it in half." →
> "Why is the left ventricular wall thicker?" → "Zoom in until I can see a cell." →
> "Show the mitochondria." → "Go back." → "Create a water molecule." → "Show the bond angle." →
> "Show projectile motion." → "Change the launch angle to 60 degrees."

It is **not** a chatbot bolted onto a viewer. Language is turned into a small set of
**validated tool calls**; the tools act on a model registry, a knowledge graph, a
visualization engine and a simulation engine. The LLM never runs code.

```
speech / text ─► agent ─► intent parser (offline) ─┐
                    └──► LLM tool calling ──────────┴─► ToolRegistry (schema + semantic checks)
                                                          │
            ┌─────────────────┬──────────────────┬───────┴─────────┬─────────────────┐
     Model Registry     Knowledge graph      RAG (curated docs)   Simulations     Chemistry (RDKit)
     (manifests,        (has_part, contains,                     (NumPy/SciPy)   PubChem / PDB data
      provenance)        scale_down_to …)
            └──────────────────────────────► Visualization engine (PyVista/VTK, off-screen) ─► Qt UI
```

## What is in the box

| | |
|---|---|
| **Heart MVP** | A real anatomical heart from **BodyParts3D** (CC BY 4.0): 15 named parts (4 chambers, 4 valves, aorta, pulmonary trunk, venae cavae, pulmonary veins, coronary arteries, cardiac veins), high and low levels of detail, provenance for every file. |
| **Multi-scale** | Heart → cardiac muscle tissue → cardiomyocyte → mitochondrion → ATP, with animated transitions, "go deeper", "go back". |
| **Chemistry** | 10 molecules from PubChem 3D conformers (water, CO₂, O₂, N₂, CH₄, NH₃, HCl, glucose, ethanol, ATP), 8 atoms with shell models (H, He, C, N, O, Na, Cl, Fe), haemoglobin (PDB 4HHB), methane combustion with every atom tracked, VSEPR shapes, bond angles, comparisons, space-filling / ball-and-stick. Any other molecule is fetched from PubChem on request. |
| **Physics** | 17 simulations: projectile, free fall, pendulum (exact, not small-angle), spring, circular motion, collisions/momentum, orbits (gravity), solar system, travelling and standing waves, interference, diffraction, refraction/TIR, reflection, electric field, magnetic field of a wire, series circuit. Each has parameters, equations, play/pause/step and measurements. |
| **Knowledge** | 48 curated documents (definition, function, structure, misconceptions, examples, questions) → 202 retrievable passages; a knowledge graph of 97 concepts and 181 relations. |
| **Agent** | 43 tools. An offline parser handles common commands instantly (English and some Hinglish, compound commands); an OpenAI-compatible LLM (OpenRouter by default) handles the rest. |
| **UI** | PySide6, 800×480: viewport (drag/pinch/double-tap), conversation, parts list, touch buttons, microphone, status line with attribution. |
| **Pipeline** | `discover → download → validate → convert → build_index`, source adapters for BodyParts3D, NIH 3D, Z-Anatomy, PubChem and RCSB PDB, licence checks before download, `ATTRIBUTIONS.md` generated. The left kidney (BodyParts3D) and caffeine (PubChem, fetched when a student asked for it) were added through this pipeline. |

## Quick start

```bash
cd ~/liza/3d_education_engine
./run.sh                                   # the touch-screen app
.venv/bin/python -m app.main --check       # load everything and report
.venv/bin/python -m app.main --say "show me the heart" --say "cut it in half" --screenshot heart.png
.venv/bin/python -m app.main --repl        # type commands
.venv/bin/python -m pytest                 # 101 tests, offline
```

Copy `.env.example` to `.env` to switch on the LLM, voice input and speech output. With no
`.env` everything still works offline, with the rule-based parser.

## Pipeline

```bash
python scripts/discover_models.py --query "human heart"             # what exists, under which licence
python scripts/download_models.py --source BodyParts3D --query "human heart"
python scripts/download_models.py --source PubChem --query caffeine
python scripts/download_models.py --source RCSB_PDB --query 4HHB --name Haemoglobin
python scripts/validate_models.py                                   # schema, provenance, licences, checksums
python scripts/convert_models.py                                    # re-run conversion from originals
python scripts/build_index.py                                       # registry, graph, RAG, ATTRIBUTIONS.md
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pieces fit, threading, safety
- [docs/raspberry_pi_setup.md](docs/raspberry_pi_setup.md) — installing and running on a Pi 5
- [docs/adding_models.md](docs/adding_models.md) — manifests, sources, recipes, generators
- [docs/adding_simulations.md](docs/adding_simulations.md) — writing a new simulation
- [docs/provenance.md](docs/provenance.md) — licences, provenance records, attributions
- [docs/tools.md](docs/tools.md) — every tool the agent can call (generated)

## Known limits

- The heart's **interventricular septum** is not a separate structure in BodyParts3D; it is
  explained but cannot be highlighted on its own.
- **Z-Anatomy** extraction needs Blender, which is not installed on the development Pi; the
  adapter and export script are written but that path has not been run end to end.
- **NIH 3D** has no public search API; entries are imported by their 3DPX id, and because the
  site shows licences without a version ("CC-BY") the operator must confirm the exact licence.
- Cells, organelles, atoms' shell models and all simulations are **generated educational
  models** and say so on screen ("Educational model — not to scale").
- The offline answerer quotes the curated notes; well-phrased explanations need the LLM.

## Inside Liza

This engine lives in `~/liza/3d_education_engine` and Liza uses it as her 3D drawer for everything
in its catalogue. `app/liza_worker.py` speaks the protocol of Liza's `viewer3d.Renderer` (pickled
command batches over stdin/stdout). Liza reads `data/liza_catalog.json`, which is rewritten by
`scripts/build_index.py`, to know what the engine can show. When the engine's model is on
her board, her conversation loop offers the student's words to `Agent.try_command` first (offline,
commands only); questions go to her own language model, which can reply with
`[ACTION: model3d_do: <instruction>]`. See the "3D models" section of Liza's README.
