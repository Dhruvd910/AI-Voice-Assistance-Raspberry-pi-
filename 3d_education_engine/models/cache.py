"""Keeping memory in check on a Raspberry Pi 5 (8 GB, shared with the GPU).

ModelLODManager  picks the level of detail a model is loaded at.
AssetCache       keeps recently used SceneModels so "go back" is instant.
MemoryManager    watches the process and empties the cache under pressure.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from typing import Callable

from visualization.scene import SceneModel

log = logging.getLogger(__name__)


class ModelLODManager:
    """high = the processed source geometry; low = decimated for the Pi.

    "auto" (the default) means low on a Pi and high elsewhere. A model that
    only ships one level is loaded at that level whatever is asked for.
    """

    ORDER = ("high", "low")

    def __init__(self, preference: str = "high"):
        self.preference = preference if preference in self.ORDER else "high"
        self.pressure = False

    def choose(self, available: dict[str, str]) -> str | None:
        """The LOD key to load from {lod: path}, or None when there are none."""
        if not available:
            return None
        wanted = "low" if self.pressure else self.preference
        if wanted in available:
            return wanted
        for lod in (self.ORDER if wanted == "high" else reversed(self.ORDER)):
            if lod in available:
                return lod
        return next(iter(available))


class AssetCache:
    """LRU cache of loaded SceneModels, bounded by an estimate of their size."""

    def __init__(self, budget_bytes: int):
        self.budget_bytes = budget_bytes
        self._items: OrderedDict[tuple[str, str], SceneModel] = OrderedDict()
        self.pinned: set[str] = set()      # model ids on screen right now

    def get(self, model_id: str, lod: str) -> SceneModel | None:
        key = (model_id, lod)
        scene = self._items.get(key)
        if scene is not None:
            self._items.move_to_end(key)
        return scene

    def put(self, model_id: str, lod: str, scene: SceneModel) -> None:
        self._items[(model_id, lod)] = scene
        self._items.move_to_end((model_id, lod))
        self.trim()

    def drop(self, model_id: str) -> None:
        for key in [k for k in self._items if k[0] == model_id]:
            del self._items[key]

    def used_bytes(self) -> int:
        return sum(scene.memory_bytes() for scene in self._items.values())

    def trim(self, budget: int | None = None) -> list[str]:
        """Evict least-recently-used, never a pinned model. Returns evicted ids."""
        budget = self.budget_bytes if budget is None else budget
        evicted = []
        for key in list(self._items):
            if self.used_bytes() <= budget:
                break
            if key[0] in self.pinned:
                continue
            del self._items[key]
            evicted.append(key[0])
        if evicted:
            log.info("Asset cache evicted %s", evicted)
        return evicted

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, model_id: str) -> bool:
        return any(k[0] == model_id for k in self._items)


def process_rss_bytes() -> int:
    """Resident set size of this process, from /proc; 0 where unavailable."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


class MemoryManager:
    """Evicts cached models and asks for low LOD when the process grows.

    `limit_bytes` is for the whole process (VTK, Qt, Python and all), not just
    meshes: that is the number that decides whether the Pi starts swapping.
    """

    def __init__(self, cache: AssetCache, lod: ModelLODManager, limit_bytes: int,
                 rss: Callable[[], int] = process_rss_bytes):
        self.cache = cache
        self.lod = lod
        self.limit_bytes = limit_bytes
        self.rss = rss

    def check(self) -> dict:
        used = self.rss()
        over = bool(used) and used > self.limit_bytes
        evicted: list[str] = []
        if over:
            evicted = self.cache.trim(budget=0)
        self.lod.pressure = over
        return {"rss_bytes": used, "limit_bytes": self.limit_bytes, "over": over, "evicted": evicted}
