"""What a friend remembers: the things a student has told her about themselves.

A leaf: imports nothing of ours. The model call is handed in by the caller.

WHY THIS EXISTS
    The chat window is the last six exchanges, so "I have a science test on
    Friday" was gone by the weekend and she could never ask how it went. A
    friend is mostly memory -- the match you lost, the cat's name, the subject
    you hate -- so each student gets a short notebook of those, kept on the
    device, and it goes into her prompt beside their class and board.

HOW IT IS KEPT
    After a turn in which the student said something about THEMSELVES (a cheap
    first-person check -- see RE_ABOUT_THEMSELVES), one small model call is
    given the notebook and what they said, and hands back the whole notebook
    updated: added to, a near-duplicate merged, a past event rewritten with how
    it went, an old one dropped, or something they asked her to forget removed.
    It runs on its own thread after the reply has started, so it never makes
    her slower to answer, and any failure -- a rate limit, a network drop, a
    reply that is not JSON -- leaves the notebook exactly as it was.

    One small JSON file per student, history/<user_id>.memory.json, written the
    same way as the progress file: beside, then moved into place.
"""

import json
import os
import re
import threading
from datetime import date, datetime

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")
# Enough for a friend's worth of detail, small enough that the prompt section
# stays a few hundred bytes.
MEMORY_KEEP = 15
MEMORY_ENABLED = os.getenv("FRIEND_MEMORY", "1") != "0"

# Something about themselves, or asking her to forget. Deliberately NOT "me" /
# "मुझे" on their own: "tell me about the sun" and "मुझे बताओ" are most of the
# questions she is asked, and none of them is news about the student.
RE_ABOUT_THEMSELVES = re.compile(
    r"\b(?:i|i'm|im|i've|i'll|i'd|my|mine|we|we're|our|main|mera|meri|mere|hum|"
    r"hamara|hamari|hamare|forget)\b"
    r"|मैं|मेरा|मेरी|मेरे|हम|हमारा|हमारी|हमारे|भूल\s*जा",
    re.IGNORECASE)

_lock = threading.Lock()


def memory_path(user_id):
    return os.path.join(MEMORY_DIR, f"{user_id}.memory.json")


def load(user_id):
    """The notebook, a list of short sentences. [] when there is none."""
    if not user_id:
        return []
    try:
        with open(memory_path(user_id), encoding="utf-8") as handle:
            facts = json.load(handle)
        return [f for f in facts if isinstance(f, str) and f.strip()] \
            if isinstance(facts, list) else []
    except (FileNotFoundError, ValueError):
        return []
    except Exception as exc:
        print(f"[MEMORY] Could not read the notebook ({exc}).", flush=True)
        return []


def save(user_id, facts):
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        temporary = memory_path(user_id) + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(facts, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, memory_path(user_id))
    except Exception as exc:
        print(f"[MEMORY] Could not write the notebook ({exc}).", flush=True)


def forget(user_id):
    """Delete a removed student's notebook, the way their history is deleted."""
    if not user_id:
        return
    try:
        os.remove(memory_path(user_id))
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[MEMORY] Could not remove the notebook ({exc}).", flush=True)


# "Fri 25 Sep", the way _INSTRUCTION asks for dates to be written.
RE_NOTE_DATE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,? (\d{1,2}) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*", re.IGNORECASE)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
           "nov", "dec"]


def when_from_today(fact, today=None):
    """"(that was yesterday)", "(that is in 3 days)" for a dated fact, else "".

    Worked out here and not left to the model: asked on a Saturday about a test
    written down as "Fri 25 Sep", it said the test was "coming up this Friday".
    """
    match = RE_NOTE_DATE.search(fact)
    if not match:
        return ""
    today = today or date.today()
    day, month = int(match.group(1)), _MONTHS.index(match.group(2).lower()[:3]) + 1
    try:
        # The nearest such date: "2 Jan" written in late December is next year.
        when = min((date(today.year + shift, month, day) for shift in (-1, 0, 1)),
                   key=lambda d: abs((d - today).days))
    except ValueError:
        return ""
    days = (when - today).days
    if days == 0:
        return " (that is today)"
    if days == 1:
        return " (that is tomorrow)"
    if days == -1:
        return " (that was yesterday)"
    return f" (that is in {days} days)" if days > 0 else f" (that was {-days} days ago)"


def prompt_block(profile):
    """The notebook as a prompt section, or "" when there is nothing in it."""
    facts = load((profile or {}).get("user_id"))
    if not facts:
        return ""
    name = (profile or {}).get("name") or "they"
    return (
        f"### WHAT {name.upper()} HAS TOLD YOU BEFORE (your own memory of them)\n"
        + "".join(f"- {fact}{when_from_today(fact)}\n" for fact in facts)
        + "Use this the way a friend does. When they greet you or just chat, and "
        "something here has HAPPENED since (a test, a match), ask how it went -- that "
        "is the first thing a friend says. Otherwise bring ONE thing up only when it "
        "fits, never every turn, and never read the list out. A plain question still "
        "gets a plain answer. Asked what you remember about them, tell them plainly. "
        "Asked to forget something, say you will.\n\n"
    )


def worth_learning_from(text):
    text = (text or "").strip()
    return (MEMORY_ENABLED and len(text.split()) >= 3
            and bool(RE_ABOUT_THEMSELVES.search(text)))


_INSTRUCTION = """You keep a small notebook for Liza, who is a friend to {name}, a child in Class {klass}. Today is {today}.

You get the notebook so far, the last thing Liza said, and what {name} just said. Return the WHOLE notebook, updated, as a JSON array of short English sentences about {name} -- at most {keep}.

Write down only what a friend would remember and bring up later: hobbies and interests, favourite things, the names of friends, pets and family they mention, subjects they like or find hard, how they have been feeling, and coming events -- with the real date worked out from today, so "on Friday" becomes "on <Fri DD Mon>".
- ONE sentence per thing, with its details in it: "Has a science test on light and shadows on <Fri DD Mon>", not two entries.
- Never "today", "yesterday" or "tomorrow" -- they are wrong by the next day. Write the date: "Lost a cricket match on <Thu DD Mon>".
- Update an entry rather than adding a near-duplicate. Once an event has happened and they say how it went, rewrite it with that.
- Drop events more than two weeks in the past.
- They ask to forget something: remove it. They ask to forget everything: return [].
- NEVER write down passwords, phone numbers, addresses, or anything they call a secret.
- A question they asked Liza, or school work they asked her to explain, is not a fact about them.
- Nothing new? Return the notebook unchanged.

Output ONLY the JSON array."""


def _parse(reply):
    """The array in a model reply, or None."""
    match = re.search(r"\[.*\]", reply or "", re.DOTALL)
    if not match:
        return None
    try:
        facts = json.loads(match.group())
    except ValueError:
        return None
    if not isinstance(facts, list) or not all(isinstance(f, str) for f in facts):
        return None
    # Without the full stop, so "Plays cricket." and "Plays cricket" are one fact.
    return [f.strip().rstrip(".") for f in facts if f.strip()][:MEMORY_KEEP]


def learn(profile, said, her_last_line, complete):
    """Update the notebook from one thing they said. Blocking; see learn_later.

    `complete(messages) -> str` is the model call, handed in so this module
    never needs to know which client or model the device uses.
    """
    user_id = (profile or {}).get("user_id")
    if not user_id or not worth_learning_from(said):
        return
    with _lock:
        before = load(user_id)
        messages = [
            {"role": "system", "content": _INSTRUCTION.format(
                name=profile.get("name") or "the student",
                klass=profile.get("class") or "?",
                today=datetime.now().strftime("%a %d %b %Y"),
                keep=MEMORY_KEEP)},
            {"role": "user", "content":
                f"NOTEBOOK: {json.dumps(before, ensure_ascii=False)}\n"
                f"LIZA SAID: {her_last_line or '(nothing yet)'}\n"
                f"{(profile.get('name') or 'THE STUDENT').upper()} SAID: {said}"},
        ]
        try:
            after = _parse(complete(messages))
        except Exception as exc:
            print(f"[MEMORY] Could not update the notebook ({exc}).", flush=True)
            return
        if after is None:
            print("[MEMORY] The notebook update was not a list; kept the old one.",
                  flush=True)
            return
        if after == before:
            return
        save(user_id, after)
        added = [f for f in after if f not in before]
        dropped = [f for f in before if f not in after]
        print(f"[MEMORY] {len(after)} thing(s) remembered"
              + (f"; new: {added}" if added else "")
              + (f"; dropped: {dropped}" if dropped else ""), flush=True)


def learn_later(profile, said, her_last_line, complete):
    """learn() on its own thread, so the reply never waits for it."""
    if not (profile or {}).get("user_id") or not worth_learning_from(said):
        return
    threading.Thread(target=learn, args=(profile, said, her_last_line, complete),
                     daemon=True, name="friend-memory").start()
