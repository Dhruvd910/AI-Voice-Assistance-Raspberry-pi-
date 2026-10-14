"""Lenses and spherical mirrors: ray diagrams that form an image.

Coordinates in centimetres: the principal axis is x, up is z, the lens (or the
mirror's pole) sits at x = 0 and the object stands on the axis to the left.
Sign convention as in the NCERT books (New Cartesian): distances measured from
the optical centre or pole, positive along the incoming light (to the right),
so an object distance u is negative.

The three rays every textbook draws come from the top of the object: parallel
to the axis, through the centre (lens) or to the pole (mirror), and through (or
towards) the focus. Each one, once bent, heads for the top of the image -- or,
for a virtual image, away from it -- so the diagram is drawn from the image the
formula gives, and the dotted backward lines meet where the image is.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

from physics.base import Parameter, Simulation, arrow, beads, fmt, point_along, polyline_tube
from visualization.scene import ScenePart

FAR = 140.0          # how far a ray is drawn when nothing stops it, cm
RAY_COLOURS = ("#ffd84d", "#ff9f43", "#6fd08c")


class _SphericalOptic(Simulation):
    mirror = False
    sign = 1             # +1 converging (convex lens, concave mirror), -1 diverging
    loop = True
    duration = 3.0
    PULSES = 3
    equations: list[str] = []

    PARAMETERS = [Parameter("focal_length", 15.0, 5.0, 30.0, "cm", "distance from the centre to the focus F", 1.0),
                  Parameter("object_distance", 35.0, 3.0, 60.0, "cm", "distance of the object from the centre", 1.0),
                  Parameter("object_height", 5.0, 1.0, 10.0, "cm", "height of the object", 0.5)]
    ALIASES = {"f": "focal_length", "focal": "focal_length", "focus": "focal_length", "u": "object_distance",
               "distance": "object_distance", "object": "object_distance", "h": "object_height", "height": "object_height"}

    # ------------------------------------------------------------ the optics
    def prepare(self):
        u = -self.p("object_distance")
        f = self.p("focal_length")
        # Cartesian focal length: + for a convex lens and a convex mirror.
        self.f = f * self.sign if not self.mirror else -f * self.sign
        h = self.p("object_height")
        self.u, self.h = u, h
        inv = (1 / self.f + 1 / u) if not self.mirror else (1 / self.f - 1 / u)
        if abs(inv) < 1e-9:
            self.v = None                                  # at the focus: image at infinity
            self.m = None
        else:
            self.v = 1 / inv
            self.m = self.v / u if not self.mirror else -self.v / u
        self.rays = self._trace()

    def real(self):
        if self.v is None:
            return False
        return self.v > 0 if not self.mirror else self.v < 0

    def image_top(self):
        return np.array([self.v, 0.0, self.m * self.h]) if self.v is not None else None

    def surface_x(self, z):
        """Where a ray at height z meets the optic: a lens is thin (x = 0); a
        mirror bends back by its sag, z² / 2R with R = 2f."""
        if not self.mirror:
            return 0.0
        R = 2 * self.f
        return z * z / (2 * R)

    def _trace(self):
        """[(path points, virtual back-extension or None)] for the three rays."""
        P = np.array([self.u, 0.0, self.h])
        hits = [self.h, 0.0]                               # parallel ray; ray to the centre / pole
        F = np.array([self.f if self.mirror else -self.f, 0.0, 0.0])    # the focus the third ray aims at
        dx = F[0] - P[0]
        if abs(dx) > 1e-6 and abs(self.u - F[0]) > 1e-6:
            hits.append(self.h + (0 - P[0]) * (F[2] - P[2]) / dx)
        rays = []
        I = self.image_top()
        f = abs(self.f)
        # How far the bent rays are drawn: past a real image, else a little way
        # beyond the focus -- not off to the horizon, or the whole diagram is
        # framed so wide that the lens is a speck.
        if self.real() and abs(self.v) < FAR:
            reach_x = abs(self.v) * 1.3 + 5
        else:
            reach_x = max(2.2 * f, 0.7 * abs(self.u), 20.0)
        for z in hits:
            L = np.array([self.surface_x(z), 0.0, z])
            if I is None:                                  # parallel out, like the centre ray
                d = (np.array([0.0, 0.0, 0.0]) - P)
                d = d / np.linalg.norm(d)
                if self.mirror:
                    d[0] = -d[0]
                back = None
            else:
                d = (I - L) if self.real() else (L - I)
                d = d / (np.linalg.norm(d) or 1.0)
                back = None if self.real() else np.array([L, I])
            run = reach_x / max(abs(d[0]), 0.15)
            out = L + d * min(run, FAR)
            rays.append((np.array([P, L, out]), back))
        return rays

    # ------------------------------------------------------------ the scene
    def _optic_mesh(self):
        H = max(12.0, self.p("object_height") * 2.2)
        if not self.mirror:
            # A lens: fat in the middle (convex) or thin in the middle (concave).
            zs = np.linspace(-H, H, 40)
            bulge = 1.6 * (1 - (zs / H) ** 2) if self.sign > 0 else 0.6 + 1.2 * (zs / H) ** 2
            outline = np.vstack([np.column_stack([bulge, np.zeros_like(zs), zs]),
                                 np.column_stack([-bulge[::-1], np.zeros_like(zs), zs[::-1]])])
            poly = pv.lines_from_points(np.vstack([outline, outline[:1]]))
            return poly.tube(radius=0.35, n_sides=8).triangulate()
        zs = np.linspace(-H, H, 60)
        arc = np.column_stack([[self.surface_x(z) for z in zs], np.zeros_like(zs), zs])
        return polyline_tube(arc, 0.6, 10)

    def pulses(self, t):
        pts = []
        for path, _back in self.rays:
            for k in range(self.PULSES):
                pts.append(point_along(path, ((t / self.duration) + k / self.PULSES) % 1.0))
        return beads(pts, 0.7, 8)

    def build_parts(self):
        u, h = self.u, self.h
        f_at = abs(self.f)
        parts = {
            "axis": ScenePart("axis", "Principal axis", polyline_tube([(u - 8, 0, 0), (max(2.2 * f_at, abs(self.v or 0) * 1.3 + 5 if abs(self.v or 0) < FAR else 0, 20.0), 0, 0)], 0.15), "#8899aa", opacity=0.7),
            "optic": ScenePart("optic", self.optic_name, self._optic_mesh(), "#9fd8ff", opacity=0.75),
            "object": ScenePart("object", "Object", arrow((u, 0, 0), (0, 0, h), 0.25, 0.8), "#ff6b4a"),
        }
        foci = [(-f_at, 0, 0), (f_at, 0, 0)] if not self.mirror else [(self.f, 0, 0)]
        centres = [] if not self.mirror else [(2 * self.f, 0, 0)]
        parts["foci"] = ScenePart("foci", "Focus F", pv.merge([pv.Sphere(radius=0.8, center=c) for c in foci]), "#ffffff")
        if centres:
            parts["centre"] = ScenePart("centre", "Centre of curvature C", pv.Sphere(radius=0.8, center=centres[0]), "#c080ff")
        else:
            parts["twice_f"] = ScenePart("twice_f", "2F", pv.merge([pv.Sphere(radius=0.6, center=(s * 2 * f_at, 0, 0)) for s in (-1, 1)]), "#c080ff")
        dashes = []
        for i, (path, back) in enumerate(self.rays):
            parts[f"ray{i + 1}"] = ScenePart(f"ray{i + 1}", ("Parallel ray", "Ray through centre" if not self.mirror else "Ray to pole",
                                                             "Ray through F")[i], polyline_tube(path, 0.18, 6), RAY_COLOURS[i])
            if back is not None:
                # The dotted line of the book: dashes back to the virtual image.
                dashes += [polyline_tube([point_along(back, a), point_along(back, a + 0.06)], 0.12, 6)
                           for a in np.arange(0, 0.94, 0.12)]
        if dashes:
            parts["traced_back"] = ScenePart("traced_back", "Traced back to the image", pv.merge(dashes), "#d0d0d0", opacity=0.6)
        I = self.image_top()
        if I is not None and abs(I[0]) < FAR and abs(I[2]) < 60:
            parts["image"] = ScenePart("image", "Image (real)" if self.real() else "Image (virtual)",
                                       arrow((I[0], 0, 0), (0, 0, I[2]), 0.25, 0.8), "#4fb0ff",
                                       opacity=1.0 if self.real() else 0.55)
        parts["light"] = ScenePart("light", "Light", self.pulses(0.0), "#ffffff")
        return parts

    def apply(self, engine):
        engine.update_part_mesh("light", self.pulses(self.t))

    def measurements(self):
        if self.v is None:
            return {"image": "at infinity: the object is at the focus, so the rays come out parallel",
                    "object_distance_u": fmt(self.u, "cm", 1), "focal_length_f": fmt(self.f, "cm", 1)}
        m = self.m
        size = "same size" if abs(abs(m) - 1) < 0.02 else "magnified" if abs(m) > 1 else "diminished"
        # What the image is like first: it is what the chapter asks about.
        out = {"image": f"{'real' if self.real() else 'virtual'}, {'inverted' if m < 0 else 'erect'}, {size}",
               "image_distance_v": fmt(self.v, "cm", 1), "magnification_m": fmt(m, "", 2),
               "object_distance_u": fmt(self.u, "cm", 1), "focal_length_f": fmt(self.f, "cm", 1)}
        if not self.mirror:
            out["power"] = fmt(100 / self.f, "D", 2)
        return out


class ConvexLens(_SphericalOptic):
    id = "convex_lens"
    title = "Image formed by a convex lens"
    optic_name = "Convex lens"
    sign = 1
    disclaimer = "Thin lens, rays near the axis. Move the object to see where the image forms and how it changes."
    equations = ["lens formula: 1/v − 1/u = 1/f", "magnification m = v / u = h′ / h", "power P = 1 / f (f in metres), in dioptres"]


class ConcaveLens(_SphericalOptic):
    id = "concave_lens"
    title = "Image formed by a concave lens"
    optic_name = "Concave lens"
    sign = -1
    disclaimer = "Thin lens. A concave lens always gives a virtual, erect, smaller image on the object's side."
    equations = ["lens formula: 1/v − 1/u = 1/f  (f negative)", "magnification m = v / u", "power P = 1 / f, in dioptres"]


class ConcaveMirror(_SphericalOptic):
    id = "concave_mirror"
    title = "Image formed by a concave mirror"
    optic_name = "Concave mirror"
    mirror = True
    sign = 1
    disclaimer = "Rays near the axis. Inside F the image becomes virtual and erect, behind the mirror."
    equations = ["mirror formula: 1/v + 1/u = 1/f", "magnification m = −v / u", "f = R / 2"]


class ConvexMirror(_SphericalOptic):
    id = "convex_mirror"
    title = "Image formed by a convex mirror"
    optic_name = "Convex mirror"
    mirror = True
    sign = -1
    disclaimer = "A convex mirror always gives a virtual, erect, smaller image behind it: the rear-view mirror of a car."
    equations = ["mirror formula: 1/v + 1/u = 1/f  (f positive)", "magnification m = −v / u", "f = R / 2"]
