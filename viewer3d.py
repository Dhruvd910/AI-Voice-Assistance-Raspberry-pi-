"""3D models a child can turn round with a finger.

"Show me a water molecule in 3D" -- a flat drawing answers what the atoms ARE,
and not what the thing LOOKS like: that water is bent, that methane is a
pyramid, that benzene is flat. This draws a model and lets the screen turn it.

TWO HALVES, AND ONLY ONE OF THEM KNOWS ABOUT PYVISTA
    A SCENE is plain data -- balls, sticks and a title -- built here from what
    the model asked for, with nothing imported but RDKit. The RENDERER turns a
    scene into pictures with PyVista (VTK underneath), and it is the only part
    that touches OpenGL. So a scene can be built, logged and tested with no
    display at all, and a new kind of model -- a heart from a .glb file, an
    orbit worked out in code -- is a new scene builder, not a new renderer.
    The biology and physics builders live in models3d.py; molecules here.

WHY A THREAD, AND A WORKER PROCESS BEHIND IT
    A drag on the touch screen sends dozens of events a second, and the Tk
    thread must never wait on a frame. So it drops turn/zoom requests on a
    queue and gets on with it; the Renderer's thread takes EVERYTHING waiting,
    sends it to the worker as one batch, and gets one frame back. That is what
    keeps a fast drag smooth rather than a backlog of stale frames played out
    after the finger has stopped. The worker is a separate process so that a
    GPU driver crash costs a model, not the tutor -- see Renderer.

WHY OFF-SCREEN
    PyVista's own window cannot live inside a Tk canvas with the pip wheels, and
    a second window on top of a full-screen app is a fight with labwc. So it
    draws into memory and the screen shows the picture -- which also keeps
    Liza's own buttons, fonts and Hindi text in charge of everything around it.

THE PI'S GPU
    Mesa's v3d driver offers an older desktop OpenGL than VTK asks for, so the
    version it reports is raised before VTK loads (MESA_GL_VERSION_OVERRIDE).
    It is a request about what to REPORT; if the driver really cannot do
    something VTK needs, the first frame fails and the caller hears so.
"""

import itertools
import json
import math
import os
import queue
import re
import sys
import threading
import time

# Before anything can load VTK. setdefault, so .env can still override it.
os.environ.setdefault("MESA_GL_VERSION_OVERRIDE", "3.3")
os.environ.setdefault("MESA_GLSL_VERSION_OVERRIDE", "330")

# The dark of the full-screen view (#0B1020), so the model sits in the same
# room as every other enlarged thing on this device.
BACKGROUND = (11, 16, 32)

# ---------------------------------------------------------------------------
# the 3D Education Engine (3d_education_engine/)
# ---------------------------------------------------------------------------
# A second drawer, for what the engine has and this file does not: a real
# anatomical heart with named parts (BodyParts3D), tissue, cells and organelles
# to zoom through, molecules from PubChem's own 3D data, physics simulations
# with settings. It runs in its own worker process with its OWN interpreter --
# its packages are not Liza's, and its `ui` package would shadow ui.py if it
# were imported here -- and speaks the same command protocol as the worker at
# the bottom of this file, so the board, the drag and the zoom buttons behave
# the same whichever one is drawing. What it can show is read from a JSON
# catalogue it writes, so nothing of the engine is imported into Liza.
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "3d_education_engine")
ENGINE_PYTHON = os.path.join(ENGINE_DIR, ".venv", "bin", "python")
ENGINE_CATALOG = os.path.join(ENGINE_DIR, "data", "liza_catalog.json")
_catalog = {"mtime": None, "models": {}}
# Words around a name that are not the name: "3D model of the human heart".
RE_ENGINE_FILLER = re.compile(r"^(?:(?:a|an|the|my|3d|model|models|of|show|me)\s+)+")


def engine_catalog():
    """{model_id: {name, kind, aliases, parts...}} -- {} when there is no engine."""
    try:
        mtime = os.path.getmtime(ENGINE_CATALOG)
    except OSError:
        return {}
    if not os.path.isfile(ENGINE_PYTHON):
        return {}
    if mtime != _catalog["mtime"]:
        try:
            with open(ENGINE_CATALOG, encoding="utf-8") as f:
                _catalog["models"] = json.load(f).get("models", {})
            _catalog["mtime"] = mtime
        except (OSError, ValueError) as exc:
            print(f"[3D] The engine catalogue could not be read: {exc}", flush=True)
            return {}
    return _catalog["models"]


def _engine_key(text):
    text = re.sub(r"[_\-]", " ", (text or "").lower())
    text = re.sub(r"[^a-z0-9\u0900-\u097F ]+", " ", text)
    return RE_ENGINE_FILLER.sub("", " ".join(text.split())).strip()


def engine_model_for(payload):
    """The engine's model id for a model3d request, or None.

    EXACT NAMES ONLY -- no "the request contains a known word" guessing. The
    engine calls its animal cell just "cell", and "red blood cells" must still
    reach models3d's red blood cells rather than become an animal cell.
    A molecule request carries its SMILES after a ";" ("Methane; C"); only the
    name is compared, and a name that is both an atom and a molecule
    ("oxygen") goes to whichever the request is for.
    """
    models = engine_catalog()
    if not models:
        return None
    name, _, smiles = (payload or "").partition(";")
    raw = name.strip()
    for mid, info in models.items():
        if raw and raw in info.get("formulas", []):
            return mid
    key = _engine_key(raw)
    variants = [key]
    for prefix in ("human ", "the human "):
        if key.startswith(prefix):
            variants.append(key[len(prefix):])
    for suffix in (" molecule", " structure", " simulation", " model"):
        if key.endswith(suffix):
            variants.append(key[: -len(suffix)])
    wants = ("chemistry.molecule." if smiles.strip() else
             "chemistry.atom." if key.endswith(" atom") or key.startswith("atom ") else "")
    for variant in variants:
        hits = sorted(mid for mid, info in models.items() if variant and variant in info.get("aliases", []))
        if hits:
            preferred = [m for m in hits if wants and m.startswith(wants)]
            return (preferred or hits)[0]
    return None


def engine_scene(model_id):
    info = engine_catalog().get(model_id, {})
    return {"kind": "engine", "model_id": model_id, "title": info.get("name", model_id),
            "legend": [], "note": info.get("disclaimer", ""),
            # Simulations and animations run on ticks; a simulation should not
            # also be spun round by the viewer while the student watches it.
            "spin": info.get("kind") != "simulation", "animated": True}

# ---------------------------------------------------------------------------
# scenes
# ---------------------------------------------------------------------------
# CPK colours, the ones every school textbook and model kit uses -- carbon
# lifted from near-black so it shows on the dark background.
ATOM_COLOURS = {
    "H": "#F2F2F2", "C": "#7A7F8C", "N": "#3B63F5", "O": "#F03A2E",
    "S": "#F5D63B", "P": "#FF8C1A", "F": "#8FE05A", "Cl": "#2FD65A",
    "Br": "#A63A2E", "I": "#9A3FC8", "Na": "#AB6CF2", "K": "#8F4FD4",
    "Ca": "#4FDB3D", "Mg": "#8AE639", "Fe": "#E07A3A", "Si": "#D9B38C",
    "B": "#FFB5B5", "Li": "#CC80FF", "Al": "#BFA6A6", "Zn": "#7D80B0",
    "Cu": "#C88033",
}
OTHER_ATOM = "#E573C9"
# Ball sizes for ball-and-stick, in angstroms: about a third of each atom's real
# size, so the sticks between them show. Hydrogen smallest, as it is.
ATOM_RADII = {"H": 0.24, "C": 0.36, "N": 0.35, "O": 0.34, "F": 0.32, "S": 0.46,
              "P": 0.46, "Cl": 0.45, "Br": 0.50, "I": 0.56}
ION_RADIUS = 0.62                      # Na+, Cl- and the like: no sticks to show
BOND_RADIUS = 0.10
BOND_GAP = 0.13                        # between the sticks of a double bond
# For the colour key under the title. Indian school spelling (sulphur).
ELEMENT_NAMES = {
    "H": "Hydrogen", "C": "Carbon", "N": "Nitrogen", "O": "Oxygen",
    "S": "Sulphur", "P": "Phosphorus", "F": "Fluorine", "Cl": "Chlorine",
    "Br": "Bromine", "I": "Iodine", "Na": "Sodium", "K": "Potassium",
    "Ca": "Calcium", "Mg": "Magnesium", "Fe": "Iron", "Si": "Silicon",
    "B": "Boron", "Li": "Lithium", "Al": "Aluminium", "Zn": "Zinc",
    "Cu": "Copper",
}


def molecule_scene(payload):
    """(scene, "") for "Water", "Ethanol; CCO" or "CCO"; (None, why not).

    The name is resolved the way the flat drawer resolves it -- the school
    table, then a SMILES, then PubChem -- so "show it in 3D" works for exactly
    the molecules "draw it" works for. The shape itself is worked out here by
    RDKit, offline: a real 3D embedding, then a force-field tidy so the bond
    angles are the ones in the textbook (104.5 for water, 109.5 for methane).
    """
    import science
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import AllChem, rdMolDescriptors
    except Exception as exc:
        return None, f"the molecule builder is not installed ({exc})"
    RDLogger.DisableLog("rdApp.*")
    parts = science._parts(payload)
    if not parts:
        return None, "there was no molecule to build"
    mol, name = science._resolve_molecule(parts)
    if mol is None:
        return None, f"I could not work out the structure of {parts[0]}"
    formula = rdMolDescriptors.CalcMolFormula(mol)
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = 7                # the same shape every time it is asked
    if AllChem.EmbedMolecule(mol, params) != 0:
        params.useRandomCoords = True
        if AllChem.EmbedMolecule(mol, params) != 0:
            return None, f"I could not work out the shape of {name or formula}"
    try:
        AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
    except Exception:
        pass                             # the embedding alone is close enough

    conf = mol.GetConformer()
    atoms = []
    for atom in mol.GetAtoms():
        symbol = atom.GetSymbol()
        p = conf.GetAtomPosition(atom.GetIdx())
        bonded = atom.GetDegree() > 0
        atoms.append({"element": symbol, "pos": (p.x, p.y, p.z),
                      "radius": ATOM_RADII.get(symbol, 0.48) if bonded else ION_RADIUS,
                      "colour": ATOM_COLOURS.get(symbol, OTHER_ATOM)})
    bonds = []
    for bond in mol.GetBonds():
        order = bond.GetBondTypeAsDouble()
        bonds.append({"a": bond.GetBeginAtomIdx(), "b": bond.GetEndAtomIdx(),
                      # Aromatic (1.5) is drawn as one stick: a ring of
                      # alternating doubles is a picture of a textbook
                      # argument, not of the molecule.
                      "order": int(order) if order in (1.0, 2.0, 3.0) else 1})
    title = f"{name} ({formula})" if name else formula
    # Which colour is which, carbon first and hydrogen last the way a formula
    # is written, so a child can read the model without being told.
    present = list(dict.fromkeys(a["element"] for a in atoms))
    order = sorted(present, key=lambda e: (e != "C", e == "H", present.index(e)))
    legend = [(e, ELEMENT_NAMES.get(e, e), ATOM_COLOURS.get(e, OTHER_ATOM))
              for e in order]
    return {"kind": "molecule", "title": title, "atoms": atoms, "bonds": bonds,
            "legend": legend,
            # Letters on the balls while there are few enough to read.
            "labels": len(atoms) <= 14}, ""


# Real model files a person has put on the device -- a heart, a skull, a
# skeleton from a model library -- for the things models3d does not build.
# Named for what they are: "human heart.glb" answers "show me a heart".
MODEL_FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "3d-models")
MODEL_FILE_TYPES = (".glb", ".gltf", ".obj", ".stl", ".ply", ".vtk")


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower())) - {
        "the", "a", "an", "of", "3d", "model", "show", "me", "human"}


def find_model_file(payload):
    """A file in 3d-models whose name matches the request, or None."""
    try:
        names = [n for n in os.listdir(MODEL_FILES_DIR)
                 if n.lower().endswith(MODEL_FILE_TYPES)]
    except OSError:
        return None
    wanted = _words(payload)
    best, best_score = None, 0
    for name in names:
        have = _words(os.path.splitext(name)[0])
        score = len(have & wanted)
        if have and score == len(have) and score > best_score:
            best, best_score = name, score
    return os.path.join(MODEL_FILES_DIR, best) if best else None


def build_scene(payload):
    """(scene, "") or (None, a reason a person would say).

    A biology or physics model first (models3d), then a model file somebody
    has put in 3d-models, and last a molecule by name or SMILES. None with a
    reason when nothing fits, and the caller shows a flat picture instead.
    """
    model_id = engine_model_for(payload)
    if model_id:
        return engine_scene(model_id), ""
    import models3d
    builder = models3d.find_model(payload)
    try:
        if builder is not None:
            return builder(payload), ""
        path = find_model_file(payload)
        if path:
            title = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
            return {"kind": "file", "title": title[:1].upper() + title[1:],
                    "path": path, "legend": [], "note": "", "spin": True,
                    "animated": False}, ""
        return molecule_scene(payload)
    except Exception as exc:
        print(f"[3D] Building the model of {payload!r} failed: {exc}", flush=True)
        return None, "the model did not come out right"


# ---------------------------------------------------------------------------
# the renderer
# ---------------------------------------------------------------------------
def _is_light(colour):
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b > 150


def _add_molecule(plotter, scene, detail):
    """Balls and sticks, merged into one mesh per colour: a few actors draw far
    faster than one per atom, and the Pi feels the difference on every frame."""
    import numpy as np
    import pyvista as pv
    atoms, by_colour = scene["atoms"], {}

    def add(colour, mesh):
        by_colour.setdefault(colour, []).append(mesh)

    for atom in atoms:
        add(atom["colour"], pv.Sphere(radius=atom["radius"], center=atom["pos"],
                                      theta_resolution=detail, phi_resolution=detail))
    for bond in scene["bonds"]:
        a, b = atoms[bond["a"]], atoms[bond["b"]]
        pa, pb = np.array(a["pos"]), np.array(b["pos"])
        axis = pb - pa
        length = float(np.linalg.norm(axis))
        if length < 1e-6:
            continue
        axis /= length
        # Sideways from the bond, for spreading a double or triple bond out.
        side = np.cross(axis, [0.0, 0.0, 1.0])
        if np.linalg.norm(side) < 1e-3:
            side = np.cross(axis, [0.0, 1.0, 0.0])
        side /= np.linalg.norm(side)
        order = bond["order"]
        offsets = {1: [0.0], 2: [-0.5, 0.5], 3: [-1.0, 0.0, 1.0]}[order]
        radius = BOND_RADIUS if order == 1 else BOND_RADIUS * 0.7
        middle = (pa + pb) / 2
        for k in offsets:
            shift = side * k * BOND_GAP * 2
            # Two halves, each the colour of the atom at its end: the look of
            # every model kit, and it shows which atom a bond belongs to.
            for start, end, colour in ((pa, middle, a["colour"]),
                                       (middle, pb, b["colour"])):
                add(colour, pv.Cylinder(center=(start + end) / 2 + shift,
                                        direction=end - start, radius=radius,
                                        height=float(np.linalg.norm(end - start)),
                                        resolution=max(8, detail // 2), capping=False))
    for colour, meshes in by_colour.items():
        plotter.add_mesh(pv.merge(meshes), color=colour, smooth_shading=True,
                         specular=0.35, specular_power=18, ambient=0.18)
    if scene.get("labels"):
        points = np.array([a["pos"] for a in atoms])
        # Dark letters on light balls, white on dark ones: white on a white
        # hydrogen could not be read at all. (A dark tag behind each letter
        # was tried; VTK draws it small and off to one side of the text.)
        for dark_text in (True, False):
            chosen = [i for i, a in enumerate(atoms)
                      if _is_light(a["colour"]) == dark_text]
            if chosen:
                plotter.add_point_labels(
                    points[chosen], [atoms[i]["element"] for i in chosen],
                    font_size=17, bold=True, shape=None, show_points=False,
                    text_color="#0B1020" if dark_text else "white",
                    always_visible=True, justification_horizontal="center",
                    justification_vertical="center")


class Renderer:
    """A 3D view, drawn by a worker process and handed back as pictures.

    show(scene) loads a model; turn/zoom/reset move the camera; spin(True)
    turns it slowly on its own until the next touch. Every finished frame is
    handed to `on_frame(PIL.Image)` on THIS object's thread -- the caller moves
    it to Tk. `on_error(reason)` likewise, when the worker cannot draw.

    WHY A WORKER PROCESS AND NOT A THREAD. VTK drives the GPU driver directly,
    and on this Pi the driver is being asked to report an OpenGL version above
    the one it is certified for (see the header). If that ever goes wrong it
    goes wrong as a crash, not an exception -- and a crash in Liza's own process
    is the tutor vanishing mid-lesson. In a worker it is one lost model and a
    sentence saying so. It also keeps VTK's few hundred megabytes out of Liza
    until somebody actually asks for a model, and gives them back on close().
    """

    SPIN_DEG_PER_S = 30.0          # a full turn every twelve seconds
    SPIN_FPS = 15.0

    def __init__(self, on_frame, on_error=None, on_moved=None):
        # on_moved(answer): the engine put a different model up without being
        # asked -- the student zoomed with the buttons far enough into the
        # nucleus to dive into it, or back out again. This object's thread.
        self.on_frame, self.on_error, self.on_moved = on_frame, on_error, on_moved
        self._commands = queue.Queue()
        self._worker = None
        self._worker_kind = None            # "viewer" (this file) or "engine"
        self._tokens = itertools.count(1)
        self._asks = {}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="viewer3d")
        self._thread.start()

    # --- asked from any thread --------------------------------------------
    def show(self, scene, size):
        self._commands.put(("show", scene, tuple(size)))

    def resize(self, size):
        self._commands.put(("resize", tuple(size)))

    def turn(self, d_azimuth, d_elevation):
        self._commands.put(("turn", d_azimuth, d_elevation))

    def zoom(self, factor):
        self._commands.put(("zoom", factor))

    def reset(self):
        self._commands.put(("reset",))

    def spin(self, on):
        self._commands.put(("spin", bool(on)))

    def animate(self, on):
        """Run the model's own motion -- orbits, waves -- or freeze it."""
        self._commands.put(("animate", bool(on)))

    def close(self):
        """Stop drawing and end the worker, handing its memory back."""
        self._commands.put(("close",))

    def ask(self, op, text="", timeout=5.0):
        """Ask the ENGINE something and wait for its answer (a dict), or None.

        op "try": carry out `text` if it is a command the engine is sure of;
        "do": carry out an instruction from Liza's model; "state": describe the
        scene. Only an engine model can answer; anything else answers
        {"handled": False}. Called from ai_loop's thread, never Tk's.
        """
        token = next(self._tokens)
        box = {"event": threading.Event(), "answer": None}
        self._asks[token] = box
        self._commands.put(("ask", token, op, text))
        if not box["event"].wait(timeout):
            self._asks.pop(token, None)
            print(f"[3D] The engine did not answer {op} {text!r} in {timeout:.0f}s.", flush=True)
            return None
        return box["answer"]

    def _answer(self, token, answer):
        box = self._asks.pop(token, None)
        if box is not None:
            box["answer"] = answer
            box["event"].set()

    # --- this object's thread ---------------------------------------------
    def _start_worker(self, kind="viewer"):
        import subprocess
        import sys
        if kind == "engine":
            # The engine's own interpreter, in its own folder: see ENGINE_DIR.
            self._worker = subprocess.Popen(
                [ENGINE_PYTHON, "-m", "app.liza_worker"], cwd=ENGINE_DIR,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        else:
            self._worker = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._worker_kind = kind

    def _worker_ready(self, kind):
        return (self._worker is not None and self._worker.poll() is None
                and self._worker_kind == kind)

    def _exchange(self, batch, kind="viewer"):
        """Send one batch of commands; the worker's reply, or raise."""
        import pickle
        import struct
        if not self._worker_ready(kind):
            # One worker at a time: switching between this file's models and
            # the engine's ends the other one, and its memory with it.
            self._stop_worker()
            self._start_worker(kind)
        data = pickle.dumps(batch)
        self._worker.stdin.write(struct.pack("!I", len(data)) + data)
        self._worker.stdin.flush()
        return pickle.loads(_read_message(self._worker.stdout))

    def _stop_worker(self):
        worker, self._worker = self._worker, None
        if worker is None:
            return
        try:
            worker.stdin.close()
            worker.wait(timeout=2)
        except Exception:
            worker.kill()

    def _run(self):
        from PIL import Image
        spinning, animating, last, scene = False, False, time.time(), None
        while True:
            # Frames on a clock while anything moves by itself; otherwise only
            # when asked, so a still model costs nothing.
            live = scene is not None and (
                (spinning and scene.get("spin", True))
                or (animating and scene.get("animated")))
            try:
                first = self._commands.get(timeout=1 / self.SPIN_FPS if live else None)
                batch = [first]
            except queue.Empty:
                batch = []
            while True:                      # EVERYTHING waiting, then one frame
                try:
                    batch.append(self._commands.get_nowait())
                except queue.Empty:
                    break
            closes = [i for i, c in enumerate(batch) if c[0] == "close"]
            if closes:
                # Only what came AFTER the last close still stands: "close the
                # old model, show the new one" in one batch is the new one.
                spinning, scene = False, None
                self._stop_worker()
                batch = batch[closes[-1] + 1:]
            for command in batch:
                if command[0] == "spin":
                    spinning = command[1]
                elif command[0] == "animate":
                    animating = command[1]
                elif command[0] == "show":
                    scene = command[1]
            batch = [c for c in batch if c[0] not in ("spin", "animate")]
            kind = "engine" if scene is not None and scene.get("kind") == "engine" else "viewer"
            if kind != "engine":
                # Only the engine can answer questions about its scene.
                for c in [c for c in batch if c[0] == "ask"]:
                    self._answer(c[1], {"handled": False, "reason": "no engine model is up"})
                batch = [c for c in batch if c[0] != "ask"]
            now = time.time()
            step = min(now - last, 0.2)
            if scene is not None and spinning and scene.get("spin", True):
                batch.append(("turn", self.SPIN_DEG_PER_S * step, 0.0))
            if scene is not None and animating and scene.get("animated"):
                batch.append(("tick", step))
            last = now
            if not batch:
                continue
            # A worker that died -- or was never started -- is given the model
            # again, so a crash costs one frame and not the rest of the lesson.
            if not self._worker_ready(kind) and scene \
                    and not any(c[0] == "show" for c in batch):
                batch.insert(0, ("show", scene, getattr(self, "_size", (400, 300))))
            for command in batch:
                if command[0] in ("show", "resize"):
                    self._size = command[-1]
            try:
                reply = self._exchange(batch, kind)
            except Exception as exc:
                reply = ("error", f"the 3D drawer stopped ({exc or 'it crashed'})")
                self._stop_worker()
            # The engine adds a dict of answers to its replies, one per "ask".
            answers = reply[-1] if reply[0] in ("frame", "ok") and isinstance(reply[-1], dict) else {}
            for token, answer in answers.items():
                unasked = token not in self._asks
                if answer.get("changed_model") and scene is not None:
                    # "Go one level deeper" put a different model up: a worker
                    # restarted after a crash must show THAT one, not the heart.
                    scene["model_id"] = answer.get("model_id", scene.get("model_id"))
                    scene["title"] = answer.get("title", scene.get("title"))
                    scene["note"] = answer.get("note", scene.get("note"))
                    scene["spin"] = answer.get("spin", scene.get("spin"))
                    if unasked and self.on_moved:
                        self.on_moved(answer)
                self._answer(token, answer)
            if reply[0] == "frame":
                _kind, width, height, pixels = reply[:4]
                self.on_frame(Image.frombytes("RGB", (width, height), pixels))
            elif reply[0] == "error":
                print(f"[3D] {reply[1]}", flush=True)
                spinning = False
                for c in batch:
                    if c[0] == "ask":
                        self._answer(c[1], {"handled": False, "reason": reply[1]})
                if self.on_error:
                    self.on_error(reply[1])


def _read_message(stream):
    import struct
    head = stream.read(4)
    if len(head) < 4:
        raise EOFError("the 3D drawer closed")
    (length,) = struct.unpack("!I", head)
    data = stream.read(length)
    if len(data) < length:
        raise EOFError("the 3D drawer closed mid-frame")
    return data


# ---------------------------------------------------------------------------
# the worker process
# ---------------------------------------------------------------------------
TAU = 2 * math.pi
LABEL_COLOUR = "white"
LEADER_COLOUR = "#8C9BC4"
# Below this width the view is the board's thumbnail, where a dozen labels are
# a smudge. They come back when it is opened full screen.
LABELS_MIN_WIDTH = 500


def _frame_camera(plotter, view=None, parts_scene=False):
    view = view or {}
    plotter.camera_position = view.get("camera", "xz" if parts_scene else "iso")
    plotter.reset_camera()
    if view.get("azimuth"):
        plotter.camera.Azimuth(view["azimuth"])
    if view.get("elevation"):
        plotter.camera.Elevation(view["elevation"])
    plotter.camera.OrthogonalizeViewUp()
    plotter.reset_camera()
    # reset_camera leaves room for the bounding SPHERE, which for a model on a
    # wide screen is a lot of empty dark around it.
    plotter.camera.Zoom(view.get("zoom", 1.25))


def _basis(normal):
    import numpy as np
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    helper = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(n, helper)
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def _point_z_at(mesh, direction):
    """Turn a mesh built along +z so that +z points along `direction`."""
    import numpy as np
    d = np.asarray(direction, float)
    d = d / np.linalg.norm(d)
    z = np.array([0.0, 0.0, 1.0])
    axis = np.cross(z, d)
    if np.linalg.norm(axis) < 1e-9:
        return mesh if d[2] > 0 else mesh.rotate_x(180)
    angle = math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(z, d))))))
    return mesh.rotate_vector(axis / np.linalg.norm(axis), angle, point=(0, 0, 0))


def _mesh(p, at_origin=False):
    """One scene part as a PyVista mesh, in place (or at the origin, for a part
    that will be moved every frame)."""
    import numpy as np
    import pyvista as pv
    shape = p["shape"]
    if shape == "tube":
        return pv.lines_from_points(np.asarray(p["points"], float),
                                    close=p.get("closed", False)).tube(
            radius=p["radius"], n_sides=12)
    if shape in ("points", "particles"):
        ball = pv.Sphere(radius=p["radius"], theta_resolution=12, phi_resolution=12)
        return pv.PolyData(np.asarray(p["centres"], float)).glyph(
            geom=ball, scale=False, orient=False)
    if shape == "sphere":
        m = pv.Sphere(radius=p["radius"], theta_resolution=48, phi_resolution=48)
    elif shape == "ellipsoid":
        m = pv.ParametricEllipsoid(*p["radii"], u_res=40, v_res=40, w_res=40)
    elif shape == "cylinder":
        m = pv.Cylinder(center=(0, 0, 0), direction=(0, 0, 1), radius=p["radius"],
                        height=p["height"], resolution=p.get("resolution", 28))
    elif shape == "cone":
        m = pv.Cone(center=(0, 0, 0), direction=(0, 0, 1), height=p["height"],
                    radius=p["radius"], resolution=28)
    elif shape == "torus":
        m = pv.ParametricTorus(ringradius=p["ring"], crosssectionradius=p["tube"],
                               u_res=64, v_res=16, w_res=16)
    elif shape == "box":
        sx, sy, sz = p["size"]
        m = pv.Box(bounds=(-sx / 2, sx / 2, -sy / 2, sy / 2, -sz / 2, sz / 2))
    elif shape == "disc":
        m = pv.Disc(center=(0, 0, 0), inner=p["inner"], outer=p["outer"],
                    normal=(0, 0, 1), r_res=2, c_res=72)
    elif shape == "icosahedron":
        m = pv.Icosahedron(radius=p["radius"], center=(0, 0, 0))
    elif shape == "prism":
        depth = p["depth"]
        corners = [(x, y, -depth / 2) for x, y in p["points2d"]]
        face = pv.PolyData(np.asarray(corners, float), faces=[len(corners), *range(len(corners))])
        m = face.extrude((0, 0, depth), capping=True)
    elif shape == "revolve":
        profile = np.asarray(p["profile"], float)
        angles = np.linspace(0, TAU, 72)
        rr, aa = np.meshgrid(profile[:, 0], angles, indexing="ij")
        zz, _ = np.meshgrid(profile[:, 1], angles, indexing="ij")
        # clean() stitches the seam where the last angle meets the first; left
        # open, it showed as a crack down the side of every red blood cell.
        m = _surface(pv.StructuredGrid(rr * np.cos(aa), rr * np.sin(aa), zz)).clean(
            tolerance=1e-6)
    else:
        raise ValueError(f"no shape called {shape!r}")
    if "rotate" in p:
        rx, ry, rz = p["rotate"]
        m = m.rotate_x(rx).rotate_y(ry).rotate_z(rz)
    if "direction" in p:
        m = _point_z_at(m, p["direction"])
    elif "normal" in p:
        m = _point_z_at(m, p["normal"])
    if not at_origin:
        m = m.translate(tuple(p.get("centre", (0, 0, 0))))
    return _cut(m, p)


def _cut(m, p):
    """Cutaways, for a model shown opened up: the eye in section, the Earth
    with a corner taken out. Each removes the part named, keeping the rest."""
    centre = tuple(p.get("centre", (0, 0, 0)))
    cut = p.get("cut") or {}
    changed = False
    if "normal" in cut:
        m = m.clip(normal=cut["normal"], origin=centre, invert=True)
        changed = True
    if "box" in cut:
        m = m.clip_box(cut["box"], invert=True)
        changed = True
    if "cut_front" in p:
        m = m.clip(normal=(1, 0, 0), origin=(p["cut_front"], 0, 0), invert=True)
        changed = True
    if "keep_front" in p:
        m = m.clip(normal=(-1, 0, 0), origin=(p["keep_front"], 0, 0), invert=True)
        changed = True
    return _surface(m) if changed else m


def _surface(m):
    """A clipped piece back to a plain surface that shades smoothly."""
    try:
        return m.extract_surface(algorithm="dataset_surface")
    except TypeError:                   # an older PyVista without the keyword
        return m.extract_surface()


class _Stage:
    """One model on one off-screen plotter, and everything that moves in it."""

    def __init__(self, scene, size):
        import pyvista as pv
        started = time.time()
        self.scene = scene
        self.plotter = pv.Plotter(off_screen=True, window_size=list(size))
        self.plotter.set_background([c / 255 for c in BACKGROUND])
        self.movers, self.waves, self.labels = [], [], []
        self.followers = None
        self.clock = 0.0
        kind = scene["kind"]
        if kind == "molecule":
            # Fewer triangles for a big molecule: sucrose is 45 atoms, and at
            # full detail every frame of a drag pays for all of them.
            _add_molecule(self.plotter, scene, 28 if len(scene["atoms"]) <= 20 else 18)
        elif kind == "file":
            self._add_file(scene["path"])
        else:
            self._add_parts(scene)
        if scene.get("lighting") == "sun":
            self._sunlight()
        if any(p.get("opacity", 1.0) < 1.0 for p in scene.get("parts", ())):
            # Correct see-through membranes: without it VTK draws translucent
            # surfaces in whatever order, and the inside of a cell flickers.
            self.plotter.enable_depth_peeling(number_of_peels=4)
        _frame_camera(self.plotter, scene.get("view"), kind == "parts")
        self.advance(0.0)
        self.show_labels(size[0] >= LABELS_MIN_WIDTH)
        print(f"[3D] {scene.get('title')} built in {time.time() - started:.2f}s "
              f"at {size[0]}x{size[1]}.", file=sys.stderr, flush=True)

    # --- building ---------------------------------------------------------
    def _add_parts(self, scene):
        import numpy as np
        import pyvista as pv
        still = {}
        for p in scene["parts"]:
            style = {"color": p["colour"], "opacity": p.get("opacity", 1.0),
                     "lighting": p.get("lighting", True)}
            if "orbit" in p:
                actor = self.plotter.add_mesh(_mesh(p, at_origin=True), smooth_shading=True,
                                              specular=0.3, **style)
                self.movers.append((actor, p["orbit"], p.get("id")))
            elif p["shape"] == "particles":
                ball = pv.Sphere(radius=p["radius"], theta_resolution=10, phi_resolution=10)
                base = np.asarray(p["centres"], float)
                mesh = pv.PolyData(base).glyph(geom=ball, scale=False, orient=False)
                self.plotter.add_mesh(mesh, smooth_shading=True, specular=0.3, **style)
                self.waves.append((mesh, base, p["wave"], ball))
            else:
                key = (style["color"], style["opacity"], style["lighting"])
                still.setdefault(key, []).append(_mesh(p))
        # One actor per look, not per part: a cell is a hundred parts and eight
        # colours, and the Pi feels the difference on every frame of a drag.
        for (colour, opacity, lit), meshes in still.items():
            self.plotter.add_mesh(meshes[0] if len(meshes) == 1 else pv.merge(meshes),
                                  color=colour, opacity=opacity, lighting=lit,
                                  smooth_shading=True, specular=0.3, specular_power=15)
        fixed = [l for l in scene.get("labels", ()) if "follow" not in l]
        for l in fixed:
            self.labels.append(self._text(l["text"], l["at"]))
        if fixed:
            leaders = [(l["anchor"], l["at"]) for l in fixed if l.get("anchor")]
            if leaders:
                pairs = np.array([pt for pair in leaders for pt in pair], float)
                # Stop each line short of its words, so it points AT them
                # rather than running through them.
                for i in range(0, len(pairs), 2):
                    pairs[i + 1] = pairs[i] + (pairs[i + 1] - pairs[i]) * 0.86
                self.labels.append(self.plotter.add_mesh(
                    pv.line_segments_from_points(pairs), color=LEADER_COLOUR,
                    line_width=2, lighting=False))
        moving = [l for l in scene.get("labels", ()) if "follow" in l]
        if moving:
            rides = []
            for l in moving:
                actor = self._text(l["text"], (0, 0, 0), size=13)
                self.labels.append(actor)
                rides.append((actor, l["follow"], np.asarray(l["offset"], float)))
            self.followers = rides

    def _text(self, text, at, size=14):
        """One label: words that always face the camera, at a point in the model.

        One actor per label, rather than PyVista's point labels. Those go
        through VTK's label placer, which quietly drops any label it decides
        overlaps another -- "Mitochondrion" and "Lysosome" vanished from the
        cell -- and cannot be moved once made, so a planet's name stayed on
        the Sun while the planet went round.
        """
        from vtkmodules.vtkRenderingCore import vtkBillboardTextActor3D
        actor = vtkBillboardTextActor3D()
        actor.SetInput(text)
        actor.SetPosition(*at)
        prop = actor.GetTextProperty()
        prop.SetFontSize(size)
        prop.SetBold(True)
        prop.SetColor(1.0, 1.0, 1.0)
        prop.SetShadow(True)            # readable over a light part as well as the dark
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        self._label_layer().AddActor(actor)
        return actor

    def _label_layer(self):
        """A second layer, drawn over the model with the same camera, that
        holds only the words. On the model's own layer a label behind any part
        of it was hidden or dimmed ("Sun" behind the Sun); up here nothing can
        stand in front of one."""
        if getattr(self, "_overlay", None) is None:
            from vtkmodules.vtkRenderingCore import vtkRenderer
            overlay = vtkRenderer()
            overlay.SetLayer(1)
            overlay.InteractiveOff()
            overlay.SetActiveCamera(self.plotter.camera)
            window = self.plotter.render_window
            window.SetNumberOfLayers(2)
            window.AddRenderer(overlay)
            self._overlay = overlay
        return self._overlay

    def _add_file(self, path):
        import pyvista as pv
        if path.lower().endswith((".glb", ".gltf")):
            self.plotter.import_gltf(path)
        else:
            self.plotter.add_mesh(pv.read(path), color="#E6E9F5", smooth_shading=True,
                                  specular=0.3)

    def _sunlight(self):
        """One light, at the Sun, so the far side of the Earth is night."""
        import pyvista as pv
        self.plotter.remove_all_lights()
        sun = pv.Light(position=(0, 0, 0), focal_point=(1, 0, 0), light_type="scene light",
                       intensity=1.3)
        sun.positional = True
        sun.cone_angle = 180
        self.plotter.add_light(sun)
        # A little from the viewer too: night on the Earth is dark, not missing.
        self.plotter.add_light(pv.Light(light_type="headlight", intensity=0.12))

    # --- every frame ------------------------------------------------------
    def advance(self, step):
        import numpy as np
        import pyvista as pv
        self.clock += step
        where = {}
        for actor, orbit, part_id in self.movers:
            around = orbit.get("around", (0, 0, 0))
            centre = where.get(around, np.zeros(3)) if isinstance(around, str) \
                else np.asarray(around, float)
            radius = orbit.get("radius", 0.0)
            if radius:
                u, v = _basis(orbit.get("normal", (0, 0, 1)))
                angle = orbit.get("phase", 0.0) + TAU * self.clock / orbit["period"]
                position = centre + radius * (math.cos(angle) * u + math.sin(angle) * v)
            else:
                position = centre
            actor.SetPosition(*position)
            if part_id:
                where[part_id] = position
        for mesh, base, wave, ball in self.waves:
            travel = np.asarray(wave["travel"], float)
            phase = TAU * self.clock / wave["period"] - TAU * (base @ travel) / wave["wavelength"]
            moved = base + np.outer(wave["amplitude"] * np.sin(phase), wave["axis"])
            mesh.copy_from(pv.PolyData(moved).glyph(geom=ball, scale=False, orient=False))
        if self.followers is not None:
            for actor, part_id, offset in self.followers:
                actor.SetPosition(*(where.get(part_id, np.zeros(3)) + offset))

    def show_labels(self, visible):
        for actor in self.labels:
            actor.SetVisibility(bool(visible))
        # Billboard text is not part of the model's bounds, so framing is not
        # thrown off by it; nothing else to do here.

    def resize(self, size):
        self.plotter.window_size = list(size)
        self.show_labels(size[0] >= LABELS_MIN_WIDTH)

    def reset(self):
        _frame_camera(self.plotter, self.scene.get("view"), self.scene["kind"] == "parts")

    def frame(self):
        self.plotter.render()
        image = self.plotter.screenshot(None, return_img=True)
        return ("frame", image.shape[1], image.shape[0], image[:, :, :3].tobytes())

    def close(self):
        self.plotter.close()


def worker_main():
    """Read command batches on stdin, answer each with one frame on stdout.

    stdout is the reply channel and nothing else may write to it, so it is
    moved to a private descriptor first and fd 1 pointed at stderr: a stray
    print from VTK or PyVista lands in Liza's log instead of in the middle of
    a frame.
    """
    import pickle
    import struct
    replies = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    commands = sys.stdin.buffer
    stage = None
    while True:
        try:
            batch = pickle.loads(_read_message(commands))
        except EOFError:
            break
        try:
            dirty = False
            for command in batch:
                name = command[0]
                if name == "show":
                    if stage is not None:
                        stage.close()
                    stage = _Stage(command[1], command[2])
                    dirty = True
                elif stage is None:
                    continue
                elif name == "resize":
                    stage.resize(command[1])
                    dirty = True
                elif name == "turn":
                    camera = stage.plotter.camera
                    camera.Azimuth(command[1])
                    camera.Elevation(command[2])
                    camera.OrthogonalizeViewUp()
                    dirty = True
                elif name == "zoom":
                    stage.plotter.camera.Zoom(command[1])
                    dirty = True
                elif name == "reset":
                    stage.reset()
                    dirty = True
                elif name == "tick":
                    stage.advance(command[1])
                    dirty = True
            reply = stage.frame() if dirty and stage is not None else ("ok",)
        except Exception as exc:
            reply = ("error", f"the 3D model could not be drawn ({exc})")
        data = pickle.dumps(reply)
        replies.write(struct.pack("!I", len(data)) + data)
        replies.flush()
    if stage is not None:
        stage.close()


if __name__ == "__main__" and "--worker" in sys.argv:
    worker_main()
