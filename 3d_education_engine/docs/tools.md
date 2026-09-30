# Tool reference

Generated from the live ToolRegistry (`agent/toolset.py`). These are the ONLY
actions the agent — rule-based parser or LLM — can take. Every call is validated against the
schema below before it runs; unknown tools and unknown arguments are refused.

## Models

| tool | arguments | what it does |
|---|---|---|
| `find_or_acquire` | `query`, `domain`? ∈ ['biology', 'chemistry', 'physics'] | When no registered model matches: search approved sources, fetch a molecule from PubChem if possible, or report what is needed. Never invents URLs. |
| `list_parts` | — | List the parts and groups of the model on screen. |
| `load_model` | `model_id` | Load and show a registered model by id. |
| `search_models` | `query` | Find registered models matching words (e.g. 'heart', 'H2O', 'projectile'). |
| `transition_to` | `model_id`, `focus`? | Move to another model with a smooth zoom transition (use for changes of scale). |
| `unload_model` | `model_id`? | Remove a model from the screen and memory. |

## Navigation

| tool | arguments | what it does |
|---|---|---|
| `go_back` | — | Return to the previously shown model. |
| `go_deeper` | — | Go one level smaller in scale (heart → tissue → cell → organelle → molecule). |
| `go_up` | — | Go one level larger in scale. |
| `zoom_to_level` | `target` | Zoom in through the scale levels until the named level/model is reached (e.g. 'cell', 'mitochondria'). |

## Visualization

| tool | arguments | what it does |
|---|---|---|
| `add_label` | `text`, `part_id`?, `position`? | Add a text label at a part or a point. |
| `animate` | `name` | Play a named animation ('heartbeat', 'play') or 'stop'. |
| `clear_clip` | — | Remove the cut. |
| `clip` | `plane` ∈ ['x', 'y', 'z'], `position`? [0.0–1.0], `keep`? ∈ ['positive', 'negative'] | Cut the model with a plane: x = left/right (sagittal, 'vertically'), y = front/back (coronal), z = top/bottom (horizontal). position 0..1 across the model. |
| `cut_through` | `part_id`, `plane`? ∈ ['x', 'y', 'z'] | Cut the model open through the middle of a part to show its inside. |
| `explode` | `amount` [0.0–3.0], `model_id`? | Pull parts apart from the centre (0 = assembled, 1 = well separated). |
| `focus` | `part_id` | Centre the camera on a part. |
| `hide_part` | `part_id` | Hide a part or group. |
| `highlight` | `part_id` | Highlight a part or group ('none' clears). |
| `isolate` | `part_id` | Show only this part or group. |
| `label_parts` | `part_id`? | Label a part or group with its name ('all' labels every part). |
| `measure_distance` | `point_a`, `point_b` | Distance between two parts (by name) or points. |
| `remove_label` | `label_id` | Remove a label by id, or 'all'. |
| `reset_camera` | — | Return to the starting view. |
| `rotate` | `x`? [-360–360], `y`? [-360–360], `z`? [-360–360] | Turn the model: y = left/right, x = tip towards/away, z = roll; degrees. |
| `set_camera` | `position`, `target` | Place the camera explicitly (model units). |
| `set_transparency` | `part_id`?, `value` [0.0–1.0] | Set opacity: 0 invisible, 0.3 see-through, 1 solid. |
| `set_visibility` | `part_id`, `visible` | Show or hide a part without highlighting it. |
| `show_part` | `part_id` | Show and emphasise a part or group; the rest stays visible but dimmed. |
| `zoom` | `amount` [0.05–20.0] | Zoom by a factor: >1 in, <1 out. |

## Knowledge

| tool | arguments | what it does |
|---|---|---|
| `explain` | `question`, `concept_id`? | Retrieve curated educational passages to answer a question. Always use this before explaining science. |
| `related_concepts` | `concept_id`, `relation`? | What the knowledge graph links to a concept (parts, what it contains, next scale...). |

## Chemistry

| tool | arguments | what it does |
|---|---|---|
| `compare_molecules` | `model_ids` | Show two or more molecules side by side with their shapes. |
| `electron_configuration` | — | Report the electron arrangement of the atom on screen and highlight its outer shell. |
| `molecule_facts` | — | Formula, mass, shape and polarity of the molecule/atom on screen. |
| `set_representation` | `style` ∈ ['ball_and_stick', 'space_filling', 'sticks', 'backbone', 'atoms', 'surface'] | Change how a molecule/protein is drawn. |
| `show_bond_angle` | — | Show and report the bond angle and shape of the molecule on screen. |

## Physics

| tool | arguments | what it does |
|---|---|---|
| `get_measurements` | — | Current parameters, equations and measurements of the simulation. |
| `pause` | — | Pause the simulation or animation. |
| `play` | — | Start the simulation or animation. |
| `reset_simulation` | — | Return the simulation to t = 0. |
| `set_parameter` | `name`, `value` | Change a simulation parameter (e.g. angle, initial_velocity, gravity). |
| `step` | `frames`? [1–600] | Advance the simulation by a number of 1/30 s frames. |
