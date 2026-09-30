"""Procedural EDUCATIONAL models of tissue, cells and organelles.

There is no reputable, openly licensed, part-labelled 3D model of a single
cardiomyocyte to download, so these are built from primitives, following the
textbook description of each structure. Every one is marked
"Educational model — not to scale" and says what it simplifies.
Dimensions are in micrometres and are realistic where stated:

    cardiomyocyte   ~100 µm long, 10-25 µm wide, branched, one central nucleus
    sarcomere       ~2 µm (drawn WIDER in the tissue view, and said so)
    mitochondrion   ~1-2 µm long, 0.5-1 µm wide, with cristae (inner-membrane folds)

Layouts use a fixed random seed, so a model looks the same every time.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from visualization.scene import SceneModel, ScenePart

NOT_TO_SCALE = "Educational model — not to scale"


# ------------------------------------------------------------------ primitives
def rounded_box(center, half, squareness: float = 0.25, direction=(1, 0, 0), res: int = 24) -> pv.PolyData:
    """A box with rounded edges: squareness 0 = box, 1 = ellipsoid."""
    mesh = pv.ParametricSuperEllipsoid(xradius=half[0], yradius=half[1], zradius=half[2],
                                       n1=squareness, n2=squareness, u_res=res, v_res=res, w_res=res)
    return _place(mesh, center, direction)


def ellipsoid(center, radii, direction=(1, 0, 0), res: int = 18) -> pv.PolyData:
    mesh = pv.ParametricEllipsoid(radii[0], radii[1], radii[2], u_res=res, v_res=res, w_res=res)
    return _place(mesh, center, direction)


def _place(mesh: pv.PolyData, center, direction) -> pv.PolyData:
    d = np.asarray(direction, dtype=float)
    d /= np.linalg.norm(d)
    x = np.array([1.0, 0.0, 0.0])
    if not np.allclose(d, x):
        axis = np.cross(x, d)
        if np.linalg.norm(axis) < 1e-9:
            mesh = mesh.rotate_z(180, inplace=False)
        else:
            angle = np.degrees(np.arccos(np.clip(np.dot(x, d), -1, 1)))
            mesh = mesh.rotate_vector(axis / np.linalg.norm(axis), angle, inplace=False)
    return mesh.translate(center, inplace=False).triangulate()


def tube_between(a, b, radius: float, sides: int = 10) -> pv.PolyData:
    return pv.Cylinder(center=(np.asarray(a) + np.asarray(b)) / 2, direction=np.asarray(b) - np.asarray(a),
                       radius=radius, height=float(np.linalg.norm(np.asarray(b) - np.asarray(a))),
                       resolution=sides).triangulate()


def merge(meshes: list[pv.PolyData]) -> pv.PolyData:
    meshes = [m for m in meshes if m.n_points]
    out = meshes[0] if len(meshes) == 1 else pv.merge(meshes)
    return out.triangulate().clean()


def cut_half(mesh: pv.PolyData, axis: str = "z", keep_below: float = 0.0) -> pv.PolyData:
    """Remove everything above `keep_below` on `axis` -- the cut-away view."""
    normal = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
    origin = [0.0, 0.0, 0.0]
    origin["xyz".index(axis)] = keep_below
    return mesh.clip(normal=normal, origin=origin, invert=True)


def _scene(entry, parts: list[ScenePart], units="µm", front="+z", view=(0.0, 0.0), **extras) -> SceneModel:
    manifest_parts = entry.parts if entry is not None else {}
    for part in parts:
        spec = manifest_parts.get(part.id, {})
        part.name = spec.get("name", part.name)
        part.description = spec.get("description", part.description)
        part.color = spec.get("color", part.color)
    return SceneModel(model_id=entry.id if entry else "preview", title=entry.name if entry else "Preview",
                      parts={p.id: p for p in parts}, units=units, view=view,
                      extras={"front": front, **extras})


# ------------------------------------------------------------------ cardiac muscle tissue
def glyphs(centers, template: pv.PolyData, directions=None) -> pv.PolyData:
    """Copies of `template` at each centre (optionally turned to a direction).
    One VTK filter instead of hundreds of meshes merged one by one."""
    pts = pv.PolyData(np.asarray(centers, dtype=float))
    if directions is None:
        return pts.glyph(geom=template, orient=False, scale=False).triangulate()
    pts["dir"] = np.asarray(directions, dtype=float)
    return pts.glyph(geom=template, orient="dir", scale=False).triangulate()


def cardiac_muscle(entry=None) -> SceneModel:
    """A block of cardiac muscle: branching cells joined end to end by
    intercalated discs, one central nucleus each, capillaries between the rows."""
    rng = np.random.default_rng(7)
    cell_len, half_w, half_h, gap = 70.0, 7.0, 6.0, 1.2
    cells, nuclei, discs = [], [], []
    rows = [(-18.0, 0.0), (0.0, 0.0), (18.0, 0.0), (-9.0, 15.0), (9.0, 15.0)]
    for r, (y, z) in enumerate(rows):
        shift = (r % 2) * cell_len / 2
        for i in range(0, 3):
            x0 = i * (cell_len + gap) - shift
            if x0 - cell_len / 2 < -cell_len * 0.8 or x0 + cell_len / 2 > cell_len * 2.6:
                continue
            cells.append(rounded_box((x0, y, z), (cell_len / 2, half_w, half_h), 0.35))
            nuclei.append(ellipsoid((x0 + rng.uniform(-5, 5), y, z), (6.5, 2.6, 2.4)))
            for end in (-1, 1):
                disc_x = x0 + end * (cell_len / 2 + gap / 2)
                step = rng.choice([-1.5, 1.5])
                # Intercalated discs are stepped, not flat: two offset plates.
                discs.append(pv.Box((disc_x - 0.35, disc_x + 0.35, y - half_w, y, z - half_h, z + half_h)))
                discs.append(pv.Box((disc_x + step - 0.35, disc_x + step + 0.35, y, y + half_w, z - half_h, z + half_h)))
    # Branches: cardiac muscle cells fork and join neighbours in the next row.
    for (y1, z1), (y2, z2) in [((-18, 0), (-9, 15)), ((0, 0), (9, 15)), ((18, 0), (9, 15))]:
        a, b = np.array([40.0, y1, z1]), np.array([66.0, y2, z2])
        cells.append(rounded_box((a + b) / 2, (np.linalg.norm(b - a) / 2, 4.0, 3.6), 0.5, direction=b - a))
    tissue = merge(cells)
    # Striations as bands on the cell surface. Real sarcomeres repeat every
    # ~2 µm, which would alias to noise at this zoom; drawn every 6 µm.
    tissue["band"] = ((tissue.points[:, 0] % 6.0) < 2.2).astype(float)
    capillaries = []
    for y, z in [(-9.0, -9.0), (9.0, -9.0), (0.0, 24.0), (-22.0, 12.0), (22.0, 12.0)]:
        line = pv.Spline(np.array([[x, y + 1.5 * np.sin(x / 25), z + 1.5 * np.cos(x / 31)]
                                   for x in np.linspace(-60, 190, 30)]), 60)
        capillaries.append(line.tube(radius=2.8, n_sides=12).triangulate())
    parts = [
        ScenePart("cardiomyocytes", "Cardiac muscle cells", tissue, "#c9505a",
                  style={"scalars": "band", "cmap": ["#d65f68", "#9c3440"], "show_scalar_bar": False}),
        ScenePart("nuclei", "Nuclei", merge(nuclei), "#5b3f9a"),
        ScenePart("intercalated_discs", "Intercalated discs", merge(discs), "#f2e6c9"),
        ScenePart("capillaries", "Capillaries", merge(capillaries), "#e0302a"),
    ]
    return _scene(entry, parts, front="iso", view=(0.0, 0.0), zoom=1.3)


# ------------------------------------------------------------------ one cardiomyocyte
def cardiomyocyte(entry=None) -> SceneModel:
    """One cardiac muscle cell, cut open along the top so the inside shows:
    myofibrils with Z-discs every 2 µm, rows of mitochondria between them, a
    central nucleus, and intercalated discs at the ends."""
    rng = np.random.default_rng(11)
    L, W, H = 100.0, 18.0, 14.0
    membrane = cut_half(merge([rounded_box((0, 0, 0), (L / 2, W / 2, H / 2), 0.3, res=32),
                               rounded_box((L / 2 - 6, 9, 0), (14, 4.5, 4.5), 0.5, direction=(1, 0.8, 0))]),
                        keep_below=1.5)
    nucleus = ellipsoid((0, 0, 0), (7.5, 3.2, 3.0), res=24)
    fibrils, disc_centers = [], []
    ys, zs = np.linspace(-6.0, 6.0, 4), np.array([-4.0, -1.2])
    for y in ys:
        for z in zs:
            if abs(y) < 4.5 and abs(z) < 3.4:
                segments = [(-L / 2 + 3, -9.5), (9.5, L / 2 - 3)]   # parted around the nucleus
            else:
                segments = [(-L / 2 + 3, L / 2 - 3)]
            for x0, x1 in segments:
                fibrils.append(tube_between((x0, y, z), (x1, y, z), 0.75, sides=10))
                disc_centers += [(x, y, z) for x in np.arange(x0 + 1.0, x1, 2.0)]   # sarcomere ~2 µm
    zdisc = pv.Cylinder(center=(0, 0, 0), direction=(1, 0, 0), radius=0.85, height=0.18, resolution=10)
    mito_centers, mito_dirs = [], []
    for y in (ys[:-1] + ys[1:]) / 2:
        for z in (-2.6, 0.2):
            for x in np.arange(-L / 2 + 5, L / 2 - 4, 3.8):
                if abs(x) < 10 and abs(y) < 4.5:
                    continue
                if rng.random() < 0.85:
                    mito_centers.append((x + rng.uniform(-0.5, 0.5), y, z))
                    mito_dirs.append((1.0, 0.0, 0.0))
    for x in (-11.0, 11.0):            # perinuclear clusters at the nucleus poles
        for _ in range(6):
            mito_centers.append((x + rng.uniform(-1.5, 1.5), rng.uniform(-2.5, 2.5), rng.uniform(-2, 1)))
            mito_dirs.append(rng.normal(size=3))
    mito = pv.ParametricEllipsoid(1.7, 0.75, 0.75, u_res=12, v_res=12, w_res=12)
    ends = []
    for x in (-L / 2, L / 2):
        ends.append(pv.Box((x - 0.4, x + 0.4, -W / 2 + 1, 0, -H / 2 + 1, 1.5)))
        ends.append(pv.Box((x + np.sign(x) * 1.5 - 0.4, x + np.sign(x) * 1.5 + 0.4, 0, W / 2 - 1, -H / 2 + 1, 1.5)))
    parts = [
        ScenePart("sarcolemma", "Cell membrane (sarcolemma)", membrane, "#e8a0a8", opacity=0.3),
        ScenePart("nucleus", "Nucleus", nucleus, "#5b3f9a"),
        ScenePart("myofibrils", "Myofibrils", merge(fibrils), "#d8707c"),
        ScenePart("z_discs", "Z-discs (sarcomere boundaries)", glyphs(disc_centers, zdisc), "#3a2030"),
        ScenePart("mitochondria", "Mitochondria", glyphs(mito_centers, mito, mito_dirs), "#f0a040"),
        ScenePart("intercalated_discs", "Intercalated discs", merge(ends), "#f2e6c9"),
    ]
    return _scene(entry, parts, front="+z", view=(0.0, 35.0), zoom=1.25)


# ------------------------------------------------------------------ mitochondrion
def mitochondrion(entry=None) -> SceneModel:
    """A mitochondrion ~2 µm long, cut open lengthways: outer membrane, inner
    membrane folded into cristae, the matrix, mitochondrial DNA and ribosomes,
    and ATP synthase particles on the cristae."""
    rng = np.random.default_rng(3)
    a, b = 1.0, 0.45                    # half length, half width (µm)
    outer = cut_half(ellipsoid((0, 0, 0), (a, b, b), res=48))
    inner = cut_half(ellipsoid((0, 0, 0), (a - 0.06, b - 0.06, b - 0.06), res=48))
    matrix = cut_half(ellipsoid((0, 0, 0), (a - 0.1, b - 0.1, b - 0.1), res=32))
    cristae, synthase = [], []
    for i, x in enumerate(np.linspace(-0.72, 0.72, 8)):
        local = (b - 0.06) * np.sqrt(max(0.0, 1 - (x / (a - 0.06)) ** 2))
        depth = local * rng.uniform(1.1, 1.5)
        top = i % 2 == 0
        y0 = local - depth if top else -local
        y1 = local if top else -local + depth
        crista = rounded_box((x, (y0 + y1) / 2, 0), (0.025, (y1 - y0) / 2, local * 0.85), 0.2, res=20)
        cristae.append(cut_half(crista))
        for y in np.linspace(y0 + 0.05, y1 - 0.05, 4):
            for z in np.linspace(-local * 0.7, -0.03, 3):
                synthase.append(pv.Sphere(radius=0.012, center=(x + 0.035, y, z), theta_resolution=6, phi_resolution=6))
    dna = [pv.Spline(np.array([[cx + 0.06 * np.cos(t), cy + 0.06 * np.sin(t), -0.12 + 0.02 * np.sin(3 * t)]
                               for t in np.linspace(0, 2 * np.pi, 30)]), 60).tube(radius=0.006)
           for cx, cy in [(-0.45, 0.05), (0.38, -0.08)]]
    ribosomes = [pv.Sphere(radius=0.014, center=(rng.uniform(-0.8, 0.8), rng.uniform(-0.25, 0.25), rng.uniform(-0.25, -0.05)),
                           theta_resolution=6, phi_resolution=6) for _ in range(40)]
    parts = [
        ScenePart("outer_membrane", "Outer membrane", outer, "#f0a040", opacity=0.55),
        ScenePart("inner_membrane", "Inner membrane", inner, "#e07a30", opacity=0.6),
        ScenePart("cristae", "Cristae", merge(cristae), "#d46a2a"),
        ScenePart("matrix", "Matrix", matrix, "#f6d7a8", opacity=0.25),
        ScenePart("atp_synthase", "ATP synthase (enlarged)", merge(synthase), "#6fd0c0"),
        ScenePart("mtdna", "Mitochondrial DNA", merge(dna), "#7040c0"),
        ScenePart("ribosomes", "Mitochondrial ribosomes (enlarged)", merge(ribosomes), "#305090"),
    ]
    return _scene(entry, parts, front="+z", view=(0.0, 25.0))


# ------------------------------------------------------------------ animal cell
def animal_cell(entry=None) -> SceneModel:
    """A generalised animal cell, cut open: membrane, nucleus and nucleolus,
    rough ER, Golgi apparatus, mitochondria, lysosomes, ribosomes, centrioles."""
    rng = np.random.default_rng(5)
    membrane = cut_half(pv.Sphere(radius=10.0, theta_resolution=48, phi_resolution=48))
    nucleus = cut_half(pv.Sphere(radius=3.6, center=(-1.0, 0.5, 0), theta_resolution=36, phi_resolution=36))
    nucleolus = pv.Sphere(radius=1.1, center=(-1.3, 0.8, -0.6))
    er = []
    for r in (4.4, 5.0, 5.6):
        shell = pv.Sphere(radius=r, center=(-1.0, 0.5, 0), theta_resolution=40, phi_resolution=40,
                          start_theta=200, end_theta=340, start_phi=40, end_phi=140)
        er.append(cut_half(shell))
    golgi = []
    for k in range(5):
        disc = pv.Sphere(radius=2.6 - 0.25 * k, center=(0, 0, 0), theta_resolution=24, phi_resolution=12,
                         start_phi=70, end_phi=110, start_theta=0, end_theta=160)
        golgi.append(disc.scale((1, 1, 0.25), inplace=False).translate((4.5, 3.2 + 0.35 * k, -1.2), inplace=False))
    mitos = [ellipsoid((rng.uniform(-6, 6), rng.uniform(-6, 6), rng.uniform(-4, -0.5)), (1.2, 0.5, 0.5),
                       direction=rng.normal(size=3), res=12) for _ in range(9)]
    mitos = [m for m in mitos if np.linalg.norm(m.center - np.array([-1.0, 0.5, 0])) > 4.8]
    lysosomes = [pv.Sphere(radius=0.45, center=(rng.uniform(-6, 7), rng.uniform(-7, 6), rng.uniform(-5, -0.5)),
                           theta_resolution=10, phi_resolution=10) for _ in range(6)]
    ribosomes = [pv.Sphere(radius=0.09, center=p, theta_resolution=5, phi_resolution=5)
                 for p in rng.uniform([-8, -8, -6], [8, 8, -0.3], size=(120, 3))
                 if 4.0 < np.linalg.norm(p - np.array([-1.0, 0.5, 0])) and np.linalg.norm(p) < 9.3]
    centrioles = [tube_between((3.5, -4.0, -1.0), (4.3, -4.0, -1.0), 0.18),
                  tube_between((3.9, -4.5, -1.0), (3.9, -3.7, -1.0), 0.18)]
    parts = [
        ScenePart("cell_membrane", "Cell membrane", membrane, "#9fc6e8", opacity=0.25),
        ScenePart("nucleus", "Nucleus", nucleus, "#6a4fb0", opacity=0.85),
        ScenePart("nucleolus", "Nucleolus", nucleolus, "#3a2470"),
        ScenePart("rough_er", "Rough endoplasmic reticulum", merge(er), "#4f9fd0", opacity=0.7),
        ScenePart("golgi", "Golgi apparatus", merge(golgi), "#e8c040"),
        ScenePart("mitochondria", "Mitochondria", merge(mitos), "#f0a040"),
        ScenePart("lysosomes", "Lysosomes", merge(lysosomes), "#70b060"),
        ScenePart("ribosomes", "Ribosomes (enlarged)", merge(ribosomes), "#304070"),
        ScenePart("centrioles", "Centrioles", merge(centrioles), "#c05050"),
    ]
    return _scene(entry, parts, front="+z", view=(0.0, 20.0))


# ------------------------------------------------------------------ nucleus
def _on_sphere(count: int, radius: float, center=(0.0, 0.0, 0.0)) -> np.ndarray:
    """`count` points spread evenly over a sphere (a Fibonacci lattice)."""
    i = np.arange(count) + 0.5
    phi = np.arccos(1 - 2 * i / count)
    theta = np.pi * (1 + 5 ** 0.5) * i
    unit = np.column_stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)])
    return np.asarray(center, dtype=float) + radius * unit


def nucleus(entry=None) -> SceneModel:
    """The nucleus of an animal cell, ~6 µm across, cut open like the cell it
    is zoomed into from: the nuclear envelope's two membranes with pores
    through them, the nucleoplasm, chromatin threads, the nucleolus, and rough
    ER running on from the outer membrane with ribosomes on it."""
    rng = np.random.default_rng(11)
    r = 3.0
    outer = cut_half(pv.Sphere(radius=r, theta_resolution=64, phi_resolution=64))
    inner = cut_half(pv.Sphere(radius=r - 0.14, theta_resolution=64, phi_resolution=64))
    plasm = cut_half(pv.Sphere(radius=r - 0.2, theta_resolution=40, phi_resolution=40))
    # Pores: a ring on the envelope wherever the cut has left it (z < 0).
    pores = []
    for p in _on_sphere(90, r - 0.07):
        if p[2] > -0.25:
            continue
        normal = p / np.linalg.norm(p)
        ring = pv.ParametricTorus(ringradius=0.16, crosssectionradius=0.055, u_res=16, v_res=8, w_res=8)
        pores.append(_place(ring.rotate_y(90, inplace=False), p, normal))
    # Chromatin: loose threads wandering through the nucleoplasm (a random walk,
    # kept inside the envelope and below the cut).
    threads = []
    for _ in range(9):
        pos = rng.uniform([-1.6, -1.6, -1.8], [1.6, 1.6, -0.3])
        path = [pos.copy()]
        step = rng.normal(size=3)
        for _ in range(38):
            step = 0.75 * step + 0.45 * rng.normal(size=3)
            pos = pos + 0.16 * step / (np.linalg.norm(step) or 1.0)
            if np.linalg.norm(pos) > r - 0.5:
                pos *= (r - 0.5) / np.linalg.norm(pos)
            pos[2] = min(pos[2], -0.1)
            path.append(pos.copy())
        threads.append(pv.Spline(np.array(path), 160).tube(radius=0.045, n_sides=8))
    nucleolus_mesh = pv.Sphere(radius=0.85, center=(0.7, -0.5, -0.9), theta_resolution=28, phi_resolution=28)
    # Rough ER: sheets curving round the outside of the envelope, as they
    # continue from its outer membrane.
    er, ribosomes = [], []
    for k, rr in enumerate((r + 0.35, r + 0.7)):
        sheet = pv.Sphere(radius=rr, theta_resolution=48, phi_resolution=48,
                          start_theta=210 - 12 * k, end_theta=320 + 8 * k, start_phi=55, end_phi=150)
        er.append(cut_half(sheet))
        for p in _on_sphere(700, rr + 0.06):
            az = (np.degrees(np.arctan2(p[1], p[0])) + 360) % 360
            pol = np.degrees(np.arccos(np.clip(p[2] / (rr + 0.06), -1, 1)))
            if 210 - 12 * k <= az <= 320 + 8 * k and 55 <= pol <= 150 and p[2] < 0 and rng.random() < 0.55:
                ribosomes.append(p)
    ribo = glyphs(np.array(ribosomes), pv.Sphere(radius=0.06, theta_resolution=6, phi_resolution=6))
    parts = [
        ScenePart("outer_membrane", "Outer nuclear membrane", outer, "#8a6fd0", opacity=0.55),
        ScenePart("inner_membrane", "Inner nuclear membrane", inner, "#6a4fb0", opacity=0.7),
        ScenePart("nuclear_pores", "Nuclear pores", merge(pores), "#f2d27a"),
        ScenePart("nucleoplasm", "Nucleoplasm", plasm, "#b9a6ea", opacity=0.18),
        ScenePart("chromatin", "Chromatin (DNA + proteins)", merge(threads), "#e46aa8"),
        ScenePart("nucleolus", "Nucleolus", nucleolus_mesh, "#3a2470"),
        ScenePart("rough_er", "Rough endoplasmic reticulum", merge(er), "#4f9fd0", opacity=0.75),
        ScenePart("ribosomes", "Ribosomes (enlarged)", ribo, "#9cc0ff"),
    ]
    return _scene(entry, parts, front="+z", view=(0.0, 20.0))


GENERATORS = {
    "biology.cells.cardiac_muscle": cardiac_muscle,
    "biology.cells.cardiomyocyte": cardiomyocyte,
    "biology.cells.mitochondrion": mitochondrion,
    "biology.cells.animal_cell": animal_cell,
    "biology.cells.nucleus": nucleus,
}
