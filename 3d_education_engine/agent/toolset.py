"""Every tool the agent can call, with its schema, wired to the engines.

Grouped by category so the LLM can be offered only what makes sense (no
physics tools while a molecule is on screen, for example), though all are
registered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.tool_registry import Tool, ToolError, ToolRegistry, number, obj, string, vec3
from database.repository import normalise

if TYPE_CHECKING:
    from app.dependency_container import Services

PART = "a part id, group id or name of the model on screen ('left_ventricle', 'chambers', 'valves'), or 'all'"


def build_toolset(sv: "Services", registry: ToolRegistry) -> ToolRegistry:
    eng = sv.engine

    def model_exists(args: dict) -> None:
        mid = args.get("model_id")
        if mid and mid not in sv.registry:
            raise ToolError(f"{mid!r} is not a registered model id; use search_models to find one")

    def after_scene_change(result: dict) -> dict:
        if eng.model_id:
            sv.scale.visited(eng.model_id)
            sv.state.current_model = eng.model_id
        return result

    # ------------------------------------------------------------ models
    def load_model(model_id: str) -> dict:
        return after_scene_change(eng.load_model(model_id))

    def transition_to(model_id: str, focus: str | None = None) -> dict:
        result = eng.transition_to(model_id, focus)
        sv.scale.visited(model_id)
        sv.state.current_model = model_id
        if not sv.interactive:
            eng.finish_transition()
        return result

    def search_models(query: str) -> dict:
        res = sv.resolver.resolve(query)
        hits = [{"model_id": m, "name": sv.registry.get(m).name, "score": round(s, 2)} for m, s in res.candidates] \
            if res.candidates else ([{"model_id": res.model_id, "name": sv.registry.get(res.model_id).name}] if res.ok else [])
        return {"query": query, "matches": hits,
                "message": f"Found {hits[0]['name']}." if hits else f"No model matches '{query}'."}

    def find_or_acquire(query: str, domain: str | None = None) -> dict:
        res = sv.resolver.resolve(query)
        if res.ok:
            return sv.on_main(lambda: load_model(res.model_id))
        from models.acquisition import plan_for_missing
        # Network work happens here, on the agent's thread; only showing the
        # result goes to the UI thread.
        plan = plan_for_missing(
            query, domain, sv.registry, sv.repo, sv.settings.assets_dir, sv.settings.data_dir / "sources",
            sv.settings.attributions_path, allow_fetch_chemistry=True, offline=sv.settings.offline)
        if plan.acquired:
            sv.kg.rebuild(sv.registry, sv.graph_file)
            sv.on_main(lambda: load_model(plan.acquired.id))
        return {"searched": plan.searched, "candidates": [c.title for c in plan.candidates[:3]],
                "acquired": plan.acquired.id if plan.acquired else None, "message": plan.message()}

    def list_parts() -> dict:
        summary = eng.summary()
        if not summary.get("model"):
            raise ToolError("nothing is loaded")
        entry = sv.registry.get(summary["model"])
        unavailable = {p: s.get("note", "") for p, s in (entry.parts.items() if entry else []) if not s.get("available", True)}
        return {"parts": {p: v["name"] for p, v in summary["parts"].items()}, "groups": summary["groups"],
                "not_in_this_model": unavailable, "message": ", ".join(v["name"] for v in summary["parts"].values())}

    # ------------------------------------------------------------ navigation across scales
    def part_model(part_id: str | None) -> str | None:
        """The model a part of the one on screen opens into, if it has one."""
        entry = sv.registry.get(eng.model_id) if eng.model_id else None
        target = entry.relationships.get("part_models", {}).get(part_id) if entry and part_id else None
        return target if target in sv.registry else None

    def zoom_into(part_id: str) -> dict:
        """Into a part: the model it opens into, with the camera diving towards
        it as the cell fades and the nucleus fades in -- or, for a part with no
        model of its own, simply a closer look at it."""
        ids = eng.resolve_parts(part_id)
        target = next((t for t in (part_model(p) for p in [part_id, *ids]) if t), None)
        if target:
            sv.state.dives.append(target)
            return transition_to(target, focus=part_id)
        result = eng.focus(part_id)
        eng.zoom(1.6)
        return {**result, "message": f"Zoomed in on {eng._names(ids)}."}

    # Zooming as a way down and back up. Four presses of Liza's + (1.25 each)
    # is 2.4x: by then the nucleus fills the view and the student is plainly
    # trying to see into it, so the cell fades into the nucleus model. Three
    # presses of - on a model zoomed into goes back to where it was entered.
    DIVE_AT_ZOOM, SURFACE_AT_ZOOM = 2.4, 0.52

    def zoom(amount: float) -> dict:
        result = eng.zoom_by(amount)
        if eng.transition is not None:
            return result
        if amount > 1 and eng.user_zoom >= DIVE_AT_ZOOM:
            entry = sv.registry.get(eng.model_id)
            targets = [p for p, t in (entry.relationships.get("part_models", {}) if entry else {}).items()
                       if t in sv.registry]
            part = eng.part_in_view(targets)
            if part:
                return zoom_into(part)
        elif amount < 1 and eng.user_zoom <= SURFACE_AT_ZOOM and sv.state.dives \
                and sv.state.dives[-1] == eng.model_id:
            sv.state.dives.pop()
            return go_back()
        return result

    def go_deeper() -> dict:
        # Into the part they last pointed at, when it has a model of its own:
        # "highlight the mitochondria" then "go deeper" means the mitochondrion.
        chosen = part_model(sv.state.selected_part)
        if chosen:
            return zoom_into(sv.state.selected_part)
        nxt = sv.scale.deeper()
        if not nxt:
            raise ToolError("this is the smallest level I can show from here")
        return transition_to(nxt, focus=sv.scale_focus.get(eng.model_id))

    def go_up() -> dict:
        up = sv.scale.up()
        if not up:
            raise ToolError("this is the largest level I can show from here")
        return transition_to(up)

    def go_back() -> dict:
        target = sv.scale.back()
        if not target:
            raise ToolError("there is nothing to go back to")
        result = eng.transition_to(target)
        sv.state.current_model = target
        if not sv.interactive:
            eng.finish_transition()
        return {**result, "message": f"Back to {sv.registry.get(target).name}."}

    def zoom_to_level(target: str) -> dict:
        """Follow the scale chain from what is on screen down to `target`, one
        transition per level, so the change of scale is visible."""
        cur = eng.model_id
        chain = sv.kg.scale_chain(cur) if cur else []
        # Look along THIS model's zoom path first: from the heart, "a cell"
        # means a heart muscle cell, not the generic animal cell.
        key = normalise(target)
        wanted = None
        for mid in chain[1:]:
            entry = sv.registry.get(mid)
            if entry is None:
                continue
            names = {normalise(n) for n in [entry.name, *entry.manifest.get("aliases", []),
                                            *entry.manifest.get("educational", {}).get("topics", [])]}
            level = entry.manifest.get("educational", {}).get("scale_level", "")
            if key == normalise(level) or key in names or any(n.endswith(" " + key) for n in names) \
                    or target == mid:
                wanted = mid
                break
        if wanted is None:
            wanted = sv.resolver.resolve(target).model_id
        if wanted is None or wanted not in chain:
            if wanted:
                return transition_to(wanted)
            raise ToolError(f"I can't reach '{target}' by zooming in from here")
        steps = chain[1: chain.index(wanted) + 1]
        for i, mid in enumerate(steps):
            eng.transition_to(mid, focus=sv.scale_focus.get(chain[i]) if i == 0 else None)
            sv.scale.visited(mid)
        sv.state.current_model = wanted
        if not sv.interactive:
            eng.finish_transition()
        names = " → ".join(sv.registry.get(m).name for m in [cur, *steps])
        return {"path": [cur, *steps], "message": f"Zooming in: {names}."}

    # ------------------------------------------------------------ knowledge
    def explain(question: str, concept_id: str | None = None) -> dict:
        concepts = [c for c in [concept_id, sv.state.selected_concept(), eng.model_id] if c]
        chunks = sv.kb.search(question, k=4, concepts=concepts,
                              sections=["definition", "function", "structure", "facts", "equations", "misconceptions"])
        part_note = ""
        if concept_id and eng.model_id and concept_id.startswith(eng.model_id + "."):
            entry = sv.registry.get(eng.model_id)
            spec = entry.parts.get(concept_id.rsplit(".", 1)[-1], {}) if entry else {}
            part_note = spec.get("description") or spec.get("note") or ""
        return {"passages": [{"concept": c.concept_id, "section": c.section, "text": c.text,
                              "score": round(c.score, 3)} for c in chunks],
                "part_description": part_note,
                "message": f"Found {len(chunks)} passage(s)." if chunks else "No notes found."}

    def related_concepts(concept_id: str, relation: str | None = None) -> dict:
        items = sv.kg.neighbours(concept_id, relation)
        return {"concept": concept_id, "related": [{"relation": i["relation"], "id": i["id"], "label": i["label"],
                                                    "can_show": bool(i.get("model_id"))} for i in items],
                "summary": sv.kg.describe(concept_id), "message": sv.kg.describe(concept_id) or "Nothing linked."}

    # ------------------------------------------------------------ chemistry
    from chemistry import tools as chem

    def compare_molecules(model_ids: list[str]) -> dict:
        for m in model_ids:
            model_exists({"model_id": m})
        return chem.compare(eng, model_ids)

    # ------------------------------------------------------------ physics
    def _sim():
        sim = eng.simulation
        if sim is None:
            raise ToolError("no simulation is running; load one first (e.g. 'show projectile motion')")
        return sim

    def set_parameter(name: str, value: float) -> dict:
        sim = _sim()
        key = sim.set_parameter(name, value)
        sim.prepare()
        sim.reset()
        eng.replace_parts(sim.build_parts())
        eng.reset_camera()
        sim.apply(eng)
        p = sim.params[key]
        return {"parameter": key, "value": p.value, "unit": p.unit, "measurements": sim.measurements(),
                "message": f"Set {key.replace('_', ' ')} to {p.value:g}{'' if p.unit in ('°', '') else ' '}{p.unit}."}

    def play() -> dict:
        sim = eng.simulation
        if sim is None:
            return eng.animate(next(iter(eng.scene.animations), "") if eng.scene and eng.scene.animations else "play")
        if sim.t >= sim.duration and not sim.loop:
            sim.reset()
        sim.playing = True
        return {"message": f"Running {sim.title}."}

    def pause() -> dict:
        if eng.simulation is not None:
            eng.simulation.playing = False
        return eng.animate("stop")

    def reset_simulation() -> dict:
        sim = _sim()
        sim.reset()
        sim.playing = False
        sim.apply(eng)
        return {"message": "Simulation reset to the start."}

    def step(frames: int = 1) -> dict:
        sim = _sim()
        sim.step(int(frames))
        sim.apply(eng)
        return {"time_s": round(sim.t, 3), "measurements": sim.measurements(), "message": f"t = {sim.t:.2f} s."}

    def get_measurements() -> dict:
        sim = _sim()
        return {**sim.describe(), "message": "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sim.measurements().items())}

    T = [
        # ---- models
        Tool("load_model", "Load and show a registered model by id.", obj({"model_id": string("registered model id")}, ["model_id"]), load_model, model_exists, "models"),
        Tool("unload_model", "Remove a model from the screen and memory.", obj({"model_id": string("model id; omit for the one on screen")}), lambda model_id=None: eng.unload_model(model_id), model_exists, "models"),
        Tool("transition_to", "Move to another model with a smooth zoom transition (use for changes of scale).",
             obj({"model_id": string("registered model id"), "focus": string("part of the current model to zoom towards")}, ["model_id"]), transition_to, model_exists, "models"),
        Tool("search_models", "Find registered models matching words (e.g. 'heart', 'H2O', 'projectile').", obj({"query": string("what the user asked for")}, ["query"]), search_models, None, "models", main_thread=False),
        Tool("find_or_acquire", "When no registered model matches: search approved sources, fetch a molecule from PubChem if possible, or report what is needed. Never invents URLs.",
             obj({"query": string("the object asked for"), "domain": string("subject area", ["biology", "chemistry", "physics"])}, ["query"]), find_or_acquire, None, "models", main_thread=False),
        Tool("list_parts", "List the parts and groups of the model on screen.", obj(), list_parts, None, "models"),
        Tool("zoom_into", "Zoom into a part: fade into the part's own model if it has one (the cell's nucleus), otherwise move in close on it.",
             obj({"part_id": string(PART)}, ["part_id"]), zoom_into, None, "navigation"),
        Tool("go_deeper", "Go one level smaller in scale (heart → tissue → cell → organelle → molecule).", obj(), go_deeper, None, "navigation"),
        Tool("go_up", "Go one level larger in scale.", obj(), go_up, None, "navigation"),
        Tool("go_back", "Return to the previously shown model.", obj(), go_back, None, "navigation"),
        Tool("zoom_to_level", "Zoom in through the scale levels until the named level/model is reached (e.g. 'cell', 'mitochondria').",
             obj({"target": string("level or model to reach")}, ["target"]), zoom_to_level, None, "navigation"),
        # ---- parts
        Tool("show_part", "Show and emphasise a part or group; the rest stays visible but dimmed.", obj({"part_id": string(PART)}, ["part_id"]), lambda part_id: eng.show_part(part_id)),
        Tool("hide_part", "Hide a part or group.", obj({"part_id": string(PART)}, ["part_id"]), lambda part_id: eng.hide_part(part_id)),
        Tool("set_visibility", "Show or hide a part without highlighting it.",
             obj({"part_id": string(PART), "visible": {"type": "boolean", "description": "true to show"}}, ["part_id", "visible"]),
             lambda part_id, visible: eng.set_visibility(part_id, visible)),
        Tool("isolate", "Show only this part or group.", obj({"part_id": string(PART)}, ["part_id"]), lambda part_id: eng.isolate(part_id)),
        Tool("highlight", "Highlight a part or group ('none' clears).", obj({"part_id": string(PART + " or 'none'")}, ["part_id"]),
             lambda part_id: eng.highlight(part_id)),
        Tool("set_transparency", "Set opacity: 0 invisible, 0.3 see-through, 1 solid.", obj({"part_id": string(PART), "value": number("opacity", 0.0, 1.0)}, ["value"]),
             lambda value, part_id="all": eng.set_transparency(part_id, value)),
        Tool("focus", "Centre the camera on a part.", obj({"part_id": string(PART)}, ["part_id"]), lambda part_id: eng.focus(part_id)),
        # ---- camera
        Tool("rotate", "Turn the model: y = left/right, x = tip towards/away, z = roll; degrees.",
             obj({"x": number("degrees", -360, 360), "y": number("degrees", -360, 360), "z": number("degrees", -360, 360)}),
             lambda x=0.0, y=0.0, z=0.0: eng.rotate(x, y, z)),
        Tool("zoom", "Zoom by a factor: >1 in, <1 out. Far enough into a part with its own model goes into that model.",
             obj({"amount": number("factor", 0.05, 20.0)}, ["amount"]), zoom),
        Tool("reset_camera", "Return to the starting view.", obj(), lambda: eng.reset_camera()),
        Tool("set_camera", "Place the camera explicitly (model units).", obj({"position": vec3("[x, y, z]"), "target": vec3("[x, y, z]")}, ["position", "target"]),
             lambda position, target: eng.set_camera(position, target)),
        # ---- cutting, exploding, labels, measuring
        Tool("clip", "Cut the model with a plane: x = left/right (sagittal, 'vertically'), y = front/back (coronal), z = top/bottom (horizontal). position 0..1 across the model.",
             obj({"plane": string("axis", ["x", "y", "z"]), "position": number("0..1", 0.0, 1.0),
                  "keep": string("which side to keep", ["positive", "negative"])}, ["plane"]),
             lambda plane, position=0.5, keep="positive": eng.clip(plane, position, keep)),
        Tool("cut_through", "Cut the model open through the middle of a part to show its inside.", obj({"part_id": string(PART), "plane": string("axis", ["x", "y", "z"])}, ["part_id"]),
             lambda part_id, plane="y": eng.cut_through(part_id, plane)),
        Tool("clear_clip", "Remove the cut.", obj(), lambda: eng.clear_clip()),
        Tool("explode", "Pull parts apart from the centre (0 = assembled, 1 = well separated).",
             obj({"amount": number("0..3", 0.0, 3.0), "model_id": string("ignored; the model on screen")}, ["amount"]),
             lambda amount, model_id=None: eng.explode(amount)),
        Tool("add_label", "Add a text label at a part or a point.", obj({"text": string("label text"), "part_id": string(PART), "position": vec3("[x, y, z]")}, ["text"]),
             lambda text, part_id=None, position=None: eng.add_label(text, position, part_id)),
        Tool("label_parts", "Label a part or group with its name ('all' labels every part).", obj({"part_id": string(PART)}),
             lambda part_id="all": eng.label_parts(part_id)),
        Tool("remove_label", "Remove a label by id, or 'all'.", obj({"label_id": string("label id or 'all'")}, ["label_id"]), lambda label_id: eng.remove_label(label_id)),
        Tool("measure_distance", "Distance between two parts (by name) or points.", obj({"point_a": {"type": ["string", "array"], "description": "part or [x,y,z]"},
                                                                                         "point_b": {"type": ["string", "array"], "description": "part or [x,y,z]"}}, ["point_a", "point_b"]),
             lambda point_a, point_b: eng.measure_distance(point_a, point_b)),
        Tool("animate", "Play a named animation ('heartbeat', 'play') or 'stop'.", obj({"name": string("animation name")}, ["name"]), lambda name: eng.animate(name)),
        # ---- knowledge
        Tool("explain", "Retrieve curated educational passages to answer a question. Always use this before explaining science.",
             obj({"question": string("the question"), "concept_id": string("model or part id it is about")}, ["question"]), explain, None, "knowledge", main_thread=False),
        Tool("related_concepts", "What the knowledge graph links to a concept (parts, what it contains, next scale...).",
             obj({"concept_id": string("model or part id"), "relation": string("has_part, contains, scale_down_to, produces, part_of...")}, ["concept_id"]),
             related_concepts, None, "knowledge", main_thread=False),
        # ---- chemistry
        Tool("show_bond_angle", "Show and report the bond angle and shape of the molecule on screen.", obj(), lambda: chem.show_bond_angle(eng), None, "chemistry"),
        Tool("set_representation", "Change how a molecule/protein is drawn.",
             obj({"style": string("representation", ["ball_and_stick", "space_filling", "sticks", "backbone", "atoms", "surface"])}, ["style"]),
             lambda style: chem.set_representation(eng, style), None, "chemistry"),
        Tool("compare_molecules", "Show two or more molecules side by side with their shapes.",
             obj({"model_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4, "description": "molecule model ids"}}, ["model_ids"]),
             compare_molecules, None, "chemistry"),
        Tool("electron_configuration", "Report the electron arrangement of the atom on screen and highlight its outer shell.", obj(), lambda: chem.electron_configuration(eng), None, "chemistry"),
        Tool("molecule_facts", "Formula, mass, shape and polarity of the molecule/atom on screen.", obj(), lambda: chem.molecule_facts(eng), None, "chemistry"),
        # ---- physics
        Tool("set_parameter", "Change a simulation parameter (e.g. angle, initial_velocity, gravity).", obj({"name": string("parameter name"), "value": number("new value")}, ["name", "value"]),
             set_parameter, None, "physics"),
        Tool("play", "Start the simulation or animation.", obj(), play, None, "physics"),
        Tool("pause", "Pause the simulation or animation.", obj(), pause, None, "physics"),
        Tool("reset_simulation", "Return the simulation to t = 0.", obj(), reset_simulation, None, "physics"),
        Tool("step", "Advance the simulation by a number of 1/30 s frames.", obj({"frames": {"type": "integer", "minimum": 1, "maximum": 600, "description": "frames"}}), step, None, "physics"),
        Tool("get_measurements", "Current parameters, equations and measurements of the simulation.", obj(), get_measurements, None, "physics"),
    ]
    for tool in T:
        registry.register(tool)
    return registry
