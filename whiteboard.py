"""The Transcribe Board, full screen: a page to write and draw on, that she can read.

Tap the corner of the board, or say "open the board", and it fills the screen:
a white page, four pens, a rubber, undo and clear. Whatever goes on it -- a
sum, a chemical equation, a force diagram, a drawing -- she understands, two
ways:

    LIVE. Each time the writing stops for a moment she looks at the page and
    says in a chip on it what she read there: "I read: 2x + 3 = 7". Nobody has
    to ask. That is how a child finds out she has understood their handwriting
    BEFORE they ask her anything about it. It is a quick glance, for the
    screen only: the answer to a question reads the picture again, properly
    (see prompts.BOARD_NOTE for what happened when the glance went with it).

    ASKED. "Ask Liza" on the page, or any question while it is open -- "solve
    this", "is this right?", "balance it", "what did I draw?" -- goes to the
    model with a picture of the page attached, by the road a camera photo
    takes (camera.attach). The reply is the ordinary spoken answer, and for
    anything worked out, the working written out beside their own writing
    (solution.py), each step lit as she says it.

This file is the half with no screen in it: the picture of the page, the
reading, the spoken commands, and the hand-over of a tapped question to
ai_loop. The page itself is drawn by ui.py (TutorUI's whiteboard section).
Like camera.py, it imports nothing of the assistant.

STROKES cross from the screen as plain tuples, (points, colour, width, erase),
points in page pixels from the page's top-left corner. An erase stroke is
painted in the page's own white, which is all a rubber on a whiteboard does.
"""

import base64
import io
import json
import math
import os
import re
import threading
import time

from PIL import Image, ImageDraw

from config import (BOARD_ENABLED, BOARD_KEEP_SKETCHES, BOARD_READ_MODEL,
                    BOARD_SEND_MAX)

PAPER = (255, 255, 255)
SKETCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "pictures", "board")

# Space kept round the writing in the picture she is sent, in page pixels. The
# page is cropped to the ink first: a sum in one corner of an 800px page is
# otherwise sent as a speck in a field of white.
MARGIN = 18
# How far a small piece of writing is blown up. A lone "7" is 40px tall on the
# page, and three times that is a digit the model reads without squinting;
# past that it is only making the stroke edges soft.
MAX_ZOOM = 3.0


# ------------------------------------------------------------------ the picture
def ink_box(strokes):
    """(x0, y0, x1, y1) round everything written, or None for a blank page.
    The rubber does not count: rubbing out an empty corner writes nothing."""
    xs, ys = [], []
    for points, _colour, width, erase in strokes:
        if erase or not points:
            continue
        half = width / 2 + 1
        for x, y in points:
            xs += (x - half, x + half)
            ys += (y - half, y + half)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------ measuring a shape
# A triangle drawn by hand, tidied and measured where it was drawn: its
# corners found from the ink, its angles written in them -- the way the board
# in the reference measures a triangle as 71°, 65° and 44°. All geometry on
# the Pi; the reading only says WHICH shape it is.
POLYGON_CORNERS = {"triangle": 3, "quadrilateral": 4, "square": 4, "rectangle": 4,
                   "rhombus": 4, "parallelogram": 4, "trapezium": 4, "trapezoid": 4,
                   "kite": 4, "pentagon": 5, "hexagon": 6, "heptagon": 7, "octagon": 8,
                   "त्रिभुज": 3, "चतुर्भुज": 4, "वर्ग": 4, "आयत": 4}


def corners_wanted(shape):
    """How many corners the named figure has -- 3 for "Right triangle; AB = 3
    cm" -- or None for a circle, a star or anything not a polygon."""
    name = re.split(r"[;,(]", (shape or "").lower(), maxsplit=1)[0]
    for word, corners in POLYGON_CORNERS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", name) or (
                not word.isascii() and word in name):
            return corners
    return None


def _hull(points):
    """The convex hull, anticlockwise on the page (y grows downwards)."""
    points = sorted(set(points))
    if len(points) < 3:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _area(polygon):
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1)
                   in zip(polygon, polygon[1:] + polygon[:1]))) / 2


def measure_polygon(strokes, corners):
    """The figure drawn on the page as `corners` corners, tidied: {"corners":
    [(x, y), ...] clockwise from the top, page coordinates; "angles": [whole
    degrees, adding up to exactly what they must]}. None when the ink is not
    that shape -- too small, or not filling the outline a polygon would."""
    points = [p for line, _colour, _width, erase in strokes if not erase for p in line]
    hull = _hull(points)
    if len(hull) < corners:
        return None
    # The corners are what is left of the outline when the points that bend
    # it least are taken away one at a time (Visvalingam): on a hand-drawn
    # triangle, everything but its three corners is nearly straight.
    shape = list(hull)
    while len(shape) > corners:
        losses = [_area([shape[i - 1], shape[i], shape[(i + 1) % len(shape)]])
                  for i in range(len(shape))]
        shape.pop(losses.index(min(losses)))
    hull_area = _area(hull)
    if hull_area < 900 or _area(shape) < 0.88 * hull_area:
        return None
    # The ink should run round the outline, not fill it or wander inside.
    def gap(p):
        best = float("inf")
        for a, b in zip(shape, shape[1:] + shape[:1]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy)
                             / max(1e-9, dx * dx + dy * dy)))
            best = min(best, math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy))
        return best
    span = max(math.dist(a, b) for a in shape for b in shape)
    near = sum(1 for p in points if gap(p) < span * 0.08)
    if near < 0.8 * len(points):
        return None
    # Clockwise on the screen, starting from the top corner: A at the top,
    # as a book letters a triangle.
    cx = sum(x for x, _ in shape) / corners
    cy = sum(y for _, y in shape) / corners
    shape.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
    top = min(range(corners), key=lambda i: (shape[i][1], shape[i][0]))
    shape = shape[top:] + shape[:top]
    raw = []
    for i, (x, y) in enumerate(shape):
        (ax, ay), (bx, by) = shape[i - 1], shape[(i + 1) % corners]
        first = math.atan2(ay - y, ax - x)
        second = math.atan2(by - y, bx - x)
        angle = abs(math.degrees(first - second)) % 360
        raw.append(360 - angle if angle > 180 else angle)
    # Whole degrees that still add up: 180 for a triangle, 360 for four sides.
    total = (corners - 2) * 180
    angles = [int(a) for a in raw]
    for i in sorted(range(corners), key=lambda i: raw[i] - int(raw[i]),
                    reverse=True)[:total - sum(angles)]:
        angles[i] += 1
    return {"corners": [(round(x, 1), round(y, 1)) for x, y in shape], "angles": angles}


def snapshot(strokes, page_size):
    """The page as she should see it: cropped to the writing, scaled so the
    longest side is BOARD_SEND_MAX or the writing is MAX_ZOOM times its size,
    black-on-white. None when nothing is written."""
    box = ink_box(strokes)
    if box is None:
        return None
    page_w, page_h = page_size
    x0 = max(0.0, box[0] - MARGIN)
    y0 = max(0.0, box[1] - MARGIN)
    x1 = min(float(page_w), box[2] + MARGIN)
    y1 = min(float(page_h), box[3] + MARGIN)
    # Never narrower than this, so a single dash is still a picture of a page.
    w, h = max(x1 - x0, 120.0), max(y1 - y0, 80.0)
    scale = min(MAX_ZOOM, BOARD_SEND_MAX / max(w, h))
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    image = Image.new("RGB", size, PAPER)
    draw = ImageDraw.Draw(image)
    for points, colour, width, erase in strokes:
        if not points:
            continue
        fill = PAPER if erase else colour
        line = [((x - x0) * scale, (y - y0) * scale) for x, y in points]
        thick = max(1, round(width * scale))
        if len(line) > 1:
            draw.line(line, fill=fill, width=thick, joint="curve")
        # Round ends -- PIL's are square, and a square end on every stroke of
        # a handwritten 3 is what makes it look printed by a robot.
        r = thick / 2
        for cx, cy in (line[0], line[-1]):
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    return image


class Sketch:
    """What was on the page when one question was asked. Quacks like
    camera.Photo, so camera.attach puts it on the message the same way."""

    def __init__(self, image, reading="", version=0):
        out = io.BytesIO()
        image.save(out, "PNG", optimize=True)
        self.png = out.getvalue()
        self.size = image.size
        self.reading = reading
        self.version = version
        self.path = _save(self.png)

    def data_url(self):
        return "data:image/png;base64," + base64.b64encode(self.png).decode()


def _save(png):
    """Keep what she was sent, so an odd answer can be checked against what
    she actually saw -- the last few only."""
    try:
        os.makedirs(SKETCH_DIR, exist_ok=True)
        path = os.path.join(SKETCH_DIR, time.strftime("board-%Y%m%d-%H%M%S.png"))
        with open(path, "wb") as f:
            f.write(png)
        old = sorted(n for n in os.listdir(SKETCH_DIR) if n.startswith("board-"))
        for name in old[:-max(1, BOARD_KEEP_SKETCHES)]:
            os.remove(os.path.join(SKETCH_DIR, name))
        return path
    except OSError:
        return ""


def turn_sketch(ui):
    """A picture of the page for this question -- or None, when the board is
    not open full screen or nothing is written on it."""
    page = getattr(ui, "board_page", None)
    page = page() if page else None
    if not page:
        return None
    version, strokes, size = page
    try:
        image = snapshot(strokes, size)
    except Exception as exc:
        print(f"[BOARD] Could not draw the page ({exc}).", flush=True)
        return None
    if image is None:
        return None
    sketch = Sketch(image, reading_for(version), version)
    print(f"[BOARD] Sending the page: {sketch.size[0]}x{sketch.size[1]}, "
          f"{len(sketch.png) // 1024} KB"
          + (f", read as {sketch.reading!r}" if sketch.reading else "") + ".", flush=True)
    return sketch


# ------------------------------------------------------------------ reading it
READ_PROMPT = """You are reading a student's whiteboard: what they wrote or drew on a touch screen with a finger. Do NOT answer or solve anything. Reply with one line of JSON and nothing else:
{"read": "...", "plot": [], "solve": "", "solid": "", "shape": "", "reaction": "", "molecule": "", "simulation": ""}

read -- exactly what is on it, in one short line:
- Maths: as written, in plain text with Unicode symbols: 2x + 3 = 7, x² − 5x + 6 = 0, ¾ + ½ = ?, √16, ∫ x² dx, sin 30°
- Chemistry: formulas with subscripts and arrows: 2H₂ + O₂ → 2H₂O
- A diagram: a few words naming it and what is marked on it: "a block on a slope with three arrows", "a circuit: a cell, a switch and two bulbs", "a right triangle with sides 3 and 4"
- A drawing: a few words: "a cat", "a cube", "a flower with five petals"
- Words: copied, in the script they are written in -- Devanagari stays Devanagari.
Finger writing is rough: read it as a teacher reads a child's writing, and let the maths decide between look-alikes (1 and 7, x and ×, 5 and S, 0 and o). Several lines: join them with " ; ".

The other fields say what could be shown or worked out. Leave each one empty unless the page clearly has it:
- plot: every function or equation in x on it, each copied as written: ["y = x² − 3", "y = A sin x + B"], ["x² − 5x + 6 = 0"]
- solve: a few words when there is something to work out -- an equation, a sum, a question, an equation to balance: "solve 2x + 3 = 7", "balance the equation", "find the net force", "find the area". Empty for a drawing or a word.
- solid: a 3D solid drawn or named, then any measurements on it: "Cube; side = 4 cm", "Cylinder; radius = 3 cm; height = 7 cm", "Octagonal prism"
- shape: a flat figure drawn or named, then any measurements on it: "Right triangle; AB = 3 cm; BC = 4 cm", "Circle; radius = 5 cm", "Hexagon"
- reaction: a chemical equation, in plain text: "H2 + O2 -> H2O"
- molecule: one compound named or drawn: "H2SO4", "methane"
- simulation: only one of these, when the page shows it: projectile motion, free fall, pendulum, spring, collision, circular motion, orbit, circuit, reflection, refraction, interference, diffraction, standing wave, magnetic field around a wire, electric field

If nothing on it can be read yet: {"read": "?"}"""

# What read() hands back, every field always there.
EMPTY_READING = {"read": "", "plot": [], "solve": "", "solid": "", "shape": "",
                 "reaction": "", "molecule": "", "simulation": ""}

_client = None
_client_lock = threading.Lock()
# Set once the model has refused a picture outright, so a device whose model
# cannot see stops asking it to on every pause in the writing.
_cannot_read = {"why": ""}


def _openrouter():
    global _client
    with _client_lock:
        if _client is None:
            key = os.getenv("OPENROUTER_API_KEY", "")
            if not key:
                return None
            import httpx
            from openai import OpenAI
            _client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1",
                             timeout=httpx.Timeout(15.0, connect=4.0))
        return _client


def read(image):
    """What is written or drawn on the page, as a dict like EMPTY_READING:
    "read" is the line for the chip ("" when nothing on it is legible yet),
    and the rest are what could be shown or worked out -- the functions to
    plot, the solid, the reaction -- which become buttons under the writing."""
    found = dict(EMPTY_READING)
    if _cannot_read["why"]:
        return found
    client = _openrouter()
    if client is None:
        return found
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    url = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    started = time.time()
    try:
        done = client.with_options(max_retries=0).chat.completions.create(
            model=BOARD_READ_MODEL,
            messages=[{"role": "system", "content": READ_PROMPT},
                      {"role": "user", "content": [
                          {"type": "image_url", "image_url": {"url": url}},
                          {"type": "text", "text": "What is on the board?"}]}],
            max_tokens=260, temperature=0,
            extra_body={"reasoning": {"enabled": False}})
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (400, 404) and "image" in str(exc).lower():
            _cannot_read["why"] = str(exc)[:160]
            print(f"[BOARD] {BOARD_READ_MODEL} cannot read pictures; live reading "
                  f"is off ({_cannot_read['why']}).", flush=True)
        else:
            print(f"[BOARD] Could not read the page ({str(exc)[:160]}).", flush=True)
        return found
    found = parse_reading(done.choices[0].message.content or "")
    extras = "; ".join(f"{key}: {value}" for key, value in found.items()
                       if key != "read" and value)
    print(f"[BOARD] Read the page in {time.time() - started:.1f}s: "
          f"{found['read'] or '(nothing legible)'!r}" + (f" ({extras})" if extras else ""),
          flush=True)
    return found


def parse_reading(text):
    """The reader's reply as a dict like EMPTY_READING. JSON as asked -- or,
    from a model that answered in a plain line anyway, that line as "read"."""
    found = dict(EMPTY_READING)
    text = (text or "").strip()
    data = None
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group())
        except ValueError:
            data = None
    if isinstance(data, dict):
        for key in EMPTY_READING:
            value = data.get(key)
            if key == "plot":
                if isinstance(value, str):
                    value = [value]
                if isinstance(value, list):
                    found["plot"] = [str(v).strip() for v in value if str(v).strip()][:4]
            elif isinstance(value, str):
                found[key] = value.strip()
        line = found["read"]
    else:
        line = text.splitlines()[0].strip() if text else ""
    line = line.strip("`\"'“”").strip()
    if line.lower().startswith(("i read:", "the board says:", "it says:")):
        line = line.partition(":")[2].strip()
    if line in ("?", "") or len(line) > 160:
        # Nothing legible -- and so nothing to offer on the strength of it.
        return dict(EMPTY_READING)
    found["read"] = line
    return found


# What can be done with what is written, as buttons under it -- with no
# model between the reading and the button, so they are there a moment after
# the reading is. Each is checked against what this device can really show:
# a "Plot it" whose formula would not plot, or a "See it in 3D" with no model
# behind it, is a button that fails, which is worse than no button.
def suggestions(found, hindi=False):
    """Up to three offers (as actions.make_offer makes them) for a reading."""
    import models3d
    import visuals
    offers = []

    def add(kind, payload, label=None, sub=None):
        if label is None or sub is None:
            words, head = visuals.offer_label(kind, payload, hindi)
            label, sub = label or words, head if sub is None else sub
        if len(sub) > 34:
            sub = sub[:33].rstrip() + "…"
        if all((o["kind"], o["payload"]) != (kind, payload) for o in offers):
            offers.append({"kind": kind, "payload": payload, "settings": [],
                           "label": label, "sub": sub})

    line = found.get("read") or ""
    curves = found.get("plot") or [
        # The reader left the field empty, but "y = x²" is on the chip: the
        # button should not depend on it remembering to fill in both.
        piece for piece in line.split(" ; ")
        if re.match(r"\s*(?:y|f\s*\(\s*x\s*\))\s*=", piece)]
    # |x − 2| as abs(x − 2): a bar is what separates a payload's parts.
    curves = [re.sub(r"\|([^|]+)\|", r"abs(\1)", c) for c in curves]
    if curves:
        try:
            spec = visuals.plot_spec("graph", "; ".join(curves))
        except Exception:
            spec = None
        if spec is not None:
            add("graph", "; ".join(curves),
                label=("ग्राफ़ बनाओ" if hindi else
                       "Plot it" if len(curves) == 1 else "Plot them"),
                sub=", ".join(curves))
    solve = found.get("solve") or ""
    if solve:
        balance = bool(found.get("reaction")) and "balanc" in solve.lower()
        words = (("संतुलित करो" if hindi else "Balance it") if balance else
                 ("हल करो" if hindi else "Solve it"))
        add("ask", "balance" if balance else "solve", label=words, sub=solve)
        # An equation to solve is solved first and plotted second; a function
        # ("y = ...") is the other way round.
        if offers[0]["kind"] == "graph" and not any(
                re.match(r"\s*(?:y|f\s*\(\s*x\s*\))\s*=", c) for c in curves):
            offers.insert(0, offers.pop())
    solid = found.get("solid") or (line if models3d.find_solid(line) else "")
    if solid and models3d.find_solid(solid) is not None:
        add("model3d", solid)
        add("shape", solid)
    shape = found.get("shape") or ""
    if found.get("measure"):
        add("measure", shape, label="कोण नापो" if hindi else "Measure it",
            sub=" · ".join(f"{a}°" for a in found["measure"]["angles"]))
        offers[-1]["measure"] = found["measure"]
        # Their own figure in the geometry lab: corners to drag, sides and
        # corners to tap and measure.
        add("explore", shape, label="जाँचो" if hindi else "Explore it",
            sub="drag its corners" if not hindi else "कोने खींचकर देखो")
        offers[-1]["measure"] = found["measure"]
    elif shape and not solid and visuals.drawable_shape(shape):
        add("shape", shape)
    reaction = found.get("reaction") or ""
    if reaction and re.search(r"->|→|=", reaction):
        add("reaction", reaction)
    molecule = found.get("molecule") or ""
    if molecule and not reaction:
        add("model3d", molecule)
    simulation = found.get("simulation") or ""
    if simulation:
        try:
            import viewer3d
            known = viewer3d.engine_model_for(simulation) is not None
        except Exception:
            known = False
        if known or models3d.find_model(simulation) is not None:
            add("model3d", simulation)
    return offers[:3]


# What the page was last read as, and which version of it. A question asked
# about the page gets the reading only if nothing has been written since.
_reading = {"version": -1, "found": dict(EMPTY_READING)}


def remember_reading(version, found):
    _reading.update(version=version, found=found)


def reading_for(version):
    """The chip's line for this version of the page, "" if it was not read."""
    return _reading["found"]["read"] if _reading["version"] == version else ""


def found_for(version):
    """Everything read off this version of the page, or None if it was not."""
    return _reading["found"] if _reading["version"] == version else None


# The version being read right now, if any -- so a question asked while the
# reading is still on its way can wait a moment for it (see needs_working).
_pending = {"version": None}


def reading_pending(version):
    return _pending["version"] == version


# Words in a question that mean there is something to work out, whatever the
# reading of the page made of it.
RE_WORK_WORDS = re.compile(
    r"\d|solve|calculat|work (?:it )?out|find|how (?:much|many|far|long|fast|big)|area|"
    r"volume|perimeter|balanc|prove|check|हल|गणना|निकालो|कितना|कितने", re.IGNORECASE)


def needs_working(found, question=""):
    """Is there something on the page to WORK OUT -- a sum, an equation, a
    balance, a solid with its measurements -- as against a drawing, a word or
    a name? Only those turns think before answering (assistant.board_tuning):
    thinking about "a cube" cost one answer nineteen seconds of silence."""
    if found is None:
        return True
    if RE_WORK_WORDS.search(question or ""):
        return True
    if found.get("solve") or found.get("plot") or found.get("reaction"):
        return True
    return bool(re.search(r"\d", (found.get("solid") or "") + (found.get("shape") or "")))


class Reader:
    """Reads the page in the background: one look at a time, the newest page
    wins. `on_read(version, found)` is called on the reader's own thread, with
    read()'s dict and, under "suggest", the buttons for it. `hindi()` says
    whether those buttons should be in Hindi."""

    def __init__(self, on_read, hindi=lambda: False):
        self.on_read = on_read
        self.hindi = hindi
        self._lock = threading.Lock()
        self._busy = False
        self._wanted = None

    def request(self, version, strokes, size):
        with self._lock:
            # Anything still waiting was a page that has since changed.
            self._wanted = (version, strokes, size)
            _pending["version"] = version
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._run, daemon=True, name="board-reader").start()

    def _run(self):
        while True:
            with self._lock:
                job, self._wanted = self._wanted, None
                if job is None:
                    self._busy = False
                    return
            version, strokes, size = job
            found = dict(EMPTY_READING)
            try:
                image = snapshot(strokes, size)
                if image is not None:
                    found = read(image)
            except Exception as exc:
                print(f"[BOARD] Reading failed ({exc}).", flush=True)
            corners = corners_wanted(found.get("shape")) if found.get("read") else None
            if corners:
                try:
                    measured = measure_polygon(strokes, corners)
                except Exception as exc:
                    print(f"[BOARD] Could not measure the figure ({exc}).", flush=True)
                    measured = None
                if measured:
                    found = dict(found, measure=dict(measured, name=found["shape"]))
            remember_reading(version, found)
            with self._lock:
                if _pending["version"] == version:
                    _pending["version"] = None
            try:
                found = dict(found, suggest=suggestions(found, self.hindi())
                             if found["read"] else [])
            except Exception as exc:
                print(f"[BOARD] No buttons for that reading ({exc}).", flush=True)
                found = dict(found, suggest=[])
            try:
                self.on_read(version, found)
            except Exception:
                pass


# ------------------------------------------------------------------ "Ask Liza"
# A tap on the page's Ask button lands on the Tk thread, and only ai_loop may
# speak or listen. So the question is left here and ai_loop collects it -- the
# same hand-over a mode tap uses (state.pending_mode_intro), with its own lock
# because it is written and read by two threads.
_ask = {"question": None}
_ask_lock = threading.Lock()


def ask(question, language="", source="board"):
    """`source` is what the question is about: "board" for the page, whose
    picture goes with it; "geometry" for the lab, which says what was
    picked in the device state instead."""
    with _ask_lock:
        _ask["question"] = (question, language, source)


def ask_waiting():
    return _ask["question"] is not None


def take_ask():
    """(question, language, source) left by an Ask button, or None. Taking it
    clears it."""
    with _ask_lock:
        asked, _ask["question"] = _ask["question"], None
    return asked


# ------------------------------------------------------------------ spoken commands
# Opening, closing, wiping and undoing the page are answered on the device,
# without the model, the way the camera switch is: the intent is plain, the
# action is local, and a second spent asking the model is a second the child
# waits with a finger in the air. What these miss, the model catches with
# [ACTION: board_open] / [ACTION: board_close].
#
# Every pattern names the board, or is said while the board is open and is
# short. "I want to write an essay" is a question for her, not a request for
# a page; "I want to draw" on its own is the page.
_OPEN = (
    r"\b(?:open|show|bring\s+up|give\s+me|get)\s+(?:me\s+)?(?:the\s+|your\s+|my\s+|a\s+)?"
    r"(?:transcribe\s+|white\s*|drawing\s+|writing\s+)?board\b(?!\s+(?:exam|result|syllabus|book))",
    r"\b(?:full\s*screen|maximi[sz]e|expand|enlarge)\s+(?:the\s+|your\s+|my\s+)?"
    r"(?:transcribe\s+|white\s*)?board\b",
    r"\bboard\s+(?:in\s+|to\s+|on\s+)?full\s*screen\b",
    r"\bmake\s+(?:the\s+|your\s+|my\s+)?(?:transcribe\s+|white\s*)?board\s+"
    r"(?:full\s*screen|big|bigger|large|larger)\b",
    r"\b(?:write|draw)\s+(?:on|in)\s+(?:the\s+|your\s+|a\s+)?(?:transcribe\s+|white\s*)?board\b",
    r"(?:बोर्ड|व्हाइटबोर्ड|वाइटबोर्ड|board)\s+(?:को\s+)?(?:खोलो|खोल\s+दो|खोलिए|"
    r"फुल\s*स्क्रीन|फ़ुल\s*स्क्रीन|full\s*screen|बड़ा)",
    r"(?:बोर्ड|board)\s+(?:पर|पे|में)\s+(?:कुछ\s+)?(?:लिखना|लिखूँ|लिखूं|लिखने|ड्रा|ड्रॉ|बनाना|बनाऊँ|बनाऊं)",
)
# Only as the whole of a short sentence: "let me draw", "I want to write",
# "मुझे कुछ बनाना है".
_OPEN_SHORT = (
    r"^(?:(?:hey\s+)?liza[,\s]+)?(?:(?:okay|ok|so|now|please)[,\s]+)*"
    r"(?:i\s+want\s+to|i\s+wanna|let\s+me|can\s+i|could\s+i|i(?:'d|\s+would)\s+like\s+to)\s+"
    r"(?:write|draw|scribble|sketch)(?:\s+(?:something|a\s+bit|now|here))?"
    r"(?:\s+please)?[?.!\s]*$",
    r"^(?:मुझे|मैं)\s+(?:कुछ\s+)?(?:लिखना|ड्रा\s+करना|ड्रॉ\s+करना|बनाना|चित्र\s+बनाना)\s+"
    r"(?:है|हैं|चाहता\s+हूँ|चाहती\s+हूँ|चाहता\s+हूं|चाहती\s+हूं)[।?.!\s]*$",
)
_CLOSE = (
    r"\b(?:close|shut|exit|leave|minimi[sz]e|shrink|hide)\s+(?:the\s+|your\s+|this\s+|my\s+)?"
    r"(?:transcribe\s+|white\s*|drawing\s+)?board\b",
    r"\b(?:exit|leave|close|get\s+out\s+of|come\s+out\s+of)\s+(?:the\s+)?full\s*screen\b",
    r"\bmake\s+(?:the\s+|your\s+)?board\s+(?:small|smaller|normal)\b",
    r"^(?:(?:okay|ok|liza|hey\s+liza|now)[,\s]+)*(?:go\s+back|close\s+it|close\s+this|"
    r"i(?:'m|\s+am)\s+done\s+(?:writing|drawing)|back\s+to\s+normal|make\s+it\s+small(?:er)?)"
    r"(?:\s+please)?[.!\s]*$",
    r"(?:बोर्ड|व्हाइटबोर्ड|board)\s+(?:को\s+)?(?:बंद|बन्द|छोटा|हटाओ)",
    r"^(?:वापस|पहले\s+जैसा)\s+(?:करो|कर\s+दो|जाओ|चलो)[।.!\s]*$",
    r"(?:फुल|फ़ुल)\s*स्क्रीन\s+(?:से\s+)?(?:बाहर|हटाओ|बंद)",
)
_CLEAR = (
    r"\b(?:clear|wipe|clean|erase|rub\s+out)\s+(?:out\s+|off\s+|up\s+)?"
    r"(?:the\s+|my\s+|this\s+|your\s+)?(?:white\s*)?(?:board|page|screen)\b",
    r"\b(?:erase|delete|rub\s+out|clear)\s+(?:it\s+all|everything|all\s+of\s+it)\b",
    r"^(?:(?:okay|ok|liza|now)[,\s]+)*(?:start\s+again|start\s+over|new\s+page)[.!\s]*$",
    r"(?:बोर्ड|स्क्रीन|सब|सब\s+कुछ|पेज)\s+(?:को\s+)?(?:साफ|साफ़|मिटा|मिटाओ|क्लियर|clear)",
)
_UNDO = (
    r"^(?:(?:okay|ok|liza|hey\s+liza|please|oops)[,\s]+)*undo(?:\s+(?:that|it|the\s+last\s+"
    r"(?:one|line|stroke)))?(?:\s+please)?[.!\s]*$",
    r"\b(?:erase|delete|remove|rub\s+out)\s+(?:the\s+|my\s+)?last\s+(?:line|stroke|thing|mark|one)\b",
    r"(?:आखिरी|आख़िरी|पिछला|पिछली)\s+(?:वाला\s+|वाली\s+|लाइन\s+)?(?:मिटाओ|मिटा\s+दो|हटाओ|हटा\s+दो)",
)
RE_BOARD_OPEN = re.compile("|".join(_OPEN), re.IGNORECASE)
RE_BOARD_OPEN_SHORT = re.compile("|".join(_OPEN_SHORT), re.IGNORECASE)
RE_BOARD_CLOSE = re.compile("|".join(_CLOSE), re.IGNORECASE)
RE_BOARD_CLEAR = re.compile("|".join(_CLEAR), re.IGNORECASE)
RE_BOARD_UNDO = re.compile("|".join(_UNDO), re.IGNORECASE)


# A question about something OFF the page, while the page is up: the camera,
# a thing in their hand, their book. The page does not go with these, so the
# camera's own rules decide.
RE_NOT_THE_BOARD = re.compile(
    r"\b(?:camera|holding|in\s+my\s+hands?|hold(?:ing)?\s+(?:it\s+)?up|"
    r"my\s+(?:book|textbook|notebook|copy|worksheet))\b|कैमरा|हाथ\s+में|किताब|कॉपी",
    re.IGNORECASE)


# A question about what is ON the page -- "solve this", "is it right?", "what
# did I draw?" -- as against one that only happens to be asked while it is up.
# Those get a word from her straight away while she works it out.
RE_ABOUT_THE_PAGE = re.compile(
    r"\b(?:this|these|that|it|solve|check|right|wrong|correct|mistake|answer|"
    r"wrote|written|write|drew|drawn|drawing|draw|board|page|balance|explain|mean)\b|"
    r"ये|यह|इसे|इसको|इसमें|सही|गलत|ग़लत|हल|बोर्ड|लिखा|बनाया|जवाब|समझाओ",
    re.IGNORECASE)


def available(ui):
    """Whether this device has a full-screen board to open: it needs a screen."""
    return BOARD_ENABLED and hasattr(ui, "board_is_open")


def board_command(text, ui):
    """"open", "close", "clear" or "undo" when the sentence is about the board
    itself; None for anything else, which then goes on to her as usual."""
    text = (text or "").strip()
    if not text or len(text) > 160 or not available(ui):
        return None
    if ui.board_is_open():
        if RE_BOARD_UNDO.search(text):
            return "undo"
        if RE_BOARD_CLEAR.search(text):
            return "clear"
        if RE_BOARD_CLOSE.search(text):
            return "close"
        return None
    if RE_BOARD_OPEN.search(text) or RE_BOARD_OPEN_SHORT.search(text):
        return "open"
    return None


# ------------------------------------------------------------------ for the model
def state_line(ui):
    """WHITEBOARD: for the device state block -- whether the page is open, what
    is on it, and the working she wrote beside it."""
    if not available(ui):
        return "not on this device"
    if not ui.board_is_open():
        return ("closed. They can open it full screen to write and draw on with a "
                "finger: [ACTION: board_open] (rule 7 section N)")
    page = ui.board_page()
    line = "OPEN, full screen"
    if page and ink_box(page[1]) is not None:
        # Not what the live reading made of it: see BOARD_NOTE for why.
        line += (" with their writing on it; a picture of the page comes with their "
                 "message (rule 7 section N)")
    else:
        line += ", nothing written on it yet"
    measured = getattr(ui, "board_measured", lambda: "")()
    if measured:
        line += f". Measured on the page, where they can see it: {measured}"
    buttons = getattr(ui, "board_buttons", lambda: [])()
    if buttons:
        # By label only: what they say is the live reading, which stays out of
        # the model's way (see BOARD_NOTE).
        line += (". Under their writing, buttons for it are up already: "
                 + ", ".join(buttons) + " -- a tap does it, so never ask whether to")
    working = getattr(ui, "board_working", lambda: "")()
    if working:
        line += f". Beside it, your {working}"
    return line
