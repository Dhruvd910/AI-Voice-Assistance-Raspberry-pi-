"""Relationships between concepts, which the agent uses to decide what to show next.

    heart --has_part--> left ventricle
    heart --scale_down_to--> cardiac muscle --scale_down_to--> cardiomyocyte
    cardiomyocyte --contains--> mitochondria --produces--> ATP

Built from two places, merged:
* knowledge/documents/graph.json -- curated concepts and relations, including
  ones no model shows (the human body, the lungs, oxygen...);
* every manifest -- its parts become has_part edges, its `relationships`
  become part_of / scale_down_to / contains edges. So a new model is in the
  graph the moment it is registered, without anyone editing graph.json.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from database.repository import Repository
from models.registry import ModelRegistry

RELATIONS = {
    "has_part", "part_of", "contains", "inside", "connected_to", "scale_down_to", "scale_up_to",
    "produces", "used_by", "pumps_to", "receives_from", "is_a", "related_to", "made_of", "example_of",
}
INVERSE = {"has_part": "part_of", "part_of": "has_part", "contains": "inside", "inside": "contains",
           "scale_down_to": "scale_up_to", "scale_up_to": "scale_down_to"}


class KnowledgeGraph:
    def __init__(self, repo: Repository):
        self.repo = repo

    # ------------------------------------------------------------ building
    @staticmethod
    def collect(registry: ModelRegistry, graph_file: Path | None) -> tuple[list[dict], list[tuple[str, str, str]]]:
        nodes: dict[str, dict] = {}
        edges: set[tuple[str, str, str]] = set()

        def node(nid: str, label: str, kind: str, model_id: str | None = None) -> None:
            if nid not in nodes:
                nodes[nid] = {"id": nid, "label": label, "kind": kind, "model_id": model_id}
            elif model_id and not nodes[nid].get("model_id"):
                nodes[nid]["model_id"] = model_id

        def edge(src: str, rel: str, dst: str) -> None:
            if rel not in RELATIONS:
                raise ValueError(f"unknown relation {rel!r} ({src} -> {dst})")
            edges.add((src, rel, dst))
            if rel in INVERSE:
                edges.add((dst, INVERSE[rel], src))

        if graph_file and graph_file.is_file():
            data = json.loads(graph_file.read_text(encoding="utf-8"))
            for n in data.get("nodes", []):
                node(n["id"], n["label"], n["kind"], n.get("model_id"))
            for src, rel, dst in data.get("edges", []):
                edge(src, rel, dst)

        for entry in registry.entries():
            m = entry.manifest
            scale = m.get("educational", {}).get("scale_level") or m["subject"]
            node(entry.id, entry.name, scale, entry.id)
            for part_id, part in entry.parts.items():
                pid = f"{entry.id}.{part_id}"
                node(pid, part["name"], "part", entry.id if part.get("available", True) else None)
                edge(entry.id, "has_part", pid)
            rel = entry.relationships
            if rel.get("parent"):
                node(rel["parent"], rel["parent"].rsplit(".", 1)[-1].replace("_", " ").title(), "concept")
                edge(entry.id, "part_of", rel["parent"])
            for key in ("scale_down_to", "scale_up_to"):
                if rel.get(key):
                    node(rel[key], rel[key].rsplit(".", 1)[-1].replace("_", " ").title(), "concept")
                    edge(entry.id, key, rel[key])
            for inner in rel.get("inside", []):
                node(inner, inner.rsplit(".", 1)[-1].replace("_", " ").title(), "concept")
                edge(entry.id, "contains", inner)
            for other in rel.get("related", []):
                node(other, other.rsplit(".", 1)[-1].replace("_", " ").title(), "concept")
                edge(entry.id, "related_to", other)
        # Every endpoint needs a node (the edges table has foreign keys).
        for src, _rel, dst in edges:
            for nid in (src, dst):
                node(nid, nid.rsplit(".", 1)[-1].replace("_", " ").capitalize(), "concept")
        # A node shows a model only if that model is registered.
        for n in nodes.values():
            if n["model_id"] and n["model_id"] not in registry:
                n["model_id"] = None
        return list(nodes.values()), sorted(edges)

    def rebuild(self, registry: ModelRegistry, graph_file: Path | None) -> tuple[int, int]:
        nodes, edges = self.collect(registry, graph_file)
        self.repo.replace_graph(nodes, edges)
        return len(nodes), len(edges)

    # ------------------------------------------------------------ queries
    def node(self, node_id: str) -> dict | None:
        return self.repo.kg_node(node_id)

    def neighbours(self, node_id: str, relation: str | None = None) -> list[dict]:
        out = []
        for rel, dst in self.repo.kg_out(node_id, relation):
            n = self.repo.kg_node(dst) or {"id": dst, "label": dst, "kind": "concept", "model_id": None}
            out.append({"relation": rel, **n})
        return out

    def parts_of(self, node_id: str) -> list[dict]:
        return self.neighbours(node_id, "has_part")

    def scale_down(self, node_id: str) -> str | None:
        nxt = self.repo.kg_out(node_id, "scale_down_to")
        return nxt[0][1] if nxt else None

    def scale_up(self, node_id: str) -> str | None:
        prev = self.repo.kg_out(node_id, "scale_up_to")
        return prev[0][1] if prev else None

    def scale_chain(self, start: str, limit: int = 12) -> list[str]:
        """start -> ... following scale_down_to, e.g. heart -> ... -> ATP."""
        chain, seen = [start], {start}
        while len(chain) < limit:
            nxt = self.scale_down(chain[-1])
            if not nxt or nxt in seen:
                break
            chain.append(nxt)
            seen.add(nxt)
        return chain

    def path(self, src: str, dst: str, max_depth: int = 6) -> list[tuple[str, str, str]] | None:
        """Shortest chain of relations from src to dst (breadth first)."""
        queue: deque[tuple[str, list]] = deque([(src, [])])
        seen = {src}
        while queue:
            current, trail = queue.popleft()
            if current == dst:
                return trail
            if len(trail) >= max_depth:
                continue
            for rel, nxt in self.repo.kg_out(current):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append((nxt, trail + [(current, rel, nxt)]))
        return None

    def describe(self, node_id: str) -> str:
        """A few plain sentences of what the graph knows, for the agent's context."""
        n = self.node(node_id)
        if n is None:
            return ""
        lines = [f"{n['label']} ({n['kind']})."]
        groups: dict[str, list[str]] = {}
        for rel, dst in self.repo.kg_out(node_id):
            label = (self.repo.kg_node(dst) or {}).get("label", dst)
            groups.setdefault(rel, []).append(label)
        for rel, labels in sorted(groups.items()):
            lines.append(f"{rel.replace('_', ' ')}: {', '.join(sorted(set(labels))[:12])}.")
        return " ".join(lines)
