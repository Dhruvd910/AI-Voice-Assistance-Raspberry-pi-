"""Geometry you can touch: a solid or a flat figure as corners, edges and faces
that can be picked and measured.

A student turns a cube round with a finger, taps edge AE and then face ABCD,
and is told that AE is perpendicular to ABCD -- 90 degrees -- with the right
angle drawn where they meet. Taps B, D and E and sees the cube cut through
them: an equilateral triangle, sixty degrees in every corner. That is what
this file works out; ui.py draws it and turns taps into picks.

Everything here is pure arithmetic on the figure's own corners, in the
figure's own units -- a cube of side 4 cm has its corners 4 apart -- so a
length is read straight off the coordinates, with no scale to go wrong. The
3D models in models3d.py are pictures to look at; these are figures to
measure, which is why they are built again here, lettered the way a textbook
letters them (ABCD round the bottom of a cube, EFGH above them).

A leaf: models3d is imported for its measurement reader and its solid names,
and nothing else of ours.
"""
import math
import re

import models3d

# The colours of the first, second and third thing picked: what is said
# about them in the panel is written in the same colour.
PICK_COLOURS = ("#DC2626", "#2563EB", "#059669")
MAX_PICKS = 3
TAU = 2 * math.pi
LETTERS = "ABCDEFGHIJKLMNPQRSTUVWXYZ"     # no O: O is a centre


# ---------------------------------------------------------------- vectors
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _len(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    n = _len(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def _mid(points):
    n = len(points)
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n,
            sum(p[2] for p in points) / n)


def _angle(u, v):
    """The angle between two directions, 0 to 180 degrees."""
    nu, nv = _len(u), _len(v)
    if nu < 1e-12 or nv < 1e-12:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(u, v) / (nu * nv)))))


def _line_angle(u, v):
    """The angle between two LINES, which have no direction: 0 to 90."""
    a = _angle(u, v)
    return min(a, 180.0 - a)


def _normal(points):
    """Newell's normal of a polygon -- right for any polygon, even a slightly
    bent one, and pointing the way its corners turn anticlockwise."""
    n = [0.0, 0.0, 0.0]
    for (x0, y0, z0), (x1, y1, z1) in zip(points, points[1:] + points[:1]):
        n[0] += (y0 - y1) * (z0 + z1)
        n[1] += (z0 - z1) * (x0 + x1)
        n[2] += (x0 - x1) * (y0 + y1)
    return _unit(tuple(n))


def _area(points):
    """The area of a flat polygon in space."""
    total = (0.0, 0.0, 0.0)
    for a, b in zip(points, points[1:] + points[:1]):
        total = _add(total, _cross(a, b))
    return _len(total) / 2


# ---------------------------------------------------------------- numbers
def number(value):
    """4.0 -> "4", 2.5 -> "2.5", 6.9282 -> "6.93"."""
    return f"{value:.2f}".rstrip("0").rstrip(".") if abs(value) >= 0.005 else "0"


def degrees(value):
    """90 -> "90°", 54.7356 -> "54.7°"."""
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}°"
    return f"{value:.1f}°"


def surd(value):
    """4√3 for 6.928..., when the square of a length is a whole number that
    is not a square. "" when there is no tidier way to write it."""
    square = value * value
    whole = round(square)
    if whole < 2 or abs(square - whole) > 1e-6 * max(1.0, square):
        return ""
    outside, inside = 1, whole
    for k in range(int(math.isqrt(whole)), 1, -1):
        if inside % (k * k) == 0:
            outside, inside = k, inside // (k * k)
            break
    if inside == 1:
        return ""
    return (f"{outside}√{inside}" if outside > 1 else f"√{inside}")


def length_words(value, unit):
    """"4√3 ≈ 6.93 cm", "5 cm"."""
    exact = surd(value)
    tail = f" {unit}" if unit else ""
    return f"{exact} ≈ {number(value)}{tail}" if exact else f"{number(value)}{tail}"


def first_up(text):
    """"face ABCD is a square" -> "Face ABCD is a square" (not "Face abcd")."""
    return text[:1].upper() + text[1:]


def area_words(value, unit):
    return (f"{number(value)} {unit}²" if unit and unit != "units"
            else f"{number(value)} square units")


def volume_words(value, unit):
    return (f"{number(value)} {unit}³" if unit and unit != "units"
            else f"{number(value)} cubic units")


# ---------------------------------------------------------------- the figure
class Figure:
    """Corners, faces and the rest of one solid or flat figure.

    points  -- (x, y, z), y up, in the figure's own units
    labels  -- a name per point; "" for the points that only shape a curve
    faces   -- lists of point indices, turned to face outwards here
    corners -- the points that are corners to pick (not a circle's points)
    smooth  -- edges inside a curved surface: drawn only at its outline, and
               never picked on their own
    groups  -- face index -> the curved surface it is a piece of; edge ->
               the rim it is a piece of. Picking one piece picks the whole.
    guides  -- (name, i, j): the measuring lines a round solid is drawn with,
               its radius and height, which can be picked like edges
    rounds  -- what a curved surface or a rim measures, by group name
    """

    def __init__(self, name, points, faces, unit="", title=None, flat=False,
                 labels=None, corners=None, smooth=(), groups=None, guides=(),
                 facts=(), rounds=None, face_names=None, kind="solid"):
        self.name = name
        self.title = title or name
        self.unit = unit
        self.flat = flat
        self.kind = kind
        self.points = [tuple(float(c) for c in p) for p in points]
        self.labels = list(labels) if labels else [LETTERS[i % len(LETTERS)]
                                                   for i in range(len(points))]
        self.corners = set(range(len(points))) if corners is None else set(corners)
        self.faces = [list(f) for f in faces]
        self.groups = dict(groups or {})
        self.guides = list(guides)
        self.facts = list(facts)
        self.rounds = dict(rounds or {})
        self.face_names = dict(face_names or {})
        self.smooth = {tuple(sorted(e)) for e in smooth}
        self._orient()
        self.edges = []
        self.edge_faces = {}
        for k, face in enumerate(self.faces):
            for a, b in zip(face, face[1:] + face[:1]):
                key = (min(a, b), max(a, b))
                if key not in self.edge_faces:
                    self.edges.append(key)
                    self.edge_faces[key] = []
                self.edge_faces[key].append(k)

    def _orient(self):
        """Every face's corners turning anticlockwise seen from OUTSIDE --
        what makes its normal point out, which visibility and the angle
        between two faces both depend on."""
        if self.flat:
            for face in self.faces:
                if _normal([self.points[i] for i in face])[2] < 0:
                    face.reverse()
            return
        centre = _mid([self.points[i] for i in self.corners] or self.points)
        for face in self.faces:
            ring = [self.points[i] for i in face]
            if _dot(_normal(ring), _sub(_mid(ring), centre)) < 0:
                face.reverse()

    # ---- what the parts are called ----
    def face_points(self, k):
        return [self.points[i] for i in self.faces[k]]

    def face_normal(self, k):
        return _normal(self.face_points(k))

    def face_name(self, k):
        """"face ABCD", the way a book names it: from its first letter, and
        round whichever way comes next in the alphabet -- not DCBA, which is
        the same face seen from inside."""
        if k in self.face_names:
            return self.face_names[k]
        ring = [self.labels[i] for i in self.faces[k]]
        start = ring.index(min(ring))
        ring = ring[start:] + ring[:start]
        if len(ring) > 2 and ring[-1] < ring[1]:
            ring = [ring[0]] + ring[1:][::-1]
        return "face " + "".join(ring)

    def edge_name(self, a, b):
        return self.labels[a] + self.labels[b]

    def element_name(self, element):
        kind = element[0]
        if kind == "vertex":
            return f"corner {self.labels[element[1]]}"
        if kind in ("edge", "segment"):
            return self.edge_name(element[1], element[2])
        if kind == "guide":
            return self.guides[element[1]][0]
        if kind == "face":
            return self.face_name(element[1])
        if kind in ("surface", "rim"):
            return element[1]
        return ""

    def is_edge(self, a, b):
        key = (min(a, b), max(a, b))
        return key in self.edge_faces and key not in self.smooth and key not in self.groups

    def describe(self):
        """One line for the model: what is open, lettered how."""
        corners = "".join(self.labels[i] for i in sorted(self.corners) if self.labels[i])
        return f"{self.title}, corners {corners}" if corners else self.title


# ---------------------------------------------------------------- building
def _faces_of_prism(n):
    """A prism's faces on two rings of n: 0..n-1 below, n..2n-1 above."""
    return ([list(range(n))[::-1], list(range(n, 2 * n))]
            + [[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)])


def _euler(faces, edges, vertices):
    return f"Faces + vertices − edges = {faces} + {vertices} − {edges} = 2"


def _box(l, b, h):
    # ABCD round the bottom, front-left first; EFGH straight above them.
    points = [(0, 0, b), (l, 0, b), (l, 0, 0), (0, 0, 0),
              (0, h, b), (l, h, b), (l, h, 0), (0, h, 0)]
    faces = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6],
             [3, 0, 4, 7]]
    return points, faces


def cube(a=4.0, unit="", given=True):
    points, faces = _box(a, a, a)
    title = f"Cube (side {number(a)} {unit})".replace(" )", ")") if given else "Cube"
    facts = ["6 square faces, 12 equal edges, 8 corners", _euler(6, 12, 8),
             f"Volume = a³ = {volume_words(a ** 3, unit)}" if given else "Volume = a³",
             (f"Surface area = 6a² = {area_words(6 * a * a, unit)}" if given
              else "Surface area = 6a²")]
    return Figure("Cube", points, faces, unit, title, facts=facts)


def cuboid(l=6.0, b=4.0, h=3.0, unit="", given=True):
    points, faces = _box(l, b, h)
    title = (f"Cuboid ({number(l)} × {number(b)} × {number(h)} {unit})".replace(" )", ")")
             if given else "Cuboid")
    facts = ["6 rectangular faces, 12 edges, 8 corners", _euler(6, 12, 8),
             f"Volume = l × b × h = {volume_words(l * b * h, unit)}",
             f"Surface area = 2(lb + bh + hl) = {area_words(2 * (l * b + b * h + h * l), unit)}"]
    return Figure("Cuboid", points, faces, unit, title, facts=facts)


def _ring(n, side, y=0.0):
    """A regular n-gon of the given side, flat at height y, a side to the front."""
    radius = side / (2 * math.sin(math.pi / n))
    start = math.pi / 2 + math.pi / n
    return [(radius * math.cos(start + TAU * i / n), y, radius * math.sin(start + TAU * i / n))
            for i in range(n)]


def prism(n=3, side=4.0, height=6.0, unit="", given=True):
    ring = _ring(n, side)
    points = ring + [(x, height, z) for x, _y, z in ring]
    name = f"{models3d.POLYGON_NAMES.get(n, f'{n}-sided')} prism"
    title = (f"{name} (side {number(side)}, height {number(height)} {unit})".replace(" )", ")")
             if given else name)
    base_area = models3d.polygon_area(n, side)
    facts = [f"{n + 2} faces: 2 {'triangles' if n == 3 else 'ends'} and {n} rectangles; "
             f"{3 * n} edges; {2 * n} corners", _euler(n + 2, 3 * n, 2 * n),
             f"Volume = base area × height = {number(base_area)} × {number(height)}"
             f" = {volume_words(base_area * height, unit)}"]
    return Figure(name, points, _faces_of_prism(n), unit, title, facts=facts)


def pyramid(n=4, side=4.0, height=4.0, unit="", given=True):
    ring = _ring(n, side)
    points = ring + [(0.0, height, 0.0)]
    faces = [list(range(n))[::-1]] + [[i, (i + 1) % n, n] for i in range(n)]
    name = f"{models3d.POLYGON_NAMES.get(n, f'{n}-sided')} pyramid"
    title = (f"{name} (side {number(side)}, height {number(height)} {unit})".replace(" )", ")")
             if given else name)
    base_area = models3d.polygon_area(n, side)
    facts = [f"{n + 1} faces: the base and {n} triangles; {2 * n} edges; {n + 1} corners",
             _euler(n + 1, 2 * n, n + 1),
             f"Volume = ⅓ × base area × height = {volume_words(base_area * height / 3, unit)}"]
    return Figure(name, points, faces, unit, title, facts=facts)


def tetrahedron(a=4.0, unit="", given=True):
    ring = _ring(3, a)
    height = a * math.sqrt(2.0 / 3.0)
    points = ring + [(0.0, height, 0.0)]
    faces = [[2, 1, 0], [0, 1, 3], [1, 2, 3], [2, 0, 3]]
    title = f"Tetrahedron (edge {number(a)} {unit})".replace(" )", ")") if given else "Tetrahedron"
    facts = ["4 equilateral triangles, 6 equal edges, 4 corners", _euler(4, 6, 4),
             f"Volume = a³ ÷ (6√2) = {volume_words(a ** 3 / (6 * math.sqrt(2)), unit)}"]
    return Figure("Tetrahedron", points, faces, unit, title, facts=facts)


ROUND_STEPS = 40


def cylinder(r=3.0, h=6.0, unit="", given=True):
    n = ROUND_STEPS
    ring = [(r * math.cos(TAU * i / n + math.pi / 2), 0.0, r * math.sin(TAU * i / n + math.pi / 2))
            for i in range(n)]
    points = ring + [(x, h, z) for x, _y, z in ring] + [(0.0, 0.0, 0.0), (0.0, h, 0.0)]
    O, O2 = 2 * n, 2 * n + 1
    labels = [""] * (2 * n) + ["O", "O′"]
    labels[0], labels[n] = "P", "Q"
    faces = _faces_of_prism(n)
    groups = {k: "the curved surface" for k in range(2, n + 2)}
    for i in range(n):
        groups[(min(i, (i + 1) % n), max(i, (i + 1) % n))] = "the bottom rim"
        groups[(min(n + i, n + (i + 1) % n), max(n + i, n + (i + 1) % n))] = "the top rim"
    smooth = [(i, n + i) for i in range(n)]
    title = (f"Cylinder (r = {number(r)}, h = {number(h)} {unit})".replace(" )", ")")
             if given else "Cylinder")
    rounds = {"the curved surface": f"Curved surface = 2πrh = {area_words(TAU * r * h, unit)}",
              "the bottom rim": f"Circumference = 2πr = {length_words(TAU * r, unit)}",
              "the top rim": f"Circumference = 2πr = {length_words(TAU * r, unit)}"}
    facts = ["2 flat circular faces and 1 curved surface; no corners",
             f"Volume = πr²h = {volume_words(math.pi * r * r * h, unit)}",
             f"Total surface = 2πr(r + h) = {area_words(TAU * r * (r + h), unit)}"]
    return Figure("Cylinder", points, faces, unit, title, labels=labels,
                  corners={0, n, O, O2}, smooth=smooth, groups=groups,
                  guides=[("radius OP", O, 0), ("radius O′Q", O2, n), ("height OO′", O, O2)],
                  facts=facts, rounds=rounds,
                  face_names={0: "the bottom (a circle)", 1: "the top (a circle)"})


def cone(r=3.0, h=4.0, unit="", given=True):
    n = ROUND_STEPS
    ring = [(r * math.cos(TAU * i / n + math.pi / 2), 0.0, r * math.sin(TAU * i / n + math.pi / 2))
            for i in range(n)]
    points = ring + [(0.0, h, 0.0), (0.0, 0.0, 0.0)]
    V, O = n, n + 1
    labels = [""] * n + ["V", "O"]
    labels[0] = "P"
    faces = [list(range(n))[::-1]] + [[i, (i + 1) % n, V] for i in range(n)]
    groups = {k: "the curved surface" for k in range(1, n + 1)}
    for i in range(n):
        groups[(min(i, (i + 1) % n), max(i, (i + 1) % n))] = "the rim of the base"
    slant = math.hypot(r, h)
    title = (f"Cone (r = {number(r)}, h = {number(h)} {unit})".replace(" )", ")")
             if given else "Cone")
    rounds = {"the curved surface": f"Curved surface = πrl = {area_words(math.pi * r * slant, unit)}"
                                    f" (slant l = {length_words(slant, unit)})",
              "the rim of the base": f"Circumference = 2πr = {length_words(TAU * r, unit)}"}
    facts = ["1 flat circular face, 1 curved surface, 1 corner (the apex V)",
             f"Slant height l = √(r² + h²) = {length_words(slant, unit)}",
             f"Volume = ⅓πr²h = {volume_words(math.pi * r * r * h / 3, unit)}"]
    return Figure("Cone", points, faces, unit, title, labels=labels, corners={0, V, O},
                  smooth=[(i, V) for i in range(n)], groups=groups,
                  guides=[("radius OP", O, 0), ("height OV", O, V), ("slant height VP", V, 0)],
                  facts=facts, rounds=rounds, face_names={0: "the base (a circle)"})


def sphere(r=3.0, unit="", given=True, half=False):
    bands, around = (6 if half else 12), 24
    points, index = [], {}
    top = 0.0 if half else -math.pi / 2
    for j in range(bands + 1):
        lat = top + (math.pi / 2 - top) * j / bands
        for i in range(around):
            lon = TAU * i / around + math.pi / 2
            index[(j, i)] = len(points)
            points.append((r * math.cos(lat) * math.cos(lon), r * math.sin(lat),
                           r * math.cos(lat) * math.sin(lon)))
    # Collapse each pole's ring to one point is not needed for drawing: the
    # top ring is a tiny circle of identical points, which draws as a dot.
    faces = []
    groups = {}
    for j in range(bands):
        for i in range(around):
            face = [index[(j, i)], index[(j, (i + 1) % around)],
                    index[(j + 1, (i + 1) % around)], index[(j + 1, i)]]
            groups[len(faces)] = "the curved surface"
            faces.append(face)
    rim_ring = [index[(0 if half else bands // 2, i)] for i in range(around)]
    if half:
        faces.append(rim_ring[::-1])
    O = len(points)
    points.append((0.0, 0.0, 0.0))
    labels = [""] * len(points)
    labels[O] = "O"
    labels[rim_ring[0]] = "P"
    rim = "the rim" if half else "the equator"
    smooth = set()
    for face in faces[:len(groups)]:
        for a, b in zip(face, face[1:] + face[:1]):
            smooth.add((min(a, b), max(a, b)))
    for a, b in zip(rim_ring, rim_ring[1:] + rim_ring[:1]):
        key = (min(a, b), max(a, b))
        smooth.discard(key)
        groups[key] = rim
    name = "Hemisphere" if half else "Sphere"
    title = f"{name} (r = {number(r)} {unit})".replace(" )", ")") if given else name
    if half:
        rounds = {"the curved surface": f"Curved surface = 2πr² = {area_words(TAU * r * r, unit)}",
                  rim: f"Circumference = 2πr = {length_words(TAU * r, unit)}"}
        facts = ["1 flat circular face and 1 curved surface",
                 f"Volume = ⅔πr³ = {volume_words(2 * math.pi * r ** 3 / 3, unit)}",
                 f"Total surface = 3πr² = {area_words(3 * math.pi * r * r, unit)}"]
        face_names = {len(faces) - 1: "the flat face (a circle)"}
    else:
        rounds = {"the curved surface": f"Surface area = 4πr² = {area_words(4 * math.pi * r * r, unit)}",
                  rim: f"Circumference of the equator = 2πr = {length_words(TAU * r, unit)}"}
        facts = ["1 curved surface: no faces, edges or corners",
                 f"Volume = ⁴⁄₃πr³ = {volume_words(4 * math.pi * r ** 3 / 3, unit)}",
                 f"Surface area = 4πr² = {area_words(4 * math.pi * r * r, unit)}"]
        face_names = {}
    return Figure(name, points, faces, unit, title, labels=labels, corners={O, rim_ring[0]},
                  smooth=smooth, groups=groups, guides=[("radius OP", O, rim_ring[0])],
                  facts=facts, rounds=rounds, face_names=face_names)


# ---- flat figures: y up, in the x-y plane, A first and anticlockwise ----
def _flat(name, corners, unit, title=None, facts=None, labels=None):
    points = [(x, y, 0.0) for x, y in corners]
    n = len(points)
    facts = facts if facts is not None else [
        f"{n} sides, {n} corners; the angles inside add up to {(n - 2) * 180}°"]
    return Figure(name, points, [list(range(n))], unit, title or name, flat=True,
                  labels=labels, facts=facts, kind="flat")


def triangle_sss(a, b, c, unit="", name="Triangle", title=None):
    """A triangle from its sides: BC = a, CA = b, AB = c. None if those three
    cannot make one."""
    if a + b <= c or b + c <= a or c + a <= b:
        return None
    # B at the origin, C along the base, A above.
    x = (c * c + a * a - b * b) / (2 * a)
    y = math.sqrt(max(0.0, c * c - x * x))
    return _flat(name, [(x, y), (0.0, 0.0), (a, 0.0)], unit, title,
                 facts=["3 sides, 3 corners; the angles inside add up to 180°"])


def regular(n, side=4.0, unit="", given=True):
    names = {3: "Equilateral triangle", 4: "Square", 5: "Regular pentagon", 6: "Regular hexagon",
             7: "Regular heptagon", 8: "Regular octagon", 9: "Regular nonagon",
             10: "Regular decagon"}
    radius = side / (2 * math.sin(math.pi / n))
    start = -math.pi / 2 - math.pi / n
    corners = [(radius * math.cos(start + TAU * i / n), radius * math.sin(start + TAU * i / n))
               for i in range(n)]
    name = names.get(n, f"Regular {n}-gon")
    title = f"{name} (side {number(side)} {unit})".replace(" )", ")") if given else name
    inside = (n - 2) * 180 / n
    facts = [f"{n} equal sides; every angle inside is {degrees(inside)}",
             f"The angles inside add up to {(n - 2) * 180}°; outside ones to 360°",
             f"Area = {area_words(models3d.polygon_area(n, side), unit)}"]
    return _flat(name, corners, unit, title, facts)


def circle(r=4.0, unit="", given=True):
    n = 72
    ring = [(r * math.cos(TAU * i / n), r * math.sin(TAU * i / n), 0.0) for i in range(n)]
    points = ring + [(0.0, 0.0, 0.0)]
    O = n
    labels = [""] * n + ["O"]
    labels[0] = "P"
    labels[n // 2] = "Q"
    groups = {}
    for i in range(n):
        groups[(min(i, (i + 1) % n), max(i, (i + 1) % n))] = "the circle"
    title = f"Circle (r = {number(r)} {unit})".replace(" )", ")") if given else "Circle"
    facts = ["Every point on it is the same distance r from the centre O",
             f"Circumference = 2πr = {length_words(TAU * r, unit)}",
             f"Area = πr² = {area_words(math.pi * r * r, unit)}"]
    return Figure("Circle", points, [list(range(n))], unit, title, flat=True, labels=labels,
                  corners={0, n // 2, O}, groups=groups,
                  guides=[("radius OP", O, 0), ("diameter PQ", 0, n // 2)], facts=facts,
                  rounds={"the circle": f"Circumference = 2πr = {length_words(TAU * r, unit)}"},
                  face_names={0: "the inside of the circle"}, kind="flat")


RE_SIDE = re.compile(r"\b([A-Z])([A-Z])\s*(?:=|:|is)\s*(\d+(?:\.\d+)?)")
RE_ANGLE = re.compile(r"(?:∠|\bangle\s*)([A-Z])?\s*(?:=|:|is|of)?\s*(\d+(?:\.\d+)?)\s*°?",
                      re.IGNORECASE)
FLAT_NAMES = [
    (r"\bright[- ]?angled?\s+triangle\b|\bright\s+triangle\b", "right triangle"),
    (r"\bequilateral\b", "equilateral triangle"),
    (r"\bisosceles\b", "isosceles triangle"),
    (r"\btriangle\b|त्रिभुज", "triangle"),
    (r"\bsquare\b|\bवर्ग\b", "square"),
    (r"\brectangle\b|आयत", "rectangle"),
    (r"\brhombus\b|\bdiamond\b", "rhombus"),
    (r"\bparallelogram\b", "parallelogram"),
    (r"\btrapezi(?:um|a)\b|\btrapezoid\b", "trapezium"),
    (r"\bkite\b", "kite"),
    (r"\bquadrilateral\b", "quadrilateral"),
    (r"\bpentagon\b", "pentagon"), (r"\bhexagon\b", "hexagon"), (r"\bheptagon\b", "heptagon"),
    (r"\boctagon\b", "octagon"), (r"\bnonagon\b", "nonagon"), (r"\bdecagon\b", "decagon"),
    (r"\bcircle\b|वृत्त", "circle"),
]
REGULAR_SIDES = {"pentagon": 5, "hexagon": 6, "heptagon": 7, "octagon": 8, "nonagon": 9,
                 "decagon": 10}


def flat_name(payload):
    """Which flat figure a request names, or None."""
    head = re.split(r"[;,(]", payload or "", maxsplit=1)[0]
    for pattern, name in FLAT_NAMES:
        if re.search(pattern, head, re.IGNORECASE):
            return name
    return None


def _flat_figure(name, payload):
    found, unit = models3d.measurements(payload)
    unit = unit or "units"
    sides = {m.group(1) + m.group(2): float(m.group(3)) for m in RE_SIDE.finditer(payload)}
    angle = next((float(m.group(2)) for m in RE_ANGLE.finditer(payload)
                  if 0 < float(m.group(2)) < 180), None)

    def side(*names, default=None):
        for key in names:
            if key in sides:
                return sides[key]
            if key[::-1] in sides:
                return sides[key[::-1]]
        return default

    given = bool(found or sides)
    a = found.get("a") or found.get("_")
    if name == "right triangle":
        # The right angle at B, as a book draws it: AB up, BC along.
        up = side("AB") or found.get("h") or 3.0
        along = side("BC") or found.get("base") or found.get("b") or 4.0
        title = f"Right triangle (AB = {number(up)}, BC = {number(along)} {unit})".replace(" )", ")")
        return _flat("Right triangle", [(0.0, up), (0.0, 0.0), (along, 0.0)], unit,
                     title if given else "Right triangle",
                     facts=["One angle is 90°: the other two add up to 90°",
                            "AC² = AB² + BC² (Pythagoras)"])
    if name == "equilateral triangle":
        return regular(3, a or side("AB", "BC", "CA") or 4.0, unit, given)
    if name == "isosceles triangle":
        equal = side("AB", "AC") or a or 5.0
        base = side("BC") or found.get("base") or found.get("b") or 6.0
        return triangle_sss(base, equal, equal, unit, "Isosceles triangle") or \
            triangle_sss(6.0, 5.0, 5.0, unit, "Isosceles triangle")
    if name == "triangle":
        ab, bc, ca = side("AB"), side("BC"), side("CA")
        if ab and bc and ca:
            made = triangle_sss(bc, ca, ab, unit, "Triangle",
                                f"Triangle (AB = {number(ab)}, BC = {number(bc)}, "
                                f"CA = {number(ca)} {unit})".replace(" )", ")"))
            if made:
                return made
        base, height = found.get("base") or bc, found.get("h")
        if base and height:
            return _flat("Triangle", [(0.4 * base, height), (0.0, 0.0), (base, 0.0)], unit,
                         f"Triangle (base {number(base)}, height {number(height)} {unit})"
                         .replace(" )", ")"),
                         facts=["Area = ½ × base × height = "
                                + area_words(base * height / 2, unit),
                                "The angles inside add up to 180°"])
        return triangle_sss(7.0, 6.0, 5.0, unit, "Triangle")
    if name == "square":
        s = a or side("AB", "BC") or 4.0
        return regular(4, s, unit, given)
    if name == "rectangle":
        l = found.get("l") or side("AB") or 6.0
        b = found.get("b") or side("BC") or 4.0
        return _flat("Rectangle", [(0, 0), (l, 0), (l, b), (0, b)], unit,
                     f"Rectangle ({number(l)} × {number(b)} {unit})".replace(" )", ")"),
                     facts=["Opposite sides equal, every angle 90°",
                            f"Area = l × b = {area_words(l * b, unit)}",
                            "The diagonals are equal and cut each other in half"])
    if name == "rhombus":
        s = a or side("AB") or 4.0
        t = math.radians(angle or 60.0)
        return _flat("Rhombus", [(0, 0), (s, 0), (s + s * math.cos(t), s * math.sin(t)),
                                 (s * math.cos(t), s * math.sin(t))], unit,
                     f"Rhombus (side {number(s)} {unit})".replace(" )", ")"),
                     facts=["4 equal sides; opposite angles equal",
                            "The diagonals cross at 90° and cut each other in half"])
    if name == "parallelogram":
        p = found.get("base") or side("AB") or found.get("l") or 6.0
        q = side("AD", "BC") or found.get("b") or 4.0
        t = math.radians(angle or 60.0)
        return _flat("Parallelogram", [(0, 0), (p, 0), (p + q * math.cos(t), q * math.sin(t)),
                                       (q * math.cos(t), q * math.sin(t))], unit,
                     facts=["Opposite sides parallel and equal; opposite angles equal",
                            "Angles next to each other add up to 180°"])
    if name == "trapezium":
        bottom = side("AB") or found.get("base") or 7.0
        top = side("DC", "CD") or 4.0
        height = found.get("h") or 3.0
        inset = (bottom - top) / 2
        return _flat("Trapezium", [(0, 0), (bottom, 0), (bottom - inset, height),
                                   (inset, height)], unit,
                     facts=["One pair of parallel sides: AB ∥ DC",
                            f"Area = ½ (AB + DC) × h = {area_words((bottom + top) * height / 2, unit)}"])
    if name == "kite":
        return _flat("Kite", [(0, -3.0), (2.2, 0), (0, 2.0), (-2.2, 0)], unit,
                     facts=["Two pairs of equal sides next to each other",
                            "The diagonals cross at 90°"])
    if name == "quadrilateral":
        return _flat("Quadrilateral", [(0, 0), (6, 0), (5, 4), (1, 3)], unit)
    if name in REGULAR_SIDES:
        return regular(REGULAR_SIDES[name], a or 3.0, unit, given)
    if name == "circle":
        return circle(found.get("r") or a or 4.0, unit, given)
    return None


def build(payload):
    """The figure a request names -- "Cube; side = 4 cm", "Right triangle;
    AB = 3 cm; BC = 4 cm" -- or None when it is not one this can measure."""
    # "Cube; side = 4 cm; pick = A, G": the figure, then what to point at.
    text = re.split(r"\bpick\b|\bselect\b", payload or "", maxsplit=1, flags=re.IGNORECASE)[0]
    head = text.split(";")[0]
    builder = models3d.find_solid(head)
    found, unit = models3d.measurements(text)
    # No unit given -- or no measurements at all, when the figure is drawn
    # at a size of its own -- is still a length of something.
    unit = unit or "units"
    if builder is not None:
        a = found.get("a") or found.get("_")
        if builder is models3d.cube:
            return cube(a or 4.0, unit, bool(a))
        if builder is models3d.cuboid:
            l, b, h = found.get("l"), found.get("b"), found.get("h")
            if l and b and h:
                return cuboid(l, b, h, unit)
            return cuboid(6.0, 4.0, 3.0, unit, given=False)
        if builder is models3d.polygon_prism:
            n = models3d.polygon_sides(head, default=3)
            side = found.get("a") or found.get("base") or 4.0
            height = found.get("h") or found.get("l") or 6.0
            return prism(n, side, height, unit, bool(found.get("a") or found.get("base")))
        if builder is models3d.polygon_pyramid:
            n = models3d.polygon_sides(head, default=4)
            side = found.get("a") or found.get("base") or 4.0
            height = found.get("h") or 4.0
            return pyramid(n, side, height, unit, bool(found.get("a") or found.get("base")))
        if builder is models3d.tetrahedron:
            return tetrahedron(a or 4.0, unit, bool(a))
        if builder is models3d.cylinder:
            return cylinder(found.get("r") or 3.0, found.get("h") or 6.0, unit,
                            bool(found.get("r") and found.get("h")))
        if builder is models3d.cone:
            r, h = found.get("r") or 3.0, found.get("h")
            if h is None and found.get("slant") and found["slant"] > r:
                h = math.sqrt(found["slant"] ** 2 - r * r)
            return cone(r, h or 4.0, unit, bool(found.get("r") and h))
        if builder is models3d.sphere:
            return sphere(found.get("r") or a or 3.0, unit, bool(found.get("r") or a))
        if builder is models3d.hemisphere:
            return sphere(found.get("r") or a or 3.0, unit, bool(found.get("r") or a), half=True)
        return None
    name = flat_name(head)
    return _flat_figure(name, text) if name else None


def picks_in(payload):
    """The "pick = A, G" part of a request, or ""."""
    match = re.search(r"\b(?:pick|select)\w*\s*[=:]?\s*(.+)$", payload or "", re.IGNORECASE)
    return match.group(1) if match else ""


def from_drawing(corners, name, grid=28.0):
    """A flat figure from the corners of one drawn on the board (page pixels,
    y down), one unit to a square of the page's dots."""
    pts = [(x / grid, -y / grid) for x, y in corners]
    cx, cy = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
    pts = [(x - cx, y - cy) for x, y in pts]
    title = f"Your {(name or 'figure').split(';')[0].strip().lower()}"
    figure = _flat(title, pts, "units", title)
    figure.facts.append("1 unit = one square of the board's dots")
    return figure


# ---------------------------------------------------------------- picks
# Where a face is, by the word for it -- in the figure's own frame, the way
# it opens: y up, the front towards the student.
FACE_WORDS = {"bottom": (1, -1), "base": (1, -1), "top": (1, 1), "front": (2, 1),
              "back": (2, -1), "left": (0, -1), "right": (0, 1)}
RE_LETTERS = re.compile(r"(?<![A-Za-z])([A-Z]′?(?:[A-Z]′?)*)(?![a-z])")
RE_POINTING = re.compile(r"\b(?:corner|vertex|vertices|point|points|angle|at)\b|∠",
                         re.IGNORECASE)


def _face_toward(figure, word):
    """The flat face a word like "bottom" means, or None."""
    for k, name in figure.face_names.items():
        if word in name or (word == "base" and "bottom" in name) or (
                word == "bottom" and "base" in name):
            return k
    axis, sign = FACE_WORDS[word]
    best, k_best = 0.7, None
    for k in range(len(figure.faces)):
        if k in figure.groups:
            continue
        lean = figure.face_normal(k)[axis] * sign
        if lean > best:
            best, k_best = lean, k
    return k_best


def picks_from_words(figure, words, strict=False):
    """The picks a sentence names: "AG", "face ABCD", "B, D, E", "edge AE",
    "angle ABC", "the bottom face", "the curved surface", "radius OP" -- for
    her to point with ([ACTION: show_visual:geometry | Cube; pick = AG]).

    `strict` is for a student's own words, through a microphone, where a
    capital letter is as likely to start a sentence ("A cube...") or be "I":
    a lone letter counts only after "corner", "point" or "angle"."""
    picks = []
    index = {label: i for i, label in enumerate(figure.labels) if label}
    for chunk in re.split(r"[,;&]|\band\b|\bwith\b", words or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        lowered = chunk.lower()
        guide = next((g for g, (name, _a, _b) in enumerate(figure.guides)
                      if name.split()[0] in lowered and name.split()[-1] in chunk), None)
        if guide is not None:
            picks.append(("guide", guide))
            continue
        named = next((group for group in set(figure.groups.values()) | set(figure.rounds)
                      if group.replace("the ", "") in lowered), None)
        if named:
            kind = "surface" if "surface" in named else "rim"
            picks.append((kind, named))
            continue
        tokens = [t for t in RE_LETTERS.findall(chunk) if all(l in index for l in
                                                               re.findall(r"[A-Z]′?", t))]
        place = re.search(r"\b(bottom|base|top|front|back|left|right)\b", lowered)
        if place and not tokens and ("face" in lowered or place.group(1) in ("base", "bottom", "top")):
            face = _face_toward(figure, place.group(1))
            if face is not None:
                picks.append(("face", face))
                continue
        if strict:
            # A lone letter only straight after the word that points at it:
            # "corner B", "angle at B" -- never the "I" of "Can I see".
            tokens = [t for t in tokens if len(re.findall(r"[A-Z]′?", t)) > 1
                      or re.sub(r"[^\w′]", "", chunk) == t        # a list's "D" and "E"
                      or re.search(r"(?:\b(?:corners?|vert(?:ex|ices)|points?|angle|at)|∠)"
                                   r"\s+(?:at\s+)?" + re.escape(t) + r"(?![A-Za-z])", chunk,
                                   re.IGNORECASE)]
        for token in tokens:
            found = [index[l] for l in re.findall(r"[A-Z]′?", token)]
            if len(found) == 3 and ("angle" in lowered or "∠" in chunk):
                # Angle ABC: the two lines that meet at B.
                a, b, c = found
                picks += [("edge" if figure.is_edge(b, x) else "segment", min(b, x), max(b, x))
                          for x in (a, c)]
            elif len(found) >= 3 and ("face" in lowered or len(found) == 4):
                face = _face_with(figure, found)
                picks.extend([("face", face)] if face is not None
                             else [("vertex", i) for i in found])
            elif len(found) == 1:
                picks.append(("vertex", found[0]))
            elif len(found) == 2:
                a, b = found
                picks.append(("edge" if figure.is_edge(a, b) else "segment", min(a, b), max(a, b)))
            else:
                picks += [("vertex", i) for i in found]
    unique = []
    for pick in picks:
        if pick not in unique:
            unique.append(pick)
    return unique[:MAX_PICKS]


def lab_line(figure, picks):
    """The lab in words for the model: the figure, its lettering, what is
    picked and what that measures."""
    text = figure.describe()
    if not picks:
        return text + "; nothing picked yet"
    said = measure(figure, picks).sentence()
    return (text + "; picked " + ", ".join(figure.element_name(p) for p in picks)
            + (f": {said}" if said else ""))


def _face_with(figure, corners):
    wanted = set(corners)
    for k, face in enumerate(figure.faces):
        if wanted <= set(face) and len(wanted) >= 3:
            return k
    return None


def add_pick(figure, picks, element):
    """`picks` with `element` added -- or taken away again if it was there.
    Two corners in a row are a line between them; a fourth pick starts over."""
    if element in picks:
        return [p for p in picks if p != element]
    picks = list(picks) + [element]
    if len(picks) > MAX_PICKS:
        picks = [element]
    return picks


# ---------------------------------------------------------------- measuring
class Reading:
    """What the picks measure: lines for the panel, marks for the figure, and
    one sentence for her."""

    def __init__(self):
        self.lines = []        # (text, colour or None, bold)
        self.marks = []        # see the mark helpers below
        self.say = []

    def line(self, text, colour=None, bold=False, say=True):
        self.lines.append((text, colour, bold))
        if say:
            self.say.append(text)

    def segment(self, a, b, colour, dashed=True):
        self.marks.append(("segment", a, b, colour, dashed))

    def arc(self, centre, u, v, colour, words, facing=None):
        """An angle drawn at `centre` between directions u and v. `facing`:
        the outward normal of the face it is drawn in, when it is in one --
        so the screen can leave out the ones on the far side."""
        self.marks.append(("arc", centre, _unit(u), _unit(v), colour, words, facing))

    def polygon(self, points, colour):
        self.marks.append(("polygon", points, colour))

    def sentence(self):
        return " ".join(self.say)

    def headline(self):
        """The line that answers the picks: what they have to do with each
        other, or, for one pick, what it is."""
        bold = [text for text, _colour, strong in self.lines if strong]
        return bold[-1] if bold else (self.lines[0][0] if self.lines else "")


def _line_of(figure, element):
    """(a, b) points of a pick that is a line: an edge, a segment, a guide."""
    if element[0] in ("edge", "segment"):
        return figure.points[element[1]], figure.points[element[2]], element[1], element[2]
    if element[0] == "guide":
        _name, a, b = figure.guides[element[1]]
        return figure.points[a], figure.points[b], a, b
    return None


def _segment_kind(figure, a, b):
    """What a line between two corners is in this figure."""
    if figure.is_edge(a, b):
        return "an edge" if not figure.flat else "a side"
    if figure.flat:
        return "a diagonal"
    for face in figure.faces:
        if a in face and b in face and len(face) > 3 and len(figure.faces) > 1:
            return "a face diagonal: it runs across a face"
    return "a space diagonal: it runs through the inside"


def _plane(figure, element):
    """(point on it, unit normal) of a pick that is a flat face."""
    if element[0] == "face":
        points = figure.face_points(element[1])
        return points[0], figure.face_normal(element[1]), points
    return None


def _shape_words(points, unit):
    """"an equilateral triangle", "a rectangle", sides, angles and area."""
    n = len(points)
    sides = [_len(_sub(points[(i + 1) % n], points[i])) for i in range(n)]
    normal = _normal(points)
    angles = []
    for i in range(n):
        p, q, r = points[i - 1], points[i], points[(i + 1) % n]
        inside = _angle(_sub(p, q), _sub(r, q))
        if _dot(_cross(_sub(q, p), _sub(r, q)), normal) < -1e-9:
            inside = 360.0 - inside
        angles.append(inside)

    def same(x, y):
        return abs(x - y) < 1e-6 * max(1.0, abs(x), abs(y)) + 1e-3 * max(abs(x), abs(y))

    if n == 3:
        kind = ("equilateral" if same(sides[0], sides[1]) and same(sides[1], sides[2])
                else "isosceles" if (same(sides[0], sides[1]) or same(sides[1], sides[2])
                                     or same(sides[0], sides[2]))
                else "scalene")
        corner = ("right-angled" if any(abs(a - 90) < 0.5 for a in angles)
                  else "obtuse" if any(a > 90.5 for a in angles) else "acute")
        name = f"{'an' if kind[0] in 'aei' else 'a'} {kind} {corner} triangle"
        if kind == "equilateral":
            name = "an equilateral triangle"
    elif n == 4:
        right = all(abs(a - 90) < 0.5 for a in angles)
        equal = all(same(s, sides[0]) for s in sides)

        def parallel(i, j):
            u = _sub(points[(i + 1) % 4], points[i])
            v = _sub(points[(j + 1) % 4], points[j])
            return _line_angle(u, v) < 0.5
        both = parallel(0, 2) and parallel(1, 3)
        name = ("a square" if right and equal else "a rectangle" if right
                else "a rhombus" if equal else "a parallelogram" if both
                else "a trapezium" if parallel(0, 2) or parallel(1, 3)
                else "a kite" if ((same(sides[0], sides[1]) and same(sides[2], sides[3]))
                                  or (same(sides[1], sides[2]) and same(sides[3], sides[0])))
                else "a quadrilateral")
    else:
        regular_ = all(same(s, sides[0]) for s in sides) and all(
            abs(a - angles[0]) < 0.5 for a in angles)
        word = {5: "pentagon", 6: "hexagon", 7: "heptagon", 8: "octagon"}.get(n, f"{n}-sided figure")
        name = f"a regular {word}" if regular_ else f"a {word}"
    return name, sides, angles, _area(points)


def whole_degrees(angles, total):
    """Angles to whole degrees that still add up to `total` -- 64°, 55° and
    61°, never 64°, 55° and 60° from a triangle whose angles add to 180."""
    whole = [int(a) for a in angles]
    short = int(round(total - sum(whole)))
    for i in sorted(range(len(angles)), key=lambda i: angles[i] - int(angles[i]),
                    reverse=True)[:max(0, short)]:
        whole[i] += 1
    return whole


def live_facts(figure):
    """A flat figure as it stands -- after a corner has been dragged, too:
    what kind it is now, its angles (whole degrees, adding up as they must),
    its sides and its area."""
    face = figure.faces[0]
    points = figure.face_points(0)
    shape, sides, angles, area = _shape_words(points, figure.unit)
    n = len(face)
    total = (n - 2) * 180
    whole = whole_degrees(angles, total)
    labels = [figure.labels[i] for i in face]
    lines = [f"Now: {shape}",
             "Angles: " + ", ".join(f"∠{labels[i]} = {whole[i]}°" for i in range(n))
             + f" — together {total}°",
             "Sides: " + ", ".join(f"{labels[i]}{labels[(i + 1) % n]} = {number(sides[i])}"
                                   for i in range(n)) + (f" {figure.unit}" if figure.unit else ""),
             f"Area = {area_words(area, figure.unit)}"]
    return lines


def reshaped(figure):
    """A corner of a flat figure has been dragged: its name now says only
    what it is made of, and what kind it has become is worked out live."""
    nouns = {3: "Triangle", 4: "Quadrilateral", 5: "Pentagon", 6: "Hexagon", 7: "Heptagon",
             8: "Octagon"}
    face = figure.faces[0]
    if len(face) <= 10:
        figure.title = (f"{nouns.get(len(face), 'Polygon')} "
                        + "".join(figure.labels[i] for i in face))
        figure.facts = []


def _labels_of(figure, indices):
    return "".join(figure.labels[i] for i in indices)


def measure(figure, picks):
    """A Reading for these picks."""
    reading = Reading()
    if not picks:
        return reading
    colours = {pick: PICK_COLOURS[i % len(PICK_COLOURS)] for i, pick in enumerate(picks)}
    vertices = [p for p in picks if p[0] == "vertex"]

    # Three corners: the plane through them, and the cut it makes.
    if len(picks) == 3 and len(vertices) == 3:
        _three_corners(figure, [p[1] for p in vertices], reading)
        return reading

    # Two corners picked one after the other: the line between them, which
    # then counts as a line for whatever else was picked.
    items = []
    i = 0
    while i < len(picks):
        pick = picks[i]
        if (pick[0] == "vertex" and i + 1 < len(picks) and picks[i + 1][0] == "vertex"):
            a, b = pick[1], picks[i + 1][1]
            kind = "edge" if figure.is_edge(a, b) else "segment"
            items.append(((kind, min(a, b), max(a, b)), colours[picks[i + 1]]))
            i += 2
            continue
        items.append((pick, colours[pick]))
        i += 1

    for element, colour in items:
        # With more than one pick, each says what it is in a line and the
        # room goes to what they have to do with each other.
        _one(figure, element, colour, reading, brief=len(items) > 1)
    if len(items) >= 2:
        for (first, c1), (second, c2) in zip(items, items[1:]):
            _two(figure, first, second, c2, reading)
        if len(items) == 3:
            _two(figure, items[0][0], items[2][0], items[2][1], reading)
    return reading


def _one(figure, element, colour, reading, brief=False):
    unit = figure.unit
    kind = element[0]
    if brief:
        mark = Reading()
        _one(figure, element, colour, mark)
        reading.marks += [m for m in mark.marks if m[0] != "arc"]
        if mark.lines:
            reading.line(mark.lines[0][0], colour, True)
        return
    if kind == "vertex":
        i = element[1]
        name = figure.labels[i]
        if figure.flat:
            for face in figure.faces[:1]:
                if i in face:
                    k = face.index(i)
                    points = figure.face_points(0)
                    _name, _sides, angles, _area_ = _shape_words(points, unit)
                    p, q, r = points[k - 1], points[k], points[(k + 1) % len(points)]
                    reading.line(f"∠{figure.labels[face[k - 1]]}{name}"
                                 f"{figure.labels[face[(k + 1) % len(face)]]} = {degrees(angles[k])}"
                                 f" inside the {figure.name.lower().replace('your ', '')}", colour, True)
                    reading.line(f"The angle outside it is {degrees(180 - angles[k])}", colour)
                    reading.arc(q, _sub(p, q), _sub(r, q), colour, degrees(angles[k]))
                    return
            reading.line(f"Point {name}", colour, True)
            return
        meeting = [e for e in figure.edges if i in e and figure.is_edge(*e)]
        if not meeting:
            reading.line(f"Point {name}", colour, True)
            return
        reading.line(f"Corner {name}: {len(meeting)} edges meet here", colour, True)
        total = 0.0
        for face in figure.faces:
            if i in face and len(face) <= 8:
                k = face.index(i)
                p, q, r = (figure.points[face[k - 1]], figure.points[i],
                           figure.points[face[(k + 1) % len(face)]])
                angle = _angle(_sub(p, q), _sub(r, q))
                total += angle
                reading.line(f"∠{figure.labels[face[k - 1]]}{name}"
                             f"{figure.labels[face[(k + 1) % len(face)]]} = {degrees(angle)}",
                             colour, say=False)
                reading.arc(q, _sub(p, q), _sub(r, q), colour, degrees(angle),
                            facing=figure.face_normal(figure.faces.index(face)))
        if total:
            reading.line(f"The angles at {name} add up to {degrees(total)} — less than 360°, "
                         f"which is what lets the corner fold up", colour)
        return
    if kind in ("edge", "segment", "guide"):
        a, b, ia, ib = _line_of(figure, element)
        length = _len(_sub(b, a))
        name = figure.element_name(element)
        reading.segment(a, b, colour, dashed=(kind != "edge"))
        what = ("" if kind == "guide" else f" — {_segment_kind(figure, ia, ib)}")
        reading.line(f"{name} = {length_words(length, unit)}{what}", colour, True)
        if kind == "edge" and not figure.flat:
            faces = figure.edge_faces.get((min(ia, ib), max(ia, ib)), [])
            if len(faces) == 2:
                angle = _dihedral(figure, faces[0], faces[1], (ia, ib), colour, reading)
                reading.line(f"{first_up(figure.face_name(faces[0]))} and "
                             f"{figure.face_name(faces[1])} meet along {name} at {degrees(angle)}",
                             colour)
        if kind == "segment" and not figure.flat and figure.name in ("Cube", "Cuboid"):
            _diagonal_rule(figure, ia, ib, length, colour, reading)
        return
    if kind == "face":
        points = figure.face_points(element[1])
        name = figure.face_name(element[1])
        reading.polygon(points, colour)
        if len(points) > 12:          # a circle, really
            r = _len(_sub(points[0], _mid(points)))
            reading.line(f"{first_up(name)}: radius {length_words(r, unit)}", colour, True)
            reading.line(f"Area = πr² = {area_words(math.pi * r * r, unit)}", colour)
            return
        shape, sides, angles, area = _shape_words(points, unit)
        face = figure.faces[element[1]]
        reading.line(f"{first_up(name)} is {shape}", colour, True)
        n = len(face)
        reading.line("Sides: " + ", ".join(
            f"{figure.labels[face[i]]}{figure.labels[face[(i + 1) % n]]} = "
            f"{length_words(sides[i], unit)}" for i in range(n)), colour)
        reading.line("Angles: " + ", ".join(
            f"∠{figure.labels[face[i]]} = {degrees(angles[i])}" for i in range(n))
            + f" (together {degrees(sum(angles))})", colour)
        reading.line(f"Area = {area_words(area, unit)}", colour)
        return
    if kind in ("surface", "rim"):
        reading.line(first_up(element[1]), colour, True)
        if element[1] in figure.rounds:
            reading.line(figure.rounds[element[1]], colour)


def _diagonal_rule(figure, ia, ib, length, colour, reading):
    """Why a box's diagonal is as long as it is."""
    d = [abs(c) for c in _sub(figure.points[ib], figure.points[ia])]
    used = [x for x in d if x > 1e-9]
    if len(used) == 2:
        reading.line(f"By Pythagoras: √({number(used[0])}² + {number(used[1])}²) = "
                     f"{length_words(length, figure.unit)}", colour)
    elif len(used) == 3:
        rule = "a√3" if figure.name == "Cube" else "√(l² + b² + h²)"
        reading.line(f"Pythagoras twice: √({' + '.join(number(x) + '²' for x in used)}) = "
                     f"{length_words(length, figure.unit)} — for any {figure.name.lower()}, "
                     f"{rule}", colour)


def _dihedral(figure, f1, f2, edge, colour, reading):
    """The angle between two faces meeting along `edge`, drawn there."""
    a, b = figure.points[edge[0]], figure.points[edge[1]]
    middle = _mid([a, b])
    along = _unit(_sub(b, a))
    arms = []
    for k in (f1, f2):
        towards = _sub(_mid(figure.face_points(k)), middle)
        arms.append(_sub(towards, _mul(along, _dot(towards, along))))
    angle = _angle(arms[0], arms[1])
    reading.arc(middle, arms[0], arms[1], colour, degrees(angle))
    return angle


def _two(figure, first, second, colour, reading):
    """What two picks have to do with each other."""
    unit = figure.unit
    line1, line2 = _line_of(figure, first), _line_of(figure, second)
    plane1, plane2 = _plane(figure, first), _plane(figure, second)
    name1, name2 = figure.element_name(first), figure.element_name(second)
    if line1 and line2:
        a1, b1, i1, j1 = line1
        a2, b2, i2, j2 = line2
        u, v = _sub(b1, a1), _sub(b2, a2)
        shared = {i1, j1} & {i2, j2}
        if shared:
            s = shared.pop()
            p = j1 if i1 == s else i1
            q = j2 if i2 == s else i2
            angle = _angle(_sub(figure.points[p], figure.points[s]),
                           _sub(figure.points[q], figure.points[s]))
            if angle < 0.05 or angle > 179.95:
                reading.line(f"{name1} and {name2} lie along one straight line", colour, True)
                return
            reading.line(f"∠{figure.labels[p]}{figure.labels[s]}{figure.labels[q]} = "
                         f"{degrees(angle)}: {name1} and {name2} meet at {figure.labels[s]}",
                         colour, True)
            reading.arc(figure.points[s], _sub(figure.points[p], figure.points[s]),
                        _sub(figure.points[q], figure.points[s]), colour, degrees(angle))
            if abs(angle - 90) < 0.05:
                reading.line(f"{name1} ⟂ {name2}: they are perpendicular", colour)
            return
        between = _line_angle(u, v)
        across = _cross(u, v)
        if _len(across) < 1e-9 * _len(u) * _len(v) + 1e-12:
            gap = _len(_cross(_sub(a2, a1), _unit(u)))
            reading.line(f"{name1} ∥ {name2}: parallel, {length_words(gap, unit)} apart — "
                         f"they never meet", colour, True)
            return
        gap = abs(_dot(_sub(a2, a1), _unit(across)))
        if gap < 1e-6:
            reading.line(f"{name1} and {name2} would cross if they were made longer, at "
                         f"{degrees(between)}", colour, True)
            return
        reading.line(f"{name1} and {name2} are skew lines: not parallel, and they never meet",
                     colour, True)
        reading.line(f"The angle between their directions is {degrees(between)}; the "
                     f"shortest gap between them is {length_words(gap, unit)}", colour)
        return
    if plane1 and plane2:
        (p1, n1, _), (p2, n2, _) = plane1, plane2
        f1, f2 = first[1], second[1]
        common = [e for e in figure.edges if e[0] in figure.faces[f1] and e[1] in figure.faces[f1]
                  and e[0] in figure.faces[f2] and e[1] in figure.faces[f2]]
        if common:
            angle = _dihedral(figure, f1, f2, common[0], colour, reading)
            reading.line(f"{first_up(name1)} and {name2} meet along "
                         f"{figure.edge_name(*common[0])} at {degrees(angle)}", colour, True)
            if abs(angle - 90) < 0.05:
                reading.line("They are perpendicular", colour)
            return
        between = _line_angle(n1, n2)
        if between < 0.05:
            gap = abs(_dot(_sub(p2, p1), n1))
            reading.line(f"{first_up(name1)} ∥ {name2}: parallel, {length_words(gap, unit)} "
                         f"apart", colour, True)
            return
        reading.line(f"{first_up(name1)} and {name2} do not touch; made bigger, they "
                     f"would meet at {degrees(180 - between if between > 90 else between)}",
                     colour, True)
        return
    if (line1 and plane2) or (line2 and plane1):
        (a, b, ia, ib), (p, n, pts), line_name, plane_name = (
            (line1, plane2, name1, name2) if line1 else (line2, plane1, name2, name1))
        direction = _sub(b, a)
        da, db = _dot(_sub(a, p), n), _dot(_sub(b, p), n)
        tilt = 90.0 - _line_angle(direction, n)
        if abs(da) < 1e-6 and abs(db) < 1e-6:
            reading.line(f"{line_name} lies in {plane_name}", colour, True)
            return
        if tilt < 0.05:
            reading.line(f"{line_name} ∥ {plane_name}: parallel to it, "
                         f"{length_words(abs(da), unit)} away", colour, True)
            return
        if abs(tilt - 90) < 0.05:
            reading.line(f"{line_name} ⟂ {plane_name}: it stands at 90° to the face", colour, True)
        else:
            reading.line(f"{line_name} makes {degrees(tilt)} with {plane_name}", colour, True)
        on, off = (a, b) if abs(da) < 1e-6 else (b, a) if abs(db) < 1e-6 else (None, None)
        if on is not None:
            d = _sub(off, on)
            flat = _sub(d, _mul(n, _dot(d, n)))
            if _len(flat) > 1e-9:
                reading.arc(on, d, flat, colour, degrees(tilt))
                # Its shadow on the face, which the angle is measured from,
                # and the drop from its far end straight down to the face:
                # the right-angled triangle the angle belongs to.
                foot = _add(on, flat)
                reading.segment(on, foot, colour, dashed=True)
                reading.segment(off, foot, colour, dashed=True)
                reading.arc(foot, _sub(off, foot), _sub(on, foot), colour, "90°")
            else:
                reading.arc(on, d, _sub(pts[1], pts[0]), colour, "90°")
        return
    vertex = first if first[0] == "vertex" else second if second[0] == "vertex" else None
    other = second if vertex is first else first
    if vertex is not None:
        point = figure.points[vertex[1]]
        name = figure.labels[vertex[1]]
        plane = _plane(figure, other)
        line = _line_of(figure, other)
        if plane:
            p, n, pts = plane
            d = _dot(_sub(point, p), n)
            if abs(d) < 1e-6:
                reading.line(f"{name} is on {figure.element_name(other)}", colour, True)
                return
            foot = _sub(point, _mul(n, d))
            reading.segment(point, foot, colour, dashed=True)
            reading.arc(foot, _sub(point, foot), _sub(pts[0], foot) if _len(_sub(pts[0], foot)) > 1e-6
                        else _sub(pts[1], foot), colour, "90°")
            reading.line(f"{name} is {length_words(abs(d), unit)} from "
                         f"{figure.element_name(other)} — measured straight down to it, at 90°",
                         colour, True)
            return
        if line:
            a, b, ia, ib = line
            if vertex[1] in (ia, ib):
                return
            u = _unit(_sub(b, a))
            foot = _add(a, _mul(u, _dot(_sub(point, a), u)))
            gap = _len(_sub(point, foot))
            reading.segment(point, foot, colour, dashed=True)
            reading.line(f"{name} is {length_words(gap, unit)} from the line "
                         f"{figure.element_name(other)}", colour, True)
            return
    if first[0] in ("surface", "rim") or second[0] in ("surface", "rim"):
        names = {first[1] if first[0] in ("surface", "rim") else None,
                 second[1] if second[0] in ("surface", "rim") else None} - {None}
        if figure.name == "Cylinder" and any("surface" in n for n in names):
            reading.line("The curved surface meets each flat face at 90° all the way round",
                         colour, True)
        elif figure.name == "Cone" and any("surface" in n for n in names):
            r = _len(_sub(figure.points[0], figure.points[-1]))
            h = _len(_sub(figure.points[-2], figure.points[-1]))
            reading.line(f"The curved surface meets the base at {degrees(math.degrees(math.atan2(h, r)))}"
                         f" all the way round", colour, True)


def _three_corners(figure, corners, reading):
    """The triangle through three corners and, in a solid, the whole cut
    the plane through them makes."""
    p, q, r = (figure.points[i] for i in corners)
    names = [figure.labels[i] for i in corners]
    across = _cross(_sub(q, p), _sub(r, p))
    colour = PICK_COLOURS[2]
    if _len(across) < 1e-9:
        reading.line(f"{''.join(names)}: these three are in a straight line", colour, True)
        return
    shape, sides, angles, area = _shape_words([p, q, r], figure.unit)
    for (a, b) in ((p, q), (q, r), (r, p)):
        reading.segment(a, b, colour, dashed=False)
    reading.line(f"Triangle {''.join(names)} is {shape}", colour, True)
    reading.line(f"{names[0]}{names[1]} = {length_words(sides[0], figure.unit)}, "
                 f"{names[1]}{names[2]} = {length_words(sides[1], figure.unit)}, "
                 f"{names[2]}{names[0]} = {length_words(sides[2], figure.unit)}", colour)
    reading.line(f"∠{names[0]} = {degrees(angles[0])}, ∠{names[1]} = {degrees(angles[1])}, "
                 f"∠{names[2]} = {degrees(angles[2])} — together 180°", colour)
    for k, (a, b, c) in enumerate(((p, q, r), (q, r, p), (r, p, q))):
        reading.arc(a, _sub(c, a), _sub(b, a), colour, degrees(angles[k]))
    if figure.flat:
        reading.polygon([p, q, r], colour)
        reading.line(f"Area = {area_words(area, figure.unit)}", colour)
        return
    cut = section(figure, p, _unit(across))
    if cut and len(cut) > 3:
        reading.polygon(cut, colour)
        cut_shape, _s, _a, cut_area = _shape_words(cut, figure.unit)
        reading.line(f"The flat cut through {', '.join(names)} goes right across the "
                     f"{figure.name.lower()}: {cut_shape}, area {area_words(cut_area, figure.unit)}",
                     colour)
    else:
        reading.polygon([p, q, r], colour)
        reading.line(f"Cut flat through {', '.join(names)}, the {figure.name.lower()} shows "
                     f"this triangle; its area is {area_words(area, figure.unit)}", colour)


def section(figure, point, normal):
    """The polygon a flat cut through `point` (square to `normal`) makes in a
    solid with flat faces, its corners in order round it."""
    found = []
    for a, b in figure.edges:
        pa, pb = figure.points[a], figure.points[b]
        da, db = _dot(_sub(pa, point), normal), _dot(_sub(pb, point), normal)
        if abs(da) < 1e-7:
            found.append(pa)
        if abs(db) < 1e-7:
            found.append(pb)
        if da * db < 0 and abs(da) > 1e-7 and abs(db) > 1e-7:
            t = da / (da - db)
            found.append(_add(pa, _mul(_sub(pb, pa), t)))
    unique = []
    for p in found:
        if all(_len(_sub(p, q)) > 1e-6 for q in unique):
            unique.append(p)
    if len(unique) < 3:
        return None
    centre = _mid(unique)
    x = _unit(_sub(unique[0], centre))
    y = _cross(normal, x)
    unique.sort(key=lambda p: math.atan2(_dot(_sub(p, centre), y), _dot(_sub(p, centre), x)))
    return unique
