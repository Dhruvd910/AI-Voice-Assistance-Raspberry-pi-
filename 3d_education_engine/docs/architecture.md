# Architecture

## The pipeline from words to pixels

1. **Input** — typed text, or speech (`voice/stt.py`: an OpenAI-compatible transcription API,
   or on-device faster-whisper; optional OpenWakeWord wake word).
2. **Agent** (`agent/agent.py`)
   - `IntentParser` recognises common commands offline and instantly ("rotate it", "cut it in
     half", "show the four chambers and label them", "dil dikhao"). It never touches the scene; it
     only produces tool calls.
   - Questions highlight the part they name, retrieve curated passages (`explain`), and are
     answered by the LLM from those passages — or, offline, by quoting the best sentences.
   - Anything else goes to the LLM with the tool schemas. The LLM can only return tool calls.
   - `ConversationState` remembers what "it" and "this part" refer to.
3. **ToolRegistry** (`agent/tool_registry.py`, `agent/toolset.py`) — the only door to action.
   Every call is checked against a JSON schema (types, ranges, enums, no unknown keys) and
   semantic checks (the model id is registered), then run. There is no tool that evaluates code,
   imports modules, or opens arbitrary files or URLs.
4. **Services** — built once by `app/dependency_container.py`:

| service | module | job |
|---|---|---|
| Model Registry | `models/registry.py` | manifests → validated entries; SQLite index |
| Resolver | `models/resolver.py` | "my heart", "H2O" → canonical model id |
| Loader + caches | `models/loader.py`, `models/cache.py` | GLB / generator / RDKit / simulation → `SceneModel`; LOD, LRU cache, memory watchdog |
| Knowledge graph | `knowledge/knowledge_graph.py` | has_part, contains, scale_down_to, produces… |
| RAG | `knowledge/rag.py`, `knowledge/embeddings.py` | curated documents → passages |
| Scale manager | `biology/scale_manager.py` | current / previous / next scale, "go back" |
| Visualization engine | `visualization/engine.py` | the scene; every tool that changes the screen |
| Chemistry | `chemistry/*` | atoms, RDKit structures, reactions, proteins |
| Physics | `physics/*` | simulations with parameters, equations, measurements |

5. **Rendering** — PyVista/VTK renders **off-screen** into an image; the Qt viewport shows it.
   The same code runs on the Pi (Mesa v3d), a desktop GPU, and in tests with no display.

## A SceneModel is the common currency

Every kind of model — a GLB from BodyParts3D, a generated cell, an RDKit molecule, a physics
simulation — becomes a `SceneModel`: named `ScenePart`s with geometry, units, a disclaimer, an
attribution line, optional animations and domain extras (the `Molecule`, the `Simulation`). The
engine draws one kind of thing. Display state (visible, highlighted, clipped, exploded) belongs to
the engine, so a cached model can be shown again without reloading.

## Threading

VTK must be driven from one thread. The engine lives on the UI thread. The agent runs on a worker
thread (an LLM call never freezes the screen) and reaches the engine through
`MainThreadExecutor`, a blocking queued call onto the UI thread. Tools that only read the database
or the network (`explain`, `search_models`, `find_or_acquire`) are marked `main_thread=False` and
run on the worker, so a slow lookup never blocks rendering.

## Multi-scale navigation

`scale_down_to` relationships in manifests (and `graph.json`) form a chain:
heart → cardiac muscle → cardiomyocyte → mitochondrion → ATP. "Zoom in until I can see a cell"
queues one `Transition` per level (`visualization/transitions.py`): the camera dives towards the
`scale_down_focus` part while the model fades, the next model is swapped in close up, and the
camera pulls back as it fades in.

## Safety and accuracy

- The LLM sees only tool definitions, a scene summary and the list of registered model ids. It is
  told to call `explain` before explaining science and to say when a model is simplified.
- Unknown objects go through `find_or_acquire` (`models/acquisition.py`): approved sources are
  searched; a molecule may be fetched from PubChem (licence known in advance); anything else is
  reported with the exact command to add it. No URL is ever invented.
- Generated models carry "Educational model — not to scale" and say what they simplify. Electron
  shell diagrams state that electrons are not on circular orbits.
- Numbers shown in chemistry come from the structure on screen; measured reference values are
  quoted beside them and labelled.

## Offline-first

Once assets are downloaded and indexed, visualization, chemistry, physics and the knowledge base
work without a network. Only the LLM, cloud speech and on-demand PubChem fetches need one.
`--offline` / `ENGINE_OFFLINE=true` forces this mode.
