"""The state the parts of Liza share.

Everything here is touched by more than one of ui.py, the assistant, the media
player and the action tags. That is the whole reason it lives on its own: a
module that owns nothing and imports nothing of ours can be imported by all of
them without a cycle. Anything used by exactly ONE concern does not belong
here -- it belongs beside that concern's code.

THERE ARE TWO KINDS OF STATE HERE AND THEY ARE REACHED DIFFERENTLY.

    Events, queues, locks and the lists are never REASSIGNED, only mutated in
    place, so importing them by name is safe and reads better:

        from state import audio_queue
        audio_queue.put(text)

    The plain values are REASSIGNED, so they must be reached through the module
    every time, for reading as well as writing:

        import state
        state.currently_playing = {"title": title, "kind": kind}

    `from state import currently_playing` would bind the value as it stood at
    import, and the writer's later assignment would never reach the reader.
    Nothing raises; the reader simply goes on seeing None for ever. That silence
    is why the two groups are kept apart here instead of left to be remembered.
"""

import queue
import threading
import time

# ---------------------------------------------------------------- the screen
# The live TutorUI, or HeadlessUI with no display. None until main() builds it,
# which is why ui_call() drops what it is given until then.
ui_instance = None

# A mode intro waiting to be spoken by ai_loop, so that audio is never started
# from the Tk thread while the microphone is open. See TutorUI.set_mode().
pending_mode_intro = None

# ---------------------------------------------------------------- speaking
audio_queue = queue.Queue()
playback_active = threading.Event()
stop_playback_event = threading.Event()
current_ai_response = ""

# Everything Liza has said recently, for echo rejection. playback_active alone
# is not enough: it clears when aplay's stdin closes, but sound keeps coming out
# of the ALSA buffer for a moment afterwards, so the mic reopens in time to
# record her own tail. That is how a mode intro came back as a [TRANSCRIPT] and
# got answered as if the student had said it.
last_spoken_text = ""
last_spoken_at = 0.0

# When the reply now playing began. Barge-in is held off for a moment after
# this; see BARGE_IN_LEAD_S.
playback_started_at = 0.0

# Word-timestamped subtitles. The session number is what stops a torn-down
# response captioning the one that replaced it.
caption_lock = threading.Lock()
caption_state = {"session": 0, "start": 0.0, "cues": []}

# ---------------------------------------------------------------- listening
wake_event = threading.Event()
sleep_event = threading.Event()    # the Sleep button was tapped; drop to standby

# ---------------------------------------------------------------- child processes
active_subprocesses = []
subprocess_lock = threading.Lock()

# ---------------------------------------------------------------- media
media_active = threading.Event()   # a song or video is playing via mpv
# Set while the track is turned down for a wake check. The barge-in detector
# treats a ducked track as silence, so it never learns the ducked level.
media_ducked = threading.Event()
media_process = None
media_procs = []                   # [yt-dlp, mpv] for the current playback
# When that player came up. Read by the barge-in path, which must not listen
# into the first moments of a track -- see MEDIA_START_GRACE_S.
media_started_at = 0.0

# ---------------------------------------------------------------- device state
# What the model is told about the device at the foot of every system prompt,
# by device_state_block(). Written from three threads -- ai_loop running an
# action, the Tk thread on a tap, and the media watcher when a song ends by
# itself -- so both writers hold device_state_lock. See set_playing_state().
currently_playing = None      # {"title": str, "kind": "music"|"video"} or None
currently_open_file = None    # absolute path inside $HOME, or None
current_ui_mode = "normal"    # "normal" | "3d"
# The graph most recently put on the board, as the student would write it:
# "y = x^2", or "y = m*x + c, m=2.4" once a slider has been dragged. Told to
# the model so that "now make it x cubed" has something to be a change TO --
# without it she is asked to alter a graph she cannot see.
#
# THE LAST ONE, not the one currently drawn, and the difference is the whole
# reason this survives clear_visual(). A new question wipes the board before
# the answer to it is composed, and that wipe happens on the Tk thread while
# the prompt is being built on ai_loop's -- so clearing this there made the
# follow-up work or not work depending on which thread won. It is cleared when
# something genuinely replaces it: another picture, or Switch User.
#
# Written by the Tk thread on every drag and read by ai_loop, which is why it
# is a plain string and not a dict: a torn read of one gets you the previous
# formula, never half of two.
current_graph = None

# What is on the board right now, and what it is made of:
#   {"kind": "cycle", "title": "Frog life cycle",
#    "steps": ["Eggs", "Tadpole", "Froglet", "Adult frog"]}
#
# Two things read it. The model is told about it, so that "explain it" is
# answered about the picture the student is looking at rather than from
# scratch. And the screen keeps the steps beside the rendered image so it can
# light up the one she is talking about -- which is the only way to highlight
# inside a picture that was drawn in a datacentre and arrived as flat pixels.
#
# `steps` is empty for the kinds that have none, a photograph or an equation.
# None means the board is clear.
current_visual = None
device_state_lock = threading.Lock()

# ---------------------------------------------------------------- kindergarten
# A child tapped Liza during a lesson, meaning "stop, I want to ask you
# something". Separate from wake_event because the KG park branch clears that
# one on every pass -- and because this is not a wake: she is already awake,
# already talking, and the point is to stop her.
kg_ask_event = threading.Event()

# ...and pressed it AGAIN, meaning stop listening. Separate from the event
# above rather than a toggle on it, because the two are read from different
# threads: the screen raises this while ai_loop is already blocked inside
# the microphone read that the first press started, and the read is ended by
# polling this from its cancel predicate.
kg_ask_cancel = threading.Event()


# Set while a KG student is on the device: ai_loop parks on this rather than
# listening. An Event rather than a bool because ai_loop waits on it.
kg_active = threading.Event()

# The KG screens run on the Tk thread and the microphone belongs to ai_loop, so
# a screen asks for a listen through these queues instead of opening one. Every
# request carries an id and an answer carries the id it belongs to; see
# kg_next_listen_id() for what that id is protecting against.
kg_listen_requests = queue.Queue()
kg_listen_results = queue.Queue()
_kg_listen_lock = threading.Lock()
_kg_listen_next = [1]          # id for the next request
_kg_listen_valid_from = [1]    # anything below this has been abandoned


# ---------------------------------------------------------------- accessors
# The only writers for the values above, kept here beside what they write. They
# were in the assistant, which meant the media player and the action tags had to
# import the assistant to record that a song had started -- one of the last
# things holding those two seams shut.

def set_playing_state(title=None, kind=None):
    """Single writer for currently_playing; title=None means nothing plays."""
    global currently_playing
    with device_state_lock:
        currently_playing = {"title": title, "kind": kind} if title else None


def get_device_state():
    """(playing, open_file, ui_mode) -- a snapshot, safe to read at leisure."""
    with device_state_lock:
        playing = dict(currently_playing) if currently_playing else None
        return playing, currently_open_file, current_ui_mode


def note_media_started():
    """Single funnel for 'a player just came up', whoever started it.

    A function rather than an assignment at each site because there are two of
    them -- start_media_playback() and open_file_action() -- and only the first
    ever set the grace period. The second is how "open the gravity file" plays a
    video, so that path came up with NO guard at all and the barge-in branch was
    free to listen into the first second of it."""
    global media_started_at
    media_started_at = time.time()


def note_spoken(text):
    """Single funnel for everything sent to the voice, whoever queued it."""
    global last_spoken_text, last_spoken_at
    last_spoken_text = f"{last_spoken_text} {text}"[-600:]
    last_spoken_at = time.time()


# HAS ANYBODY USED HER SINCE THIS PROCESS STARTED?
#
# False until the first real wake -- a tap, the wake word, a Kindergarten screen
# or a mode card. While it is False the standby wake bar stays at its strictest,
# because a device that was switched on and left has no conversation to protect
# and nothing to lose by being hard to wake. See WAKE_COLD_AFTER_S in config.py
# and the cold check in ai_loop.
#
# A plain value, so it is read and written through the MODULE -- state.used_since_start
# -- and never imported by name. See the note at the top of this file.
used_since_start = False
