"""Biology and physics as 3D models, built from simple shapes.

A leaf: numpy and math only. viewer3d turns what these return into pictures.

WHY BUILT FROM SHAPES AND NOT DOWNLOADED
    A heart or a cell from a model library is tens of megabytes, needs the
    network this device often does not have, and comes under somebody else's
    licence. A textbook's 3D figure is not a scan either: it is spheres, tubes
    and discs arranged to show the PARTS, each one labelled. That is what these
    are -- drawn in code, instant, offline, and labelled like the book.
    Anything that cannot honestly be drawn this way (a human heart) is not
    faked: it is left out, and the request becomes a picture instead.

WHAT A SCENE IS
    Plain data, so it can be handed to the worker process:
      parts   -- shapes: sphere, ellipsoid, cylinder, cone, torus, box, disc,
                 tube (through points), revolve (a profile spun round z),
                 icosahedron, points (many small balls), particles (small
                 balls that move as a wave). Each has a colour, and may have an
                 opacity, `lighting: False` (glows), a rotation and a centre.
                 A part with `orbit` moves round a point or another part.
      labels  -- text at a point, with a pointer line back to the part it
                 names; or following a moving part.
      legend  -- (symbol, name, colour) for the key under the title.
      note    -- one short line under the model ("Not to scale").
      view    -- where the camera starts.
    Positions are in the model's own units; the camera frames whatever is there.
"""

import math
import re

import numpy as np

TAU = 2 * math.pi
# Orbits and shells: drawn flat and glowing faintly, so they show whichever
# way the light falls -- lit like solid things they went black on the far side.
ORBIT_COLOUR = "#56648F"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def part(shape, colour, **kw):
    kw.update(shape=shape, colour=colour)
    return kw


def label(text, at, anchor=None):
    """Text at `at`, with a pointer line from `anchor` (the part) if given."""
    return {"text": text, "at": tuple(float(v) for v in at),
            "anchor": None if anchor is None else tuple(float(v) for v in anchor)}


def follow(text, part_id, offset=(0.0, 0.0, 0.6)):
    """Text that rides along above a moving part."""
    return {"text": text, "follow": part_id, "offset": tuple(offset)}


def callout(text, anchor, reach, centre=(0, 0, 0), lift=0.0):
    """A label pushed straight out from `centre` through `anchor` to distance
    `reach`, so the words sit outside the model against the dark background
    and a line points back in to the part."""
    anchor = np.asarray(anchor, float)
    direction = anchor - np.asarray(centre, float)
    # Out SIDEWAYS in the opening view (which looks along y): a label pushed
    # straight towards the camera lands on top of the model it names.
    direction[1] = 0.0
    norm = np.linalg.norm(direction)
    direction = direction / norm if norm > 1e-9 else np.array([1.0, 0, 0])
    at = np.asarray(centre, float) + direction * reach + np.array([0, 0, lift])
    return label(text, at, anchor)


def columns(items, half_width, top, bottom, y=0.0):
    """Labels in two tidy columns either side of the model, the way a
    textbook lays out a labelled cell. Each part goes to the side it is on,
    sorted top to bottom so no two pointer lines cross."""
    items = [(text, np.asarray(anchor, float)) for text, anchor in items]
    left = sorted([i for i in items if i[1][0] < 0], key=lambda i: i[1][0])
    right = sorted([i for i in items if i[1][0] >= 0], key=lambda i: -i[1][0])
    while len(left) > len(right) + 1:
        right.append(left.pop())
    while len(right) > len(left) + 1:
        left.append(right.pop())
    out = []
    for group, x in ((left, -half_width), (right, half_width)):
        group.sort(key=lambda i: -i[1][2])
        heights = np.linspace(top, bottom, len(group)) if len(group) > 1 else [(top + bottom) / 2]
        out += [label(text, (x, y, z), anchor) for (text, anchor), z in zip(group, heights)]
    return out


def rows(items, left, right, above, below, y=0.0):
    """The same for a long model: labels in a row above it and a row below."""
    items = [(text, np.asarray(anchor, float)) for text, anchor in items]
    top = [i for i in items if i[1][2] >= 0]
    low = [i for i in items if i[1][2] < 0]
    while len(top) > len(low) + 1:
        top.sort(key=lambda i: i[1][2])
        low.append(top.pop(0))
    while len(low) > len(top) + 1:
        low.sort(key=lambda i: -i[1][2])
        top.append(low.pop(0))
    out = []
    for group, z in ((top, above), (low, below)):
        group.sort(key=lambda i: i[1][0])
        xs = np.linspace(left, right, len(group)) if len(group) > 1 else [(left + right) / 2]
        out += [label(text, (x, y, z), anchor) for (text, anchor), x in zip(group, xs)]
    return out


def circle(radius, centre=(0, 0, 0), normal=(0, 0, 1), n=96):
    """Points round a circle, for orbits and field lines."""
    u, v = _basis(normal)
    t = np.linspace(0, TAU, n, endpoint=False)
    return [tuple(np.asarray(centre) + radius * (math.cos(a) * u + math.sin(a) * v))
            for a in t]


def _basis(normal):
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    helper = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    u = np.cross(n, helper)
    u /= np.linalg.norm(u)
    return u, np.cross(n, u)


def fibonacci_sphere(count, radius=1.0, centre=(0, 0, 0)):
    """`count` points spread evenly over a sphere."""
    points = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(count):
        y = 1 - 2 * (i + 0.5) / count
        r = math.sqrt(1 - y * y)
        a = golden * i
        points.append(tuple(np.asarray(centre) + radius * np.array(
            [math.cos(a) * r, y, math.sin(a) * r])))
    return points


def scene(title, parts, labels=(), legend=(), note="", view=None, spin=True,
          lighting="normal"):
    return {"kind": "parts", "title": title, "parts": list(parts),
            "labels": list(labels), "legend": list(legend), "note": note,
            "view": dict(view or {}), "spin": spin, "lighting": lighting,
            "animated": any("orbit" in p or p["shape"] == "particles" for p in parts)}


# ===========================================================================
# PHYSICS
# ===========================================================================
PLANETS = [
    # name, colour, radius, orbit radius, orbit period (s), starting angle
    ("Mercury", "#B5A89A", 0.28, 3.3, 4.9, 0.3),
    ("Venus", "#E8C27A", 0.45, 4.4, 7.9, 2.1),
    ("Earth", "#3B82F6", 0.48, 5.6, 10.0, 4.0),
    ("Mars", "#D9573B", 0.36, 6.8, 13.7, 5.5),
    ("Jupiter", "#D8B48A", 1.05, 8.8, 34.5, 1.2),
    ("Saturn", "#E6CF8F", 0.88, 11.0, 54.0, 3.3),
    ("Uranus", "#8FD8E6", 0.6, 12.9, 91.0, 0.8),
    ("Neptune", "#4666E0", 0.58, 14.6, 128.0, 2.7),
]


def solar_system(_payload=""):
    parts = [part("sphere", "#FDB813", radius=1.9, lighting=False)]
    labels = [label("Sun", (0, 0, 2.7))]
    for name, colour, radius, orbit, period, phase in PLANETS:
        parts.append(part("tube", ORBIT_COLOUR, points=circle(orbit), radius=0.03,
                          closed=True, lighting=False))
        key = name.lower()
        parts.append(part("sphere", colour, radius=radius, id=key,
                          orbit={"around": (0, 0, 0), "radius": orbit,
                                 "period": period, "phase": phase}))
        if name == "Saturn":
            parts.append(part("disc", "#CDB77A", inner=1.2, outer=1.9,
                              rotate=(25, 0, 0), opacity=0.85,
                              orbit={"around": key, "radius": 0}))
        labels.append(follow(name, key, (0, 0, radius + 0.55)))
    return scene("The Solar System", parts, labels,
                 note="Sizes, distances and speeds are not to scale",
                 view={"elevation": 38, "zoom": 1.45}, spin=False)


# First twenty elements: symbol, name, usual mass number.
ELEMENTS = [
    ("H", "Hydrogen", 1), ("He", "Helium", 4), ("Li", "Lithium", 7),
    ("Be", "Beryllium", 9), ("B", "Boron", 11), ("C", "Carbon", 12),
    ("N", "Nitrogen", 14), ("O", "Oxygen", 16), ("F", "Fluorine", 19),
    ("Ne", "Neon", 20), ("Na", "Sodium", 23), ("Mg", "Magnesium", 24),
    ("Al", "Aluminium", 27), ("Si", "Silicon", 28), ("P", "Phosphorus", 31),
    ("S", "Sulphur", 32), ("Cl", "Chlorine", 35), ("Ar", "Argon", 40),
    ("K", "Potassium", 39), ("Ca", "Calcium", 40),
]
_ELEMENT_ALIASES = {"aluminum": "aluminium", "sulfur": "sulphur"}


def find_element(text):
    """(Z, symbol, name, A) for the first of the first twenty elements named."""
    lowered = (text or "").lower()
    for alias, spelling in _ELEMENT_ALIASES.items():
        lowered = re.sub(rf"\b{alias}\b", spelling, lowered)
    for z, (symbol, name, mass) in enumerate(ELEMENTS, start=1):
        if re.search(rf"\b{name.lower()}\b", lowered):
            return z, symbol, name, mass
    for z, (symbol, name, mass) in enumerate(ELEMENTS, start=1):
        if re.search(rf"(?<![A-Za-z]){symbol}(?![a-z])", text or ""):
            return z, symbol, name, mass
    return None


def shell_config(z):
    """2, 8, 8, then the rest -- the rule taught for the first twenty."""
    shells, left = [], z
    for capacity in (2, 8, 8, 8):
        if left <= 0:
            break
        shells.append(min(capacity, left))
        left -= shells[-1]
    return shells


def atom(payload=""):
    found = find_element(payload) or (6, "C", "Carbon", 12)
    z, symbol, name, mass = found
    neutrons = mass - z
    rng = np.random.default_rng(z)
    nucleon = 0.3
    ball = nucleon * mass ** (1 / 3) * 1.15
    placed = []
    min_gap = nucleon * 1.45
    tries = 0
    while len(placed) < mass:
        tries += 1
        p = rng.uniform(-ball, ball, 3)
        if np.linalg.norm(p) > ball:
            continue
        if all(np.linalg.norm(p - q) >= min_gap for q in placed) or tries > 40000:
            placed.append(p)
    kinds = np.array([True] * z + [False] * neutrons)
    rng.shuffle(kinds)
    protons = [tuple(p) for p, k in zip(placed, kinds) if k]
    neutral = [tuple(p) for p, k in zip(placed, kinds) if not k]
    parts = [part("points", "#E8453C", centres=protons, radius=nucleon)]
    if neutral:
        parts.append(part("points", "#9AA3B5", centres=neutral, radius=nucleon))
    shells = shell_config(z)
    labels = [label(f"Nucleus: {z} proton{'s' * (z > 1)}, {neutrons} neutron{'s' * (neutrons != 1)}",
                    (0, -0.4, ball + 2.4 + len(shells) * 1.0), (0, 0, ball))]
    for index, count in enumerate(shells):
        radius = ball + 1.0 + index * 1.05
        parts.append(part("tube", ORBIT_COLOUR, points=circle(radius), radius=0.025,
                          closed=True, lighting=False))
        for e in range(count):
            parts.append(part("sphere", "#3FA7FF", radius=0.17,
                              orbit={"around": (0, 0, 0), "radius": radius,
                                     "period": 3.0 + 1.6 * index,
                                     "phase": TAU * e / count}))
        shell_name = "KLMN"[index]
        # Fanned round the front, one angle per shell, so no two labels sit
        # on top of each other.
        a = math.radians(-15 - 26 * index)
        labels.append(label(f"{shell_name} shell: {count}",
                            ((radius + 1.0) * math.cos(a), (radius + 1.0) * math.sin(a), -0.5),
                            (radius * math.cos(a), radius * math.sin(a), 0)))
    config = ", ".join(str(c) for c in shells)
    return scene(f"{name} atom ({symbol})  ·  {config}", parts, labels,
                 legend=[("p", "Proton", "#E8453C"), ("n", "Neutron", "#9AA3B5"),
                         ("e", "Electron", "#3FA7FF")],
                 note="Bohr model: electrons move in fixed shells",
                 view={"elevation": 35, "zoom": 1.15}, spin=False)


def _pole_field(p, pole):
    """Unit field at p from a north pole at +pole and a south pole at -pole."""
    x, y, z = p
    nx, sx = x - pole, x + pole
    rn = (nx * nx + y * y + z * z) ** 1.5
    rs = (sx * sx + y * y + z * z) ** 1.5
    bx, by, bz = nx / rn - sx / rs, y / rn - y / rs, z / rn - z / rs
    size = math.sqrt(bx * bx + by * by + bz * bz) or 1.0
    return bx / size, by / size, bz / size


def _pole_lines(pole, planes, angles, bound=9.0, step=0.06, phase=0.0):
    """Field lines traced from the north pole round to the south pole.

    The magnet as two poles at its ends -- the picture a textbook draws, with
    lines leaving one end and arriving at the other -- traced step by step
    (midpoint rule). Plain floats, not numpy: it is a few thousand tiny steps,
    and numpy's per-call cost would be most of the time on the Pi.
    Returns (lines, arrows); one arrow per line, halfway along it.
    """
    lines, arrows = [], []
    for k in range(planes):
        phi = TAU * k / planes + phase
        for degrees in angles:
            a = math.radians(degrees)
            d = (math.cos(a), math.sin(a) * math.cos(phi), math.sin(a) * math.sin(phi))
            p = (pole + 0.3 * d[0], 0.3 * d[1], 0.3 * d[2])
            pts = [p]
            for _ in range(2000):
                b = _pole_field(p, pole)
                mid = (p[0] + b[0] * step / 2, p[1] + b[1] * step / 2, p[2] + b[2] * step / 2)
                b = _pole_field(mid, pole)
                p = (p[0] + b[0] * step, p[1] + b[1] * step, p[2] + b[2] * step)
                pts.append(p)
                if (p[0] + pole) ** 2 + p[1] ** 2 + p[2] ** 2 < 0.09 \
                        or p[0] ** 2 + p[1] ** 2 + p[2] ** 2 > bound * bound:
                    break
            pts = pts[::2]
            lines.append(pts)
            i = len(pts) // 2
            arrows.append((pts[i], tuple(np.subtract(pts[i + 1], pts[i]))))
    return lines, arrows


def _dipole_lines(scales, planes, theta_edge, rotate_axis="x", count_plane_offset=0.0):
    """Field lines of a magnet along x: r = L sin^2(theta), in several planes
    round the axis. Starts where each line leaves the magnet's end."""
    lines, arrows = [], []
    for L in scales:
        edge = max(math.asin(min(1.0, math.sqrt(theta_edge / L))), 0.12)
        thetas = np.linspace(edge, math.pi - edge, 70)
        for k in range(planes):
            phi = TAU * k / planes + count_plane_offset
            pts = []
            for th in thetas:
                r = L * math.sin(th) ** 2
                x, rho = r * math.cos(th), r * math.sin(th)
                pts.append((x, rho * math.cos(phi), rho * math.sin(phi)))
            lines.append(pts)
            # Outside a magnet the field runs from N (+x) round to S (-x).
            arrows.append(((0.0, L * math.cos(phi), L * math.sin(phi)), (-1.0, 0.0, 0.0)))
    return lines, arrows


def bar_magnet(_payload=""):
    parts = [part("box", "#E23B3B", size=(2.0, 0.9, 0.7), centre=(1.0, 0, 0)),
             part("box", "#2F6BE0", size=(2.0, 0.9, 0.7), centre=(-1.0, 0, 0))]
    lines, arrows = _pole_lines(1.7, 6, (50, 75, 105, 140), bound=11.0, phase=math.pi / 2)
    for pts in lines:
        parts.append(part("tube", "#9FB4E8", points=pts, radius=0.03))
    for tip, direction in arrows:
        parts.append(part("cone", "#FFD60A", radius=0.13, height=0.35,
                          centre=tip, direction=direction))
    on_a_line = lines[2][len(lines[2]) // 3]
    return scene("Magnetic field of a bar magnet", parts, [
        label("N", (1.6, 0, 0.75)), label("S", (-1.6, 0, 0.75)),
        label("North pole", (3.6, 0, -2.2), (2.0, 0, -0.3)),
        label("South pole", (-3.6, 0, -2.2), (-2.0, 0, -0.3)),
        label("Magnetic field lines", (on_a_line[0] + 1.5, 0, on_a_line[2] + 1.5), on_a_line),
    ], note="Outside the magnet, field lines run from N to S",
        view={"elevation": 20, "zoom": 1.35})


def wire_field(_payload=""):
    parts = [part("cylinder", "#C88033", radius=0.12, height=8.0, centre=(0, 0, 0)),
             part("cone", "#FFD60A", radius=0.3, height=0.6, centre=(0, 0, 4.3),
                  direction=(0, 0, 1))]
    for height in (-2.5, 0.0, 2.5):
        for radius in (1.0, 1.8, 2.6):
            parts.append(part("tube", "#9FB4E8", points=circle(radius, (0, 0, height)),
                              radius=0.03, closed=True))
            # Anticlockwise seen from above, for a current flowing up.
            parts.append(part("cone", "#FFD60A", radius=0.12, height=0.3,
                              centre=(radius, 0, height), direction=(0, 1, 0)))
    return scene("Magnetic field around a current-carrying wire", parts, [
        label("Current (I) flows up", (0.8, 0, 5.2), (0, 0, 4.5)),
        label("Field lines are circles", (3.6, -1.5, 2.5), (2.6, 0, 2.5)),
        label("Wire", (-1.2, -1.2, -3.6), (0, 0, -3.4)),
    ], note="Right-hand thumb rule: thumb along the current, fingers curl with the field",
        view={"elevation": 20, "zoom": 1.2})


def solenoid(_payload=""):
    turns, length, radius = 10, 6.0, 1.2
    t = np.linspace(0, turns * TAU, turns * 40)
    coil = [(-length / 2 + length * a / (turns * TAU), radius * math.cos(a),
             radius * math.sin(a)) for a in t]
    parts = [part("tube", "#C88033", points=coil, radius=0.07)]
    for y, z in [(0, 0), (0.5, 0), (-0.5, 0), (0, 0.5), (0, -0.5)]:
        parts.append(part("tube", "#9FB4E8", points=[(-3.6, y, z), (3.6, y, z)],
                          radius=0.03))
        parts.append(part("cone", "#FFD60A", radius=0.12, height=0.3,
                          centre=(0.2, y, z), direction=(1, 0, 0)))
    lines, arrows = _pole_lines(3.1, 4, (70, 100, 135), bound=12.0, phase=math.pi / 2)
    for pts in lines:
        parts.append(part("tube", "#9FB4E8", points=pts, radius=0.03))
    for tip, direction in arrows:
        parts.append(part("cone", "#FFD60A", radius=0.13, height=0.35,
                          centre=tip, direction=direction))
    return scene("Magnetic field of a solenoid", parts, [
        label("N", (4.0, 0, 0.6)), label("S", (-4.0, 0, 0.6)),
        label("Coil carrying current", (0, -3.0, -2.4), (0, -1.2, -0.3)),
        label("Strong, straight field inside", (1.5, 2.8, 2.2), (1.5, 0.5, 0)),
    ], note="A solenoid behaves like a bar magnet",
        view={"elevation": 25, "zoom": 1.6})


# Refractive index per colour. Real crown glass spans about 1.51-1.53; these
# span 1.48-1.62 so the fan is wide enough to see (the note says so).
SPECTRUM = [("Red", "#FF3B30", 1.48), ("Orange", "#FF9500", 1.503),
            ("Yellow", "#FFD60A", 1.527), ("Green", "#34C759", 1.55),
            ("Blue", "#0A84FF", 1.573), ("Indigo", "#5E5CE6", 1.597),
            ("Violet", "#BF5AF2", 1.62)]


def _refract(d, n, eta):
    """Snell's law on unit vectors; n faces against d. None on total reflection."""
    cos_i = -float(np.dot(n, d))
    k = 1 - eta * eta * (1 - cos_i * cos_i)
    if k < 0:
        return None
    t = eta * d + (eta * cos_i - math.sqrt(k)) * n
    return t / np.linalg.norm(t)


def _hit(origin, direction, a, b):
    """Where the ray origin + s*direction crosses segment a-b, or None."""
    e = b - a
    m = np.array([[direction[0], -e[0]], [direction[1], -e[1]]])
    if abs(np.linalg.det(m)) < 1e-9:
        return None
    s, u = np.linalg.solve(m, a - origin)
    return origin + s * direction if s > 1e-6 and 0 <= u <= 1 else None


def prism(_payload=""):
    apex, left, right = (np.array(p) for p in ([0.0, 1.8], [-1.56, -0.9], [1.56, -0.9]))
    parts = [part("prism", "#9ED8F0", points2d=[tuple(left), tuple(right), tuple(apex)],
                  depth=2.4, opacity=0.3)]
    # 50 degrees onto the first face: every colour gets out of the second one.
    entry_dir = np.array([math.cos(math.radians(20)), math.sin(math.radians(20))])
    entry = (left + apex) / 2
    start = entry - entry_dir * 5.0
    parts.append(part("tube", "#FFFFFF", points=[(*start, 0), (*entry, 0)], radius=0.06))
    ends = []
    # Spreading exaggerated (see the note): the real spread is a degree or two.
    for name, colour, n_glass in SPECTRUM:
        edge = apex - left
        normal = np.array([-edge[1], edge[0]])
        normal /= np.linalg.norm(normal)
        if np.dot(normal, entry_dir) > 0:
            normal = -normal
        inside = _refract(entry_dir, normal, 1 / n_glass)
        exit_point = _hit(entry, inside, apex, right)
        if inside is None or exit_point is None:
            continue
        edge = right - apex
        normal = np.array([edge[1], -edge[0]])
        normal /= np.linalg.norm(normal)
        if np.dot(normal, inside) > 0:
            normal = -normal
        out = _refract(inside, normal, n_glass)
        if out is None:
            continue
        end = exit_point + out * 5.0
        ends.append(end)
        parts.append(part("tube", colour, points=[(*entry, 0), (*exit_point, 0), (*end, 0)],
                          radius=0.045))
    tip = np.mean(ends, axis=0) if ends else np.array([4.0, -2.0])
    return scene("White light through a glass prism", parts, [
        label("White light", (start[0] + 0.8, start[1] + 1.0, 0), (*(start + entry_dir * 1.5), 0)),
        label("Glass prism", (0.2, 2.6, 0), (0.1, 1.5, 0)),
        label("Spectrum: V I B G Y O R", (tip[0] + 0.6, tip[1] - 1.2, 0), (tip[0] - 0.3, tip[1], 0)),
    ], legend=[(n[0], n, c) for n, c, _ in reversed(SPECTRUM)],
        note="Violet bends most, red least. Spreading exaggerated to show it",
        view={"camera": "xy", "elevation": 20, "azimuth": -12, "zoom": 1.3}, spin=False)


def transverse_wave(_payload=""):
    xs = np.arange(-9.0, 9.01, 0.45)
    beads = [(float(x), 0.0, 0.0) for x in xs]
    parts = [
        part("tube", "#3A4A75", points=[(-9.5, 0, 0), (9.5, 0, 0)], radius=0.02),
        part("particles", "#3FA7FF", centres=beads, radius=0.2,
             wave={"axis": (0, 0, 1), "travel": (1, 0, 0), "amplitude": 1.3,
                   "wavelength": 6.0, "period": 2.4}),
        part("cone", "#FFD60A", radius=0.25, height=0.6, centre=(10.2, 0, 0),
             direction=(1, 0, 0)),
        part("tube", "#8C9BC4", points=[(-3, 0, -2.3), (3, 0, -2.3)], radius=0.03),
        part("cone", "#8C9BC4", radius=0.12, height=0.3, centre=(3, 0, -2.3), direction=(1, 0, 0)),
        part("cone", "#8C9BC4", radius=0.12, height=0.3, centre=(-3, 0, -2.3), direction=(-1, 0, 0)),
    ]
    return scene("A transverse wave", parts, [
        label("Wave travels this way", (9.0, 0, 1.2), (10.0, 0, 0.2)),
        label("One wavelength", (0, 0, -3.0), (0, 0, -2.4)),
        label("Rest position", (-8.0, 0, -1.0), (-8.0, 0, -0.05)),
    ], note="Each bead only moves up and down; the wave moves along",
        view={"camera": "xz", "elevation": 12, "zoom": 1.3}, spin=False)


def sound_wave(_payload=""):
    xs = np.arange(-9.0, 9.01, 0.3)
    centres = [(float(x), 0.0, float(z)) for x in xs for z in (-0.9, -0.3, 0.3, 0.9)]
    parts = [
        part("particles", "#FFD60A", centres=centres, radius=0.09,
             wave={"axis": (1, 0, 0), "travel": (1, 0, 0), "amplitude": 0.28,
                   "wavelength": 4.0, "period": 1.6}),
        part("cone", "#C9D2EA", radius=1.1, height=0.8, centre=(-10.0, 0, 0),
             direction=(1, 0, 0)),
        part("cone", "#FFD60A", radius=0.25, height=0.6, centre=(10.0, 0, 1.6),
             direction=(1, 0, 0)),
        part("tube", "#FFD60A", points=[(7.0, 0, 1.6), (9.8, 0, 1.6)], radius=0.04),
    ]
    return scene("A sound wave (longitudinal)", parts, [
        label("Vibrating source", (-10.0, 0, 1.8), (-10.0, 0, 0.9)),
        label("Sound travels this way", (8.0, 0, 2.5), (8.5, 0, 1.65)),
        label("Crowded = compression, spread out = rarefaction", (0, 0, -2.2), (0, 0, -0.8)),
    ], note="Particles move back and forth along the direction the sound travels",
        view={"camera": "xz", "elevation": 20, "azimuth": -10, "zoom": 1.3}, spin=False)


def earth_moon(_payload=""):
    parts = [part("sphere", "#FDB813", radius=1.8, lighting=False),
             part("tube", ORBIT_COLOUR, points=circle(8.0), radius=0.03, closed=True,
                  lighting=False),
             part("sphere", "#2E86DE", radius=0.85, id="earth",
                  orbit={"around": (0, 0, 0), "radius": 8.0, "period": 36.0, "phase": 0.6}),
             # The axis keeps pointing the same way all year: that is the seasons.
             part("cylinder", "#E6E9F5", radius=0.04, height=2.2,
                  direction=(math.sin(math.radians(23.5)), 0, math.cos(math.radians(23.5))),
                  orbit={"around": "earth", "radius": 0}),
             part("sphere", "#CFCFCF", radius=0.3, id="moon",
                  orbit={"around": "earth", "radius": 1.9, "period": 6.0, "phase": 0.0})]
    return scene("The Sun, the Earth and the Moon", parts, [
        label("Sun", (0, 0, 3.0)), follow("Earth", "earth", (0, 0, 1.4)),
        follow("Moon", "moon", (0, 0, 0.65)),
    ], note="Lit by the Sun: see day and night, and the Moon's phases. Not to scale",
        view={"elevation": 32, "zoom": 1.5}, spin=False, lighting="sun")


def earth_layers(_payload=""):
    # One corner cut away, so every layer shows at once.
    cut = {"box": (0, 10, -10, 0, 0, 10)}
    parts = [part("sphere", "#6D8B4E", radius=3.0, cut=cut),
             part("sphere", "#E07A3A", radius=2.85, cut=cut),
             part("sphere", "#F6BD60", radius=1.65, cut=cut),
             part("sphere", "#FFF3B0", radius=0.95)]
    return scene("Layers of the Earth", parts, [
        label("Crust (thin, rocky)", (5.2, 0, -1.6), (2.0, -2.0, -0.9)),
        label("Mantle (hot rock)", (5.2, 0, 0.0), (2.0, -2.0, 0.0)),
        label("Outer core (liquid metal)", (5.2, 0, 1.6), (1.15, -1.15, 0.05)),
        label("Inner core (solid metal)", (5.2, 0, 3.2), (0.3, -0.5, 0.75)),
    ], note="Not to scale: the crust is far thinner than drawn",
        view={"azimuth": 0, "elevation": 22, "zoom": 1.15})


# ===========================================================================
# BIOLOGY
# ===========================================================================
def _scatter(rng, count, low, high, avoid=None, avoid_r=0.0, flat=1.0):
    """Points in a shell low..high from the origin, not inside `avoid`."""
    out = []
    while len(out) < count:
        p = rng.normal(size=3)
        p /= np.linalg.norm(p)
        p *= rng.uniform(low, high)
        p[2] *= flat
        if avoid is not None and np.linalg.norm(p - avoid) < avoid_r:
            continue
        out.append(p)
    return out


def animal_cell(_payload=""):
    rng = np.random.default_rng(3)
    nucleus = np.array([0.4, 0.2, 0.0])
    parts = [part("sphere", "#F5A3B5", radius=5.0, opacity=0.13),
             part("sphere", "#7B5CD6", radius=1.6, centre=tuple(nucleus), opacity=0.55),
             part("sphere", "#3E2A8C", radius=0.55, centre=tuple(nucleus + [0.2, 0.1, 0.1]))]
    # Endoplasmic reticulum: layered curved membranes wrapped round the nucleus,
    # studded with ribosomes on the rough side.
    studs = []
    for layer, z in enumerate((-0.6, -0.2, 0.2, 0.6)):
        radius = 2.2 + 0.25 * layer
        arc = [tuple(nucleus + [radius * math.cos(a), radius * math.sin(a), z])
               for a in np.linspace(math.radians(200), math.radians(340), 30)]
        parts.append(part("tube", "#5AA9E6", points=arc, radius=0.11))
        studs += [tuple(nucleus + [(radius + 0.17) * math.cos(a), (radius + 0.17) * math.sin(a), z])
                  for a in np.linspace(math.radians(205), math.radians(335), 12)]
    parts.append(part("points", "#DDE4F7", centres=studs, radius=0.07))
    for k, z in enumerate((-0.36, -0.12, 0.12, 0.36)):
        parts.append(part("ellipsoid", "#F2B134", radii=(0.95 - 0.08 * abs(k - 1.5), 0.32, 0.07),
                          centre=(-2.5, 1.9, z), rotate=(0, 0, 35)))
    parts.append(part("points", "#F2B134", centres=[(-1.6, 2.7, 0.3), (-1.8, 1.2, -0.2),
                                                   (-3.3, 2.8, 0.1)], radius=0.14))
    mito = [(2.9, 2.2, 0.8), (-3.2, -1.2, -0.6), (1.2, -3.3, 0.9), (3.4, -0.6, -1.0),
            (-0.8, 3.4, -1.1)]
    for i, c in enumerate(mito):
        parts.append(part("ellipsoid", "#F07A3A", radii=(0.75, 0.3, 0.3), centre=c,
                          rotate=tuple(float(v) for v in rng.uniform(0, 180, 3))))
    lyso = [(-2.2, -2.8, 0.4), (2.4, 3.0, -0.5), (-3.6, 0.8, 1.0)]
    parts.append(part("points", "#6CCB5F", centres=lyso, radius=0.3))
    parts.append(part("sphere", "#9AD5F5", radius=0.5, centre=(2.9, -2.4, 0.6), opacity=0.7))
    parts.append(part("cylinder", "#D9D9D9", radius=0.08, height=0.5, centre=(-1.4, -3.6, -0.6),
                      direction=(1, 0, 0)))
    parts.append(part("cylinder", "#D9D9D9", radius=0.08, height=0.5, centre=(-1.4, -3.6, -0.6),
                      direction=(0, 0, 1)))
    free = _scatter(rng, 110, 1.9, 4.6, avoid=nucleus, avoid_r=2.0, flat=0.8)
    parts.append(part("points", "#DDE4F7", centres=[tuple(p) for p in free], radius=0.05))
    return scene("Animal cell", parts, columns([
        ("Cell membrane", (4.2, -2.6, -0.9)),
        ("Nucleus", tuple(nucleus + [-0.9, -0.8, 1.0])),
        ("Nucleolus", tuple(nucleus + [0.2, -0.3, 0.3])),
        ("Mitochondrion", mito[0]),
        ("Endoplasmic reticulum", tuple(nucleus + [2.4 * math.cos(math.radians(290)),
                                                   2.4 * math.sin(math.radians(290)), -0.6])),
        ("Golgi apparatus", (-2.5, 1.9, 0.36)),
        ("Lysosome", lyso[2]),
        ("Vacuole", (2.9, -2.4, 1.1)),
        ("Ribosomes", tuple(free[0])),
    ], 7.6, 4.2, -4.2), note="Parts shown larger than life so they can be seen",
        view={"elevation": 20, "zoom": 1.35})


def plant_cell(_payload=""):
    rng = np.random.default_rng(5)
    w, h, d = 10.0, 7.0, 5.0
    parts = [part("box", "#6FBF4A", size=(w, h, d), opacity=0.16),
             part("box", "#B4E08A", size=(w - 0.5, h - 0.5, d - 0.5), opacity=0.08)]
    corners = [(sx * w / 2, sy * h / 2, sz * d / 2) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    for a in corners:
        for b in corners:
            if a < b and sum(x != y for x, y in zip(a, b)) == 1:
                parts.append(part("tube", "#4E9A34", points=[a, b], radius=0.09))
    parts.append(part("ellipsoid", "#8FD3F5", radii=(3.3, 2.1, 1.6), centre=(0.6, -0.2, 0),
                      opacity=0.45))
    nucleus = (-3.3, 1.6, 0.4)
    parts.append(part("sphere", "#7B5CD6", radius=1.0, centre=nucleus, opacity=0.6))
    parts.append(part("sphere", "#3E2A8C", radius=0.35, centre=(-3.2, 1.7, 0.5)))
    chloro = [(-3.6, -1.9, -0.8), (-1.4, 2.8, 1.2), (1.8, 2.7, -1.2), (4.0, 1.6, 0.8),
              (4.1, -1.8, -0.9), (1.2, -2.9, 1.3), (-1.8, -2.8, 0.2), (3.0, 0.2, 1.8)]
    for c in chloro:
        parts.append(part("ellipsoid", "#2E9E44", radii=(0.6, 0.32, 0.2), centre=c,
                          rotate=tuple(float(v) for v in rng.uniform(0, 180, 3))))
    for c in [(-2.2, 0.6, -1.4), (2.6, -2.4, -1.5)]:
        parts.append(part("ellipsoid", "#F07A3A", radii=(0.45, 0.2, 0.2), centre=c,
                          rotate=tuple(float(v) for v in rng.uniform(0, 180, 3))))
    return scene("Plant cell", parts, columns([
        ("Cell wall", (5.0, -3.5, 2.5)),
        ("Cell membrane", (-4.75, -3.25, 2.25)),
        ("Vacuole (large)", (1.2, -1.2, 1.3)),
        ("Nucleus", (-3.3, 1.6, 1.4)),
        ("Chloroplast", chloro[4]),
        ("Mitochondrion", (2.6, -2.4, -1.5)),
        ("Cytoplasm", (-4.0, -0.5, -1.5)),
    ], 8.2, 3.6, -3.6), view={"elevation": 20, "zoom": 1.25})


BASE_COLOURS = {"A": "#34C759", "T": "#FF453A", "G": "#FFD60A", "C": "#0A84FF"}


def dna(_payload=""):
    rng = np.random.default_rng(11)
    pairs = [("A", "T"), ("T", "A"), ("G", "C"), ("C", "G")]
    bp, rise, radius, twist = 24, 0.34, 1.0, math.radians(36)
    offset = math.radians(150)          # the grooves: major and minor
    z0 = -bp * rise / 2

    def strand(shift):
        return [(radius * math.cos(i * twist / 8 + shift), radius * math.sin(i * twist / 8 + shift),
                 z0 + i * rise / 8) for i in range(bp * 8)]

    parts = [part("tube", "#E3E8F5", points=strand(0), radius=0.13),
             part("tube", "#AEB9D6", points=strand(offset), radius=0.13)]
    for i in range(bp):
        a, b = pairs[rng.integers(4)]
        angle, z = i * twist, z0 + i * rise
        pa = np.array([radius * math.cos(angle), radius * math.sin(angle), z])
        pb = np.array([radius * math.cos(angle + offset), radius * math.sin(angle + offset), z])
        mid = (pa + pb) / 2
        for start, end, base in ((pa, mid, a), (mid, pb, b)):
            parts.append(part("cylinder", BASE_COLOURS[base], radius=0.09,
                              height=float(np.linalg.norm(end - start)),
                              centre=tuple((start + end) / 2), direction=tuple(end - start)))
    return scene("DNA double helix", parts, [
        label("Sugar-phosphate backbone", (3.4, 0, 3.0), strand(0)[-12]),
        label("Base pairs", (2.8, -0.6, -1.2), (0.3, -0.2, -1.0)),
    ], legend=[("A", "Adenine", BASE_COLOURS["A"]), ("T", "Thymine", BASE_COLOURS["T"]),
               ("G", "Guanine", BASE_COLOURS["G"]), ("C", "Cytosine", BASE_COLOURS["C"])],
        note="A always pairs with T, and G with C",
        view={"elevation": 10, "zoom": 0.95})


def neuron(_payload=""):
    rng = np.random.default_rng(8)
    parts = [part("sphere", "#F2A65A", radius=1.0, opacity=0.65),
             part("sphere", "#7B3F00", radius=0.4, centre=(-0.1, 0, 0.1)),
             part("cone", "#F2A65A", radius=0.45, height=0.9, centre=(1.2, 0, 0),
                  direction=(1, 0, 0))]
    dendrite_tip = None

    def branch(start, direction, length, radius, depth):
        nonlocal dendrite_tip
        direction = direction / np.linalg.norm(direction)
        points = [tuple(start)]
        p = np.array(start, float)
        for _ in range(4):
            direction = direction + rng.normal(scale=0.25, size=3)
            direction /= np.linalg.norm(direction)
            p = p + direction * length / 4
            points.append(tuple(p))
        parts.append(part("tube", "#F2A65A", points=points, radius=radius))
        dendrite_tip = dendrite_tip or points[-1]
        if depth > 0:
            for _ in range(2):
                branch(p, direction + rng.normal(scale=0.7, size=3), length * 0.7,
                       radius * 0.65, depth - 1)

    for k in range(6):
        a = TAU * k / 6
        d = np.array([-0.8, math.cos(a), math.sin(a)])
        branch(d / np.linalg.norm(d) * 0.9, d, 1.8, 0.13, 2)
    parts.append(part("tube", "#F2A65A", points=[(1.6, 0, 0), (13.0, 0, 0)], radius=0.12))
    x = 2.0
    while x + 1.6 <= 12.4:
        parts.append(part("cylinder", "#F5F0DC", radius=0.32, height=1.6,
                          centre=(x + 0.8, 0, 0), direction=(1, 0, 0), opacity=0.92))
        x += 1.95
    for k in range(5):
        a = TAU * k / 5
        end = (14.3, 0.9 * math.cos(a), 0.9 * math.sin(a))
        parts.append(part("tube", "#F2A65A", points=[(13.0, 0, 0), (13.7, 0.4 * math.cos(a),
                                                                    0.4 * math.sin(a)), end],
                          radius=0.07))
        parts.append(part("sphere", "#F2A65A", radius=0.17, centre=end))
    return scene("Neuron (nerve cell)", parts, [
        label("Dendrites", (-4.2, 0, 3.0), dendrite_tip),
        label("Cell body", (0.6, 0, -2.6), (0.2, -0.3, -0.9)),
        label("Nucleus", (0.8, 0, 2.6), (-0.1, 0.1, 0.5)),
        label("Axon", (6.8, 0, -1.6), (6.8, 0, -0.33)),
        label("Myelin sheath", (4.9, 0, 1.6), (4.9, 0, 0.33)),
        label("Node of Ranvier", (9.7, 0, 1.8), (9.8, 0, 0.13)),
        label("Axon terminals", (14.4, 0, -2.0), (14.3, -0.3, -0.85)),
    ], note="Messages travel from the dendrites, through the cell body, along the axon",
        view={"elevation": 20, "azimuth": -10, "zoom": 1.6})


def _rbc_profile(radius=2.0, n=60):
    """Half of a red cell's cross-section: thin in the middle, thick at the rim."""
    xs = np.linspace(0, 1, n)
    top = [(radius * x, radius * math.sqrt(max(0.0, 1 - x * x)) * (0.1 + 0.6 * x * x - 0.4 * x ** 4))
           for x in xs]
    return top + [(r, -z) for r, z in reversed(top[:-1])]


def red_blood_cell(_payload=""):
    profile = _rbc_profile()
    parts = [part("revolve", "#D7263D", profile=profile),
             part("revolve", "#C21F35", profile=profile, centre=(4.4, 2.2, 0.6), rotate=(55, 0, 20)),
             part("revolve", "#C21F35", profile=profile, centre=(-4.2, 2.0, -0.4), rotate=(90, 0, -30))]
    return scene("Red blood cells", parts, [
        label("Thin in the middle", (0.6, -2.6, 1.8), (0, 0, 0.2)),
        label("Thick, rounded rim", (3.4, -2.8, -0.2), (1.8, -0.8, 0.2)),
        label("Seen side-on", (-4.2, 0.0, 3.0), (-4.2, 2.0, 1.4)),
    ], note="Biconcave discs, packed with haemoglobin to carry oxygen",
        view={"elevation": 35, "zoom": 1.2})


def virus(_payload=""):
    tips = fibonacci_sphere(60, 1.0)
    parts = [part("sphere", "#BFC6D9", radius=2.0)]
    for t in tips:
        t = np.array(t)
        parts.append(part("cylinder", "#E0484F", radius=0.07, height=0.55,
                          centre=tuple(t * 2.25), direction=tuple(t)))
        parts.append(part("ellipsoid", "#E0484F", radii=(0.18, 0.18, 0.24),
                          centre=tuple(t * 2.6), direction=tuple(t)))
    dots = fibonacci_sphere(40, 2.02)
    parts.append(part("points", "#F4C542", centres=dots[::1], radius=0.09))
    front = [t for t in tips if t[1] < -0.3]          # on the side facing the camera
    return scene("Virus (coronavirus)", parts, columns([
        ("Spike proteins", tuple(np.array(max(front, key=lambda t: t[0] + t[2])) * 2.6)),
        ("Envelope", tuple(np.array(min(front, key=lambda t: t[0] - t[2])) * 2.0)),
        ("Envelope proteins", min((d for d in dots if d[1] < -1.0), key=lambda d: d[0] + d[2])),
    ], 4.6, 2.2, -2.2), note="The genetic material (RNA) is inside the envelope",
        view={"zoom": 1.2})


def bacteriophage(_payload=""):
    rng = np.random.default_rng(4)
    coil, p = [], np.array([0.0, 0.0, 3.2])
    for _ in range(60):
        p = p + rng.normal(scale=0.25, size=3)
        if np.linalg.norm(p - [0, 0, 3.2]) > 0.65:
            p = np.array([0, 0, 3.2]) + (p - [0, 0, 3.2]) * 0.6
        coil.append(tuple(p))
    parts = [part("icosahedron", "#8DA9E6", radius=1.05, centre=(0, 0, 3.2), opacity=0.45),
             part("tube", "#FF6B6B", points=coil, radius=0.05),
             part("cylinder", "#C9D2EA", radius=0.35, height=0.18, centre=(0, 0, 2.15)),
             part("cylinder", "#C9D2EA", radius=0.24, height=1.9, centre=(0, 0, 1.1))]
    for z in np.linspace(0.3, 1.9, 7):
        parts.append(part("torus", "#AEB9D6", ring=0.25, tube=0.05, centre=(0, 0, float(z))))
    parts.append(part("cylinder", "#C9D2EA", radius=0.6, height=0.14, centre=(0, 0, 0.1),
                      resolution=6))
    for k in range(6):
        a = TAU * k / 6 + TAU / 12
        knee = (1.3 * math.cos(a), 1.3 * math.sin(a), 0.5)
        foot = (1.9 * math.cos(a), 1.9 * math.sin(a), -1.1)
        parts.append(part("tube", "#C9D2EA", points=[(0.5 * math.cos(a), 0.5 * math.sin(a), 0.05),
                                                     knee, foot], radius=0.05))
    return scene("Bacteriophage (a virus that infects bacteria)", parts, [
        callout("Head (capsid)", (0.9, 0, 3.7), 3.4, centre=(0, 0, 2.6)),
        callout("DNA inside", coil[20], 3.4, centre=(0, 0, 3.2)),
        callout("Tail sheath", (0.3, 0, 1.2), 3.2, centre=(0, 0, 1.2)),
        callout("Base plate", (0.6, 0, 0.05), 3.2, centre=(0, 0, 0.5)),
        callout("Tail fibres", (1.6, 0, -0.5), 3.4, centre=(0, 0, 0.2)),
    ], view={"elevation": 15, "zoom": 1.2})


def _capsule_profile(radius, length, n=24):
    """A capsule's outline, spun round z by `revolve`: a rod with round ends."""
    half = length / 2
    top = [(radius * math.sin(a), half + radius * math.cos(a)) for a in np.linspace(0, math.pi / 2, n)]
    bottom = [(radius * math.cos(a), -half - radius * math.sin(a)) for a in np.linspace(0, math.pi / 2, n)]
    return top + bottom


def bacterium(_payload=""):
    rng = np.random.default_rng(9)
    parts = [part("revolve", "#8ED16F", profile=_capsule_profile(1.35, 5.0), rotate=(0, 90, 0),
                  opacity=0.22),
             part("revolve", "#5DBB63", profile=_capsule_profile(1.15, 5.0), rotate=(0, 90, 0),
                  opacity=0.16)]
    walk, p = [], np.zeros(3)
    for _ in range(140):
        p = p + rng.normal(scale=0.28, size=3)
        p = np.clip(p, [-1.6, -0.6, -0.6], [1.6, 0.6, 0.6])
        walk.append(tuple(p))
    parts.append(part("tube", "#FF6B6B", points=walk, radius=0.05))
    parts.append(part("torus", "#FFB86B", ring=0.3, tube=0.045, centre=(2.3, 0.4, -0.2),
                      rotate=(40, 20, 0)))
    ribo = []
    while len(ribo) < 80:
        q = rng.uniform([-3.2, -1.0, -1.0], [3.2, 1.0, 1.0])
        if abs(q[0]) <= 2.5 and np.hypot(q[1], q[2]) < 0.95 or np.linalg.norm(
                [max(abs(q[0]) - 2.5, 0), q[1], q[2]]) < 0.95:
            ribo.append(tuple(q))
    parts.append(part("points", "#E6E9F5", centres=ribo, radius=0.06))
    t = np.linspace(0, 7.0, 90)
    parts.append(part("tube", "#D7DEEF", points=[(-3.7 - s, 0.45 * math.sin(2.2 * s),
                                                  0.25 * math.cos(2.2 * s)) for s in t],
                      radius=0.05))
    for k in range(22):
        a = TAU * k / 22
        x = rng.uniform(-2.4, 2.4)
        base = np.array([x, 1.35 * math.cos(a), 1.35 * math.sin(a)])
        tip = base + np.array([0, math.cos(a), math.sin(a)]) * 0.55
        parts.append(part("tube", "#D7DEEF", points=[tuple(base), tuple(tip)], radius=0.022))
    return scene("Bacterium (rod-shaped)", parts, rows([
        ("Cell wall", (-0.8, -1.3, 0.35)),
        ("Cell membrane", (-2.2, -0.9, 0.6)),
        ("Nucleoid (DNA)", walk[60]),
        ("Plasmid", (2.3, 0.4, -0.1)),
        ("Ribosomes", ribo[3]),
        ("Pili", (1.6, -0.5, 1.8)),
        ("Flagellum", (-7.8, 0.4, -0.3)),
        ("Cytoplasm", (0.6, -0.8, -0.5)),
    ], -8.0, 3.4, 3.0, -3.0), view={"elevation": 18, "zoom": 1.4})


def flower(_payload=""):
    parts = [part("tube", "#3D9A3D", points=[(0, 0, -6.0), (0, 0, -0.4)], radius=0.15),
             part("sphere", "#4CAF50", radius=0.5, centre=(0, 0, -0.3))]
    for k in range(5):
        a = TAU * k / 5
        c, s = math.cos(a), math.sin(a)
        parts.append(part("ellipsoid", "#F06292", radii=(2.0, 0.95, 0.07),
                          centre=(1.9 * c, 1.9 * s, 0.35), rotate=(0, -25, math.degrees(a))))
        b = a + TAU / 10
        parts.append(part("ellipsoid", "#4CAF50", radii=(1.2, 0.42, 0.06),
                          centre=(1.1 * math.cos(b), 1.1 * math.sin(b), -0.55),
                          rotate=(0, 30, math.degrees(b))))
    for k in range(6):
        a = TAU * k / 6 + 0.3
        top = (0.95 * math.cos(a), 0.95 * math.sin(a), 1.9)
        parts.append(part("tube", "#FFF3B0", points=[(0.2 * math.cos(a), 0.2 * math.sin(a), 0.2), top],
                          radius=0.04))
        parts.append(part("ellipsoid", "#FFC107", radii=(0.12, 0.12, 0.22), centre=top))
    parts += [part("ellipsoid", "#8BC34A", radii=(0.42, 0.42, 0.55), centre=(0, 0, 0.35),
                   opacity=0.55),
              part("points", "#FFFFFF", centres=[(0.12, 0.1, 0.3), (-0.14, 0.05, 0.4),
                                                (0.02, -0.15, 0.25)], radius=0.08),
              part("cylinder", "#AED581", radius=0.07, height=1.4, centre=(0, 0, 1.55)),
              part("sphere", "#CDDC39", radius=0.2, centre=(0, 0, 2.3))]
    return scene("Parts of a flower", parts, [
        label("Petal", (4.2, -1.8, 2.0), (2.6, -0.8, 0.9)),
        label("Sepal", (1.9, -2.6, -1.9), (0.9, -1.1, -0.8)),
        label("Anther", (-2.0, 1.8, 3.2), (-0.62, 0.72, 1.95)),
        label("Filament", (-2.6, 0.2, 1.4), (-0.5, 0.35, 1.2)),
        label("Stigma", (1.0, 1.4, 3.4), (0, 0, 2.45)),
        label("Style", (1.6, 1.8, 2.2), (0, 0.05, 1.6)),
        label("Ovary", (-2.2, -1.9, 0.6), (-0.3, -0.25, 0.3)),
        label("Ovule", (2.4, 0, -0.4), (0.1, 0.1, 0.3)),
    ], legend=[("P", "Petal", "#F06292"), ("S", "Sepal", "#4CAF50"),
               ("St", "Stamen", "#FFC107"), ("Pi", "Pistil", "#8BC34A")],
        note="Stamen = anther + filament.  Pistil = stigma + style + ovary",
        view={"elevation": 30, "zoom": 1.1})


def eye(_payload=""):
    top_off = {"normal": (0, 0, 1)}            # cut the top half away: a section
    parts = [part("sphere", "#F2EFE6", radius=1.2, cut=top_off, cut_front=0.95),
             part("sphere", "#8C2F39", radius=1.16, cut=top_off, cut_front=0.62),
             part("sphere", "#E0564B", radius=1.12, cut=top_off, cut_front=0.55),
             part("sphere", "#BFE6FF", radius=0.8394, centre=(0.5406, 0, 0), cut=top_off,
                  keep_front=0.95, opacity=0.4),
             part("disc", "#7A4A2A", inner=0.22, outer=0.72, centre=(0.9, 0, 0),
                  normal=(1, 0, 0), cut=top_off),
             part("ellipsoid", "#D9F1FF", radii=(0.2, 0.45, 0.45), centre=(0.72, 0, 0),
                  opacity=0.65),
             part("torus", "#C0504D", ring=0.6, tube=0.07, centre=(0.72, 0, 0),
                  normal=(1, 0, 0), cut=top_off),
             part("cylinder", "#F4D35E", radius=0.18, height=0.9, centre=(-1.55, 0.25, 0),
                  direction=(1, 0, 0))]
    return scene("The human eye (cut open)", parts, [
        label("Cornea", (1.9, -0.9, 0.7), (1.3, -0.3, 0)),
        label("Iris", (1.5, 1.3, 0.9), (0.9, 0.5, 0)),
        label("Pupil", (1.9, 0.35, 0.9), (0.95, 0.05, 0)),
        label("Lens", (0.9, -1.4, 1.2), (0.72, -0.25, 0.3)),
        label("Ciliary muscle", (0.2, 1.8, 0.8), (0.72, 0.6, 0)),
        label("Retina", (-0.6, -1.9, 0.9), (-0.7, -0.85, 0)),
        label("Sclera", (-0.2, 1.9, -0.4), (-0.4, 1.12, -0.2)),
        label("Optic nerve", (-2.4, -0.5, 0.8), (-1.8, 0.25, 0.15)),
    ], note="Light passes through the cornea, pupil and lens, and focuses on the retina",
        view={"elevation": 55, "azimuth": -20, "zoom": 1.1})


def mitochondrion(_payload=""):
    parts = [part("ellipsoid", "#F4A261", radii=(3.0, 1.3, 1.3), opacity=0.22),
             part("ellipsoid", "#F4A261", radii=(2.8, 1.12, 1.12), opacity=0.14)]
    for k, x in enumerate(np.linspace(-2.1, 2.1, 8)):
        room = 1.12 * math.sqrt(max(0.0, 1 - (x / 2.8) ** 2))
        shift = 0.28 * room if k % 2 else -0.28 * room
        parts.append(part("ellipsoid", "#E76F51", radii=(0.07, room * 0.72, room * 0.9),
                          centre=(float(x), shift, 0)))
    rng = np.random.default_rng(2)
    dots = [tuple(p) for p in rng.uniform([-2.2, -0.6, -0.6], [2.2, 0.6, 0.6], (25, 3))]
    parts.append(part("points", "#FFE8A3", centres=dots, radius=0.06))
    return scene("Mitochondrion", parts, rows([
        ("Outer membrane", (1.2, -1.15, 0.3)),
        ("Inner membrane", (-2.0, -0.7, 0.4)),
        ("Cristae (folds)", (0.3, -0.2, -0.6)),
        ("Matrix", (-1.0, -0.3, -0.3)),
    ], -3.0, 3.0, 2.4, -2.4), note="The powerhouse of the cell: releases energy from food",
        view={"elevation": 40, "zoom": 1.2})


def chloroplast(_payload=""):
    parts = [part("ellipsoid", "#74C69D", radii=(3.0, 1.5, 1.0), opacity=0.22)]
    grana = [(-1.8, 0.2, 0), (-0.6, -0.5, 0), (0.6, 0.4, 0), (1.8, -0.3, 0), (0.0, 0.3, -0.1)]
    for gx, gy, gz in grana:
        for k in range(5):
            parts.append(part("cylinder", "#2D6A4F", radius=0.36, height=0.07,
                              centre=(gx, gy, gz - 0.28 + 0.14 * k)))
    for a, b in zip(grana, grana[1:]):
        parts.append(part("tube", "#52B788", points=[a, b], radius=0.04))
    return scene("Chloroplast", parts, rows([
        ("Outer membrane", (1.0, -1.35, 0.3)),
        ("Granum (stack of thylakoids)", (0.6, 0.04, 0.3)),
        ("Thylakoid", (-1.8, -0.16, 0.0)),
        ("Stroma", (2.3, -0.3, -0.3)),
        ("Lamella", (-1.2, -0.15, -0.05)),
    ], -3.2, 3.2, 2.0, -2.0), note="Where photosynthesis happens: the green comes from chlorophyll",
        view={"elevation": 40, "zoom": 1.2})


# ---------------------------------------------------------------------------
# finding the model a request is asking for
# ---------------------------------------------------------------------------
# Tried in order, first match wins. More specific names first: "plant cell"
# before "cell", "sound wave" before "wave", "bacteriophage" before "virus".
CATALOGUE = [
    # Just "Earth" or "a globe": the layers model, one corner cut away. Before
    # this "3D model of Earth" matched nothing, fell through to PubChem and
    # came up as a flat picture, three times running. Ahead of the solar
    # system, whose "planet" would otherwise take "planet earth"; anchored, so
    # it takes only a request that is the Earth and nothing else.
    (r"^\W*(?:the |a |planet |our |3d |model of )*(?:earth|globe)\W*(?:model|in 3d|3d)?\W*$|\bglobe\b",
     earth_layers),
    (r"solar system|planets?\b(?! of)|\bsun and (the )?planets", solar_system),
    (r"(sun|earth).*(moon)|(moon).*(earth|sun)|phases? of the moon|eclipse|day and night|seasons",
     earth_moon),
    (r"layers? of the earth|earth'?s (layers|interior|structure)|inside the earth|\bcore\b.*\bmantle\b",
     earth_layers),
    (r"\batom\b|bohr|electron shells?|atomic (structure|model)|structure of (an )?atom", atom),
    (r"solenoid|\bcoil\b", solenoid),
    (r"(straight )?(current[- ]carrying )?(wire|conductor)", wire_field),
    (r"bar magnet|magnetic field|magnet\b|field lines", bar_magnet),
    (r"prism|dispersion|spectrum|vibgyor|rainbow", prism),
    (r"sound wave|longitudinal|compression|rarefaction", sound_wave),
    (r"transverse|\bwaves?\b", transverse_wave),
    (r"plant cell", plant_cell),
    (r"animal cell|human cell|\bcell\b(?! phone)", animal_cell),
    (r"\bdna\b|double helix|deoxyribo", dna),
    (r"neuron|nerve cell", neuron),
    (r"red blood|\brbcs?\b|erythrocyte|blood cells?", red_blood_cell),
    (r"bacteriophage|\bphage\b", bacteriophage),
    (r"virus|corona", virus),
    (r"bacteri", bacterium),
    (r"flower", flower),
    (r"\beyes?\b|eyeball", eye),
    (r"mitochondri", mitochondrion),
    (r"chloroplast", chloroplast),
]

# The model is told which of these exist in prompts.py (the model3d entry of
# AGENTIC_ACTIONS). A new builder here needs its name added there too.


def find_model(payload):
    """The builder for a request, or None when none of these fits."""
    text = (payload or "").lower()
    for pattern, builder in CATALOGUE:
        if re.search(pattern, text):
            # "Carbon" is a molecule question; "carbon atom" is this one.
            return builder
    return None
