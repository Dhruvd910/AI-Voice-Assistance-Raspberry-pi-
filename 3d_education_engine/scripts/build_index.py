"""Rebuild everything derived from the manifests and documents.

    python scripts/build_index.py

Validates and registers every manifest in SQLite, rebuilds the knowledge
graph and the RAG index, and regenerates ATTRIBUTIONS.md. Safe to run at any
time: the database is an index, the manifests and documents are the truth.
"""

from __future__ import annotations

import argparse
import sys

from _common import setup


def rebuild(settings, quiet: bool = False) -> int:
    from database.repository import Repository
    from knowledge.embeddings import make_embedder
    from knowledge.knowledge_graph import KnowledgeGraph
    from knowledge.rag import KnowledgeBase
    from models.provenance import write_attributions
    from models.registry import ModelRegistry

    repo = Repository(settings.database_path)
    registry = ModelRegistry(settings.manifests_dir, settings.assets_dir)
    report = registry.load()
    removed = registry.sync(repo)
    nodes, edges = KnowledgeGraph(repo).rebuild(registry, settings.documents_dir / "graph.json")
    kb = KnowledgeBase(repo, make_embedder(settings.embedder), settings.rag_backend, settings.data_dir / "chroma")
    passages = kb.build(settings.documents_dir)
    write_attributions(repo.provenance(), settings.attributions_path)
    from models.catalog import export_catalog
    export_catalog(registry, settings.data_dir / "liza_catalog.json")
    repo.set_meta("index_signature", "rebuilt-by-script")
    if not quiet:
        print(f"models registered: {len(report.loaded)}  (removed stale: {len(removed)})")
        print(f"knowledge graph:   {nodes} nodes, {edges} edges")
        print(f"RAG passages:      {passages}  (embedder {kb.embedder.name}, backend {kb.backend})")
        print(f"attributions:      {settings.attributions_path}")
        for path, problems in report.rejected.items():
            print(f"REJECTED {path}:\n  - " + "\n  - ".join(problems))
    return 1 if report.rejected else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return rebuild(setup(args.verbose))


if __name__ == "__main__":
    sys.exit(main())
