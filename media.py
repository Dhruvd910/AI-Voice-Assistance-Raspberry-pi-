"""Playing music and video, and telling when a sentence asked for it.

A leaf. It reads the shared state and can call the screen, but nothing in here
imports the assistant, so the play, pause and stop path can be changed without
reading the rest of the program. What ai_loop needs from it is imported FROM
here rather than reached back for.
"""

import ctypes
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time

from ddgs import DDGS

import state
from config import MPV_AUDIO_DEVICE
from state import media_active, note_media_started, set_playing_state, subprocess_lock
from uibridge import ui_call

# Linux prctl(PR_SET_PDEATHSIG): asks the kernel to signal a child when its
# parent dies. Without it, killing Liza (or crashing) orphans mpv onto init and
# the song keeps playing with nothing left to stop it -- atexit and cleanup
# handlers are no help there, because SIGKILL never runs them.
PR_SET_PDEATHSIG = 1

def _die_with_parent():
    """preexec_fn for media children, so playback can never outlive the app."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass
# ==========================================
# Media Playback (music / video)
# ==========================================
# Checked in Python, before the request ever reaches the LLM: "play X" is a
# deterministic device command, not something to reason about, and doing it
# here guarantees it always fires instead of depending on the model reliably
# emitting some new tag. Video phrasing is checked first since it is the more
# specific request; bare "play <topic>" with neither word is left alone so
# things like "play a guessing game" don't get hijacked into a music search.
RE_PLAY_VIDEO_LEAD = re.compile(
    r'^\s*(?:play|show|watch)\s+(?:me\s+)?(?:a\s+|the\s+)?video\s+(?:of\s+|for\s+|about\s+)?(.+)$',
    re.IGNORECASE)
RE_PLAY_VIDEO_TRAIL = re.compile(
    r'^\s*play\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(.+?)\s+video\s*$',
    re.IGNORECASE)
RE_PLAY_MUSIC_LEAD = re.compile(
    r'^\s*play\s+(?:a\s+|the\s+|some\s+)?(?:music|song)\s+(?:of\s+|for\s+|by\s+|called\s+)?(.+)$',
    re.IGNORECASE)
RE_PLAY_MUSIC_TRAIL = re.compile(
    r'^\s*play\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(.+?)\s+(?:song|music)\s*$',
    re.IGNORECASE)
# Bare "play <something>". In practice people just say "play heatwave" rather
# than "play music heatwave", and letting that fall through to the LLM is worse
# than a wrong guess: the model cheerfully answers "okay, playing Heatwave" and
# nothing plays, so Liza ends up lying about what she did.
RE_PLAY_BARE = re.compile(r'^\s*play\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(.+)$',
                          re.IGNORECASE)
# ...but "play" also introduces plenty of things that are not media at all.
# Checked anywhere in the object of the sentence so "a guessing game" is caught
# as well as "a game".
RE_NOT_MEDIA = re.compile(
    r'\b(?:game|games|quiz|puzzle|riddle|chess|cards|along|'
    r'dead|nice|fair|safe|piano|guitar|drums|violin)\b',
    re.IGNORECASE)
# A request with no actual title in it ("play a song") is genuine but has
# nothing to search for, so it goes to the LLM to ask which one.
TITLELESS = {"", "a", "an", "the", "some", "it", "that", "this", "one",
             "music", "song", "songs", "video", "videos", "something",
             # Hindi equivalents, including the spellings Whisper actually
             # produces (it drops the trailing vowel about half the time).
             "गाना", "गान", "गाने", "गीत", "म्यूजिक", "म्यूज़िक", "वीडियो", "कुछ"}

# Hindi puts the verb last ("कोई भी गाना बजाओ"), so the English lead-in patterns
# above can never match it. These strip the trailing verb instead and keep
# whatever came before it as the search query.
RE_HI_PLAY_VERB = re.compile(
    r'\s*(?:प्ले\s*(?:करो|कर|कीजिए|करिए|करदो)|बजाओ|बजा\s*दो|बजाइए|बजाए|'
    r'चलाओ|चला\s*दो|चलाइए|सुनाओ|सुना\s*दो|लगाओ|लगा\s*दो|दिखाओ|दिखा\s*दो)\s*',
    re.IGNORECASE)
# Words carrying no search value, dropped token by token. NOT done with \b
# regexes: Devanagari vowel signs are combining marks rather than word
# characters, so "गाना\b" fails while "गान\b" matches and leaves a stray "ा"
# behind. Splitting on whitespace sidesteps the whole problem.
HI_DROP_TOKENS = {
    "कोई", "भी", "एक", "ज़रा", "जरा", "प्लीज़", "प्लीज", "मुझे", "मेरे", "लिए", "को",
    "गाना", "गान", "गाने", "गीत", "म्यूजिक", "म्यूज़िक", "वीडियो", "विडियो", "कुछ",
    "song", "music", "video", "please",
}
RE_HI_VIDEO = re.compile(r'वीडियो|विडियो', re.IGNORECASE)
RE_DEVANAGARI_ANY = re.compile(r'[ऀ-ॿ]')

# The English RE_PLAY_* patterns above are all anchored at the start of the
# string ("^play ..."), because they run against text that has already been
# checked. But a student asking out loud almost never leads with the bare
# verb -- "Can you play a song for me?", "Please play Believer", "I want to
# watch a video about volcanoes" -- so those requests used to miss every
# regex, fall through to the LLM, and get an improvised "Sure, which song?"
# that never actually played anything (nothing sets pending_media_kind on
# that path, so even naming the title next turn went nowhere). Stripping a
# polite lead-in and trailing filler before matching lets the same anchored
# patterns catch the phrasing people actually use.
RE_MEDIA_LEAD_IN = re.compile(
    r'^\s*'
    # Discourse fillers, and they are not optional politeness -- they are how
    # people actually open a sentence. "Okay, can you play a video of gravity?"
    # failed on the "Okay," alone: the polite forms below were all matched, but
    # nothing stripped what came before them, so the whole request fell through
    # to the model. Which cannot play anything, and said it would anyway.
    r'(?:(?:okay|ok|so|yeah|yep|yes|well|now|alright|right|um|uh|er|hmm|'
    r'and|but|also|actually|hey\s+liza)[,\s]+)*'
    r'(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|'
    r'i\s+want\s+to\s+|i\s+wanna\s+|i\s+would\s+like\s+to\s+|i\'d\s+like\s+to\s+)*',
    re.IGNORECASE)
RE_MEDIA_TRAILING_FILLER = re.compile(
    r'\s*(?:'
    # Where to look is not part of what to look for. "Play a video of gravity in
    # YouTube" searched for the literal string "gravity in YouTube" -- it got a
    # usable hit by luck, but the source name is in the query on every such
    # request and is only ever noise in it.
    r'(?:on|in|from|over|using|through)\s+'
    r'(?:youtube|yt|you\s+tube|google|spotify|the\s+internet|internet|online)|'
    r'for\s+me\s+please|for\s+me|please'
    r')\s*[.?!]*$', re.IGNORECASE)

def _playable_target(raw, allow_nonmedia=False):
    """The searchable title inside a play request, or None when there isn't one."""
    target = (raw or "").strip(" .!?\"'“”।")
    if target.lower() in TITLELESS:
        return None
    if not allow_nonmedia and RE_NOT_MEDIA.search(target):
        return None
    return target

def _detect_hindi_play(text):
    """('video'|'music', query) for a Hindi play request, else (None, None)."""
    if not RE_DEVANAGARI_ANY.search(text) or not RE_HI_PLAY_VERB.search(text):
        return None, None
    kind = "video" if RE_HI_VIDEO.search(text) else "music"
    # Everything except the verb is potential search text -- Hindi routinely
    # trails a modifier after it ("...प्ले करो बॉलीवुड का"), so the tail is kept
    # rather than discarded.
    rest = RE_HI_PLAY_VERB.sub(" ", text)
    tokens = [t for t in re.split(r'\s+', rest) if t]
    kept = [t for t in tokens if t.strip(".!?।,\"'").lower() not in HI_DROP_TOKENS]
    query = " ".join(kept).strip(" .!?।,\"'")
    if len(query) < 2:
        # "कोई भी गाना बजाओ" -- a real request, but with no title in it.
        return kind, None
    return kind, query

def detect_play_media(text):
    """(kind, query) where kind is 'video'/'music'/None.

    A kind with query=None means "they asked for media but did not say which",
    which the caller answers by asking for a title -- see PENDING media handling
    in ai_loop(). Returning None for those instead would hand the turn to the
    LLM, which then has to be trusted not to claim it played something."""
    text = (text or "").strip()
    if not text:
        return None, None
    # Whisper punctuates everything it transcribes, and the two TRAIL patterns
    # are anchored with \s*$ -- so "play microcontroller video." could never
    # match "...video$", fell through to the bare pattern, and came back as a
    # MUSIC request whose query still had the word "video" in it. The search
    # then went looking for "microcontroller video song" and played a Haryanvi
    # track. In other words both trailing patterns were dead code in production:
    # a spoken request always arrives with the full stop attached.
    text = text.rstrip(" \t.!?…।॥\"'“”")
    if not text:
        return None, None
    kind, query = _detect_hindi_play(text)
    if kind:
        return kind, query
    # Only the English patterns below are anchored at the start, so only they
    # need the polite lead-in/trailing-filler stripped -- Hindi's verb-final
    # RE_HI_PLAY_VERB.search() above already tolerates a lead-in as-is.
    text = RE_MEDIA_LEAD_IN.sub('', text)
    # Peeled repeatedly, not once: both patterns are end-anchored, so "...on
    # YouTube please" strips only "please" on a single pass and leaves the
    # source name in the query. Real requests stack two or three of these.
    previous = None
    while previous != text:
        previous = text
        text = RE_MEDIA_TRAILING_FILLER.sub('', text).strip()
    # An explicit "video"/"song" keyword means the user has already said what
    # they want, so only the titleless check applies to those.
    for rx in (RE_PLAY_VIDEO_LEAD, RE_PLAY_VIDEO_TRAIL):
        m = rx.match(text)
        if m:
            return "video", _playable_target(m.group(1), allow_nonmedia=True)
    for rx in (RE_PLAY_MUSIC_LEAD, RE_PLAY_MUSIC_TRAIL):
        m = rx.match(text)
        if m:
            return "music", _playable_target(m.group(1), allow_nonmedia=True)
    m = RE_PLAY_BARE.match(text)
    if m:
        target = _playable_target(m.group(1))
        if target:
            return "music", target
        # Bare "play" with a non-media object ("play chess") is not a media
        # request at all, so it must fall through to the LLM.
        if (m.group(1) or "").strip().lower() in TITLELESS:
            return "music", None
    return None, None

RE_YOUTUBE_ID = re.compile(r'(?:v=|youtu\.be/|/shorts/|/watch/)([A-Za-z0-9_-]{11})')

def _normalize_youtube_url(url):
    """A search index does not always give back a URL that actually loads --
    e.g. "/watch/<id>" instead of "/watch?v=<id>" -- so the video ID is pulled
    out and a canonical watch URL is rebuilt from it instead of trusting the
    href as-is."""
    m = RE_YOUTUBE_ID.search(url or "")
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None

def _clean_video_title(title, fallback):
    """Search snippets occasionally concatenate several results' text together;
    keep only the first clean segment, up to the first "YouTube" mention.

    Falls back to what the student actually asked for when the index hands back
    a useless title -- some results are literally named "YouTube", and reading
    that out loud ("Playing YouTube.") is worse than saying nothing useful."""
    title = (title or "").strip()
    cut = re.split(r'\s*-?\s*youtube', title, maxsplit=1, flags=re.IGNORECASE)[0].strip(" -|·")
    if len(cut) < 3:
        return fallback
    return cut[:90]

# A plain search for a song title drifts badly -- "heatwave" returned a study
# playlist called "When It's Too Hot To Think". Music searches are therefore
# pinned to songs, and results that are clearly not one are skipped.
RE_NOT_A_SONG = re.compile(
    r'\b(?:tutorial|lesson|how\s+to|documentary|podcast|interview|review|'
    r'reaction|news|explained|lecture|study\s+with|asmr|meditation|'
    r'sleep\s+music|white\s+noise|full\s+movie|episode|gameplay|trailer)\b',
    re.IGNORECASE)

def _pick_result(results, url_key, query, songs_only):
    best = None
    for r in results:
        url = _normalize_youtube_url(r.get(url_key))
        if not url:
            continue
        title = _clean_video_title(r.get("title"), query)
        if songs_only and RE_NOT_A_SONG.search(title):
            best = best or {"title": title, "url": url}   # keep as last resort
            continue
        return {"title": title, "url": url}
    return best

# yt-dlp's default client rotation currently lands on `android_vr` for these
# URLs, and YouTube answers the media request with HTTP 403 -- every song and
# video failed this way, with mpv exiting 2 and nothing coming out of the
# speaker. The other clients each fail differently on this Pi: `web`/`ios`
# offer images only, `mweb`/`ios` demand a GVS PO token, `tv` gets DRM-only
# formats, and `web_safari` returns HLS with no `bestaudio` to select.
# `web_embedded` is the one that still hands over a plain progressive stream,
# so it is pinned rather than left to the rotation. Overridable because this is
# a running battle with YouTube and the winning client changes.
YTDLP_PLAYER_CLIENT = os.getenv("YTDLP_PLAYER_CLIENT",
                                "youtube:player_client=web_embedded")


def _ytdlp_search(search_query, limit=6):
    """YouTube's own search, via yt-dlp. Returns [{title, url}, ...].

    `--flat-playlist` keeps this to a single search request: it lists the
    results without resolving each video's formats, which is what makes it
    fast enough to sit on the critical path before playback."""
    ytdlp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "yt-dlp")
    if not os.path.exists(ytdlp):
        ytdlp = "yt-dlp"
    proc = subprocess.run(
        [ytdlp, f"ytsearch{limit}:{search_query}", "--flat-playlist",
         "--extractor-args", YTDLP_PLAYER_CLIENT,
         "--print", "%(title)s\t%(url)s"],
        capture_output=True, text=True, timeout=20)
    results = []
    for line in proc.stdout.splitlines():
        title, _, url = line.partition("\t")
        if url.strip():
            results.append({"title": title.strip(), "url": url.strip()})
    if not results and proc.stderr.strip():
        raise RuntimeError(proc.stderr.strip().splitlines()[-1])
    return results


def search_first_video(query, kind="video"):
    """Title and URL of the first result for `query`, or None. For kind="music"
    the search is constrained to songs rather than videos in general."""
    songs_only = kind == "music"
    search_query = f"{query} song" if songs_only else query
    # Asking YouTube directly rather than a web index: DDG's video endpoint
    # returns nothing at all these days (its parser no longer matches DDG's
    # response, and ddgs has no second video backend to fall back to), which
    # left every request falling through to a site:youtube.com text search.
    # That worked, but ranked by web relevance rather than YouTube's own, so
    # "photosynthesis" surfaced amateur uploads over the obvious explainers,
    # and the titles came back as run-together snippets. This is also the
    # faster path -- one request instead of a failure plus a fallback.
    try:
        hit = _pick_result(_ytdlp_search(search_query), "url", query, songs_only)
        if hit:
            return hit
    except Exception as exc:
        print(f"[MEDIA] YouTube search failed ({exc}); trying a text search instead.", flush=True)

    # Last resort, kept for the case where yt-dlp itself is the thing that is
    # broken -- a stale binary, or YouTube blocking it outright.
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{search_query} site:youtube.com/watch", max_results=6))
        hit = _pick_result(results, "href", query, songs_only)
        if hit:
            return hit
    except Exception as exc:
        print(f"[MEDIA] Fallback text search also failed: {exc}", flush=True)
    return None

# "Hey Liza, stop the music" during playback. The wake word alone already stops
# it (see the barge-in in ai_loop), so all this decides is whether the words
# AFTER the wake word still need answering: a stop request has already been
# carried out and sending it to the model only gets back "nothing is playing".
RE_STOP_MEDIA_PHRASE = re.compile(
    r'^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?'
    # "close"/"shut" belong here as well as in RE_CLOSE_FILE_PHRASE: with a
    # video on screen "close the current file" means the video, and this pattern
    # is only ever consulted while something is actually playing.
    r'(?:stop|pause|mute|quiet|silence|shh+|halt|end|cancel|close|shut|'
    r'turn\s+(?:it|the\s+\w+)?\s*off|'
    r'shut\s+(?:it|up)|no\s+more|enough|band|chup|ruk\w*)'
    # The Hinglish verb tails are not decoration: Whisper romanises Hindi
    # constantly (see detect_user_language), so "band karo" and "chup karo"
    # arrive far more often than the Devanagari branch below ever fires. Without
    # them the stem matched and the tail did not, and the commonest way in the
    # room to say "stop" did nothing at all.
    r'(?:\s+(?:it|that|this|the|a|an|song|music|video|track|playing|now|please|'
    # "the CURRENT file", "the open document" -- the words people actually put
    # between the verb and the thing. Without them the stem matched and the
    # sentence did not.
    r'current|open|file|document|doc|window|clip|movie|'
    r'kar\w*|kr\w*|do|de|dijiye|dijiyega|deejiye))*\s*$'
    # Devanagari vowel signs are combining marks, which \w excludes -- the same
    # trap documented at RE_ECHO_TOKEN. Without the block spelled out here,
    # "रोको" does not match its own stem "रोक".
    r'|^\s*(?:बंद|रोक|चुप|बस)[ऀ-ॣ०-ॿ]*(?:\s+\S+){0,3}\s*$',
    re.IGNORECASE)

MEDIA_STOPPED_ACKS = {"en": "Stopped.", "hi": "बंद कर दिया।", "hinglish": "बंद कर दिया।"}

# She goes completely silent between the question and her first word, and on
# this device most of that gap is the model. Measured against the real
# 3,669-token system prompt, first token came back in 2.93s, 7.78s and 19.99s on
# three IDENTICAL requests -- that spread is Groq queue time, and nothing on this
# Pi can shorten it. What CAN be fixed is the silence: a person who needs a
# moment says so, and going quiet for anywhere between three and twenty seconds
# is most of what "she takes too long" actually is. It also looks exactly like
# not having been heard, which is what makes people repeat themselves.
#
# Fires only when she is genuinely slow -- a reply that begins inside the window
# cancels it -- so a fast turn sounds no different from before.
# Set THINKING_FILLER=0 to go back to silence while she thinks.
MPV_IPC_PATH = os.path.join("/tmp", f"liza-mpv-{os.getpid()}.sock")

def mpv_command(command):
    """One JSON IPC request to the running mpv, or None if it is not there.

    A fresh connection per call rather than a kept-open one: mpv is torn down
    and restarted on every track, and a cached socket would then point at a
    dead process and silently swallow every command afterwards."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            sock.connect(MPV_IPC_PATH)
            sock.sendall((json.dumps({"command": command}) + "\n").encode())
            buffer = b""
            deadline = time.time() + 0.6
            while time.time() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    return None
                complete, _, buffer = (buffer + chunk).rpartition(b"\n")
                for line in complete.split(b"\n"):
                    if not line:
                        continue
                    message = json.loads(line)
                    # Async events share this stream; only replies carry "error".
                    if "error" in message:
                        return message.get("data")
    except Exception:
        pass
    return None

def media_is_paused():
    """True when a player is up but making no sound.

    The mic is closed during playback because the track would otherwise be
    transcribed as commands -- but a PAUSED track makes no sound, so there is
    nothing to protect against and every reason to go back to listening
    normally. Without this, pausing from the music card left media_active set,
    the loop pinned in the barge-in branch, and the only way back in a strict
    wake word shouted at a silent room.

    Read from mpv rather than from a flag of our own: the pause can come from
    the music card, from a tap on a fullscreen video, or from mpv's own input
    config, and only mpv knows about all three. A failed query means mpv is busy
    or gone, and "not paused" is the safe reading of that -- it keeps the mic
    shut rather than opening it over a track that is still playing."""
    if not media_active.is_set():
        return False
    return mpv_command(["get_property", "pause"]) is True

def media_progress_worker(title=None):
    """Feeds the music card's progress bar for as long as mpv is alive, and
    clears the card's "Loading…" line the moment playback genuinely starts.

    Keyed off the first non-zero time-pos rather than mpv merely being up:
    mpv is spawned long before it has bytes to render, so its presence says
    nothing about whether the student is hearing anything yet."""
    playing = False
    while media_active.is_set():
        position = mpv_command(["get_property", "time-pos"])
        duration = mpv_command(["get_property", "duration"])
        paused = mpv_command(["get_property", "pause"])
        if not playing and position:
            playing = True
            ui_call(lambda t=title: state.ui_instance.set_now_playing(t))
        ui_call(lambda p=position, d=duration, s=paused:
                state.ui_instance.set_media_progress(p, d, s))
        time.sleep(0.5)

def start_media_playback(kind, hit):
    """Streams `hit['url']` through yt-dlp into mpv (audio-only for music,
    fullscreen for video). Piped rather than handing mpv the raw googlevideo
    URL directly -- mpv's own ytdl_hook regularly fails to open those signed
    CDN links ("EDL: Could not open source file"), while piping the bytes
    yt-dlp already fetched through stdin has proven reliable in testing.
    The distro's yt-dlp package lags YouTube's changes badly, so the venv's
    own copy (kept current via `pip install -U yt-dlp`) is used explicitly."""
    # Whatever is already playing has to go first, or the two overlap on the speaker.
    stop_media_playback()
    ytdlp = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "yt-dlp")
    if not os.path.exists(ytdlp):
        ytdlp = "yt-dlp"

    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Fullscreen video hides Liza's own "Tap to Stop" button, and there is no
    # keyboard on the Pi, so without these bindings a video is undismissable.
    input_conf = os.path.join(base_dir, "mpv-input.conf")

    # Stale socket from a previous track: mpv refuses to bind over one, and the
    # UI would then talk to nothing for the whole song.
    try: os.unlink(MPV_IPC_PATH)
    except OSError: pass
    ipc = f"--input-ipc-server={MPV_IPC_PATH}"

    if kind == "music":
        yt_fmt = "bestaudio"
        mpv_args = ["mpv", "--no-video", "--really-quiet", ipc,
                    f"--audio-device={MPV_AUDIO_DEVICE}", "-"]
    else:
        yt_fmt = "best[height<=480]/best"
        mpv_args = ["mpv", "--fs", "--really-quiet", ipc,
                    f"--input-conf={input_conf}",
                    f"--audio-device={MPV_AUDIO_DEVICE}", "-"]

    # Kept on disk rather than discarded: a failed playback used to be entirely
    # silent, which made "it said it was playing but nothing happened"
    # impossible to diagnose.
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    yt_log = open(os.path.join(log_dir, "media_ytdlp.log"), "w")
    mpv_log = open(os.path.join(log_dir, "media_mpv.log"), "w")

    yt_proc = subprocess.Popen([ytdlp, "-f", yt_fmt,
                                "--extractor-args", YTDLP_PLAYER_CLIENT,
                                "-o", "-", hit["url"]],
                                stdout=subprocess.PIPE, stderr=yt_log,
                                preexec_fn=_die_with_parent)
    mpv_proc = subprocess.Popen(mpv_args, stdin=yt_proc.stdout, stderr=mpv_log,
                                preexec_fn=_die_with_parent)
    yt_proc.stdout.close()  # let yt_proc receive SIGPIPE if mpv exits first

    with subprocess_lock:
        state.active_subprocesses.extend([yt_proc, mpv_proc])
        state.media_procs = [yt_proc, mpv_proc]
    state.media_process = mpv_proc
    media_active.set()
    note_media_started()
    set_playing_state(hit["title"], kind)
    ui_call(lambda t=hit["title"]: state.ui_instance.set_now_playing(t, loading=True))
    threading.Thread(target=media_progress_worker, args=(hit["title"],),
                     daemon=True).start()

    def watcher():
        rc = mpv_proc.wait()
        try: yt_proc.terminate()
        except Exception: pass
        media_active.clear()
        with subprocess_lock:
            for p in (yt_proc, mpv_proc):
                if p in state.active_subprocesses: state.active_subprocesses.remove(p)
            # In-place: a bare assignment here would bind a local, not the global.
            state.media_procs[:] = []
        for handle in (yt_log, mpv_log):
            try: handle.close()
            except Exception: pass
        # 4 is mpv's "interrupted by a signal", which is what a deliberate stop
        # looks like, so it is not worth reporting as a failure.
        if rc not in (0, 4, -9, -15):
            print(f"[MEDIA] Playback ended with mpv exit {rc}; "
                  f"see logs/media_mpv.log and logs/media_ytdlp.log.", flush=True)
        try: os.unlink(MPV_IPC_PATH)
        except OSError: pass
        # A track that simply ended: nothing called stop_media_playback(), so
        # this is the only place the state can be told it is over.
        set_playing_state(None)
        ui_call(lambda: state.ui_instance.set_now_playing(None))

    threading.Thread(target=watcher, daemon=True).start()

def media_set_pause(paused):
    """Pause or resume the running player. Used to duck a track the instant
    somebody speaks over it, before a word of it has been transcribed.

    Pausing rather than stopping is what makes that safe to do on suspicion:
    it is instant, and it is undone again the moment the audio turns out not to
    have been meant for her."""
    if not media_active.is_set():
        return False
    mpv_command(["set_property", "pause", bool(paused)])
    return True

# How far the track is turned down while a wake check runs over it, as a
# percentage of its own volume. Low enough that a normal voice is comfortably
# the loudest thing in the room, brief enough to read as a dip rather than an
# interruption -- the check is under two seconds and the level is put straight
# back afterwards.
MEDIA_DUCK_VOLUME = float(os.getenv("MEDIA_DUCK_VOLUME", "35"))
# How long the wake check over a track may take, and so how long the track is
# turned down for. Kept far below the normal wake window on purpose -- see the
# note at the call site. 2s to START speaking, 3s to finish the phrase.
MEDIA_WAKE_TIMEOUT_S = float(os.getenv("MEDIA_WAKE_TIMEOUT_S", "2.0"))
MEDIA_WAKE_PHRASE_S = float(os.getenv("MEDIA_WAKE_PHRASE_S", "3.0"))

def media_duck_volume():
    """Turn the track down for the length of a wake check. Returns the volume to
    put back, or None if there was nothing to duck.

    THIS IS WHAT MAKES "HEY LIZA" WORK OVER A VIDEO. The periodic check below
    used to listen at full volume, so Whisper was handed the student's voice
    mixed with the soundtrack and returned the soundtrack -- in the logs, over a
    physics video: "Ask anybody around you this simple question, what is
    gravity?", which is the video talking, and then the student's actual "Hey
    Liza stop" coming back as the Urdu-script nonsense 'هیلی زائے سٹوپ'. Neither
    matched the wake pattern, so both were ignored and the student had to say it
    again. That is the delay in stopping a video: not the stopping, but being
    heard at all.

    Ducking rather than pausing, because this runs on a timer and not on
    suspicion. A pause every few seconds through a whole video would be worse
    than the problem; a dip to 15% for a second and a half is barely noticed
    and gives Whisper a nearly quiet room."""
    if not media_active.is_set():
        return None
    current = mpv_command(["get_property", "volume"])
    if current is None:
        return None
    mpv_command(["set_property", "volume", MEDIA_DUCK_VOLUME])
    return current

def media_restore_volume(previous):
    """Put back what media_duck_volume() turned down."""
    if previous is None or not media_active.is_set():
        return
    mpv_command(["set_property", "volume", previous])

def stop_media_playback():
    """Stops music/video without touching Liza's own speech pipeline, which is
    why this targets the media processes directly instead of reusing
    interrupt_playback()'s sweep of every active subprocess."""
    if not media_active.is_set():
        set_playing_state(None)
        return False
    with subprocess_lock:
        procs = list(state.media_procs)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    # mpv occasionally ignores SIGTERM mid-decode; make sure it really goes.
    deadline = time.time() + 2
    while media_active.is_set() and time.time() < deadline:
        time.sleep(0.05)
    if media_active.is_set():
        for proc in procs:
            try: proc.kill()
            except Exception: pass
    media_active.clear()
    set_playing_state(None)
    return True

