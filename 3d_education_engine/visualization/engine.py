"""The Visualization Engine: the one object that owns the 3D scene.

Every public method here is something a validated tool can ask for. Nothing
else changes what is on screen -- the agent, the UI buttons and the tests all
come through these methods -- so the screen can never be in a state that no
tool call explains.

The engine renders OFF-SCREEN into an image. That keeps it identical on the
Pi (Mesa v3d), on a desktop GPU and in tests with no display, and lets the Qt
viewport stay a plain image widget.

Threading: VTK is not thread-safe. Call the engine from ONE thread (the UI
thread); the agent reaches it through a main-thread executor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from app.config import Settings, prepare_gl_environment

prepare_gl_environment()
import pyvista as pv  # noqa: E402  (after the GL environment is set)

from database.repository import normalise  # noqa: E402
from models.cache import MemoryManager  # noqa: E402
from models.loader import ModelLoader  # noqa: E402
from models.registry import ModelRegistry  # noqa: E402
from visualization import clipping, highlighting  # noqa: E402
from visualization.annotations import Annotations  # noqa: E402
from visualization.camera import CameraController  # noqa: E402
from visualization.scene import Animator, SceneModel  # noqa: E402
from visualization.transitions import Transition  # noqa: E402
from visualization.transparency import clamp_opacity  # noqa: E402

log = logging.getLogger(__name__)

ALL = {"", "all", "it", "this", "that", "everything", "model", "whole", "whole model", "them"}

# Textbook labels (see VisualizationEngine.textbook_labels): at most this many
# names at once, in columns this many model-radii out from the middle, with the
# camera stepped back this much to make room for them.
LABEL_CAP = 16
LABEL_COLUMN = 1.2
LABEL_ROOM_ZOOM = 0.82
# Narrower than this is Liza's board thumbnail, where names are a smudge.
LABELS_MIN_WIDTH = 500


class EngineError(ValueError):
    """A request the engine understood but cannot carry out; the message says why."""


@dataclass
class PartState:
    visible: bool = True
    opacity: float = 1.0
    highlighted: bool = False
    scale: float = 1.0
    offset: np.ndarray = field(default_factory=lambda: np.zeros(3))
    translation: np.ndarray = field(default_factory=lambda: np.zeros(3))


class VisualizationEngine:
    def __init__(self, settings: Settings, registry: ModelRegistry, loader: ModelLoader,
                 memory: MemoryManager | None = None):
        self.settings = settings
        self.registry = registry
        self.loader = loader
        self.memory = memory
        r = settings.render
        self.plotter = pv.Plotter(off_screen=True, window_size=[r.width, r.height])
        self.plotter.set_background(r.background)
        if r.depth_peeling:
            try:
                self.plotter.enable_depth_peeling(number_of_peels=4)
            except Exception as exc:        # some GPUs cannot; transparency is then approximate
                log.warning("Depth peeling unavailable: %s", exc)
        self.camera = CameraController(self.plotter)
        self.annotations = Annotations(self.plotter)
        self.scene: SceneModel | None = None
        self.state: dict[str, PartState] = {}
        self.actors: dict[str, object] = {}
        self.clip_spec: clipping.ClipSpec | None = None
        self.explode_amount = 0.0
        self.fade = 1.0
        self.animator: Animator | None = None
        self.transition: Transition | None = None
        self.queued_transitions: list[tuple[str, str | None]] = []
        self.listeners: list[Callable[[str, dict], None]] = []
        # "full": title, disclaimer and attribution drawn into the frame.
        # "attribution": only the credit line -- for a host such as Liza that
        # draws the title and the small print around the picture itself.
        self.overlay_mode = "full"
        # Name the parts textbook-style as soon as a model is up (Liza turns
        # this on; the desktop app has its parts list for that).
        self.auto_labels = False
        # Parts a simulation moves whole (a planet: its label rides on it) and
        # parts it rebuilds every frame (an arrow, a trail: no label at all).
        self._moved_parts: set[str] = set()
        self._regrown_parts: set[str] = set()
        # How far the student has zoomed with the buttons since the view was
        # last reset, and the part the camera was last pointed at.
        self.user_zoom = 1.0
        self.focused_part: str | None = None
        self._label_columns = False          # names in columns beside the model
        self.dirty = True

    # ================================================================ helpers
    @property
    def model_id(self) -> str | None:
        return self.scene.model_id if self.scene else None

    def _emit(self, event: str, **data) -> None:
        if event == "parts_changed":
            self._relabel()
        self.dirty = True
        for listener in list(self.listeners):
            try:
                listener(event, data)
            except Exception:
                log.exception("listener failed on %s", event)

    def _require_scene(self) -> SceneModel:
        if self.scene is None:
            raise EngineError("nothing is loaded yet -- ask for a model first, e.g. 'show the heart'")
        return self.scene

    def groups(self) -> dict[str, list[str]]:
        scene = self._require_scene()
        out: dict[str, list[str]] = {}
        entry = self.registry.get(scene.model_id)
        if entry:
            out.update(entry.groups)
        out.update(scene.extras.get("groups", {}))
        return {g: [p for p in members if p in scene.parts] for g, members in out.items()}

    def resolve_parts(self, ref: str | None) -> list[str]:
        """Part ids named by `ref`: a part, a group, the model, or 'all'."""
        scene = self._require_scene()
        text = (ref or "").strip()
        key = normalise(text)
        entry = self.registry.get(scene.model_id)
        if key in ALL or text == scene.model_id or key == normalise(scene.title):
            return list(scene.parts)
        if text.startswith(scene.model_id + "."):
            text = text[len(scene.model_id) + 1:]
            key = normalise(text)
        if text in scene.parts:
            return [text]
        groups = self.groups()
        group_key = key.replace(" ", "_")
        if group_key in groups:
            return groups[group_key]
        if entry:
            group = self.registry.resolve_group(scene.model_id, text)
            if group and group in groups:
                return groups[group]
            part = self.registry.resolve_part(scene.model_id, text)
            if part:
                if part not in scene.parts:
                    note = entry.parts.get(part, {}).get("note", "it is not in this model")
                    raise EngineError(f"{entry.parts[part]['name']} cannot be shown: {note}")
                return [part]
        for pid, part in scene.parts.items():
            if normalise(part.name) == key or pid == group_key:
                return [pid]
        for gid, names in scene.extras.get("group_aliases", {}).items():
            if key in {normalise(n) for n in names} and gid in groups:
                return groups[gid]
        raise EngineError(f"'{ref}' is not a part of {scene.title}. Parts: "
                          + ", ".join(scene.parts) + (". Groups: " + ", ".join(groups) if groups else ""))

    def part_name(self, part_id: str) -> str:
        return self.scene.parts[part_id].name if self.scene and part_id in self.scene.parts else part_id

    def _names(self, ids: list[str]) -> str:
        names = [self.part_name(p) for p in ids]
        if self.scene and len(ids) == len(self.scene.parts):
            return f"the whole {self.scene.title.lower()}"
        return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] if names else "nothing"

    # ================================================================ drawing
    def restyle(self, part_id: str) -> None:
        actor, st, part = self.actors.get(part_id), self.state.get(part_id), self.scene.parts.get(part_id)
        if actor is None or st is None or part is None:
            return
        actor.SetVisibility(bool(st.visible))
        any_highlight = any(s.highlighted for s in self.state.values())
        if st.highlighted:
            highlighting.style_highlighted(actor, part.color)
        elif any_highlight:
            highlighting.style_dimmed(actor, part.color)
        else:
            highlighting.style_normal(actor, part.color, st.opacity)
        prop = actor.GetProperty()
        prop.SetOpacity(prop.GetOpacity() * self.fade)
        center = part.center
        actor.SetOrigin(*center)
        actor.SetScale(st.scale)
        actor.SetPosition(*(st.offset + st.translation))

    def restyle_all(self) -> None:
        for pid in self.actors:
            self.restyle(pid)
        self.dirty = True

    def _clear_scene(self) -> None:
        self._label_columns = False
        self._moved_parts.clear()
        self._regrown_parts.clear()
        self.focused_part = None
        for pid in list(self.actors):
            self.plotter.remove_actor(self.actors.pop(pid), reset_camera=False, render=False)
        self.annotations.clear_labels()
        self.state.clear()
        self.clip_spec = None
        self.explode_amount = 0.0
        if self.animator is not None:
            self.animator = None
        if self.scene is not None and self.memory is not None:
            self.memory.cache.pinned.discard(self.scene.model_id)
        self.scene = None

    def _add_part_actor(self, pid: str) -> None:
        part = self.scene.parts[pid]
        kwargs = dict(smooth_shading=part.smooth_shading, name=f"part:{pid}", reset_camera=False,
                      render=False)
        kwargs.update(part.style)
        if "scalars" not in part.style:
            kwargs["color"] = part.color
        actor = self.plotter.add_mesh(part.mesh, **kwargs)
        self.actors[pid] = actor
        self.state[pid] = PartState(visible=part.visible, opacity=part.opacity)
        self.restyle(pid)

    def render(self) -> np.ndarray:
        """The current frame as an (H, W, 3) uint8 array."""
        self.plotter.render()
        self.dirty = False
        return self.plotter.screenshot(return_img=True)

    def screenshot(self, path: str) -> str:
        self.plotter.render()
        self.plotter.screenshot(path)
        return path

    def resize(self, width: int, height: int) -> None:
        self.plotter.window_size = [max(64, width), max(64, height)]
        self.annotations.set_visible(width >= LABELS_MIN_WIDTH)
        self.dirty = True

    def tick(self, dt: float) -> bool:
        """Advance transitions, animations and simulations. True if a redraw is due."""
        moved = False
        if self.transition is not None:
            if not self.transition.step():
                err = self.transition.error
                self.transition = None
                self._emit("transition_done", error=str(err) if err else None)
                if self.queued_transitions and not err:
                    self._start_transition(*self.queued_transitions.pop(0))
                else:
                    self.queued_transitions.clear()
                    self._auto_label()
            moved = True
        elif self.animator is not None:
            self.animator.tick(dt, self)
            moved = True
        sim = self.simulation
        if sim is not None and sim.playing and self.transition is None:
            sim.advance(dt)
            sim.apply(self)
            moved = True
        if moved:
            self._move_followers()
        return moved or self.dirty

    @property
    def simulation(self):
        return self.scene.extras.get("simulation") if self.scene else None

    # ================================================================ tools: models
    def load_model(self, model_id: str, _keep_fade: bool = False) -> dict:
        entry = self.registry.get(model_id)
        if entry is None:
            raise EngineError(f"there is no model {model_id!r} in the registry")
        return self.show_scene(self.loader.load(model_id), _keep_fade=_keep_fade)

    def show_scene(self, scene: SceneModel, _keep_fade: bool = False) -> dict:
        """Put any SceneModel on screen: a registry model, or a composite such
        as two molecules side by side (whose parts keep their own provenance)."""
        model_id = scene.model_id
        entry = self.registry.get(model_id)
        self._clear_scene()
        self.scene = scene
        if not _keep_fade:
            self.fade = 1.0
            self.transition = None
        for pid in scene.parts:
            self._add_part_actor(pid)
        for label in scene.labels:
            self.annotations.add_label(label.text, label.position, label.id or None)
        self.refresh_overlays()
        self.reset_camera()
        if self.memory is not None:
            self.memory.cache.pinned.add(model_id)
            self.memory.check()
        sim = self.simulation
        if sim is not None:
            sim.reset()
            sim.apply(self)
        if self.transition is None:
            self._auto_label()
        unavailable = [p for p, spec in entry.parts.items() if not spec.get("available", True)] if entry else []
        self._emit("model_loaded", model_id=model_id)
        return {"model_id": model_id, "name": scene.title, "parts": list(scene.parts),
                "groups": list(self.groups()), "unavailable_parts": unavailable,
                "disclaimer": scene.disclaimer, "attribution": scene.attribution,
                "message": f"Showing {scene.title}."}

    def unload_model(self, model_id: str | None = None) -> dict:
        model_id = model_id or self.model_id
        if model_id and self.model_id == model_id:
            self._clear_scene()
            self.annotations.set_overlays("", None, None)
        if model_id and self.memory is not None:
            self.memory.cache.drop(model_id)
        self._emit("model_unloaded", model_id=model_id)
        return {"message": f"Unloaded {model_id}." if model_id else "Nothing was loaded."}

    def refresh_overlays(self) -> None:
        scene = self.scene
        if scene is None:
            return
        full = self.overlay_mode == "full"
        self.annotations.set_overlays(scene.title if full else "", scene.disclaimer if full else None,
                                      scene.attribution if scene.attribution != "Generated model" or full else None)

    def replace_parts(self, parts: dict) -> None:
        """Swap every part for new geometry, keeping the camera (representations)."""
        scene = self._require_scene()
        for pid in list(self.actors):
            self.plotter.remove_actor(self.actors.pop(pid), reset_camera=False, render=False)
        self.state.clear()
        scene.parts = parts
        for pid in parts:
            self._add_part_actor(pid)
        if self.clip_spec:
            for actor in self.actors.values():
                clipping.apply_to_actor(actor, self.clip_spec)
        self._emit("parts_changed")

    def transition_to(self, model_id: str, focus: str | None = None, frames: int | None = None) -> dict:
        if model_id not in self.registry:
            raise EngineError(f"there is no model {model_id!r} in the registry")
        if self.scene is None:
            return self.load_model(model_id)
        name = self.registry.get(model_id).name
        if self.transition is not None:
            # Already moving: go on to this one afterwards (heart -> tissue -> cell).
            self.queued_transitions.append((model_id, focus))
            return {"model_id": model_id, "message": f"Then on to {name}."}
        self._start_transition(model_id, focus, frames)
        return {"model_id": model_id, "message": f"Moving to {name}."}

    def _start_transition(self, model_id: str, focus: str | None, frames: int | None = None) -> None:
        point = None
        if focus:
            try:
                point = self.scene.center(self.resolve_parts(focus))
            except EngineError:
                point = None
        self.annotations.clear_labels()
        self.transition = Transition(self, model_id, frames or self.settings.render.transition_frames, point)
        self._emit("transition_started", model_id=model_id)

    def finish_transition(self) -> None:
        """Run any transitions still in progress to the end (no display, or tests)."""
        while self.transition is not None:
            self.transition.finish()
            err = self.transition.error
            self.transition = None
            if err:
                self.queued_transitions.clear()
                raise EngineError(f"could not load the next model: {err}")
            if self.queued_transitions:
                self._start_transition(*self.queued_transitions.pop(0))
        self._auto_label()

    # ================================================================ tools: parts
    def show_part(self, part: str) -> dict:
        ids = self.resolve_parts(part)
        solo = normalise(part or "") not in ALL
        if solo and len(ids) < len(self.scene.parts):
            # "Show the left ventricle": bring it forward, keep the rest as context.
            for pid, st in self.state.items():
                st.visible = True
                st.highlighted = pid in ids
        else:
            for pid in ids:
                self.state[pid].visible = True
        self.restyle_all()
        self._emit("parts_changed")
        return {"shown": ids, "message": f"Showing {self._names(ids)}."}

    def hide_part(self, part: str) -> dict:
        ids = self.resolve_parts(part)
        for pid in ids:
            self.state[pid].visible = False
            self.state[pid].highlighted = False
        self.restyle_all()
        self._emit("parts_changed")
        return {"hidden": ids, "message": f"Hid {self._names(ids)}."}

    def set_visibility(self, part: str, visible: bool) -> dict:
        """Plain show/hide, with no highlighting (the parts list checkboxes)."""
        ids = self.resolve_parts(part)
        for pid in ids:
            self.state[pid].visible = bool(visible)
        self.restyle_all()
        self._emit("parts_changed")
        return {"parts": ids, "message": f"{'Showing' if visible else 'Hid'} {self._names(ids)}."}

    def isolate(self, part: str) -> dict:
        ids = self.resolve_parts(part)
        for pid, st in self.state.items():
            st.visible = pid in ids
            st.highlighted = False
        self.restyle_all()
        self._emit("parts_changed")
        return {"shown": ids, "message": f"Showing only {self._names(ids)}."}

    def highlight(self, part: str | None) -> dict:
        if part is None or normalise(part) in {"none", "nothing", "off", "clear"}:
            for st in self.state.values():
                st.highlighted = False
            self.restyle_all()
            self._emit("parts_changed")
            return {"highlighted": [], "message": "Highlight cleared."}
        ids = self.resolve_parts(part)
        for pid, st in self.state.items():
            st.highlighted = pid in ids
            if pid in ids:
                st.visible = True
        self.restyle_all()
        self._emit("parts_changed")
        return {"highlighted": ids, "message": f"Highlighted {self._names(ids)}."}

    def set_transparency(self, part: str | None, value: float) -> dict:
        value = clamp_opacity(value)
        ids = self.resolve_parts(part)
        for pid in ids:
            self.state[pid].opacity = value
            self.state[pid].highlighted = False if value < 1.0 else self.state[pid].highlighted
        self.restyle_all()
        self._emit("parts_changed")
        return {"parts": ids, "opacity": value,
                "message": f"Set {self._names(ids)} to {int(round(value * 100))}% opacity."}

    def scale_part(self, part_id: str, scale: float) -> None:
        """For animators (heartbeat); not a tool."""
        if part_id in self.state:
            self.state[part_id].scale = float(scale)
            self.restyle(part_id)

    def move_part(self, part_id: str, translation) -> None:
        """For simulations: place a part relative to where it was built."""
        self._moved_parts.add(part_id)
        if part_id in self.state:
            self.state[part_id].translation = np.asarray(translation, dtype=float)
            self.restyle(part_id)

    def update_part_mesh(self, part_id: str, mesh) -> None:
        """For simulations: new geometry for a part (a growing trail, a moving wave)."""
        self._regrown_parts.add(part_id)
        if self.scene and part_id in self.scene.parts:
            self.scene.parts[part_id].mesh.copy_from(mesh, deep=True)
            self.dirty = True

    # ================================================================ tools: camera
    def rotate(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> dict:
        self._require_scene()
        for name, v in (("x", x), ("y", y), ("z", z)):
            if not -360.0 <= v <= 360.0:
                raise EngineError(f"rotate {name}: angles are in degrees, between -360 and 360")
        self.camera.rotate(x, y, z)
        self.dirty = True
        return {"message": "Rotated.", "camera": self.camera.state()}

    def zoom(self, amount: float) -> dict:
        self._require_scene()
        if not 0.05 <= amount <= 20.0:
            raise EngineError("zoom amount is a factor: above 1 zooms in, below 1 zooms out (0.05-20)")
        self.camera.zoom(amount)
        self.dirty = True
        return {"message": "Zoomed in." if amount > 1 else "Zoomed out."}

    def focus(self, part: str) -> dict:
        ids = self.resolve_parts(part)
        self.focused_part = ids[0] if len(ids) == 1 else part
        for pid in ids:
            self.state[pid].visible = True
        self.restyle_all()
        self.camera.focus(self.scene.bounds(ids))
        self.dirty = True
        return {"focused": ids, "message": f"Centred on {self._names(ids)}."}

    def reset_camera(self) -> dict:
        scene = self._require_scene()
        entry = self.registry.get(scene.model_id)
        front = scene.extras.get("front") or (entry.manifest.get("view", {}).get("front") if entry else None) or "iso"
        self.camera.home(scene.bounds(list(scene.parts)), front, *scene.view)
        if scene.extras.get("zoom"):
            self.camera.zoom(float(scene.extras["zoom"]))
        if self._label_columns:
            # Room either side for the columns of names.
            self.camera.zoom(LABEL_ROOM_ZOOM)
        self.user_zoom = 1.0
        self.dirty = True
        return {"message": "View reset."}

    def set_camera(self, position, target, up=None) -> dict:
        self._require_scene()
        if len(position) != 3 or len(target) != 3:
            raise EngineError("position and target are [x, y, z]")
        self.camera.set(position, target, up)
        self.dirty = True
        return {"message": "Camera set.", "camera": self.camera.state()}

    # ================================================================ tools: cutting, exploding
    def clip(self, plane: str, position: float = 0.5, keep: str = "negative") -> dict:
        scene = self._require_scene()
        spec = clipping.plane_for(clipping.PLANE_WORDS.get(plane, plane), position, keep,
                                  scene.bounds(list(scene.parts)))
        self.clip_spec = spec
        for actor in self.actors.values():
            clipping.apply_to_actor(actor, spec)
        self._emit("clipped")
        side = {"x": "from side to side", "y": "from front to back", "z": "across the middle"}[spec.axis]
        return {"plane": spec.axis, "position": position, "keep": keep,
                "message": f"I've cut it open {side} so you can see inside."}

    def cut_through(self, part: str, plane: str = "y") -> dict:
        """Cut the model open through the middle of a part, and highlight it:
        "show me what is inside the left ventricle"."""
        scene = self._require_scene()
        ids = self.resolve_parts(part)
        axis = clipping.PLANE_WORDS.get(plane, plane)
        b = scene.bounds(list(scene.parts))
        i = clipping.AXES[axis]
        centre = scene.center(ids)[i]
        position = float(np.clip((centre - b[2 * i]) / ((b[2 * i + 1] - b[2 * i]) or 1.0), 0.0, 1.0))
        result = self.clip(axis, position, "positive")
        self.highlight(part)
        # Undimmed context: the cut already shows the inside.
        for pid, st in self.state.items():
            st.highlighted = pid in ids
        self.restyle_all()
        return {**result, "message": f"Cut open through {self._names(ids)} to show the inside."}

    def clear_clip(self) -> dict:
        self.clip_spec = None
        for actor in self.actors.values():
            clipping.apply_to_actor(actor, None)
        self._emit("clipped")
        return {"message": "Cut removed; the model is whole again."}

    def explode(self, amount: float) -> dict:
        scene = self._require_scene()
        if not 0.0 <= amount <= 3.0:
            raise EngineError("explode amount is between 0 (assembled) and 3 (far apart)")
        center = scene.center(list(scene.parts))
        for pid, part in scene.parts.items():
            self.state[pid].offset = (part.center - center) * amount
        self.explode_amount = amount
        self.restyle_all()
        self._emit("parts_changed")
        return {"message": "Put back together." if amount == 0 else "Pulled the parts apart."}

    # ================================================================ tools: labels, measuring
    def add_label(self, text: str, position=None, part: str | None = None) -> dict:
        scene = self._require_scene()
        if position is None:
            ids = self.resolve_parts(part) if part else list(scene.parts)
            position = scene.center(ids)
            if len(ids) == 1:
                offset = self.state[ids[0]].offset + self.state[ids[0]].translation
                position = position + offset
        if len(position) != 3:
            raise EngineError("label position is [x, y, z]")
        label = self.annotations.add_label(text[:80], position)
        self.dirty = True
        return {"label_id": label.id, "message": f"Labelled '{text[:80]}'."}

    def label_parts(self, part: str | None = None) -> dict:
        """Name each part, textbook-style (see textbook_labels). Labelling some
        parts replaces the labels that were up, so the columns stay tidy."""
        self._require_scene()
        ids = [pid for pid in self.resolve_parts(part) if self.state[pid].visible]
        made = self.textbook_labels(ids)
        return {"labels": made, "message": f"Labelled {len(made)} part(s)."}

    # ---------------------------------------------------------------- textbook labels
    def _part_points(self, pid: str) -> np.ndarray:
        st = self.state[pid]
        pts = np.asarray(self.scene.parts[pid].mesh.points, dtype=float)
        if len(pts) > 6000:
            pts = pts[:: len(pts) // 6000 + 1]
        return pts + st.offset + st.translation

    def _label_ids(self) -> list[str]:
        """The parts named when a model first appears: the manifest's own
        choice when it has one (the skeleton's 27 parts are too many), else
        every part up to LABEL_CAP, keeping the biggest."""
        scene = self.scene
        entry = self.registry.get(scene.model_id)
        wanted = (entry.manifest.get("default_labels") if entry else None) or list(scene.parts)
        ids = [p for p in wanted if p in scene.parts and self.state[p].visible
               and p not in self._regrown_parts]
        if len(ids) > LABEL_CAP:
            size = {p: float(np.linalg.norm(np.ptp(self._part_points(p), axis=0))) for p in ids}
            keep = set(sorted(ids, key=lambda p: -size[p])[:LABEL_CAP])
            ids = [p for p in ids if p in keep]
        return ids

    def _auto_label(self) -> None:
        if self.auto_labels and self.scene is not None and not self.scene.labels:
            self.textbook_labels(self._label_ids())
            if self._label_columns:
                # Laid out for the framing the model came up in; stepping back
                # afterwards only widens the margins the columns sit in.
                self.camera.zoom(LABEL_ROOM_ZOOM)

    def textbook_labels(self, ids: list[str]) -> list[str]:
        """Names in two columns either side of the model, each with a line back
        to a point on its part -- the way a textbook labels a cell. A part the
        simulation moves gets its name riding above it instead; one it rebuilds
        every frame (an arrow, a trail) gets none. Laid out for the camera as
        it is now, and fixed to the model after that, so the names turn with it."""
        scene = self._require_scene()
        for lid in [l for l in self.annotations.labels if l.startswith("part_")]:
            self.annotations.remove_label(lid)
        self._label_columns = False
        ids = [p for p in ids if p in scene.parts and p not in self._regrown_parts]
        if not ids:
            return []
        cam = self.plotter.camera
        view = np.array(cam.GetFocalPoint()) - np.array(cam.GetPosition())
        view /= np.linalg.norm(view) or 1.0
        up = np.array(cam.GetViewUp(), dtype=float)
        right = np.cross(view, up)
        right /= np.linalg.norm(right) or 1.0
        up = np.cross(right, view)
        b = np.array(scene.bounds())
        center = np.array([(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2])
        radius = 0.5 * float(np.linalg.norm(b[1::2] - b[0::2])) or 1.0
        self._label_frame = (up, radius)
        placed = []
        entry = self.registry.get(scene.model_id)
        riding = set(self._moved_parts) | set(entry.manifest.get("riding_labels", []) if entry else [])
        for pid in ids:
            if pid in riding:
                continue
            pts = self._part_points(pid)
            # A point on the side of the part facing the camera, as near the
            # middle of it AS SEEN as possible. Not the point nearest its 3D
            # centre: a left-and-right part (both temporal lobes, both femurs)
            # has its centre in the midline, and the nearest point to that was
            # on the hidden inner face -- "Temporal lobe" pointed at the
            # parietal lobe.
            rel = pts - center
            toward = rel @ -view
            front = toward >= (np.quantile(toward, 0.7) if len(pts) > 10 else toward.min())
            flat = np.column_stack([rel @ right, rel @ up])
            middle = flat.mean(axis=0)
            candidates = np.flatnonzero(front)
            anchor = pts[candidates[int(np.argmin(np.linalg.norm(flat[candidates] - middle, axis=1)))]]
            placed.append((pid, anchor, float((anchor - center) @ right), float((anchor - center) @ up)))
        left = [p for p in placed if p[2] < 0]
        rite = [p for p in placed if p[2] >= 0]
        # Even columns: the parts nearest the middle cross over.
        while len(left) > len(rite) + 1:
            left.sort(key=lambda p: p[2])
            rite.append(left.pop())
        while len(rite) > len(left) + 1:
            rite.sort(key=lambda p: -p[2])
            left.append(rite.pop())
        made = []
        for group, side in ((left, -1.0), (rite, 1.0)):
            # Top to bottom in the order the parts are, so no two lines cross.
            group.sort(key=lambda p: -p[3])
            n = len(group)
            heights = np.linspace(0.82, -0.82, n) * radius if n > 1 else [float(np.clip(group[0][3], -0.8 * radius, 0.8 * radius))] if n else []
            for (pid, anchor, _x, _y), h in zip(group, heights):
                at = center + side * right * LABEL_COLUMN * radius + up * h
                label = self.annotations.add_callout(self._label_text(pid), anchor, at, f"part_{pid}",
                                                     "right" if side < 0 else "left")
                made.append(label.id)
                self._label_columns = True
        for pid in ids:
            if pid in riding:
                made.append(self.annotations.add_follower(self._label_text(pid), pid,
                                                          self._follow_point(pid), f"part_{pid}").id)
        self._move_followers()
        self.dirty = True
        return made

    def _label_text(self, pid: str) -> str:
        from visualization.annotations import _wrap
        name = self.part_name(pid).replace(" (right and left)", "")
        return _wrap(name, 24)

    def _follow_point(self, pid: str) -> np.ndarray:
        up, radius = getattr(self, "_label_frame", (np.array([0.0, 0.0, 1.0]), 1.0))
        pts = self._part_points(pid)
        half = 0.5 * float(np.linalg.norm(np.ptp(pts, axis=0)))
        return pts.mean(axis=0) + up * (half + 0.05 * radius)

    def _move_followers(self) -> None:
        """Each riding label to its part again, and stacked where two would
        overlap on screen: the four inner planets pass within a few pixels of
        each other and of the Sun, and their names were one smudge."""
        riders = [l for l in self.annotations.labels.values()
                  if l.follows and self.scene is not None and l.follows in self.scene.parts]
        if not riders:
            return
        ren, cam = self.plotter.renderer, self.plotter.camera
        height = max(1, self.plotter.window_size[1])
        placed = []                                   # (x, y, half-width) on screen
        spots = []
        for label in riders:
            point = self._follow_point(label.follows)
            ren.SetWorldPoint(*point, 1.0)
            ren.WorldToDisplay()
            x, y, _z = ren.GetDisplayPoint()
            spots.append((y, x, label, point))
        up = getattr(self, "_label_frame", (np.array([0.0, 0.0, 1.0]), 1.0))[0]
        for y, x, label, point in sorted(spots, key=lambda s: s[0]):
            half = 4.2 * max(len(line) for line in label.text.split("\n"))
            lift = 0.0
            while any(abs(x - px) < half + ph and abs((y + lift) - py) < 15 for px, py, ph in placed):
                lift += 15
            placed.append((x, y + lift, half))
            if lift:
                depth = float(np.linalg.norm(np.asarray(point) - np.array(cam.GetPosition())))
                per_pixel = 2 * depth * np.tan(np.radians(cam.GetViewAngle()) / 2) / height
                point = np.asarray(point) + up * lift * per_pixel
            self.annotations.move_follower(label.id, point)

    def part_in_view(self, candidates) -> str | None:
        """Which of `candidates` the camera is looking into: the part it was
        last pointed at, if that is one; else the compact part the view is
        centred inside (the nucleus, in the middle of a cell); else the one
        nearest the centre of the view."""
        if self.scene is None:
            return None
        candidates = [p for p in candidates if p in self.scene.parts and self.state[p].visible]
        if not candidates:
            return None
        if self.focused_part in candidates:
            return self.focused_part
        focal = np.array(self.plotter.camera.GetFocalPoint())
        whole = np.array(self.scene.bounds())
        span = float(np.linalg.norm(whole[1::2] - whole[0::2])) or 1.0
        best, best_score = None, None
        for pid in candidates:
            b = np.array(self.scene.parts[pid].bounds)
            size = float(np.linalg.norm(b[1::2] - b[0::2]))
            inside = bool(np.all(focal >= b[0::2] - 1e-6) and np.all(focal <= b[1::2] + 1e-6))
            if inside and size < 0.6 * span:
                score = size / span - 1.0          # inside a compact part beats everything
            else:
                score = float(np.min(np.linalg.norm(self._part_points(pid) - focal, axis=1))) / span
            if best_score is None or score < best_score:
                best, best_score = pid, score
        return best

    def zoom_by(self, amount: float) -> dict:
        """A zoom the student asked for, counted towards diving into a part."""
        result = self.zoom(amount)
        self.user_zoom *= amount
        return result

    def _relabel(self) -> None:
        """After parts were hidden, isolated or pulled apart: the same names,
        laid out again for what is still showing and where it now is."""
        named = [l[len("part_"):] for l in self.annotations.labels if l.startswith("part_")]
        if named and self.scene is not None:
            self.textbook_labels([p for p in named if p in self.state and self.state[p].visible])

    def remove_label(self, label_id: str) -> dict:
        if label_id in {"all", "*"}:
            self.annotations.clear_labels()
            self.dirty = True
            return {"message": "Removed all labels."}
        if not self.annotations.remove_label(label_id):
            raise EngineError(f"no label {label_id!r}; labels: {list(self.annotations.labels)}")
        self.dirty = True
        return {"message": f"Removed {label_id}."}

    def _point(self, ref) -> np.ndarray:
        if isinstance(ref, (list, tuple)) and len(ref) == 3:
            return np.asarray(ref, dtype=float)
        ids = self.resolve_parts(str(ref))
        return self.scene.center(ids)

    def measure_distance(self, point_a, point_b) -> dict:
        scene = self._require_scene()
        a, b = self._point(point_a), self._point(point_b)
        dist = float(np.linalg.norm(b - a))
        text = f"{dist:.1f} {scene.units}"
        self.annotations.add_measurement(a, b, text)
        self.dirty = True
        approx = "" if scene.units == "mm" else " (model units; educational model, not to scale)"
        return {"distance": round(dist, 3), "units": scene.units,
                "message": f"Distance between centres: {text}{approx}."}

    # ================================================================ tools: animation
    def animate(self, name: str) -> dict:
        scene = self._require_scene()
        if normalise(name) in {"stop", "none", "off"}:
            if self.animator is not None:
                self.animator.reset(self)
            self.animator = None
            if self.simulation is not None:
                self.simulation.playing = False
            return {"message": "Stopped."}
        if self.simulation is not None and normalise(name) in {"play", "run", "start", "simulate"}:
            self.simulation.playing = True
            return {"message": f"Running {scene.title}."}
        if name not in scene.animations:
            offered = list(scene.animations) + (["play"] if self.simulation is not None else [])
            raise EngineError(f"{scene.title} has no animation {name!r}; available: {offered or 'none'}")
        self.animator = scene.animations[name]()
        return {"message": f"Playing the {name} animation."}

    # ================================================================ state
    def summary(self) -> dict:
        if self.scene is None:
            return {"model": None}
        scene = self.scene
        return {
            "model": scene.model_id, "name": scene.title,
            "parts": {pid: {"name": p.name, "visible": self.state[pid].visible,
                            "highlighted": self.state[pid].highlighted,
                            "opacity": round(self.state[pid].opacity, 2)}
                      for pid, p in scene.parts.items()},
            "groups": list(self.groups()),
            "clip": None if self.clip_spec is None else {"plane": self.clip_spec.axis,
                                                          "position": self.clip_spec.position},
            "exploded": self.explode_amount,
            "animation": getattr(self.animator, "name", None),
            "animations": list(scene.animations),
            "labels": {k: v.text for k, v in self.annotations.labels.items()},
            "units": scene.units,
            "disclaimer": scene.disclaimer,
            "simulation": self.simulation.describe() if self.simulation is not None else None,
        }
