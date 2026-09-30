"""Shared setup for the command-line scripts: import path, settings, logging."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import load_settings, prepare_gl_environment  # noqa: E402

prepare_gl_environment()


def setup(verbose: bool = False):
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return load_settings()


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    widths = [min(w, 60) for w in widths]
    line = lambda r: "  ".join(str(c)[:w].ljust(w) for c, w in zip(r, widths))
    return "\n".join([line(headers), line(["-" * w for w in widths]), *[line(r) for r in rows]])
