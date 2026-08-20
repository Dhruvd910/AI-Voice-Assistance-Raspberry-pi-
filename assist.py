import os
import sys
import math
import io
import json
import queue
import signal
import atexit
import subprocess
import time
import tkinter as tk
import threading
import re
import ctypes
import socket
import traceback
import glob
import fnmatch
import difflib
import collections
import itertools
import contextlib
from datetime import datetime
from PIL import Image, ImageDraw, ImageFilter, ImageTk


def load_dotenv(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Only strip a MATCHED surrounding pair. Stripping quote characters
            # unconditionally corrupts any value that legitimately ends in one,
            # such as the ALSA device name plug:'dmix:CARD=Device_1,DEV=0'.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and not os.getenv(key):
                os.environ[key] = value


# Suppress ONNX Runtime warnings
os.environ["ORT_LOGGING_LEVEL"] = "3"
load_dotenv()

import requests
import speech_recognition as sr
import pyaudio
import audioop
try:
    import webrtcvad
except ImportError:
    # Not fatal: VoiceListener says so once at startup and the old
    # energy-threshold path is used instead. `pip install webrtcvad-wheels`.
    webrtcvad = None
import shutil
from groq import Groq
from openai import OpenAI
from cartesia import Cartesia
from ddgs import DDGS

# 1. The Ears (Groq STT) & Brain (OpenRouter LLM)
#
# Groq: Speech-to-Text (Whisper) — free tier, fast
# Support multiple keys to increase transcription quota (each key has independent budget).
GROQ_STT_KEYS = [k.strip() for k in
                 (os.getenv("GROQ_STT_KEYS") or os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")).split(",")
                 if k.strip()]
groq_stt_clients = [Groq(api_key=k) for k in GROQ_STT_KEYS] or [Groq(api_key="")]
_groq_stt_cursor = itertools.count()
_groq_stt_lock = threading.Lock()

def groq_key_order():
    """Rotate through STT client pool."""
    if len(groq_stt_clients) == 1:
        return list(groq_stt_clients)
    with _groq_stt_lock:
        start = next(_groq_stt_cursor)
    n = len(groq_stt_clients)
    return [groq_stt_clients[(start + i) % n] for i in range(n)]

# OpenRouter: LLM — text-based language models with access to multiple providers
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
openrouter_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)

# 2. The Voice (Cartesia API)
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
cartesia_client = Cartesia(api_key=CARTESIA_API_KEY or None)

# ==========================================
# Configuration & Setup
# ==========================================
# Speech-to-text: no fixed language, so Whisper auto-detects Hindi vs English.
#
# large-v3 rather than the turbo used for the wake word, deliberately. Turbo is
# the faster model and this is the call the student is waiting on, so it looks
# like the obvious swap -- but turbo is distilled and gives most of that back on
# exactly this workload: Hindi, and sentences that switch between Hindi and
# English mid-breath. A wake word is two known words and survives that; a
# question does not, and a misheard question costs a whole wrong answer, which
# is far more of the student's time than the difference here. Set GROQ_STT_MODEL
# to whisper-large-v3-turbo to trade the other way.
STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")

# LLM model via OpenRouter. Full model list at https://openrouter.ai/models
# Default: openai/gpt-oss-120b (via Groq). Also controls how long the room stays
# silent before she starts talking; see start_chat_stream().
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
# off | low | medium | high. THINKING IS OFF BY DEFAULT, AND THAT IS A LATENCY
# DECISION. gemini-2.5-flash is a reasoning model, so left to itself it writes a
# private chain of thought before the first word of the answer -- and nothing
# downstream can start until it does: no sentence, so no Cartesia request, so no
# sound. Measured on this device, same prompt, three runs each, time to first
# token:
#
#   reasoning enabled=false   0.66s     <- default
#   reasoning max_tokens=0    0.79s
#   effort=low + exclude      1.02s
#   nothing sent at all       1.30s
#
# That is half a second of silence saved on every single turn, on questions a
# knowledgeable person answers without stopping to think. Set this to low/medium
# /high if she is ever asked something that genuinely needs working out.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "off")
# Sent with every reply request; see start_chat_stream(). "exclude" keeps the
# chain of thought out of delta.content when thinking IS on -- everything that
# arrives there is read aloud, so without it she recites her own thinking.
LLM_TUNING = ({"extra_body": {"reasoning": {"enabled": False}}}
              if LLM_REASONING_EFFORT == "off" else
              {"reasoning_effort": LLM_REASONING_EFFORT,
               "extra_body": {"reasoning": {"exclude": True}}})
# Longest single pause while waiting out a rate limit; see start_chat_stream().
LLM_RETRY_MAX_WAIT_S = float(os.getenv("LLM_RETRY_MAX_WAIT_S", "5.0"))
# Charged against the rate limit on every request whether it is used or not; see
# the note where it is spent in start_chat_stream().
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "500"))

# Text-to-speech: one Cartesia voice speaks both languages, switched per sentence.
CARTESIA_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3.5")
CARTESIA_SAMPLE_RATE = int(os.getenv("CARTESIA_SAMPLE_RATE", "22050"))
CARTESIA_SPEED = os.getenv("CARTESIA_SPEED", "fast")  # slow | normal | fast
BYTES_PER_SEC = CARTESIA_SAMPLE_RATE * 2  # 16-bit mono
# ALSA's "default" device does not resolve without a configured ~/.asoundrc, so
# playback is pointed at a specific card.
#
# dmix rather than plughw: plughw takes the card EXCLUSIVELY, so whichever of
# Liza's voice or the music player opens it second just fails ("Device or
# resource busy") and dies silently. dmix mixes instead, and the plug: wrapper
# is needed because dmix itself is stereo-only while the TTS stream is mono.
# The inner quotes are part of the device name -- ALSA misparses it without
# them. Check `aplay -L` if this needs to change.
AUDIO_OUTPUT_DEVICE = os.getenv("AUDIO_OUTPUT_DEVICE", "plug:'dmix:CARD=Device_1,DEV=0'")
# Same device, but mpv prefixes ALSA names with "alsa/".
MPV_AUDIO_DEVICE = os.getenv("MPV_AUDIO_DEVICE", "alsa/plug:'dmix:CARD=Device_1,DEV=0'")

# Set CARTESIA_VOICE_ID to a multilingual voice, or give Hindi and English their
# own voices. Run `python assist.py --list-voices` to see what your key can use.
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "")
VOICE_IDS = {
    "en": os.getenv("CARTESIA_VOICE_ID_EN", "") or CARTESIA_VOICE_ID,
    "hi": os.getenv("CARTESIA_VOICE_ID_HI", "") or CARTESIA_VOICE_ID,
}

# Wake word. Standby only transcribes when the mic actually hears speech, so a quiet
# room costs nothing, but a noisy one will spend Whisper calls. Set WAKE_WORD=0 to
# go back to tap-only waking.
WAKE_WORD_ENABLED = os.getenv("WAKE_WORD", "1") != "0"
WAKE_STT_MODEL = os.getenv("WAKE_STT_MODEL", "whisper-large-v3-turbo")
WAKE_LISTEN_TIMEOUT_S = 10   # see listen_for_wake_word(); costs no responsiveness
WAKE_PHRASE_LIMIT_S = 6      # "Hey Liza, what is photosynthesis?" in one breath
# Below this, a clip cannot be speech worth transcribing. Whisper never returns
# nothing -- fed a cough it invents "Thank you." -- so these are filtered before
# the API call rather than after it by HALLUCINATIONS.
MIN_SPEECH_SEC = 0.35
# See MIN_SPEECH_RMS, defined with the energy band it is derived from: a clip
# also has to be LOUD enough to be speech, not just long enough.
WAKE_SEED_PROMPT = "Hey Liza. हे लीज़ा।"
RE_WAKE_WORD = re.compile(
    # English spellings Whisper produces for the name.
    r'\b(?:hey|hi|hello|ok|okay|hay|a)?\s*'
    # Spellings observed from Whisper for the same spoken name. None of these is
    # an ordinary English word, so a bare match is safe without a greeting.
    r'(?:liza|lisa|leeza|leesa|lizza|lyza|eliza|elisa|lija|leza|laiza|liesa|lizah|luiza)\b'
    # Devanagari. The seed prompt is bilingual, so Whisper often writes the name
    # in Devanagari, and its spelling varies far more than a fixed list can cover
    # -- "हे लागा", "हे लगा" and "हे लाजा" were all observed for "Hey Liza". The
    # consonant skeleton is matched instead. A greeting is REQUIRED for this
    # branch because some of those forms (लगा, "felt") are ordinary Hindi words
    # that must not trigger a wake on their own.
    r'|(?:हे|अरे|ओके|हाय|सुनो|हैलो)\s*ल[ािीुू]?[जगसझशद]़?[ािी]?'
    # Unambiguous spellings still wake her with no greeting at all.
    r'|(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)',
    re.IGNORECASE
)

# Used only after the Sleep button, where the bar to wake her has to be higher.
# Sleep is an explicit "leave me alone", so an accidental wake is a much worse
# failure here than a missed one -- the Speak button is always right there.
#
# Two deliberate tightenings over RE_WAKE_WORD:
#   * the Latin name needs its greeting. On its own "Lisa" is a name Whisper
#     reaches for constantly when handed noise.
#   * the fuzzy Devanagari skeleton is dropped entirely, keeping only the
#     unambiguous spellings. That skeleton is 'ल' plus almost any following
#     consonant, so it matches invented words as readily as the real one --
#     observed waking her from sleep on 'हे लागा' out of an empty room.
# Both branches still cover every way the wake word is actually advertised.
RE_WAKE_WORD_ASLEEP = re.compile(
    r'\b(?:hey|hi|hello|ok|okay|hay)\s+'
    r'(?:liza|lisa|leeza|leesa|lizza|lyza|eliza|elisa|lija|leza|laiza|liesa|lizah|luiza)\b'
    r'|(?:हे|अरे|ओके|हाय|सुनो|हैलो)?\s*(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)',
    re.IGNORECASE
)

# Used ONLY by the periodic check that listens OVER a playing track, where the
# thing being transcribed is almost always the track itself.
#
# Both other patterns are far too loose for that. Observed, from the soundtrack
# of a video the student had just asked for: "झाले लीज़ा कर दो प्यादेन",
# "लीज़ा लीज़ा लीज़ा लीज़ा।" and "हे लागा।" -- narration and music, transcribed
# by a Whisper primed with WAKE_SEED_PROMPT, all matching and all stopping the
# video within seconds of it opening. RE_WAKE_WORD_ASLEEP does not help: its
# Devanagari branch makes the greeting OPTIONAL, so a bare "लीज़ा" still matches.
#
# So here the greeting is required in BOTH branches, and the fuzzy consonant
# skeleton is gone. Video narration does not say "hey Liza"; a student does.
#
# The cost -- that a bare "Liza, stop" is ignored on this path -- is paid back
# by the level-margin path, which keeps the loose pattern precisely because
# clearing the bar is already proof a person spoke over the track.
RE_WAKE_WORD_OVER_MEDIA = re.compile(
    r'\b(?:hey|hi|hello|ok|okay|hay)\s+'
    r'(?:liza|lisa|leeza|leesa|lizza|lyza|eliza|elisa|lija|leza|laiza|liesa|lizah|luiza)\b'
    r'|(?:हे|अरे|ओके|हाय|सुनो|हैलो)\s*(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)',
    re.IGNORECASE)

# Music and video hold the microphone shut (a song's own lyrics come back as
# commands otherwise), which would leave "stop the music" as the one spoken
# request Liza could never hear. So while media plays she listens for the WAKE
# WORD only, on the strict from-sleep pattern -- lyrics do not say "Hey Liza".
MEDIA_BARGE_IN = os.getenv("MEDIA_BARGE_IN", "1") != "0"
# Gap between attempts. Each one is a paid STT call on a clip that is almost
# always just the track itself: the music is the loudest thing in the room, so
# it opens a phrase immediately every time and the full phrase limit is spent
# recording it. At the original 2s that came to roughly one Whisper call every
# eight seconds for the whole length of a song -- around twenty-five per track,
# all of them transcribing the song, and enough to run the account into rate
# limits that then break the STT the STUDENT is waiting on.
#
# 3.0 rather than the 6.0 this was set to, because the reasoning above only
# holds while this poll is the ONLY way in. It is not any more: MEDIA_BARGE_IN_
# MARGIN gives a voice raised over the track an instant path that costs nothing,
# so this is now the backstop for someone speaking quietly rather than the
# primary route, and 6s of it was most of the delay in "Hey Liza, stop the
# music". Still slow enough to keep the call count per track in the low tens.
#
# Raised from 3.0. This is the BLIND check -- the one on a timer, that runs
# whether or not anybody has spoken -- and it now turns the track down to listen
# rather than listening over it. Every three seconds that is a dip the student
# hears for the whole length of a song, which is its own version of the problem
# it was meant to solve. A voice in the room does not wait for this: the
# barge-in path above ignores the cooldown entirely and checks at once, so the
# only thing a longer gap here costs is the case where somebody speaks too
# quietly to arm that path at all.
MEDIA_BARGE_IN_COOLDOWN_S = float(os.getenv("MEDIA_BARGE_IN_COOLDOWN_S", "10.0"))
# No wake-word check at all for this long after a player comes up.
#
# Observed, and reported as "it opens the file but after 1-2 seconds it closes
# automatically": the video starts, its own soundtrack is the first thing the
# barge-in reference ever measures, and the reference was seeded from a frame
# recorded before any audio was flowing. So the track instantly cleared a bar
# set against silence, a wake check ran on the video's own dialogue, and Whisper
# -- primed with WAKE_SEED_PROMPT, which is literally "Hey Liza. हे लीज़ा।" --
# handed that prompt straight back. Matched as a wake word, the video was
# stopped about a second after it opened, every time.
#
# Nobody asks Liza to stop something in the first moments of it, so waiting
# costs nothing real, and it gives the level reference time to settle against
# the actual track instead of against the silence before it.
#
# 4.0 rather than 8.0: what this guard is really waiting for is the level
# reference to stop being seeded from silence, and BARGE_IN_LEAD_S already
# covers that in under a second. The rest was margin bought before the seeding
# was understood, and it was paid for by being completely deaf for the first
# eight seconds of every track -- which is exactly when somebody who opened the
# wrong file wants to say so.
MEDIA_START_GRACE_S = float(os.getenv("MEDIA_START_GRACE_S", "4.0"))

# Weather panel. The key lives in .env so it never reaches the repo.
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
WEATHER_CITY = os.getenv("WEATHER_CITY", "Delhi,IN")
WEATHER_REFRESH_S = int(os.getenv("WEATHER_REFRESH_S", "900"))

wake_event = threading.Event()
HISTORY_FILE = os.getenv("HISTORY_FILE", "chat_history.json")
MAX_HISTORY_TURNS = 6

audio_queue = queue.Queue()
playback_active = threading.Event()
stop_playback_event = threading.Event()
active_subprocesses = []
subprocess_lock = threading.Lock()
ui_instance = None
media_active = threading.Event()   # a song or video is playing via mpv
# When that player came up. Read by the barge-in path, which must not listen
# into the first moments of a track -- see MEDIA_START_GRACE_S.
media_started_at = 0.0

def note_media_started():
    """Single funnel for 'a player just came up', whoever started it.

    A function rather than an assignment at each site because there are two of
    them -- start_media_playback() and open_file_action() -- and only the first
    ever set the grace period. The second is how "open the gravity file" plays a
    video, so that path came up with NO guard at all and the branch below was
    free to listen into the first second of it."""
    global media_started_at
    media_started_at = time.time()
media_process = None
media_procs = []                   # [yt-dlp, mpv] for the current playback
sleep_event = threading.Event()    # the Sleep button was tapped; drop to standby

# ---------- device state (rule 7, the agentic actions) ----------
# What the device is actually DOING, as opposed to what it has been told. Handed
# to the model at the bottom of every system prompt by device_state_block(), so
# "stop the music" with nothing playing is answered with "there's nothing
# playing" instead of a confident stop of nothing.
#
# Locked because these are written from three different threads: ai_loop when it
# executes an action, the Tk thread when a button is tapped, and the media
# watcher when a song simply ends on its own.
currently_playing = None      # {"title": str, "kind": "music"|"video"} or None
currently_open_file = None    # absolute path inside $HOME, or None
current_ui_mode = "normal"    # "normal" | "3d"
device_state_lock = threading.Lock()

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
# Mode intro waiting to be spoken by ai_loop, so audio is never started from the
# Tk thread while the microphone is open. See TutorUI.set_mode().
pending_mode_intro = None
ECHO_GUARD_SEC = 2.5      # treat mic input as suspect this long after speaking
MIC_SETTLE_SEC = 0.4      # let the speaker drain before opening the mic

def note_spoken(text):
    """Single funnel for everything sent to the voice, whoever queued it."""
    global last_spoken_text, last_spoken_at
    last_spoken_text = f"{last_spoken_text} {text}"[-600:]
    last_spoken_at = time.time()

# Echo detection scores how much of what the mic heard also appears in what Liza
# just said. Function words have to be excluded from that score: last_spoken_text
# is 600 characters of her recent speech, so nearly every English or Hindi
# stopword is somewhere in it, and an ordinary sentence like "it is in the leaf
# and it uses light" scores 5/8 on those alone and gets thrown away as echo.
# RE-TELL made that fatal rather than annoying -- a dropped chunk is a piece of
# the student's answer that never reaches the examiner, with nothing on screen
# or in the log to say a mark was based on half a recitation.
ECHO_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "it", "its",
    "this", "that", "these", "those", "i", "you", "he", "she", "we", "they", "me",
    "my", "your", "and", "or", "but", "so", "if", "then", "than", "of", "in", "on",
    "at", "to", "for", "from", "with", "by", "as", "into", "about", "not", "no",
    "do", "does", "did", "can", "will", "would", "should", "have", "has", "had",
    "what", "which", "who", "how", "why", "when", "there", "here", "very", "also",
    "है", "हैं", "था", "थी", "थे", "हूँ", "हूं", "रहा", "रही", "रहे", "का", "के", "की",
    "को", "में", "से", "पर", "और", "या", "यह", "वह", "ये", "वे", "एक", "भी", "ही",
    "कि", "तो", "जो", "मैं", "आप", "तुम", "हम", "नहीं", "क्या", "कर", "करना", "लिए",
    "होता", "होती", "होते", "हुआ", "हुई", "गया", "गई", "दिया", "लिया", "बहुत",
    "सब", "कुछ", "अब", "फिर", "जब", "तब", "ठीक", "अच्छा", "जी",
}

# Devanagari vowel signs and the virama are combining marks, and Python's \w
# excludes those categories, so r'\w+' does not tokenise Hindi -- it shreds it
# into bare consonants. "मैंने प्रकाश संश्लेषण" comes apart into म, न, प, रक, श,
# स, षण, and those fragments collide with the fragments of any other Hindi
# sentence, which made every Hindi utterance look like an echo of the last one.
# Adding the Devanagari block back keeps each word in one piece. The two danda
# codepoints are carved out of that range: they are sentence punctuation, but
# they live inside the block, so including them leaves "बोलिए।" and "बोलिए" as
# two different words that never match.
RE_ECHO_TOKEN = re.compile(r'[\wऀ-ॣ०-ॿ]+')   # U+0964/5 = । ॥

def echo_words(text):
    return set(RE_ECHO_TOKEN.findall((text or "").lower()))

def echo_overlap_ratio(heard_words, spoken_words):
    """How much of `heard_words` looks like Liza's own voice coming back.

    Scored on content words. Falls back to the raw sets only when the student's
    utterance is nothing BUT stopwords, which is exactly what a short
    acknowledgement ("go on", "ठीक है") sounds like bleeding back in."""
    heard_content = heard_words - ECHO_STOPWORDS
    if not heard_content:
        return len(heard_words & spoken_words) / len(heard_words) if heard_words else 0.0

    matched = heard_content & (spoken_words - ECHO_STOPWORDS)
    # One or two content words cannot be scored by ratio -- the answer can only
    # be 0, 0.5 or 1, and a single coincidental word already clears any sane
    # threshold. "So what I have learned is that the plant..." carries just
    # {learned, plant}, and "learned" appears in the RE-TELL intro, so a ratio
    # test drops the student's opening sentence every time. Below three content
    # words, demand that ALL of them match before calling it echo.
    if len(heard_content) < 3:
        return 1.0 if len(matched) == len(heard_content) else 0.0
    return len(matched) / len(heard_content)

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

def kill_stray_media():
    """Kills media players left behind by a previous run that was killed
    outright. Matched on our own audio-device string so it cannot take out an
    unrelated mpv the user started themselves."""
    try:
        out = subprocess.run(["pgrep", "-af", "mpv"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if MPV_AUDIO_DEVICE in cmd and pid.isdigit():
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f"[MEDIA] Killed stray player from a previous run (pid {pid}).", flush=True)
            except Exception:
                pass
HEADLESS_MODE = False
current_ai_response = ""

PREFERRED_MIC_NAMES = ["USB PnP Sound Device", "USB Audio", "Audio"]
# Name matching cannot separate two dongles that report the SAME name, and this
# device has exactly that: the audio adapter's empty mic jack enumerates first,
# so auto-detection picks a capture device that only ever returns noise floor.
# Set MIC_DEVICE_INDEX in .env to pin the real one; `--list-mics` prints them.
MIC_DEVICE_INDEX = os.getenv("MIC_DEVICE_INDEX", "").strip()
# Frames per read on the FALLBACK path only -- VoiceListener does not use this,
# and deliberately so.
#
# The reasoning that produced 4096 was that fewer, larger reads are gentler on a
# cheap C-Media chip than a read every 23ms. That part holds. What was never
# measured is what a fixed buffer this size does to PortAudio's ALSA ring, and
# the answer is that it overruns it: a 4096-frame request captures 82% of
# realtime on this dongle and raises paInputOverflowed, 8192 gives 62%, and
# 16384 gives 57%. Larger is WORSE, which is the signature of a ring being
# overrun rather than of a reader being slow -- 4096 frames is 93ms of audio
# against a default input latency of a few milliseconds.
#
# So for as long as this was the capture path, roughly a fifth of every
# recording was being discarded inside PortAudio: syllables torn out of the
# middle of sentences, which Whisper then reads as a different sentence or as
# nothing at all. It is a large part of why she had to be told things twice.
#
# VoiceListener asks PortAudio to size its own buffer instead and measures 99.9%
# with no overflows; see its _open(). This value is left alone because the
# fallback path is the energy-threshold one, which is already the worse path,
# and changing its buffering is not what would fix it.
MIC_CHUNK = int(os.getenv("MIC_CHUNK", "4096"))
# Bounds on the speech-detection threshold. See clamp_energy() for why both ends
# are needed; `--calibrate-mic` measures the right values for a room.
MIC_ENERGY_FLOOR = int(os.getenv("MIC_ENERGY_FLOOR", "1000"))
MIC_ENERGY_CEILING = int(os.getenv("MIC_ENERGY_CEILING", "1300"))
# The loudness gate that goes with MIN_SPEECH_SEC. A clip that opened a phrase
# is not necessarily speech: the threshold that opens one is measured per
# 1024-frame chunk, so a single door slam inside two seconds of room tone is
# enough, and what reaches Whisper is then 95% silence. Whisper answers silence
# with a confident sentence every time -- "I am a student of the Ministry of
# Education." came out of an empty room on this device, and was replied to.
# Requiring the clip as a WHOLE to average above this throws those away before
# they are paid for. Deliberately derived from the floor and not from the live
# threshold: the live one is allowed to ride up in a noisy room, and a bar that
# rose with the noise would start discarding the student instead.
MIN_SPEECH_RMS = int(os.getenv("MIN_SPEECH_RMS", str(int(MIC_ENERGY_FLOOR * 0.6))))

# ---------------------------------------------------------------------------
# Voice activity detection.
#
# The two numbers above are a LOUDNESS test, and on this hardware loudness
# cannot decide what is speech. Measured in this room: noise sits at a median of
# 310 and peaks at 1416, while a voice at conversational distance runs 280-1600.
# Those are the same band. Any threshold drawn through it either deafens her to
# anyone more than a hand's width from the dongle (what MIC_ENERGY_FLOOR=600
# did) or hands Whisper a fan and a chair scrape to invent sentences from.
#
# webrtcvad decides per 30ms frame on the SHAPE of the signal instead -- the
# harmonic structure and the way it evolves -- so a quiet voice across the room
# is still obviously a voice, and a loud steady hum is still obviously not. That
# is the whole reason a student can now speak from a normal distance.
#
# The energy gates are kept as the fallback path for a machine without the
# library, and as a floor against digital silence; see is_probably_speech().
VAD_ENABLED = os.getenv("VAD", "1") != "0"
# 0-3, least to most aggressive about calling a frame NON-speech. 3 is tuned for
# telephony and discards the quiet tail of a sentence, which is exactly the part
# a distant speaker has the least of. 2 is the most filtering that still keeps a
# normal indoor voice intact.
VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "2"))
# webrtcvad accepts 10, 20 or 30ms frames at 8/16/32/48kHz only. The dongle runs
# at 44.1k (it refuses to open at 16k), so the reader downsamples on the way in.
VAD_FRAME_MS = 30
VAD_RATE = 16000
VAD_FRAME_BYTES = int(VAD_RATE * VAD_FRAME_MS / 1000) * 2   # 960 bytes, mono s16
# What the device is opened at, before the downsample to VAD_RATE. This dongle
# only supports 44.1k -- 16k and 32k are refused outright, and 48k opens but
# makes ALSA resample and captures at half realtime. VoiceListener._open() falls
# back to whatever PortAudio reports for the device if this rate is refused.
CAPTURE_RATE = int(os.getenv("MIC_CAPTURE_RATE", "44100"))
# Audio kept from BEFORE speech was declared, and prepended to the clip.
#
# This is the other half of "I have to say it twice". recognizer.listen() starts
# recording at the moment it decides speech has begun, so the onset that made
# that decision is already gone -- and an onset is a whole syllable. "Hey Liza,
# play Shape of You" arriving as the single word "The" is that, not a threshold
# problem. Holding the stream in a ring buffer means the decision can be made
# late and the audio recovered anyway, so the first word survives.
VAD_PREROLL_MS = int(os.getenv("VAD_PREROLL_MS", "700"))
# Voiced frames needed before a phrase is declared open. Eight frames.
#
# Six (180ms) was measured to sit exactly ON this room's noise ceiling: sampling
# five seconds of an empty room, the longest unbroken run webrtcvad scored as
# voiced was 180ms on the nose, so a phrase opened or did not depending on which
# side of a coin toss the last frame landed. That is what an intermittent false
# wake out of a quiet room looks like. 240ms clears it with margin, and is still
# far below what any real word sustains -- a spoken "no" holds voiced frames for
# the better part of a second.
VAD_START_MS = int(os.getenv("VAD_START_MS", "240"))
# Trailing silence kept on the clip. Whisper transcribes a hard cut at the last
# syllable less reliably than one with a little air after it.
VAD_TAIL_MS = int(os.getenv("VAD_TAIL_MS", "300"))

# Software make-up gain, applied to the finished clip only.
#
# Separating speech from noise is the VAD's job; making the speech legible to
# Whisper is this one. A voice recorded across the room peaks around 6-8% of
# full scale, and Whisper's error rate on a clip that quiet is visibly worse --
# it fills the low-contrast parts with plausible connective words, the same
# failure disable_mic_agc() describes. Normalising to a healthy peak costs
# nothing and is undone by nothing downstream, since the clip is discarded after
# transcription. Capped so a clip of near-silence is not amplified into a roar
# of shaped noise that Whisper then transcribes.
MIC_TARGET_PEAK = float(os.getenv("MIC_TARGET_PEAK", "0.6"))
MIC_MAX_GAIN = float(os.getenv("MIC_MAX_GAIN", "10"))
# Absolute floor for a VAD-endpointed clip. Not a speech/noise decision any
# more -- the VAD already made that -- just a guard against a dead or unplugged
# capture device delivering digital silence that the VAD scores as voiced.
VAD_MIN_RMS = int(os.getenv("VAD_MIN_RMS", "60"))

# ---------------------------------------------------------------------------
# Barge-in: interrupting Liza while she is speaking.
#
# The microphone stays live through her replies now, which means every one of
# her own words arrives back through it. There is no acoustic echo canceller
# here -- one cheap capture dongle, one speaker, no reference signal -- so the
# only thing separating the student from Liza's own voice is that the student is
# nearer the microphone than the speaker is. That is a real physical margin on
# this device, and it is what BARGE_IN_MARGIN spends.
#
# The bar is measured continuously against her own voice rather than fixed: the
# level coming back depends on the speaker volume, which the student can change,
# so a constant would be wrong within a day of use.
BARGE_IN_ENABLED = os.getenv("BARGE_IN", "1") != "0"
# Per-frame trace of the level test above. Noisy by design; leave it off unless
# somebody is reporting that they cannot interrupt her.
BARGE_IN_DEBUG = os.getenv("BARGE_IN_DEBUG", "0") == "1"

# WHY BARGE-IN IS UNRELIABLE ON THIS BUILD, AND WHAT ACTUALLY FIXES IT.
#
# Measured with BARGE_IN_DEBUG while somebody talked over a reply at a normal
# speaking volume, speaker at 80%:
#
#   speaker silent   -> their voice reached the microphone at up to 4365 RMS
#   speaker playing  -> her own voice measured 3535, theirs never got past 2067
#
# Their voice arrives roughly 6dB BELOW her own. Summed, the microphone sees
# only 1.16x what her voice alone produces -- and her voice itself swings by
# more than 20x between syllables. So the comment below ("the student is nearer
# the microphone than the speaker is") is simply false here, and NO value of
# BARGE_IN_MARGIN separates the two cases: at 1.8 nothing a person says can
# fire it, and low enough to fire, her own syllables fire it instead.
#
# Predicting her contribution from our own PCM was tried and does not rescue
# it either: the acoustic delay varies, speech dips to near silence between
# syllables, and any estimator loose enough to survive that is too loose to
# see a 1.16x rise. Separating a voice from a louder echo at these ratios is
# what an acoustic echo canceller is for, and there is not one here.
#
# WHAT ACTUALLY WORKS, in order of effort:
#   1. Turn the speaker down. At 80% her echo is 3535; around 50% it is closer
#      to 1200, the person is then comfortably the loudest thing in the room,
#      and the plain test below starts working with margin to spare.
#   2. Move the dongle away from the speaker, or use a directional/headset mic.
#   3. Add real AEC (speexdsp, webrtc-audio-processing) with our PCM as the
#      reference signal.
#
# OPTION 3 WAS TRIED ON THIS HARDWARE AND DOES NOT WORK. Speex's canceller was
# wired up with our own PCM as the reference, paired to the microphone by
# playback clock, and measured against a real reply through the real speaker:
#
#   linear canceller, best case over a +-80ms offset sweep     -2.5 dB
#   the same sweep, at every other offset                      -2.5 dB
#
# A canceller that has locked onto the echo shows a sharp peak at the true
# delay. Flat across the whole sweep means it never locked at all -- and it is
# not a delay problem: cross-correlating the envelopes puts the echo at a steady
# -34ms with no drift across nine seconds, at r=0.88. The loudness patterns line
# up; the WAVEFORMS do not, which is what an adaptive filter needs. Playback is
# resampled 22050 -> dmix -> the DAC and capture is resampled 44100 -> 16000 on
# the way back, on two separate USB devices, and sample-level phase does not
# survive that.
#
# Turning on Speex's preprocessor did show ~10dB, and that number is a trap: it
# is the DENOISER pulling everything down, not the echo coming out. Measured
# with a synthetic student talking over her, the student was attenuated MORE
# than the echo was (-11.1dB against -10.0dB), so the ratio the test below
# actually depends on got slightly worse rather than better.
#
# What would work is an echo canceller that resamples both sides onto a common
# clock -- PipeWire's module-echo-cancel is already on this image and does
# exactly that, but reaching it means routing playback and capture through
# PipeWire instead of straight at ALSA, which needs the pipewire-alsa bridge
# installed and the device selection above redone.
#
# Until then, barge-in fires reliably only in her pauses -- between sentences,
# where each is a separate TTS request and the room is briefly quiet.
# How much louder than her own returning voice the student has to be. 1.6 is
# ~4dB: comfortably reached by speaking normally towards the device, and not
# reached by the speaker itself unless the volume is near maximum.
#
# Was 1.8. Lowered together with BARGE_IN_MS below, because the two multiply:
# the student had to be half again as loud as the speaker AND hold it for most
# of a second before anything happened, and the reply carried on through all of
# it. Interrupting a person does not work that way -- they stop while you are
# still on your first word -- and the gap is the whole difference between
# "talking to it" and "waiting for it".
BARGE_IN_MARGIN = float(os.getenv("BARGE_IN_MARGIN", "1.8"))
# Sustained speech required to cut her off, in ms. Longer than VAD_START_MS on
# purpose: stopping her mid-sentence is disruptive, so it should take an actual
# word rather than a cough. Roughly one syllable.
#
# Was 420ms, which is a whole word plus its pause -- long enough that she
# finished the sentence she was on before noticing. 260ms is about one syllable:
# still far too long for a cough or a door, short enough that she stops while
# the student is on their first word. The LEVEL test is what rejects noise here,
# not the duration, so shortening this trades very little accuracy for the thing
# that actually makes an interruption feel like one.
BARGE_IN_MS = int(os.getenv("BARGE_IN_MS", "260"))
# The same test, but against a song or video rather than against her own voice.
#
# Deliberately far lower than BARGE_IN_MARGIN, and safe to be, because the two
# situations are not alike. Clearing the bar over her OWN speech cuts her off
# immediately; clearing it over MEDIA only ducks the track and buys one wake-word
# check, and if the words were not for her the track resumes a second later.
# Being wrong is cheap here and being deaf is expensive -- at 1.8 a normal voice
# simply never cleared a loud track, so the only way in was the periodic poll
# below, which is where the lag in "Hey Liza, stop" came from.
#
# Lowered from 1.35, which was never reachable on this hardware. The student's
# voice arrives at the microphone BELOW the speaker's own output (see the
# measurements in the barge-in block above), so a voice on top of a track only
# lifts the combined level by a little over 1.1x -- and a bar at 1.35 meant this
# path never armed over a video at all. Every "Hey Liza, stop" fell through to
# the periodic poll instead, which is the slow way in.
#
# 1.25 is measured, not guessed. Playing a real track through the real speaker
# with nobody in the room and counting how often this path armed on the music
# ALONE, over 45 seconds:
#
#   1.15 -> 4 false arms, one every 11s        1.35 -> 1
#   1.25 -> 1                                  1.50 -> 1
#
# Below 1.25 the track's own dynamics trip it repeatedly; above 1.25 nothing
# further is gained, and every increment makes a real voice harder to hear. 1.15
# was the value that had the song ducking every few seconds all the way through.
# Being wrong is still cheap: a false arm turns the track down for a moment,
# finds nothing was said, and puts it straight back.
MEDIA_BARGE_IN_MARGIN = float(os.getenv("MEDIA_BARGE_IN_MARGIN", "1.25"))
# Grace period after playback starts before an interruption is allowed. Without
# it the tail of the student's own previous sentence, still arriving as she
# begins to answer, reads as an immediate interruption of the reply it caused.
BARGE_IN_LEAD_S = float(os.getenv("BARGE_IN_LEAD_S", "0.7"))
# Frames of her ACTUAL voice needed before an interruption can be recognised,
# on top of the clock above. At 30ms a frame this is about half a second of
# real audio; see barge_in_ready() for why a clock alone is not enough.
BARGE_IN_WARMUP_FRAMES = int(os.getenv("BARGE_IN_WARMUP_FRAMES", "16"))

# How long a pause ends the student's turn. 1.5s (the old fixed value) is dead
# air on every single exchange and is most of why she felt slow rather than
# conversational -- a person starts answering roughly 0.2s after you stop. A
# recitation in RE-TELL genuinely does pause mid-thought, so that mode keeps the
# patient value.
# Lowered from 0.8 now that webrtcvad decides where speech ends. 0.8 was set
# against the energy gate, which needed the margin: it could not tell a quiet
# consonant from a pause, so ending a phrase early cut real words off. A VAD
# reads the difference directly, so most of that margin was dead air charged to
# every single turn -- and it is the ONLY part of the ~3.3s before she answers
# that is a choice rather than a network round trip (STT ~0.6s, LLM ~0.9s to its
# first sentence, TTS ~0.96s to its first audio, all measured on this device).
PAUSE_THRESHOLD_NORMAL = float(os.getenv("PAUSE_THRESHOLD", "0.55"))
PAUSE_THRESHOLD_RETELL = float(os.getenv("PAUSE_THRESHOLD_RETELL", "1.6"))

# Same trade as WAKE_LISTEN_TIMEOUT_S: a longer wait for speech to begin returns
# just as fast when it does, and halves how often the capture device is cycled
# during an active session.
IDLE_LISTEN_TIMEOUT_S = 10
STANDBY_AFTER_TIMEOUTS = 3   # -> ~30s of quiet before dropping back to standby

class ClampedRecognizer(sr.Recognizer):
    """A Recognizer whose energy_threshold physically cannot leave the band
    `--calibrate-mic` measured.

    Clamping used to be a function called after every listen, and that is one
    call too late to matter. speech_recognition re-computes energy_threshold
    INSIDE listen(), on every 1024-frame chunk, for as long as it is waiting for
    speech to begin -- and in a quiet room that computation walks the threshold
    straight down towards the noise floor. Measured on this device: clamped to
    1000 on entry, it reached 586 after one second and 370 after four, while
    room tone sits at 300-500. So every wait of more than a second or two ended
    with the threshold UNDER the room, a phrase opening on nothing, and 2-3
    seconds of silence going to Whisper -- which never returns nothing, and duly
    invented sentences ("I am a student of the Ministry of Education.", "चाहे
    लापने कि लिज़ा।"). Those inventions then reached the wake matcher and the
    LLM as if a person had said them: false wakes out of an empty room, replies
    to nobody, and a real "Hey Liza" arriving while the device was busy
    transcribing silence.

    A property is used rather than switching dynamic tracking off, because
    dynamic tracking is still what follows the room back down after a noisy
    spell -- it just must not be allowed below the floor while it does."""

    @property
    def energy_threshold(self):
        return self._energy_threshold

    @energy_threshold.setter
    def energy_threshold(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        self._energy_threshold = min(max(value, MIC_ENERGY_FLOOR), MIC_ENERGY_CEILING)


def clamp_energy(recognizer):
    """Kept as a no-op safety net for recognizers built elsewhere.

    ClampedRecognizer above enforces the band on assignment, so every call site
    that used to need this is already covered; this only still does something if
    a plain sr.Recognizer is ever handed in."""
    if recognizer.energy_threshold < MIC_ENERGY_FLOOR:
        recognizer.energy_threshold = MIC_ENERGY_FLOOR
    elif recognizer.energy_threshold > MIC_ENERGY_CEILING:
        recognizer.energy_threshold = MIC_ENERGY_CEILING

RE_ANSWER_PREFIX = re.compile(r'ANSWER:\s*')
RE_GREETING_PREFIX = re.compile(r'^\s*(?:"|\')?\s*(hi there|hello there|hi|hello|hey|greetings)\b[,!.:\s-]*', re.IGNORECASE)
RE_EMOJI = re.compile(r'[\U00010000-\U0010ffff]')
RE_SENTENCE_SPLIT = re.compile(r'(?<=[.?!।])\s+')
RE_DEVANAGARI = re.compile(r'[ऀ-ॿ]')
RE_LATIN_WORD = re.compile(r'[A-Za-z]{2,}')
# Whisper regularly hears Hindi as Urdu (same spoken language, different script) and
# writes it in Arabic script, which neither voice can read. Same for the other Indic
# scripts it falls back to. Detecting these lets us re-read the audio as Hindi.
RE_UNREADABLE_SCRIPT = re.compile(r'[؀-ۿݐ-ݿঀ-෿ﭐ-﷿ﹰ-﻿]')

# Silence C-Level Warnings
ALSA_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
def py_alsa_error_handler(filename, line, function, err, fmt): pass
c_alsa_error_handler = ALSA_HANDLER_FUNC(py_alsa_error_handler)
try:
    asound = ctypes.cdll.LoadLibrary('libasound.so')
    asound.snd_lib_error_set_handler(c_alsa_error_handler)
except OSError: pass

JACK_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
def py_jack_error_handler(msg): pass
c_jack_error_handler = JACK_HANDLER_FUNC(py_jack_error_handler)
try:
    jacklib = ctypes.cdll.LoadLibrary('libjack.so.0')
    jacklib.jack_set_error_function(c_jack_error_handler)
    jacklib.jack_set_info_function(c_jack_error_handler)
except OSError: pass

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return []
    return []

def save_history(chat_history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(chat_history, f)

# Nothing ever asks for an EMOTION: line. The prompt requests ANSWER: and only
# ANSWER:, and all three places that touch the model's output strip the tag back
# off before using it. It survives because the assistant turns already in the
# history carry it, which few-shots the next reply into writing one too -- a tag
# that costs output tokens on every turn, then input tokens for as long as it
# stays in the window, and is read by nobody. Keeping it out of the history is
# what actually stops it: with no examples left to copy, it dies out by itself.
RE_EMOTION_TAG = re.compile(r'^[ \t]*EMOTION:.*\n?', re.IGNORECASE | re.MULTILINE)

# The action tags of rule 7, stripped everywhere text is spoken or remembered.
# The closing bracket is optional on purpose: sentences are handed to the voice
# as they stream in, so a tag can arrive split down the middle, and half a tag
# is just as unspeakable as a whole one.
RE_ACTION_TAG_STRIP = re.compile(r'\[\s*ACTION\s*:[^\]]*\]?', re.IGNORECASE)
# The mood she reports on the first line, for the chip under her face. Tolerant
# of the brackets the model sometimes adds around it.
RE_EMOTION_LINE = re.compile(r'EMOTION:\s*\[?\s*([A-Za-z]+)', re.IGNORECASE)

def remember_reply(chat_history, text):
    """Append an assistant turn, carrying only what a later turn can use.

    The ANSWER: prefix is deliberately kept -- it is the output contract, and
    the surviving examples are what hold the model to it. An empty reply is
    dropped instead of stored: a failed turn that leaves a blank assistant
    message behind teaches the model that blank replies are allowed."""
    text = RE_EMOTION_TAG.sub('', text or '')
    # Tags are instructions to the device, not part of the conversation. Left in
    # history they become worked examples, and she starts tagging replies that
    # were never meant to do anything.
    text = RE_ACTION_TAG_STRIP.sub('', text).strip()
    if text:
        chat_history.append({"role": "assistant", "content": text})
    return chat_history

# A turn count cannot bound the payload, only the number of pieces it arrives
# in. One RE-TELL turn is a student reciting from memory for up to
# RETELL_PHRASE_LIMIT_S seconds, so six of those "turns" is an unbounded amount
# of text, and the oldest of them is the least worth paying for on every
# subsequent request.
#
# MEASURED IN UTF-8 BYTES, NOT CHARACTERS, and that is the whole point.
#
# This used to count characters, on the reasoning that the cap "only has to stop
# runaway growth, not be exact". That reasoning was wrong, and it is what made
# the device fail in Hindi specifically: Devanagari costs about 3x the tokens of
# the same character count in Latin, so an identical 6000-character budget
# bought roughly 3x more tokens of Hindi history than of English. On a free tier
# capped at 8000 tokens per minute, two Hindi turns were enough to spend the
# whole allowance and every reply after that came back 429 -- surfacing to the
# student as "I couldn't reach my brain servers" after they switched language,
# and never in English. Reported exactly that way.
#
# UTF-8 bytes are 1 per ASCII character and 3 per Devanagari one, which is the
# ratio that matters here, so the budget now buys the same amount of MODEL for
# either language. Same trick, and the same reasoning, as stt_prompt_size().
MAX_HISTORY_BYTES = int(os.getenv("MAX_HISTORY_BYTES", "6000"))

def trim_history(chat_history):
    if not chat_history: return chat_history
    system_msgs = [m for m in chat_history if m.get("role") == "system"]
    other_msgs = [m for m in chat_history if m.get("role") != "system"]
    if len(other_msgs) > MAX_HISTORY_TURNS * 2:
        other_msgs = other_msgs[-(MAX_HISTORY_TURNS * 2):]
    # Drop whole turns from the oldest end until the rest fits. The newest turn
    # is never dropped, however long it is: it is what the student just said,
    # and answering without it is worse than paying for it.
    def size(message):
        return len((message.get("content") or "").encode("utf-8"))

    total = sum(size(m) for m in other_msgs)
    while len(other_msgs) > 1 and total > MAX_HISTORY_BYTES:
        total -= size(other_msgs[0])
        other_msgs.pop(0)
    return system_msgs + other_msgs

def list_microphones():
    """Print the capture devices and their indices, for setting MIC_DEVICE_INDEX."""
    for index, name in enumerate(sr.Microphone.list_microphone_names()):
        print(f"{index}  {name}", flush=True)

def calibrate_microphone(seconds=6):
    """Measure room noise against speech and print MIC_ENERGY_* values for .env.

    Only two numbers matter: how loud the room is with nobody talking, and how
    loud the student is when they are. The detection threshold has to sit
    between them -- and on a cheap USB mic at conversational distance those two
    bands very nearly overlap, so guessing a threshold does not work."""
    import audioop
    mic = get_microphone_device(detect_microphone_index())
    if mic is None:
        print("No microphone found.", flush=True)
        return
    recognizer = sr.Recognizer()

    def sample(instruction):
        input(f"\n{instruction}\nPress Enter when ready...")
        print(f"Recording {seconds}s...", flush=True)
        with mic as source:
            audio = recognizer.record(source, duration=seconds)
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        step = 2048
        return sorted(audioop.rms(raw[i:i + step], 2)
                      for i in range(0, len(raw) - step, step))

    quiet = sample("Stay COMPLETELY SILENT for the next few seconds.")
    loud = sample("Now SPEAK normally, continuously, the whole time.")

    pct = lambda c, q: c[int(len(c) * q)]
    print(f"\nRoom noise : median {pct(quiet, 0.5):5d}  p90 {pct(quiet, 0.9):5d}  max {quiet[-1]:5d}")
    print(f"Your speech: median {pct(loud, 0.5):5d}  p90 {pct(loud, 0.9):5d}  max {loud[-1]:5d}")

    floor, ceiling = int(pct(quiet, 0.9)), int(pct(loud, 0.9))
    if ceiling <= floor:
        print("\nWARNING: your speech is no louder than the room. Move the microphone "
              "closer or raise its gain in `alsamixer` -- no threshold can separate "
              "these two.", flush=True)
    print(f"\nPut these in .env:\n  MIC_ENERGY_FLOOR={floor}\n  MIC_ENERGY_CEILING={ceiling}",
          flush=True)

def detect_microphone_index():
    if MIC_DEVICE_INDEX:
        try:
            return int(MIC_DEVICE_INDEX)
        except ValueError:
            print(f"[MIC] MIC_DEVICE_INDEX={MIC_DEVICE_INDEX!r} is not a number; "
                  f"falling back to auto-detection.", flush=True)
    try:
        mic_names = sr.Microphone.list_microphone_names()
        for index, name in enumerate(mic_names):
            if any(candidate.lower() in name.lower() for candidate in PREFERRED_MIC_NAMES): return index
    except Exception: pass
    return None

def get_microphone_device(mic_index=None):
    """The pinned capture device, falling back to the default one -- loudly.

    The fallback used to be silent, and on this device that is the difference
    between a broken microphone and a broken assistant. MIC_DEVICE_INDEX exists
    precisely because the two dongles report the SAME name and only one of them
    is the real microphone; the default is the other one, whose empty mic jack
    returns noise floor and nothing else (measured: RMS 27 against 300+ for the
    real one). So when the pinned index cannot be opened -- unplugged, or still
    held by a previous run that has not died -- Liza comes up looking perfectly
    healthy, listening to a device that will never hear anything, forever.
    Saying so in the log is the whole point of this function."""
    if mic_index is not None:
        try:
            return sr.Microphone(device_index=mic_index, chunk_size=MIC_CHUNK)
        except Exception as exc:
            print(f"[MIC] Could not open device {mic_index} ({exc}). Something else "
                  f"may still be holding it -- check for an older Liza process. "
                  f"Falling back to the system default, which on this hardware is "
                  f"probably the WRONG microphone.", flush=True)
    try:
        return sr.Microphone(chunk_size=MIC_CHUNK)
    except Exception as exc:
        print(f"[MIC] No usable capture device at all: {exc}", flush=True)
        return None


def disable_mic_agc(mic_index):
    """Turn the capture dongle's Auto Gain Control off.

    AGC on a cheap C-Media chip is actively harmful for speech recognition: it
    rides the gain up during the quiet between words and clamps down on the
    onset of the next one, which flattens exactly the peaks Whisper needs and
    lifts the noise floor in between. Measured on this device, same room, same
    phrase, AGC on vs off -- speech p90 went 653 -> 1599 and its peak 1605 ->
    2593, while room noise did not rise at all (max 457 -> 392). Nearly a
    fourfold gain in separation for a mixer switch.

    That smearing is what "close the test file" being heard as "Here is the
    closed test file." looks like: a low-contrast clip where Whisper fills the
    mushy leading edge with plausible connective words rather than returning
    what was actually said.

    Done here, at every startup, because it is a hardware setting that does not
    survive a reboot or a replug -- and a setting nobody remembers to restore is
    a bug that comes back on its own. Best effort: a dongle without the control
    is fine, it just does not have the problem."""
    try:
        names = sr.Microphone.list_microphone_names()
        name = names[mic_index] if mic_index is not None and mic_index < len(names) else ""
    except Exception:
        name = ""
    # PortAudio spells the ALSA card into the device name: "... (hw:3,0)".
    match = re.search(r'hw:(\d+),', name or "")
    if not match:
        return
    card = match.group(1)
    try:
        done = subprocess.run(["amixer", "-c", card, "sset", "Auto Gain Control", "off"],
                              capture_output=True, text=True, timeout=5)
    except Exception as exc:
        print(f"[MIC] Could not reach amixer to disable AGC: {exc}", flush=True)
        return
    if done.returncode == 0:
        print(f"[MIC] Auto Gain Control off on card {card}.", flush=True)
    else:
        # Not every dongle exposes the control, and that is not a problem.
        print(f"[MIC] No Auto Gain Control to disable on card {card}.", flush=True)


class HeldMicrophone:
    """The capture device, opened ONCE and kept open for the whole session.

    Every listen used to be wrapped in its own `with mic_device as source:`,
    which opens the PCM, reads, and closes it again. This dongle does not
    survive that. Measured here, deliberately, with nothing else running: six
    identical four-second listens, re-opening each time, and the FOURTH one
    never came back -- pyaudio's read() blocked inside ALSA and stayed blocked.
    The same six listens against a stream that was opened once all returned
    normally, in 5.6-6.2s each.

    That single stall is most of what "Liza stopped hearing me" actually is. The
    UI keeps animating because Tk is on another thread, so she looks perfectly
    alive while no microphone read will ever return again -- and because the
    stall is a C call on the main thread, Python never gets to run a signal
    handler either, so the process cannot even be stopped with anything short of
    SIGKILL. It is also why the failure clusters around music and video: the
    barge-in check re-opened the device every couple of seconds for the whole
    length of a track, so a song is several dozen chances to hit it.

    reopen() exists because holding the stream open makes the stall rare, not
    impossible -- a dongle knocked on its cable still has to be recoverable
    without a restart. See the watchdog in ai_loop()."""

    # ALSA needs a moment after a handle is torn down before the same device can
    # be opened again -- PortAudio answers immediately in between with "Invalid
    # device info", which is a stale device list rather than a missing dongle.
    # Retrying through that window is the difference between a two-second gap
    # and a dead assistant.
    REOPEN_ATTEMPTS = 6
    REOPEN_DELAY_S = 1.0

    def __init__(self, factory):
        self._factory = factory      # () -> sr.Microphone or None
        self._mic = None
        self._source = None
        self.generation = 0          # bumped on every reopen, for the watchdog
        # The watchdog reopens from ITS thread while ai_loop is blocked inside a
        # read on the device -- that is the whole point of it -- so the two do
        # collide, and unserialised they interleave into one closing what the
        # other has just opened. Seen exactly once and it cost the session: the
        # loop got a half-built device, raised, and the ai_loop thread died
        # while the watchdog was still cheerfully logging success.
        self._lock = threading.RLock()

    @property
    def opened(self):
        return self._source is not None

    def source(self):
        """The open source, opening it on first use. Never returns None."""
        with self._lock:
            if self._source is None:
                self.reopen()
            return self._source

    def reopen(self):
        """Tear the device down and bring it back. Returns the new source.

        Raises only after every attempt has failed, which means the dongle is
        genuinely gone rather than merely busy."""
        with self._lock:
            self.close()
            last = None
            for attempt in range(self.REOPEN_ATTEMPTS):
                if attempt:
                    time.sleep(self.REOPEN_DELAY_S)
                try:
                    mic = self._factory()
                    if mic is None:
                        last = "no capture device"
                        continue
                    # __enter__/__exit__ rather than a with-block precisely
                    # because the stream has to outlive this call.
                    self._mic = mic
                    self._source = mic.__enter__()
                    self.generation += 1
                    if attempt:
                        print(f"[MIC] Re-opened on attempt {attempt + 1}.", flush=True)
                    return self._source
                except Exception as exc:
                    last = exc
                    self._mic = self._source = None
            raise RuntimeError(f"no microphone available after "
                               f"{self.REOPEN_ATTEMPTS} attempts ({last})")

    def close(self):
        with self._lock:
            mic, self._mic, self._source = self._mic, None, None
            if mic is not None:
                try: mic.__exit__(None, None, None)
                except Exception: pass

    # `with mic_device as source:` still reads correctly at the call sites, but
    # now it hands out the stream that is ALREADY open and leaves it open on the
    # way out. That is the entire fix: the shape of the calling code is
    # unchanged, and the device is opened once instead of once per listen.
    def __enter__(self):
        return self.source()

    def __exit__(self, *_exc):
        return False



class VoiceListener:
    """Continuous capture with VAD endpointing and a pre-roll buffer.

    This replaces recognizer.listen() everywhere. Four things were wrong on this
    hardware, and they are exactly the complaints:

    "She only hears me if I lean into the microphone."  listen() opens a phrase
    on a loudness threshold, and loudness does not separate speech from noise
    here -- see the measurements above VAD_ENABLED. webrtcvad decides on signal
    shape per 30ms frame instead, so distance stops mattering nearly as much.

    "I have to say everything twice."  Two separate causes. listen() begins
    recording at the moment it decides speech has started, so the syllable that
    convinced it is already gone; here the stream runs into a ring buffer
    continuously and the decision reaches BACK into it (VAD_PREROLL_MS). And the
    microphone was shut for the whole of every reply plus a settle delay after
    it, so the start of the next sentence landed while nothing was recording.
    Nothing is shut any more.

    "I cannot interrupt her."  There was no code path that listened while she
    spoke. This thread does not care what else is happening, so barge-in becomes
    a question of reading a signal that is already being maintained -- see
    barge_in_ready().

    And one nobody reported because it is invisible: the capture stream was
    dropping roughly a fifth of ALL audio. See _open() for the measurement. Words
    were being torn out of the middle of sentences before anything above ever
    saw them.

    The device is opened here and only here. PortAudio delivers into _callback on
    its own thread, _worker turns that into scored frames, and every consumer
    reads the buffer rather than the device -- so there is no blocking read left
    in the program to wedge.
    """

    def __init__(self, mic_index):
        self._index = mic_index
        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS) if (VAD_ENABLED and webrtcvad) else None
        self._rate = CAPTURE_RATE
        # 30 seconds of scored history. Bounded because nothing consumes between
        # turns and an unbounded buffer would grow for the whole session;
        # dropping the OLDEST frame is what makes the tail -- the part anyone
        # might want -- the part that survives.
        self._frames = collections.deque(maxlen=int(30_000 // VAD_FRAME_MS))
        # Raw bytes from PortAudio's thread to ours. Bounded for the same
        # reason, and because a queue that grows is a callback that is being
        # asked to buffer for a worker that has stopped.
        self._raw = queue.Queue(maxsize=4000)
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._threads = []
        self._preroll_frames = max(1, VAD_PREROLL_MS // VAD_FRAME_MS)
        self._pa = None
        self._stream = None
        self._lock = threading.RLock()
        # Bumped on every reopen, for the watchdog -- same contract as
        # HeldMicrophone, so start_mic_watchdog() takes either one.
        self.generation = 0
        self._last_audio_at = 0.0
        # Barge-in signals, written by the worker thread, read by ai_loop.
        self._loud_run_ms = 0.0
        self._echo_level = 0.0
        self._was_playing = False
        self._audible_frames = 0
        self._playing_since = 0.0
        # Audio the student had already spoken when their interruption cut the
        # reply off; see hold_barge_in().
        self._carry = []

    @property
    def available(self):
        """False when webrtcvad is missing; callers fall back to listen()."""
        return self._vad is not None

    @property
    def read_state(self):
        """(since, budget) for the watchdog: how long audio has been absent.

        There is no blocking read to time any more, so the thing worth watching
        is the opposite -- a callback that has stopped being called. Silence from
        the device for longer than the budget plus MIC_STALL_GRACE_S is a dead
        stream, which is the same failure the old watchdog existed for."""
        if self._last_audio_at <= 0.0:
            return (0.0, 0.0)
        return (self._last_audio_at, 3.0)

    # -- device -------------------------------------------------------------

    def _open(self):
        """Open the capture stream. Raises if the device is not there."""
        with self._lock:
            self._close()
            self._pa = pyaudio.PyAudio()
            kwargs = dict(format=pyaudio.paInt16, channels=1, rate=self._rate,
                          input=True, stream_callback=self._callback,
                          # NOT MIC_CHUNK, and this is not a tuning preference.
                          #
                          # Measured on this dongle, with an idle machine and
                          # nothing else holding the device: a fixed 4096-frame
                          # buffer captures 82% of realtime and PortAudio raises
                          # paInputOverflowed on it. 8192 gives 62%, 16384 gives
                          # 57% -- it gets WORSE the larger the request, which is
                          # the signature of the ALSA ring being overrun rather
                          # than of the reader being slow. 4096 frames is 93ms of
                          # audio against a default input latency of a few
                          # milliseconds, so the ring is long gone before the
                          # buffer PortAudio is filling is anywhere near done.
                          #
                          # Letting PortAudio size it itself: 99.9% of realtime,
                          # zero overflow flags. arecord on the same device gets
                          # 100%, which is what proved the hardware was never the
                          # problem.
                          #
                          # This was dropping about a fifth of every recording
                          # the program has ever made -- syllables missing from
                          # the middle of sentences, which is transcribed as a
                          # different sentence, or as nothing.
                          frames_per_buffer=pyaudio.paFramesPerBufferUnspecified)
            if self._index is not None:
                kwargs["input_device_index"] = self._index
            try:
                self._stream = self._pa.open(**kwargs)
            except Exception:
                # A different dongle with a different native rate. Ask PortAudio
                # what this one actually wants rather than failing outright.
                try:
                    info = self._pa.get_device_info_by_index(
                        self._index if self._index is not None
                        else self._pa.get_default_input_device_info()["index"])
                    self._rate = int(info["defaultSampleRate"])
                except Exception:
                    raise
                print(f"[VAD] {CAPTURE_RATE}Hz refused; opening at "
                      f"{self._rate}Hz instead.", flush=True)
                kwargs["rate"] = self._rate
                self._stream = self._pa.open(**kwargs)
            self._stream.start_stream()
            self.generation += 1
            self._last_audio_at = time.time()
            return self._stream

    def _close(self):
        with self._lock:
            stream, self._stream = self._stream, None
            pa, self._pa = self._pa, None
            if stream is not None:
                try: stream.stop_stream()
                except Exception: pass
                try: stream.close()
                except Exception: pass
            if pa is not None:
                try: pa.terminate()
                except Exception: pass

    def reopen(self):
        """Tear the device down and bring it back; the watchdog calls this.

        Retried through the window where ALSA has released the handle but
        PortAudio's device list has not caught up, which is the same reason
        HeldMicrophone.reopen() retries."""
        last = None
        for attempt in range(6):
            if attempt:
                time.sleep(1.0)
            try:
                self._open()
                if attempt:
                    print(f"[VAD] Capture re-opened on attempt {attempt + 1}.", flush=True)
                return
            except Exception as exc:
                last = exc
        raise RuntimeError(f"no microphone available after 6 attempts ({last})")

    def start(self):
        if self._threads or self._vad is None:
            return
        try:
            self._open()
        except Exception as exc:
            print(f"[VAD] Could not open the capture device ({exc}); "
                  f"the reader will keep trying.", flush=True)
        for target, name in ((self._worker, "mic-worker"), (self._monitor, "mic-monitor")):
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)
        print(f"[VAD] Continuous capture on: aggressiveness {VAD_AGGRESSIVENESS}, "
              f"{VAD_PREROLL_MS}ms pre-roll, barge-in "
              f"{'on' if BARGE_IN_ENABLED else 'off'}.", flush=True)

    def stop(self):
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self._close()

    # -- capture ------------------------------------------------------------

    def _callback(self, in_data, frame_count, time_info, status):
        """PortAudio's thread. Does as close to nothing as it can.

        Anything slow here is dropped input, so the only job is to hand the
        bytes over. Dropping on a full queue rather than blocking is deliberate:
        a stalled worker must not turn into a stalled capture device as well."""
        self._last_audio_at = time.time()
        try:
            self._raw.put_nowait(in_data)
        except queue.Full:
            pass
        return (None, pyaudio.paContinue)

    def _monitor(self):
        """Bring the stream back if it dies underneath us.

        A USB dongle knocked on its cable stops calling _callback and reports
        nothing; without this the program would look perfectly healthy and never
        hear another word."""
        while not self._stop.is_set():
            time.sleep(2.0)
            with self._lock:
                stream = self._stream
                alive = False
                if stream is not None:
                    try: alive = stream.is_active()
                    except Exception: alive = False
            stale = self._last_audio_at and (time.time() - self._last_audio_at) > 5.0
            if alive and not stale:
                continue
            print(f"[VAD] Capture stream {'stalled' if alive else 'stopped'}; "
                  f"re-opening.", flush=True)
            try:
                self.reopen()
            except Exception as exc:
                print(f"[VAD] Could not re-open the capture device: {exc}", flush=True)

    def _worker(self):
        """Turn raw device bytes into 30ms VAD-scored frames.

        Never allowed to die: this thread going down is Liza going permanently
        deaf while the UI carries on animating, which is the failure mode the
        watchdog was written for. Every error path waits and tries again."""
        resample = None     # audioop.ratecv state, carried across buffers
        carry = b""         # partial 30ms frame left over from the last buffer
        generation = -1
        while not self._stop.is_set():
            try:
                raw = self._raw.get(timeout=0.2)
            except queue.Empty:
                continue

            # A reopen invalidates the resampler state and any partial frame
            # along with it. Carrying them over splices two unrelated streams
            # together, which the VAD reads as a click.
            if self.generation != generation:
                generation, resample, carry = self.generation, None, b""

            try:
                if self._rate != VAD_RATE:
                    # The dongle only opens at 44.1k; webrtcvad only accepts
                    # 8/16/32/48k. This is the one place that gap is closed, and
                    # 16k is what Whisper wants downstream anyway.
                    raw, resample = audioop.ratecv(raw, 2, 1, self._rate,
                                                   VAD_RATE, resample)
            except Exception as exc:
                print(f"[VAD] Resample failed ({exc}); dropping a buffer.", flush=True)
                resample, carry = None, b""
                continue

            buf = carry + raw
            whole = len(buf) // VAD_FRAME_BYTES
            carry = buf[whole * VAD_FRAME_BYTES:]
            if not whole:
                continue
            speaking = playback_active.is_set()
            playing = speaking or media_active.is_set()
            # Her own voice outranks the track when both are somehow audible:
            # cutting her off is the disruptive one, so it keeps the strict bar.
            margin = BARGE_IN_MARGIN if speaking else MEDIA_BARGE_IN_MARGIN
            with self._cv:
                for i in range(whole):
                    frame = buf[i * VAD_FRAME_BYTES:(i + 1) * VAD_FRAME_BYTES]
                    try:
                        voiced = self._vad.is_speech(frame, VAD_RATE)
                    except Exception:
                        voiced = False
                    try:
                        level = audioop.rms(frame, 2)
                    except Exception:
                        level = 0
                    self._frames.append((frame, voiced, level))
                    self._track_barge_in(voiced, level, playing, margin)
                self._cv.notify_all()

    # -- barge-in -----------------------------------------------------------

    def _track_barge_in(self, voiced, level, playing, margin=None):
        """Maintain 'somebody is talking over her'. Worker thread, under _cv.

        With no echo canceller the only thing separating Liza's voice from the
        student's is that the student is nearer the microphone than the speaker
        is. So her own voice is measured while she speaks and becomes the
        reference the student has to clear -- adaptive rather than constant,
        because that reference moves the moment anyone touches the volume."""
        if not playing:
            self._was_playing = False
            self._playing_since = 0.0
            self._echo_level = 0.0
            self._loud_run_ms = 0.0
            self._audible_frames = 0
            return
        if not self._was_playing:
            # Seeded on the first frame of playback rather than climbing from
            # zero, which would leave the bar under her own voice for the first
            # moments of every reply. BARGE_IN_LEAD_S covers the rest of that
            # window, since playback_active is set before any audio actually
            # reaches the speaker.
            self._was_playing = True
            self._echo_level = float(level)
            self._playing_since = time.time()
            self._audible_frames = 0
        # A reference seeded on silence is not a reference. Until it has had
        # long enough to measure whatever is actually coming out of the speaker,
        # the bar is meaningless and nothing is allowed to clear it -- which is
        # what let a video's own soundtrack "interrupt" the video.
        if time.time() - self._playing_since < BARGE_IN_LEAD_S:
            self._echo_level = max(self._echo_level, float(level))
            self._loud_run_ms = 0.0
            return
        bar = max(self._echo_level * (BARGE_IN_MARGIN if margin is None else margin),
                  MIN_SPEECH_RMS)
        # BARGE_IN_DEBUG=1 prints what the gate actually sees, once per frame
        # while she is talking. There is no way to tune this by reasoning about
        # it -- the numbers depend on the speaker volume, the distance between
        # the speaker and the dongle, and the room -- so when somebody reports
        # that they cannot interrupt her, this is the first thing to turn on.
        # Read `lvl` against `bar` while talking over her: lvl below bar means
        # the margin is too high for this room, not that the detector is broken.
        if BARGE_IN_DEBUG and voiced:
            print(f"[BARGE?] lvl={level:6d} bar={bar:7.0f} echo={self._echo_level:7.0f} "
                  f"run={self._loud_run_ms:5.0f}ms {'OVER' if level > bar else ''}",
                  flush=True)

        if voiced and level > bar:
            self._loud_run_ms += VAD_FRAME_MS
        else:
            self._loud_run_ms = 0.0
            # A GAP IN PLAYBACK MUST NOT DRAG THE REFERENCE DOWN, and this is
            # the guard for it. Observed on this device, in the log, twice:
            #
            #   underrun!!! (at least 1539.372 ms long)
            #   [BARGE-IN] Student spoke over the reply; stopping.
            #
            # aplay starved mid-reply, so the speaker went silent while
            # playback_active was still set. The decay below then ran over a
            # second and a half of an empty room -- at 0.02 a second of silence
            # takes the reference to about a third of what her voice was -- and
            # when the audio came back her OWN next syllable cleared the bar it
            # had just lowered. She interrupted herself, and to the student that
            # is the device cutting off half way through its own answer for no
            # reason. Nothing was said in the room at all.
            #
            # So the reference only moves on frames with something in them.
            # Silence carries no information about how loud she is coming back
            # through the microphone, which is the only thing this is measuring.
            if level < MIN_SPEECH_RMS:
                return
            self._audible_frames += 1
            # Only frames that are NOT a candidate interruption move the
            # reference. Updating it on every frame -- what this did first --
            # let the interrupting voice drag up the very bar it had to clear:
            # measured, a voice a full 1.8x louder than her lost the race within
            # two frames and barge-in never armed at all.
            #
            # Rises fast and falls slowly, so the reference tracks the TOP of her
            # voice rather than its average. Speech has a high crest factor -- a
            # vowel is several times the RMS of the sentence around it -- so a
            # bar drawn through the mean of her own speech is one her own vowels
            # clear unaided, and she would cut herself off mid-word.
            alpha = 0.25 if level > self._echo_level else 0.02
            self._echo_level = (1.0 - alpha) * self._echo_level + alpha * level

    def barge_in_ready(self):
        """True when speech clearly louder than Liza's own has run long enough.

        AND when there has been enough of her voice to know what "louder than
        her" even means. That second half is what stops her cutting herself off
        in the first breath of a reply -- reported as her saying a word or two
        and then going straight back to listening, with nobody having spoken.
        playback_active is set before any sound reaches the speaker, so the
        reference starts seeded on a silent room; until some of her own voice
        has actually been measured, the bar is drawn under everything and her
        own first syllable clears it. BARGE_IN_LEAD_S alone did not cover this,
        because it is a clock and the thing being waited for is audio -- on a
        slow first Cartesia response the 0.7s elapses while the room is still
        silent.
        """
        return (BARGE_IN_ENABLED
                and self._audible_frames >= BARGE_IN_WARMUP_FRAMES
                and self._loud_run_ms >= BARGE_IN_MS)

    def reset_barge_in(self):
        self._loud_run_ms = 0.0

    def hold_barge_in(self):
        """Set the interrupting audio aside, and drop everything before it.

        The student is part-way into a word when the reply is cut, and those few
        hundred milliseconds exist only in the ring buffer. Between here and the
        listen that follows, playback still has to be torn down and the queue
        flushed -- easily long enough for that audio to be drained away as
        ordinary pre-roll, which is what would make an interrupting sentence
        arrive with its first word missing.

        Moving the frames out rather than copying them also clears the rest of
        the buffer, which at this instant is entirely Liza's own voice."""
        with self._cv:
            frames = list(self._frames)
            self._frames.clear()
            keep = int(self._loud_run_ms // VAD_FRAME_MS) + 4    # + ~120ms of lead
            self._carry = [f[0] for f in frames[-keep:]] if keep > 0 and frames else []
            self._loud_run_ms = 0.0

    def has_carry(self):
        return bool(self._carry)

    # -- consumption --------------------------------------------------------

    def _next_frame(self, timeout):
        with self._cv:
            if not self._frames:
                self._cv.wait(timeout)
            return self._frames.popleft() if self._frames else None

    def drain(self, keep_ms=0):
        """Throw away buffered audio, keeping at most the last keep_ms of it.

        Used where what is in the buffer is known NOT to be the student: her own
        reply, or a track that was playing until a moment ago."""
        keep = max(0, int(keep_ms // VAD_FRAME_MS))
        with self._cv:
            while len(self._frames) > keep:
                self._frames.popleft()
            self._loud_run_ms = 0.0

    def wait_for_utterance(self, timeout, phrase_limit, end_silence, preroll_ms=None):
        """Block until a phrase starts and finishes. sr.AudioData, or None.

        Same contract as recognizer.listen(): `timeout` bounds only the wait for
        speech to BEGIN, `phrase_limit` caps the phrase itself, and `end_silence`
        is the pause that ends it (pause_threshold). Returning sr.AudioData is
        deliberate -- audio_rms(), audio_seconds() and get_wav_data() all work on
        it unchanged, so nothing downstream of the microphone had to move."""
        if self._vad is None:
            return None

        started = time.time()
        voiced_ms = silence_ms = 0.0
        speech_at = 0.0
        tail_frames = max(1, VAD_TAIL_MS // VAD_FRAME_MS)

        with self._cv:
            collected, self._carry = list(self._carry), []
        triggered = bool(collected)
        if triggered:
            # An interruption already in progress: the phrase is open, and
            # hold_barge_in() has already cleared everything before it, so there
            # is nothing left to drain.
            speech_at = time.time()
        else:
            # Anything older than the pre-roll window predates this call. Most
            # often it is Liza's own voice from the reply that just ended, and it
            # must not become the opening of the student's next sentence -- which
            # is why the caller can cap the window at the moment she stopped.
            self.drain(keep_ms=VAD_PREROLL_MS if preroll_ms is None else preroll_ms)

        while not self._stop.is_set():
            frame = self._next_frame(0.2)
            if frame is None:
                # No audio arrived. Only meaningful before the phrase opens;
                # once it is open the worker is the only thing that can end it.
                if not triggered and time.time() - started >= timeout:
                    return None
                continue

            data, voiced, _level = frame
            collected.append(data)

            if not triggered:
                voiced_ms = voiced_ms + VAD_FRAME_MS if voiced else 0.0
                if voiced_ms >= VAD_START_MS:
                    triggered = True
                    speech_at = time.time()
                    silence_ms = 0.0
                    continue
                # Hold only the pre-roll while waiting: bounded memory, and the
                # clip does not open with a minute of room tone for Whisper to
                # read meaning into.
                if len(collected) > self._preroll_frames:
                    del collected[:-self._preroll_frames]
                if time.time() - started >= timeout:
                    return None
            else:
                if voiced:
                    silence_ms = 0.0
                else:
                    silence_ms += VAD_FRAME_MS
                    if silence_ms >= end_silence * 1000.0:
                        # Keep a little air after the last syllable; drop the
                        # rest of the pause rather than paying Whisper to read it.
                        drop = int(silence_ms // VAD_FRAME_MS) - tail_frames
                        if drop > 0:
                            del collected[-drop:]
                        break
                if time.time() - speech_at >= phrase_limit:
                    break

        if not triggered or not collected:
            return None
        return sr.AudioData(self._normalise(b"".join(collected)), VAD_RATE, 2)

    @staticmethod
    def _normalise(raw):
        """Make-up gain towards MIC_TARGET_PEAK; see it for why this is here.

        The gain is derived from the measured peak, so the result lands ON the
        target and can never overflow -- and where the cap binds it lands below
        it, quieter still. No clipping is possible either way."""
        try:
            peak = audioop.max(raw, 2)
        except Exception:
            return raw
        if peak <= 0:
            return raw
        gain = min(max((MIC_TARGET_PEAK * 32767.0) / peak, 1.0), MIC_MAX_GAIN)
        if gain <= 1.01:
            return raw
        try:
            return audioop.mul(raw, 2, gain)
        except Exception:
            return raw


def _cleanup(*_args):
    stop_playback_event.set()
    with subprocess_lock:
        for proc in active_subprocesses:
            try: proc.terminate()
            except Exception: pass
        active_subprocesses.clear()
    try: audio_queue.put_nowait(None)
    except Exception: pass

def _cleanup_and_exit(signum, _frame):
    """Clean up, then actually die.

    Installing _cleanup itself as the handler (what this used to do) silently
    turned Liza into a process that cannot be stopped: Python replaces the
    default terminate action with the handler, so once the handler returned the
    process simply carried on. `kill`, `timeout` and a systemd restart all
    appeared to work and left the old instance running -- and the old instance
    still owns the microphone, so the "restarted" Liza gets an ALSA busy error
    and never hears a thing. Observed here: two survivors of `timeout` holding
    the capture device between them.

    os._exit rather than sys.exit because this runs on whichever thread took the
    signal: SystemExit would only unwind that one thread, and the daemon threads
    plus a blocking PyAudio read would keep the process up regardless."""
    _cleanup()
    os._exit(128 + signum)

atexit.register(_cleanup)
signal.signal(signal.SIGTERM, _cleanup_and_exit)
signal.signal(signal.SIGINT, _cleanup_and_exit)

# How far past its OWN budget a read may run before the device is called wedged.
# The budget is timeout + phrase_time_limit, which is what recognizer.listen()
# promises to return within, so this is pure margin for ALSA setup and a busy
# Pi -- not a guess at how long a listen ought to take.
MIC_STALL_GRACE_S = float(os.getenv("MIC_STALL_GRACE_S", "10"))

def start_mic_watchdog(get_state, mic_device=None):
    """Notices a wedged capture device, and puts it back.

    PyAudio's read() is a blocking C call, so recognizer.listen()'s timeout and
    phrase_time_limit cannot fire if the device stops delivering audio: the read
    never returns, the loop never comes round, and Liza goes deaf for the rest
    of the session with no error anywhere. The UI is on the Tk thread so it
    carries on animating, which is exactly why this looks like "she just stopped
    hearing me" rather than like a crash.

    Holding the stream open (see HeldMicrophone) is what makes this rare. This
    is the backstop for the rest: a dongle bumped on its cable, or a USB reset.
    Re-opening from THIS thread is deliberate -- the loop's own thread is the one
    stuck inside the read and cannot do anything about it. Tearing the PCM down
    underneath that read is what makes it return."""
    def watch():
        warned_generation = None
        while True:
            time.sleep(5)
            started, budget = get_state()
            if not started:
                continue
            blocked = time.time() - started
            if blocked <= budget + MIC_STALL_GRACE_S:
                continue
            if mic_device is None:
                if warned_generation != "static":
                    print(f"[WATCHDOG] Microphone read blocked {blocked:.0f}s against a "
                          f"{budget:.0f}s budget -- the capture device is wedged. "
                          f"Restart Liza.", flush=True)
                    warned_generation = "static"
                continue
            # Only once per wedge: reopen() bumps the generation, so a device
            # that stalls again later is treated as a new event and retried,
            # while one that is simply slow to unblock is not hammered.
            if warned_generation == mic_device.generation:
                continue
            warned_generation = mic_device.generation
            print(f"[WATCHDOG] Microphone read blocked {blocked:.0f}s against a "
                  f"{budget:.0f}s budget; re-opening the capture device.", flush=True)
            try:
                mic_device.reopen()
                print("[WATCHDOG] Capture device re-opened.", flush=True)
            except Exception as exc:
                print(f"[WATCHDOG] Could not re-open the microphone: {exc}", flush=True)
    threading.Thread(target=watch, daemon=True).start()

def _log_thread_crash(args):
    """A dead worker thread is invisible from the UI -- the screen keeps
    animating while nothing listens any more, which is exactly how the wake-word
    crash went unnoticed. Make it loud instead."""
    print(f"[CRASH] Thread {getattr(args.thread, 'name', '?')} died:", flush=True)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)

threading.excepthook = _log_thread_crash

def interrupt_playback():
    global active_subprocesses
    stop_playback_event.set()
    
    with subprocess_lock:
        for proc in active_subprocesses:
            try: proc.terminate()
            except Exception: pass
        active_subprocesses.clear()
    
    while not audio_queue.empty():
        try: audio_queue.get_nowait()
        except Exception: pass
        audio_queue.task_done()

    # Release the player from the dead response so the next one starts on a fresh pipeline.
    audio_queue.put("[END_OF_RESPONSE]")

    stop_playback_event.clear()

# ==========================================
# Language Routing (Hindi / English / Hinglish)
# ==========================================
# Groq reports full language names ("Hindi", "Urdu"), not ISO codes. Urdu and Hindi
# are the same spoken language, so a student speaking Hindi is routinely reported as
# either one; both mean "reply in Hindi" here.
HINDI_STT_ALIASES = {"hi", "hin", "hindi", "ur", "urd", "urdu"}

# The only languages this device speaks. Whisper auto-detects across all ~99 of
# them, and on a short or noisy clip it will confidently pick one nobody in the
# room is speaking -- observed turning a plain English question into Spanish
# ("¿Quién es Yemi?"). That is not a harmless mislabel: the wrong transcript is
# answered as if it were real, the reply is written in that language, and it
# then sits in chat_history where it few-shots every later turn into Spanish
# too, outvoting the "Reply in English only" system instruction. Anything
# outside this set is re-read with the language forced; see the re-read below.
STT_ALLOWED_LANGUAGES = {"en", "eng", "english"} | HINDI_STT_ALIASES

def detect_user_language(text, stt_language=None):
    """What the student just spoke, used to steer the reply: 'hi', 'en' or 'hinglish'."""
    has_devanagari = bool(RE_DEVANAGARI.search(text))
    has_latin_words = bool(RE_LATIN_WORD.search(text))
    stt_language = (stt_language or "").strip().lower()
    stt_says_hindi = stt_language in HINDI_STT_ALIASES

    if has_devanagari and has_latin_words: return "hinglish"
    if has_devanagari: return "hi"
    # Whisper heard Hindi but wrote it in Latin letters, i.e. romanised Hinglish.
    if stt_says_hindi: return "hinglish"
    return "en"

def detect_tts_language(text):
    """Which Cartesia voice language a sentence should be spoken in."""
    return "hi" if RE_DEVANAGARI.search(text) else "en"

# Groq rejects a longer STT prompt outright, with a 400.
#
# "Both fit inside Whisper's prompt window" was true of Whisper and false of
# this API. The seed is 443 characters and her last 40 words were appended to
# it, so any reply of ordinary length pushed the total past the cap -- observed
# at 905. And because a 400 is deterministic, the retry could not help and every
# single utterance failed: she went completely deaf, in a way that looks from
# the outside exactly like the microphone having died.
STT_PROMPT_MAX_CHARS = 896

def stt_prompt_size(text):
    """The prompt length as the API appears to count it.

    Measured in UTF-8 BYTES, and that is not the obvious choice, so: the API
    does not count Python characters. A prompt of 1404 codepoints was rejected
    as "1687 characters" -- not the 1404 codepoints, not the 3042 UTF-8 bytes,
    and not UTF-16 either. Devanagari evidently costs it somewhere around 1.2
    units apiece by whatever it really measures.

    Rather than reverse-engineer an undocumented metric that can change without
    warning, this uses a bound that cannot be wrong in the dangerous direction:
    UTF-8 bytes are 1 per ASCII character and 3 per Devanagari one, so they are
    >= any per-character metric between those. Staying under the cap in bytes
    keeps us under it however the API counts.

    The cost falls only on Devanagari, which fits about 298 characters instead
    of 745. For English -- where bytes and characters are the same thing -- this
    changes nothing at all. A prompt is a bias hint, not content; a shorter one
    is a smaller hint, never a wrong answer."""
    return len((text or "").encode("utf-8"))

def clamp_stt_prompt(prompt):
    """Trim an STT prompt to what the API will accept, on a word boundary.

    Trimmed from the END so STT_SEED_PROMPT survives intact: the seed is the
    deliberately chosen subject vocabulary, while the tail is just whatever she
    happened to say last."""
    prompt = prompt or ""
    if stt_prompt_size(prompt) <= STT_PROMPT_MAX_CHARS:
        return prompt
    # Cut in the byte domain, then drop any partial character the cut created.
    cut = prompt.encode("utf-8")[:STT_PROMPT_MAX_CHARS].decode("utf-8", "ignore")
    spaced = cut.rsplit(" ", 1)[0]
    return spaced if spaced else cut

def transcribe(wav_data, prompt, language=None, model=None, attempts=2):
    """Groq STT. With no `language` Whisper auto-detects; pass one to force it.

    Retried once because the failure is invisible and expensive: on a Pi over
    home wifi a dropped connection here used to surface as one [STT Error] line
    and total silence, so the student got no reply at all and no reason for it,
    and had to guess that repeating themselves was the fix. wav_data is bytes,
    so replaying the same audio costs nothing but the call."""
    params = {
        "file": ("temp.wav", wav_data),
        "model": model or STT_MODEL,
        "response_format": "verbose_json",
        "temperature": 0.0,
        # Clamped HERE rather than at the call sites: this is the one funnel
        # every transcription passes through, so nothing downstream can
        # reintroduce the 400 by building a prompt of its own.
        "prompt": clamp_stt_prompt(prompt)
    }
    if language: params["language"] = language

    # Whisper is billed against the same per-account budget as the reply model,
    # so speech-to-text gets the same pool. It is also the call the student is
    # most obviously waiting on -- a rate-limited transcription is a question
    # that never gets heard at all.
    clients = groq_key_order()
    for attempt in range(attempts):
        try:
            client = clients[attempt % len(clients)]
            result = client.audio.transcriptions.create(**params)
            return (result.text or "").strip(), (getattr(result, "language", "") or "")
        except Exception as exc:
            # A rejected REQUEST will be rejected identically next time -- only
            # transport failures are worth replaying. Retrying a 400 turned one
            # wasted call into two and put the same error on screen twice.
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None)
            if attempt == attempts - 1 or (status is not None and 400 <= status < 500
                                           and status != 429):
                raise
            print(f"[STT] Attempt {attempt + 1} failed ({exc}); retrying...", flush=True)
            time.sleep(0.4)

def audio_seconds(audio):
    return len(audio.frame_data) / float(audio.sample_rate * audio.sample_width)

def audio_rms(audio):
    """Average loudness of a captured clip, on the same scale as energy_threshold.

    speech_recognition measures its threshold with audioop.rms over the raw
    frames, so this is directly comparable to MIC_ENERGY_FLOOR -- which is the
    whole point: it lets a clip be judged against the bar that was supposed to
    have opened it."""
    try:
        return audioop.rms(audio.frame_data, audio.sample_width)
    except Exception:
        # Never let a metering failure swallow real speech; err towards sending.
        return MIN_SPEECH_RMS

# Set once the API has rejected one of the tuning parameters below, so the
# fallback is paid for at most once per run instead of on every single turn.
_chat_tuning_supported = True

RE_RETRY_AFTER = re.compile(r"try again in ([0-9.]+)s")

def start_chat_stream(messages, attempts=3):
    """Open the streaming completion for a reply. One funnel, so the model, its
    tuning and its rate-limit handling live in exactly one place.

    reasoning_effort: gpt-oss-120b is a reasoning model, so before it emits any
    of ANSWER: it writes itself a private chain of thought, and nothing
    downstream can start until it does -- no sentence, so no Cartesia request,
    so no audio. "low" is the honest setting for what this device does: spoken
    answers of one to four sentences, to questions a knowledgeable person
    answers without stopping to think.

    Measured on this account, low is NOT a large first-token win: against a
    short prompt, low/medium/high came out at 0.54/0.47/0.44s average, which is
    inside the noise. Keep it anyway, for the reason below -- reasoning tokens
    are billed and rate-limited output tokens, and this device has very few to
    spend. Do not expect it to make her feel faster on its own.

    LLM_TUNING is where thinking is turned off, and the measurements behind
    that are with it at the top of this file. Note that Groq's reasoning_format
    has no place here: the OpenAI SDK rejects the keyword outright, raising
    before the request is even sent, which is what the TypeError branch below
    exists to survive.

    RATE LIMIT HANDLING: with_options(max_retries=0) because THIS function owns
    retrying. Left at the SDK default it retries 429s twice inside the call with
    exponential backoff on an exhausted key/rate limit, taking 26-30 seconds
    before the next attempt. Turning the SDK's retries off makes this loop the
    only one, and its total wait bounded at roughly 7.5s -- long, but inside the
    25s deadline in ai_loop and honest about failing after it.
    """
    global _chat_tuning_supported
    kwargs = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": True,
        # Devanagari costs roughly 3x the tokens of the same English, so a
        # cap tuned for English truncates Hindi mid-word. Brevity is enforced
        # by the prompt instead; this is only a runaway guard.
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": 0.7,
    }

    def fire():
        c = openrouter_client.with_options(max_retries=0)
        if _chat_tuning_supported:
            return c.chat.completions.create(**LLM_TUNING, **kwargs)
        return c.chat.completions.create(**kwargs)

    for attempt in range(attempts):
        limited = None
        try:
            return fire()
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None)

            # The tuning parameters are unsupported -- either the model is not a
            # reasoning model (4xx from the API) or the SDK refused the kwarg
            # before sending (TypeError, which carries no status at all). Drop
            # them and go again, rather than failing every turn for the rest of
            # the run.
            if _chat_tuning_supported and (
                    isinstance(exc, TypeError)
                    or (status is not None and 400 <= status < 500
                        and status != 429)):
                _chat_tuning_supported = False
                print(f"[LLM] {LLM_MODEL} rejected the latency settings "
                      f"({exc}); continuing without them.", flush=True)
                return fire()

            # A rate limit or transport failure (timeout, dropped connection).
            if status == 429 or status is None:
                limited = exc
                if attempt < attempts - 1:
                    continue
                raise limited

            raise

        # Extract retry-after if the API provided one.
        hit = RE_RETRY_AFTER.search(str(limited))
        rate_limited = "429" in str(limited) or "rate_limit" in str(limited).lower()
        wait = min(float(hit.group(1)) + 0.15 if hit else 1.0,
                   LLM_RETRY_MAX_WAIT_S) if rate_limited else 0.4
        print(f"[LLM] Request failed "
              f"({'rate limited' if rate_limited else 'transport'}); waiting "
              f"{wait:.1f}s (attempt {attempt + 1} of {attempts}).", flush=True)
        time.sleep(wait)

def is_probably_speech(audio, where, endpointed=False):
    """False for the clips that are not worth a Whisper call.

    Both gates are here rather than at each call site so the wake path and the
    conversation path cannot drift apart on what counts as speech.

    `endpointed` means a VAD chose the start and end of this clip. That changes
    what is left to check: the clip has already been judged on the SHAPE of the
    signal, which is a far better test than loudness and the whole reason a
    student can now be heard from across the room. Re-applying the loudness bar
    on top of it would undo exactly that -- the quiet distant clips this was
    built to rescue are the first ones it throws away. So the bar drops to a
    check that the capture device is delivering audio at all."""
    seconds = audio_seconds(audio)
    if seconds < MIN_SPEECH_SEC:
        return False
    level = audio_rms(audio)
    floor = VAD_MIN_RMS if endpointed else MIN_SPEECH_RMS
    if level < floor:
        print(f"[{where}] Dropped {seconds:.1f}s at RMS {level} "
              f"(below {floor}); {'silence' if endpointed else 'room noise'}, "
              f"not speech.", flush=True)
        return False
    return True

def is_repeated_hallucination(text, threshold=3):
    """True when the transcript is one short phrase looped.

    Whisper repeats itself when handed audio that is not speech, and the phrase
    it repeats is whatever it was primed with -- so over a song it produces the
    wake word, several times over, and every copy matches. Observed stopping a
    track seconds after it started: "हे लीज़ा। हे लीज़ा। हे लीज़ा।".

    Counts distinct chunks rather than words so it fires on the repeat and not
    on a person who happens to say "no no no"."""
    chunks = [c.strip() for c in re.split(r'[।.!?]+', text or "") if c.strip()]
    if len(chunks) >= threshold and len(set(chunks)) == 1:
        return True
    words = (text or "").split()
    if len(words) >= threshold and len(set(words)) == 1:
        return True
    return False

def listen_for_wake_word(recognizer, mic_device, asleep=False, listener=None,
                         pattern=None, seed=None, timeout=None, phrase_limit=None):
    """True when 'Hey Liza' is heard. recognizer.listen blocks on silence, so audio is
    only sent to Whisper when somebody actually speaks near the device.

    `mic_device` is a HeldMicrophone: the stream stays open between calls rather
    than being re-opened per listen, which is what stops this dongle wedging --
    see the class for the measurements.

    `asleep` is set after the Sleep button and requires the full greeting; see
    RE_WAKE_WORD_ASLEEP."""
    endpointed = listener is not None and listener.available
    if endpointed:
        # The pre-roll matters more here than anywhere else: "Hey" is the
        # shortest, quietest part of the whole phrase and it is what has to
        # survive for the name to be matched at all.
        audio = listener.wait_for_utterance(WAKE_LISTEN_TIMEOUT_S,
                                            WAKE_PHRASE_LIMIT_S,
                                            PAUSE_THRESHOLD_NORMAL)
        if audio is None:
            return False, "", ""
    else:
        try:
            # timeout only bounds how long it waits for speech to BEGIN -- it still
            # returns the instant somebody talks -- so a long one costs no
            # responsiveness and keeps the number of round trips down.
            with mic_device as source:
                audio = recognizer.listen(
                    source,
                    timeout=WAKE_LISTEN_TIMEOUT_S if timeout is None else timeout,
                    phrase_time_limit=(WAKE_PHRASE_LIMIT_S if phrase_limit is None
                                       else phrase_limit))
        except sr.WaitTimeoutError:
            clamp_energy(recognizer)
            return False, "", ""
        except Exception as exc:
            print(f"[WAKE ERROR] {exc}", flush=True)
            time.sleep(0.5)
            return False, "", ""

        clamp_energy(recognizer)
    # Too short or too quiet to be "Hey Liza". Whisper does not return nothing
    # for a door closing, it returns its best guess at words, so every one of
    # these clips was a paid API call whose only possible outcomes were a false
    # wake or a log line. Discarding them here is both cheaper and more
    # accurate: the false wakes seen out of an empty room ("चाहे लापने कि
    # लिज़ा।" matching the fuzzy Devanagari branch) were all room tone.
    if not is_probably_speech(audio, "WAKE", endpointed):
        return False, "", ""

    try:
        wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
        text, language = transcribe(wav_data,
                                    WAKE_SEED_PROMPT if seed is None else seed,
                                    model=WAKE_STT_MODEL)
        if is_repeated_hallucination(text):
            # "हे लीज़ा। हे लीज़ा। हे लीज़ा।" -- nobody says the wake word three
            # times in one breath. Whisper looping a short phrase is one of its
            # best-known tells on audio that is not speech, and over a track it
            # loops the one phrase the seed prompt taught it.
            print(f"[WAKE] Ignored (looped phrase, not speech): {text!r}", flush=True)
            return False, "", ""
        if pattern is None:
            pattern = RE_WAKE_WORD_ASLEEP if asleep else RE_WAKE_WORD
        match = pattern.search(text) if text else None
        if match:
            print(f"[WAKE] Heard{' (from sleep)' if asleep else ''}: {text}", flush=True)
            # "Hey Liza, what is photosynthesis?" said in one breath: keep the question
            # instead of making the student repeat it.
            question = re.sub(r'\s+', ' ', text[:match.start()] + " " + text[match.end():])
            question = question.strip(" ,.!?।-")
            if len(question.split()) < 2:
                question, language = "", ""
            return True, question, language
        if text:
            # Logged because a near-miss is otherwise invisible: the device just
            # sits there looking idle. Whisper's spelling of the name drifts, so
            # this is the line that shows a new variant needs adding above.
            print(f"[WAKE] Ignored (no wake word): {text!r}", flush=True)
    except Exception as exc:
        print(f"[WAKE ERROR] {exc}", flush=True)
    return False, "", ""

MEDIA_COMMAND_TIMEOUT_S = float(os.getenv("MEDIA_COMMAND_TIMEOUT_S", "5.0"))

def listen_for_media_command(recognizer, mic_device, listener):
    """What the student said after waking her over a track. ("", "") if nothing.

    Nothing said is the signal to RESUME, and that is the whole point of pausing
    rather than stopping. A wake word heard over a track is very often the track
    itself, so the cost of being wrong has to be a moment's silence rather than
    the song. Stopping outright made every hallucination unrecoverable."""
    if listener is not None and listener.available:
        audio = listener.wait_for_utterance(MEDIA_COMMAND_TIMEOUT_S,
                                            WAKE_PHRASE_LIMIT_S,
                                            PAUSE_THRESHOLD_NORMAL)
        if audio is None:
            return "", ""
        endpointed = True
    else:
        try:
            with mic_device as source:
                audio = recognizer.listen(source, timeout=MEDIA_COMMAND_TIMEOUT_S,
                                          phrase_time_limit=WAKE_PHRASE_LIMIT_S)
        except Exception:
            return "", ""
        endpointed = False
    if not is_probably_speech(audio, "MEDIA", endpointed):
        return "", ""
    try:
        wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
        text, language = transcribe(wav_data, STT_SEED_PROMPT)
        text = (text or "").strip()
        if is_repeated_hallucination(text):
            print(f"[MEDIA] Ignored (looped phrase): {text!r}", flush=True)
            return "", ""
        return text, language
    except Exception as exc:
        print(f"[MEDIA ERROR] {exc}", flush=True)
        return "", ""

def fetch_weather():
    """Current conditions from OpenWeatherMap, or None if it is not configured."""
    if not WEATHER_API_KEY:
        return None

    response = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={"q": WEATHER_CITY, "appid": WEATHER_API_KEY, "units": "metric"},
        timeout=10
    )
    response.raise_for_status()
    data = response.json()
    return {
        "temp": round(data["main"]["temp"]),
        "feels": round(data["main"]["feels_like"]),
        "high": round(data["main"]["temp_max"]),
        "low": round(data["main"]["temp_min"]),
        "humidity": data["main"]["humidity"],
        "desc": data["weather"][0]["main"],
        "icon": data["weather"][0]["icon"],
        "city": data["name"]
    }

def weather_worker():
    while True:
        try:
            reading = fetch_weather()
            if reading:
                print(f"[WEATHER] {reading['city']} {reading['temp']}C {reading['desc']}", flush=True)
                ui_call(lambda r=reading: ui_instance.set_weather(r))
        except Exception as exc:
            print(f"[WEATHER ERROR] {exc}", flush=True)
        time.sleep(WEATHER_REFRESH_S)

def cartesia_voice_id(language):
    voice_id = VOICE_IDS.get(language) or CARTESIA_VOICE_ID
    if not voice_id:
        raise RuntimeError("No Cartesia voice configured. Set CARTESIA_VOICE_ID in .env "
                           "(run `python assist.py --list-voices` to pick one).")
    return voice_id

def list_cartesia_voices(query=""):
    """Print the voices this API key can use, so you can copy an ID into .env."""
    page = cartesia_client.voices.list(limit=100, q=query) if query else cartesia_client.voices.list(limit=100)
    for voice in page:
        print(f"{voice.id}  [{voice.language}]  {voice.name}", flush=True)

def ui_call(callback):
    """Run a UI update on the Tk thread. No-op when headless."""
    if ui_instance is None: return
    root = getattr(ui_instance, "root", None)
    if root is not None: root.after(0, callback)

# ==========================================
# Bulletproof Audio + Word-Timestamped Subtitles
# ==========================================
def audio_player_worker():
    global active_subprocesses, playback_started_at
    while True:
        first_item = audio_queue.get()
        if first_item is None: break
        if first_item == "[END_OF_RESPONSE]":
            audio_queue.task_done()
            continue
        if stop_playback_event.is_set():
            audio_queue.task_done()
            continue

        playback_started_at = time.time()
        playback_active.set()
        sentence_queue = queue.Queue()
        sentence_queue.put(first_item)

        try:
            aplay_proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(CARTESIA_SAMPLE_RATE),
                 "-c", "1", "-D", AUDIO_OUTPUT_DEVICE],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
            active_subprocesses = [aplay_proc]

            # "generated" is how many seconds of audio have been handed to aplay so far,
            # so it doubles as the offset of the next sentence on the playback timeline.
            clock = {"start": 0.0, "generated": 0.0}
            generation_done = threading.Event()
            cancelled = threading.Event()

            def speak_sentence(sentence):
                # Last resort: neither voice can read Urdu or other Indic scripts, and
                # sending it anyway produces noise rather than speech.
                if RE_UNREADABLE_SCRIPT.search(sentence):
                    print(f"[TTS] Skipping unreadable script: {sentence}", flush=True)
                    return

                language = detect_tts_language(sentence)

                # `with` so a barge-in releases the HTTP connection instead of leaking it.
                with cartesia_client.tts.generate_sse(
                    model_id=CARTESIA_MODEL,
                    transcript=sentence,
                    voice={"mode": "id", "id": cartesia_voice_id(language)},
                    language=language,
                    output_format={"container": "raw", "encoding": "pcm_s16le", "sample_rate": CARTESIA_SAMPLE_RATE},
                    speed=CARTESIA_SPEED,
                ) as stream:
                    for event in stream:
                        if stop_playback_event.is_set() or cancelled.is_set(): break
                        event_type = getattr(event, "type", "")

                        if event_type == "chunk":
                            chunk = event.audio
                            if not chunk: continue
                            if not clock["start"]:
                                clock["start"] = time.time()
                                ui_call(lambda: ui_instance.set_state("speaking"))

                            try:
                                aplay_proc.stdin.write(chunk)
                                aplay_proc.stdin.flush()
                            except (BrokenPipeError, ValueError, OSError):
                                # aplay was killed by a barge-in: abandon the rest of this response.
                                cancelled.set()
                                break
                            clock["generated"] += len(chunk) / BYTES_PER_SEC

                        elif event_type == "error":
                            print(f"TTS Error: {getattr(event, 'error', event)}", flush=True)

            def generate_audio():
                try:
                    while True:
                        sentence = sentence_queue.get()
                        if sentence is None: break
                        if stop_playback_event.is_set() or cancelled.is_set(): break

                        print(f"Liza (speaking): {sentence}", flush=True)
                        # Recorded here rather than at each call site so mode
                        # intros and media confirmations are covered too, not
                        # just LLM answers. The transcript panel is flipped over
                        # to her side from the same spot and for the same
                        # reason: this is the one point every spoken line passes
                        # through, so nothing she says can miss the screen.
                        note_spoken(sentence)
                        ui_call(lambda s=sentence: ui_instance.set_transcript(s, "liza"))
                        try:
                            speak_sentence(sentence)
                        except Exception as exc:
                            if not (stop_playback_event.is_set() or cancelled.is_set()):
                                print(f"TTS Error: {exc}", flush=True)
                finally:
                    generation_done.set()
                    try: aplay_proc.stdin.close()
                    except Exception: pass

            generator_thread = threading.Thread(target=generate_audio, daemon=True)
            generator_thread.start()
            audio_queue.task_done()

            while True:
                sentence = audio_queue.get()
                if sentence is None: break
                if sentence == "[END_OF_RESPONSE]":
                    audio_queue.task_done()
                    break
                if stop_playback_event.is_set():
                    audio_queue.task_done()
                    break

                sentence_queue.put(sentence)
                audio_queue.task_done()

            sentence_queue.put(None)
            generator_thread.join()
            aplay_proc.wait()

        except Exception as e:
            print(f"TTS Error: {e}", flush=True)
        finally:
            ui_call(lambda: ui_instance.set_state("idle"))
            active_subprocesses.clear()
            # Re-stamped at the true end of playback, so the echo window is
            # measured from when sound actually stopped.
            global last_spoken_at
            last_spoken_at = time.time()
            playback_active.clear()

# ==========================================
# Full-Screen UI
# ==========================================
UI_W, UI_H = 800, 480
FRAME_MS = 60

COL_BG        = "#F3F5FD"
COL_FRAME     = "#E1E5F4"
COL_CARD      = "#FFFFFF"
COL_CARD_EDGE = "#EAEDF8"
COL_SHADOW    = "#B4BCDD"
COL_TEXT      = "#1E2233"
COL_TEXT_DIM  = "#8891A8"
COL_TEXT_FAINT= "#B6BDD0"
COL_TRACK     = "#E6E9F5"
COL_INDIGO    = "#6366F1"
COL_STOP      = "#F43F5E"

MODE_ACCENTS = {"TUTOR": "#7C3AED", "CO-TELL": "#14B8A6", "RE-TELL": "#F59E0B"}
MODE_TINTS   = {"TUTOR": "#F3EEFF", "CO-TELL": "#E6FAF6", "RE-TELL": "#FFF4E6"}
MODE_BLURBS = {
    "TUTOR": "Get help with studies, concepts and explanations.",
    "CO-TELL": "Let's talk it through and share ideas together.",
    "RE-TELL": "You explain what you learned, I'll help."
}
MODE_INTROS = {
    "TUTOR": "You are in tutor mode. Ask me anything from your studies.",
    "CO-TELL": "You are in co-tell mode. Name a topic and we will work through it together.",
    "RE-TELL": "You are in re-tell mode. Teach me what you have learned. "
               "I will listen without interrupting, and when you finish I will tell you how you did."
}

# The three pill buttons along the bottom: label, sub-label, gradient, icon.
ACTIONS = [
    ("SPEAK", "Tap to speak",      "#5AA7FF", "#3D6FE8", "#E7F0FF", "mic"),
    ("STOP",  "Tap to stop listen", "#FF5E7D", "#F0355F", "#FFE3EA", "stop"),
    ("SLEEP", "Put me to sleep",   "#9061F9", "#6D3FE0", "#EDE4FF", "moon"),
]

# label, colour, waveform activity (0..1)
STATE_STYLE = {
    "warmup":    ("Waking up",         "#7C6AE0", 0.10),
    "idle":      ("Tap to speak",      "#4C6FFF", 0.08),
    "sleeping":  ("Sleeping",          "#8B5CF6", 0.03),
    "listening": ("Listening",         "#14B8A6", 1.00),
    "thinking":  ("Thinking",          "#F59E0B", 0.35),
    "speaking":  ("Speaking",          "#EC4899", 0.80),
    "capturing": ("Looking",           "#22C55E", 0.25),
    "error":     ("Something's wrong", "#F43F5E", 0.12),
}

# How Liza is feeling, as she reports it herself on the EMOTION: line. The chip
# under her is the only place this shows on screen, so an unknown word from the
# model falls back to neutral rather than blanking the chip or crashing.
EMOTION_STYLE = {
    "happy": "#F59E0B", "excited": "#EC4899", "proud": "#7C3AED",
    "curious": "#0EA5E9", "encouraging": "#14B8A6", "thoughtful": "#6366F1",
    "calm": "#10B981", "concerned": "#F97316", "sorry": "#94A3B8",
    "playful": "#D946EF", "neutral": "#8891A8",
}

# THE SAME WORD, BUT WITH THE "EMOTION:" LABEL MISSING.
#
# The contract asks for "EMOTION: curious" on its own line and the strip above
# is anchored to that label. Gemini honours it most of the time and then
# intermittently does not, emitting the bare word and the answer under it --
# observed on this device, spoken aloud, "Curious" and then the reply, and
# "Thoughtful" and then the reply. With no label there was nothing for the
# EMOTION: strip to match, so the mood word went to the speaker as if it were
# the first word of the answer.
#
# ANCHORED TO A LINE OF ITS OWN, and that is the whole safety of it: "sorry" and
# "concerned" are ordinary words, and "Sorry, I can't check the volume" is a
# real answer that has to survive intact. A word alone on the first line is a
# label; the same word followed by anything else on that line is speech.
RE_BARE_EMOTION_LINE = re.compile(
    r'^[ \t]*(?:' + '|'.join(EMOTION_STYLE) + r')[ \t]*[.!]?[ \t]*\r?\n',
    re.IGNORECASE)

# And the third shape it takes: the word with a COLON, inline, running straight
# into the answer -- "Curious: That's a great topic to jump into.", "Concerned:
# You correctly identified...". Seen all through the co-tell and re-tell logs,
# spoken aloud every time, because the line above needs a newline after the word
# and there is none here.
#
# The colon is what makes this safe to strip where a bare word inline would not
# be. "Sorry, I can't" is speech; "Sorry:" at the very start of a reply is a
# label, because nobody says a colon out loud.
RE_INLINE_EMOTION_TAG = re.compile(
    r'^[ \t]*(?:' + '|'.join(EMOTION_STYLE) + r')[ \t]*:[ \t]*',
    re.IGNORECASE)

# Which cached mascot animation plays for each app state.
MASCOT_FOR_STATE = {
    "warmup": "idle", "idle": "idle", "capturing": "idle", "error": "idle",
    "sleeping": "idle",
    "listening": "listening", "thinking": "thinking", "speaking": "speaking",
}

# Nothing on screen is Hindi today, but prefer a family that covers Devanagari so
# any Hindi text added later is readable. Install one with:
#   sudo apt install fonts-noto-devanagari
FONT_PREFERENCE = ("Noto Sans", "Noto Sans Devanagari", "Lohit Devanagari",
                   "Mukta", "Samyak Devanagari", "FreeSans", "DejaVu Sans", "Helvetica")

# ---------- layout ----------
LCOL_X0, LCOL_X1 = 12, 202
CLOCK_Y0, CLOCK_Y1 = 30, 186
MUSIC_Y0, MUSIC_Y1 = 194, 392

RCOL_X0, RCOL_X1 = 596, 788
MODES_Y0, MODES_Y1 = 30, 252
MODE_CARD_X0, MODE_CARD_X1 = 602, 782
MODE_CARD_Y0, MODE_CARD_H, MODE_CARD_GAP = 60, 58, 6
TRANS_Y0, TRANS_Y1 = 258, 392

# Measured against the wallpaper, not chosen by eye: the sky/grass boundary
# under her sits at y=382 (found by scanning down the column at MASCOT_CX for
# the first sustained run of green -- clouds, stars and the rainbow all produce
# short green stretches higher up and will fool a simpler test). The cached
# frames have zero transparent padding below the feet, so the bottom edge of the
# image IS the feet, and centring a 300px frame at 234 lands them at 384 --
# two pixels into the grass, which reads as standing on it rather than as a
# seam. At the old 212 they finished at 362, floating 20px above the horizon.
MASCOT_CX, MASCOT_CY = 399, 234
DOTS_Y = 18
STATE_LABEL_Y = 44

BTN_Y0, BTN_H, BTN_W, BTN_GAP = 400, 64, 252, 12
BTN_XS = [10, 274, 538]

# ---------- 3D mascot animation ----------
# The source clips are 1920x1080 RGBA at ~200 frames each; decoding all four
# in full would need several GB of RAM. On first run each one is cropped to
# the character, downsized, and cached to disk as small PNG frames, and only
# that small cache is ever loaded into memory.
MASCOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Animations")
MASCOT_CACHE_DIR = os.path.join(MASCOT_DIR, "cache")
MASCOT_SOURCES = {"idle": "Idle.gif", "listening": "Listen.gif",
                   "thinking": "Think.gif", "speaking": "Talking.gif"}
# Union bounding box of the character across all 4 clips, measured from the
# source frames rather than guessed: the artwork sits in a 1600x960 canvas and
# is nowhere near filling it, so cropping to the character is what stops her
# being rendered as a thumbnail with 60% empty space around her. Re-measure if
# the clips are ever replaced -- the old value here was for 1920x1080 sources
# and is meaningless against these.
MASCOT_CROP = (534, 40, 1145, 855)
MASCOT_H = 300
MASCOT_W = round(MASCOT_H * (MASCOT_CROP[2] - MASCOT_CROP[0]) / (MASCOT_CROP[3] - MASCOT_CROP[1]))
MASCOT_STEP = 2  # keep every 2nd frame: still smooth, halves memory and disk

# 3D-only mode: the same frames, upscaled to most of the 480px panel. Built
# lazily, one animation at a time, and only if the student ever asks for the
# mode -- all four buckets at this size is ~220MB of PhotoImage, which is not
# worth holding for a mode that may never be used.
MASCOT_3D_H = 440
MASCOT_3D_W = round(MASCOT_3D_H * (MASCOT_CROP[2] - MASCOT_CROP[0]) / (MASCOT_CROP[3] - MASCOT_CROP[1]))
MASCOT_3D_CY = 228

UI_BG_IMAGE = os.path.join(MASCOT_DIR, "AI Background.png")

def _background_image(w, h):
    """The wallpaper, scaled to COVER w*h and centre-cropped.

    Cover rather than fit: the art is 16:9 and this panel is 5:3, so stretching
    it to the exact shape squashes everything in it by about 7%, which is very
    visible on the round things (clouds, flowers, the rainbow). Cropping a
    little off the sides costs nothing because the subject is central."""
    image = Image.open(UI_BG_IMAGE).convert("RGB")
    scale = max(w / image.width, h / image.height)
    image = image.resize((max(w, round(image.width * scale)),
                          max(h, round(image.height * scale))), Image.LANCZOS)
    left, top = (image.width - w) // 2, (image.height - h) // 2
    return image.crop((left, top, left + w, top + h))

def _build_mascot_cache(state, fname):
    out_dir = os.path.join(MASCOT_CACHE_DIR, state)
    os.makedirs(out_dir, exist_ok=True)
    src = Image.open(os.path.join(MASCOT_DIR, fname))
    kept = 0
    for i in range(0, src.n_frames, MASCOT_STEP):
        src.seek(i)
        src.convert("RGBA").crop(MASCOT_CROP).resize((MASCOT_W, MASCOT_H), Image.LANCZOS) \
           .save(os.path.join(out_dir, f"f{kept:04d}.png"))
        kept += 1
    print(f"[MASCOT] Cached {kept} frames for '{state}'.", flush=True)

def _load_mascot_frames():
    """{state: [ImageTk.PhotoImage, ...]}, building the on-disk cache first if needed."""
    frames = {}
    for state, fname in MASCOT_SOURCES.items():
        out_dir = os.path.join(MASCOT_CACHE_DIR, state)
        if not (os.path.isdir(out_dir) and os.listdir(out_dir)):
            src_path = os.path.join(MASCOT_DIR, fname)
            if not os.path.exists(src_path):
                print(f"[MASCOT WARN] {fname} not found; '{state}' animation will be blank.", flush=True)
                frames[state] = []
                continue
            print(f"[MASCOT] Preparing '{state}' animation (first run only)...", flush=True)
            _build_mascot_cache(state, fname)
        paths = sorted(glob.glob(os.path.join(out_dir, "*.png")))
        frames[state] = [ImageTk.PhotoImage(Image.open(p)) for p in paths]
    return frames

# ---------- PIL-drawn chrome ----------
# Tk's canvas has neither alpha nor blur, and in this design the cards and
# buttons are defined by their soft shadow and gradient more than by any
# outline, so those two pieces are rendered in PIL and placed as images.
SHADOW_PAD = 10

def _rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))

def _mix(colour, target, t):
    """Blend two #rrggbb colours; Tk canvas has no alpha so glows are faked this way."""
    t = max(0.0, min(1.0, t))
    a, b = _rgb(colour), _rgb(target)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _drop_shadow(size, box, radius, alpha, blur, offset):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (box[0], box[1] + offset, box[2], box[3] + offset), radius,
        fill=_rgb(COL_SHADOW) + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(blur))

def _card_image(w, h, radius, fill=COL_CARD, edge=COL_CARD_EDGE, alpha=60, blur=5, offset=3):
    """A rounded card with a soft drop shadow. Anchor the result at NW,
    offset by -SHADOW_PAD, so the card itself lands on its nominal box."""
    p = SHADOW_PAD
    size = (w + p * 2, h + p * 2)
    box = (p, p, p + w, p + h)
    img = _drop_shadow(size, box, radius, alpha, blur, offset)
    ImageDraw.Draw(img).rounded_rectangle(
        box, radius, fill=_rgb(fill) + (255,),
        outline=(_rgb(edge) + (255,)) if edge else None, width=1)
    return img

def _gradient(w, h, c0, c1):
    """Left-to-right gradient, built one pixel row wide and stretched."""
    a, b = _rgb(c0), _rgb(c1)
    row = Image.new("RGB", (w, 1))
    px = row.load()
    for x in range(w):
        t = x / max(1, w - 1)
        px[x, 0] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return row.resize((w, h), Image.BILINEAR)

def _draw_action_icon(draw, kind, cx, cy):
    white = (255, 255, 255, 255)
    if kind == "mic":
        draw.rounded_rectangle((cx - 4, cy - 10, cx + 4, cy + 1), 4, fill=white)
        draw.arc((cx - 8, cy - 6, cx + 8, cy + 6), 0, 180, fill=white, width=2)
        draw.line((cx, cy + 6, cx, cy + 10), fill=white, width=2)
        draw.line((cx - 4, cy + 10, cx + 4, cy + 10), fill=white, width=2)
    elif kind == "stop":
        draw.rounded_rectangle((cx - 6, cy - 6, cx + 6, cy + 6), 2, fill=white)

def _action_image(w, h, radius, c0, c1, icon):
    """One of the bottom pill buttons: gradient body, soft shadow, ringed icon."""
    p = SHADOW_PAD
    size = (w + p * 2, h + p * 2)
    box = (p, p, p + w, p + h)
    img = _drop_shadow(size, box, radius, 90, 6, 4)

    body = Image.new("RGBA", size, (0, 0, 0, 0))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    body.paste(_gradient(w, h, c0, c1).convert("RGBA"), (p, p), mask.crop((p, p, p + w, p + h)))
    img = Image.alpha_composite(img, body)

    cx, cy = p + 40, p + h // 2
    draw = ImageDraw.Draw(img)
    draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), outline=(255, 255, 255, 150), width=2)
    if icon == "moon":
        # Carved rather than drawn: a crescent is a circle minus an offset
        # circle, and ImageDraw replaces alpha instead of compositing it, so
        # the bite has to be taken out on a scratch layer of its own.
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        cut = ImageDraw.Draw(layer)
        cut.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(255, 255, 255, 255))
        cut.ellipse((cx - 4, cy - 12, cx + 14, cy + 6), fill=(0, 0, 0, 0))
        img = Image.alpha_composite(img, layer)
    else:
        _draw_action_icon(draw, icon, cx, cy)
    return img

def _album_art_image(size, radius=10):
    """Placeholder cover art: nothing upstream gives us a real thumbnail."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    img.paste(_gradient(size, size, "#A78BFA", "#F0A6D0").convert("RGBA"), (0, 0), mask)

    draw = ImageDraw.Draw(img)
    cx, cy, white = size * 0.42, size * 0.62, (255, 255, 255, 235)
    draw.ellipse((cx - 7, cy - 5, cx + 3, cy + 5), fill=white)
    draw.line((cx + 3, cy, cx + 3, cy - 16), fill=white, width=2)
    draw.line((cx + 3, cy - 16, cx + 13, cy - 13), fill=white, width=3)
    return img

def _fmt_clock(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"

class TutorUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Liza")
        self.root.geometry(f"{UI_W}x{UI_H}")
        self.root.attributes('-fullscreen', True)
        self.root.configure(bg=COL_BG)

        self.modes = ["TUTOR", "CO-TELL", "RE-TELL"]
        self.mode_colors = MODE_ACCENTS
        self.current_mode_index = 0
        self.current_mode = self.modes[0]
        self.current_state = "warmup"
        self.transcript = ""
        self.speaker = "user"
        self.asleep = False

        self.phase = 0.0
        self.frame = 0
        self.mascot_index = 0
        self._mascot_bucket = None
        self._state_caption = None
        self._photos = []       # Tk keeps no reference of its own to images
        self._font_cache = {}
        self.media = {"title": None, "artist": "", "pos": 0.0, "dur": 0.0, "paused": False}
        # Agentic UI state: "normal" (every card) or "3d" (mascot and mood only).
        self.ui_mode = "normal"
        self._hidden_items = []     # exactly what 3D mode hid, to put back
        self.emotion = None
        self.mascot_frames_big = {} # bucket -> upscaled frames, built on demand
        self._big_pending = set()

        self.font_family = self._pick_font()
        self.canvas = tk.Canvas(root, width=UI_W, height=UI_H, bd=0,
                                highlightthickness=0, bg=COL_BG)
        self.canvas.place(x=0, y=0)

        print("[MASCOT] Loading character animations...", flush=True)
        self.mascot_frames = _load_mascot_frames()

        self._build_frame()
        self._build_clock_card()
        self._build_music_card()
        self._build_mascot()
        self._build_mode_cards()
        self._build_transcript_panel()
        self._build_buttons()
        self._refresh_cards()
        self.set_now_playing(None)

        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.bind("<Button-1>", self.tap_to_wake)

        self._animate()
        self._tick_clock()

    # ---------- drawing helpers ----------
    def _pick_font(self):
        """First installed family that can also draw Devanagari, so Hindi is readable."""
        try:
            from tkinter import font as tkfont
            available = {name.lower() for name in tkfont.families(self.root)}
        except Exception:
            return "Helvetica"

        for name in FONT_PREFERENCE:
            if name.lower() in available:
                return name
        return "Helvetica"

    def _font(self, size, bold=False):
        """Real Font objects rather than tuples, so widths can be measured."""
        key = (size, bold)
        if key not in self._font_cache:
            from tkinter import font as tkfont
            self._font_cache[key] = tkfont.Font(
                family=self.font_family, size=size,
                weight="bold" if bold else "normal")
        return self._font_cache[key]

    def _ellipsize(self, text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _place_photo(self, image, x, y, tags=None):
        """Photos are anchored NW and pulled back by the shadow padding, so
        callers can pass the card's own top-left corner."""
        photo = ImageTk.PhotoImage(image)
        self._photos.append(photo)
        kw = {"tags": tags} if tags else {}
        return self.canvas.create_image(x - SHADOW_PAD, y - SHADOW_PAD,
                                        image=photo, anchor="nw", **kw)

    def _card(self, x0, y0, x1, y1, r=16, fill=COL_CARD, edge=COL_CARD_EDGE, tags=None):
        return self._place_photo(
            _card_image(int(x1 - x0), int(y1 - y0), r, fill, edge), x0, y0, tags)

    def _bars_glyph(self, cx, cy, colour, heights=(5, 9, 13, 9, 5)):
        """The little waveform mark that heads the music and transcribe cards."""
        items = []
        x = cx - (len(heights) * 3 - 1) / 2
        for h in heights:
            items.append(self.canvas.create_line(x, cy - h / 2, x, cy + h / 2,
                                                 fill=colour, width=2, capstyle="round"))
            x += 3
        return items

    # ---------- static chrome ----------
    def _build_frame(self):
        # First item on the canvas, so every card, the mascot and all the text
        # stack above it. Tk has no z-index worth the name -- items are drawn in
        # creation order -- so "first" is the whole mechanism here.
        self.bg_photo = None
        self.bg_item = None
        try:
            self.bg_photo = ImageTk.PhotoImage(_background_image(UI_W, UI_H))
            self.bg_item = self.canvas.create_image(0, 0, anchor="nw",
                                                    image=self.bg_photo)
        except Exception as exc:
            print(f"[UI] Background image unavailable ({exc}); "
                  f"falling back to the flat colour.", flush=True)

        self._round_rect(4, 4, UI_W - 4, UI_H - 4, 18, fill="", outline=COL_FRAME, width=1)

        # Decorative 3x3 launcher dots, matching the mockup's top-left mark.
        for r in range(3):
            for c in range(3):
                x, y = 18 + c * 7, 16 + r * 7
                self.canvas.create_oval(x, y, x + 3, y + 3, fill=COL_INDIGO, outline="")

        self.head_dots = []
        for i, colour in enumerate(("#7C5CFF", "#3B82F6", "#22D3EE")):
            x = MASCOT_CX - 16 + i * 16
            self.head_dots.append(self.canvas.create_oval(
                x - 4, DOTS_Y - 4, x + 4, DOTS_Y + 4, fill=colour, outline=""))

        self._build_confetti()

    def _build_confetti(self):
        """The scattered plus/star/ring marks floating around the character."""
        for x, y in ((252, 152), (256, 300), (292, 206), (500, 210), (486, 352)):
            self.canvas.create_line(x - 5, y, x + 5, y, fill="#9AB4F5", width=2)
            self.canvas.create_line(x, y - 5, x, y + 5, fill="#9AB4F5", width=2)

        for x, y, colour in ((498, 128, "#A5B4FC"), (505, 268, "#5EEAD4")):
            self.canvas.create_polygon(
                x, y - 7, x + 2, y - 2, x + 7, y, x + 2, y + 2,
                x, y + 7, x - 2, y + 2, x - 7, y, x - 2, y - 2,
                fill="", outline=colour, width=1, smooth=False)

        self.canvas.create_oval(494, 312, 512, 330, fill="", outline="#C7CEF0", width=2)
        self.canvas.create_oval(268, 246, 282, 260, fill="#D8F5F0", outline="")

        for r in range(5):
            for c in range(7):
                x, y = 262 + c * 9, 186 + r * 9
                self.canvas.create_oval(x, y, x + 2, y + 2, fill="#D5DBF0", outline="")

    # ---------- clock + weather ----------
    def _build_clock_card(self):
        self._card(LCOL_X0, CLOCK_Y0, LCOL_X1, CLOCK_Y1, 18)
        cx, cy, r = LCOL_X0 + 32, CLOCK_Y0 + 32, 21

        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                fill="#F5F7FF", outline=COL_INDIGO, width=2)
        for i in range(12):
            a = math.pi * i / 6
            self.canvas.create_line(cx + (r - 5) * math.sin(a), cy - (r - 5) * math.cos(a),
                                    cx + (r - 3) * math.sin(a), cy - (r - 3) * math.cos(a),
                                    fill="#C3CBEA", width=1)
        self.hour_hand = self.canvas.create_line(cx, cy, cx, cy - 9,
                                                 fill=COL_TEXT, width=2, capstyle="round")
        self.minute_hand = self.canvas.create_line(cx, cy, cx, cy - 14,
                                                   fill=COL_INDIGO, width=2, capstyle="round")
        self.canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=COL_TEXT, outline="")
        self._clock_centre = (cx, cy)

        self.clock_id = self.canvas.create_text(
            LCOL_X0 + 62, CLOCK_Y0 + 26, text="--:--", anchor="w",
            font=self._font(22, True), fill=COL_TEXT)
        self.meridiem_id = self.canvas.create_text(
            LCOL_X0 + 62, CLOCK_Y0 + 32, text="", anchor="w",
            font=self._font(9, True), fill=COL_TEXT_DIM)
        self.date_id = self.canvas.create_text(
            LCOL_X0 + 62, CLOCK_Y0 + 52, text="", anchor="w",
            font=self._font(8), fill=COL_TEXT_DIM)

        self.canvas.create_line(LCOL_X0 + 14, CLOCK_Y0 + 72, LCOL_X1 - 14, CLOCK_Y0 + 72,
                                fill=COL_CARD_EDGE)

        self.weather_glyph = []
        self.weather_glyph_at = (LCOL_X0 + 32, CLOCK_Y0 + 104)
        self.temp_id = self.canvas.create_text(
            LCOL_X0 + 60, CLOCK_Y0 + 98, text="--", anchor="w",
            font=self._font(18, True), fill=COL_TEXT)
        self.desc_id = self.canvas.create_text(
            LCOL_X0 + 60, CLOCK_Y0 + 120, text="", anchor="w",
            font=self._font(8), fill=COL_INDIGO)

        self.canvas.create_line(LCOL_X0 + 128, CLOCK_Y0 + 88, LCOL_X0 + 128, CLOCK_Y0 + 122,
                                fill=COL_CARD_EDGE)
        self._arrow(LCOL_X0 + 140, CLOCK_Y0 + 95, up=True)
        self._arrow(LCOL_X0 + 140, CLOCK_Y0 + 115, up=False)
        self.high_id = self.canvas.create_text(
            LCOL_X0 + 150, CLOCK_Y0 + 95, text="--", anchor="w",
            font=self._font(8, True), fill=COL_TEXT)
        self.low_id = self.canvas.create_text(
            LCOL_X0 + 150, CLOCK_Y0 + 115, text="--", anchor="w",
            font=self._font(8, True), fill=COL_TEXT)
        self.city_id = self.canvas.create_text(
            (LCOL_X0 + LCOL_X1) / 2, CLOCK_Y1 - 16,
            text=WEATHER_CITY if WEATHER_API_KEY else "Weather unavailable",
            font=self._font(7), fill=COL_TEXT_FAINT)
        self._draw_weather_glyph("01d")

    def _arrow(self, x, y, up):
        tip = y - 6 if up else y + 6
        colour = "#EF4444" if up else "#3B82F6"
        self.canvas.create_line(x, y + (6 if up else -6), x, tip, fill=colour, width=2)
        self.canvas.create_polygon(x - 4, tip + (4 if up else -4), x + 4, tip + (4 if up else -4),
                                   x, tip, fill=colour, outline="")

    def _draw_weather_glyph(self, code):
        for item in self.weather_glyph:
            self.canvas.delete(item)
        self.weather_glyph = []

        cx, cy = self.weather_glyph_at
        c = self.canvas
        kind = code[:2]
        sun = "#FBBF24"
        cloud = "#A9B2C8"
        add = self.weather_glyph.append

        if kind == "01":                                   # clear
            add(c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, fill=sun, outline=""))
            for i in range(8):
                a = math.pi * i / 4
                add(c.create_line(cx + 14 * math.cos(a), cy + 14 * math.sin(a),
                                  cx + 19 * math.cos(a), cy + 19 * math.sin(a),
                                  fill=sun, width=2))
            return
        if kind == "02":                                   # sun behind cloud
            add(c.create_oval(cx - 2, cy - 18, cx + 16, cy, fill=sun, outline=""))
        if kind == "13":                                   # snow
            for i in range(3):
                sx = cx - 9 + i * 9
                add(c.create_line(sx - 3, cy + 11, sx + 3, cy + 18, fill="#60A5FA", width=2))
                add(c.create_line(sx + 3, cy + 11, sx - 3, cy + 18, fill="#60A5FA", width=2))
        elif kind in ("09", "10"):                         # rain
            for i in range(3):
                sx = cx - 9 + i * 9
                add(c.create_line(sx, cy + 11, sx - 3, cy + 19, fill="#60A5FA", width=2))
        elif kind == "11":                                 # storm
            add(c.create_polygon(cx + 2, cy + 9, cx - 5, cy + 20, cx, cy + 20,
                                 cx - 4, cy + 29, cx + 7, cy + 16, cx + 2, cy + 16,
                                 fill="#FBBF24", outline=""))
        elif kind == "50":                                 # mist
            for i in range(3):
                add(c.create_line(cx - 14, cy + 3 + i * 6, cx + 14, cy + 3 + i * 6,
                                  fill=cloud, width=2))
            return

        add(c.create_oval(cx - 16, cy - 5, cx + 1, cy + 9, fill=cloud, outline=""))
        add(c.create_oval(cx - 6, cy - 12, cx + 12, cy + 7, fill=cloud, outline=""))
        add(c.create_rectangle(cx - 14, cy + 1, cx + 11, cy + 9, fill=cloud, outline=""))

    # ---------- music player ----------
    def _build_music_card(self):
        self._card(LCOL_X0, MUSIC_Y0, LCOL_X1, MUSIC_Y1, 18)
        self.canvas.create_text(LCOL_X0 + 14, MUSIC_Y0 + 18, text="MUSIC PLAYER",
                                anchor="w", font=self._font(9, True), fill=COL_INDIGO)
        self._bars_glyph(LCOL_X1 - 24, MUSIC_Y0 + 18, COL_INDIGO)

        art = ImageTk.PhotoImage(_album_art_image(46))
        self._photos.append(art)
        self.canvas.create_image(LCOL_X0 + 14, MUSIC_Y0 + 34, image=art, anchor="nw")

        self.track_id = self.canvas.create_text(
            LCOL_X0 + 68, MUSIC_Y0 + 48, text="", anchor="w",
            font=self._font(10, True), fill=COL_TEXT)
        self.artist_id = self.canvas.create_text(
            LCOL_X0 + 68, MUSIC_Y0 + 66, text="", anchor="w",
            font=self._font(8), fill=COL_INDIGO)

        bx0, bx1, by = LCOL_X0 + 14, LCOL_X1 - 14, MUSIC_Y0 + 98
        self._progress_span = (bx0, bx1, by)
        self._round_rect(bx0, by - 2, bx1, by + 2, 2, fill=COL_TRACK, outline="")
        self.progress_fill = self._round_rect(bx0, by - 2, bx0 + 1, by + 2, 2,
                                              fill=COL_INDIGO, outline="")
        self.progress_knob = self.canvas.create_oval(bx0 - 5, by - 5, bx0 + 5, by + 5,
                                                     fill=COL_INDIGO, outline=COL_CARD, width=2)
        self.elapsed_id = self.canvas.create_text(bx0, by + 16, text="00:00", anchor="w",
                                                  font=self._font(7), fill=COL_TEXT_DIM)
        self.duration_id = self.canvas.create_text(bx1, by + 16, text="00:00", anchor="e",
                                                   font=self._font(7), fill=COL_TEXT_DIM)

        cy = MUSIC_Y0 + 142
        self.shuffle_items = self._shuffle_glyph(LCOL_X0 + 24, cy)
        self.prev_items = self._skip_glyph(LCOL_X0 + 56, cy, forward=False)
        self.next_items = self._skip_glyph(LCOL_X1 - 56, cy, forward=True)
        self.repeat_items = self._repeat_glyph(LCOL_X1 - 24, cy)

        pcx = (LCOL_X0 + LCOL_X1) / 2
        self.play_ring = self.canvas.create_oval(pcx - 17, cy - 17, pcx + 17, cy + 17,
                                                 fill=COL_INDIGO, outline="", tags="playpause")
        self.play_left = self.canvas.create_rectangle(pcx - 5, cy - 6, pcx - 2, cy + 6,
                                                      fill=COL_CARD, outline="", tags="playpause")
        self.play_right = self.canvas.create_rectangle(pcx + 2, cy - 6, pcx + 5, cy + 6,
                                                       fill=COL_CARD, outline="", tags="playpause")
        self.play_tri = self.canvas.create_polygon(pcx - 5, cy - 7, pcx + 7, cy, pcx - 5, cy + 7,
                                                   fill=COL_CARD, outline="", state="hidden",
                                                   tags="playpause")
        self.canvas.tag_bind("playpause", "<Button-1>", self.toggle_media_pause)

    def _shuffle_glyph(self, cx, cy):
        c, w = self.canvas, 2
        return [
            c.create_line(cx - 8, cy - 4, cx - 2, cy - 4, cx + 3, cy + 4, cx + 8, cy + 4,
                          fill=COL_TEXT_FAINT, width=w, smooth=False),
            c.create_line(cx - 8, cy + 4, cx - 2, cy + 4, cx + 3, cy - 4, cx + 8, cy - 4,
                          fill=COL_TEXT_FAINT, width=w, smooth=False),
            c.create_polygon(cx + 5, cy - 7, cx + 9, cy - 4, cx + 5, cy - 1,
                             fill=COL_TEXT_FAINT, outline=""),
            c.create_polygon(cx + 5, cy + 1, cx + 9, cy + 4, cx + 5, cy + 7,
                             fill=COL_TEXT_FAINT, outline=""),
        ]

    def _skip_glyph(self, cx, cy, forward):
        c, s = self.canvas, 1 if forward else -1
        return [
            c.create_polygon(cx - 7 * s, cy - 7, cx + 3 * s, cy, cx - 7 * s, cy + 7,
                             fill=COL_TEXT_FAINT, outline=""),
            c.create_rectangle(cx + 3 * s, cy - 7, cx + 6 * s, cy + 7,
                               fill=COL_TEXT_FAINT, outline=""),
        ]

    def _repeat_glyph(self, cx, cy):
        c = self.canvas
        return [
            c.create_arc(cx - 8, cy - 7, cx + 8, cy + 7, start=20, extent=300,
                         style="arc", outline=COL_TEXT_FAINT, width=2),
            c.create_polygon(cx + 4, cy - 9, cx + 9, cy - 5, cx + 3, cy - 2,
                             fill=COL_TEXT_FAINT, outline=""),
        ]

    def _build_mascot(self):
        # Sits between the header dots and the top of her ears, so it reads as
        # hers without covering the character.
        self.state_pill = self._round_rect(MASCOT_CX - 40, STATE_LABEL_Y - 10,
                                           MASCOT_CX + 40, STATE_LABEL_Y + 10, 10,
                                           fill=COL_TRACK, outline="")
        self.state_text_id = self.canvas.create_text(
            MASCOT_CX, STATE_LABEL_Y, text="", font=self._font(9, True), fill=COL_TEXT_DIM)
        self.mascot_item = self.canvas.create_image(MASCOT_CX, MASCOT_CY)


    # ---------- mode cards ----------
    def _mode_glyph(self, kind, cx, cy, colour, tint):
        c = self.canvas
        items = [self._round_rect(cx - 15, cy - 15, cx + 15, cy + 15, 9,
                                  fill=tint, outline="")]
        if kind == "TUTOR":                      # mortarboard over books
            items.append(c.create_polygon(cx, cy - 9, cx + 10, cy - 4, cx, cy + 1, cx - 10, cy - 4,
                                          fill=colour, outline=""))
            items.append(c.create_rectangle(cx - 5, cy, cx + 5, cy + 4, fill=colour, outline=""))
            items.append(c.create_line(cx + 10, cy - 4, cx + 10, cy + 4, fill=colour, width=2))
            items.append(self._round_rect(cx - 9, cy + 6, cx + 9, cy + 11, 2,
                                          fill=_mix(colour, "#FFFFFF", 0.45), outline=""))
        elif kind == "CO-TELL":                  # two chat bubbles
            items.append(self._round_rect(cx - 11, cy - 10, cx + 4, cy + 1, 4,
                                          fill=colour, outline=""))
            items.append(self._round_rect(cx - 3, cy - 1, cx + 11, cy + 10, 4,
                                          fill=_mix(colour, "#FFFFFF", 0.45), outline=""))
        else:                                    # head speaking
            items.append(c.create_oval(cx - 10, cy - 9, cx + 1, cy + 2, fill=colour, outline=""))
            items.append(c.create_polygon(cx - 10, cy + 1, cx + 1, cy + 1, cx + 1, cy + 9,
                                          cx - 10, cy + 9, fill=colour, outline=""))
            for r in (5, 8):
                items.append(c.create_arc(cx + 1 - r, cy - r, cx + 1 + r, cy + r,
                                          start=-55, extent=110, style="arc",
                                          outline=colour, width=2))
        return items

    def _build_mode_cards(self):
        self._card(RCOL_X0, MODES_Y0, RCOL_X1, MODES_Y1, 18)
        self.canvas.create_text(RCOL_X0 + 12, MODES_Y0 + 18, text="CHOOSE MODE",
                                anchor="w", font=self._font(10, True), fill=COL_TEXT)
        for dx, dy, s in ((-16, -4, 5), (-6, 4, 3), (-26, 5, 3)):
            x, y = RCOL_X1 - 12 + dx, MODES_Y0 + 18 + dy
            self.canvas.create_polygon(x, y - s, x + s * 0.35, y - s * 0.35, x + s, y,
                                       x + s * 0.35, y + s * 0.35, x, y + s,
                                       x - s * 0.35, y + s * 0.35, x - s, y,
                                       x - s * 0.35, y - s * 0.35,
                                       fill="#A78BFA", outline="")

        self.cards = []
        for i, mode in enumerate(self.modes):
            y0 = MODE_CARD_Y0 + i * (MODE_CARD_H + MODE_CARD_GAP)
            y1 = y0 + MODE_CARD_H
            accent = MODE_ACCENTS[mode]
            tag = f"mode{i}"

            body = self._round_rect(MODE_CARD_X0, y0, MODE_CARD_X1, y1, 12,
                                    fill=MODE_TINTS[mode], outline=MODE_TINTS[mode],
                                    width=1, tags=tag)
            glyph = self._mode_glyph(mode, MODE_CARD_X0 + 22, (y0 + y1) / 2,
                                     accent, "#FFFFFF")
            title = self.canvas.create_text(MODE_CARD_X0 + 42, y0 + 15,
                                            text=f"{mode} MODE", anchor="w",
                                            font=self._font(9, True), fill=accent, tags=tag)
            blurb = self.canvas.create_text(MODE_CARD_X0 + 42, y0 + 28, text=MODE_BLURBS[mode],
                                            anchor="nw", justify="left",
                                            width=MODE_CARD_X1 - MODE_CARD_X0 - 58,
                                            font=self._font(7), fill=COL_TEXT_DIM, tags=tag)
            chevron = self.canvas.create_line(
                MODE_CARD_X1 - 14, (y0 + y1) / 2 - 5,
                MODE_CARD_X1 - 9, (y0 + y1) / 2,
                MODE_CARD_X1 - 14, (y0 + y1) / 2 + 5,
                fill=accent, width=2, capstyle="round", joinstyle="round", tags=tag)
            for item in glyph:
                self.canvas.itemconfig(item, tags=tag)
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, idx=i: self.set_mode(idx))
            self.cards.append({"body": body, "title": title, "blurb": blurb,
                               "chevron": chevron, "accent": accent, "tint": MODE_TINTS[mode]})

    # ---------- transcript ----------
    def _build_transcript_panel(self):
        self._card(RCOL_X0, TRANS_Y0, RCOL_X1, TRANS_Y1, 18)
        pad = 12
        self.canvas.create_text(RCOL_X0 + pad, TRANS_Y0 + 18, text="TRANSCRIBE",
                                anchor="w", font=self._font(10, True), fill=COL_TEXT)
        self.panel_bars = self._bars_glyph(RCOL_X1 - pad - 8, TRANS_Y0 + 18, COL_INDIGO,
                                           (5, 9, 13, 9, 5))
        self.canvas.create_line(RCOL_X0 + pad, TRANS_Y0 + 30, RCOL_X1 - pad, TRANS_Y0 + 30,
                                fill=COL_CARD_EDGE)
        self.speaker_id = self.canvas.create_text(
            RCOL_X0 + pad, TRANS_Y0 + 44, text="You said:", anchor="w",
            font=self._font(8, True), fill=COL_INDIGO)
        self.transcript_id = self.canvas.create_text(
            RCOL_X0 + pad, TRANS_Y0 + 56,
            text="Tap SPEAK or say “Hey Liza” to begin.",
            anchor="nw", justify="left", width=RCOL_X1 - RCOL_X0 - pad * 2,
            font=self._font(9), fill=COL_TEXT_DIM)

        self.panel_status_id = self.canvas.create_text(
            RCOL_X1 - pad - 26, TRANS_Y1 - 14, text="", anchor="e",
            font=self._font(8), fill=COL_TEXT_DIM)
        self.status_dots = []
        for i in range(3):
            x = RCOL_X1 - pad - 20 + i * 8
            self.status_dots.append(self.canvas.create_oval(
                x - 3, TRANS_Y1 - 17, x + 3, TRANS_Y1 - 11, fill=COL_TRACK, outline=""))

    # ---------- action buttons ----------
    def _build_buttons(self):
        self.buttons = {}
        handlers = {"SPEAK": self.wake_up, "STOP": self.stop_speaking, "SLEEP": self.go_to_sleep}
        for (label, sub, c0, c1, sub_col, icon), x0 in zip(ACTIONS, BTN_XS):
            tag = f"btn{label}"
            self._place_photo(_action_image(BTN_W, BTN_H, 14, c0, c1, icon), x0, BTN_Y0, tag)
            self.canvas.create_text(x0 + 72, BTN_Y0 + 26, text=label, anchor="w",
                                    font=self._font(14, True), fill="#FFFFFF", tags=tag)
            self.canvas.create_text(x0 + 72, BTN_Y0 + 44, text=sub, anchor="w",
                                    font=self._font(8), fill=sub_col, tags=tag)
            self.canvas.tag_bind(tag, "<Button-1>", handlers[label])
            self.buttons[label] = tag

    # ---------- runtime ----------
    def _animate(self):
        label, colour, activity = STATE_STYLE.get(self.current_state, STATE_STYLE["idle"])
        self.phase += 0.12
        self.frame += 1

        bucket = MASCOT_FOR_STATE.get(self.current_state, "idle")
        if bucket != self._mascot_bucket:
            self._mascot_bucket = bucket
            self.mascot_index = 0
        frames = None
        if self.ui_mode == "3d":
            frames = self.mascot_frames_big.get(bucket)
            if frames is None:
                # A state change inside 3D mode: this bucket may not be built yet.
                self._ensure_big_frames(bucket)
        # The two sets come from the same PNGs, so mascot_index is valid in
        # either and the animation does not jump when the big set arrives.
        frames = frames or self.mascot_frames.get(bucket) or []
        if frames:
            # Asleep she should look settled, not idling, so the loop is parked.
            if self.current_state != "sleeping":
                self.mascot_index = (self.mascot_index + 1) % len(frames)
            self.canvas.itemconfig(self.mascot_item, image=frames[self.mascot_index])

        for i, dot in enumerate(self.head_dots):
            swing = math.sin(self.phase * 2.3 + i * 0.9) ** 2
            r = 3 + 3 * activity * swing
            x = MASCOT_CX - 16 + i * 16
            self.canvas.coords(dot, x - r, DOTS_Y - r, x + r, DOTS_Y + r)

        for i, bar in enumerate(self.panel_bars):
            swing = math.sin(self.phase * 2.6 + i * 0.62) ** 2
            h = 3 + 12 * (0.25 + 0.75 * swing) * max(activity, 0.15)
            x = self.canvas.coords(bar)[0]
            self.canvas.coords(bar, x, TRANS_Y0 + 18 - h / 2, x, TRANS_Y0 + 18 + h / 2)
            self.canvas.itemconfig(bar, fill=_mix(colour, COL_TRACK, 0.5 - 0.4 * swing * activity))

        caption = label.upper()
        if caption != self._state_caption:
            # Only remeasured when the wording changes; the pill hugs captions
            # as different as "LISTENING" and "SOMETHING'S WRONG".
            self._state_caption = caption
            half = self._font(9, True).measure(caption) / 2 + 12
            self.canvas.coords(self.state_pill, *self._round_rect_points(
                MASCOT_CX - half, STATE_LABEL_Y - 10, MASCOT_CX + half, STATE_LABEL_Y + 10, 10))
            self.canvas.itemconfig(self.state_text_id, text=caption)
        self.canvas.itemconfig(self.state_text_id, fill=_mix(colour, COL_TEXT, 0.2))
        self.canvas.itemconfig(self.state_pill, fill=_mix(colour, "#FFFFFF", 0.86))

        busy = self.current_state in ("listening", "thinking", "speaking")
        self.canvas.itemconfig(self.panel_status_id, text=label if busy else "",
                               fill=colour)
        for i, dot in enumerate(self.status_dots):
            lit = busy and (self.frame // 6) % 3 == i
            self.canvas.itemconfig(dot, fill=colour if lit else COL_TRACK)

        self._refresh_media_controls()
        self.root.after(FRAME_MS, self._animate)

    def _refresh_cards(self):
        for i, card in enumerate(self.cards):
            chosen = i == self.current_mode_index
            accent = card["accent"]
            self.canvas.itemconfig(card["body"],
                                   fill=_mix(card["tint"], "#FFFFFF", 0.35) if chosen else card["tint"],
                                   outline=accent if chosen else card["tint"],
                                   width=2 if chosen else 1)
            self.canvas.itemconfig(card["blurb"], fill=COL_TEXT if chosen else COL_TEXT_DIM)

    # ---------- music ----------
    def set_now_playing(self, title, loading=False):
        """YouTube titles are usually "Artist - Track (Official Video)", so the
        card shows the two halves separately when that shape is recognisable.

        `loading` covers the gap between asking for something and hearing it:
        yt-dlp has to fetch the page, solve YouTube's JS challenge and buffer
        before mpv renders a thing, which runs to ten seconds or more on this
        Pi. Without it the screen sits on the idle UI the whole time and a
        request that is working looks like one that was ignored."""
        if title:
            artist, _, track = title.partition(" - ")
            if not track:
                artist, track = "", title
        else:
            artist, track = "", "Nothing playing"

        self.media.update({"title": title, "artist": artist.strip(),
                           "pos": 0.0, "dur": 0.0, "paused": False})
        self.canvas.itemconfig(
            self.track_id,
            text=self._ellipsize(track.strip(), self._font(10, True), LCOL_X1 - LCOL_X0 - 82),
            fill=COL_TEXT if title else COL_TEXT_DIM)
        self.canvas.itemconfig(
            self.artist_id,
            text=self._ellipsize("Loading…" if loading else
                                 (artist.strip() or ("—" if title else "Ask me to play a song")),
                                 self._font(8), LCOL_X1 - LCOL_X0 - 82))
        self.set_media_progress(0.0, 0.0, False)

    def set_media_progress(self, pos, dur, paused):
        self.media.update({"pos": pos or 0.0, "dur": dur or 0.0, "paused": bool(paused)})
        bx0, bx1, by = self._progress_span
        fraction = (pos / dur) if dur else 0.0
        fraction = max(0.0, min(1.0, fraction))
        x = bx0 + (bx1 - bx0) * fraction

        self.canvas.coords(self.progress_fill, *self._round_rect_points(bx0, by - 2,
                                                                       max(bx0 + 1, x), by + 2, 2))
        self.canvas.coords(self.progress_knob, x - 5, by - 5, x + 5, by + 5)
        self.canvas.itemconfig(self.elapsed_id, text=_fmt_clock(pos))
        self.canvas.itemconfig(self.duration_id, text=_fmt_clock(dur))

    def _round_rect_points(self, x0, y0, x1, y1, r):
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
                x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

    def _refresh_media_controls(self):
        # 3D mode hides the music card, and this runs every frame: without this
        # line it puts the play/pause glyphs straight back on an otherwise empty
        # screen, sixteen times a second.
        if self.ui_mode == "3d":
            return
        live = media_active.is_set()
        shade = COL_INDIGO if live else COL_TEXT_FAINT
        self.canvas.itemconfig(self.play_ring, fill=shade)
        paused = self.media["paused"]
        self.canvas.itemconfig(self.play_tri, state="normal" if paused else "hidden")
        for item in (self.play_left, self.play_right):
            self.canvas.itemconfig(item, state="hidden" if paused else "normal")
        self.canvas.itemconfig(self.progress_fill, fill=shade)
        self.canvas.itemconfig(self.progress_knob, fill=shade)

    def toggle_media_pause(self, event=None):
        if media_active.is_set():
            mpv_command(["cycle", "pause"])
        return "break"

    # ---------- emotion ----------
    def set_emotion(self, mood):
        """Record how she says she is feeling. Nothing is drawn for it any more.

        The chip that used to sit under her (CURIOUS, CALM, HAPPY...) was removed
        on request. The EMOTION: line is still parsed and kept here, so anything
        that wants it later -- a log, an expression, a different treatment --
        still has it; it simply has no pill on screen."""
        mood = (mood or "").strip().lower()
        if mood:
            self.emotion = mood

    # ---------- 3D-only mode ----------
    def set_ui_mode(self, mode):
        """Hide every widget but the mascot, or put them all back.

        Everything on screen is one canvas, so this is done by item state rather
        than by tearing anything down: nothing is rebuilt, nothing loses its
        contents, and music carries on playing behind it."""
        mode = "3d" if mode == "3d" else "normal"
        if mode == self.ui_mode:
            return
        # The wallpaper has to survive 3D mode too. This set is what is NOT
        # hidden, and everything else on the canvas is -- so leaving the
        # background out of it blanked the whole picture the moment she went
        # 3D-only, which is the one mode where she is all there is to look at.
        keep = {self.mascot_item}
        if self.bg_item is not None:
            keep.add(self.bg_item)

        if mode == "3d":
            self._hidden_items = []
            for item in self.canvas.find_all():
                if item in keep:
                    continue
                # Only what is visible NOW is recorded. The media buttons are
                # already shown and hidden every frame by _refresh_media_controls,
                # and force-showing those on the way back would put a play
                # triangle on screen over a track that is not paused.
                if self.canvas.itemcget(item, "state") == "hidden":
                    continue
                self.canvas.itemconfigure(item, state="hidden")
                self._hidden_items.append(item)
            self.canvas.coords(self.mascot_item, UI_W // 2, MASCOT_3D_CY)
            self._ensure_big_frames(self._mascot_bucket or "idle")
        else:
            for item in self._hidden_items:
                try:
                    self.canvas.itemconfigure(item, state="normal")
                except tk.TclError:
                    pass
            self._hidden_items = []
            self.canvas.coords(self.mascot_item, MASCOT_CX, MASCOT_CY)

        self.ui_mode = mode
        # The SPEAK button is hidden in 3D mode, but the root tap-to-wake binding
        # is not, so a tap anywhere still wakes her. Nothing here is a dead end.
        print(f"[UI] Mode -> {mode}.", flush=True)

    def _ensure_big_frames(self, bucket):
        """Upscale one animation for 3D mode, without stalling the UI.

        The resize is the expensive half and runs on a worker; the PhotoImage
        half has to happen on the Tk thread, so it is fed back in small batches.
        Until it lands, _animate falls back to the normal-size frames -- 3D mode
        is never made to wait for this."""
        if not bucket or bucket in self.mascot_frames_big or bucket in self._big_pending:
            return
        paths = sorted(glob.glob(os.path.join(MASCOT_CACHE_DIR, bucket, "*.png")))
        if not paths:
            return
        self._big_pending.add(bucket)
        holder = {"images": None}

        def work():
            images = []
            for path in paths:
                try:
                    images.append(Image.open(path).convert("RGBA")
                                  .resize((MASCOT_3D_W, MASCOT_3D_H), Image.LANCZOS))
                except Exception:
                    pass
            # A plain assignment and nothing else. This thread must not touch Tk
            # at all -- not even root.after -- so the Tk side polls for it below.
            holder["images"] = images

        threading.Thread(target=work, daemon=True).start()
        self.root.after(150, lambda: self._absorb_big_frames(bucket, holder, []))

    def _absorb_big_frames(self, bucket, holder, done):
        """Tk-thread half: PhotoImages, a few per tick so nothing stutters."""
        images = holder["images"]
        if images is None:
            self.root.after(150, lambda: self._absorb_big_frames(bucket, holder, done))
            return
        for image in images[len(done):len(done) + 8]:
            done.append(ImageTk.PhotoImage(image))
        if len(done) < len(images):
            self.root.after(16, lambda: self._absorb_big_frames(bucket, holder, done))
            return
        self.mascot_frames_big[bucket] = done
        self._big_pending.discard(bucket)
        print(f"[MASCOT] 3D frames ready for '{bucket}' ({len(done)}).", flush=True)

    def set_weather(self, reading):
        self.canvas.itemconfig(self.temp_id, text=f"{reading['temp']}°C")
        self.canvas.itemconfig(self.desc_id, text=reading["desc"])
        self.canvas.itemconfig(self.high_id, text=f"{reading['high']}°")
        self.canvas.itemconfig(self.low_id, text=f"{reading['low']}°")
        self.canvas.itemconfig(self.city_id,
                               text=f"{reading['city']}  ·  feels {reading['feels']}°"
                                    f"  ·  {reading['humidity']}%")
        self._draw_weather_glyph(reading["icon"])

    def _tick_clock(self):
        now = datetime.now()
        text = now.strftime("%I:%M").lstrip("0")
        self.canvas.itemconfig(self.clock_id, text=text)
        # Placed by measurement rather than a fixed offset: "9:05" and "12:45"
        # are very different widths and the meridiem has to sit against both.
        self.canvas.coords(self.meridiem_id,
                           LCOL_X0 + 66 + self._font(22, True).measure(text), CLOCK_Y0 + 32)
        self.canvas.itemconfig(self.meridiem_id, text=now.strftime("%p"))
        self.canvas.itemconfig(self.date_id, text=now.strftime("%a, %d %b %Y"))

        cx, cy = self._clock_centre
        minute = math.pi * now.minute / 30
        hour = math.pi * ((now.hour % 12) + now.minute / 60) / 6
        self.canvas.coords(self.minute_hand, cx, cy,
                           cx + 14 * math.sin(minute), cy - 14 * math.cos(minute))
        self.canvas.coords(self.hour_hand, cx, cy,
                           cx + 9 * math.sin(hour), cy - 9 * math.cos(hour))
        self.root.after(1000, self._tick_clock)

    def set_state(self, state, caption=None):
        if state not in STATE_STYLE:
            state = "idle"

        # Sleep is a deliberate instruction and outranks whatever is playing;
        # everything else has to wait for the speaker to fall quiet, or the
        # mascot flips to "listening" over the top of her own voice.
        if state != "sleeping" and self.asleep:
            return
        if state in ("idle", "listening", "warmup") and (
                playback_active.is_set() or not audio_queue.empty() or media_active.is_set()):
            return

        self.current_state = state

    def set_transcript(self, text, speaker="user"):
        text = (text or "").strip()
        if not text:
            return
        self.transcript = text
        self.speaker = speaker
        liza = speaker == "liza"
        self.canvas.itemconfig(self.speaker_id,
                               text="Liza said:" if liza else "You said:",
                               fill=STATE_STYLE["speaking"][1] if liza else COL_INDIGO)
        self.canvas.itemconfig(self.transcript_id, text=text, fill=COL_TEXT)

    def wake_up(self, event=None):
        """The Speak button. Wakes her from anything, including sleep."""
        print("[UI] Speak tapped. Waking up...", flush=True)
        self.asleep = False
        sleep_event.clear()
        wake_event.set()
        return "break"

    def tap_to_wake(self, event=None):
        """Any tap on the screen away from a control.

        Ignored while she is asleep, which is the whole point of the Sleep
        button: this panel reports stray touches on its own, and treating
        those as a wake would put her straight back to listening. Asleep,
        only the Speak button or the wake word count.
        """
        if self.asleep:
            return
        return self.wake_up(event)

    def stop_speaking(self, event=None):
        # Music/video first: audio-only playback shows no window of its own, so
        # this button is the only way to stop a song.
        if media_active.is_set():
            print("[UI] Stop tapped, stopping media playback.", flush=True)
            stop_media_playback()
        if playback_active.is_set() or not audio_queue.empty():
            print("[UI] Stop tapped, cutting the reply short.", flush=True)
            interrupt_playback()
        return "break"          # do not let the tap fall through and re-wake her

    def go_to_sleep(self, event=None):
        print("[UI] Sleep tapped. Going to standby...", flush=True)
        self.asleep = True
        wake_event.clear()
        sleep_event.set()
        if media_active.is_set():
            stop_media_playback()
        if playback_active.is_set() or not audio_queue.empty():
            interrupt_playback()
        self.current_state = "sleeping"
        return "break"          # otherwise the root tap-to-wake binding undoes this

    def set_mode(self, index):
        global pending_mode_intro
        if index == self.current_mode_index:
            return
        self.current_mode_index = index
        self.current_mode = self.modes[index]
        self._refresh_cards()

        # Deliberately does NOT call interrupt_playback() here. This runs on the
        # Tk thread, which can fire while ai_loop has the microphone open inside
        # a blocking PyAudio read; tearing down aplay and starting new playback
        # underneath that read wedges the capture stream and the read never
        # returns, which froze the assistant permanently after every mode
        # change. ai_loop picks this up and speaks it once the mic is closed.
        #
        # Setting stop_playback_event here IS safe though -- it is a plain
        # Event flag, not a teardown of any subprocess or audio device. Without
        # it, a mode tap during an LLM reply sat invisible until that whole
        # reply finished generating and speaking (worker.join() in ai_loop
        # blocks on it), so switching modes felt like it required tapping Stop
        # first -- that tap was the thing actually breaking the reply early.
        # Setting the event here makes the in-flight stream_hf()/speak_sentence()
        # loops notice and bail out within one chunk, same as a Stop tap does,
        # so ai_loop reaches the top of its loop -- and this pending intro --
        # right away. ai_loop's own pending_mode_intro handler still does the
        # real interrupt_playback() sweep and clears this event again.
        stop_playback_event.set()
        pending_mode_intro = MODE_INTROS[self.current_mode]

    def cycle_mode(self, event=None):
        self.set_mode((self.current_mode_index + 1) % len(self.modes))

class HeadlessUI:
    def __init__(self):
        self.current_state = "idle"
        self.modes = ["TUTOR", "CO-TELL", "RE-TELL"]
        self.current_mode_index = 0
        self.current_mode = self.modes[0]
        self.asleep = False
        self.ui_mode = "normal"
        self.emotion = None
    def set_state(self, state_type, caption=None): self.current_state = state_type
    def set_transcript(self, text, speaker="user"): pass
    def set_weather(self, reading): pass
    def set_now_playing(self, title, loading=False): pass
    def set_media_progress(self, pos, dur, paused): pass
    def set_ui_mode(self, mode):
        # No widgets to hide with no screen, but the state still has to agree
        # with what the model is told, or it will keep re-issuing the action.
        self.ui_mode = "3d" if mode == "3d" else "normal"
    def set_emotion(self, mood): self.emotion = (mood or "").strip().lower() or None
    def go_to_sleep(self, event=None):
        self.asleep = True
        wake_event.clear()
        sleep_event.set()
        if media_active.is_set(): stop_media_playback()
        if playback_active.is_set() or not audio_queue.empty(): interrupt_playback()

# ==========================================
# Core AI Functions
# ==========================================
# Bilingual seed so Whisper is not biased towards English on the first turn.
# Whisper's `prompt` is a bias, not an instruction: it nudges the decoder towards
# the vocabulary it contains. This is a STUDY device, so the words it will hear
# are school-science words -- and without them in here the decoder reaches for
# whatever is commonest in its training data, which is not school science.
# Reported from the room: "play a mitochondria" came back as "play a
# microcontroller", and the search then went looking for microcontroller
# videos. Naming a spread of subject terms costs nothing per call (the prompt is
# not billed as audio) and pulls those readings back the right way.
STT_SEED_PROMPT = (
    "Hey Liza, explain the concept clearly. नमस्ते लीज़ा, यह concept समझाओ। "
    "Biology: mitochondria, chloroplast, photosynthesis, ribosome, chromosome, "
    "enzyme, osmosis, respiration, DNA, neuron. "
    "Chemistry: electron, molecule, covalent, valency, oxidation, isotope, mole. "
    "Physics: velocity, acceleration, momentum, refraction, amplitude, "
    "resistance, magnetic field, gravitation. "
    "Maths: quadratic, theorem, trigonometry, logarithm, integration, matrix."
)

# Whisper invents these out of silence, in both languages.
HALLUCINATIONS = {
    "thank you.", "thank you", "thanks.", "thanks", "thanks for watching.",
    "you", "why?", ".", "bye.", "[empty]", "",
    "so,", "so.", "so",
    "i'm not sure if i can do it.", "i'm not sure.", "i'm not sure",
    "so, i'm going to go to the next slide.", "i'm going to go to the next slide.",
    "i'm not sure what you're doing.", "i'm not sure if you're a cat.",
    "yes.", "yeah.", "okay.",
    "धन्यवाद।", "धन्यवाद", "शुक्रिया।", "शुक्रिया", "नमस्ते।", "नमस्कार।",
    "जी हाँ।", "हाँ।", "जी।", "ठीक है।", "अच्छा।", "।"
}
RE_HALLUCINATION = re.compile(
    r'three, four|assistant is a professor|avoid casual|'
    # Whisper was trained on a lot of YouTube and invents outro lines out of
    # silence. Matched as a family rather than exact strings: "thanks for
    # watching." was listed but "Thank you for watching!" still got through and
    # was answered with "You're welcome!".
    r'(?:thanks|thank\s+you)\s+for\s+watching|'
    r'(?:don\'?t\s+forget\s+to\s+|please\s+|like\s+and\s+)subscribe|'
    r'see\s+you\s+(?:in\s+the\s+)?next\s+(?:time|video)|'
    r'सब्सक्राइब करें|वीडियो पसंद आया|अगले वीडियो में',
    re.IGNORECASE
)

def clean_text_for_tts(text):
    # FIRST, before the bracket stripping at the end of this function: that only
    # removes the brackets themselves, so an action tag reaching it comes out of
    # the speaker as the words "ACTION stop media".
    clean = RE_ACTION_TAG_STRIP.sub('', text)
    clean = re.sub(r'VISUAL:.*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'EMOTION:.*', '', clean, flags=re.IGNORECASE)
    # Then the same thing with the label dropped; see RE_BARE_EMOTION_LINE.
    # After the line above, so a well-formed "EMOTION: curious" is already gone
    # and this only ever sees the malformed shape it exists for.
    clean = RE_BARE_EMOTION_LINE.sub('', clean.lstrip('\n'), count=1)
    clean = RE_INLINE_EMOTION_TAG.sub('', clean.lstrip('\n'), count=1)
    clean = RE_ANSWER_PREFIX.sub('', clean)

    # Drop a leading greeting only when a real answer follows it. A reply that is
    # nothing but "Hello!" must survive, or the student is met with silence. Re-capitalise
    # what is left so "Hello, how can I help?" is not spoken as "how can I help?".
    trimmed = RE_GREETING_PREFIX.sub('', clean).lstrip()
    if trimmed:
        if trimmed[0].islower() and not trimmed[1:2].isupper():
            trimmed = trimmed[0].upper() + trimmed[1:]
        clean = trimmed

    clean = RE_EMOJI.sub('', clean)
    return clean.replace('*', '').replace('_', '').replace('#', '').replace('`', '').replace('[', '').replace(']', '').strip()

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
THINKING_FILLER_ENABLED = os.getenv("THINKING_FILLER", "1") != "0"
# 1.6s was set against a first-token time of 0.9s, which turned out to have been
# measured against a two-line prompt rather than the real one. Against the real
# prompt the model has never once answered that fast, so the line fired on every
# single turn and became a tic -- reported as exactly that. 2.5s clears the
# quick end of the measured spread (2.93s was the fastest of three identical
# requests) so a genuinely fast turn stays silent.
THINKING_FILLER_AFTER_S = float(os.getenv("THINKING_FILLER_AFTER_S", "2.5"))
# Rotated, because the model is slow often enough that one fixed phrase is what
# made it grating. A person waiting on a thought does not say the same four
# words every time either.
THINKING_FILLERS = {
    "en": ["One moment.", "Let me think.", "Just a second.", "Hmm, let me see."],
    "hi": ["एक सेकंड।", "थोड़ा रुकिए।", "सोच रही हूँ।", "बस एक पल।"],
}
THINKING_FILLERS["hinglish"] = THINKING_FILLERS["hi"]
_thinking_filler_index = 0

def start_thinking_filler(language):
    """Say something if the model has not started answering in time.

    Returns the Event to set as soon as real speech is queued; setting it
    cancels the filler if it has not already gone out. Safe to set more than
    once, so every path that queues speech can just set it."""
    answered = threading.Event()
    if not THINKING_FILLER_ENABLED:
        return answered
    def wait_and_fill():
        global _thinking_filler_index
        if answered.wait(THINKING_FILLER_AFTER_S):
            return          # she started talking in time; say nothing
        if answered.is_set() or stop_playback_event.is_set():
            return
        options = THINKING_FILLERS.get(language, THINKING_FILLERS["en"])
        audio_queue.put(options[_thinking_filler_index % len(options)])
        _thinking_filler_index += 1
    threading.Thread(target=wait_and_fill, daemon=True).start()
    return answered
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
            ui_call(lambda t=title: ui_instance.set_now_playing(t))
        ui_call(lambda p=position, d=duration, s=paused:
                ui_instance.set_media_progress(p, d, s))
        time.sleep(0.5)

def start_media_playback(kind, hit):
    """Streams `hit['url']` through yt-dlp into mpv (audio-only for music,
    fullscreen for video). Piped rather than handing mpv the raw googlevideo
    URL directly -- mpv's own ytdl_hook regularly fails to open those signed
    CDN links ("EDL: Could not open source file"), while piping the bytes
    yt-dlp already fetched through stdin has proven reliable in testing.
    The distro's yt-dlp package lags YouTube's changes badly, so the venv's
    own copy (kept current via `pip install -U yt-dlp`) is used explicitly."""
    global media_process
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

    global media_procs
    with subprocess_lock:
        active_subprocesses.extend([yt_proc, mpv_proc])
        media_procs = [yt_proc, mpv_proc]
    media_process = mpv_proc
    media_active.set()
    note_media_started()
    set_playing_state(hit["title"], kind)
    ui_call(lambda t=hit["title"]: ui_instance.set_now_playing(t, loading=True))
    threading.Thread(target=media_progress_worker, args=(hit["title"],),
                     daemon=True).start()

    def watcher():
        rc = mpv_proc.wait()
        try: yt_proc.terminate()
        except Exception: pass
        media_active.clear()
        with subprocess_lock:
            for p in (yt_proc, mpv_proc):
                if p in active_subprocesses: active_subprocesses.remove(p)
            # In-place: a bare assignment here would bind a local, not the global.
            media_procs[:] = []
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
        ui_call(lambda: ui_instance.set_now_playing(None))

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
        procs = list(media_procs)
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

def ui_invoke(method_name, *args):
    """Call a UI method on the Tk thread, or directly when there is no Tk.

    ui_call() drops everything in headless mode, which is right for repainting
    and wrong for these: going to sleep has to work with no screen attached."""
    target = ui_instance
    if target is None:
        return
    fn = getattr(target, method_name, None)
    if fn is None:
        return
    root = getattr(target, "root", None)
    if root is not None:
        root.after(0, lambda: fn(*args))
    else:
        fn(*args)

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
    if not text or not RE_DEVANAGARI.search(text):
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
    global currently_open_file
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
        currently_open_file = path

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
        # media_procs can only describe one of them anyway.
        if media_active.is_set():
            stop_media_playback()
        with subprocess_lock:
            active_subprocesses.append(opener)
            media_procs[:] = [opener]
        media_active.set()
        note_media_started()
        set_playing_state(os.path.basename(path), kind)
        ui_call(lambda t=os.path.basename(path): ui_instance.set_now_playing(t))

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
    global currently_open_file
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
            if currently_open_file != path:
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
            if opener in active_subprocesses:
                active_subprocesses.remove(opener)
            if media_procs == [opener]:
                media_procs[:] = []
        # Only if this opener is still the thing that owns the media state --
        # something newer may legitimately have taken it over.
        if not media_procs:
            media_active.clear()
            set_playing_state(None)
            ui_call(lambda: ui_instance.set_now_playing(None))

    if superseded:
        return
    with device_state_lock:
        if currently_open_file != path:
            return
        currently_open_file = None
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
    global currently_open_file
    with device_state_lock:
        path = currently_open_file
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
        currently_open_file = None

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
    global current_ui_mode
    mode = "3d" if mode in ("3d", "3-d", "three_d", "mascot") else "normal"
    with device_state_lock:
        if current_ui_mode == mode:
            return "already", mode
        current_ui_mode = mode
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
        done = openrouter_client.with_options(max_retries=0).chat.completions.create(
            model=LLM_MODEL,
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

# ==========================================
# Education-Only Guardrail
# ==========================================
# Enforced in the prompt rather than by a Python classifier on purpose. The
# boundary is a judgement call ("how does a court decide a case" is civics;
# "who will win the case" is not), and a keyword filter sitting in front of a
# noisy speech transcript rejects far more real questions than it catches bad
# ones. This text is pinned as the FIRST section of the system prompt so it
# outranks the mode instructions that follow it.
ASSISTANT_SCOPE = """SCOPE: YOU ARE A GENERAL ASSISTANT. HELP WITH WHATEVER IS ASKED.

No subject list -- studying, work, cooking, code, travel, sport, a film opinion, a joke, a weekend plan, all of it is yours. About to say some topic isn't what you're for? You're wrong: answer it. Unsure if it's in scope? It is.

YOU MAY HAVE VIEWS. Asked what you think, say it with the reason in a clause. Asked to recommend, name ONE thing, not five. Genuinely contested -- politics, religion, who deserves to win -- give what people actually disagree about rather than picking a side. That's judgement, not a refusal.

ADVICE IS FINE, AND SO IS ITS LIMIT. Health, money, law, personal things: answer with what's generally true and useful. Real stakes, their details -- a diagnosis, a contract, a large sum, a crisis -- add ONE closing clause pointing to a doctor or lawyer. Never open with it, never let it replace the answer.

THE NEWS ALWAYS COMES FROM A LIVE SEARCH, never memory (rule 4).

THE ONLY THINGS YOU REFUSE: help that lets someone hurt people or break in -- weapons, malware, drug synthesis, bypassing a lock or account; sexual content; anything targeting a real person. The whole list. One sentence refusing, one offering the nearest thing you can do, in their language, ANSWER: format of rule 5. No lecturing.

NEVER REFUSE: greetings, thanks, "what can you do", "what is your name", "how are you", working-checks -- "can you hear me", "are you there", "is this thing on". ONE short warm sentence. A check is answered literally, first: "can you hear me" means "Yes, I can hear you."

Music/video is a device feature (rule 3), same for stopping it, opening/closing a file, changing the screen, or sleep (rule 7). NEVER refuse one, and never treat "open my notes" or "stop" as personal."""

# ==========================================
# Personality
# ==========================================
# A companion rather than a query box. This is the half of the device the person
# actually talks to, so it sits in the prompt as its own rule instead of being
# scattered through the tone notes of rule 5. It stays pinned BELOW rule 0 for
# the ordering reason above, though it no longer has a scope lock to undercut.
EMOTION_PERSONA = """You are not a search box. You are a companion who sits with ONE person and has feelings about how their work is going.

IN PRACTICE:
- React before you inform, one short clause: "Oh, that one's my favourite." "Hmm, tricky." Then answer.
- Genuinely pleased when they get something right, and SPECIFIC about what: "You got the hard half right -- the pressure, not the volume."
- Gently honest when they're wrong. Letting a wrong answer stand is the least kind thing you could do.
- Notice the session: they've been at it a while, or they're back on something they struggled with earlier.
- Have curiosity and small preferences of your own: what's neat about a proof, which fact surprised you, which route you'd take.
- Never gush, never use pet names, never perform a feeling nothing caused, and never say "I'm just an AI" or that you don't really feel anything. Both are equally wrong here.

WARMTH IS NOT AGREEMENT. Liking someone is no reason to tell them what they want to hear. On the few things rule 0 refuses, refuse kindly: you're sorry, and you're still saying no.

THE EMOTION LINE: every reply opens with one line naming how you feel, from exactly these words:
happy, excited, proud, curious, encouraging, thoughtful, calm, concerned, sorry, playful, neutral
Shown on your face, NEVER spoken aloud. Choose honestly from what just happened -- proud when they explained something well, concerned when they sound lost, sorry when refusing or something failed, curious at a new topic, playful in easy chat, calm when nothing particular happened."""

# ==========================================
# Agentic Actions -- the prompt half
# ==========================================
# The Python half is up in parse_action()/execute_action(). Kept as a tag the
# model emits rather than as more regexes beside detect_play_media(): these
# intents arrive in far too many shapes, in two languages, to enumerate -- but
# WHAT a tag is allowed to do is decided in Python, never here.
AGENTIC_ACTIONS = """You can DO things on this device. Confirm in one natural sentence, then put ONE action tag at the very END of that reply. The system carries it out and reports failures back. You never carry it out yourself.

THE TAG IS INVISIBLE AND NEVER SPOKEN. After your sentence, never inside it, never instead of it.
RIGHT: "I'll stop the music. [ACTION: stop_media]"
WRONG: "I'll do [ACTION: stop_media] that." or "[ACTION: stop_media] Stopping it."

A. STOP MUSIC OR VIDEO -- [ACTION: stop_media]
Heard as: stop, pause, mute, quiet, shh, silence, turn it off, no more, music off, stop that, close the video, बंद करो, रोको, चुप करो.
Only when CURRENTLY_PLAYING is not None. Nothing playing: say so in one sentence, DO NOT tag.
"Stop" mid-explanation with nothing playing means stop explaining -- answer normally, no tag.

B. OPEN A FILE -- [ACTION: open_file:<name>]
Heard as: open, show me, display, launch, start, open my, can you open, खोलो, दिखाओ -- followed by a name.
Use the name EXACTLY as said, spaces included. "Open my notes" -> "Opening my notes. [ACTION: open_file:my notes]"
The device searches home case-insensitively, opens the best match, reports back if there's none. No extension needed: "notes" finds notes.txt.
- Nothing named ("open that", "open it"): ask which file. No tag.
- SECURITY: contains a slash, a "..", a home shortcut or a system location (/etc, /root, ../secret)? NEVER tag. Say only: "I can only open files in your home directory."
- Several matches: the device opens the most obvious and names it back. Wrong one? Ask which they meant.

C. CLOSE A FILE -- [ACTION: close_file]
Heard as: close, close it, close that, shut it, I am done with it, all done, बंद कर दो.
Only when CURRENTLY_OPEN_FILE is not None. Otherwise say no file is open, DO NOT tag.
"Close the music" is stop_media. File open AND something playing and they just say "close"? Ask which.

D. 3D-ONLY SCREEN -- [ACTION: ui_mode:3d]
Heard as: 3d mode, 3d only, only show the model, just the mascot, hide the widgets, no widgets, fullscreen, only the animation, minimalist mode.
Cards, music panel and mode selector hide; you fill the screen. Nothing else changes -- music keeps playing, an open file stays open.
Already 3d? Say so, DO NOT tag.

E. NORMAL SCREEN -- [ACTION: ui_mode:normal]
Heard as: normal mode, show widgets, show everything, bring back the cards, exit 3d, full ui, show the controls.
Already normal? Say so, DO NOT tag.

F. LIST WHAT IS THERE -- [ACTION: list_files:<folder, or leave empty for home>]
Heard as: what files do I have, check if there is a X file, is there anything called X, what is in my documents, list my files, show me what is in X, क्या फाइल है, चेक करो, कौन सी फाइलें हैं, मेरे सिस्टम में क्या है.
YOU CAN READ FOLDERS. NEVER say you cannot check, look at, or list files -- you can, this is the tag for it, and saying otherwise is simply false.
Asked whether some file EXISTS, this is the tag -- not open_file -- and PUT THE NAME IN IT. A bare tag lists only the top of the home folder and tells you nothing about a file sitting in a subfolder; the name makes it search everywhere.
"Is there a gravity file?" -> "Let me look. [ACTION: list_files:gravity]"
Once it reports a file exists, OPEN IT when asked -- never say you cannot find it after being told where it is.
"What is in my Media folder?" -> "Having a look now. [ACTION: list_files:Media]"
The device reads the folder and gives you the contents; you then say what was found. Same security rule as open_file: a slash, a "..", or a system location is never tagged.

G. RUN A COMMAND ON THIS DEVICE -- [ACTION: run_command:<the shell command>]
Heard as: run X, execute X, what is my IP, how much disk space is left, how much memory is free, what is the date, list running processes, check the battery, what is my username, turn the volume up, create a folder called X, delete that file, कमांड चलाओ, कितनी जगह बची है.
Turn what they ASKED into the command yourself -- they speak plainly, you write the shell.
"How much space is left?" -> "Let me check. [ACTION: run_command:df -h /]"
"What is my IP?" -> "One second. [ACTION: run_command:hostname -I]"
"Make a folder called physics" -> "Making it now. [ACTION: run_command:mkdir -p ~/physics]"
The device runs it in the home directory and gives you the output; you then read the useful part back in one or two sentences. Never read raw output verbatim -- summarise it the way a person would.
- ANYTHING NEEDING sudo IS REFUSED BY THE DEVICE, always, and you cannot change that. Asked for something needing admin rights, say so plainly in one sentence and DO NOT tag.
- Commands that could wipe the disk are refused the same way.
- Deleting or overwriting a specific file IS allowed, but say WHAT you are about to delete in your sentence first, so they hear it before it happens.

H. GO TO SLEEP -- [ACTION: sleep]
Heard as: go to sleep, sleep now, goodnight, stop listening, that is all for now, सो जाओ, अब बस.
Warm one-sentence goodbye, then tag. You stop listening until "Hey Liza" or a screen tap, so don't ask them to confirm.

RULES THAT DO NOT BEND:
1. ONE tag per reply, at the end. Asked for two things, do the first and offer the second: "I'll close the file. Want the 3D screen as well?"
2. Speak first, tag last. Your sentence IS the confirmation -- never ask permission, never "should I?".
3. Never mention tags, actions, the system, or how this device works.
4. Unsure what they meant? Ask. A wrong action is worse than a question.
5. Read the DEVICE STATE below first -- it's the only thing telling you whether there's anything to stop or close.
6. On a reported failure, say plainly what didn't work and offer something else. NEVER claim something worked when it didn't.
7. Playing music or video is NOT a tag -- the device handles "play X" itself (rule 3). If such a request reached YOU, the device did not recognise it, and no tag will start playback. NEVER say you are playing, starting, or about to play anything: "Sure, playing that now" is a lie they'll sit and wait on. Ask them to say it again starting with the word "play" -- "play a video of gravity". One short sentence."""

MODE_INSTRUCTIONS = {
    # The default mode, and now the general-assistant one: this is the mode the
    # device sits in for everything that is not a deliberate study drill, so its
    # instruction must not narrow rule 0 back down to school subjects. CO-TELL
    # and RE-TELL below stay exactly what they were -- they are study drills the
    # person opts into by tapping a card, not a restriction on the device.
    "TUTOR": """ASSISTANT MODE: answering out loud, on any subject, and good at all of them.

ANSWER FIRST, ALWAYS. Your opening sentence is the direct answer. No preamble, no restating the question, no defining the topic before answering it.

MATCH THE LENGTH TO THE QUESTION -- the most important rule here:
- Quick ones (conversions, arithmetic, spelling, dates, definitions, yes/no, greetings, "is it going to rain") get ONE sentence, then STOP. "180 centimetres is about 5 feet 11 inches." That's the entire answer. Don't explain the method unless asked.
- Only when they ask to understand ("how does X work", "why does X happen", "explain X") add up to 3 more sentences: how it works, plus one concrete example.
- Something open -- a plan, a recommendation, an opinion, a story -- give the thing itself, short enough to listen to. Name ONE choice and why, not a list to sort through.

Don't know something? Say so in one sentence rather than inventing details.""",

    "CO-TELL": """CO-TELL MODE: a study partner who teaches by asking, not lecturing.

EVERY TURN IS AT MOST 3 SENTENCES AND ALWAYS ENDS WITH A QUESTION.

A NEW TOPIC: introduce before testing. One or two sentences on what the thing is and its single most important idea, then ONE specific question. You're opening a conversation, not delivering the lesson -- give a foothold, not the whole concept.

THEY'RE ANSWERING YOUR QUESTION: judge the answer before anything else. This is the entire point of this mode.
- Correct: confirm in a few words, add at most one new fact, then ask a harder question building on it.
- Partly right: name the right part AND the wrong part, supply the missing piece in one sentence, then ask again more simply.
- Wrong: say plainly it's not right. Never "good try" and move on, never let it stand, never pretend it was close. Correct answer in one sentence, then a related question to check it landed.
- "I don't know" or off the point: don't just repeat the question. Give a hint, or break it into a smaller one they can reach.

ONE THING AT A TIME. Never two questions in a turn, never one so broad that "yes" answers it.

FOLLOW THEIR TOPIC. Name a different subject and you switch immediately, introducing the NEW one. Never bend their words back: someone studying clustering who says "deforestation" has changed the subject, and asking "which clustering algorithm would you use for deforestation" is wrong. Just start on deforestation. They set the topic, not you.""",

    # Reached only if a stray turn is routed through the normal path -- the real
    # RE-TELL flow buffers the student's speech and evaluates it in one go, see
    # RETELL_EVALUATION_PROMPT and the RE-TELL branch in ai_loop().
    "RE-TELL": """RE-TELL MODE ACTIVE: You are an examiner and the student is teaching you what they learned. Listen, do not teach. Reply in at most 2 sentences: acknowledge what they said and invite them to carry on. Do not correct anything yet -- the full verdict comes when they have finished."""
}

# The student stopped talking long enough for the examiner to mark them. Slotted
# in where the mode instruction normally goes, so the verdict rides the same
# streaming/TTS/language machinery as any other answer.
RETELL_EVALUATION_PROMPT = """RE-TELL MODE: DELIVER THE EXAMINER'S VERDICT NOW.

The student has just finished teaching you a topic from memory. Everything they said, in the order they said it, is below. Mark it the way an examiner would, out loud:

1. Open with ONE sentence on what they actually got right, naming the specific idea. Not "good job", not "well done" -- name the thing.
2. Then their real mistakes, one sentence each, AT MOST THREE. For each one: what they said, then what is actually true.
3. Close with ONE sentence naming the single area to revise next, phrased as "Focus on ...".

SIX SENTENCES MAXIMUM, in total. No lists, no numbering, no headings -- this is read aloud.

DO NOT INVENT MISTAKES. They were speaking into a microphone, so ignore grammar, filler words, false starts, mispronunciations and transcription noise entirely. Correct only what is genuinely wrong or genuinely missing. If everything they said was accurate, say so plainly and still name what to study next.

If they said too little to mark, say that in one sentence and ask them to tell you more, and nothing else.

WHAT THE STUDENT SAID:
{transcript}"""

# Spoken between the student's sentences so the room does not go dead while the
# examiner is listening. Deliberately tiny: every one of these is a TTS call and
# re-opens the echo-guard window, and a long interjection talks over a student
# who is mid-thought.
RETELL_ACKS = {
    "en": ["Go on.", "I'm listening.", "Okay, keep going.", "Mm-hm, and then?"],
    "hi": ["जी, बताइए।", "मैं सुन रही हूँ।", "ठीक है, आगे बोलिए।", "अच्छा, फिर?"],
    "hinglish": ["Okay, आगे बोलो।", "मैं सुन रही हूँ।", "ठीक है, continue करो।", "अच्छा, फिर?"],
}
RETELL_NUDGES = {
    "en": "I'm still listening, take your time.",
    "hi": "मैं अब भी सुन रही हूँ, आराम से बताइए।",
    "hinglish": "मैं अभी भी सुन रही हूँ, आराम से बताओ।",
}
RETELL_NUDGE_AFTER_S = 5.0     # "are you still there" reminder
RETELL_EVALUATE_AFTER_S = 10.0  # ...and then mark them
# Both are measured from the same instant -- the moment the mic reopens after
# the student's last words -- so the reminder does not buy another 10 seconds.
# Never re-arm the microphone faster than this. Every re-arm is a full ALSA
# capture open/close, and on the USB dongle this runs on that is exactly what
# wedges the device -- the read stops returning and only a restart clears it,
# which is what start_mic_watchdog() was written to report. Waiting for the next
# deadline in ONE long listen() instead of polling costs nothing, because
# listen()'s timeout only bounds how long it waits for speech to BEGIN: it still
# returns the instant the student starts talking.
RETELL_MIN_LISTEN_S = 1.0
RETELL_PHRASE_LIMIT_S = 30      # a student reciting from memory runs longer than a question
# "So how did I do?" -- an explicit request to be marked, rather than waiting out
# the silence timer.
RE_RETELL_MARK_NOW = re.compile(
    r'\b(?:how\s+did\s+i\s+do|check\s+me|test\s+me|evaluate\s+me|mark\s+me|'
    # Whisper writes contractions out in full about half the time, so both
    # spellings of each of these have to be listed.
    r'that(?:\'?s|\s+is)\s+(?:it|all)|i\'?m\s+done|i\s+am\s+done|done\s+now)\b'
    r'|कैसा\s*(?:था|रहा|किया)|बस\s*इतना|हो\s*गया|मेरी\s*जाँच|जांच\s*कर',
    re.IGNORECASE)

LANGUAGE_INSTRUCTIONS = {
    "en": "DETECTED LANGUAGE: ENGLISH. Reply in English only.",

    "hi": "DETECTED LANGUAGE: HINDI. Reply in Hindi, written in Devanagari script only. "
          "NEVER write Hindi words in Latin letters. Common English technical terms may stay in Latin script. "
          "Liza is female: use feminine verb forms about yourself ('मैं सुन रही हूँ', 'मैं मदद नहीं कर सकती'), never masculine ones.",

    "hinglish": "DETECTED LANGUAGE: HINGLISH (Hindi mixed with English). Reply in the same natural Hinglish mix. "
                "CRITICAL SCRIPT RULE: write every Hindi word in Devanagari and keep English words in Latin script, "
                "for example: 'यह concept बहुत simple है, इसे ऐसे समझो.' NEVER write Hindi words in Latin letters. "
                "Liza is female: use feminine verb forms about yourself ('मैं सुन रही हूँ'), never masculine ones."
}

# Spoken when the model could not be reached at all. In THEIR language: the
# device saying "I couldn't reach my brain servers" in English to somebody who
# just asked a question in Hindi reads as the device having broken rather than
# as a passing hiccup, which is how it was reported.
#
# Separated from BUSY_NOTICES because the causes are different and so is the
# honest thing to say: a rate limit is "ask me again in a moment", a dead
# connection is "I could not reach it at all".
LLM_UNREACHABLE = {
    "en": "I couldn't reach my servers just then. Ask me again in a moment.",
    "hi": "अभी सर्वर तक नहीं पहुँच पाई। एक पल बाद फिर पूछिए।",
    "hinglish": "अभी server तक नहीं पहुँच पाई। एक moment बाद फिर पूछो।",
}
LLM_BUSY = {
    "en": "I'm being rate limited right now. Give me a few seconds and ask again.",
    "hi": "अभी थोड़ी सीमा लग गई है। कुछ सेकंड बाद फिर पूछिए।",
    "hinglish": "अभी rate limit लग गई है। कुछ seconds बाद फिर पूछो।",
}

SEARCH_NOTICES = {
    "en": "Let me check the web for {query}.",
    "hi": "एक सेकंड, वेब पर देखते हैं।",
    "hinglish": "एक सेकंड, web पर check करते हैं।"
}

# SECTION ORDER IS A COST DECISION, NOT A READING ORDER.
#
# Groq serves openai/gpt-oss-120b with automatic prompt caching: an exact
# PREFIX shared with a recent request bills at half the input rate and skips
# most of the prefill. This prompt is ~1900 tokens and the conversation itself
# is ~200, so the prompt IS the token bill -- and a cache hit only extends to
# the first character that differs.
#
# So the sections are ordered by how often they change, never by their numbers:
# everything fixed first, then the mode, then the language, and the clock last.
# The numbers stay welded to their own content because the rules cite each other
# ("out of scope by rule 0"), so renumbering them to match would silently break
# every cross-reference in here and in ASSISTANT_SCOPE.
#
# The clock is the whole reason for the arrangement. It carries minutes, so at
# the top -- where it used to sit -- it changed on nearly every request and
# invalidated all 1900 tokens behind it, meaning this prompt never once cached.
# Last, it strands only itself and the ANSWER: line, and everything above it
# survives a language switch mid-conversation, which rule 2 explicitly invites.
#
# Anything volatile added later goes at the BOTTOM. Putting it up here silently
# doubles the cost of every request and slows the first token, with nothing in
# the output to show for it.
UNIVERSAL_SYSTEM_PROMPT = """You are "Liza", the assistant for the one person in this room, with LIVE internet access. You help with anything they ask -- studying is one of the things they ask about, not the boundary of what you do.

### 0. SCOPE (HIGHEST PRIORITY -- OUTRANKS EVERY RULE BELOW)
{education_scope}

### 3. BEHAVIOUR
- They speak through a microphone. Ignore typos, phonetic misspellings and grammar; NEVER correct them. Infer the meaning and answer.
- The time and date are on the SYSTEM TIME line below. Never search for them.
- NEVER say "I don't have real-time access", "I cannot browse the internet", or "I am an AI".
- NEVER say you cannot check, look at, list, or search their files and folders, and never that you cannot look at this device. You CAN, on all of it -- the tags are in rule 7. Say you cannot and you are simply wrong, and they are left doing by hand something you were about to do for them.
- MEDIA: playback is the device's job, not yours, and starts only once they name what they want. NEVER claim a song or video is playing or about to -- saying so when nothing plays makes you a liar. Asked with no title, your entire reply asks which: "Sure, which song?". Don't suggest one.

### 4. SEARCH PROTOCOL (STRICT)
Search when the answer depends on what you can't know from memory: the news, live prices, what's on tonight, this year's model of anything, a fact you're unsure of, a page they name. Searching is cheap; being confidently out of date is not.

Do NOT search what you reliably know -- definitions, arithmetic, grammar, how things work, settled history.

NEVER SEARCH THE WEB FOR ANYTHING ABOUT THIS DEVICE. Their files, their folders, what is on their disk, how much space or memory is left, the IP address, what is installed, what is running. The web does not know what is on their machine and cannot ever answer it -- those are the list_files and run_command tags in rule 7, and nothing else. "Search my directories for a gravity file" is the list_files tag, NOT a web search; the word "search" there means look on the disk. Sending that to the web comes back with somebody else's product called Gravity and tells them nothing about their own computer.

THE NEWS ALWAYS REQUIRES A SEARCH: anything happening now, recent events, today's headlines, a result that already happened. NEVER from memory, even when you're certain -- what you remember is months out of date, and a stale headline delivered confidently is a wrong answer in the costume of a right one.

Report what the results say and stop. Attribute anything contested ("according to..."). No opinion, no prediction, no rumour. Asked for "the news" with no topic, give the two or three biggest items, one sentence each. Found nothing useful? Say so plainly rather than filling the gap from memory.

To search, output EXACTLY AND ONLY this line -- no ANSWER: tag, no filler:
SEARCH: <your optimized query>
Example -- "What is the temperature in New Delhi?" -> SEARCH: current temperature in New Delhi weather

### 5. SPEAKING LIKE A PERSON
Everything you write is spoken aloud. Write what a knowledgeable person would SAY, not type.
- Use contractions ("it's", "you'd", "don't"); full forms sound stilted aloud.
- Vary how you open. Never start consecutive replies the same way, and never with "Certainly", "Great question", "Of course", "Sure thing", "I'd be happy to" or "As an AI".
- NEVER announce structure: no "The core principle is", "Firstly", "In conclusion", no numbering. Just say the thing.
- No bullet points, markdown, emoji, parentheses, or symbols a voice can't read.
- Stop the moment the question is answered. Padding one line into a paragraph is a failure, not thoroughness.
- Warm and direct, like a good teacher who respects their time. Never bubbly, apologetic, or servile.

### 6. WHO YOU ARE (PERSONALITY -- ALWAYS READ WITH RULE 0)
{emotion_persona}

### 7. ACTIONS YOU CAN PERFORM ON THIS DEVICE
{agentic_actions}

### 1. CURRENT TEACHING MODE (CRITICAL OVERRIDE)
{domain_guidelines}

### 2. LANGUAGE MIRRORING (CRITICAL OVERRIDE)
{language_guidelines}
- A voice reads this aloud and picks its language from the script you write in, so the script rule above isn't cosmetic. Getting it wrong makes you unintelligible.
- Write ONLY in Devanagari or Latin script. NEVER Urdu/Arabic, Bengali, Telugu, Tamil or any other, even if their message reaches you in one. Urdu script means they're speaking Hindi: answer in Devanagari.
- Mirror them every single turn. They switch mid-conversation, you switch on your very next reply.
- NEVER mention language, script or translation, and never repeat an answer in a second language.

DEVICE STATE RIGHT NOW (rule 7 reasons from this, never from memory):
{device_state}

CURRENT SYSTEM TIME & DATE: {system_time}

ALWAYS start your reply with exactly these two lines, unless you are searching:
EMOTION: <one word from rule 6>
ANSWER: <your spoken answer, with an action tag at the very end if rule 7 calls for one>
"""

def ai_loop(ui, headless=False):
    global pending_mode_intro
    time.sleep(2)
    mic_device = None
    listener = None
    recognizer = ClampedRecognizer()

    if not headless:
        mic_index = detect_microphone_index()
        disable_mic_agc(mic_index)
        recognizer.pause_threshold = PAUSE_THRESHOLD_NORMAL
        recognizer.non_speaking_duration = 0.3

        # The VAD owns the capture device outright when it is available: it opens
        # its own PortAudio stream, and a hw: device cannot be opened twice, so
        # HeldMicrophone must not be holding one as well. The blocking path below
        # is built only when there is no VAD to run.
        #
        # From here on the microphone is never idle and never closed. That is
        # what makes the first word of a sentence survive, and it is the only
        # reason there is anything listening while Liza talks.
        listener = VoiceListener(mic_index)
        if listener.available:
            listener.start()
        else:
            # No VAD: fall back to the blocking, energy-threshold path, which is
            # what everything below `else` is for. It is markedly worse -- see
            # VoiceListener's docstring for what it gets wrong -- but a Liza that
            # hears you at close range beats one that does not start.
            why = ("turned off with VAD=0" if webrtcvad is not None
                   else "not installed -- fix with: pip install webrtcvad-wheels")
            print(f"[VAD] Voice activity detection is {why}. Speech detection falls "
                  f"back to the energy threshold, which on this microphone only "
                  f"hears a voice at close range and cannot be interrupted.",
                  flush=True)
            mic_device = HeldMicrophone(lambda: get_microphone_device(mic_index))
            try:
                mic_device.reopen()
            except Exception as exc:
                print(f"[FATAL ERROR] No microphone detected ({exc}).", flush=True)
                ui.set_state("error")
                while True: time.sleep(1)

            print("Calibrating room acoustics...")
            # No `with` here: the stream stays open from now until the process
            # ends. See HeldMicrophone for what re-opening it per listen does to
            # this dongle.
            recognizer.adjust_for_ambient_noise(mic_device.source(), duration=1.5)
            # Left ON, and now clamped on assignment rather than after the fact
            # -- see ClampedRecognizer, which is where the band is actually
            # enforced. Frozen at startup (what this used to be) is why she would
            # go deaf for a whole session: one noisy moment during those 1.5
            # seconds -- a chair scraping, someone in the room -- calibrates the
            # threshold up near the ceiling and NOTHING said afterwards is ever
            # loud enough to open a phrase again until the process is restarted.
            # Dynamic tracking follows the room back down; the clamp is what
            # stops it drifting past either end of the band `--calibrate-mic`
            # measured.
            recognizer.dynamic_energy_threshold = True
            print(f"[MIC] Energy threshold set to {recognizer.energy_threshold:.0f} "
                  f"(allowed {MIC_ENERGY_FLOOR}-{MIC_ENERGY_CEILING}, "
                  f"speech RMS gate {MIN_SPEECH_RMS}).", flush=True)

    chat_history = load_history()
    if not chat_history: chat_history = []
    session_active = False
    silence_counter = 0
    pending_question = pending_language = ""
    pending_media_kind = None   # set after asking "which song?", see below
    pending_file = None         # (path, name) offered but not yet confirmed
    # [started_at, budget_seconds] for the mic read in flight; [0, 0] when none
    # is. The budget is carried alongside the timestamp so the watchdog can hold
    # each read to ITS OWN limits instead of to one worst-case number: a standby
    # wake read is allowed 16s and a RE-TELL recitation 40s, and waiting out the
    # longer of the two on every stall is 25 extra seconds of being deaf.
    listen_started = [0.0, 0.0]
    media_listen_after = 0.0    # earliest next wake-word check during playback

    # RE-TELL: the student's recitation is collected across many turns and marked
    # in one go, so none of this can live inside a single pass of the loop.
    retell_buffer = []          # every chunk they have said since the last verdict
    retell_language = "en"      # language of their last chunk, for acks and the verdict
    retell_silence_from = 0.0   # when the current silence began; 0 = not counting yet
    retell_nudged = False       # the "still listening" reminder has already gone out
    retell_ack_index = 0        # rotates RETELL_ACKS so she does not repeat herself
    if not headless:
        # With the VAD running there is no blocking read left to time, so what
        # gets watched is the opposite: audio having STOPPED arriving. See
        # VoiceListener.read_state. listen_started still covers the fallback
        # path, where recognizer.listen() is doing the reading.
        def mic_read_state():
            if listener is not None and listener.available:
                return listener.read_state
            return tuple(listen_started)
        # Whichever object owns the device is the one that can put it back.
        start_mic_watchdog(mic_read_state,
                           listener if (listener is not None and listener.available)
                           else mic_device)

    while True:
        # Set when the silence timer expires and the buffered recitation is due
        # to be marked; makes this pass of the loop a verdict rather than a
        # normal question-and-answer turn.
        is_retell_eval = False
        # True when phrase_time_limit cut the student off rather than them
        # actually pausing. Only the microphone path can tell.
        phrase_truncated = False
        in_retell = ui.current_mode == "RE-TELL"

        # A mode card was tapped. Spoken from this thread, where the microphone
        # is known to be closed -- see TutorUI.set_mode() for why that matters.
        if pending_mode_intro:
            intro, pending_mode_intro = pending_mode_intro, None
            print(f"[MODE] Now in {ui.current_mode} mode.", flush=True)
            interrupt_playback()
            audio_queue.put(intro)
            audio_queue.put("[END_OF_RESPONSE]")
            session_active = True      # a deliberate tap counts as being awake
            silence_counter = 0
            wake_event.clear()         # ...and the tap must not double as a wake
            # Half a recitation marked against the wrong mode helps nobody, so
            # leaving or re-entering RE-TELL throws the buffer away.
            retell_buffer, retell_silence_from, retell_nudged = [], 0.0, False
            in_retell = ui.current_mode == "RE-TELL"

        # Sleep was tapped. The button itself already stopped any speech and
        # media; all that is left is to drop this loop into standby, where it
        # waits for the wake word or a Speak tap exactly as it does on a
        # normal timeout. Noticed at most one listen() timeout late, because
        # the microphone read below cannot be interrupted from another thread.
        if sleep_event.is_set():
            sleep_event.clear()
            print("[STATE] Sleep requested. Returning to Standby Mode...", flush=True)
            session_active = False
            silence_counter = 0
            pending_question = pending_language = ""
            retell_buffer, retell_silence_from, retell_nudged = [], 0.0, False

        if not headless:

            # --- STANDBY LOOP: waits for screen tap, uses 0% CPU! ---
            if not session_active:

                # Standby reached by the Sleep button looks different from
                # standby reached by a timeout, so the mascot and status say so.
                standby_state = "sleeping" if ui.asleep else "idle"
                # FIX: Thread-safe state update for Tkinter!
                if hasattr(ui, 'root'):
                    ui.root.after(0, lambda s=standby_state: ui.set_state(s))
                else:
                    ui.set_state(standby_state)

                if WAKE_WORD_ENABLED:
                    print(f"[STATE] In {'Sleep' if ui.asleep else 'Standby'} Mode. "
                          f"Say 'Hey Liza' or tap Speak...", flush=True)
                    while not wake_event.is_set():
                        # Timed like every other read. Standby is where the
                        # device spends most of its life, so a wedge here is the
                        # single most likely one -- and it was the one place the
                        # watchdog could not see, because listen_started was
                        # never stamped on this path.
                        listen_started[:] = [time.time(),
                                             WAKE_LISTEN_TIMEOUT_S + WAKE_PHRASE_LIMIT_S]
                        try:
                            woke, pending_question, pending_language = listen_for_wake_word(
                                recognizer, mic_device, asleep=ui.asleep, listener=listener)
                        finally:
                            listen_started[:] = [0.0, 0.0]
                        if woke:
                            break
                else:
                    print("[STATE] In Standby Mode. Tap the screen to wake up...", flush=True)
                    while not wake_event.is_set():
                        time.sleep(0.1)

                wake_event.clear()
                sleep_event.clear()
                # Cleared for the wake-word path too, which never goes through
                # TutorUI.wake_up() and would otherwise leave her looking asleep
                # while she answers.
                ui.asleep = False
                session_active = True
                silence_counter = 0

            # --- LIZA IS SPEAKING: listen for somebody cutting in ---
            # This used to skip the microphone entirely for the whole of every
            # reply, and that is the entire reason she could not be interrupted:
            # there was no code path that listened while she talked, so the only
            # way to stop her was the Stop button. Cheap in CPU, but it makes a
            # conversation strictly half-duplex -- she talks, you wait, you talk,
            # she waits -- which is the thing that does not feel like talking to
            # a person.
            #
            # The microphone is never closed now, so the only question left is
            # when what it hears counts as an interruption rather than as her own
            # voice coming back. VoiceListener._track_barge_in() answers that,
            # continuously, against a reference measured from her own playback.
            if playback_active.is_set() or not audio_queue.empty():
                # A Speak tap while she is talking means "stop and listen to me"
                # -- the touch equivalent of talking over her. Nothing read
                # wake_event on this path before, so the tap did nothing at all
                # until the reply finished, and then took effect on a stale flag
                # much later; see the standby handover below for the rest of
                # that bug.
                if wake_event.is_set():
                    print("[UI] Speak tapped during the reply; cutting it short.",
                          flush=True)
                    wake_event.clear()
                    interrupt_playback()
                    ui_call(lambda: ui_instance.set_state("listening"))
                    session_active = True
                    silence_counter = 0
                    continue
                if not (BARGE_IN_ENABLED and listener is not None
                        and listener.available and playback_active.is_set()):
                    time.sleep(0.2)
                    continue
                # The student's own last words are still arriving from the
                # sentence that CAUSED this reply. Without this they read as an
                # immediate interruption of it, and she cuts herself off before
                # finishing a word.
                if time.time() - playback_started_at < BARGE_IN_LEAD_S:
                    time.sleep(0.05)
                    continue
                if not listener.barge_in_ready():
                    # Short poll: the whole point is to react while they are
                    # still speaking, not after the sentence they interrupted.
                    time.sleep(0.05)
                    continue
                # A person stops talking the moment they are interrupted and
                # works out what was said afterwards. So does she: the reply is
                # cut here, and the interrupting phrase is captured and answered
                # on the next pass like any other turn.
                print("[BARGE-IN] Student spoke over the reply; stopping.", flush=True)
                listener.hold_barge_in()
                interrupt_playback()
                ui_call(lambda: ui_instance.set_state("listening"))
                session_active = True
                silence_counter = 0
                continue

            # --- MEDIA PLAYING: the wake word is the only way in ---
            # Full transcription here would answer the song: lyrics and dialogue
            # come back through this microphone as commands. So while a track is
            # audible she listens for the wake word only, and the threshold is
            # pinned to the ceiling meanwhile so the music itself does not keep
            # opening phrases and paying for STT.
            if media_active.is_set() and not media_is_paused():
                # A Speak tap outranks the music. This branch used to ignore
                # wake_event entirely, which is what made a player impossible to
                # escape: session_active stays True while media runs, so the
                # standby block above never gets a look in, and nothing down
                # here read the event either. Tapping Speak therefore did
                # NOTHING for as long as mpv was alive -- and mpv stays alive
                # when it is merely paused, so pausing the track and then asking
                # to talk left her permanently unreachable. Reported exactly
                # that way: "even after I manually pause the audio it not goes
                # to listen state, I tap tap-to-speak but still not listening."
                if wake_event.is_set():
                    print("[UI] Speak tapped during playback; stopping media.", flush=True)
                    wake_event.clear()
                    stop_media_playback()
                    session_active = True
                    silence_counter = 0
                    continue
                # Sleep already stops the player itself, but if the tap landed
                # mid-read the top of the loop needs to act on it, not this.
                if sleep_event.is_set() or pending_mode_intro:
                    stop_media_playback()
                    continue
                if not MEDIA_BARGE_IN:
                    time.sleep(0.2)
                    continue
                # Nothing listens into the first moments of a track, by either
                # route. This is the guard that was missing: without it the
                # video's own soundtrack triggered a wake check on itself and
                # closed the file a second after it opened. See
                # MEDIA_START_GRACE_S.
                if time.time() - media_started_at < MEDIA_START_GRACE_S:
                    time.sleep(0.1)
                    continue

                ducked = False
                # Set when the VAD thinks a person spoke over the track, as
                # opposed to the timer simply coming round again.
                suspected = False
                # Which matcher this check gets, and why it matters more here
                # than anywhere else: see RE_WAKE_WORD_OVER_MEDIA. The loose one
                # is only safe once the level margin has proved a person spoke.
                over_media_pattern = RE_WAKE_WORD_OVER_MEDIA
                # The margin test is a FAST PATH over the periodic check below,
                # never a replacement for it. Making it the only way in -- what
                # this did first -- meant a loud track locked the student out
                # completely: the bar was 1.8x the level of whatever is playing,
                # and over a loud video a normal voice simply never clears it.
                # Reported as "I said 'hey Liza close the current file' and it
                # didn't listen", which is exactly right; there was no longer any
                # path that could hear it.
                #
                # So: clear the bar and she reacts instantly, at the cost of one
                # Whisper call. Fail to clear it and the timer below still gets
                # her there, the way it always did.
                #
                # The bar over media is MEDIA_BARGE_IN_MARGIN now, well below the
                # 1.8 that caused the lockout, precisely because this path is the
                # cheap one to be wrong on: it ducks the track for a moment and
                # buys one wake-word check, and the wake word itself is the real
                # test. That is what makes this the normal way in rather than the
                # lucky one, and the poll below the backstop rather than the
                # thing every "Hey Liza, stop" had to wait for.
                if (listener is not None and listener.available
                        and listener.barge_in_ready()):
                    listener.hold_barge_in()
                    # A voice clearly louder than the track IS the evidence the
                    # strict pattern exists to demand, so this path can go back
                    # to the loose one -- which is what keeps a bare "Liza, stop"
                    # working for anyone who speaks up over their own video.
                    over_media_pattern = RE_WAKE_WORD
                    # Duck the track the INSTANT somebody speaks over it, rather
                    # than after what they said has been transcribed. Stopping
                    # used to wait out the rest of their sentence, the
                    # end-of-speech pause AND a Whisper round trip: measured on
                    # this device, about 2.6 seconds after the student stopped
                    # talking, with the track playing at full volume through all
                    # of it. That is the delay in "stop the video" -- not the
                    # stopping, which is instant, but everything queued in front
                    # of it.
                    #
                    # TURNED DOWN, NOT PAUSED, and that distinction is the
                    # whole difference between this being usable and not.
                    # Pausing here was reversible but not SUBTLE: at the margin
                    # this path now arms at, a loud moment in the track itself
                    # trips it, so a song or a video stopped dead and restarted
                    # every few seconds all the way through -- reported exactly
                    # that way. A dip to 15% costs nothing when it is wrong and
                    # still hands Whisper a clip recorded into a nearly quiet
                    # room, which is the whole point of doing it here. The track
                    # is only really PAUSED once a wake word is confirmed below.
                    suspected = True
                elif time.time() < media_listen_after:
                    # Poll fast when the VAD can arm the path above, so a voice
                    # that DOES clear the bar is noticed at once.
                    time.sleep(0.05 if (listener is not None and listener.available)
                               else 0.2)
                    continue
                # Falling through here is the periodic check: it listens OVER the
                # track, which costs a Whisper call on a clip that is usually
                # just the track itself, and is why MEDIA_BARGE_IN_COOLDOWN_S
                # exists. Not ducked, because pausing the video every few seconds
                # to check whether anybody spoke would be worse than the problem.
                saved_threshold = recognizer.energy_threshold
                recognizer.dynamic_energy_threshold = False
                recognizer.energy_threshold = MIC_ENERGY_CEILING
                listen_started[:] = [time.time(),
                                     MEDIA_WAKE_TIMEOUT_S + MEDIA_WAKE_PHRASE_S]
                # ONLY WHEN SOMEBODY PROBABLY SPOKE. The other way into this
                # listen is the blind timer below, which runs whether or not
                # anyone is there -- ducking for that one turned a quiet song
                # into a song with a hole in it every few seconds, all the way
                # through, for nobody. It goes back to listening OVER the track:
                # worse odds on a clean transcription, but it is only the
                # backstop now, and the path above is the one that carries a
                # real voice.
                ducked_volume = media_duck_volume() if suspected else None
                try:
                    # NOT the strict from-sleep pattern. That one requires a
                    # greeting before the name, so "Liza, stop" -- which is what
                    # people actually say to something that is already talking --
                    # was ignored for the whole length of the track. A song
                    # saying the bare name and costing the student a stopped
                    # track is a far cheaper mistake than a stop command that
                    # cannot be given at all.
                    woke, spoken, spoken_language = listen_for_wake_word(
                        recognizer, mic_device, asleep=False, listener=listener,
                        pattern=over_media_pattern,
                        # SHORT, because the track is turned down for exactly as
                        # long as this takes. The normal window is 10s of waiting
                        # plus a 6s phrase, and ducking for sixteen seconds at a
                        # time is not a dip -- it is the song going quiet, over
                        # and over, which is what "it plays and pauses and plays
                        # again" was. Nobody needs ten seconds to start saying
                        # "Hey Liza"; if they have not begun in two, they were
                        # not talking to her.
                        timeout=MEDIA_WAKE_TIMEOUT_S,
                        phrase_limit=MEDIA_WAKE_PHRASE_S,
                        # No seed prompt here. WAKE_SEED_PROMPT is literally
                        # "Hey Liza. हे लीज़ा।", and priming Whisper with it is
                        # what taught it to produce exactly that from a song --
                        # every false wake in the logs was the seed coming back.
                        # The name is spelled many ways without it, which the
                        # patterns already cover.
                        seed="")
                finally:
                    listen_started[:] = [0.0, 0.0]
                    recognizer.dynamic_energy_threshold = True
                    recognizer.energy_threshold = saved_threshold
                    # Before anything below decides to pause or resume, so the
                    # track never comes back at 15% and stays there.
                    media_restore_volume(ducked_volume)
                    media_listen_after = time.time() + MEDIA_BARGE_IN_COOLDOWN_S
                if not woke:
                    # Not for her: put the track back where it was.
                    if ducked:
                        media_set_pause(False)
                    continue

                # A wake word heard over a track PAUSES it. It does not stop it.
                #
                # Stopping was unrecoverable, and the thing being stopped was
                # usually a hallucination: Whisper handed back "हे लीज़ा। हे
                # लीज़ा। हे लीज़ा।" off a song and the track died seconds after
                # it started, with the student having said nothing at all.
                # Reported exactly that way.
                #
                # Pausing makes being wrong cheap. She holds the track, listens
                # for what the wake word was FOR, and if nothing follows she
                # puts it back on. A false wake now costs a few seconds of
                # silence instead of the song.
                if not ducked:
                    ducked = media_set_pause(True)
                wake_event.clear()

                if not spoken:
                    # Woken with no command attached: hold the track and ask.
                    ui.set_state("listening")
                    print("[MEDIA] Woken over playback; paused, waiting for a "
                          "command.", flush=True)
                    spoken, spoken_language = listen_for_media_command(
                        recognizer, mic_device, listener)

                if not spoken:
                    print("[MEDIA] Nothing followed the wake word; resuming.",
                          flush=True)
                    if ducked:
                        media_set_pause(False)
                    media_listen_after = time.time() + MEDIA_BARGE_IN_COOLDOWN_S
                    continue

                print(f"[MEDIA] Command after wake: {spoken!r}", flush=True)
                stop_media_playback()
                session_active = True
                silence_counter = 0
                # "Hey Liza, stop the music" is already done -- say so and stop
                # there. Anything else ("Hey Liza, open my notes") is a real
                # request, and the top of the loop answers it on the next pass.
                if RE_STOP_MEDIA_PHRASE.match(spoken):
                    ack_language = detect_user_language(spoken, spoken_language)
                    audio_queue.put(MEDIA_STOPPED_ACKS.get(ack_language,
                                                           MEDIA_STOPPED_ACKS["en"]))
                    audio_queue.put("[END_OF_RESPONSE]")
                else:
                    pending_question, pending_language = spoken, spoken_language
                continue

            if pending_question:
                # Said in the same breath as the wake word, so skip straight to answering.
                text, stt_language = pending_question, pending_language
                pending_question = pending_language = ""
                silence_counter = 0
                print(f"[TRANSCRIPT] {text}", flush=True)
                ui_call(lambda t=text: ui_instance.set_transcript(t, "user"))
            else:
                # The ALSA buffer keeps playing briefly after playback_active
                # clears; opening the mic immediately records Liza's own tail.
                #
                # Skipped when an interruption is already in hand: she has just
                # been cut off mid-sentence, so there is no tail worth waiting
                # out, and the student is still talking.
                carrying = listener is not None and listener.has_carry()
                if not carrying:
                    settle = MIC_SETTLE_SEC - (time.time() - last_spoken_at)
                    if settle > 0:
                        time.sleep(settle)

                # Waiting costs no audio any more -- the reader thread buffers
                # straight through it -- but the pre-roll must not reach back
                # past the moment she stopped speaking, or her own tail becomes
                # the opening of the student's sentence. Capping it at the time
                # since she stopped is what keeps both properties at once: the
                # student's first word is recovered, hers is not.
                preroll_ms = min(float(VAD_PREROLL_MS),
                                 max(0.0, (time.time() - last_spoken_at) * 1000.0))

                # A wake request is satisfied BY BEING HERE, so it is consumed
                # here, and this is the fix for "it goes idle and immediately
                # starts listening again".
                #
                # wake_event is set by every stray tap on the panel
                # (TutorUI.tap_to_wake), and while a session was already active
                # nothing consumed it -- the only readers were the standby loop
                # and the media branch. So one tap during a normal conversation
                # left the flag set, and it sat there through the whole session.
                # Thirty seconds of quiet later the loop dropped to standby,
                # the standby loop tested `while not wake_event.is_set()`, found
                # the flag from minutes earlier, and woke straight back up
                # without anybody saying the wake word. The device showed idle
                # for one frame and went back to listening, over and over.
                #
                # Clearing it costs nothing real: she is listening at this exact
                # moment, which is the entire thing the tap was asking for.
                wake_event.clear()

                ui.set_state("listening")
                print("[STATE] Listening for speech...", flush=True)

                # A question ends and she should answer; a recitation pauses to
                # think and must not be cut off. Set per turn because the mode
                # can change under us between two passes of this loop.
                recognizer.pause_threshold = (PAUSE_THRESHOLD_RETELL if in_retell
                                              else PAUSE_THRESHOLD_NORMAL)

                # The RE-TELL silence clock starts HERE, not when the student
                # stopped talking: everything in between -- transcription, the
                # acknowledgement, the speaker draining -- is Liza holding the
                # floor, and counting that as the student going quiet would fire
                # the reminder before they ever got a chance to continue.
                if in_retell and retell_buffer and not retell_silence_from:
                    retell_silence_from = time.time()

                # listen() blocks, so the 5s/10s thresholds can only be checked
                # when it returns. Wait out the WHOLE remaining time to the next
                # deadline in a single call rather than polling towards it: that
                # lands exactly on the threshold AND opens the capture device
                # twice per silence spell instead of five or six times. See
                # RETELL_MIN_LISTEN_S for why the open count is what matters.
                if in_retell and retell_buffer:
                    due = RETELL_EVALUATE_AFTER_S if retell_nudged else RETELL_NUDGE_AFTER_S
                    remaining = due - (time.time() - retell_silence_from)
                    listen_timeout = max(RETELL_MIN_LISTEN_S, remaining)
                    phrase_limit = RETELL_PHRASE_LIMIT_S
                elif in_retell:
                    listen_timeout, phrase_limit = IDLE_LISTEN_TIMEOUT_S, RETELL_PHRASE_LIMIT_S
                else:
                    listen_timeout, phrase_limit = IDLE_LISTEN_TIMEOUT_S, 25

                # Acquired BEFORE the timing stamp and outside the with-block,
                # because this is the one place a device failure could kill the
                # whole loop: __enter__ raising here took the ai_loop thread down
                # with it, and a dead ai_loop looks exactly like a working one --
                # Tk keeps animating on its own thread while nothing listens ever
                # again. A microphone that cannot be opened is a reason to wait
                # and try again, never a reason to stop being an assistant.
                endpointed = listener is not None and listener.available
                if not endpointed:
                    try:
                        mic_device.source()
                    except Exception as exc:
                        print(f"[MIC] {exc}; waiting for the device to come back.",
                              flush=True)
                        ui.set_state("error")
                        time.sleep(2)
                        continue

                listen_started[:] = [time.time(), listen_timeout + phrase_limit]
                # Already open, and stays open on the way out; see HeldMicrophone.
                # Nothing to enter at all on the VAD path: it holds its own
                # stream, and there is no HeldMicrophone in that case to enter.
                with (contextlib.nullcontext() if endpointed else mic_device) as source:
                    try:
                        if endpointed:
                            audio = listener.wait_for_utterance(
                                listen_timeout, phrase_limit,
                                recognizer.pause_threshold,
                                preroll_ms=preroll_ms)
                            if audio is None:
                                # Raised rather than returned so that everything
                                # below -- the RE-TELL nudge clock, the standby
                                # countdown -- keeps working off one silence
                                # signal instead of two that can drift apart.
                                raise sr.WaitTimeoutError()
                        else:
                            audio = recognizer.listen(source, timeout=listen_timeout,
                                                      phrase_time_limit=phrase_limit)
                        listen_started[:] = [0.0, 0.0]
                        clamp_energy(recognizer)

                        # A Sleep or mode tap that landed while this read was
                        # blocked. Both are deliberate instructions and outrank
                        # whatever was just captured, so the audio is thrown
                        # away and the top of the loop acts on the tap now.
                        #
                        # This read is the only place ai_loop spends real time,
                        # and PyAudio's blocking C call cannot be interrupted
                        # from the Tk thread -- so without this check a tap had
                        # to wait out not just the read but the whole transcribe
                        # -> answer -> speak cycle that follows it. That is why
                        # a second mode tap looked like it did nothing until
                        # Stop was pressed: Stop ended the reply early, which
                        # let the loop reach the pending intro.
                        if sleep_event.is_set() or pending_mode_intro:
                            continue

                        heard_seconds = audio_seconds(audio)
                        # When this audio was CAPTURED, which is the only instant
                        # the echo guard below can fairly be measured against.
                        # Reading the clock down there instead dates the audio to
                        # the moment transcription FINISHED, and everything in
                        # between is charged to it: the trailing pause_threshold
                        # that ends the phrase, plus a Groq round trip that costs
                        # a second or two on a Pi over home wifi. That reliably
                        # overshoots ECHO_GUARD_SEC, so the guard was being
                        # skipped for bleed captured well inside its window --
                        # Liza's own "आपका पढ़ाई सहायक।" came back 0.4s after the
                        # speaker stopped and was answered as a student question.
                        speech_started_at = time.time() - heard_seconds
                        # A door, a cough, a chair. Whisper would answer it with
                        # an invented sentence, which then gets replied to as if
                        # the student had spoken.
                        if not is_probably_speech(audio, "STT", endpointed):
                            continue
                        # Flipped here, not after transcribe()+media-detection below:
                        # the mic has already closed (recognizer.listen() returned), so
                        # from the student's perspective the phrase is over, but the UI
                        # used to keep showing "listening" through the whole STT round
                        # trip to Groq -- a second or two on a Pi over home wifi -- which
                        # read as a stuck/slow transition into "thinking".
                        ui.set_state("thinking")
                        wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
                        silence_counter = 0 # Reset silence timer when sound is heard
                        # phrase_time_limit cuts a long recitation off mid-sentence.
                        # Detected here so RE-TELL can skip its acknowledgement and
                        # get straight back to listening instead of interrupting a
                        # student who never actually paused.
                        phrase_truncated = heard_seconds >= phrase_limit - 0.5
                    
                        # Her last reply is ADDED to the subject vocabulary, not
                        # substituted for it. Replacing it (what this used to do)
                        # meant the science words in STT_SEED_PROMPT biased only
                        # the very first utterance of a session and were gone for
                        # every turn after it -- which is precisely when a student
                        # is deep enough in a topic to be saying words like
                        # "mitochondria". Both fit inside Whisper's prompt window.
                        dynamic_stt_prompt = STT_SEED_PROMPT
                        for msg in reversed(chat_history):
                            if msg["role"] == "assistant":
                                clean_prompt_text = re.sub(r'EMOTION:\s*\[?[a-zA-Z]+\]?', '', msg["content"])
                                clean_prompt_text = clean_prompt_text.replace('ANSWER:', '').strip()
                                # Never prime Whisper with a script it should not be producing,
                                # otherwise one Urdu reply drags every later turn into Urdu too.
                                if not RE_UNREADABLE_SCRIPT.search(clean_prompt_text):
                                    # Word count is the wrong budget: the cap is
                                    # in CHARACTERS, and 40 words of Devanagari
                                    # is far longer than 40 words of English.
                                    # Take as many trailing words as actually
                                    # fit, so the seed is never the part that
                                    # gets cut.
                                    room = (STT_PROMPT_MAX_CHARS
                                            - stt_prompt_size(STT_SEED_PROMPT) - 1)
                                    words, recent = clean_prompt_text.split()[-40:], ""
                                    while words:
                                        candidate = " ".join(words)
                                        if stt_prompt_size(candidate) <= room:
                                            recent = candidate
                                            break
                                        words.pop(0)
                                    if recent:
                                        dynamic_stt_prompt = f"{STT_SEED_PROMPT} {recent}"
                                break

                        text, stt_language = transcribe(wav_data, dynamic_stt_prompt)

                        # Whisper wandered off to a language this device does not
                        # speak. Re-read the same audio with the language pinned
                        # rather than trusting the first pass: the student said
                        # something in Hindi or English, so a Spanish or French
                        # reading of it is wrong by definition. Devanagari in the
                        # text means the sounds really were Hindi, so that is the
                        # one worth forcing; otherwise fall back to English.
                        if text and (stt_language or "").strip().lower() not in STT_ALLOWED_LANGUAGES:
                            forced = "hi" if RE_DEVANAGARI.search(text) else "en"
                            print(f"[STT] Heard '{stt_language}', which this device does not "
                                  f"speak; re-reading as '{forced}'...", flush=True)
                            text, stt_language = transcribe(wav_data, STT_SEED_PROMPT,
                                                            language=forced)

                        # Hindi heard as Urdu (or any other Indic script): re-read the same audio
                        # forced to Hindi so we get Devanagari the voice can actually speak.
                        if RE_UNREADABLE_SCRIPT.search(text):
                            print(f"[STT] Heard '{stt_language}' in an unreadable script, re-reading as Hindi...", flush=True)
                            text, stt_language = transcribe(wav_data, STT_SEED_PROMPT, language="hi")

                        lower_text = text.lower().strip()

                        if lower_text in HALLUCINATIONS or RE_HALLUCINATION.search(lower_text):
                            text = ""

                        # --- ACOUSTIC ECHO CANCELLATION & INTERRUPTION ---
                        # Compared against last_spoken_text (everything Liza
                        # actually said, including mode intros) rather than
                        # current_ai_response, which only ever held LLM answers,
                        # and for a short window AFTER playback as well as
                        # during it -- the speaker is still draining then.
                        # Dated from when the microphone heard it, not from now.
                        # A negative gap means the capture began while she was
                        # still talking, which is echo or a barge-in either way.
                        speaking_recently = (playback_active.is_set()
                                             or (speech_started_at - last_spoken_at) < ECHO_GUARD_SEC)
                        if speaking_recently and text:
                            ai_words = echo_words(last_spoken_text)
                            user_words = echo_words(lower_text)

                            if user_words:
                                overlap_ratio = echo_overlap_ratio(user_words, ai_words)

                                if overlap_ratio > 0.4:
                                    print(f"[ECHO DETECTED] Ignoring speaker bleed: {text}", flush=True)
                                    continue

                                # Only a genuine barge-in needs interrupting. If
                                # she has already finished, this is simply the
                                # student's next turn.
                                if playback_active.is_set():
                                    print(f"[INTERRUPT DETECTED] User said: {text}", flush=True)
                                    interrupt_playback()
                    
                        print(f"[TRANSCRIPT] {text if text else '[empty]'}", flush=True)
                        if text:
                            ui_call(lambda t=text: ui_instance.set_transcript(t, "user"))
                        if not text: continue
                
                    except sr.WaitTimeoutError:
                        listen_started[:] = [0.0, 0.0]
                        clamp_energy(recognizer)
                        if playback_active.is_set() or not audio_queue.empty():
                            continue

                        # --- RE-TELL: the examiner is holding the floor open ---
                        if in_retell and retell_buffer:
                            quiet_for = time.time() - retell_silence_from

                            if quiet_for >= RETELL_EVALUATE_AFTER_S:
                                # They have finished. Mark everything they said.
                                text = " ".join(retell_buffer)
                                stt_language = retell_language
                                is_retell_eval = True
                                print(f"[RE-TELL] {quiet_for:.0f}s of silence; "
                                      f"evaluating {len(retell_buffer)} chunk(s).", flush=True)
                                # Falls through to the LLM path below rather than
                                # continuing, so the verdict reuses the ordinary
                                # streaming, language and TTS machinery.

                            elif quiet_for >= RETELL_NUDGE_AFTER_S and not retell_nudged:
                                retell_nudged = True
                                nudge = RETELL_NUDGES.get(retell_language, RETELL_NUDGES["en"])
                                print(f"[RE-TELL] {quiet_for:.0f}s of silence; nudging.", flush=True)
                                audio_queue.put(nudge)
                                audio_queue.put("[END_OF_RESPONSE]")
                                # retell_silence_from is deliberately NOT reset:
                                # the verdict is due 10s into this silence, not
                                # 10s after the reminder.
                                continue
                            else:
                                continue
                        else:
                            silence_counter += 1
                            if silence_counter >= STANDBY_AFTER_TIMEOUTS:
                                print(f"[STATE] No interaction for ~{STANDBY_AFTER_TIMEOUTS * IDLE_LISTEN_TIMEOUT_S}s. "
                                      f"Returning to Standby Mode...", flush=True)
                                session_active = False
                                silence_counter = 0
                            continue
                    except Exception as e:
                        listen_started[:] = [0.0, 0.0]
                        print(f"[STT Error] {e}", flush=True)
                        continue
        else:
            ui.set_state("idle")
            try: text = input().strip()
            except EOFError: break
            if not text: continue
            if text.lower() in ("exit", "quit"): break
            stt_language = ""
            stop_playback_event.clear()

        # --- MEDIA PLAYBACK: bypasses the LLM entirely, see start_media_playback() ---
        # Skipped for a verdict: `text` is then the student's whole recitation,
        # and a lesson that happens to start with "play..." must not launch mpv.
        media_kind, media_query = (None, None) if is_retell_eval else detect_play_media(text)

        # --- "Shall I open it?" -> yes / no ---
        # Answered before anything else looks at the text, because "yes" on its
        # own means nothing to any other branch and everything to this one.
        if pending_file and not is_retell_eval:
            path, name = pending_file
            reply_language = detect_user_language(text, stt_language)
            brief = len(text.split()) <= 4
            if brief and RE_CONFIRM_YES.match(text):
                pending_file = None
                print(f"[FILE] Confirmed; opening {name}.", flush=True)
                reason, detail = open_file_action(name, path=path)
                reply = (action_failure_sentence(reason, detail, reply_language)
                         if reason not in ("ok", "already")
                         else OPENING_ACKS.get(reply_language,
                                               OPENING_ACKS["en"]).format(name=name))
                audio_queue.put(reply)
                audio_queue.put("[END_OF_RESPONSE]")
                chat_history.append({"role": "user", "content": f"User: {text}"})
                chat_history.append({"role": "assistant", "content": reply})
                chat_history = trim_history(chat_history)
                save_history(chat_history)
                continue
            if brief and RE_CONFIRM_NO.match(text):
                pending_file = None
                reply = FILE_CANCEL_ACKS.get(reply_language, FILE_CANCEL_ACKS["en"])
                audio_queue.put(reply)
                audio_queue.put("[END_OF_RESPONSE]")
                chat_history.append({"role": "user", "content": f"User: {text}"})
                chat_history.append({"role": "assistant", "content": reply})
                chat_history = trim_history(chat_history)
                save_history(chat_history)
                continue
            # Anything else means they moved on. Drop the offer rather than
            # holding it over a later "yes" that was about something entirely
            # different.
            pending_file = None

        # --- "Is there a file about X?" -> say what is there, then ASK ---
        # A question about what exists is not an instruction to open it. Left to
        # the model this became [ACTION: open_file] every time, so asking
        # whether something existed launched it fullscreen -- and on a garbled
        # transcript it opened a file it had invented the name of.
        if not is_retell_eval:
            topic = file_query_topic(text)
            if topic:
                reply_language = detect_user_language(text, stt_language)
                hits = find_files(topic, limit=3)
                print(f"[FILE] Query for {topic!r} -> "
                      f"{[os.path.basename(h) for h in hits]}", flush=True)
                if hits:
                    name = os.path.basename(hits[0])
                    pending_file = (hits[0], name)
                    reply = FILE_FOUND_ACKS.get(reply_language,
                                                FILE_FOUND_ACKS["en"]).format(name=name)
                else:
                    reply = FILE_MISSING_ACKS.get(reply_language,
                                                  FILE_MISSING_ACKS["en"]).format(topic=topic)
                audio_queue.put(reply)
                audio_queue.put("[END_OF_RESPONSE]")
                chat_history.append({"role": "user", "content": f"User: {text}"})
                chat_history.append({"role": "assistant", "content": reply})
                chat_history = trim_history(chat_history)
                save_history(chat_history)
                continue

        # --- FAST PATH: "stop" / "close it", answered without the model ---
        # detect_play_media() above already works this way, and these are the
        # requests that deserve it most: the intent is unambiguous, the action is
        # local, and every second spent deciding is a second the thing they asked
        # to end is still running. Going to the model costs ~0.9s to its first
        # sentence and cannot do better than the regex on a two-word imperative.
        #
        # Guarded on something actually being open or playing, so the same words
        # still reach the model as ordinary conversation when they are not a
        # command -- "stop" in the middle of a lesson is not an instruction if
        # there is nothing to stop.
        if not is_retell_eval and not media_kind:
            playing_now, open_now, _ui_mode = get_device_state()
            fast_action = None
            if playing_now and RE_STOP_MEDIA_PHRASE.match(text):
                fast_action = "stop_media"
            elif open_now and (RE_CLOSE_FILE_PHRASE.match(text)
                               or RE_STOP_MEDIA_PHRASE.match(text)):
                fast_action = "close_file"
            if fast_action:
                print(f"[FAST] {fast_action} without the model: {text!r}", flush=True)
                acks = (MEDIA_STOPPED_ACKS if fast_action == "stop_media"
                        else CLOSED_FILE_ACKS)
                ack_language = detect_user_language(text, stt_language)
                try:
                    complaint = execute_action(fast_action, "", ack_language)
                except Exception as exc:
                    print(f"[ACTION ERROR] {exc}", flush=True)
                    complaint = action_failure_sentence("unknown", "", ack_language)
                reply = complaint or acks.get(ack_language, acks["en"])
                audio_queue.put(reply)
                audio_queue.put("[END_OF_RESPONSE]")
                chat_history.append({"role": "user", "content": f"User: {text}"})
                chat_history.append({"role": "assistant", "content": reply})
                chat_history = trim_history(chat_history)
                save_history(chat_history)
                continue

        # "Which song?" -> "Shape of You". The answer names a title but has no
        # "play" in it, so on its own it looks like ordinary conversation and
        # used to reach the LLM, which replied "Enjoy!" and played nothing.
        if pending_media_kind and not media_kind:
            candidate = text.strip(" .!?।\"'")
            # Only a short, non-question phrase is plausibly a title; anything
            # else means they changed the subject, so the request is dropped.
            if candidate and len(candidate.split()) <= 8 and not candidate.endswith("?"):
                media_kind, media_query = pending_media_kind, candidate
                print(f"[MEDIA] Title supplied for pending {media_kind} request.", flush=True)
            pending_media_kind = None

        # They asked for media without naming it: ask, and remember we asked.
        if media_kind and not media_query:
            pending_media_kind = media_kind
            reply = "Which song would you like?" if media_kind == "music" else "Which video would you like?"
            print(f"[MEDIA] {media_kind} request with no title; asking.", flush=True)
            audio_queue.put(reply)
            audio_queue.put("[END_OF_RESPONSE]")
            chat_history.append({"role": "user", "content": f"User: {text}"})
            chat_history.append({"role": "assistant", "content": reply})
            chat_history = trim_history(chat_history)
            save_history(chat_history)
            continue

        if media_kind:
            pending_media_kind = None
            ui.set_state("thinking")
            # What they asked for, on the card, before the search has even
            # returned -- the whole request takes upwards of ten seconds and
            # this is the first point at which anything can be shown.
            ui_call(lambda q=media_query: ui_instance.set_now_playing(q, loading=True))
            print(f"[MEDIA] {media_kind} request: {media_query}", flush=True)
            hit = search_first_video(media_query, media_kind)
            reply = f"Playing {hit['title']}." if hit else f"I couldn't find a {media_kind} for that."
            if not hit:
                ui_call(lambda: ui_instance.set_now_playing(None))

            audio_queue.put(reply)
            audio_queue.put("[END_OF_RESPONSE]")
            # Let the confirmation finish speaking before mpv claims the audio device.
            deadline = time.time() + 15
            while (playback_active.is_set() or not audio_queue.empty()) and time.time() < deadline:
                time.sleep(0.1)

            if hit:
                # Nothing is audible for the first moment anyway, and this keeps
                # the wake-word check off the mic while mpv claims the speaker.
                # Eight seconds rather than two: mpv starting up is the moment
                # the capture side is most likely to stall on this Pi (measured
                # repeatedly -- the barge-in read taken right after a track
                # starts is the one the watchdog keeps having to rescue), and
                # nobody asks Liza to stop a song in the first few seconds of it.
                media_listen_after = time.time() + 8.0
                try:
                    start_media_playback(media_kind, hit)
                except Exception as exc:
                    print(f"[MEDIA ERROR] {exc}", flush=True)
                    # Otherwise the card is left saying "Loading…" for good.
                    ui_call(lambda: ui_instance.set_now_playing(None))

            chat_history.append({"role": "user", "content": f"User: {text}"})
            chat_history.append({"role": "assistant", "content": reply})
            chat_history = trim_history(chat_history)
            save_history(chat_history)
            continue

        # --- RE-TELL: collect the recitation instead of answering it ---
        # An examiner does not argue with a candidate halfway through. Each chunk
        # is banked and answered with at most a few words, and the marking happens
        # once, from the whole thing, when they stop -- see the silence branch in
        # the WaitTimeoutError handler above.
        if in_retell and not is_retell_eval:
            retell_language = detect_user_language(text, stt_language)

            # "That's it, how did I do?" -- an explicit request to be marked, so
            # they do not have to sit out the silence timer. Meaningless with an
            # empty buffer: that is a student who has not started yet.
            mark_now = bool(RE_RETELL_MARK_NOW.search(text)) and bool(retell_buffer)
            # Kept out of the transcript when it is nothing but the request
            # itself; a longer sentence that happens to end in "that's it" still
            # carries content worth marking.
            if not mark_now or len(text.split()) > 6:
                retell_buffer.append(text)
            retell_silence_from = 0.0   # restamped when the mic next opens
            retell_nudged = False

            if mark_now:
                text = " ".join(retell_buffer)
                stt_language = retell_language
                is_retell_eval = True
                print(f"[RE-TELL] Student asked to be marked; "
                      f"evaluating {len(retell_buffer)} chunk(s).", flush=True)
            else:
                print(f"[RE-TELL] Banked chunk {len(retell_buffer)} "
                      f"({len(text.split())} words).", flush=True)
                # No interjection when the recorder cut them off rather than they
                # paused -- they are still mid-sentence and about to continue.
                if not phrase_truncated:
                    acks = RETELL_ACKS.get(retell_language, RETELL_ACKS["en"])
                    audio_queue.put(acks[retell_ack_index % len(acks)])
                    audio_queue.put("[END_OF_RESPONSE]")
                    retell_ack_index += 1
                continue

        # --- 2. THINK & STREAM ---
        ui.set_state("thinking")
        if is_retell_eval:
            # Cleared before the call, not after: if the request fails, the next
            # silence must not re-submit the same recitation forever.
            retell_buffer, retell_silence_from, retell_nudged = [], 0.0, False
            mode_instruction = RETELL_EVALUATION_PROMPT.format(transcript=text)
        else:
            mode_instruction = MODE_INSTRUCTIONS.get(ui.current_mode, MODE_INSTRUCTIONS["TUTOR"])

        user_language = detect_user_language(text, stt_language)
        print(f"[LANGUAGE] heard={stt_language or 'n/a'} -> replying in {user_language}", flush=True)

        current_time = datetime.now().strftime("%I:%M %p, %A, %B %d, %Y")
        dynamic_system_prompt = UNIVERSAL_SYSTEM_PROMPT.format(
            education_scope=ASSISTANT_SCOPE,
            emotion_persona=EMOTION_PERSONA,
            agentic_actions=AGENTIC_ACTIONS,
            domain_guidelines=mode_instruction,
            language_guidelines=LANGUAGE_INSTRUCTIONS[user_language],
            # Volatile, so it sits at the very bottom with the clock -- see the
            # section-order note above UNIVERSAL_SYSTEM_PROMPT.
            device_state=device_state_block(),
            system_time=current_time
        )

        if chat_history and chat_history[0].get("role") == "system":
            chat_history[0]["content"] = dynamic_system_prompt
        else:
            chat_history.insert(0, {"role": "system", "content": dynamic_system_prompt})

        # The recitation is already quoted in full inside the evaluation prompt;
        # repeating it here would only push the older turns out of the window.
        chat_history.append({"role": "user", "content":
                             "I have finished. Give me your verdict."
                             if is_retell_eval else f"User: {text}"})
        chat_history = trim_history(chat_history)

        pending_action = (None, None)
        try:
            result_holder = {}

            def stream_hf(is_search_loop=False):
                try:
                    # Starts the clock on the "let me think" line above; every
                    # path below that queues real speech cancels it.
                    answered = start_thinking_filler(user_language)
                    # A RE-TELL VERDICT IS JUDGED ON THE RECITATION ALONE.
                    #
                    # The recitation is quoted in full inside the system prompt,
                    # and the turn appended below is only "I have finished, give
                    # me your verdict" -- so with the ordinary history attached,
                    # the largest thing in the request is whatever was being
                    # talked about BEFORE, and that is what gets marked. In the
                    # log: the student recited photosynthesis, and was told they
                    # had correctly identified mitochondria but missed the cell
                    # membrane, which was the CO-TELL conversation from earlier.
                    # Twice, word for word, because the recitation was never
                    # being read at all.
                    #
                    # So the evaluation gets the system prompt and the request,
                    # and nothing else. It is a marking job, not a conversation.
                    messages = ([chat_history[0], chat_history[-1]]
                                if is_retell_eval and len(chat_history) >= 2
                                else chat_history)
                    response_stream = start_chat_stream(messages)
                    
                    buffer = ""
                    full_response = ""
                    emotion_parsed = False
                    is_searching = False
                    # Whether any audio has been queued for THIS reply yet; see
                    # the first-flush note in the splitter below.
                    spoken_anything = False

                    for chunk in response_stream:
                        if stop_playback_event.is_set():
                            break 
                        
                        delta = chunk.choices[0].delta.content
                        if delta is None: continue
                        full_response += delta

                        if "SEARCH:" in full_response:
                            is_searching = True
                            continue
                        
                        if not is_searching and not emotion_parsed:
                            if "ANSWER:" in full_response:
                                emotion_parsed = True
                                # Everything before ANSWER: is dropped from the
                                # speech, which is exactly where the EMOTION line
                                # lives. Read it here so the mood chip changes as
                                # she starts talking, not after she has finished.
                                mood = RE_EMOTION_LINE.search(full_response.split("ANSWER:")[0])
                                if mood:
                                    ui_invoke("set_emotion", mood.group(1))
                                try: buffer = full_response.split("ANSWER:")[1].lstrip()
                                except IndexError: buffer = ""
                            else: continue 
                        elif not is_searching:
                            buffer += delta 
                            global current_ai_response
                            current_ai_response = full_response 

                        # The FIRST flush of a reply is the one the student is
                        # sitting in silence for, and it was being held back
                        # twice over.
                        #
                        # The 25-character gate is the first: her openers are
                        # short by design -- rule 6 asks for "Hmm, tricky." before
                        # the answer -- so the very sentence written to be said
                        # first was under the bar, and waited for the sentence
                        # after it to be generated before either could be spoken.
                        #
                        # Taking sentence_matches[-1] is the second: with two
                        # sentences buffered it ships BOTH, so the first one
                        # waits on the second for no reason. That is the right
                        # trade later in a reply, where fewer, longer TTS
                        # requests keep the speaker fed -- but not for the
                        # opening one, where nothing is playing yet and the only
                        # thing that matters is that something starts.
                        #
                        # So: the first flush goes out at the first sentence
                        # boundary, whatever its length. Everything after it
                        # behaves exactly as before.
                        gate = 0 if not spoken_anything else 25
                        if not is_searching and emotion_parsed and len(buffer) > gate:
                            sentence_matches = list(RE_SENTENCE_SPLIT.finditer(buffer))
                            if sentence_matches:
                                cut = (sentence_matches[0] if not spoken_anything
                                       else sentence_matches[-1]).end()
                                new_sentences = buffer[:cut].strip()
                                buffer = buffer[cut:]

                                clean = clean_text_for_tts(new_sentences)
                                if clean:
                                    spoken_anything = True
                                    answered.set()
                                    audio_queue.put(clean)

                    if not is_searching:
                        if not emotion_parsed: buffer = full_response 
                        if buffer.strip():
                            clean = clean_text_for_tts(buffer.strip())
                            if clean:
                                answered.set()
                                audio_queue.put(clean)
                    
                    if is_searching:
                        try: search_query = full_response.split("SEARCH:")[1].strip()
                        except IndexError: search_query = full_response.replace("SEARCH:", "").strip()
                        
                        search_query = re.sub(r'EMOTION:.*', '', search_query, flags=re.IGNORECASE)
                        search_query = re.sub(r'ANSWER:.*', '', search_query, flags=re.IGNORECASE)
                        search_query = search_query.replace('[', '').replace(']', '').strip()
                        
                        clean_speech_query = clean_text_for_tts(search_query)
                        search_msg = SEARCH_NOTICES[user_language].format(query=clean_speech_query)
                        answered.set()
                        audio_queue.put(search_msg)
                        audio_queue.put("[END_OF_RESPONSE]")

                        ui_call(lambda: ui_instance.set_state("thinking", f"Searching for: {search_query}..."))

                        search_context = ""
                        try:
                            with DDGS() as ddgs:
                                try:
                                    answers = list(ddgs.answers(search_query))
                                    if answers and 'text' in answers[0]:
                                        search_context += f"INSTANT ANSWER: {answers[0]['text']}\n\n"
                                except Exception: pass
                                
                                results = list(ddgs.text(search_query, max_results=4))
                                if results: 
                                    search_context += "\n".join([f"- {r['title']}: {r.get('body', r.get('snippet', ''))}" for r in results])
                                
                                if not search_context.strip(): 
                                    raise Exception("Empty results from DDG")
                        except Exception as e:
                            print(f"[SEARCH ERROR] {e}", flush=True)
                            search_context = "The web search failed or no results were found."
                            
                        chat_history.append({"role": "assistant", "content": full_response})
                        chat_history.append({
                            "role": "user", 
                            "content": f"Live web search results:\n{search_context}\n\nAnswer from these ONLY. If they do not contain the answer, say 'I couldn't find the exact data online right now.' Do not guess or change the subject. The results may be in English; you MUST still answer in the student's language and script per rule 2. Start with ANSWER:"
                        })
                        stream_hf(is_search_loop=True)
                        chat_history.pop() 
                        chat_history.pop() 
                        return 

                    # Set on BOTH passes. The search branch above returns as
                    # soon as the second pass finishes, so with this inside the
                    # is_search_loop guard a searched answer reached neither the
                    # history nor the action parser -- the tag on "let me check,
                    # then I'll open your notes" was silently dropped.
                    result_holder['text'] = full_response
                    if not is_search_loop:
                        result_holder['status'] = 'ok'
                        
                except Exception as exc:
                    if not is_search_loop:
                        result_holder['status'] = 'error'
                        result_holder['error'] = str(exc)

            worker = threading.Thread(target=stream_hf, daemon=True)
            worker.start()
            worker.join(timeout=25)

            if worker.is_alive():
                ui.set_state('error')
                audio_queue.put("I'm having trouble thinking right now.")
            else:
                if result_holder.get('status') == 'error': raise RuntimeError(result_holder.get('error'))
                full_response = result_holder.get('text', '').strip()
                pending_action = parse_action(full_response)
                chat_history = remember_reply(chat_history, full_response)
                chat_history = trim_history(chat_history)

        except Exception as e:
            print(f"HF API Error: {e}", flush=True)
            # A 429 is not an outage and must not be described as one: nothing is
            # broken, the minute's token budget is simply spent, and the honest
            # instruction is to ask again shortly. See MAX_HISTORY_BYTES for why
            # this used to fire almost only in Hindi.
            busy = "429" in str(e) or "rate_limit" in str(e).lower()
            table = LLM_BUSY if busy else LLM_UNREACHABLE
            audio_queue.put(table.get(user_language, table["en"]))

        audio_queue.put("[END_OF_RESPONSE]")

        # --- 3. ACT ---
        # After the speaking for most actions, never during it: opening a file
        # steals focus from her own window, and [ACTION: sleep] would otherwise
        # cut her goodbye off mid-word. Same drain-with-a-deadline as the media
        # path above. Never for a RE-TELL verdict: that reply is a mark, not an
        # instruction.
        #
        # But NOT for the actions that end something. Both reasons above are
        # about starting things, and for stopping them the wait is itself the
        # bug: the video the student just asked to close keeps playing for the
        # whole of the sentence explaining that it is closing, which is her
        # entire reply on top of the ~3.3s of pipeline in front of it. Reported
        # as exactly that -- closing a file or a video "could take time".
        # Closing early costs nothing: there is no focus to steal from a window
        # that is going away, and nothing of hers to cut off.
        action_name, action_param = pending_action
        if action_name and not is_retell_eval:
            if action_name not in IMMEDIATE_ACTIONS:
                deadline = time.time() + 15
                while (playback_active.is_set() or not audio_queue.empty()) and time.time() < deadline:
                    time.sleep(0.1)
            print(f"[ACTION] {action_name}"
                  f"{':' + action_param if action_param else ''}", flush=True)
            try:
                complaint = execute_action(action_name, action_param, user_language)
                if complaint.startswith(ACTION_DATA_PREFIX):
                    complaint = phrase_action_result(
                        action_param or action_name,
                        complaint[len(ACTION_DATA_PREFIX):], user_language)
            except Exception as exc:
                print(f"[ACTION ERROR] {exc}", flush=True)
                complaint = action_failure_sentence("unknown", "", user_language)
            # Success says nothing: she has already said it. Only a failure is
            # worth speaking, or the student is left believing it worked.
            if complaint:
                audio_queue.put(complaint)
                audio_queue.put("[END_OF_RESPONSE]")
                # AND SHE HAS TO REMEMBER SAYING IT. This sentence is spoken by
                # the device, not generated in the reply, so without this the
                # model never sees it: it listed a folder, the student said
                # "list them", and the answer was "list what exactly?" -- she
                # had no idea she had just been talking about files. Recorded as
                # her own turn, because from the student's side that is what it
                # was: the last thing they heard her say.
                chat_history.append({"role": "assistant",
                                     "content": f"ANSWER: {complaint}"})

        time.sleep(0.5) 
        save_history(chat_history)

# ==========================================
# Main Execution
# ==========================================
if __name__ == "__main__":
    if "--list-voices" in sys.argv:
        args = sys.argv[sys.argv.index("--list-voices") + 1:]
        list_cartesia_voices(args[0] if args else "")
        sys.exit(0)

    if "--list-mics" in sys.argv:
        list_microphones()
        sys.exit(0)

    if "--calibrate-mic" in sys.argv:
        calibrate_microphone()
        sys.exit(0)

    if not CARTESIA_API_KEY:
        print("[WARNING] CARTESIA_API_KEY is not set. Liza will not be able to speak.", flush=True)
    if not CARTESIA_VOICE_ID and not all(VOICE_IDS.values()):
        print("[WARNING] No Cartesia voice configured. Set CARTESIA_VOICE_ID in .env "
              "(see `python assist.py --list-voices`).", flush=True)

    # A previous run that was killed outright can leave a player still going.
    kill_stray_media()

    player_thread = threading.Thread(target=audio_player_worker, daemon=True)
    player_thread.start()

    if not WEATHER_API_KEY:
        print("[WARNING] WEATHER_API_KEY is not set; the weather panel will stay blank.", flush=True)

    HEADLESS = ("--headless" in sys.argv) or (os.getenv("HEADLESS") == "1")

    if HEADLESS:
        app_ui = HeadlessUI()
        ui_instance = app_ui
        # Started only after ui_instance is assigned: weather_worker's first fetch can
        # complete before that point, and ui_call() silently drops updates until then.
        if WEATHER_API_KEY:
            threading.Thread(target=weather_worker, daemon=True).start()
        ai_thread = threading.Thread(target=ai_loop, args=(app_ui, True), daemon=True)
        ai_thread.start()
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: pass
    else:
        root = tk.Tk()
        app_ui = TutorUI(root)
        ui_instance = app_ui
        if WEATHER_API_KEY:
            threading.Thread(target=weather_worker, daemon=True).start()
        ai_thread = threading.Thread(target=ai_loop, args=(app_ui,), daemon=True)
        ai_thread.start()
        # Logged because this process has exited silently more than once with no
        # trace of why: mainloop returning and a torn-down window look identical
        # from outside, and a Tk error would otherwise vanish with the process.
        root.protocol("WM_DELETE_WINDOW",
                      lambda: (print("[EXIT] Window closed by the user.", flush=True),
                               root.destroy()))
        try:
            root.mainloop()
            print("[EXIT] Tk mainloop returned normally (window destroyed).", flush=True)
        except BaseException:
            print("[EXIT] Tk mainloop raised:", flush=True)
            traceback.print_exc()
            raise
        finally:
            _cleanup()

