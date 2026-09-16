"""Student profiles and grade-adaptive answering.

Deliberately free of any Tkinter, audio or network import: the band logic is the
half of this feature that decides what every answer sounds like, and it has to be
testable without booting an 800x480 fullscreen UI on a Pi.

Storage matches the existing chat_history.json pattern -- a plain JSON file
written with json.dump -- rather than introducing SQLite for what is at most a
handful of children sharing one device.
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

# PostgreSQL is the primary store; this JSON file is the fallback and the
# migration source. Mirrored rather than replaced because a profile is the one
# thing the device cannot start without -- if the database is down mid-boot, a
# child still has to be able to pick their name. The JSON write is a few hundred
# bytes and happens only when a profile changes, so keeping both costs nothing.
try:
    import store
except ImportError:                                  # pragma: no cover
    store = None

# Anchored to the app directory rather than left relative to the working
# directory. The `liza` launcher never cd's into APP_DIR, so it runs with
# whatever CWD the shell or the desktop icon happened to have -- and a store
# resolved from there would follow it, so a child launched from the wrong place
# would find their profile simply gone and be asked to set up again.
PROFILES_FILE = os.getenv(
    "PROFILES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json"))

# "KG" plus 1-12, in the order the picker shows them.
KG_CLASS = "KG"
CLASS_VALUES = [KG_CLASS] + [str(n) for n in range(1, 13)]
BOARDS = ["CBSE", "ICSE", "State", "Other"]

# ==========================================
# Grade bands
# ==========================================
# The mode decides HOW she teaches (ask vs tell vs examine); the band decides how
# deep and how plain the language is. They are orthogonal, which is why the band
# is a separate fragment injected alongside the mode instruction rather than
# thirteen more copies of MODE_INSTRUCTIONS.
BAND_PRIMARY = "primary"
BAND_ELEMENTARY = "elementary"
BAND_MIDDLE = "middle"
BAND_SECONDARY = "secondary"
BAND_SENIOR = "senior"

_BAND_BY_CLASS = {}
for _n in (1, 2, 3):            _BAND_BY_CLASS[_n] = BAND_PRIMARY
for _n in (4, 5, 6):            _BAND_BY_CLASS[_n] = BAND_ELEMENTARY
for _n in (7, 8):               _BAND_BY_CLASS[_n] = BAND_MIDDLE
for _n in (9, 10):              _BAND_BY_CLASS[_n] = BAND_SECONDARY
for _n in (11, 12):             _BAND_BY_CLASS[_n] = BAND_SENIOR

BAND_LABELS = {
    BAND_PRIMARY:    "Primary (Class 1-3)",
    BAND_ELEMENTARY: "Elementary (Class 4-6)",
    BAND_MIDDLE:     "Middle (Class 7-8)",
    BAND_SECONDARY:  "Secondary (Class 9-10)",
    BAND_SENIOR:     "Senior Secondary (Class 11-12)",
}

# Written as hard ceilings and explicit bans rather than as adjectives ("simpler",
# "more advanced"). A model told to be "simple" still reaches for "organelle" and
# still writes six sentences; told the word is banned and four sentences is the
# limit, it does not. The sentence caps also have to survive rule 5 of the main
# prompt, which already tells her to stop when the question is answered -- these
# tighten that, never loosen it.
BAND_INSTRUCTIONS = {
    BAND_PRIMARY: (
        "Explain at a Class 1-3 level. Use only words a seven-year-old already knows, "
        "in short sentences. Compare the thing to something in their own home or "
        "playground. NEVER use a technical or scientific term, not even to define it. "
        "AT MOST 3 SENTENCES."
    ),
    BAND_ELEMENTARY: (
        "Explain at a Class 4-6 level. Give one plain definition in everyday words, then "
        "ONE relatable real-world example. Avoid jargon; if a technical word is truly "
        "unavoidable, say what it means in the same breath. AT MOST 4 SENTENCES."
    ),
    BAND_MIDDLE: (
        "Explain at a Class 7-8 level. Introduce the correct technical terms and define "
        "each one as you use it. Give the explanation some structure -- what it is, then "
        "how it works -- and one example. AT MOST 5 SENTENCES."
    ),
    BAND_SECONDARY: (
        "Explain at a Class 9-10 level. Use textbook vocabulary without stopping to "
        "define the common terms. Cover the detail an exam answer would need, including "
        "the mechanism, not just the definition. If the question asks what something is "
        "or how it works, a one-line definition is an INCOMPLETE answer at this level. "
        "If there is a formula the syllabus expects, say it aloud in words rather than "
        "as symbols. "
        "AT MOST 6 SENTENCES."
    ),
    BAND_SENIOR: (
        "Explain at a Class 11-12 level. Full technical depth and correct terminology "
        "throughout, assuming a solid grounding in the subject. Include the mechanism, "
        "the relevant classifications or sub-types, and name any significant exception "
        "or nuance. If the question asks what something is or how it works, a one-line "
        "definition is an INCOMPLETE answer at this level: name the main parts or types "
        "and how they work, not just the category the thing belongs to. "
        # Rule 5 bans symbols a voice cannot read, and the model was reading that
        # as "no equations", so Class 11 gravity came back with no formula at all
        # -- the one thing that actually separates it from the Class 8 answer.
        # The ban is on NOTATION, not on the relationship: said aloud, an equation
        # is just a sentence.
        "When a law or relationship has a governing equation, SAY IT ALOUD IN WORDS "
        "-- \"force equals G times the two masses divided by the distance squared\" -- "
        "and say what each quantity is. Never write symbols, superscripts or notation; "
        "a voice is reading this. "
        "AT MOST 7 SENTENCES."
    ),
}


def normalise_class(class_value):
    """"6", 6, " Class 6 ", "kg" -> "6" / "KG". None when it isn't a class at all."""
    if class_value is None:
        return None
    text = str(class_value).strip()
    if not text:
        return None
    if text.upper().replace(".", "").replace(" ", "") in ("KG", "LKG", "UKG", "NURSERY"):
        return KG_CLASS
    digits = re.search(r'\d+', text)
    if not digits:
        return None
    number = int(digits.group())
    return str(number) if 1 <= number <= 12 else None


def is_kindergarten(class_value):
    return normalise_class(class_value) == KG_CLASS


def get_grade_band(class_value):
    """The band for a class, or None for KG and anything unrecognised.

    KG deliberately has no band: those students never reach the LLM answering
    path at all, they are routed to the spelling and story screens instead.
    """
    normalised = normalise_class(class_value)
    if normalised is None or normalised == KG_CLASS:
        return None
    return _BAND_BY_CLASS[int(normalised)]


def band_instruction(class_value):
    """The system-prompt fragment for a class. "" when there is nothing to add.

    Empty rather than a default band when no profile is set, so the device
    behaves exactly as it did before this feature until somebody creates one.
    """
    band = get_grade_band(class_value)
    return BAND_INSTRUCTIONS.get(band, "")


# ==========================================
# Storage
# ==========================================
# One lock around the whole read-modify-write. The UI thread creates and switches
# profiles while ai_loop reads the active one on every turn, so an unguarded
# save would happily truncate the file mid-read.
_lock = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank_store():
    return {"active_user_id": None, "profiles": []}


def load_store(path=None):
    path = path or PROFILES_FILE
    with _lock:
        if not os.path.exists(path):
            return _blank_store()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            # A corrupt store must not stop the device booting; it just means
            # the next launch asks who is using it.
            return _blank_store()
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
            return _blank_store()
        data.setdefault("active_user_id", None)
        return data


def save_store(data, path=None):
    path = path or PROFILES_FILE
    with _lock:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
    _mirror(data)


def _mirror(data):
    """Push profiles into PostgreSQL. Silent no-op when it is unavailable."""
    if store is None:
        return
    try:
        for profile in data.get("profiles", []):
            store.upsert_student(profile)
    except Exception:
        # store.py already degrades quietly; this guard is for the case where it
        # is missing entirely, so a broken import can never stop a child being
        # able to choose their name.
        pass


def list_profiles(path=None):
    return load_store(path).get("profiles", [])


def get_active_profile(path=None):
    """The profile in use, or None when nobody has been set up yet."""
    data = load_store(path)
    active_id = data.get("active_user_id")
    if not active_id:
        return None
    for profile in data.get("profiles", []):
        if profile.get("user_id") == active_id:
            return profile
    return None


def create_profile(name, class_value, board=None, path=None, activate=True):
    """Add a profile and (by default) make it the active one."""
    normalised = normalise_class(class_value)
    if normalised is None:
        raise ValueError(f"not a class: {class_value!r}")
    profile = {
        "user_id": uuid.uuid4().hex[:12],
        "name": (name or "").strip() or "Student",
        "class": normalised,
        "board": (board or None),
        "created_at": _now(),
        "last_active": _now(),
    }
    with _lock:
        data = load_store(path)
        data["profiles"].append(profile)
        if activate or not data.get("active_user_id"):
            data["active_user_id"] = profile["user_id"]
        save_store(data, path)
    return profile


def set_active_profile(user_id, path=None):
    """Switch user. Returns the newly active profile, or None if there's no such id."""
    with _lock:
        data = load_store(path)
        for profile in data.get("profiles", []):
            if profile.get("user_id") == user_id:
                profile["last_active"] = _now()
                data["active_user_id"] = user_id
                save_store(data, path)
                return profile
    return None


def update_profile(user_id, name=None, class_value=None, board=None, path=None):
    with _lock:
        data = load_store(path)
        for profile in data.get("profiles", []):
            if profile.get("user_id") != user_id:
                continue
            if name is not None:
                profile["name"] = name.strip() or profile["name"]
            if class_value is not None:
                normalised = normalise_class(class_value)
                if normalised is None:
                    raise ValueError(f"not a class: {class_value!r}")
                profile["class"] = normalised
            if board is not None:
                profile["board"] = board or None
            profile["last_active"] = _now()
            save_store(data, path)
            return profile
    return None


def delete_profile(user_id, path=None):
    with _lock:
        data = load_store(path)
        remaining = [p for p in data.get("profiles", []) if p.get("user_id") != user_id]
        if len(remaining) == len(data.get("profiles", [])):
            return False
        data["profiles"] = remaining
        if data.get("active_user_id") == user_id:
            data["active_user_id"] = remaining[0]["user_id"] if remaining else None
        save_store(data, path)
        if store is not None:
            try:
                # Cascades to messages and student_concepts -- see the FKs in
                # schema.sql. Deleting a child means deleting what the device
                # knows about them, not just hiding their name.
                store.delete_student(user_id)
                # Not covered by that cascade: the progress log keeps a JSON
                # copy of every row beside the database one, and a deleted
                # child's work coming back the next time Postgres was down is
                # exactly the leak _forget_history_file below exists to close.
                store.forget_progress_file(user_id)
            except Exception:
                pass
        _forget_history_file(user_id)
        return True


def _forget_history_file(user_id):
    """Remove the JSON fallback copy of a deleted student's conversation.

    The database row is gone, but this file is what the device reads when
    PostgreSQL is down -- leaving it behind would mean a deleted child's
    conversation coming back the next time the store was unavailable.
    """
    if not user_id:
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "history", f"{user_id}.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[PROFILE] Could not remove {path} ({exc}).", flush=True)


def touch_active(path=None):
    """Stamp last_active on the profile in use. Cheap enough to call per session."""
    with _lock:
        data = load_store(path)
        active_id = data.get("active_user_id")
        for profile in data.get("profiles", []):
            if profile.get("user_id") == active_id:
                profile["last_active"] = _now()
                save_store(data, path)
                return profile
    return None


def active_profile():
    """The profile in use, or None. Never raises -- the UI draws either way.

    Lives here rather than in the assistant because the screen, the action tags
    and the Kindergarten flow all ask who is using the device, and none of them
    should have to import the assistant to find out."""
    try:
        return get_active_profile()
    except Exception as exc:
        print(f"[PROFILE] Could not read the profile store: {exc}", flush=True)
        return None

