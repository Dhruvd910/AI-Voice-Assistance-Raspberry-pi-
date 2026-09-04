"""The things Liza can DO on the device: the action tags of rule 7.

Opening and closing a file, listing a folder, running a shell command, changing
the screen mode, going to sleep -- and the file search that has to find what was
asked for before any of it can happen. The model asks for one of these with an
[ACTION: name:param] tag; what is safe to do about that is decided here, in
Python, and never by the model.

Four names still come back through `import assistant`: RE_DEVANAGARI,
active_user_id, openrouter_client and LLM_MODEL. They are reached through the
module and only at call time, exactly as ui.py does, so the cycle is safe -- but
they are the last thing keeping this file from being a leaf, and they belong in
a language module, beside the profile helpers, and in config respectively.
"""

import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import threading
import time

import profiles
import state
import store
from config import MPV_AUDIO_DEVICE
from media import _die_with_parent, stop_media_playback
from state import (device_state_lock, get_device_state, media_active,
                   note_media_started, set_playing_state, subprocess_lock)
from uibridge import ui_call, ui_invoke

CLOSED_FILE_ACKS = {"en": "Closed.", "hi": "बंद कर दिया।", "hinglish": "बंद कर दिया।"}

# "Is there a file about gravity?" is a QUESTION, and it was being answered by
# opening the file -- the model saw anything mentioning a file as an instruction
# to open one, so asking whether something existed launched it fullscreen. What
# a person does instead is say what they found and wait to be told.
#
# Handled here rather than in the prompt because the model cannot see the disk:
# left to itself it invents plausible filenames (observed: "open_file:Phylaem"
# off a garbled transcript). find_files() actually looks.
RE_FILE_QUERY = re.compile(
    r'^\s*(?:hey\s+liza[,\s]*)?'
    # Spoken sentences rarely start on the question. "No, like is there a video
    # about gravity?" is one utterance from the logs, and anchoring at ^ without
    # this missed it entirely.
    r'(?:(?:no|yeah|yes|so|ok|okay|um|uh|err|actually|like|well|hmm|but|and)[,\s]+)*'
    r'(?:please\s+)?'
    r'(?:(?:can|could|would)\s+you\s+)?(?:just\s+)?(?:check|see|look|tell\s+me)?\s*'
    r'(?:if|whether)?\s*'
    # Both word orders: people say "is there a file" and "if there is a file".
    r'(?:there\s+)?(?:is|are|do|does)\s+(?:there\s+|you\s+have\s+|we\s+have\s+)?'
    r'(?:any\s+|a\s+|an\s+|the\s+|some\s+)?'
    r'(?:\w+\s+)??'                     # "study", "pdf", "science"
    r'(?:files?|documents?|videos?|notes?|pdfs?)\b'
    r'(?P<rest>.*)$',
    re.IGNORECASE)
# Leading filler between "...file" and the actual topic. Peeled repeatedly, the
# way _file_variants does it, because real speech stacks several: "available
# named", "there related to", "with the name".
RE_FILE_QUERY_LEAD = re.compile(
    r'^[\s.,:?!-]*(?:is|are|available|there|present|saved|stored|named|called|'
    r'about|regarding|related|relating|to|on|for|in|of|with|the|a|an|any|name|'
    r'my|our|which|that)\b[\s.,:?!-]*', re.IGNORECASE)

def file_query_topic(text):
    """The topic in 'is there any file about X', or None if that is not the ask."""
    match = RE_FILE_QUERY.match(text or "")
    if not match:
        return None
    rest = match.group("rest") or ""
    previous = None
    while previous != rest:
        previous = rest
        rest = RE_FILE_QUERY_LEAD.sub('', rest)
    topic = rest.strip(" .,:;?!-\"'।")
    return topic or None

RE_CONFIRM_YES = re.compile(
    # The Devanagari spellings sit OUTSIDE the \b group on purpose: \w excludes
    # combining marks, so a word ending in one ("हाँ") has no word boundary
    # after it and \b can never match there. Same trap as RE_ECHO_TOKEN.
    r'^\s*(?:(?:yes|yeah|yep|yup|sure|ok|okay|alright|please|go\s+ahead|'
    r'open\s+it|play\s+it|do\s+it|show\s+me|haan|haa|ji)\b'
    r'|हाँ|हां|जी|ठीक|खोलो|चलाओ)', re.IGNORECASE)
RE_CONFIRM_NO = re.compile(
    r"^\s*(?:(?:no|nope|nah|not\s+now|don'?t|do\s+not|cancel|never\s+mind|leave\s+it|"
    r'nahi|nahin)\b|नहीं|नही|ना|रहने)', re.IGNORECASE)

FILE_FOUND_ACKS = {
    "en": "Yes, {name} is available. Shall I open it?",
    "hi": "हाँ, {name} उपलब्ध है। क्या मैं इसे खोलूँ?",
}
FILE_FOUND_ACKS["hinglish"] = FILE_FOUND_ACKS["hi"]
FILE_MISSING_ACKS = {
    "en": "I couldn't find any file about {topic}.",
    "hi": "{topic} से जुड़ी कोई फ़ाइल नहीं मिली।",
}
FILE_MISSING_ACKS["hinglish"] = FILE_MISSING_ACKS["hi"]
OPENING_ACKS = {"en": "Opening {name}.", "hi": "{name} खोल रही हूँ।"}
OPENING_ACKS["hinglish"] = OPENING_ACKS["hi"]
FILE_CANCEL_ACKS = {"en": "Okay, leaving it closed.", "hi": "ठीक है, नहीं खोल रही।"}
FILE_CANCEL_ACKS["hinglish"] = FILE_CANCEL_ACKS["hi"]

# "close it", "close the file", "इसे बंद करो". Deliberately narrower than
# RE_STOP_MEDIA_PHRASE: this one only ever fires while a file is actually open,
# and it must not swallow "close" used as ordinary conversation ("how close is
# the moon"), so it has to be the whole utterance.
RE_CLOSE_FILE_PHRASE = re.compile(
    r'^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?'
    r'(?:close|shut|exit|quit)\s*'
    r'(?:the\s+|this\s+|that\s+|my\s+)?(?:current\s+|open\s+)?'
    r'(?:file|document|doc|window|it|this|that)?\s*'
    r'(?:now|please)?\s*$'
    r'|^\s*(?:इस[ेको]?|यह|फ़ाइल|फाइल)?\s*(?:बंद|बन्द)\s*(?:कर\S*)?\s*(?:दो|दीजिए|दीजिये)?\s*$',
    re.IGNORECASE)

# ==========================================
# Agentic Actions (rule 7)
# ==========================================
# The model ends a reply with at most one [ACTION: name] or [ACTION: name:param]
# tag and the device carries it out. Deliberately a TAG rather than a Python
# intent-matcher like detect_play_media(): "close it", "shh", "बंद करो" and "just
# the mascot" are the same handful of intents wearing a hundred different
# sentences, and the model is already reading that sentence. What Python owns
# instead is everything the model must never be trusted with -- where a file may
# be searched for, what may be killed, and when it actually happens.
RE_ACTION_TAG = re.compile(r'\[\s*ACTION\s*:\s*([a-z_]+)\s*(?::\s*([^\]]*))?\]',
                           re.IGNORECASE)

def parse_action(text):
    """(name, param) for the FIRST tag in a reply, or (None, None).

    One action per reply is a prompt rule, and enforcing it here as well means a
    model that ignores it opens one file instead of five."""
    matches = list(RE_ACTION_TAG.finditer(text or ""))
    if not matches:
        return None, None
    if len(matches) > 1:
        print(f"[ACTION] {len(matches)} tags in one reply; obeying the first only.", flush=True)
    name = matches[0].group(1).lower()
    param = (matches[0].group(2) or "").strip().strip('"\'.,!?।')
    return name, param


# ---------- file search ----------
HOME_DIR = os.path.realpath(os.path.expanduser("~"))
# Directories with nothing a student would ask for and thousands of entries to
# walk. The mascot cache alone is 419 PNGs, and on a Pi that is most of the
# search budget spent on files that can never be the answer.
FILE_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv", "env", "cache",
                  ".git", "snap", "site-packages", "dist-packages", "logs"}
FILE_SEARCH_MAX_DEPTH = 6
FILE_SEARCH_MAX_SECONDS = 6.0
# Leading words the student says but no filename ever starts with: "open MY notes".
RE_FILE_LEAD = re.compile(r'^(?:my|the|that|this|a|an|our|some)\s+', re.IGNORECASE)
# Trailing words a person says AROUND a filename but that are not part of it.
# The file-type words matter as much as "file" does: "open my physics pdf" is at
# least as natural as "open my physics file", and without them the search looks
# for a file literally called "physics pdf" and finds nothing. Safe to strip
# because _file_variants keeps the unstripped form first, so a file genuinely
# named "physics pdf" still wins on the more specific pass.
RE_FILE_TRAIL = re.compile(
    r'\s+(?:file|document|doc|pdf|txt|text|image|picture|photo|slides|'
    r'presentation|sheet|spreadsheet|video|audio|clip|movie|recording|'
    r'song|track|please|for\s+me)$', re.IGNORECASE)

# The same words in Hindi, stripped BEFORE transliteration rather than after.
# "ग्रेविटी फाइल" transliterates to "greviti phaila", and by then the trailing
# word is Latin nonsense that the English pattern above cannot recognise -- the
# search then looks for a file called "greviti phaila" and finds nothing, which
# is exactly what "ग्रेविटी नाम की फाइल" did. Taken off in Devanagari, while it
# is still a word, what reaches the transliterator is just the name.
RE_FILE_TRAIL_HI = re.compile(
    r'\s*(?:नाम\s*(?:की|का|से)?|वाली|वाला|नामक)?\s*'
    r'(?:फाइल|फ़ाइल|फ़ाईल|फाईल|डॉक्युमेंट|दस्तावेज़|वीडियो|विडियो|'
    r'ऑडियो|फोटो|फ़ोटो|तस्वीर|इमेज|गाना|गाने|क्लिप|मूवी|फिल्म)\s*$')
# And the leading ones: "मेरी ग्रेविटी फाइल" -- "my gravity file".
RE_FILE_LEAD_HI = re.compile(r'^\s*(?:मेरी|मेरा|मेरे|वो|ये|यह|वह|कोई|एक)\s+')

# A slash, a "..", a "~" or a leading drive-like path never appears in a name a
# student SAID out loud -- those only turn up when something is trying to leave
# the home directory. Checked as its own thing so open_file can tell the student
# why it refused instead of pretending the file was simply missing.
RE_PATHY = re.compile(r'[\\/~]|\.\.')

def looks_like_path(phrase):
    return bool(RE_PATHY.search(phrase or ""))

def safe_relative_name(phrase):
    """True for a plain relative name -- no traversal, no absolute root."""
    phrase = (phrase or "").strip().strip('"\'')
    return bool(phrase) and not phrase.startswith(("/", "~")) \
        and ".." not in phrase.split("/") and "\\" not in phrase

def resolve_relative_name(phrase):
    """A path like "sample/gravity" as a real file under $HOME, or None.

    THIS IS A SUBFOLDER, NOT AN ESCAPE ATTEMPT, and the difference matters
    because refusing both looks identical to the student. Once she has been
    told a file lives in a folder -- which list_files now tells her -- the
    natural next tag is open_file:sample/gravity, and RE_PATHY rejected it for
    containing a slash. The log has exactly that: the folder found, the file
    named inside it, and then "I can only open files in your home folder".

    Only a plain relative path is accepted: no "..", no leading slash, no "~",
    and the RESOLVED path is checked to still be inside $HOME, so a symlink
    pointing out of it is refused like anything else. The extension is optional
    the same way it is everywhere else, because names said out loud have none."""
    phrase = (phrase or "").strip().strip('"\'')
    if (not phrase or phrase.startswith(("/", "~"))
            or ".." in phrase.split("/") or "\\" in phrase):
        return None
    if "/" not in phrase:
        return None
    candidate = os.path.realpath(os.path.join(HOME_DIR, phrase))
    if not candidate.startswith(HOME_DIR + os.sep):
        return None
    if os.path.isfile(candidate):
        return candidate
    # No extension said: "sample/gravity" for gravity.mp4.
    parent, stem = os.path.dirname(candidate), os.path.basename(candidate).lower()
    try:
        for name in sorted(os.listdir(parent)):
            full = os.path.join(parent, name)
            if os.path.isfile(full) and os.path.splitext(name)[0].lower() == stem:
                return full
    except OSError:
        pass
    return None

def _file_variants(phrase):
    """The names worth searching for, most specific first.

    The lead and the trail have to come off TOGETHER as well as separately.
    Stripping them only independently -- which is what this did -- turns "open
    my test file" into the candidates {"my test file", "my test", "test file"}
    and never once into "test", so test.txt sitting in the home directory was
    reported as not found. Both words are exactly what a person puts around a
    filename when they say it out loud, so the combination is the common case,
    not an edge one.

    Stripping also repeats: "open my notes document please" carries two trailing
    words, and one pass leaves the second one attached."""
    base = re.sub(r'\s+', ' ', (phrase or "")).strip().strip('"\'')

    def strip_all(text):
        """Peel every leading article and trailing filler word, not just one."""
        previous = None
        while previous != text:
            previous = text
            text = RE_FILE_TRAIL.sub('', RE_FILE_LEAD.sub('', text)).strip()
        return text

    candidates = (base,
                  RE_FILE_TRAIL.sub('', base),
                  RE_FILE_LEAD.sub('', base),
                  strip_all(base))
    variants = []
    for candidate in candidates:
        candidate = candidate.strip().lower()
        if len(candidate) >= 2 and candidate not in variants:
            variants.append(candidate)
    return variants

# Devanagari spelled out in Latin letters, for filenames said in Hindi.
#
# A student speaking Hindi says the name of an English-named file in Devanagari,
# because that is what Whisper writes their speech as. In the logs, asking for
# gravity.mp4 in Hindi reached the search as "ग्रेविटी" and found nothing, twice
# over -- she then explained she could not play videos, which was not true and
# was only ever a rationalisation of the file not having been found. Asking for
# the same file in English opened it immediately.
#
# The mapping is deliberately rough. It is not a transliteration scheme --
# "ग्रेविटी" comes out "greviti", not "gravity" -- it only has to land close
# enough for the fuzzy tier in _match_score to recognise the word.
DEVANAGARI_LATIN = {
    'क': 'k', 'ख': 'kh', 'ग': 'g', 'घ': 'gh', 'ङ': 'n', 'च': 'ch', 'छ': 'chh',
    'ज': 'j', 'झ': 'jh', 'ञ': 'n', 'ट': 't', 'ठ': 'th', 'ड': 'd', 'ढ': 'dh',
    'ण': 'n', 'त': 't', 'थ': 'th', 'द': 'd', 'ध': 'dh', 'न': 'n', 'प': 'p',
    'फ': 'ph', 'ब': 'b', 'भ': 'bh', 'म': 'm', 'य': 'y', 'र': 'r', 'ल': 'l',
    'व': 'v', 'श': 'sh', 'ष': 'sh', 'स': 's', 'ह': 'h', 'ळ': 'l',
    'अ': 'a', 'आ': 'aa', 'इ': 'i', 'ई': 'ee', 'उ': 'u', 'ऊ': 'oo', 'ऋ': 'ri',
    'ए': 'e', 'ऐ': 'ai', 'ओ': 'o', 'औ': 'au',
    'ा': 'a', 'ि': 'i', 'ी': 'i', 'ु': 'u', 'ू': 'u', 'ृ': 'ri', 'े': 'e',
    'ै': 'ai', 'ो': 'o', 'ौ': 'au', 'ं': 'n', 'ः': 'h', 'ँ': 'n', '़': '',
    'ॉ': 'o', 'ॅ': 'a',
    '्': '',   # virama: it kills the inherent vowel, so it maps to nothing
}
# Consonants carry an inherent "a" unless a virama or another vowel follows.
_DEV_CONSONANTS = set('कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहळ')
_DEV_VIRAMA = '्'

def transliterate_devanagari(text):
    """Devanagari to rough Latin. '' when there is nothing to convert."""
    if not text or not assistant.RE_DEVANAGARI.search(text):
        return ""
    chars = list(text)
    out = []
    for i, ch in enumerate(chars):
        if ch not in DEVANAGARI_LATIN:
            out.append(ch)
            continue
        out.append(DEVANAGARI_LATIN[ch])
        if ch in _DEV_CONSONANTS:
            nxt = chars[i + 1] if i + 1 < len(chars) else ''
            # Nothing after it, or another consonant: the inherent vowel is
            # sounded. A matra or a virama replaces it, so it is not.
            if not (nxt == _DEV_VIRAMA or
                    (nxt in DEVANAGARI_LATIN and nxt not in _DEV_CONSONANTS)):
                out.append('a')
    return re.sub(r'\s+', ' ', ''.join(out)).strip()

# How alike a transliterated query and a filename have to be to count as the
# same word. "greviti" against "gravity" scores about 0.71, and pairs that are
# genuinely different words sit well below that -- so the bar is set just under
# the real match rather than at a round number.
FUZZY_MATCH_RATIO = float(os.getenv("FUZZY_MATCH_RATIO", "0.68"))

def _match_score(filename, query, fuzzy=False):
    """0 = exact, 1 = prefix, 2 = contained, 3 = close enough, None = no match.

    `fuzzy` is set only for a query that came out of transliterate_devanagari,
    where the spelling is approximate by construction and an exact test rejects
    every one of them. A Latin query the student actually said is never matched
    loosely: that would start opening the wrong file.

    The extension is scored separately from the stem so that "open notes" finds
    notes.txt at the same rank as a file literally called "notes" -- the student
    is saying a name out loud, and names spoken out loud have no extension."""
    name = filename.lower()
    stem = os.path.splitext(name)[0]
    if any(ch in query for ch in "*?"):
        return 0 if fnmatch.fnmatch(name, query) or fnmatch.fnmatch(stem, query) else None
    if name == query or stem == query:
        return 0
    if name.startswith(query) or stem.startswith(query):
        return 1
    if query in name:
        return 2
    if fuzzy and len(query) >= 4:
        if difflib.SequenceMatcher(None, stem, query).ratio() >= FUZZY_MATCH_RATIO:
            return 3
    return None

def find_file(phrase):
    """Best match for a spoken filename under $HOME, or None."""
    matches = find_files(phrase, limit=1)
    return matches[0] if matches else None

def find_files(phrase, limit=3):
    """Ranked matches for a spoken filename under $HOME; [] if none.

    Ranked rather than "first hit wins": os.walk's order is arbitrary, so first
    hit means "open notes" can land on notes_backup_old.txt while notes.txt sits
    one directory away. Rank is (how exactly the name matches, how shallow the
    path is, alphabetical), which is as close to what a person means by "the
    obvious one" as this can get without asking."""
    variants = _file_variants(phrase)
    # The same name transliterated, when it was said in Hindi. Appended AFTER
    # the literal variants and matched loosely, so an exact Latin hit always
    # outranks an approximate one and the fuzzy tier only ever decides cases
    # that would otherwise have found nothing at all.
    hindi = phrase or ""
    for _ in range(3):     # "मेरी ग्रेविटी नाम की फाइल" carries both ends
        stripped = RE_FILE_LEAD_HI.sub('', RE_FILE_TRAIL_HI.sub('', hindi)).strip()
        if stripped == hindi:
            break
        hindi = stripped
    fuzzy_variants = [v for v in _file_variants(transliterate_devanagari(hindi))
                      if v not in variants]
    search = [(v, False) for v in variants] + [(v, True) for v in fuzzy_variants]
    if not search:
        return None
    # Rejected before the search, not after it, so a traversal attempt never
    # touches the disk at all.
    if looks_like_path(phrase):
        print(f"[ACTION] Refusing path-like filename: {phrase!r}", flush=True)
        return None

    deadline = time.time() + FILE_SEARCH_MAX_SECONDS
    found = []             # [(key, path)], sorted at the end
    for root, dirs, files in os.walk(HOME_DIR):
        if time.time() > deadline:
            print("[ACTION] File search hit its time limit.", flush=True)
            break
        depth = root[len(HOME_DIR):].count(os.sep)
        if depth >= FILE_SEARCH_MAX_DEPTH:
            dirs[:] = []
        # In place: os.walk only honours pruning done to the list it handed us.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in FILE_SKIP_DIRS]
        for name in files:
            for rank, (query, fuzzy) in enumerate(search):
                score = _match_score(name, query, fuzzy)
                if score is None:
                    continue
                # rank keeps "my notes" ahead of the fallback "notes".
                key = (rank * 3 + score, depth, name.lower())
                found.append((key, os.path.join(root, name)))
                break

    out = []
    for _key, candidate in sorted(found, key=lambda item: item[0]):
        path = os.path.realpath(candidate)
        # A symlink pointing out of $HOME is the one way a legitimate-looking
        # name can still reach /etc, so the check is on the resolved path.
        if not (path == HOME_DIR or path.startswith(HOME_DIR + os.sep)):
            print(f"[ACTION] {candidate} resolves outside $HOME; refusing.", flush=True)
            continue
        if path not in out:
            out.append(path)
        if len(out) >= limit:
            break
    return out

# ---------- opening a file fullscreen ----------
# Viewers that can go fullscreen themselves, keyed on the binary their .desktop
# Exec line names. Asked on the command line rather than by fullscreening the
# window afterwards, because afterwards is not available here: this Pi runs a
# wlroots Wayland compositor, and the usual tool for the job (wmctrl, EWMH over
# X11) cannot see a single window on it -- verified, `wmctrl -lp` lists nothing,
# not even Liza's own Tk window. The viewer's own flag works on X11 and Wayland
# alike and needs nothing installed.
#
# A viewer that is not in here still opens, just in a normal window. That is a
# missing nicety, not a failure, so it must never block the open.
FULLSCREEN_FLAGS = {
    "evince": "--fullscreen", "atril": "--fullscreen", "okular": "--presentation",
    "xpdf": "-fullscreen", "mupdf": "-f", "qpdfview": "--fullscreen",
    "zathura": "--mode=fullscreen",
    "eog": "--fullscreen", "eom": "--fullscreen", "feh": "--fullscreen",
    "ristretto": "--fullscreen",
    "mpv": "--fs", "vlc": "--fullscreen", "totem": "--fullscreen",
}

DESKTOP_DIRS = ("/usr/share/applications", "/usr/local/share/applications",
                os.path.expanduser("~/.local/share/applications"))

def _desktop_exec_binary(desktop_id):
    """The binary a .desktop file actually runs, or None."""
    for directory in DESKTOP_DIRS:
        candidate = os.path.join(directory, desktop_id)
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Exec="):
                        parts = line[5:].split()
                        if parts:
                            return os.path.basename(parts[0])
        except OSError:
            pass
    return None

def file_mime(path):
    """The MIME type xdg thinks this file is, or "" if it cannot say."""
    try:
        return subprocess.run(["xdg-mime", "query", "filetype", path],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""

def media_kind_of(path):
    """'music', 'video', or None -- is opening this going to make a noise?"""
    mime = file_mime(path)
    if mime.startswith("audio/"):
        return "music"
    if mime.startswith("video/"):
        return "video"
    return None

def fullscreen_viewer_command(path):
    """[binary, flag, path] when the default viewer can open fullscreen itself.

    None means "no idea" -- the caller falls back to xdg-open, which is what
    always happened before and still opens the file, just windowed."""
    mime = file_mime(path)
    if not mime:
        return None
    try:
        desktop_id = subprocess.run(["xdg-mime", "query", "default", mime],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    binary = _desktop_exec_binary(desktop_id) if desktop_id else None
    flag = FULLSCREEN_FLAGS.get(binary or "")
    if not flag or shutil.which(binary) is None:
        return None
    command = [binary, flag]
    if binary == "mpv":
        # Same shared dmix device as her voice and the YouTube path. Left to its
        # own devices mpv opens the ALSA default, which on this Pi is a card
        # that takes exclusive use -- so a local audio file would either fail to
        # open the speaker or hold it, and Liza would go silent for as long as
        # it played. dmix mixes instead, so she can still talk over it and, more
        # to the point, still answer "close it".
        command.append(f"--audio-device={MPV_AUDIO_DEVICE}")
    return command + [path]

# ---------- the actions themselves ----------
def open_file_action(phrase, path=None):
    """('ok', path) or (reason, detail). Opens with the system default app.

    `path` skips the search entirely, for the case where the file has ALREADY
    been found and named to the student -- see the "shall I open it?" flow. Re-
    searching there would be both wasteful and wrong: the walk is ranked, and
    nothing guarantees it lands on the same file it just offered."""
    if path is None:
        if not phrase:
            return "no_name", ""
        # A plain subfolder path is resolved before the refusal, not after it:
        # "sample/gravity" is where the file actually is, not an escape attempt.
        path = resolve_relative_name(phrase)
        if path is None:
            if looks_like_path(phrase):
                # A SAFE relative name that simply did not resolve is still not
                # an escape attempt -- it is usually the right file under a
                # half-remembered folder. The log has "sample/gravity" for a
                # file that lives in Media/samples, which is the folder the
                # student named and one level off. So the last word gets the
                # ordinary recursive search before anything is refused; only a
                # genuinely path-like name ("..", "/etc", "~") falls through.
                if safe_relative_name(phrase):
                    path = find_file(os.path.basename(phrase.rstrip("/")))
                if path is None:
                    print(f"[ACTION] Refusing path-like filename: {phrase!r}",
                          flush=True)
                    return "unsafe", ""
            else:
                path = find_file(phrase)
    if path is None:
        return "not_found", phrase
    if not os.access(path, os.R_OK):
        return "no_permission", os.path.basename(path)
    # The viewer's own fullscreen flag when it has one, xdg-open otherwise.
    # A 480px-tall touchscreen with no keyboard is not a place to read a PDF in
    # a floating window, and there is no window manager here to maximise it
    # afterwards -- see fullscreen_viewer_command().
    command = fullscreen_viewer_command(path) or ["xdg-open", path]
    try:
        # stdin MUST be detached. xdg-open execs the real viewer in place, so
        # without this the viewer inherits Liza's own stdin and starts eating
        # it -- in headless mode that is the student's next typed command,
        # swallowed with nothing to show for it. Observed, not theoretical.
        # The inherited PDEATHSIG survives that exec too, so a viewer opened
        # this way closes when Liza does, exactly like mpv.
        opener = subprocess.Popen(command,
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  preexec_fn=_die_with_parent)
    except Exception as exc:
        print(f"[ACTION ERROR] {command[0]} failed: {exc}", flush=True)
        return "open_failed", os.path.basename(path)

    # Give it a moment to fall over. With a viewer attached the process stays
    # alive for as long as the window is open, so "still running" is the success
    # case; a quick non-zero exit is no handler, no display, or a refusal, and
    # saying "opening your notes" over the top of that is a lie the student can
    # see. A viewer launched directly may also exit 0 immediately after handing
    # the file to an already-running instance of itself, which is still success.
    deadline = time.time() + 1.0
    while time.time() < deadline:
        status = opener.poll()
        if status is None:
            time.sleep(0.05)
            continue
        if status != 0:
            print(f"[ACTION] {command[0]} exited {status} for {path}.", flush=True)
            return "open_failed", os.path.basename(path)
        break
    with device_state_lock:
        state.currently_open_file = path

    # An audio or video file makes exactly as much noise as a YouTube track, so
    # it has to count as media, not merely as an open file. Registered here it
    # inherits everything that path already has: the microphone stays shut while
    # it plays, "Liza, stop" breaks in, and the Stop button reaches it.
    #
    # Without this the file's own soundtrack came straight back through the mic
    # and was answered as if the student had said it -- observed, verbatim:
    # "[TRANSCRIPT] This is a sample audio file. Gravity pulls every mass toward
    # every other mass." followed by Liza agreeing with it at length.
    kind = media_kind_of(path)
    if kind:
        # Whatever was already making noise has to go first, exactly as
        # start_media_playback() does it -- two soundtracks on one speaker, and
        # state.media_procs can only describe one of them anyway.
        if media_active.is_set():
            stop_media_playback()
        with subprocess_lock:
            state.active_subprocesses.append(opener)
            state.media_procs[:] = [opener]
        media_active.set()
        note_media_started()
        set_playing_state(os.path.basename(path), kind)
        ui_call(lambda t=os.path.basename(path): state.ui_instance.set_now_playing(t))

    threading.Thread(target=watch_open_file, args=(path, opener, kind),
                     daemon=True).start()
    how = "fullscreen" if command[0] != "xdg-open" else "windowed"
    print(f"[ACTION] Opened {path} ({how}, via {command[0]}"
          f"{', as ' + kind if kind else ''})", flush=True)
    return "ok", path

def _pids_holding(path):
    """Every process of ours whose command line names `path`.

    xdg-open is a shell script that execs the real viewer and exits, so its PID
    is not the thing holding the file and cannot be kept for later. /proc is
    read directly rather than shelling out to pgrep: no quoting to get wrong,
    and no chance of a pattern matching more than it was meant to."""
    me = os.getpid()
    uid = os.getuid()
    hits = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        # The FULL path only. Matching the bare name would put every shell that
        # ever echoed "notes.txt" in range of a SIGTERM.
        if path and path in cmdline:
            hits.append(int(entry))
    return hits

def watch_open_file(path, opener, kind=None):
    """Clear CURRENTLY_OPEN_FILE once nothing is showing the file any more.

    Without this the state only ever changes when somebody says "close it": a
    five-second audio clip that finished on its own, or a window the student
    shut themselves, left Liza believing a file was still open for the rest of
    the session. She then answers questions about the device from a state that
    stopped being true minutes ago -- which is exactly what CURRENTLY_OPEN_FILE
    exists to prevent.

    The launcher is waited on first because for a direct launch it IS the
    viewer, so that wait is the precise answer. The poll afterwards covers
    xdg-open, which exits as soon as it has handed the file over and says
    nothing about the viewer it started. The grace period is there because that
    hand-off is not instant, and a viewer that has not appeared yet looks
    exactly like one that has already gone."""
    try:
        opener.wait()
    except Exception:
        pass

    superseded = False
    grace = time.time() + OPEN_FILE_GRACE_S
    while True:
        if not _pids_holding(path) and time.time() > grace:
            break
        with device_state_lock:
            if state.currently_open_file != path:
                superseded = True   # closed, or another file took the slot
                break
        time.sleep(OPEN_FILE_POLL_S)

    # The media registration is torn down FIRST and unconditionally, because it
    # belongs to this opener and to nobody else. It used to hang off the same
    # early return as everything below, which meant an explicit "close it" --
    # which clears currently_open_file before this thread ever looks -- sent the
    # watcher down the superseded path and media_active was never cleared. The
    # microphone then stayed in barge-in mode for the rest of the session with
    # nothing playing: Liza deaf, for good, after successfully closing a video.
    if kind:
        with subprocess_lock:
            if opener in state.active_subprocesses:
                state.active_subprocesses.remove(opener)
            if state.media_procs == [opener]:
                state.media_procs[:] = []
        # Only if this opener is still the thing that owns the media state --
        # something newer may legitimately have taken it over.
        if not state.media_procs:
            media_active.clear()
            set_playing_state(None)
            ui_call(lambda: state.ui_instance.set_now_playing(None))

    if superseded:
        return
    with device_state_lock:
        if state.currently_open_file != path:
            return
        state.currently_open_file = None
    print(f"[ACTION] {os.path.basename(path)} was closed from outside; "
          f"state cleared.", flush=True)

# How long to allow for a viewer to actually appear before "no process is
# holding this" is believed, and how often to look afterwards. The poll walks
# /proc, so it is deliberately unhurried -- nothing depends on noticing within
# the second.
OPEN_FILE_GRACE_S = 5.0
OPEN_FILE_POLL_S = 3.0

def close_file_action():
    """('ok'|'nothing'|'close_failed', filename). Clears the state either way."""
    with device_state_lock:
        path = state.currently_open_file
    if not path:
        return "nothing", ""

    name = os.path.basename(path)
    pids = _pids_holding(path)
    for pid in pids:
        try: os.kill(pid, signal.SIGTERM)
        except OSError: pass
    deadline = time.time() + 1.0
    while time.time() < deadline and _pids_holding(path):
        time.sleep(0.05)
    for pid in _pids_holding(path):
        try: os.kill(pid, signal.SIGKILL)
        except OSError: pass

    # Cleared whether or not anything was actually killed. A viewer this process
    # cannot reach is a viewer it will never reach, and leaving the state set
    # means every later "close it" tries again and fails again.
    with device_state_lock:
        state.currently_open_file = None

    if not pids:
        # Nothing was holding it, which is not a failure -- it is the ordinary
        # end of a short audio or video file, or a window the student closed
        # themselves. Reported as a failure (what this used to do) she says "I
        # couldn't close welcome.wav, you may need to close it yourself" about a
        # clip that finished playing eight seconds ago, which is both wrong and
        # visibly wrong. The wanted state is the actual state, so this is
        # success and she says nothing further -- she has already said "closing
        # it", and it is closed.
        print(f"[ACTION] {name} was already closed.", flush=True)
        return "ok", name

    # Still there after a SIGTERM and a SIGKILL: genuinely unkillable, and worth
    # telling the student about, because only they can deal with it now.
    survivors = _pids_holding(path)
    if survivors:
        print(f"[ACTION] {name} still held by {survivors} after SIGKILL.", flush=True)
        return "close_failed", name

    print(f"[ACTION] Closed {name} (pids {pids}).", flush=True)
    return "ok", name

def set_ui_mode_action(mode):
    """('ok'|'already', mode). Widgets off and the mascot fullscreen, or back."""
    mode = "3d" if mode in ("3d", "3-d", "three_d", "mascot") else "normal"
    with device_state_lock:
        if state.current_ui_mode == mode:
            return "already", mode
        state.current_ui_mode = mode
    ui_invoke("set_ui_mode", mode)
    print(f"[ACTION] UI mode -> {mode}", flush=True)
    return "ok", mode

def sleep_action():
    ui_invoke("go_to_sleep")
    print("[ACTION] Going to sleep on request.", flush=True)
    return "ok", ""

# Spoken only when an action could not be carried out. Success needs nothing
# said here -- she has already said it, which is the whole point of the tag
# coming after the sentence rather than instead of it.
# ---------- listing what is actually there ----------
# SHE COULD ALWAYS FIND A FILE AND NEVER LOOK AT ONE, and that gap is what put
# "I can't directly check for files or list them" in the logs three times over,
# in front of a working find_files(). The model was telling the truth about the
# tags it had: open_file needs a name to search FOR, so a question like "is
# there a gravity file on my system" had nothing behind it and got refused.
LIST_MAX_ENTRIES = int(os.getenv("LIST_MAX_ENTRIES", "40"))
# Read out loud, so the answer has to be short enough to listen to. Beyond this
# she says how many there are instead of naming every one.
LIST_SPEAK_LIMIT = int(os.getenv("LIST_SPEAK_LIMIT", "12"))

def list_directory_action(phrase):
    """('listing', text) with what is in a folder, or (reason, detail).

    A bare tag lists the home directory. A name looks the folder up the same way
    open_file does, so "list my documents" and "what is in Media" both work."""
    target = HOME_DIR
    label = "your home folder"
    phrase = (phrase or "").strip()
    if phrase and phrase.lower() not in ("home", "~", "my home", "home folder"):
        if looks_like_path(phrase):
            print(f"[ACTION] Refusing path-like folder: {phrase!r}", flush=True)
            return "unsafe", ""
        found = find_directory(phrase)
        if found is None:
            # NOT A FOLDER, SO IT WAS A QUESTION ABOUT A FILE. os.listdir only
            # ever sees one level, and answering "is there a gravity file?" from
            # the top of the home folder is how she came to say there was none
            # while gravity.mp4 sat in Media/samples -- and then refused to even
            # try opening it, because she had just "checked". The recursive
            # search is the one that actually answers the question asked.
            hits = find_files(phrase, limit=5) or []
            if not hits:
                return "not_found", phrase
            named = "; ".join(
                f"{os.path.basename(h)} in "
                f"{os.path.relpath(os.path.dirname(h), HOME_DIR) or 'your home folder'}"
                for h in hits)
            print(f"[ACTION] Found {len(hits)} matching {phrase!r}.", flush=True)
            return "listing", (f"Yes -- {named}." if len(hits) == 1
                               else f"Yes, {len(hits)} of them: {named}.")
        target, label = found, os.path.basename(found)

    try:
        names = sorted(os.listdir(target))
    except OSError as exc:
        print(f"[ACTION] Cannot list {target}: {exc}", flush=True)
        return "no_permission", label

    names = [n for n in names if not n.startswith(".")][:LIST_MAX_ENTRIES]
    if not names:
        return "listing", f"{label} is empty."

    folders = [n for n in names if os.path.isdir(os.path.join(target, n))]
    files = [n for n in names if n not in folders]
    print(f"[ACTION] Listed {target}: {len(folders)} folders, {len(files)} files.",
          flush=True)

    def phrase_for(items, singular, plural):
        if not items:
            return ""
        if len(items) > LIST_SPEAK_LIMIT:
            return f"{len(items)} {plural}"
        named = ", ".join(os.path.splitext(n)[0] if n not in folders else n
                          for n in items)
        return f"{len(items)} {singular if len(items) == 1 else plural}: {named}"

    parts = [p for p in (phrase_for(folders, "folder", "folders"),
                         phrase_for(files, "file", "files")) if p]
    return "listing", f"In {label} there is {' and '.join(parts)}."

def find_directory(phrase):
    """Best matching FOLDER under $HOME, or None. Mirrors find_files' ranking."""
    variants = _file_variants(phrase)
    fuzzy_variants = [v for v in _file_variants(transliterate_devanagari(phrase))
                      if v not in variants]
    search = [(v, False) for v in variants] + [(v, True) for v in fuzzy_variants]
    if not search or looks_like_path(phrase):
        return None
    deadline = time.time() + FILE_SEARCH_MAX_SECONDS
    best = None
    for root, dirs, _files in os.walk(HOME_DIR):
        if time.time() > deadline:
            break
        depth = root[len(HOME_DIR):].count(os.sep)
        if depth >= FILE_SEARCH_MAX_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in FILE_SKIP_DIRS]
        for name in dirs:
            for rank, (query, fuzzy) in enumerate(search):
                score = _match_score(name, query, fuzzy)
                if score is None:
                    continue
                key = (rank * 3 + score, depth, name.lower())
                if best is None or key < best[0]:
                    best = (key, os.path.join(root, name))
                break
    if best is None:
        return None
    path = os.path.realpath(best[1])
    if not (path == HOME_DIR or path.startswith(HOME_DIR + os.sep)):
        return None
    return path

# ---------- running a command on the device ----------
# SUDO IS THE LINE, and it is drawn here rather than in the prompt because a
# prompt rule is a request and this has to be a guarantee. Everything else the
# student can type into their own terminal, they can now say out loud.
#
# Checked per SEGMENT, not once over the whole string: "date; sudo reboot" is
# two commands and only the second one matters, so a test against the first
# word of the line would wave it straight through.
RE_SHELL_SPLIT = re.compile(r'(?:\|\||&&|[;|&\n])')
# Privilege escalation, and the handful of things that take the machine or the
# disk with them. Not an attempt at a sandbox -- it is the student's own device
# and their own shell -- just the commands where being misheard is unrecoverable.
BLOCKED_COMMANDS = {
    "sudo", "su", "pkexec", "doas", "runuser",
    "mkfs", "mkfs.ext4", "mkfs.vfat", "fdisk", "parted", "sfdisk",
    "shutdown", "poweroff", "halt", "reboot", "init",
    "passwd", "chpasswd", "useradd", "userdel", "usermod", "visudo",
}
# Whole-line shapes that no first-word test catches.
RE_CATASTROPHIC = re.compile(
    # rm -rf / and every way of writing it: "/", "~", "~/", "$HOME", "/home",
    # with or without a trailing slash or star. The trailing slash is the one
    # that matters -- "rm -rf ~/" is the same command as "rm -rf ~" and an
    # earlier version of this pattern let it straight through.
    r'rm\s+(?:-\w+\s+)*(?:/|~|\$HOME|/home|/root)/?\*?\s*$|'
    r'>\s*/dev/[sn][dv][a-z]|'                    # writing over a disk
    r'\bdd\b[^|]*of=/dev/|'
    r':\(\)\s*\{.*\}\s*;\s*:|'                    # fork bomb
    r'\bchmod\s+-R\s+777\s+/\s*$',
    re.IGNORECASE)
COMMAND_TIMEOUT_S = float(os.getenv("COMMAND_TIMEOUT_S", "15"))
# Read out loud, so what comes back has to be short.
COMMAND_OUTPUT_CHARS = int(os.getenv("COMMAND_OUTPUT_CHARS", "600"))

def command_is_allowed(command):
    """(True, '') or (False, why). See BLOCKED_COMMANDS for what 'why' means."""
    if RE_CATASTROPHIC.search(command):
        return False, "destructive"
    for segment in RE_SHELL_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        # env-style prefixes ("FOO=1 sudo x") and leading parens/backticks
        words = [w for w in segment.strip("()`$ ").split() if "=" not in w.split("/")[0]]
        if not words:
            continue
        first = os.path.basename(words[0]).lower()
        if first in BLOCKED_COMMANDS:
            return False, "sudo" if first in ("sudo", "su", "pkexec", "doas",
                                              "runuser") else "destructive"
    return True, ""

def run_command_action(command):
    """('command_output', text) with what the command printed, or (reason, detail)."""
    command = (command or "").strip()
    if not command:
        return "no_command", ""
    allowed, why = command_is_allowed(command)
    if not allowed:
        print(f"[ACTION] Refused command ({why}): {command!r}", flush=True)
        return why, command
    print(f"[ACTION] Running: {command!r}", flush=True)
    try:
        done = subprocess.run(command, shell=True, cwd=HOME_DIR, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "command_timeout", command
    except Exception as exc:
        print(f"[ACTION] Command failed to start: {exc}", flush=True)
        return "command_failed", command
    output = (done.stdout or "").strip()
    print(f"[ACTION] Exit {done.returncode}, {len(output)} bytes of output.", flush=True)
    if not output:
        return ("command_output", "Done." if done.returncode == 0
                else f"That finished with error code {done.returncode}.")
    if len(output) > COMMAND_OUTPUT_CHARS:
        output = output[:COMMAND_OUTPUT_CHARS].rsplit("\n", 1)[0] + " ... and more."
    return "command_output", output

ACTION_FAILURES = {
    "not_found":     {"en": "I couldn't find a file called {d}.",
                      "hi": "{d} नाम की कोई फ़ाइल नहीं मिली।",
                      "hinglish": "{d} नाम की कोई file नहीं मिली।"},
    "no_name":       {"en": "Tell me the file name and I'll open it.",
                      "hi": "फ़ाइल का नाम बताइए, मैं खोल देती हूँ।",
                      "hinglish": "File का नाम बताओ, मैं open कर देती हूँ।"},
    "unsafe":        {"en": "I can only open files in your home folder.",
                      "hi": "मैं सिर्फ़ आपके होम फ़ोल्डर की फ़ाइलें खोल सकती हूँ।",
                      "hinglish": "मैं सिर्फ़ आपके home folder की files खोल सकती हूँ।"},
    "no_permission": {"en": "I don't have permission to open {d}.",
                      "hi": "{d} खोलने की अनुमति मेरे पास नहीं है।",
                      "hinglish": "{d} open करने की permission मेरे पास नहीं है।"},
    "open_failed":   {"en": "I couldn't open {d}.",
                      "hi": "मैं {d} नहीं खोल पाई।",
                      "hinglish": "मैं {d} open नहीं कर पाई।"},
    "nothing":       {"en": "No file is open right now.",
                      "hi": "अभी कोई फ़ाइल खुली नहीं है।",
                      "hinglish": "अभी कोई file खुली नहीं है।"},
    "close_failed":  {"en": "I couldn't close {d}. You may need to close it yourself.",
                      "hi": "मैं {d} बंद नहीं कर पाई। आपको खुद बंद करना पड़ेगा।",
                      "hinglish": "मैं {d} close नहीं कर पाई, आपको खुद करना पड़ेगा।"},
    "not_playing":   {"en": "Nothing is playing right now.",
                      "hi": "अभी कुछ नहीं चल रहा।",
                      "hinglish": "अभी कुछ भी play नहीं हो रहा।"},
    "no_command":    {"en": "Tell me what to run and I'll do it.",
                      "hi": "क्या चलाना है बताइए, मैं कर देती हूँ।",
                      "hinglish": "क्या run करना है बताओ, मैं कर देती हूँ।"},
    "sudo":          {"en": "That one needs admin rights, so I can't run it. Everything else I can.",
                      "hi": "उसके लिए एडमिन अधिकार चाहिए, वह मैं नहीं चला सकती। बाकी सब चला सकती हूँ।",
                      "hinglish": "Uske liye admin rights chahiye, wo main नहीं चला सकती। बाकी सब कर सकती हूँ।"},
    "destructive":   {"en": "I won't run that one -- it could wipe something you can't get back.",
                      "hi": "वह मैं नहीं चलाऊँगी, उससे कुछ ऐसा मिट सकता है जो वापस नहीं आएगा।",
                      "hinglish": "Wo main नहीं चलाऊँगी, usse कुछ ऐसा delete हो सकता है jo वापस नहीं आएगा।"},
    "command_timeout": {"en": "That took too long, so I stopped it.",
                      "hi": "उसमें बहुत समय लग रहा था, मैंने रोक दिया।",
                      "hinglish": "Usme बहुत time लग रहा था, मैंने रोक दिया।"},
    "command_failed": {"en": "I couldn't run that one.",
                      "hi": "मैं वह नहीं चला पाई।",
                      "hinglish": "मैं wo run नहीं कर पाई।"},
    "unknown":       {"en": "I can't do that one yet.",
                      "hi": "यह काम मैं अभी नहीं कर सकती।",
                      "hinglish": "यह काम मैं अभी नहीं कर सकती।"},
}

def action_failure_sentence(reason, detail, language):
    wording = ACTION_FAILURES.get(reason)
    if not wording:
        return ""
    return wording.get(language, wording["en"]).format(d=detail or "")

# Actions that come back with DATA rather than with a yes or a no. Marked on the
# way out so the ACT block can tell "here is what the folder holds" apart from
# "I couldn't find it", which are both non-empty strings by the time they arrive.
ACTION_DATA_PREFIX = "\x00DATA\x00"

# Raw output is not speech, and reading it out is the difference between an
# assistant and a terminal. `df -h` comes back as "/dev/mmcblk0p2 29G 22G 6.2G
# 78% /", and `hostname -I` as an IPv4 followed by a full IPv6 address -- read
# aloud, letter by letter, that is unusable. So it goes back through the model
# once for a sentence a person would actually say.
#
# A SEPARATE, TINY PROMPT rather than the 12,000-character system one: this is
# the second call of the turn and the student is already waiting on it, so it
# carries only what the job needs.
RESULT_PHRASING_PROMPT = {
    "en": "Reply in English.",
    "hi": "Reply in Hindi, in Devanagari script.",
    "hinglish": "Reply in Hinglish -- Hindi in Latin letters, mixing in the "
                "English words a person would actually use.",
}

def phrase_action_result(asked_for, raw, language="en"):
    """One spoken sentence describing `raw`, or `raw` itself if that fails.

    Falling back to the raw text matters: a failed rewrite must not turn a
    working command into silence, and awkward speech beats no answer."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    instruction = RESULT_PHRASING_PROMPT.get(language, RESULT_PHRASING_PROMPT["en"])
    try:
        done = assistant.openrouter_client.with_options(max_retries=0).chat.completions.create(
            model=assistant.LLM_MODEL,
            messages=[{"role": "system", "content":
                       "You turn the output of a command into one or two spoken "
                       "sentences for a voice assistant. Say what it MEANS, not "
                       "what it printed. No markdown, no lists, no file paths or "
                       "device names unless they are the answer. A short address "
                       "or number IS the answer when that is what was asked for, "
                       "so say it; only skip the long unreadable ones, like an "
                       "IPv6 address, and say you are skipping it. " + instruction},
                      {"role": "user", "content":
                       f"The student asked for: {asked_for}\n"
                       f"The device returned:\n{raw}\n\n"
                       f"Say what this tells them, briefly."}],
            max_tokens=160, temperature=0.3,
            extra_body={"reasoning": {"enabled": False}})
        spoken = (done.choices[0].message.content or "").strip()
        return spoken or raw
    except Exception as exc:
        print(f"[ACTION] Could not phrase the result ({exc}); "
              f"reading it as it came.", flush=True)
        return raw

def execute_action(name, param, language="en"):
    """Carry out one parsed tag. Returns a sentence to speak, or "" on success.

    Runs on ai_loop's thread, after her confirmation has finished speaking --
    never on the Tk thread, and never mid-reply."""
    reason, detail = "unknown", ""
    if name == "stop_media":
        reason, detail = ("ok", "") if stop_media_playback() else ("not_playing", "")
    elif name == "open_file":
        reason, detail = open_file_action(param)
    elif name == "close_file":
        reason, detail = close_file_action()
    elif name == "ui_mode":
        reason, detail = set_ui_mode_action(param)
    elif name == "sleep":
        reason, detail = sleep_action()
    elif name == "list_files":
        reason, detail = list_directory_action(param)
    elif name == "run_command":
        reason, detail = run_command_action(param)
    else:
        print(f"[ACTION] Unknown action {name!r}.", flush=True)

    # "already in 3D mode" is the model being told something it should have
    # known from the state block, not a failure worth interrupting her for.
    if reason in ("ok", "already"):
        return ""
    # Not every action fails or stays silent. These two RETURN something -- the
    # contents of a folder, the output of a command -- and that text is the
    # whole point of running them, so it is spoken as it comes back rather than
    # being looked up as an error.
    if reason in ("listing", "command_output"):
        return ACTION_DATA_PREFIX + detail
    return action_failure_sentence(reason, detail, language)

# Actions that END something, and so must not wait for her to finish speaking.
# See the ACT block in ai_loop() for why the rest still do.
IMMEDIATE_ACTIONS = {"stop_media", "close_file"}

def device_state_block():
    """The three CURRENT_* lines rule 7 reasons over, for the prompt's tail."""
    playing, open_file, ui_mode = get_device_state()
    if playing:
        now = f'{{"title": "{playing["title"]}", "kind": "{playing["kind"]}"}}'
    else:
        now = "None (nothing is playing)"
    return (f"CURRENTLY_PLAYING: {now}\n"
            f"CURRENTLY_OPEN_FILE: {open_file or 'None (no file is open)'}\n"
            f"CURRENT_UI_MODE: {ui_mode}")

def student_profile_block():
    """Who she is teaching and how deep to pitch it. "" when nobody is set up.

    Read from the profile store on EVERY turn rather than cached at session
    start. Switch User has to change the depth of the very next answer without a
    restart, and this is a few hundred bytes of JSON on a turn that is already
    waiting on a Whisper call and an LLM call -- the read is free by comparison.

    Empty string when there is no profile, which is what keeps the device
    behaving exactly as it did before this feature until somebody makes one.
    """
    try:
        profile = profiles.get_active_profile()
    except Exception as exc:
        print(f"[PROFILE] Could not read the profile store: {exc}", flush=True)
        return ""
    if not profile:
        return ""
    instruction = profiles.band_instruction(profile.get("class"))
    if not instruction:
        # KG, or a class that failed to parse. KG students never reach this
        # prompt at all -- they are routed to the spelling and story screens.
        return ""
    # The whole section, header included, so that a device with no profile on it
    # emits nothing at all here rather than an empty heading -- see the caller.
    return (
        "### 1a. WHO YOU ARE TEACHING (CRITICAL OVERRIDE -- GOVERNS DEPTH, NOT BEHAVIOUR)\n"
        f"You are teaching {profile.get('name') or 'a student'}, "
        f"who is in Class {profile.get('class')}.\n"
        f"{instruction}\n"
        "This sets HOW DEEP and HOW PLAIN the answer is. The mode below still decides HOW\n"
        "you teach -- explaining, asking, or examining -- and rule 5 still decides how you\n"
        "speak. The sentence ceiling here is a maximum, never a target: a one-line question\n"
        "still gets a one-line answer. NEVER mention the student's class, their year, or\n"
        "that you are adjusting anything; just answer at that level.\n\n"
        + learning_history_block(profile)
    )


# How confusion is spotted, for deciding whether a concept goes down as met or
# as struggled-with. Deliberately narrow: only an explicit admission counts, so
# an ordinary question never marks a child down.
RE_STUCK = re.compile(
    r"\b(i don'?t (understand|get|know)|i'?m confused|makes no sense|"
    r"still don'?t|can'?t do|too hard|explain again|didn'?t understand)\b",
    re.IGNORECASE)


# The concept of the turn being answered right now. Set by note_learning, which
# runs before the prompt is built, and read by learning_history_block so the gap
# advice is about the question actually being asked.
_current_concept = None


def note_learning(text):
    """Record which concept a question touched, and how it seemed to go.

    Advisory only: a missed concept, a student with no profile, or a database
    that is down all simply mean nothing is written. Nothing downstream depends
    on this succeeding.
    """
    try:
        user_id = assistant.active_user_id()
        if not user_id:
            return
        slug = store.detect_concept(text)
        if not slug:
            return
        outcome = "struggling" if RE_STUCK.search(text or "") else "met"
        global _current_concept
        _current_concept = slug
        if store.record_concept(user_id, slug, outcome):
            print(f"[LEARN] {slug} ({outcome})", flush=True)
    except Exception as exc:
        print(f"[LEARN] Could not record the concept ({exc}).", flush=True)


def learning_history_block(profile):
    """What she knows about this student's progress, from the store.

    Empty when there is nothing recorded, or when PostgreSQL is down -- which is
    what makes the whole knowledge-graph feature strictly additive. A device with
    no store behaves exactly as it did before.
    """
    user_id = profile.get("user_id")
    if not user_id:
        return ""
    recent = store.student_summary(user_id, limit=6)
    if not recent:
        return ""
    seen = ", ".join(f"{row['name'].lower()} ({row['status']})" for row in recent)
    lines = [
        "### 1b. WHAT THIS STUDENT HAS ALREADY WORKED ON WITH YOU",
        f"Recently, most recent first: {seen}.",
        "Connect new ideas back to the ones they are confident about -- that is what a "
        "teacher who remembers them would do. NEVER read this list aloud, never say you "
        "have a record of them, and never open with what they did last time.",
    ]

    # The gap query. Asked about whatever the CURRENT question is about, falling
    # back to whatever they are struggling with -- a student who has just asked
    # about algebra needs the gaps behind algebra, not behind last week's topic.
    weak = [row for row in recent if row["status"] == "struggling"]
    target = _current_concept or (weak[0]["slug"] if weak else None)
    if target:
        try:
            student_class = int(profile.get("class"))
        except (TypeError, ValueError):
            student_class = None
        gaps = store.missing_prerequisites(user_id, target,
                                           student_class=student_class) or []
        nearest = [g["name"].lower() for g in gaps[:3]]
        if nearest:
            concept = store.concept_by_slug(target) or {}
            lines.append(
                f"Before {concept.get('name', target).lower()} they have not solidly met: "
                f"{', '.join(nearest)}. If they struggle with it now, do not re-explain "
                f"the same thing louder -- drop back to the earliest of those, check it "
                f"with one question, and build up from there."
            )
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# The back-reference, bound LAST on purpose.
#
# assistant.py imports names FROM this file, and this file calls back into the
# assistant. With `import assistant` up with the other imports, that cycle only
# resolved when the assistant happened to be imported first: `import actions` on
# its own ran this module as far as that line, handed control to the assistant,
# and the assistant's `from actions import ...` then found a module that had not
# defined anything yet. A plain ImportError, and only for whoever opened this
# file to work on it.
#
# Down here every name above is already defined, so the import resolves whichever
# module is asked for first. It binds the MODULE only -- nothing is read off it
# until something is actually called -- which is what makes that legal at all.
import assistant
