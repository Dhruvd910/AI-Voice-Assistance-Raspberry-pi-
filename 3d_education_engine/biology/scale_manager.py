"""Multi-scale navigation: "go one level deeper", "go back".

    heart -> cardiac muscle -> cardiomyocyte -> mitochondrion -> ATP

The levels come from the knowledge graph's scale_down_to edges, so adding a
model with a scale relationship extends the chain without touching this file.
Keeps current / previous / next, and a history stack so "go back" returns to
wherever the student actually came from -- which is not always one level up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from knowledge.knowledge_graph import KnowledgeGraph
from models.registry import ModelRegistry


@dataclass
class ScaleState:
    current: str | None = None
    history: list[str] = field(default_factory=list)


class ScaleManager:
    def __init__(self, graph: KnowledgeGraph, registry: ModelRegistry):
        self.graph = graph
        self.registry = registry
        self.state = ScaleState()

    @property
    def current_scale(self) -> str | None:
        return self.state.current

    @property
    def previous_scale(self) -> str | None:
        return self.state.history[-1] if self.state.history else None

    @property
    def next_scale(self) -> str | None:
        return self._showable(self.graph.scale_down(self.state.current)) if self.state.current else None

    @property
    def parent_scale(self) -> str | None:
        return self._showable(self.graph.scale_up(self.state.current)) if self.state.current else None

    def _showable(self, node_id: str | None) -> str | None:
        return node_id if node_id and node_id in self.registry else None

    def visited(self, model_id: str) -> None:
        """Record that `model_id` is now on screen (however it got there)."""
        if model_id == self.state.current:
            return
        if self.state.current:
            self.state.history.append(self.state.current)
            del self.state.history[:-20]
        self.state.current = model_id

    def deeper(self) -> str | None:
        """The model one level down, or None when this is the smallest level shown."""
        return self.next_scale

    def up(self) -> str | None:
        return self.parent_scale

    def back(self) -> str | None:
        """Where "go back" should go, and forget it. None when there is no history."""
        if not self.state.history:
            return None
        target = self.state.history.pop()
        self.state.current = target
        return target

    def path_label(self) -> str:
        """'Human Heart › Cardiac Muscle Tissue › Cardiomyocyte' for the status bar."""
        cur = self.state.current
        if not cur:
            return ""
        chain = [cur]
        while (up := self.graph.scale_up(chain[0])) and up not in chain and len(chain) < 8:
            chain.insert(0, up)
        names = [self.registry.get(m).name if self.registry.get(m)
                 else (self.graph.node(m) or {}).get("label", m.rsplit(".", 1)[-1].replace("_", " "))
                 for m in chain]
        return " › ".join(names)

    def describe(self) -> dict:
        return {"current_scale": self.current_scale, "previous_scale": self.previous_scale,
                "next_scale": self.next_scale, "parent_scale": self.parent_scale}
