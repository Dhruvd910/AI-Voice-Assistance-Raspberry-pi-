"""Opacity, and the words people use for it."""

from __future__ import annotations

# "Make it transparent" should still leave the shape readable.
DEFAULT_TRANSPARENT = 0.3

WORDS = {
    "transparent": DEFAULT_TRANSPARENT, "see-through": DEFAULT_TRANSPARENT,
    "see through": DEFAULT_TRANSPARENT, "glass": 0.2, "ghost": 0.12,
    "semi-transparent": 0.5, "half transparent": 0.5, "translucent": 0.5,
    "opaque": 1.0, "solid": 1.0, "normal": 1.0,
}


def clamp_opacity(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError("transparency value is an opacity between 0 (invisible) and 1 (solid)")
    return float(value)
