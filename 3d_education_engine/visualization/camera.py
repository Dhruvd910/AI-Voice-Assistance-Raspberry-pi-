"""Camera moves, expressed the way a student asks for them.

rotate(x, y, z) turns the MODEL in front of the viewer: y spins it left/right
about the vertical axis, x tips it towards/away, z rolls it. Implemented by
orbiting the camera the opposite way, which looks identical and never
disturbs the geometry that clipping and measuring depend on.
"""

from __future__ import annotations

import numpy as np

FRONT_VECTORS = {
    # (direction the camera LOOKS FROM, relative to the model), view-up
    "-y": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),   # BodyParts3D: anterior view
    "+x": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),    # BodyParts3D: from the left side, face to the left
    "-x": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),   # from the right side, face to the right
    "+z": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),    # generated models: looking down on x-y
    "iso": ((1.0, -1.0, 0.8), (0.0, 0.0, 1.0)),
}


class CameraController:
    def __init__(self, plotter):
        self.plotter = plotter

    @property
    def camera(self):
        return self.plotter.camera

    def home(self, bounds, front: str = "-y", azimuth: float = 0.0, elevation: float = 0.0) -> None:
        direction, up = FRONT_VECTORS.get(front, FRONT_VECTORS["iso"])
        center = np.array([(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2])
        cam = self.camera
        cam.SetFocalPoint(*center)
        cam.SetPosition(*(center + np.asarray(direction) * 10.0))
        cam.SetViewUp(*up)
        self.plotter.reset_camera(bounds=bounds)
        if azimuth:
            cam.Azimuth(azimuth)
        if elevation:
            cam.Elevation(elevation)
            cam.OrthogonalizeViewUp()
        self.plotter.renderer.ResetCameraClippingRange()

    def rotate(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        cam = self.camera
        if y:
            cam.Azimuth(-y)
        if x:
            cam.Elevation(-x)
            cam.OrthogonalizeViewUp()
        if z:
            cam.Roll(z)
        self.plotter.renderer.ResetCameraClippingRange()

    def zoom(self, factor: float) -> None:
        """factor > 1 moves in, < 1 moves out."""
        self.camera.Zoom(float(np.clip(factor, 0.05, 20.0)))
        self.plotter.renderer.ResetCameraClippingRange()

    def focus(self, bounds, fill: float = 0.8) -> None:
        """Centre on `bounds` and zoom so it fills roughly `fill` of the view."""
        cam = self.camera
        center = np.array([(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2])
        direction = np.array(cam.GetPosition()) - np.array(cam.GetFocalPoint())
        direction /= np.linalg.norm(direction) or 1.0
        cam.SetFocalPoint(*center)
        cam.SetPosition(*(center + direction * 10.0))
        self.plotter.reset_camera(bounds=bounds)
        cam.Zoom(fill / 0.8)
        self.plotter.renderer.ResetCameraClippingRange()

    def set(self, position, target, up=None) -> None:
        cam = self.camera
        cam.SetPosition(*position)
        cam.SetFocalPoint(*target)
        if up is not None:
            cam.SetViewUp(*up)
        cam.OrthogonalizeViewUp()
        self.plotter.renderer.ResetCameraClippingRange()

    def dolly_towards(self, point, fraction: float) -> None:
        """Move the camera `fraction` of the way to `point` (transitions)."""
        cam = self.camera
        pos, focal = np.array(cam.GetPosition()), np.array(cam.GetFocalPoint())
        target = np.asarray(point, dtype=float)
        cam.SetFocalPoint(*(focal + (target - focal) * fraction))
        cam.SetPosition(*(pos + (target - pos) * fraction))
        self.plotter.renderer.ResetCameraClippingRange()

    def state(self) -> dict:
        cam = self.camera
        return {"position": [round(v, 3) for v in cam.GetPosition()],
                "target": [round(v, 3) for v in cam.GetFocalPoint()],
                "up": [round(v, 3) for v in cam.GetViewUp()],
                "view_angle": round(cam.GetViewAngle(), 2)}
