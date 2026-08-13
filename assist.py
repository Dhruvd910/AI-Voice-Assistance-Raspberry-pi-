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
from groq import Groq
from cartesia import Cartesia
from ddgs import DDGS

# 1. The Ears & Brain (Groq API)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
groq_client = Groq(api_key=GROQ_API_KEY)

# 2. The Voice (Cartesia API)
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
cartesia_client = Cartesia(api_key=CARTESIA_API_KEY or None)

# ==========================================
# Configuration & Setup
# ==========================================
# Speech-to-text: no fixed language, so Whisper auto-detects Hindi vs English.
STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")

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
media_process = None
media_procs = []                   # [yt-dlp, mpv] for the current playback
sleep_event = threading.Event()    # the Sleep button was tapped; drop to standby

# Everything Liza has said recently, for echo rejection. playback_active alone
# is not enough: it clears when aplay's stdin closes, but sound keeps coming out
# of the ALSA buffer for a moment afterwards, so the mic reopens in time to
# record her own tail. That is how a mode intro came back as a [TRANSCRIPT] and
# got answered as if the student had said it.
last_spoken_text = ""
last_spoken_at = 0.0
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
# Bounds on the speech-detection threshold. See clamp_energy() for why both ends
# are needed; `--calibrate-mic` measures the right values for a room.
MIC_ENERGY_FLOOR = int(os.getenv("MIC_ENERGY_FLOOR", "1000"))
MIC_ENERGY_CEILING = int(os.getenv("MIC_ENERGY_CEILING", "1300"))

# How long a pause ends the student's turn. 1.5s (the old fixed value) is dead
# air on every single exchange and is most of why she felt slow rather than
# conversational -- a person starts answering roughly 0.2s after you stop. A
# recitation in RE-TELL genuinely does pause mid-thought, so that mode keeps the
# patient value.
PAUSE_THRESHOLD_NORMAL = float(os.getenv("PAUSE_THRESHOLD", "0.8"))
PAUSE_THRESHOLD_RETELL = float(os.getenv("PAUSE_THRESHOLD_RETELL", "1.6"))

# Same trade as WAKE_LISTEN_TIMEOUT_S: a longer wait for speech to begin returns
# just as fast when it does, and halves how often the capture device is cycled
# during an active session.
IDLE_LISTEN_TIMEOUT_S = 10
STANDBY_AFTER_TIMEOUTS = 3   # -> ~30s of quiet before dropping back to standby

def clamp_energy(recognizer):
    """Hold the speech threshold inside the band `--calibrate-mic` measured.

    Both ends matter and for opposite reasons. Too high and the student is
    inaudible: on this hardware speech only reaches ~1316 while room noise peaks
    near 1730, so an unclamped threshold parks itself above her voice and she is
    never heard again. Too low and room noise opens a phrase constantly, so
    Whisper gets fed silence and invents 'Thank you.' out of it."""
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

def remember_reply(chat_history, text):
    """Append an assistant turn, carrying only what a later turn can use.

    The ANSWER: prefix is deliberately kept -- it is the output contract, and
    the surviving examples are what hold the model to it. An empty reply is
    dropped instead of stored: a failed turn that leaves a blank assistant
    message behind teaches the model that blank replies are allowed."""
    text = RE_EMOTION_TAG.sub('', text or '').strip()
    if text:
        chat_history.append({"role": "assistant", "content": text})
    return chat_history

# A turn count cannot bound the payload, only the number of pieces it arrives
# in. One RE-TELL turn is a student reciting from memory for up to
# RETELL_PHRASE_LIMIT_S seconds, so six of those "turns" is an unbounded amount
# of text, and the oldest of them is the least worth paying for on every
# subsequent request. Characters are counted rather than tokens because the
# ratio differs ~3x between Devanagari and Latin and no tokeniser for this model
# is available locally -- the cap only has to stop runaway growth, not be exact.
MAX_HISTORY_CHARS = 6000

def trim_history(chat_history):
    if not chat_history: return chat_history
    system_msgs = [m for m in chat_history if m.get("role") == "system"]
    other_msgs = [m for m in chat_history if m.get("role") != "system"]
    if len(other_msgs) > MAX_HISTORY_TURNS * 2:
        other_msgs = other_msgs[-(MAX_HISTORY_TURNS * 2):]
    # Drop whole turns from the oldest end until the rest fits. The newest turn
    # is never dropped, however long it is: it is what the student just said,
    # and answering without it is worse than paying for it.
    total = sum(len(m.get("content") or "") for m in other_msgs)
    while len(other_msgs) > 1 and total > MAX_HISTORY_CHARS:
        total -= len(other_msgs[0].get("content") or "")
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
    try: return sr.Microphone(device_index=mic_index) if mic_index is not None else sr.Microphone()
    except Exception as e:
        try: return sr.Microphone()
        except Exception: return None

def _cleanup(*_args):
    stop_playback_event.set()
    with subprocess_lock:
        for proc in active_subprocesses:
            try: proc.terminate()
            except Exception: pass
        active_subprocesses.clear()
    try: audio_queue.put_nowait(None)
    except Exception: pass

atexit.register(_cleanup)
signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

def start_mic_watchdog(get_state):
    """Warns when a listen has not returned for far longer than its own limits
    allow. PyAudio's read() is a blocking C call, so recognizer.listen()'s
    timeout/phrase_time_limit cannot fire if the device itself stops delivering
    audio -- the assistant simply goes quiet forever with no error anywhere.
    This at least names the problem in the log instead of leaving it invisible."""
    def watch():
        warned = False
        while True:
            time.sleep(5)
            started = get_state()
            if started and (time.time() - started) > 60:
                if not warned:
                    print("[WATCHDOG] Microphone read has been blocked for over "
                          "60s -- the capture device is wedged. Restart Liza.", flush=True)
                    warned = True
            else:
                warned = False
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
        "prompt": prompt
    }
    if language: params["language"] = language

    for attempt in range(attempts):
        try:
            result = groq_client.audio.transcriptions.create(**params)
            return (result.text or "").strip(), (getattr(result, "language", "") or "")
        except Exception as exc:
            if attempt == attempts - 1:
                raise
            print(f"[STT] Attempt {attempt + 1} failed ({exc}); retrying...", flush=True)
            time.sleep(0.4)

def audio_seconds(audio):
    return len(audio.frame_data) / float(audio.sample_rate * audio.sample_width)

def listen_for_wake_word(recognizer, mic_device, asleep=False):
    """True when 'Hey Liza' is heard. recognizer.listen blocks on silence, so audio is
    only sent to Whisper when somebody actually speaks near the device.

    `asleep` is set after the Sleep button and requires the full greeting; see
    RE_WAKE_WORD_ASLEEP."""
    try:
        with mic_device as source:
            # timeout only bounds how long it waits for speech to BEGIN -- it
            # still returns the instant somebody talks -- so a long one costs no
            # responsiveness and halves how often the capture device is opened
            # and closed. That churn is what wedges this USB dongle (the failure
            # start_mic_watchdog() exists to report), and standby used to cycle
            # it every four seconds indefinitely.
            audio = recognizer.listen(source, timeout=WAKE_LISTEN_TIMEOUT_S,
                                      phrase_time_limit=WAKE_PHRASE_LIMIT_S)
    except sr.WaitTimeoutError:
        clamp_energy(recognizer)
        return False, "", ""
    except Exception as exc:
        print(f"[WAKE ERROR] {exc}", flush=True)
        time.sleep(0.5)
        return False, "", ""

    clamp_energy(recognizer)
    # Too short to be "Hey Liza". Whisper does not return nothing for a door
    # closing, it returns its best guess at words, so every one of these clips
    # was a paid API call whose only possible outcomes were a false wake or a
    # log line. Discarding them here is both cheaper and more accurate.
    if audio_seconds(audio) < MIN_SPEECH_SEC:
        return False, "", ""

    try:
        wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
        text, language = transcribe(wav_data, WAKE_SEED_PROMPT, model=WAKE_STT_MODEL)
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
    global active_subprocesses
    while True:
        first_item = audio_queue.get()
        if first_item is None: break
        if first_item == "[END_OF_RESPONSE]":
            audio_queue.task_done()
            continue
        if stop_playback_event.is_set():
            audio_queue.task_done()
            continue

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

MASCOT_CX, MASCOT_CY = 399, 212
DOTS_Y = 18
STATE_LABEL_Y = 44

BTN_Y0, BTN_H, BTN_W, BTN_GAP = 400, 64, 252, 12
BTN_XS = [10, 274, 538]

# ---------- 3D mascot animation ----------
# The source clips are 1920x1080 RGBA at ~200 frames each; decoding all four
# in full would need several GB of RAM. On first run each one is cropped to
# the character, downsized, and cached to disk as small PNG frames, and only
# that small cache is ever loaded into memory.
MASCOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "GIFS")
MASCOT_CACHE_DIR = os.path.join(MASCOT_DIR, "cache")
MASCOT_SOURCES = {"idle": "IDLE.gif", "listening": "listning.gif",
                   "thinking": "thinking.gif", "speaking": "TALKING.gif"}
MASCOT_CROP = (635, 0, 1359, 1080)   # union bounding box of the character, all 4 clips
MASCOT_H = 300
MASCOT_W = round(MASCOT_H * (MASCOT_CROP[2] - MASCOT_CROP[0]) / (MASCOT_CROP[3] - MASCOT_CROP[1]))
MASCOT_STEP = 2  # keep every 2nd frame: still smooth, halves memory and disk

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
        frames = self.mascot_frames.get(bucket) or []
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
    def set_now_playing(self, title):
        """YouTube titles are usually "Artist - Track (Official Video)", so the
        card shows the two halves separately when that shape is recognisable."""
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
            text=self._ellipsize(artist.strip() or ("—" if title else "Ask me to play a song"),
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
    def set_state(self, state_type, caption=None): self.current_state = state_type
    def set_transcript(self, text, speaker="user"): pass
    def set_weather(self, reading): pass
    def set_now_playing(self, title): pass
    def set_media_progress(self, pos, dur, paused): pass

# ==========================================
# Core AI Functions
# ==========================================
# Bilingual seed so Whisper is not biased towards English on the first turn.
STT_SEED_PROMPT = "Hey Liza, explain the concept clearly. नमस्ते लीज़ा, यह concept समझाओ।"

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
    clean = re.sub(r'VISUAL:.*', '', text, flags=re.IGNORECASE)
    clean = re.sub(r'EMOTION:.*', '', clean, flags=re.IGNORECASE)
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
RE_PLAY_VIDEO_TRAIL = re.compile(r'^\s*play\s+(.+?)\s+video\s*$', re.IGNORECASE)
RE_PLAY_MUSIC_LEAD = re.compile(
    r'^\s*play\s+(?:a\s+|the\s+|some\s+)?(?:music|song)\s+(?:of\s+|for\s+|by\s+|called\s+)?(.+)$',
    re.IGNORECASE)
RE_PLAY_MUSIC_TRAIL = re.compile(r'^\s*play\s+(.+?)\s+(?:song|music)\s*$', re.IGNORECASE)
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
    r'^\s*(?:(?:can|could|would)\s+you\s+(?:please\s+)?|please\s+|'
    r'i\s+want\s+to\s+|i\s+wanna\s+|i\s+would\s+like\s+to\s+|i\'d\s+like\s+to\s+)+',
    re.IGNORECASE)
RE_MEDIA_TRAILING_FILLER = re.compile(
    r'\s*(?:for\s+me\s+please|for\s+me|please)\s*[.?!]*$', re.IGNORECASE)

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
    kind, query = _detect_hindi_play(text)
    if kind:
        return kind, query
    # Only the English patterns below are anchored at the start, so only they
    # need the polite lead-in/trailing-filler stripped -- Hindi's verb-final
    # RE_HI_PLAY_VERB.search() above already tolerates a lead-in as-is.
    text = RE_MEDIA_TRAILING_FILLER.sub('', RE_MEDIA_LEAD_IN.sub('', text)).strip()
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

def search_first_video(query, kind="video"):
    """Title and URL of the first result for `query`, or None. For kind="music"
    the search is constrained to songs rather than videos in general."""
    songs_only = kind == "music"
    search_query = f"{query} song" if songs_only else query
    try:
        with DDGS() as ddgs:
            results = list(ddgs.videos(search_query, max_results=6))
        hit = _pick_result(results, "content", query, songs_only)
        if hit:
            return hit
    except Exception as exc:
        print(f"[MEDIA] Video search failed ({exc}); trying a text search instead.", flush=True)

    # DDG's dedicated video endpoint is occasionally rate-limited even while
    # plain text search still works, so fall back to a YouTube-restricted
    # text search rather than giving up.
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{search_query} site:youtube.com/watch", max_results=6))
        hit = _pick_result(results, "href", query, songs_only)
        if hit:
            return hit
    except Exception as exc:
        print(f"[MEDIA] Fallback text search also failed: {exc}", flush=True)
    return None

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

def media_progress_worker():
    """Feeds the music card's progress bar for as long as mpv is alive."""
    while media_active.is_set():
        position = mpv_command(["get_property", "time-pos"])
        duration = mpv_command(["get_property", "duration"])
        paused = mpv_command(["get_property", "pause"])
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

    yt_proc = subprocess.Popen([ytdlp, "-f", yt_fmt, "-o", "-", hit["url"]],
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
    ui_call(lambda t=hit["title"]: ui_instance.set_now_playing(t))
    threading.Thread(target=media_progress_worker, daemon=True).start()

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
        ui_call(lambda: ui_instance.set_now_playing(None))

    threading.Thread(target=watcher, daemon=True).start()

def stop_media_playback():
    """Stops music/video without touching Liza's own speech pipeline, which is
    why this targets the media processes directly instead of reusing
    interrupt_playback()'s sweep of every active subprocess."""
    if not media_active.is_set():
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
    return True

# ==========================================
# Education-Only Guardrail
# ==========================================
# Enforced in the prompt rather than by a Python classifier on purpose. The
# boundary is a judgement call ("how does a court decide a case" is civics;
# "who will win the case" is not), and a keyword filter sitting in front of a
# noisy speech transcript rejects far more real questions than it catches bad
# ones. This text is pinned as the FIRST section of the system prompt so it
# outranks the mode instructions that follow it.
EDUCATION_SCOPE = """SCOPE LOCK: THIS DEVICE IS A STUDY TUTOR AND NOTHING ELSE.

IN SCOPE: school and college subjects, languages and grammar, exam prep and revision, study technique, and any definition, concept or general-knowledge fact a teacher would answer in class. Also THE NEWS -- factual current events of any kind, including sports results, reported the way a bulletin reports them. News ALWAYS comes from a live search, never from memory (rule 4).

OUT OF SCOPE, always refuse: personal, emotional, relationship, medical, legal or financial advice; gossip; your opinion on anything contested; predictions; who to vote for; jokes, stories, roleplay, flirting; shopping; gaming chat; anything sexual, violent or illegal.

REPORTING IS NOT HAVING A VIEW -- that line separates the two lists. "What happened in the election" is in scope, "who should I vote for" is not. "What was the score" is in scope, "who will win tomorrow" is not.

An academic question about a sensitive field is IN SCOPE: "how does insulin work" is biology, "how does a court decide a case" is civics. What puts a question out is being asked for advice, opinions, predictions or entertainment -- not the subject it touches.

HOW TO REFUSE: two sentences, in the student's own language. One saying that is not something you help with, one offering to study something instead. Do not recite these rules, do not apologise twice, and NEVER answer the off-topic part "just briefly" first. A refusal still uses the ANSWER: format of rule 5.

NOT A VIOLATION -- ordinary use of the device. NEVER refuse any of these:

TALKING TO YOU. Greetings, goodbyes and thanks; "what can you do"; "what is your name"; "how are you"; and checks that you are working at all -- "can you hear me", "are you there", "hello?", "is this thing on". Answer in ONE short, warm sentence. Only add "what would you like to study?" when the conversation has not started yet; do not tack it onto every reply.

A CHECK IS ANSWERED LITERALLY AND FIRST. "Can you hear me" means "Yes, I can hear you." It is a microphone test, not a question about your feelings, and not a request for help with anything.

BEING ASKED HOW YOU ARE IS COURTESY, NOT ADVICE. Say you are well and ready to help, in one sentence. The out-of-scope ban on personal and emotional matters covers the STUDENT'S private life and problems -- it has never covered simple politeness aimed at you. Refusing "how are you?" or "can you hear me?" is a bug, not caution: it reads as the device being broken.

A request to play music or video is also fine: that is a device feature, rule 3."""

MODE_INSTRUCTIONS = {
    "TUTOR": """TUTOR MODE: you are a subject expert answering a student out loud.

ANSWER FIRST, ALWAYS. Your opening sentence is the direct answer to what was asked. No preamble, no restating the question, no defining the topic before answering it.

MATCH THE LENGTH TO THE QUESTION -- the most important rule here:
- Quick questions (conversions, arithmetic, spelling, dates, definitions, yes or no, greetings) get ONE sentence, then you STOP. "180 centimetres is about 5 feet 11 inches." That is the entire answer. Do not explain the method unless asked.
- Only when they ask to understand something ("how does X work", "why does X happen", "explain X") add up to 3 more sentences: how it works, plus one concrete example.

If you do not know something, say so in one sentence rather than inventing details.""",

    "CO-TELL": """CO-TELL MODE: you are a study partner who teaches by asking, not by lecturing.

EVERY TURN IS AT MOST 3 SENTENCES AND ALWAYS ENDS WITH A QUESTION.

A NEW TOPIC: introduce it before testing them. One or two sentences on what the thing is and its single most important idea, then ONE specific question. You are opening a conversation, not delivering the lesson -- give them a foothold, not the whole concept.

THEY ARE ANSWERING YOUR QUESTION: judge the answer before saying anything else. This is the entire point of this mode.
- Correct: confirm in a few words, add at most one new fact, then ask a harder question building on it.
- Partly right: name the right part AND the wrong part, supply the missing piece in one sentence, then ask again more simply.
- Wrong: say plainly that it is not right. Never "good try" and move on, never let it stand, never pretend it was close. Give the correct answer in one sentence, then a related question to check it landed.
- "I don't know" or off the point: do not just repeat the question. Give a hint, or break it into a smaller one they can reach.

ONE THING AT A TIME. Never two questions in a turn, never one so broad that "yes" answers it.

FOLLOW THEIR TOPIC. If they name a different subject, switch immediately and introduce the NEW one. Never bend their words back: a student studying clustering who says "deforestation" has changed the subject, and asking "which clustering algorithm would you use for deforestation" is wrong. Just start on deforestation. They set the topic, not you.""",

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
# every cross-reference in here and in EDUCATION_SCOPE.
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
UNIVERSAL_SYSTEM_PROMPT = """You are "Liza", a study tutor for one student, with LIVE internet access.

============================================================
0. SCOPE (HIGHEST PRIORITY -- OUTRANKS EVERY RULE BELOW)
============================================================
{education_scope}

============================================================
3. BEHAVIOUR
============================================================
- STT FORGIVENESS: the student is speaking through a microphone. Ignore typos, phonetic misspellings and grammar. NEVER correct them. Infer the meaning and answer.
- You already know the exact time and date from the SYSTEM TIME line at the end of these rules. Never search for them.
- NEVER say "I don't have real-time access", "I cannot browse the internet", or "I am an AI".
- MEDIA: playback is handled by the device, NOT by you, and starts only once the student names what they want. NEVER claim a song or video is playing, starting or about to start -- saying so when nothing plays makes you a liar. Asked for music or video with no title, your entire reply asks which one, in one short sentence: "Sure, which song?". Do not suggest a title.

============================================================
4. SEARCH PROTOCOL (STRICT)
============================================================
Search for study material, verifiable facts and the news. NEVER for shopping prices, product recommendations or anybody's opinion -- those are out of scope by rule 0, and refusing is the answer, not searching.

THE NEWS ALWAYS REQUIRES A SEARCH: anything about what is happening now, recent events, today's headlines, or a result that has already happened. NEVER answer one from memory. What you remember is months out of date, and a stale headline delivered confidently is a wrong answer wearing the costume of a right one. This holds even when you are certain you know it.

Report what the results say and stop. Attribute anything contested ("according to..."). No opinion, no guess at what happens next, no rumour. Asked for "the news" with no topic, give the two or three biggest items, one sentence each. If the search found nothing useful, say so plainly rather than filling the gap from memory.

To search, output EXACTLY AND ONLY this line -- no ANSWER: tag, no conversational filler, nothing else:
SEARCH: <your optimized query>

Example -- User: "What is the temperature in New Delhi?" -> SEARCH: current temperature in New Delhi weather

============================================================
5. SPEAKING LIKE A PERSON
============================================================
Everything you write is spoken aloud. Write what a knowledgeable person would SAY, not what they would type.
- Use contractions: "it's", "you'd", "that's", "don't". Full forms sound stilted read aloud.
- Vary how you open. Never start consecutive replies the same way, and never with "Certainly", "Great question", "Of course", "Sure thing", "I'd be happy to" or "As an AI".
- NEVER announce your structure: no "The core principle is", "The mechanism is", "Firstly", "In conclusion", "In a real-world example", and no numbering. Just say the thing.
- No bullet points, no markdown, no emoji, no parentheses, and no symbols a voice cannot read.
- Stop the moment the question is answered. A short answer is a good answer; padding one line into a paragraph is a failure, not thoroughness.
- Be warm and direct, like a good teacher who respects the student's time. Never bubbly, never apologetic, never servile.

============================================================
1. CURRENT TEACHING MODE (CRITICAL OVERRIDE)
============================================================
{domain_guidelines}

============================================================
2. LANGUAGE MIRRORING (CRITICAL OVERRIDE)
============================================================
{language_guidelines}
- Your answer is read aloud by a voice that picks its language from the script you write in, so the script rule above is not cosmetic. Getting it wrong makes you unintelligible.
- Write ONLY in Devanagari or Latin script. NEVER Urdu/Arabic, Bengali, Telugu, Tamil or any other, even if the student's message reaches you written in one. A message in Urdu script is a student speaking Hindi: answer in Devanagari.
- Mirror them every single turn. If they switch language mid-conversation, you switch on your very next reply.
- NEVER mention language, script or translation, and never repeat an answer in a second language.

CURRENT SYSTEM TIME & DATE: {system_time}

ALWAYS start your reply with exactly this, unless you are searching:
ANSWER: <your spoken answer>
"""

def ai_loop(ui, headless=False):
    global pending_mode_intro
    time.sleep(2)
    mic_device = None
    recognizer = sr.Recognizer()

    if not headless:
        mic_index = detect_microphone_index()
        mic_device = get_microphone_device(mic_index)

        if mic_device is None:
            print("[FATAL ERROR] No microphone detected.", flush=True)
            ui.set_state("error")
            while True: time.sleep(1)

        print("Calibrating room acoustics...")
        recognizer.pause_threshold = PAUSE_THRESHOLD_NORMAL
        recognizer.non_speaking_duration = 0.3
        with mic_device as source:
            recognizer.adjust_for_ambient_noise(source, duration=1.5)
            # Left ON, then hard-clamped after every listen by clamp_energy().
            # Frozen at startup (what this used to be) is why she would go deaf
            # for a whole session: one noisy moment during those 1.5 seconds --
            # a chair scraping, someone in the room -- calibrates the threshold
            # up near the ceiling and NOTHING said afterwards is ever loud
            # enough to open a phrase again until the process is restarted.
            # Dynamic tracking follows the room back down; the clamp is what
            # stops it drifting above the student's own voice.
            recognizer.dynamic_energy_threshold = True
            # Clamped at BOTH ends, and the ceiling matters more than the floor
            # here. Measured on this device: room noise sits around 525 and
            # peaks near 1730, while speech only reaches about 1316 -- so
            # adjust_for_ambient_noise can easily calibrate ABOVE the student's
            # own voice, and then nothing they say is ever loud enough to open a
            # phrase. The old hard floor of 1500 did exactly that: 95% of speech
            # fell under it. Retune with `python assist.py --calibrate-mic`.
            clamp_energy(recognizer)
            print(f"[MIC] Energy threshold set to {recognizer.energy_threshold:.0f} "
                  f"(allowed {MIC_ENERGY_FLOOR}-{MIC_ENERGY_CEILING}).", flush=True)

    chat_history = load_history()
    if not chat_history: chat_history = []
    session_active = False
    silence_counter = 0
    pending_question = pending_language = ""
    pending_media_kind = None   # set after asking "which song?", see below
    listen_started = [0.0]      # timestamp the current mic read began, for the watchdog

    # RE-TELL: the student's recitation is collected across many turns and marked
    # in one go, so none of this can live inside a single pass of the loop.
    retell_buffer = []          # every chunk they have said since the last verdict
    retell_language = "en"      # language of their last chunk, for acks and the verdict
    retell_silence_from = 0.0   # when the current silence began; 0 = not counting yet
    retell_nudged = False       # the "still listening" reminder has already gone out
    retell_ack_index = 0        # rotates RETELL_ACKS so she does not repeat herself
    if not headless:
        start_mic_watchdog(lambda: listen_started[0])

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
                        woke, pending_question, pending_language = listen_for_wake_word(
                            recognizer, mic_device, asleep=ui.asleep)
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

            # Only show listening state if Liza is completely done talking
            # --- FIX: PREVENT SELF-TALKING LOOP ---
            # If Liza is currently speaking, skip the microphone entirely.
            # This saves Pi CPU, prevents ALSA underruns, and stops the infinite loop.
            # Also skipped while music/video is playing, so song lyrics don't get
            # picked up and misheard as a command.
            if playback_active.is_set() or not audio_queue.empty() or media_active.is_set():
                time.sleep(0.2)
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
                settle = MIC_SETTLE_SEC - (time.time() - last_spoken_at)
                if settle > 0:
                    time.sleep(settle)

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

                listen_started[0] = time.time()
                with mic_device as source:
                    try:
                        audio = recognizer.listen(source, timeout=listen_timeout,
                                                  phrase_time_limit=phrase_limit)
                        listen_started[0] = 0
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
                        if heard_seconds < MIN_SPEECH_SEC:
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
                    
                        dynamic_stt_prompt = STT_SEED_PROMPT
                        for msg in reversed(chat_history):
                            if msg["role"] == "assistant":
                                clean_prompt_text = re.sub(r'EMOTION:\s*\[?[a-zA-Z]+\]?', '', msg["content"])
                                clean_prompt_text = clean_prompt_text.replace('ANSWER:', '').strip()
                                # Never prime Whisper with a script it should not be producing,
                                # otherwise one Urdu reply drags every later turn into Urdu too.
                                if not RE_UNREADABLE_SCRIPT.search(clean_prompt_text):
                                    dynamic_stt_prompt = " ".join(clean_prompt_text.split()[-40:])
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
                        listen_started[0] = 0
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
                        listen_started[0] = 0
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
            chat_history.append({"role": "user", "content": f"Student: {text}"})
            chat_history.append({"role": "assistant", "content": reply})
            chat_history = trim_history(chat_history)
            save_history(chat_history)
            continue

        if media_kind:
            pending_media_kind = None
            ui.set_state("thinking")
            print(f"[MEDIA] {media_kind} request: {media_query}", flush=True)
            hit = search_first_video(media_query, media_kind)
            reply = f"Playing {hit['title']}." if hit else f"I couldn't find a {media_kind} for that."

            audio_queue.put(reply)
            audio_queue.put("[END_OF_RESPONSE]")
            # Let the confirmation finish speaking before mpv claims the audio device.
            deadline = time.time() + 15
            while (playback_active.is_set() or not audio_queue.empty()) and time.time() < deadline:
                time.sleep(0.1)

            if hit:
                try:
                    start_media_playback(media_kind, hit)
                except Exception as exc:
                    print(f"[MEDIA ERROR] {exc}", flush=True)

            chat_history.append({"role": "user", "content": f"Student: {text}"})
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
            education_scope=EDUCATION_SCOPE,
            domain_guidelines=mode_instruction,
            language_guidelines=LANGUAGE_INSTRUCTIONS[user_language],
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
                             if is_retell_eval else f"Student: {text}"})
        chat_history = trim_history(chat_history)

        try:
            result_holder = {}

            def stream_hf(is_search_loop=False):
                try:
                    response_stream = groq_client.chat.completions.create(
                        # Devanagari costs roughly 3x the tokens of the same English, so a
                        # cap tuned for English truncates Hindi mid-word. Brevity is enforced
                        # by the prompt instead; this is only a runaway guard.
                        model="openai/gpt-oss-120b", messages=chat_history, stream=True, max_tokens=800, temperature=0.7
                    )
                    
                    buffer = ""
                    full_response = ""
                    emotion_parsed = False
                    is_searching = False

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
                                try: buffer = full_response.split("ANSWER:")[1].lstrip()
                                except IndexError: buffer = ""
                            else: continue 
                        elif not is_searching:
                            buffer += delta 
                            global current_ai_response
                            current_ai_response = full_response 

                        if not is_searching and emotion_parsed and len(buffer) > 25:
                            sentence_matches = list(RE_SENTENCE_SPLIT.finditer(buffer))
                            if sentence_matches:
                                last_match = sentence_matches[-1].end()
                                new_sentences = buffer[:last_match].strip()
                                buffer = buffer[last_match:]

                                clean = clean_text_for_tts(new_sentences)
                                if clean: audio_queue.put(clean)

                    if not is_searching:
                        if not emotion_parsed: buffer = full_response 
                        if buffer.strip():
                            clean = clean_text_for_tts(buffer.strip())
                            if clean: audio_queue.put(clean)
                    
                    if is_searching:
                        try: search_query = full_response.split("SEARCH:")[1].strip()
                        except IndexError: search_query = full_response.replace("SEARCH:", "").strip()
                        
                        search_query = re.sub(r'EMOTION:.*', '', search_query, flags=re.IGNORECASE)
                        search_query = re.sub(r'ANSWER:.*', '', search_query, flags=re.IGNORECASE)
                        search_query = search_query.replace('[', '').replace(']', '').strip()
                        
                        clean_speech_query = clean_text_for_tts(search_query)
                        search_msg = SEARCH_NOTICES[user_language].format(query=clean_speech_query)
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

                    if not is_search_loop:
                        result_holder['status'] = 'ok'
                        result_holder['text'] = full_response
                        
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
                chat_history = remember_reply(chat_history, full_response)
                chat_history = trim_history(chat_history)

        except Exception as e:
            print(f"HF API Error: {e}", flush=True)
            audio_queue.put("I couldn't reach my brain servers.")

        audio_queue.put("[END_OF_RESPONSE]")
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

