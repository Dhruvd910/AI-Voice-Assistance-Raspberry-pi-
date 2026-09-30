"""Drawing the eye to a part: tint it, light it, and fade everything else."""

from __future__ import annotations

DIMMED_OPACITY = 0.18


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    h = color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def tint(color: str, amount: float = 0.3, towards: str = "#ffffff") -> tuple[float, float, float]:
    """Mix `color` towards `towards`. Lightening rather than recolouring:
    mixing towards a gold highlight turned the blue (deoxygenated) chambers
    grey, and the colour is what tells the student which side they are on."""
    a, b = hex_to_rgb(color), hex_to_rgb(towards)
    return tuple((1 - amount) * x + amount * y for x, y in zip(a, b))


def style_highlighted(actor, base_color: str) -> None:
    prop = actor.GetProperty()
    prop.SetColor(*tint(base_color))
    prop.SetAmbient(0.3)
    prop.SetOpacity(1.0)


def style_normal(actor, base_color: str, opacity: float) -> None:
    prop = actor.GetProperty()
    prop.SetColor(*hex_to_rgb(base_color))
    prop.SetAmbient(0.0)
    prop.SetOpacity(opacity)


def style_dimmed(actor, base_color: str) -> None:
    prop = actor.GetProperty()
    prop.SetColor(*hex_to_rgb(base_color))
    prop.SetAmbient(0.0)
    prop.SetOpacity(DIMMED_OPACITY)
