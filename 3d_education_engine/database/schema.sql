-- The registry, the knowledge graph and the RAG index, in one SQLite file.
--
-- The JSON manifests under models/manifests/ and assets/**/metadata/ are the
-- source of truth; this database is an index rebuilt from them by
-- scripts/build_index.py, so it can always be deleted and regenerated.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS models (
    id              TEXT PRIMARY KEY,           -- biology.anatomy.heart
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,              -- biology | chemistry | physics
    subject         TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- asset | procedural | molecule | simulation
    manifest_path   TEXT NOT NULL,
    manifest_json   TEXT NOT NULL,
    disclaimer      TEXT,
    scale_level     TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_aliases (
    alias           TEXT NOT NULL,              -- normalised: lower case, single spaces
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    PRIMARY KEY (alias, model_id)
);

CREATE TABLE IF NOT EXISTS model_parts (
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    part_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    available       INTEGER NOT NULL DEFAULT 1, -- 0: named in the curriculum, absent from the source
    note            TEXT,
    PRIMARY KEY (model_id, part_id)
);

CREATE TABLE IF NOT EXISTS part_aliases (
    model_id        TEXT NOT NULL,
    part_id         TEXT NOT NULL,
    alias           TEXT NOT NULL,
    PRIMARY KEY (model_id, part_id, alias),
    FOREIGN KEY (model_id, part_id) REFERENCES model_parts(model_id, part_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS part_groups (
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    group_id        TEXT NOT NULL,              -- chambers, valves, great_vessels
    part_id         TEXT NOT NULL,
    PRIMARY KEY (model_id, group_id, part_id)
);

CREATE TABLE IF NOT EXISTS model_capabilities (
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    capability      TEXT NOT NULL,
    PRIMARY KEY (model_id, capability)
);

-- One row per source a model draws on. Never deleted by an index rebuild
-- unless the manifest itself is gone.
CREATE TABLE IF NOT EXISTS provenance (
    model_id            TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    source_name         TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    creator             TEXT NOT NULL,
    license             TEXT NOT NULL,
    license_url         TEXT NOT NULL,
    attribution_text    TEXT NOT NULL,
    download_date       TEXT NOT NULL,
    modification_status TEXT NOT NULL,
    PRIMARY KEY (model_id, source_name, source_id)
);

CREATE TABLE IF NOT EXISTS asset_files (
    model_id        TEXT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,              -- original | processed | runtime | thumbnail
    path            TEXT NOT NULL,              -- relative to the project root
    sha256          TEXT,
    bytes           INTEGER,
    PRIMARY KEY (model_id, role, path)
);

CREATE TABLE IF NOT EXISTS kg_nodes (
    id              TEXT PRIMARY KEY,           -- usually a model or part id
    label           TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- organism | organ | part | tissue | cell | organelle | molecule | atom | concept | simulation
    model_id        TEXT                        -- the model that shows it, if any
);

CREATE TABLE IF NOT EXISTS kg_edges (
    src             TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    relation        TEXT NOT NULL,              -- has_part | contains | connected_to | scale_down_to | produces | ...
    dst             TEXT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    PRIMARY KEY (src, relation, dst)
);
CREATE INDEX IF NOT EXISTS kg_edges_dst ON kg_edges(dst, relation);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id              INTEGER PRIMARY KEY,
    doc_path        TEXT NOT NULL,
    concept_id      TEXT NOT NULL,
    section         TEXT NOT NULL,              -- definition | function | structure | misconceptions | ...
    text            TEXT NOT NULL,
    grades          TEXT,
    difficulty      TEXT,
    embedder        TEXT NOT NULL,
    embedding       BLOB NOT NULL               -- float32, L2-normalised
);
CREATE INDEX IF NOT EXISTS rag_chunks_concept ON rag_chunks(concept_id);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
