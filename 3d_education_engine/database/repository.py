"""Thin SQLite access layer. Every SQL statement in the project lives here."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def normalise(text: str) -> str:
    return " ".join(text.lower().replace("_", " ").replace("-", " ").split())


class Repository:
    """One connection, shared, guarded by a lock.

    SQLite is fine being touched from the UI thread and the agent thread as
    long as they take turns; check_same_thread=False plus the lock gives that.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------ models
    def upsert_model(self, manifest: dict, manifest_path: str) -> None:
        """Replace everything the index holds about one model, in one go."""
        mid = manifest["id"]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        edu = manifest.get("educational", {})
        with self.transaction() as db:
            db.execute("DELETE FROM models WHERE id = ?", (mid,))
            db.execute(
                "INSERT INTO models (id, name, domain, subject, kind, manifest_path, "
                "manifest_json, disclaimer, scale_level, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, manifest["name"], manifest["domain"], manifest["subject"],
                 manifest["kind"], manifest_path, json.dumps(manifest, ensure_ascii=False),
                 edu.get("disclaimer"), edu.get("scale_level"), now))
            aliases = {normalise(manifest["name"]), normalise(mid.rsplit(".", 1)[-1])}
            aliases.update(normalise(a) for a in manifest.get("aliases", []))
            aliases.update(normalise(t) for t in edu.get("topics", []) if t)
            db.executemany("INSERT OR IGNORE INTO model_aliases VALUES (?, ?)",
                           [(a, mid) for a in aliases if a])
            for part_id, part in manifest.get("parts", {}).items():
                db.execute("INSERT INTO model_parts VALUES (?,?,?,?,?)",
                           (mid, part_id, part["name"], int(part.get("available", True)),
                            part.get("note")))
                names = {normalise(part["name"]), normalise(part_id)}
                names.update(normalise(a) for a in part.get("aliases", []))
                db.executemany("INSERT OR IGNORE INTO part_aliases VALUES (?,?,?)",
                               [(mid, part_id, a) for a in names if a])
            for group_id, members in manifest.get("groups", {}).items():
                db.executemany("INSERT OR IGNORE INTO part_groups VALUES (?,?,?)",
                               [(mid, group_id, p) for p in members])
            db.executemany("INSERT OR IGNORE INTO model_capabilities VALUES (?,?)",
                           [(mid, c) for c in manifest.get("capabilities", [])])
            for src in [manifest["source"], *manifest.get("additional_sources", [])]:
                db.execute(
                    "INSERT OR REPLACE INTO provenance VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (mid, src["provider"], src["url"], src["source_id"], src["creator"],
                     src["license"], src["license_url"], src["attribution"],
                     src["download_date"], src["modification_status"]))

    def delete_models_not_in(self, keep: set[str]) -> list[str]:
        gone = [r["id"] for r in self._query("SELECT id FROM models") if r["id"] not in keep]
        with self.transaction() as db:
            db.executemany("DELETE FROM models WHERE id = ?", [(m,) for m in gone])
        return gone

    def record_asset_file(self, model_id: str, role: str, path: str,
                          sha256: str | None, size: int | None) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO asset_files VALUES (?,?,?,?,?)",
                       (model_id, role, path, sha256, size))

    def model_ids(self) -> list[str]:
        return [r["id"] for r in self._query("SELECT id FROM models ORDER BY id")]

    def manifest(self, model_id: str) -> dict | None:
        rows = self._query("SELECT manifest_json FROM models WHERE id = ?", (model_id,))
        return json.loads(rows[0]["manifest_json"]) if rows else None

    def models_summary(self) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT id, name, domain, subject, kind, disclaimer, scale_level FROM models ORDER BY domain, id")]

    def aliases(self) -> list[tuple[str, str]]:
        return [(r["alias"], r["model_id"]) for r in self._query("SELECT alias, model_id FROM model_aliases")]

    def part_aliases(self, model_id: str) -> list[tuple[str, str]]:
        return [(r["alias"], r["part_id"]) for r in self._query(
            "SELECT alias, part_id FROM part_aliases WHERE model_id = ?", (model_id,))]

    def provenance(self, model_id: str | None = None) -> list[dict]:
        if model_id:
            rows = self._query("SELECT * FROM provenance WHERE model_id = ? ORDER BY source_name", (model_id,))
        else:
            rows = self._query("SELECT * FROM provenance ORDER BY model_id, source_name")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ knowledge graph
    def replace_graph(self, nodes: list[dict], edges: list[tuple[str, str, str]]) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM kg_edges")
            db.execute("DELETE FROM kg_nodes")
            db.executemany("INSERT INTO kg_nodes VALUES (?,?,?,?)",
                           [(n["id"], n["label"], n["kind"], n.get("model_id")) for n in nodes])
            db.executemany("INSERT OR IGNORE INTO kg_edges VALUES (?,?,?)", edges)

    def kg_node(self, node_id: str) -> dict | None:
        rows = self._query("SELECT * FROM kg_nodes WHERE id = ?", (node_id,))
        return dict(rows[0]) if rows else None

    def kg_nodes(self) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM kg_nodes ORDER BY id")]

    def kg_out(self, src: str, relation: str | None = None) -> list[tuple[str, str]]:
        if relation:
            rows = self._query("SELECT relation, dst FROM kg_edges WHERE src = ? AND relation = ?", (src, relation))
        else:
            rows = self._query("SELECT relation, dst FROM kg_edges WHERE src = ?", (src,))
        return [(r["relation"], r["dst"]) for r in rows]

    def kg_in(self, dst: str, relation: str | None = None) -> list[tuple[str, str]]:
        if relation:
            rows = self._query("SELECT src, relation FROM kg_edges WHERE dst = ? AND relation = ?", (dst, relation))
        else:
            rows = self._query("SELECT src, relation FROM kg_edges WHERE dst = ?", (dst,))
        return [(r["src"], r["relation"]) for r in rows]

    # ------------------------------------------------------------ RAG
    def replace_chunks(self, chunks: list[dict]) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM rag_chunks")
            db.executemany(
                "INSERT INTO rag_chunks (doc_path, concept_id, section, text, grades, difficulty, "
                "embedder, embedding) VALUES (?,?,?,?,?,?,?,?)",
                [(c["doc_path"], c["concept_id"], c["section"], c["text"], c.get("grades"),
                  c.get("difficulty"), c["embedder"], c["embedding"]) for c in chunks])

    def chunks(self, concept_ids: Iterable[str] | None = None) -> list[dict]:
        if concept_ids:
            ids = list(concept_ids)
            marks = ",".join("?" * len(ids))
            rows = self._query(f"SELECT * FROM rag_chunks WHERE concept_id IN ({marks})", ids)
        else:
            rows = self._query("SELECT * FROM rag_chunks")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ meta
    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None
