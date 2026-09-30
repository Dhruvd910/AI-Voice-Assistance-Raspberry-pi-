"""Builds every service once, in dependency order, and hands out the bundle.

    Settings -> Repository -> ModelRegistry -> KnowledgeGraph + KnowledgeBase
             -> AssetCache / LOD / MemoryManager -> ModelLoader -> VisualizationEngine
             -> ScaleManager -> ToolRegistry -> Agent

Nothing else constructs these, so tests, the CLI and the Qt app all run the
same wiring. `on_main` is how code on another thread reaches the engine: the
Qt app replaces it with a queued call onto the UI thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import Settings, load_settings

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    repo: Any
    registry: Any
    resolver: Any
    kg: Any
    kb: Any
    cache: Any
    lod: Any
    memory: Any
    loader: Any
    engine: Any
    scale: Any
    state: Any
    tools: Any
    agent: Any = None
    graph_file: Path | None = None
    scale_focus: dict[str, str] = field(default_factory=dict)
    interactive: bool = False           # True in the Qt app: transitions animate frame by frame
    on_main: Callable[[Callable[[], Any]], Any] = staticmethod(lambda fn: fn())


def build(settings: Settings | None = None, interactive: bool = False, rebuild_index: bool | None = None) -> Services:
    settings = settings or load_settings()
    # Imports here so `python -m app.main --help` stays instant.
    from agent.agent import Agent
    from agent.conversation_state import ConversationState
    from agent.tool_registry import ToolRegistry
    from agent.toolset import build_toolset
    from biology.scale_manager import ScaleManager
    from database.repository import Repository
    from knowledge.embeddings import make_embedder
    from knowledge.knowledge_graph import KnowledgeGraph
    from knowledge.rag import KnowledgeBase
    from models.cache import AssetCache, MemoryManager, ModelLODManager
    from models.loader import ModelLoader
    from models.provenance import write_attributions
    from models.registry import ModelRegistry
    from models.resolver import ModelResolver
    from visualization.engine import VisualizationEngine

    repo = Repository(settings.database_path)
    registry = ModelRegistry(settings.manifests_dir, settings.assets_dir)
    report = registry.load()
    for path, problems in report.rejected.items():
        log.warning("Manifest rejected: %s: %s", path, "; ".join(problems))
    registry.sync(repo)
    graph_file = settings.documents_dir / "graph.json"
    kg = KnowledgeGraph(repo)
    kb = KnowledgeBase(repo, make_embedder(settings.embedder), settings.rag_backend, settings.data_dir / "chroma")
    signature = _index_signature(settings, registry)
    if rebuild_index or (rebuild_index is None and repo.get_meta("index_signature") != signature):
        kg.rebuild(registry, graph_file)
        n = kb.build(settings.documents_dir)
        write_attributions(repo.provenance(), settings.attributions_path)
        from models.catalog import export_catalog
        export_catalog(registry, settings.data_dir / "liza_catalog.json")
        repo.set_meta("index_signature", signature)
        log.info("Index rebuilt: %d passages", n)

    budget = settings.render.memory_budget_mb << 20
    cache = AssetCache(budget)
    lod = ModelLODManager(settings.lod)
    memory = MemoryManager(cache, lod, limit_bytes=max(budget * 3, 1500 << 20))
    loader = ModelLoader(registry, cache, lod)
    engine = VisualizationEngine(settings, registry, loader, memory)
    scale = ScaleManager(kg, registry)
    tools = ToolRegistry()
    sv = Services(settings=settings, repo=repo, registry=registry, resolver=ModelResolver(registry), kg=kg, kb=kb,
                  cache=cache, lod=lod, memory=memory, loader=loader, engine=engine, scale=scale,
                  state=ConversationState(), tools=tools, graph_file=graph_file, interactive=interactive,
                  scale_focus={e.id: e.relationships.get("scale_down_focus") for e in registry.entries()
                               if e.relationships.get("scale_down_focus")})
    build_toolset(sv, tools)
    sv.agent = Agent(sv)
    return sv


def _index_signature(settings: Settings, registry) -> str:
    """Changes whenever a manifest or document changes, so the index is rebuilt then and only then."""
    import hashlib
    h = hashlib.sha256()
    paths = [e.manifest_path for e in registry.entries()] + sorted(settings.documents_dir.rglob("*"))
    for p in sorted(set(paths)):
        if p.is_file():
            h.update(str(p).encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()
