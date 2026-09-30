"""Cutting a model open with a plane, without touching its geometry.

The cut is a clipping plane on each part's MAPPER: the GPU discards what is on
the far side, so a cut is instant, can be moved every frame, and is undone by
removing the plane. The meshes are surfaces, so the cut shows the inside of
the chambers rather than a filled cross-section -- which is what "show me
inside the left ventricle" wants.
"""

from __future__ import annotations

from dataclasses import dataclass

import vtk

AXES = {"x": 0, "y": 1, "z": 2}
# How people say which way to cut, for a model whose front is -y and up is +z.
PLANE_WORDS = {
    "vertical": "x", "vertically": "x", "sagittal": "x", "left-right": "x", "in half": "x",
    "coronal": "y", "front-back": "y", "frontal": "y",
    "horizontal": "z", "horizontally": "z", "transverse": "z", "axial": "z",
}


@dataclass
class ClipSpec:
    axis: str            # x | y | z
    position: float      # 0..1 across the model's extent on that axis
    keep: str            # "positive" keeps the side the axis points to, "negative" the other
    origin: tuple[float, float, float]


def plane_for(axis: str, position: float, keep: str, bounds) -> ClipSpec:
    if axis not in AXES:
        raise ValueError(f"plane must be one of {sorted(AXES)}, got {axis!r}")
    if not 0.0 <= position <= 1.0:
        raise ValueError("position must be between 0 and 1 (fraction across the model)")
    if keep not in {"positive", "negative"}:
        raise ValueError("keep must be 'positive' or 'negative'")
    i = AXES[axis]
    lo, hi = bounds[2 * i], bounds[2 * i + 1]
    origin = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2]
    origin[i] = lo + (hi - lo) * position
    return ClipSpec(axis, position, keep, tuple(origin))


def vtk_plane(spec: ClipSpec) -> vtk.vtkPlane:
    normal = [0.0, 0.0, 0.0]
    # vtk keeps what is on the side the normal points TO.
    normal[AXES[spec.axis]] = 1.0 if spec.keep == "positive" else -1.0
    plane = vtk.vtkPlane()
    plane.SetOrigin(*spec.origin)
    plane.SetNormal(*normal)
    return plane


def apply_to_actor(actor, spec: ClipSpec | None) -> None:
    mapper = actor.GetMapper()
    mapper.RemoveAllClippingPlanes()
    if spec is not None:
        mapper.AddClippingPlane(vtk_plane(spec))
