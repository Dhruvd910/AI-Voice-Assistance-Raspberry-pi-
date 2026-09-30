"""Educational RAG: explanations come from curated documents, not from the model's memory.

Visualization data (meshes, manifests) and educational text are kept apart.
The text lives in knowledge/documents/**/*.md, one concept per file:

    ---
    concept: biology.anatomy.heart.left_ventricle
    title: Left ventricle
    grades: 8-12
    difficulty: intermediate
    related: biology.anatomy.heart, biology.anatomy.heart.aorta
    ---
    ## Definition
    ...
    ## Function / ## Structure / ## Relationships / ## Misconceptions /
    ## Examples / ## Questions

Each "##" section becomes one retrievable chunk, tagged with the concept, so
the agent can ask for "the misconceptions about the left ventricle" as well
as search by meaning. Stored in SQLite by default; Chroma if configured.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from database.repository import Repository
from knowledge.embeddings import Embedder

log = logging.getLogger(__name__)

SECTIONS = ("definition", "function", "structure", "relationships", "misconceptions",
            "examples", "questions", "equations", "facts")


@dataclass
class Chunk:
    concept_id: str
    section: str
    text: str
    title: str
    doc_path: str
    grades: str = ""
    difficulty: str = ""
    score: float = 0.0


def parse_document(path: Path, root: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = raw
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        body = raw[m.end():]
    concept = meta.get("concept")
    if not concept:
        raise ValueError(f"{path}: frontmatter needs 'concept:'")
    title = meta.get("title", concept)
    chunks = []
    for block in re.split(r"(?m)^##\s+", body):
        block = block.strip()
        if not block:
            continue
        heading, _, text = block.partition("\n")
        section = heading.strip().lower()
        text = " ".join(text.split())
        if not text:
            continue
        chunks.append(Chunk(concept, section, text, title, str(path.relative_to(root)),
                            meta.get("grades", ""), meta.get("difficulty", "")))
    return chunks


class KnowledgeBase:
    def __init__(self, repo: Repository, embedder: Embedder, backend: str = "sqlite",
                 chroma_dir: Path | None = None):
        self.repo = repo
        self.embedder = embedder
        self.backend = backend
        self._chroma = None
        self._cache: tuple[list[dict], np.ndarray] | None = None
        if backend == "chroma":
            try:
                import chromadb                      # optional dependency
                client = chromadb.PersistentClient(path=str(chroma_dir or "data/chroma"))
                self._chroma = client.get_or_create_collection("education")
            except ImportError:
                log.warning("RAG_BACKEND=chroma but chromadb is not installed; using SQLite")
                self.backend = "sqlite"

    # ------------------------------------------------------------ indexing
    def build(self, documents_dir: Path) -> int:
        chunks: list[Chunk] = []
        for path in sorted(documents_dir.rglob("*.md")):
            chunks += parse_document(path, documents_dir)
        texts = [f"{c.title}. {c.section}. {c.text}" for c in chunks]
        vectors = self.embedder.embed(texts) if texts else np.zeros((0, 1), np.float32)
        rows = [{"doc_path": c.doc_path, "concept_id": c.concept_id, "section": c.section,
                 "text": c.text, "grades": c.grades, "difficulty": c.difficulty,
                 "embedder": self.embedder.name, "embedding": vectors[i].astype(np.float32).tobytes()}
                for i, c in enumerate(chunks)]
        self.repo.replace_chunks(rows)
        if self._chroma is not None and chunks:
            ids = [f"{c.concept_id}#{c.section}#{i}" for i, c in enumerate(chunks)]
            self._chroma.upsert(ids=ids, embeddings=vectors.tolist(), documents=[c.text for c in chunks],
                                metadatas=[{"concept_id": c.concept_id, "section": c.section,
                                            "title": c.title, "doc_path": c.doc_path} for c in chunks])
        self.repo.set_meta("rag_embedder", self.embedder.name)
        self._cache = None
        return len(chunks)

    def _matrix(self) -> tuple[list[dict], np.ndarray]:
        if self._cache is None:
            rows = [r for r in self.repo.chunks() if r["embedder"] == self.embedder.name]
            mat = (np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                   if rows else np.zeros((0, self.embedder.dim), np.float32))
            self._cache = (rows, mat)
        return self._cache

    # ------------------------------------------------------------ retrieval
    def search(self, query: str, k: int = 4, concepts: list[str] | None = None,
               sections: list[str] | None = None, min_score: float = 0.05,
               exclude_sections: tuple[str, ...] = ("questions",)) -> list[Chunk]:
        """Best passages for `query`. The "questions" sections are study
        questions, not answers, so they are left out unless asked for."""
        rows, mat = self._matrix()
        if not rows:
            return []
        qv = self.embedder.embed([query])[0]
        scores = mat @ qv
        wanted = set(concepts or [])
        for i, r in enumerate(rows):
            if r["section"] in exclude_sections:
                scores[i] = -1.0
                continue
            if wanted and (r["concept_id"] in wanted or any(r["concept_id"].startswith(c + ".") for c in wanted)):
                scores[i] += 0.15           # the part on screen is probably what they mean
            if sections and r["section"] in sections:
                scores[i] += 0.1
        order = np.argsort(-scores)[:k]
        return [Chunk(rows[i]["concept_id"], rows[i]["section"], rows[i]["text"],
                      rows[i]["concept_id"].rsplit(".", 1)[-1].replace("_", " "), rows[i]["doc_path"],
                      rows[i]["grades"] or "", rows[i]["difficulty"] or "", float(scores[i]))
                for i in order if scores[i] >= min_score]

    def concept_sections(self, concept_id: str) -> dict[str, str]:
        return {r["section"]: r["text"] for r in self.repo.chunks([concept_id])}


def offline_answer(question: str, chunks: list[Chunk], max_sentences: int = 3) -> str:
    """An answer built only from retrieved text, for when no LLM is configured.

    Picks the sentences that share the most words with the question, in their
    original order. Never adds anything that is not in the documents.
    """
    if not chunks:
        return "I don't have notes on that yet."
    from knowledge.embeddings import tokens
    q = set(tokens(question))
    why = question.strip().lower().startswith(("why", "kyon", "kyun"))
    sentences = []
    for rank, c in enumerate(chunks[:2]):
        for s in re.split(r"(?<=[.!?])\s+", c.text):
            if s.rstrip().endswith("?"):
                continue
            overlap = len(q & set(tokens(s))) + (1.5 if why and "because" in s.lower() else 0) - 0.5 * rank
            sentences.append((overlap, len(sentences), s))
    best = sorted(sentences, key=lambda x: (-x[0], x[1]))[:max_sentences]
    return " ".join(s for _, _, s in sorted(best, key=lambda x: x[1]))
