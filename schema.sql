-- Liza's store: students, what they said, and what they know.
--
-- PostgreSQL rather than a graph database, deliberately. The only genuinely
-- graph-shaped question here is "this student is stuck on algebra, which earlier
-- concepts might they have missed?", which is a recursive walk up the
-- prerequisite edges -- and WITH RECURSIVE does exactly that. A conversation
-- transcript is a linear sequence, not a graph, so it lives in an ordinary table
-- where it belongs.
--
-- Everything here must survive the device losing power mid-lesson, so writes are
-- small and independent rather than batched at the end of a session.

CREATE TABLE IF NOT EXISTS students (
    user_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    class       TEXT NOT NULL,          -- 'KG' or '1'..'12'
    board       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per spoken turn. Ordered by id, which is also the reason id is a
-- sequence rather than a timestamp: two turns inside the same second still have
-- a definite order.
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES students(user_id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_user_idx ON messages (user_id, id);

-- ---------------------------------------------------------------------------
-- The knowledge graph
-- ---------------------------------------------------------------------------
-- Nodes. `min_class` is the earliest class the idea is normally met in, which is
-- what lets a recommendation stay age-appropriate: a Class 7 student missing a
-- prerequisite should be sent back to a Class 4 idea, not forward to a Class 10
-- one that happens to be related.
CREATE TABLE IF NOT EXISTS concepts (
    id        SERIAL PRIMARY KEY,
    slug      TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    subject   TEXT NOT NULL,
    min_class INT
);

-- Edges: "you need `prereq_id` before `concept_id` will make sense."
-- Directed and acyclic in intent; see the cycle guard in the traversal query,
-- because a mis-entered pair would otherwise loop forever.
CREATE TABLE IF NOT EXISTS concept_prereqs (
    concept_id INT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    prereq_id  INT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    PRIMARY KEY (concept_id, prereq_id),
    CHECK (concept_id <> prereq_id)
);
CREATE INDEX IF NOT EXISTS concept_prereqs_prereq_idx ON concept_prereqs (prereq_id);

-- What each student has met and how it went. `confidence` is a running 0..1
-- estimate, `status` the human-readable form of the same thing so a query can
-- be read without decoding a float.
CREATE TABLE IF NOT EXISTS student_concepts (
    user_id     TEXT NOT NULL REFERENCES students(user_id) ON DELETE CASCADE,
    concept_id  INT  NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'met'
                CHECK (status IN ('met', 'struggling', 'confident')),
    confidence  REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    times_seen  INT  NOT NULL DEFAULT 1,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, concept_id)
);
CREATE INDEX IF NOT EXISTS student_concepts_user_idx ON student_concepts (user_id, last_seen DESC);
