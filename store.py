"""PostgreSQL persistence for students, conversation and the knowledge graph.

WHY POSTGRES AND NOT A GRAPH DATABASE. The only genuinely graph-shaped question
this device asks is the one that matters most -- "this student is stuck on
algebra, which earlier concepts might they have missed?" -- and that is a
recursive walk up prerequisite edges, which `WITH RECURSIVE` does natively. A
conversation transcript is a linear sequence and gains nothing from being a
graph. Running Neo4j beside this would mean a JVM, a service to supervise and
roughly a gigabyte of RAM on a Pi, to answer a query Postgres already answers.

WHY IT FALLS BACK. This is a child's tutor, not a web service. If the database
is down, unreachable, or was never provisioned, Liza has to keep teaching --
so every read and write here degrades to the JSON files the device used before,
and `available()` reports which one is live. A failure to connect is logged once
and then stops being interesting; it must never reach the UI thread as an
exception.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:                                  # pragma: no cover
    psycopg = None
    dict_row = None

# Peer authentication over the unix socket: the OS user IS the database user, so
# there is no password to keep in .env and nothing to leak. DATABASE_URL
# overrides it for a device that keeps its store somewhere else.
DATABASE_URL = os.getenv("DATABASE_URL", "dbname=liza")
STORE_ENABLED = os.getenv("LIZA_STORE", "1") != "0"

_lock = threading.RLock()
_conn = None
_failed = False          # complained once already; stay quiet from here on


def _now():
    return datetime.now(timezone.utc)


def connect():
    """A live connection, or None. Never raises."""
    global _conn, _failed
    if not STORE_ENABLED or psycopg is None:
        return None
    with _lock:
        if _conn is not None:
            try:
                if _conn.closed:
                    _conn = None
                else:
                    return _conn
            except Exception:
                _conn = None
        try:
            _conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
            _failed = False
            return _conn
        except Exception as exc:
            if not _failed:
                print(f"[STORE] PostgreSQL unavailable ({exc}). "
                      f"Falling back to the JSON files.", flush=True)
                _failed = True
            _conn = None
            return None


def available():
    return connect() is not None


def query(sql, params=(), fetch="all"):
    """Run one statement. Returns rows, one row, or None on any failure.

    Swallowing errors is deliberate -- see the module docstring. The caller's
    fallback path is what keeps the device usable, so an exception here would
    only convert a degraded store into a broken lesson.
    """
    conn = connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "none" or cur.description is None:
                return []
            return cur.fetchall() if fetch == "all" else cur.fetchone()
    except Exception as exc:
        print(f"[STORE] Query failed ({exc}).", flush=True)
        global _conn
        with _lock:
            try:
                if _conn is not None:
                    _conn.close()
            except Exception:
                pass
            _conn = None       # force a reconnect next time
        return None


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------
def upsert_student(profile):
    """Mirror one profile into the store. Safe to call on every change."""
    return query(
        """INSERT INTO students (user_id, name, class, board, created_at, last_active)
           VALUES (%s, %s, %s, %s, COALESCE(%s, now()), COALESCE(%s, now()))
           ON CONFLICT (user_id) DO UPDATE
             SET name = EXCLUDED.name,
                 class = EXCLUDED.class,
                 board = EXCLUDED.board,
                 last_active = EXCLUDED.last_active""",
        (profile.get("user_id"), profile.get("name") or "Student",
         str(profile.get("class")), profile.get("board"),
         profile.get("created_at"), profile.get("last_active")),
        fetch="none") is not None


def delete_student(user_id):
    return query("DELETE FROM students WHERE user_id = %s", (user_id,),
                 fetch="none") is not None


def list_students():
    rows = query("SELECT * FROM students ORDER BY last_active DESC")
    return rows or []


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------
def append_messages(user_id, messages):
    """Append turns for one student. Returns False if the store took none."""
    if not user_id or not messages:
        return False
    conn = connect()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            for message in messages:
                role = message.get("role")
                if role not in ("user", "assistant", "system"):
                    continue
                cur.execute(
                    "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
                    (user_id, role, message.get("content") or ""))
        return True
    except Exception as exc:
        print(f"[STORE] Could not append messages ({exc}).", flush=True)
        return False


def load_messages(user_id, limit=40):
    """The most recent turns for one student, oldest first. None if unavailable.

    None rather than [] so the caller can tell "no store" from "new student" --
    the first must fall back to JSON, the second must not.
    """
    if not user_id:
        return None
    rows = query(
        """SELECT role, content FROM (
               SELECT id, role, content FROM messages
               WHERE user_id = %s AND role <> 'system'
               ORDER BY id DESC LIMIT %s
           ) recent ORDER BY id ASC""",
        (user_id, limit))
    if rows is None:
        return None
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def forget_student_history(user_id):
    return query("DELETE FROM messages WHERE user_id = %s", (user_id,),
                 fetch="none") is not None


# ---------------------------------------------------------------------------
# The knowledge graph
# ---------------------------------------------------------------------------
def concept_by_slug(slug):
    return query("SELECT * FROM concepts WHERE slug = %s", (slug,), fetch="one")


def record_concept(user_id, slug, outcome="met"):
    """Note that a student met a concept, and how it went.

    `outcome` is 'met', 'struggling' or 'confident'. Confidence moves gradually
    rather than jumping: one confused question about fractions does not mean a
    child has forgotten fractions, and one right answer does not mean they have
    mastered them. The 0.25 step means roughly three consistent signals to move
    between states, which is about how long a real teacher would take to change
    their mind.
    """
    if not user_id or not slug:
        return False
    delta = {"confident": 0.25, "met": 0.05, "struggling": -0.25}.get(outcome, 0.0)
    return query(
        """INSERT INTO student_concepts (user_id, concept_id, status, confidence,
                                         times_seen, last_seen)
           SELECT %s, c.id, %s, GREATEST(0, LEAST(1, 0.5 + %s)), 1, now()
             FROM concepts c WHERE c.slug = %s
           ON CONFLICT (user_id, concept_id) DO UPDATE
             SET times_seen = student_concepts.times_seen + 1,
                 last_seen  = now(),
                 confidence = GREATEST(0, LEAST(1, student_concepts.confidence + %s)),
                 status = CASE
                     WHEN GREATEST(0, LEAST(1, student_concepts.confidence + %s)) >= 0.75
                          THEN 'confident'
                     WHEN GREATEST(0, LEAST(1, student_concepts.confidence + %s)) <= 0.35
                          THEN 'struggling'
                     ELSE 'met' END""",
        (user_id, outcome if outcome in ("met", "struggling", "confident") else "met",
         delta, slug, delta, delta, delta),
        fetch="none") is not None


def missing_prerequisites(user_id, slug, max_depth=4, student_class=None,
                          lookback=3):
    """See below. `student_class` and `lookback` guard the cold-start problem."""
    return _missing_prerequisites(user_id, slug, max_depth, student_class, lookback)


def _missing_prerequisites(user_id, slug, max_depth=4, student_class=None,
                           lookback=3):
    """The gaps behind a concept: what comes BEFORE it that this student is weak on.

    This is the query the whole schema exists for -- "a Class 7 student is
    struggling with algebra, which earlier concepts might they have missed?" It
    walks the prerequisite edges backwards from the concept they are stuck on and
    returns the ones they have never met, or have met and struggled with.

    Ordered nearest-first, because the useful answer to a stuck student is the
    step immediately behind them, not the root of the subject.

    `seen` carries the path so far and is what stops a mis-entered pair of edges
    (A needs B, B needs A) looping forever; depth is capped as a second guard.
    """
    if not user_id or not slug:
        return None
    rows = query(
        """WITH RECURSIVE start AS (
               SELECT id FROM concepts WHERE slug = %s
           ),
           walk (concept_id, depth, seen) AS (
               SELECT p.prereq_id, 1, ARRAY[s.id, p.prereq_id]
                 FROM start s JOIN concept_prereqs p ON p.concept_id = s.id
             UNION ALL
               SELECT p.prereq_id, w.depth + 1, w.seen || p.prereq_id
                 FROM walk w JOIN concept_prereqs p ON p.concept_id = w.concept_id
                WHERE w.depth < %s AND NOT p.prereq_id = ANY(w.seen)
           )
           SELECT c.slug, c.name, c.subject, c.min_class,
                  MIN(w.depth) AS depth,
                  sc.status, sc.confidence, sc.times_seen, sc.last_seen
             FROM walk w
             JOIN concepts c ON c.id = w.concept_id
             LEFT JOIN student_concepts sc
                    ON sc.concept_id = w.concept_id AND sc.user_id = %s
            WHERE
               -- Met it and it did not stick. Always worth surfacing, whatever
               -- class the idea belongs to: this is measured, not assumed.
                  sc.confidence < 0.5
               -- Or never met it -- but ONLY if the idea is recent enough to be
               -- a plausible gap for this student. Without this, a Class 7 child
               -- with no recorded history of addition is told to go back to
               -- addition, because "no record" reads as "does not know it". The
               -- store starts empty for every child, so untreated, that made the
               -- advice worst exactly when it was newest.
               OR (sc.user_id IS NULL
                   AND (%s::int IS NULL
                        OR c.min_class IS NULL
                        OR c.min_class >= %s::int - %s::int))
            GROUP BY c.slug, c.name, c.subject, c.min_class,
                     sc.status, sc.confidence, sc.times_seen, sc.last_seen
            ORDER BY MIN(w.depth) ASC, c.min_class ASC""",
        (slug, max_depth, user_id, student_class, student_class, lookback))
    return rows


def student_summary(user_id, limit=8):
    """What this student has been working on lately, strongest first."""
    rows = query(
        """SELECT c.slug, c.name, c.subject, sc.status, sc.confidence, sc.last_seen
             FROM student_concepts sc JOIN concepts c ON c.id = sc.concept_id
            WHERE sc.user_id = %s
            ORDER BY sc.last_seen DESC LIMIT %s""",
        (user_id, limit))
    return rows or []


# ---------------------------------------------------------------------------
# Recognising which concept a question is about
# ---------------------------------------------------------------------------
# Keyword matching against the concept table rather than an LLM classifier. This
# runs on the turn the student is waiting on, and a second model call to label a
# question would add latency to every single answer to improve a signal that is
# only ever advisory. A missed match costs nothing -- the concept simply is not
# recorded that turn -- so the cheap check is the right trade here.
#
# Longest name first, so "cell organelles" wins over "cell" and "linear
# equations" over "algebra".
_CONCEPT_CACHE = None
_EXTRA_KEYWORDS = {
    "fractions": ("fraction", "numerator", "denominator"),
    "multiplication": ("times table", "multiply", "product of"),
    "division": ("divide", "quotient"),
    "percentages": ("percent", "percentage"),
    "algebra": ("algebra", "variable", "solve for x"),
    "photosynthesis": ("photosynthesis",),
    "gravity": ("gravity", "gravitational"),
    "cell": ("cell",),
    "derivatives": ("differentiate", "derivative"),
    "trigonometry": ("sine", "cosine", "tangent", "trigonometry"),
}


def _concept_keywords():
    global _CONCEPT_CACHE
    if _CONCEPT_CACHE is not None:
        return _CONCEPT_CACHE
    rows = query("SELECT slug, name FROM concepts")
    if rows is None:
        return []
    pairs = []
    for row in rows:
        words = {row["name"].lower(), row["slug"].replace("-", " ")}
        words.update(_EXTRA_KEYWORDS.get(row["slug"], ()))
        for word in words:
            pairs.append((word, row["slug"]))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    _CONCEPT_CACHE = pairs
    return pairs


def detect_concept(text):
    """The concept slug a question is about, or None. Best-effort."""
    if not text:
        return None
    lowered = text.lower()
    for word, slug in _concept_keywords():
        if word in lowered:
            return slug
    return None
