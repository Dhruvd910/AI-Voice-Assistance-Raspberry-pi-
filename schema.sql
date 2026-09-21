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

-- ---------------------------------------------------------------------------
-- What each student actually DID
-- ---------------------------------------------------------------------------
-- student_concepts above is what she BELIEVES about a child -- a running
-- confidence per idea, which is the right shape for deciding what to teach
-- next and the wrong shape for answering "what did they do today". It carries
-- no dates you can list, it collapses ten sessions into one row, and nothing
-- outside the graded conversation flow ever writes to it: a Kindergarten child
-- could spend a week on this device and leave no trace at all.
--
-- So this is the log beside it. One row per thing finished -- a story heard, a
-- word spelled, a test scored, a topic taught -- appended and never updated,
-- because the question it exists to answer is a question about time.
CREATE TABLE IF NOT EXISTS activities (
    id         BIGSERIAL PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES students(user_id) ON DELETE CASCADE,
    -- 'story', 'spelling', 'letters', 'counting', 'order', 'test', 'lesson'.
    -- Text rather than an enum: a new KG screen should not need a migration
    -- before it can record anything.
    kind       TEXT NOT NULL,
    -- What it was about: the story's title, the word, the letter, the concept.
    topic      TEXT NOT NULL,
    -- One short line for the progress screen to show. Never read by any query.
    detail     TEXT,
    -- Marks out of marks available. NULL for the things that are not scored --
    -- listening to a story is not a test and must never look like one.
    score      INT,
    out_of     INT,
    language   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS activities_user_idx
    ON activities (user_id, created_at DESC);
-- The progress screen groups by kind and by topic, over one student.
CREATE INDEX IF NOT EXISTS activities_user_kind_idx
    ON activities (user_id, kind, created_at DESC);

-- ---------------------------------------------------------------------------
-- The textbooks
-- ---------------------------------------------------------------------------
-- WHY POSTGRES FULL TEXT AND NOT EMBEDDINGS. The obvious way to build this is
-- a vector index, and on this device the obvious way is the wrong one: an
-- embedding model has to run on every question AND over every one of the tens
-- of thousands of chunks at ingest time, on a Pi, and then a second index has
-- to be kept in step with the first. Postgres already has a GIN-indexed
-- tsvector, it is already running, it answers in single-digit milliseconds
-- over a whole shelf of books, and a child's question is full of exactly the
-- rare nouns lexical search is best at -- "photosynthesis", "trigonometry",
-- "Mughal". Semantic search earns its cost where the words differ and the
-- meaning matches; a textbook and the question about it share the words.
CREATE TABLE IF NOT EXISTS books (
    id       SERIAL PRIMARY KEY,
    board    TEXT NOT NULL,             -- 'CBSE', 'ICSE', or whatever was ingested
    class    INT  NOT NULL CHECK (class BETWEEN 1 AND 12),
    subject  TEXT NOT NULL,
    -- The chapter's OWN name, read off its first page -- "The Wonderful World
    -- of Science", not the file it arrived in. Without it the index knew a
    -- passage was in "Chapter 2" and nothing more, so a question about what a
    -- chapter is CALLED had nothing to answer from and was answered out of the
    -- model's memory of a book that has since been withdrawn.
    title    TEXT NOT NULL,
    chapter  INT,
    -- The raw text of the chapter's first two pages. Kept so that chapter names
    -- can be read again with a better reader WITHOUT the PDF, which is deleted
    -- once it is indexed: the whole shelf is ~17GB of PDFs and ~300MB indexed,
    -- on an SD card with 5GB free.
    head_text TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    -- The file it was read from. UNIQUE so re-running the ingest over a folder
    -- replaces a book rather than doubling it, which is the normal way this is
    -- used: drop in the missing chapters and run it again.
    source   TEXT UNIQUE NOT NULL,
    pages    INT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS books_class_idx ON books (board, class, subject);

CREATE TABLE IF NOT EXISTS book_chunks (
    id       BIGSERIAL PRIMARY KEY,
    book_id  INT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter  TEXT,
    page     INT,
    content  TEXT NOT NULL,
    -- Built at insert time with the configuration that matches the chunk's own
    -- language: 'english' stems, and stemming Hindi with the English stemmer
    -- produces lexemes that match nothing. The query is built the same way
    -- from the question's own script, so same-language search works and
    -- cross-language search correctly returns nothing.
    tsv      tsvector NOT NULL
);
CREATE INDEX IF NOT EXISTS book_chunks_tsv_idx ON book_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS book_chunks_book_idx ON book_chunks (book_id);
