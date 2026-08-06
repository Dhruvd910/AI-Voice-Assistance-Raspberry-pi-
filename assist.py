import os
import sys
import math
import random
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
import traceback
from datetime import datetime


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
            value = value.strip().strip('"\'')
            if key and not os.getenv(key):
                os.environ[key] = value


# Suppress ONNX Runtime warnings
os.environ["ORT_LOGGING_LEVEL"] = "3"
load_dotenv()

import av
import requests
import yt_dlp
import speech_recognition as sr
from groq import Groq
from cartesia import Cartesia
from ddgs import DDGS

# Pillow turns decoded frames into something Tk can blit. Without it video is still
# played, but only its sound: there is no other way to get pixels onto the canvas.
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

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
WAKE_SEED_PROMPT = "Hey Liza. हे लीज़ा।"
# Spelt phonetically rather than listed. Over music the seed prompt has to be
# dropped (it makes Whisper hear the name in the music), and unseeded it writes
# whatever it likes: Liza, Lisa, Leeza and Leiser have all come back from the same
# word. The shape is l + vowels + s/z/j + vowel, with the handful of ordinary
# English words that fit that shape excluded so they cannot wake her by accident.
WAKE_NAME = r'e?l[aeiy]{1,3}[szj][aeiou]{1,2}r?'
WAKE_NOT_NAME = r'laser|lazer|lease|leaser|liaise|lager'
RE_WAKE_WORD = re.compile(
    rf'\b(?:hey|hi|hello|ok|okay|hay)?\s*\b(?!(?:{WAKE_NOT_NAME})\b)(?:{WAKE_NAME})\b'
    # "hey Liza" said quickly comes back as one word, "Heliz", and the boundary
    # between greeting and name never appears for the branch above to find. The
    # greeting has to be spelt out here, which is also what keeps this safe.
    rf'|\b(?:hey|hay|hei|hi|he)l[aeiy]{{1,3}}[szj][aeiou]{{0,2}}r?\b'
    rf'|(?:हे|अरे|ओके|हाय|सुनो)?\s*(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा|लीज़र|लाइज़ा|लिसा)',
    re.IGNORECASE
)

# Music playback sample rate. PyAV decodes YouTube audio and pipes raw PCM to aplay.
MUSIC_SAMPLE_RATE = int(os.getenv("MUSIC_SAMPLE_RATE", "44100"))

# Video playback. This box has no mpv, ffmpeg or browser, so frames are decoded in
# process by PyAV and blitted onto the Tk canvas, exactly like the music path already
# does for audio. A Pi 4 decodes and scales 360p H.264 at roughly 90fps, so a 30fps
# video has headroom; anything larger does not, which is why the format preference
# below asks YouTube for the small muxed stream first.
VIDEO_ENABLED = os.getenv("VIDEO", "1") != "0"
# Frames arriving faster than this are dropped rather than drawn: Tk, not the decoder,
# is the bottleneck, and drawing every frame of a 60fps upload just builds latency.
VIDEO_MAX_FPS = float(os.getenv("VIDEO_MAX_FPS", "30"))

# Barge-in: keep an ear open while Liza is talking or media is playing, so "stop" is
# heard mid-sentence instead of after it. This spends a Whisper call every few seconds
# for as long as something is playing, so set BARGE_IN=0 to trade it back for quota.
BARGE_IN_ENABLED = os.getenv("BARGE_IN", "1") != "0"

# How long the microphone stays open once the wake word has paused a track. Short
# on purpose: the student is watching a frozen picture until they speak or it ends.
MEDIA_SILENCE_S = float(os.getenv("MEDIA_SILENCE_S", "3"))

# Weather panel. The key lives in .env so it never reaches the repo.
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
WEATHER_CITY = os.getenv("WEATHER_CITY", "Delhi,IN")
WEATHER_REFRESH_S = int(os.getenv("WEATHER_REFRESH_S", "900"))

wake_event = threading.Event()
sleep_event = threading.Event()   # 'stop listening': drop the session, go to standby
HISTORY_FILE = os.getenv("HISTORY_FILE", "chat_history.json")
MAX_HISTORY_TURNS = 6

audio_queue = queue.Queue()
playback_active = threading.Event()
stop_playback_event = threading.Event()
active_subprocesses = []
subprocess_lock = threading.Lock()
# The main loop and the barge-in listener share one microphone, and PyAudio does not
# survive two threads opening the same device at once.
mic_lock = threading.Lock()
ui_instance = None
HEADLESS_MODE = False
current_ai_response = ""

PREFERRED_MIC_NAMES = ["USB PnP Sound Device", "USB Audio", "Audio"]

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
# Whisper sometimes returns nothing but zero-width joiners or direction marks. They are
# invisible, so the transcript looks like real speech and gets answered as if it were.
RE_INVISIBLE = re.compile(r'[​-‏‪-‮⁠-⁯﻿]')

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

def trim_history(chat_history):
    if not chat_history: return chat_history
    system_msgs = [m for m in chat_history if m.get("role") == "system"]
    other_msgs = [m for m in chat_history if m.get("role") != "system"]
    if len(other_msgs) > MAX_HISTORY_TURNS * 2:
        other_msgs = other_msgs[-(MAX_HISTORY_TURNS * 2):]
    return system_msgs + other_msgs

def detect_microphone_index():
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
    # The players run aplay outside active_subprocesses, so ending the process
    # without this leaves music or a video still coming out of the speaker.
    for name in ("music_player", "video_player"):
        player = globals().get(name)
        if player is not None:
            try: player.stop()
            except Exception: pass
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

def transcribe(wav_data, prompt, language=None, model=None):
    """Groq STT. With no `language` Whisper auto-detects; pass one to force it."""
    params = {
        "file": ("temp.wav", wav_data),
        "model": model or STT_MODEL,
        "response_format": "verbose_json",
        "temperature": 0.0,
        "prompt": prompt
    }
    if language: params["language"] = language

    result = groq_client.audio.transcriptions.create(**params)
    return (result.text or "").strip(), (getattr(result, "language", "") or "")

def listen_for_wake_word(recognizer, mic_device, over_media=False):
    """(heard, question, language). recognizer.listen blocks on silence, so audio is
    only sent to Whisper when somebody actually speaks near the device.

    Every path must return the full triple: the caller unpacks it, and a silent room
    takes the timeout branch every few seconds.

    `over_media` means the microphone is listening past a track that is playing out
    of the speaker beside it. The seed prompt is dropped for those: priming Whisper
    with "Hey Liza" while it is being fed music is how it starts hearing "Hey Liza"
    in the music, and a false wake there interrupts whatever is playing.
    """
    try:
        # The barge-in listener shares this device; only one of us may hold it open.
        with mic_lock, mic_device as source:
            audio = recognizer.listen(source, timeout=4, phrase_time_limit=4)
    except sr.WaitTimeoutError:
        return False, "", ""
    except Exception as exc:
        print(f"[WAKE ERROR] {exc}", flush=True)
        time.sleep(0.5)
        return False, "", ""

    try:
        wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
        if over_media:
            text, language = transcribe_command(wav_data), ""
        else:
            text, language = transcribe(wav_data, WAKE_SEED_PROMPT, model=WAKE_STT_MODEL)
        match = RE_WAKE_WORD.search(text) if text else None
        if match:
            print(f"[WAKE] Heard: {text}", flush=True)
            # "Hey Liza, what is photosynthesis?" said in one breath: keep the question
            # instead of making the student repeat it.
            question = re.sub(r'\s+', ' ', text[:match.start()] + " " + text[match.end():])
            question = question.strip(" ,.!?।-")
            if len(question.split()) < 2:
                question, language = "", ""
            return True, question, language
    except Exception as exc:
        print(f"[WAKE ERROR] {exc}", flush=True)
    return False, "", ""

# ==========================================
# Music (DuckDuckGo -> YouTube -> PyAV -> aplay)
# ==========================================
# English puts the verb first ("play hanuman chalisa"), Hindi puts it last
# ("hanuman chalisa bajao"), so both orders are matched.
#
# Whisper writes the English word "play" in Devanagari when the speaker is talking
# Hindi, and it is not consistent about which spelling: प्ले, प्रे and प्लै have all
# come back from the same request. Those have to be accepted as leading verbs too,
# or a Hindi speaker asking for a song never triggers playback at all.
PLAY_VERBS_LEADING = (r'play|put\s+on|start\s+playing|bajao|chalao|lagao'
                      r'|प्ले|प्रे|प्लै|प्लेय|बजाओ|चलाओ|लगाओ')
PLAY_VERBS_TRAILING = (r'baja\s*do|bajao|chala\s*do|chalao|laga\s*do|lagao'
                       r'|बजा\s*दो|बजाओ|चला\s*दो|चलाओ|लगा\s*दो|लगाओ')

RE_MUSIC_PLAY = re.compile(
    rf'^(?:please\s+|zara\s+|ज़रा\s+)?(?:{PLAY_VERBS_LEADING})\s+(?P<a>.{{2,80}}?)[\s,.!?।]*$'
    rf'|^(?P<b>.{{2,80}}?)\s+(?:{PLAY_VERBS_TRAILING})[\s,.!?।]*$',
    re.IGNORECASE
)

# "play tum hi ho by arijit singh on youtube" -> the trailing platform is noise.
RE_MUSIC_TAIL = re.compile(
    r'\s*(?:on\s+(?:youtube|yt|spotify)|ओन\s*यूट्यूब|यूट्यूब\s*(?:पर|से)?|यू\s*ट्यूब\s*(?:पर|से)?)\s*$',
    re.IGNORECASE
)
RE_MUSIC_STOP = re.compile(
    r'\b(?:stop|turn\s+off|shut)\b|\bband\s*kar|\bbandh\s*kar|बंद\s*कर|रोक\s*दो|रोको',
    re.IGNORECASE
)
RE_MUSIC_PAUSE = re.compile(r'\b(?:pause|hold\s+on|wait)\b|रोक(?:िए)?\s*ज़रा|पॉज़', re.IGNORECASE)
RE_MUSIC_RESUME = re.compile(r'\b(?:resume|continue|carry\s+on|play\s+again|unpause)\b|फिर\s*से\s*चला|जारी\s*रखो', re.IGNORECASE)

# ==========================================
# Interruption commands
# ==========================================
# Four different "stop"s, and they must not be confused for each other:
#   stop listening -> go back to standby until the wake word or a tap
#   stop Liza      -> shut up, but keep listening
#   close video    -> drop the video overlay
#   stop music     -> stop the track
# Word boundaries keep "listening" from matching the name "Liza", and the name is
# required for the assistant command so it never swallows "stop the music".
# Same phonetic spelling as the wake word: "stop Liza" reaches us through the same
# unseeded transcription, so it sees the same spread of spellings.
LIZA_NAME = rf'(?:(?!(?:{WAKE_NOT_NAME})\b){WAKE_NAME}|लीज़ा|लिज़ा|लीजा|लिजा|लीसा|लीज़र)'
VIDEO_WORD = r'(?:video|vedio|veedio|movie|clip|वीडियो|विडियो|वीडीयो)'
MUSIC_WORD = r'(?:music|song|songs|track|gaana|gana|गाना|गाने|संगीत|म्यूज़िक|म्यूजिक)'
BAND_KARO = r'(?:band|bandh)\s*(?:kar\w*|karo|do)'
# Whisper spells this inconsistently, especially with a soundtrack behind the
# speaker: the same "बंद करो" comes back as बंड, बन्द or बँद from one take to the next.
BAND_DEV = r'(?:बंद|बन्द|बंड|बण्ड|बँद|बद)'

RE_STOP_LISTENING = re.compile(
    rf'\b(?:stop|quit|end|finish|cancel)\s+(?:the\s+)?listen\w*'
    rf'|\bstop\s+listen'
    rf'|\b(?:go\s+to\s+sleep|go\s+sleep|sleep\s+now|goodbye|good\s*bye|bye\s+liza)\b'
    rf'|\b(?:sunna|sunana)\s*(?:band|bandh)\w*|\bso\s*ja(?:o|iye)?\b'
    rf'|सुन(?:ना)?\s*{BAND_DEV}|सो\s*जा(?:ओ|इए)?|{BAND_DEV}\s*करो\s*सुनना',
    re.IGNORECASE
)
# "stop Liza" / "लीज़ा रुको": silence her, but stay in the conversation.
RE_STOP_ASSISTANT = re.compile(
    rf'\b(?:stop|quiet|silence|shut\s*up|sleep|bas|chup|ruko)\b[\s,]*{LIZA_NAME}\b'
    rf'|{LIZA_NAME}\b[\s,]*(?:stop|quiet|silence|shut\s*up|sleep|bas|chup|ruko|{BAND_KARO})'
    rf'|(?:रुको|रुक\s*जाओ|बस|चुप|{BAND_DEV}\s*करो)\s*{LIZA_NAME}'
    rf'|{LIZA_NAME}[\s,]*(?:रुको|रुक\s*जाओ|बस|चुप|{BAND_DEV}\s*करो)',
    re.IGNORECASE
)
RE_STOP_VIDEO = re.compile(
    rf'\b(?:close|stop|exit|quit|end|hide|off|{BAND_KARO})\s+(?:the\s+|this\s+|that\s+)?{VIDEO_WORD}\b'
    rf'|\b{VIDEO_WORD}\s+(?:ko\s+)?(?:{BAND_KARO}|close|stop|off|hata\s*do|bandh)'
    rf'|{VIDEO_WORD}\s*(?:को\s*)?(?:{BAND_DEV}|रोक|हटा)'
    rf'|(?:{BAND_DEV}|रोक\w*|हटा\w*)\s*(?:करो\s*|दो\s*)?{VIDEO_WORD}',
    re.IGNORECASE
)
RE_STOP_MUSIC = re.compile(
    rf'\b(?:close|stop|turn\s+off|shut\s+off|shut|end|{BAND_KARO})\s+(?:the\s+|this\s+)?{MUSIC_WORD}\b'
    rf'|\b{MUSIC_WORD}\s+(?:ko\s+)?(?:{BAND_KARO}|stop|off|close)'
    rf'|{MUSIC_WORD}\s*(?:को\s*)?(?:{BAND_DEV}|रोक)'
    rf'|(?:{BAND_DEV}|रोक\w*)\s*(?:करो\s*|दो\s*)?{MUSIC_WORD}',
    re.IGNORECASE
)
# A bare "stop" with nothing else in the utterance. Whatever is currently making
# noise is what it means, so it is resolved at the call site rather than here.
RE_STOP_BARE = re.compile(
    rf'^\s*(?:just\s+|please\s+|ab\s+)?'
    rf'(?:stop(?:\s+it|\s+now|\s+please)?|quiet|silence|shut\s*up|enough|ruko|ruk\s*jao|bas|chup'
    rf'|{BAND_KARO}|रुको|रुक\s*जाओ|बस|चुप|{BAND_DEV}\s*(?:करो|कर\s*दो)|रोको|रोक\s*दो)'
    rf'\s*[.!?।]*\s*$',
    re.IGNORECASE
)

def detect_interrupt(text):
    """Which 'stop' the student meant, or None.

    Order matters: the specific commands are tried before the bare one, so
    "stop listening" is never mistaken for "stop [whatever is playing]".
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    if RE_STOP_LISTENING.search(stripped): return "stop_listening"
    if RE_STOP_VIDEO.search(stripped): return "stop_video"
    if RE_STOP_MUSIC.search(stripped): return "stop_music"
    if RE_STOP_ASSISTANT.search(stripped): return "stop_assistant"
    if RE_STOP_BARE.match(stripped): return "stop_bare"
    return None

# A question is never a music command, even if it happens to end in a play verb.
RE_QUESTION = re.compile(r'\?|\bwh(?:at|y|o|en|ere|ich)\b|\bhow\b|\bkya\b|\bkyu|\bkaise\b|क्या|क्यों|कैसे|कौन', re.IGNORECASE)

MUSIC_STOPWORDS = {"music", "song", "songs", "a song", "some music", "something",
                   "gaana", "gana", "गाना", "संगीत", "कुछ"}
VIDEO_STOPWORDS = {"video", "a video", "some video", "movie", "clip", "वीडियो", "विडियो"}

# "play video of the solar system" is a video; "play tum hi ho" is a song. The only
# thing separating them is the word "video", so it decides which player gets the
# request, and then has to come back out of the search query: YouTube should be
# searched for "the solar system", not "video of the solar system".
RE_VIDEO_MARK = re.compile(VIDEO_WORD, re.IGNORECASE)
RE_VIDEO_STRIP_LEAD = re.compile(
    rf'^(?:me\s+|us\s+|mujhe\s+|हमें\s+|मुझे\s+)?(?:a\s+|an\s+|the\s+|some\s+|one\s+|any\s+)?'
    rf'{VIDEO_WORD}s?\s*(?:of|for|about|on|named|called|titled|wala|वाला)?\s*',
    re.IGNORECASE
)
RE_VIDEO_STRIP_TAIL = re.compile(
    rf'\s*(?:ka|ki|ke|का|की|के)?\s*{VIDEO_WORD}s?\s*$', re.IGNORECASE)
# "play video on youtube of black holes" puts the platform in the middle of the
# sentence, where the trailing-tail strip cannot reach it.
RE_PLATFORM_ANY = re.compile(
    r'\b(?:on\s+)?(?:youtube|yt|spotify)\b|यूट्यूब|यू\s*ट्यूब', re.IGNORECASE)
RE_LEAD_CONNECTIVE = re.compile(r'^\s*(?:of|about|for|on)\s+', re.IGNORECASE)

# "show me the solar system", "solar system dikhao". Only consulted once the word
# "video" is already in the sentence, because on its own "show me X" is far more
# often a question than a request to play something.
SHOW_VERBS_LEADING = r'show|open|display|dikhao|dikha\s*do|दिखाओ|दिखा\s*दो|ओपन|खोलो'
SHOW_VERBS_TRAILING = r'dikhao|dikha\s*do|dikhaiye|दिखाओ|दिखा\s*दो|दिखाइए'
RE_VIDEO_SHOW = re.compile(
    rf'^(?:please\s+|zara\s+|ज़रा\s+)?(?:{SHOW_VERBS_LEADING})\s+(?P<a>.{{2,80}}?)[\s,.!?।]*$'
    rf'|^(?P<b>.{{2,80}}?)\s+(?:{SHOW_VERBS_TRAILING})[\s,.!?।]*$',
    re.IGNORECASE
)

def _clean_query(match, tail_re=None):
    query = next((g for g in match.groups() if g), "").strip(" ,.!?।")
    query = RE_MUSIC_TAIL.sub("", query).strip(" ,.!?।")
    if tail_re:
        query = tail_re.sub("", query).strip(" ,.!?।")
    return query

def detect_media_command(text, music_active, video_active):
    """('play'|'play_video', query) | ('stop'|'stop_video'|'pause'|'resume', '') | None.

    Only the lenient, context-dependent readings live here: a bare "turn it off"
    means the music when music is playing and nothing at all when it is not. The
    explicit commands ("stop the music", "close the video") are matched earlier by
    detect_interrupt, which does not need anything to be playing to understand them.
    """
    stripped = text.strip()

    if video_active and RE_MUSIC_STOP.search(stripped): return ("stop_video", "")
    if music_active:
        if RE_MUSIC_STOP.search(stripped): return ("stop", "")
        if RE_MUSIC_PAUSE.search(stripped): return ("pause", "")
        if RE_MUSIC_RESUME.search(stripped): return ("resume", "")

    if RE_QUESTION.search(stripped):
        return None

    wants_video = bool(RE_VIDEO_MARK.search(stripped))

    match = RE_MUSIC_PLAY.match(stripped)
    # "show me a video of X" never reaches the play verbs, so give it its own pass.
    if not match and wants_video:
        match = RE_VIDEO_SHOW.match(stripped)
    if not match:
        return None

    if wants_video:
        query = _clean_query(match, RE_VIDEO_STRIP_TAIL)
        query = RE_VIDEO_STRIP_LEAD.sub("", query)
        query = RE_PLATFORM_ANY.sub(" ", query)
        query = RE_LEAD_CONNECTIVE.sub("", re.sub(r'\s+', ' ', query).strip())
        query = query.strip(" ,.!?।")
        if not query or query.lower() in VIDEO_STOPWORDS:
            return None
        return ("play_video", query)

    query = _clean_query(match)
    if not query or query.lower() in MUSIC_STOPWORDS:
        return None
    return ("play", query)

YDL_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "ignoreerrors": True,
            "format": "bestaudio[abr<=96]/bestaudio/best"}

# Video wants sound and pictures from one stream. YouTube's muxed format 18 (360p
# H.264 + AAC) is asked for by name first: the separate DASH streams are mostly AV1,
# which this Pi decodes far too slowly, and pairing a video-only with an audio-only
# stream would mean syncing two containers. Everything after "18/" is a fallback for
# uploads that no longer carry it.
YDL_VIDEO_OPTS = dict(YDL_OPTS, format=(
    "18/best[height<=480][vcodec^=avc1][acodec!=none]"
    "/best[height<=480][acodec!=none]/best[height<=720][acodec!=none]"))
MUSIC_TRIES = 4

RE_TITLE_SPLIT = re.compile(r'\s*[|｜]\s*')

def pretty_title(title, limit=48):
    """YouTube titles are full of emoji, channel names and '| Official Video' tails.
    Keep the part before the first bar so the widget shows the song, not the noise."""
    text = RE_EMOJI.sub('', title or '').strip()
    head = RE_TITLE_SPLIT.split(text)[0].strip()
    text = re.sub(r'\s+', ' ', head or text)
    if len(text) > limit:
        text = text[:limit - 1].rstrip(" ,-") + "…"
    return text

def _title_score(query, title):
    """Fraction of the words asked for that appear in a candidate's title."""
    wanted = set(re.findall(r'\w+', query.lower()))
    if not wanted:
        return 0.0
    have = set(re.findall(r'\w+', (title or "").lower()))
    return len(wanted & have) / len(wanted)

def _playable(info):
    """A search hit is only useful if it actually carries an audio stream."""
    if not isinstance(info, dict): return False
    if info.get("is_live"): return False          # live streams stall the decoder
    return bool(info.get("url"))

def resolve_track(query, want_video=False):
    """Find something playable for `query`, or None.

    DuckDuckGo is tried first, but it rate-limits hard and raises rather than
    returning an empty list, so YouTube's own search is the fallback. Several
    candidates are attempted because the top hit is regularly a live stream, a
    region-blocked upload or an entry with no audio stream, and one bad hit used
    to mean nothing played at all.
    """
    options = YDL_VIDEO_OPTS if want_video else YDL_OPTS
    candidates = []
    try:
        with DDGS() as ddgs:
            for result in ddgs.videos(query, max_results=10):
                url = result.get("content") or ""
                if "youtube.com/watch" in url or "youtu.be/" in url:
                    candidates.append((url, result.get("title") or ""))
    except Exception as exc:
        print(f"[MEDIA] DuckDuckGo unavailable ({exc}); falling back to YouTube search",
              flush=True)

    # Prefer the hits whose title actually looks like what was asked for.
    candidates.sort(key=lambda c: -_title_score(query, c[1]))
    targets = [url for url, _ in candidates[:MUSIC_TRIES]]
    targets.append(f"ytsearch{MUSIC_TRIES}:{query}")

    for target in targets:
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            print(f"[MEDIA] Candidate failed ({str(exc)[:90]})", flush=True)
            continue

        # ignoreerrors turns a dead video into None instead of an exception, so a
        # single unavailable hit no longer throws away the whole batch of results.
        entries = info.get("entries") if isinstance(info, dict) else None
        for candidate in (entries or [info]):
            if _playable(candidate):
                return candidate
            if candidate:
                print(f"[MEDIA] Skipping {candidate.get('title', '?')[:40]!r} "
                      f"(live or no stream)", flush=True)
    return None

class MusicPlayer:
    """Streams YouTube audio to aplay. Decoding happens in-process through PyAV,
    which is the only decoder on this box; there is no mpv or ffmpeg binary."""

    def __init__(self):
        self.state = "stopped"          # stopped | loading | playing | paused
        self.title = ""
        self.last_query = ""            # so the play button can restart a finished track
        self._stop = threading.Event()
        self._user_paused = False
        self._ducked = False
        self._proc = None

    def is_active(self):
        return self.state in ("loading", "playing", "paused")

    def _should_hold(self):
        # Liza talking, the mic listening, or an explicit pause all silence the music.
        return self._user_paused or self._ducked or playback_active.is_set()

    def play(self, query):
        self.stop()
        self._stop.clear()
        self._user_paused = False
        self._ducked = False
        self.title = pretty_title(query)
        self.last_query = query
        self.state = "loading"
        threading.Thread(target=self._worker, args=(query,), daemon=True).start()

    def toggle(self):
        """Pause, resume, or replay the last track once it has finished or been stopped."""
        if not self.is_active():
            if self.last_query:
                print(f"[MUSIC] Replaying {self.last_query!r}", flush=True)
                self.play(self.last_query)
            return
        self._user_paused = not self._user_paused
        self.state = "paused" if self._user_paused else "playing"
        print(f"[MUSIC] {'Paused' if self._user_paused else 'Resumed'}", flush=True)

    def set_paused(self, paused):
        if not self.is_active(): return
        self._user_paused = paused
        self.state = "paused" if paused else "playing"

    def duck(self, ducked):
        """Pause because the assistant needs the speaker or the microphone."""
        self._ducked = ducked
        if self.is_active() and not self._user_paused:
            self.state = "paused" if ducked else "playing"

    def stop(self):
        """Ends playback but keeps last_query, so the play button can start it again."""
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc:
            try: proc.terminate()
            except Exception: pass
        self.state = "stopped"
        self.title = ""

    def _worker(self, query):
        container = proc = None
        try:
            try:
                info = resolve_track(query)
            except Exception as exc:
                print(f"[MUSIC] Search failed for {query!r} ({exc})", flush=True)
                info = None

            if not info:
                print(f"[MUSIC] Nothing playable found for {query!r}", flush=True)
                self.state = "stopped"
                self.title = ""
                audio_queue.put(f"I couldn't find {query} to play.")
                audio_queue.put("[END_OF_RESPONSE]")
                return

            if self._stop.is_set(): return
            self.title = pretty_title(info.get("title") or query)
            print(f"[MUSIC] {info.get('title', query)}", flush=True)

            container = av.open(info["url"], timeout=20)
            audio_stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=MUSIC_SAMPLE_RATE)

            proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(MUSIC_SAMPLE_RATE), "-c", "2"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
            self._proc = proc
            self.state = "paused" if self._should_hold() else "playing"

            for frame in container.decode(audio_stream):
                if self._stop.is_set(): break
                while self._should_hold() and not self._stop.is_set():
                    time.sleep(0.1)
                if self._stop.is_set(): break

                for chunk in resampler.resample(frame):
                    proc.stdin.write(chunk.to_ndarray().tobytes())
            if not self._stop.is_set():
                print(f"[MUSIC] Finished: {self.title}", flush=True)

        except (BrokenPipeError, OSError):
            pass                                    # stopped mid-write
        except Exception as exc:
            print(f"[MUSIC ERROR] {exc}", flush=True)
        finally:
            if container:
                try: container.close()
                except Exception: pass
            if proc:
                try:
                    proc.stdin.close()
                    proc.terminate()
                except Exception: pass
            if self._proc is proc:
                self._proc = None
            self.state = "stopped"
            self.title = ""

music_player = MusicPlayer()

def _fit_box(src_w, src_h, box_w, box_h):
    """Largest size that fits the screen without distorting the picture."""
    if not src_w or not src_h:
        return box_w, box_h
    scale = min(box_w / src_w, box_h / src_h)
    # swscale wants even dimensions; odd ones make it pad and tear the image.
    return max(2, int(src_w * scale) & ~1), max(2, int(src_h * scale) & ~1)

class VideoPlayer:
    """Plays a YouTube video full screen.

    There is no mpv, ffmpeg, omxplayer or browser on this box, so nothing can be
    handed a URL. Instead one muxed stream is demuxed in process: audio goes to
    aplay exactly as music does, and video frames are scaled by swscale and blitted
    onto a Tk overlay. aplay is what keeps time -- writing to a full pipe blocks
    until the speaker has caught up -- so frames are paced against the wall clock
    and any that fall behind are dropped rather than shown late.
    """

    def __init__(self):
        self.state = "stopped"          # stopped | loading | playing | paused
        self.title = ""
        self.last_query = ""
        self._stop = threading.Event()
        self._ducked = False
        self._proc = None

    def is_active(self):
        return self.state in ("loading", "playing", "paused")

    def _should_hold(self):
        # Her voice, or the microphone being open for the student, both silence it.
        return self._ducked or playback_active.is_set()

    def duck(self, ducked):
        """Pause because the assistant has taken the speaker or the microphone.

        The picture freezes rather than closing: the student asked for this video
        and is only interrupting it, so it has to be there to go back to.
        """
        if self._ducked == ducked:
            return
        self._ducked = ducked
        if self.is_active():
            self.state = "paused" if ducked else "playing"
            ui_call(lambda p=ducked: ui_instance.set_video_paused(p))

    def can_show(self):
        """Audio-only playback would be a confusing way to answer 'play a video',
        so the request is refused up front when the screen cannot show one."""
        return VIDEO_ENABLED and PIL_AVAILABLE and not HEADLESS_MODE

    def play(self, query):
        self.stop()
        music_player.stop()             # one thing on the speaker at a time
        self._stop.clear()
        self._ducked = False
        self.title = pretty_title(query)
        self.last_query = query
        self.state = "loading"
        ui_call(lambda t=self.title: ui_instance.show_video(t))
        threading.Thread(target=self._worker, args=(query,), daemon=True).start()

    def stop(self):
        if self.state == "stopped":
            return
        print("[VIDEO] Stopped", flush=True)
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc:
            try: proc.terminate()
            except Exception: pass
        self.state = "stopped"
        self.title = ""
        ui_call(lambda: ui_instance.hide_video())

    def _fail(self, message):
        self.state = "stopped"
        self.title = ""
        ui_call(lambda: ui_instance.hide_video())
        audio_queue.put(message)
        audio_queue.put("[END_OF_RESPONSE]")

    def _worker(self, query):
        container = proc = None
        try:
            try:
                info = resolve_track(query, want_video=True)
            except Exception as exc:
                print(f"[VIDEO] Search failed for {query!r} ({exc})", flush=True)
                info = None

            if not info:
                print(f"[VIDEO] Nothing playable found for {query!r}", flush=True)
                self._fail(f"I couldn't find a video of {query}.")
                return
            if self._stop.is_set(): return

            self.title = pretty_title(info.get("title") or query)
            print(f"[VIDEO] {info.get('title', query)}", flush=True)
            ui_call(lambda t=self.title: ui_instance.set_video_title(t))

            container = av.open(info["url"], timeout=20)
            if not container.streams.video:
                self._fail(f"I couldn't play a video of {query}.")
                return

            v_stream = container.streams.video[0]
            v_stream.thread_type = "AUTO"          # use all four cores to decode
            a_stream = container.streams.audio[0] if container.streams.audio else None

            width, height = _fit_box(v_stream.width, v_stream.height, UI_W, UI_H)
            print(f"[VIDEO] {v_stream.width}x{v_stream.height} -> {width}x{height}", flush=True)

            resampler = None
            if a_stream:
                proc = subprocess.Popen(
                    ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
                     "-r", str(MUSIC_SAMPLE_RATE), "-c", "2"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
                )
                self._proc = proc
                resampler = av.AudioResampler(format="s16", layout="stereo",
                                              rate=MUSIC_SAMPLE_RATE)

            self.state = "paused" if self._should_hold() else "playing"
            streams = [s for s in (v_stream, a_stream) if s is not None]
            time_base = float(v_stream.time_base or 0) or 1 / 30
            start = None
            min_gap = 1.0 / VIDEO_MAX_FPS if VIDEO_MAX_FPS > 0 else 0.0
            last_drawn = 0.0
            dropped = shown = 0

            for packet in container.demux(*streams):
                if self._stop.is_set(): break
                if packet.dts is None: continue     # flush packet at end of stream

                # Her voice, or an open microphone, wins over the video exactly as
                # it does over music. The time spent held is added back to the clock
                # afterwards, or every frame after the pause would count as late and
                # be dropped in a rush to catch up.
                if self._should_hold():
                    held_from = time.monotonic()
                    while self._should_hold() and not self._stop.is_set():
                        time.sleep(0.1)
                    if start is not None:
                        start += time.monotonic() - held_from

                if a_stream is not None and packet.stream is a_stream:
                    for frame in packet.decode():
                        if self._stop.is_set(): break
                        for chunk in resampler.resample(frame):
                            proc.stdin.write(chunk.to_ndarray().tobytes())
                    continue

                for frame in packet.decode():
                    if self._stop.is_set(): break
                    if start is None:
                        start = time.monotonic()

                    stamp = float(frame.pts) * time_base if frame.pts is not None else 0.0
                    lag = (time.monotonic() - start) - stamp

                    # Ahead of the sound: wait for it. The cap keeps a bad timestamp
                    # from parking the decoder for a minute.
                    if lag < -0.005:
                        time.sleep(min(-lag, 0.5))
                        lag = 0.0
                    # Behind, or drawing faster than Tk can keep up: skip this one.
                    elif lag > 0.25:
                        dropped += 1
                        continue

                    now = time.monotonic()
                    if now - last_drawn < min_gap:
                        dropped += 1
                        continue
                    last_drawn = now

                    picture = frame.reformat(width=width, height=height, format="rgb24")
                    ui_call_video_frame((width, height), picture.to_ndarray().tobytes())
                    shown += 1

            if not self._stop.is_set():
                print(f"[VIDEO] Finished: {self.title} ({shown} frames, {dropped} dropped)",
                      flush=True)

        except (BrokenPipeError, OSError):
            pass                                    # stopped mid-write
        except Exception as exc:
            print(f"[VIDEO ERROR] {exc}", flush=True)
        finally:
            if container:
                try: container.close()
                except Exception: pass
            if proc:
                try:
                    proc.stdin.close()
                    proc.terminate()
                except Exception: pass
            if self._proc is proc:
                self._proc = None
            # A video that ran to its end closes the overlay by itself; one that was
            # stopped already had it closed by stop().
            if not self._stop.is_set():
                self.state = "stopped"
                self.title = ""
                ui_call(lambda: ui_instance.hide_video())

video_player = VideoPlayer()

def _is_echo(text):
    """True when what the microphone picked up is Liza's own voice coming back
    out of the speaker, rather than the student saying something."""
    global current_ai_response
    heard = set(re.findall(r'\w+', text.lower()))
    spoken = set(re.findall(r'\w+', current_ai_response.lower()))
    if not heard or not spoken:
        return False
    return len(heard & spoken) / len(heard) > 0.4

def handle_interrupt(action, language=None):
    """Carry out one of detect_interrupt's verdicts.

    Returns "" when the utterance was not a command after all, otherwise what was
    acted on: "assistant", "media" or "sleep". Callers use that to decide whether
    to keep listening afterwards -- stopping a track should hand the speaker back
    and go quiet, not leave her standing there with the microphone open.

    `language` opts into a spoken confirmation, which suits a command she was
    already listening for and not one snatched out of the middle of playback.
    """
    speaking = playback_active.is_set() or not audio_queue.empty()

    if action == "stop_bare":
        # A bare "stop" is a panic button: silence everything that is making a
        # noise, not merely the loudest of them. Stopping only her voice used to
        # leave the music ducked rather than stopped, and ducked music comes
        # straight back the moment the session ends and the speaker is released.
        stopped = ""
        if video_player.is_active():
            video_player.stop()
            stopped = "media"
        if music_player.is_active():
            music_player.stop()
            stopped = "media"
        if speaking:
            print("[STATE] Told to be quiet.", flush=True)
            interrupt_playback()
            stopped = stopped or "assistant"
        return stopped

    if action == "stop_listening":
        print("[STATE] Told to stop listening; back to standby.", flush=True)
        interrupt_playback()
        video_player.stop()
        sleep_event.set()
        return "sleep"

    if action == "stop_video":
        video_player.stop()
        if language: audio_queue.put(MEDIA_REPLIES[language]["video_stop"])
        return "media"

    if action == "stop_music":
        music_player.stop()
        if language: audio_queue.put(MEDIA_REPLIES[language]["stop"])
        return "media"

    if action == "stop_assistant":
        # Only her voice stops here. She stays awake and listening, which is what
        # separates this from "stop listening". Any music was ducked while she
        # spoke, so it is released rather than left silently paused.
        print("[STATE] Told to be quiet.", flush=True)
        interrupt_playback()
        return "assistant"

    return ""

# How sure Whisper has to be that it heard a person before a stop command counts.
# The microphone sits next to the speaker, so while something plays it is listening
# to a soundtrack, and Whisper answers non-speech audio by inventing sentences.
# These two scores are what separate a dreamt-up command from a spoken one.
BARGE_MAX_NO_SPEECH = float(os.getenv("BARGE_MAX_NO_SPEECH", "0.35"))
BARGE_MIN_LOGPROB = float(os.getenv("BARGE_MIN_LOGPROB", "-0.75"))
# The speaker drowns the room, so a command has to be a little louder than the music
# before it is worth sending anywhere. Multiplies the threshold the room calibrated
# to. Kept gentle on purpose: this only saves Whisper calls, while the scores above
# are what actually reject the music, and a high bar here would mean having to shout.
BARGE_MEDIA_GAIN = float(os.getenv("BARGE_MEDIA_GAIN", "1.8"))

def _segment_value(segment, key):
    """Groq returns segments as dicts on some versions and objects on others."""
    if isinstance(segment, dict):
        return segment.get(key)
    return getattr(segment, key, None)

def _command_call(wav_data, language=None):
    params = {
        "file": ("temp.wav", wav_data),
        "model": WAKE_STT_MODEL,
        "response_format": "verbose_json",
        "temperature": 0.0,
    }
    if language: params["language"] = language
    return groq_client.audio.transcriptions.create(**params)

def _sounds_like_speech(result, text):
    segments = getattr(result, "segments", None) or []
    for segment in segments:
        no_speech = _segment_value(segment, "no_speech_prob")
        logprob = _segment_value(segment, "avg_logprob")
        if no_speech is not None and no_speech > BARGE_MAX_NO_SPEECH:
            print(f"[MIC] Not speech ({no_speech:.2f}), ignoring: {text}", flush=True)
            return False
        if logprob is not None and logprob < BARGE_MIN_LOGPROB:
            print(f"[MIC] Low confidence ({logprob:.2f}), ignoring: {text}", flush=True)
            return False
    return True

def transcribe_command(wav_data):
    """Transcribe a short barge-in capture, or return "" if it was not speech.

    Deliberately passes NO prompt. Seeding this with the stop commands taught
    Whisper to hand them straight back whenever it was given music to listen to:
    every track stopped itself within seconds of starting, because the microphone
    hears the speaker.
    """
    result = _command_call(wav_data)
    text = (result.text or "").strip()
    if not text:
        return ""

    # Hindi heard as Urdu, the same way the main loop sees it. Without the re-read
    # "बंद करो" comes back as "بند کرو" and matches no command at all, so spoken
    # Hindi could never stop anything.
    if RE_UNREADABLE_SCRIPT.search(text):
        result = _command_call(wav_data, language="hi")
        text = (result.text or "").strip()
        if not text:
            return ""

    return text if _sounds_like_speech(result, text) else ""

def barge_in_worker(mic_device, energy_threshold):
    """Listens for "stop" while Liza is talking, and only then.

    The main loop ignores the microphone while she speaks, because it would
    otherwise hear the speaker and answer itself, which is why a command used to go
    unheard until the sentence had finished. This thread covers that stretch and
    acts only on stop commands, so a mis-hear costs nothing.

    Music and video are deliberately not covered: the microphone stays shut for
    those, and the wake word in the standby loop is the only way back in. Listening
    through a whole track meant transcribing it, which cost a Whisper call every few
    seconds and gave Whisper thousands of chances to imagine a stop command.
    """
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = False
    # This thread only ever listens while something is playing, so the bar is set
    # above the speaker for its whole life rather than being toggled per burst.
    recognizer.energy_threshold = energy_threshold * BARGE_MEDIA_GAIN
    recognizer.pause_threshold = 0.6            # commands are short; react quickly
    recognizer.non_speaking_duration = 0.3

    while True:
        if not playback_active.is_set():
            time.sleep(0.25)
            continue

        if not mic_lock.acquire(timeout=0.5):
            continue
        try:
            with mic_device as source:
                audio = recognizer.listen(source, timeout=2, phrase_time_limit=4)
        except sr.WaitTimeoutError:
            continue
        except Exception as exc:
            print(f"[BARGE-IN ERROR] {exc}", flush=True)
            time.sleep(0.5)
            continue
        finally:
            mic_lock.release()

        try:
            wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
            text = RE_INVISIBLE.sub("", transcribe_command(wav_data)).strip()
            if not text:
                continue

            # The same inventions the main loop already knows to throw away.
            if text.lower() in HALLUCINATIONS or RE_HALLUCINATION.search(text.lower()):
                print(f"[BARGE-IN] Hallucination, ignoring: {text}", flush=True)
                continue

            action = detect_interrupt(text)
            if not action:
                continue

            # Her own words must not be able to stop her. Short commands skip this
            # check: "stop" is one word, so it overlaps almost any sentence she is
            # in the middle of speaking, and the check would swallow every one.
            if (playback_active.is_set() and len(text.split()) >= 3
                    and _is_echo(text)):
                print(f"[BARGE-IN] Ignoring speaker bleed: {text}", flush=True)
                continue

            print(f"[BARGE-IN] Heard {text!r} -> {action}", flush=True)
            handle_interrupt(action)
        except Exception as exc:
            print(f"[BARGE-IN ERROR] {exc}", flush=True)

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

def ui_call_video_frame(size, data):
    """Hand a decoded frame to the UI. Deliberately not routed through ui_call:
    frames arrive 30 times a second and must not queue up behind each other."""
    if ui_instance is None: return
    push = getattr(ui_instance, "push_video_frame", None)
    if push is not None: push(size, data)

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
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(CARTESIA_SAMPLE_RATE), "-c", "1"],
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
            playback_active.clear()

# ==========================================
# Full-Screen UI
# ==========================================
UI_W, UI_H = 800, 480
FRAME_MS = 70

COL_BG = "#070512"
COL_PANEL = "#110C2B"
COL_PANEL_EDGE = "#2A2159"
COL_TEXT = "#EDEAFF"
COL_TEXT_DIM = "#8B84B8"

MODE_ACCENTS = {"TUTOR": "#A855F7", "CO-TELL": "#38BDF8", "RE-TELL": "#F59E0B"}
MODE_INTROS = {
    "TUTOR": "You are in tutor mode.",
    "CO-TELL": "You are in co-tell mode. Let's study together!",
    "RE-TELL": "You are in re-tell mode. Tell me what you have learned, I am ready to listen."
}

# label, colour, wobble, animation speed, waveform activity
STATE_STYLE = {
    "warmup":    ("WAKING UP",        "#6D5BD0", 0.05, 0.05, 0.10),
    "idle":      ("TAP TO SPEAK",     "#8B5CF6", 0.05, 0.06, 0.08),
    "listening": ("I'M LISTENING...", "#22D3EE", 0.12, 0.16, 1.00),
    "thinking":  ("THINKING...",      "#FBBF24", 0.17, 0.27, 0.35),
    "speaking":  ("SPEAKING",         "#E879F9", 0.10, 0.20, 0.80),
    "capturing": ("LOOKING...",       "#34D399", 0.08, 0.12, 0.25),
    "error":     ("SOMETHING BROKE",  "#FB7185", 0.06, 0.08, 0.12)
}

# scale, colour blend (>=0 mixes toward the background, <0 toward white), x, y offset.
# Tk has no alpha, so the glow is a stack of solid shapes fading into the background.
BLOB_LAYERS = [
    (1.40, 0.87, 0, 0),
    (1.30, 0.76, 0, 0),
    (1.21, 0.63, 0, 0),
    (1.13, 0.48, 0, 0),
    (1.06, 0.30, 0, 0),
    (1.00, 0.00, 0, 0),
    (0.66, -0.22, -13, -15)
]

# Music titles are regularly Devanagari, and the stock Pi image ships DejaVu only,
# which has no Devanagari glyphs, so those titles render as boxes until you run:
#   sudo apt install fonts-noto-devanagari
FONT_PREFERENCE = ("Noto Sans", "Noto Sans Devanagari", "Lohit Devanagari",
                   "Mukta", "Samyak Devanagari", "FreeSans", "DejaVu Sans", "Helvetica")

BLOB_CX, BLOB_CY, BLOB_R = 392, 232, 66
WAVE_Y, WAVE_BARS, WAVE_BAR_W, WAVE_GAP = 58, 21, 3, 5
INFO_X0, INFO_Y0, INFO_X1, INFO_Y1 = 16, 48, 234, 340
# The cards carry a glyph and a name only. They used to explain each mode in three
# lines of small print, which nobody reads twice and which crowded the column.
CARD_X0, CARD_X1, CARD_Y0, CARD_H, CARD_GAP = 548, 786, 48, 62, 9
MUSIC_X0, MUSIC_Y0, MUSIC_X1, MUSIC_Y1 = 16, 352, 234, 464
COL_MUSIC = "#34D399"
COL_VIDEO = "#38BDF8"
COL_STOP = "#FB7185"
COL_MIC = "#22D3EE"
COL_SLEEP = "#8B5CF6"
# Full-width pills down the rest of the mode column. Round enough to read as
# buttons at arm's length on a 5-inch panel, and tall enough to hit with a thumb.
BTN_X0, BTN_X1 = 548, 786
BTN_Y0, BTN_H, BTN_GAP = 278, 52, 13

def _mix(colour, target, t):
    """Blend two #rrggbb colours; Tk canvas has no alpha so glows are faked this way."""
    t = max(0.0, min(1.0, t))
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(target[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

class TutorUI:
    BLOB_POINTS = 44

    def __init__(self, root):
        self.root = root
        self.root.title("AI Tutor")
        self.root.geometry(f"{UI_W}x{UI_H}")
        self.root.attributes('-fullscreen', True)
        self.root.configure(bg=COL_BG)

        self.modes = ["TUTOR", "CO-TELL", "RE-TELL"]
        self.mode_colors = MODE_ACCENTS
        self.current_mode_index = 0
        self.current_mode = self.modes[0]
        self.current_state = "warmup"

        self.phase = 0.0
        self.frame = 0

        self.font_family = self._pick_font()
        self.canvas = tk.Canvas(root, width=UI_W, height=UI_H, bd=0,
                                highlightthickness=0, bg=COL_BG)
        self.canvas.place(x=0, y=0)

        self._build_backdrop()
        self._build_header()
        self._build_info_panel()
        self._build_blob()
        self._build_mode_cards()
        self._build_buttons()
        self._build_music_panel()
        self._build_video_overlay()
        self._refresh_cards()

        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.bind("<Button-1>", self._tap_anywhere)

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
                if name in ("DejaVu Sans", "Helvetica"):
                    print("[UI] No Devanagari font installed; Hindi song titles will show as "
                          "boxes. Fix with: sudo apt install fonts-noto-devanagari", flush=True)
                return name
        return "Helvetica"

    def _font(self, size, bold=False):
        return (self.font_family, size, "bold") if bold else (self.font_family, size)

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _build_backdrop(self):
        # A few faint stars so the dark panel does not read as a dead rectangle.
        random.seed(7)
        for _ in range(46):
            x, y = random.randint(4, UI_W - 4), random.randint(4, UI_H - 4)
            size = random.choice((1, 1, 2))
            shade = random.choice(("#1B1740", "#241E52", "#2E2668"))
            self.canvas.create_oval(x, y, x + size, y + size, fill=shade, outline="")

    def _build_header(self):
        self.state_text_id = self.canvas.create_text(
            BLOB_CX, 28, text="WAKING UP", font=self._font(13, True), fill=COL_TEXT_DIM)

        self.bars = []
        total = WAVE_BARS * (WAVE_BAR_W + WAVE_GAP) - WAVE_GAP
        x = BLOB_CX - total / 2
        for _ in range(WAVE_BARS):
            self.bars.append(self.canvas.create_rectangle(
                x, WAVE_Y - 2, x + WAVE_BAR_W, WAVE_Y + 2, fill=COL_TEXT_DIM, outline=""))
            x += WAVE_BAR_W + WAVE_GAP

    def _build_info_panel(self):
        self._round_rect(INFO_X0, INFO_Y0, INFO_X1, INFO_Y1, 18,
                         fill=COL_PANEL, outline=COL_PANEL_EDGE, width=2)
        mid = (INFO_X0 + INFO_X1) / 2

        self.clock_id = self.canvas.create_text(
            mid, INFO_Y0 + 62, text="--:--", font=self._font(40, True), fill=COL_TEXT)
        self.date_id = self.canvas.create_text(
            mid, INFO_Y0 + 98, text="", font=self._font(10), fill=COL_TEXT_DIM)

        self.canvas.create_line(INFO_X0 + 22, INFO_Y0 + 130, INFO_X1 - 22, INFO_Y0 + 130,
                                fill=COL_PANEL_EDGE)

        self.weather_glyph = []
        self.weather_glyph_at = (INFO_X0 + 48, INFO_Y0 + 196)
        self.temp_id = self.canvas.create_text(
            INFO_X0 + 88, INFO_Y0 + 186, text="--", anchor="w",
            font=self._font(28, True), fill=COL_TEXT)
        self.desc_id = self.canvas.create_text(
            INFO_X0 + 88, INFO_Y0 + 216, text="", anchor="w",
            font=self._font(10), fill=COL_TEXT_DIM)
        self.city_id = self.canvas.create_text(
            mid, INFO_Y1 - 28, text="Weather unavailable" if not WEATHER_API_KEY else WEATHER_CITY,
            font=self._font(9), fill=COL_TEXT_DIM)
        self._draw_weather_glyph("01d")

    def _draw_weather_glyph(self, code):
        for item in self.weather_glyph:
            self.canvas.delete(item)
        self.weather_glyph = []

        cx, cy = self.weather_glyph_at
        c = self.canvas
        kind = code[:2]
        sun = "#FBBF24"
        cloud = "#94A3B8"
        add = self.weather_glyph.append

        if kind == "01":                                   # clear
            add(c.create_oval(cx - 13, cy - 13, cx + 13, cy + 13, fill=sun, outline=""))
            for i in range(8):
                a = math.pi * i / 4
                add(c.create_line(cx + 17 * math.cos(a), cy + 17 * math.sin(a),
                                  cx + 23 * math.cos(a), cy + 23 * math.sin(a),
                                  fill=sun, width=2))
            return
        if kind == "02":                                   # sun behind cloud
            add(c.create_oval(cx - 2, cy - 20, cx + 18, cy, fill=sun, outline=""))
        if kind == "13":                                   # snow
            for i in range(3):
                sx = cx - 10 + i * 10
                add(c.create_line(sx - 4, cy + 12, sx + 4, cy + 20, fill="#E0F2FE", width=2))
                add(c.create_line(sx + 4, cy + 12, sx - 4, cy + 20, fill="#E0F2FE", width=2))
        elif kind in ("09", "10"):                         # rain
            for i in range(3):
                sx = cx - 10 + i * 10
                add(c.create_line(sx, cy + 12, sx - 3, cy + 21, fill="#60A5FA", width=2))
        elif kind == "11":                                 # storm
            add(c.create_polygon(cx + 2, cy + 10, cx - 6, cy + 22, cx, cy + 22,
                                 cx - 4, cy + 32, cx + 8, cy + 18, cx + 2, cy + 18,
                                 fill="#FBBF24", outline=""))
        elif kind == "50":                                 # mist
            for i in range(3):
                add(c.create_line(cx - 16, cy + 4 + i * 7, cx + 16, cy + 4 + i * 7,
                                  fill=cloud, width=2))
            return

        add(c.create_oval(cx - 18, cy - 6, cx + 2, cy + 10, fill=cloud, outline=""))
        add(c.create_oval(cx - 7, cy - 13, cx + 13, cy + 8, fill=cloud, outline=""))
        add(c.create_rectangle(cx - 16, cy + 1, cx + 12, cy + 10, fill=cloud, outline=""))

    def _build_music_panel(self):
        self._round_rect(MUSIC_X0, MUSIC_Y0, MUSIC_X1, MUSIC_Y1, 18,
                         fill=COL_PANEL, outline=COL_PANEL_EDGE, width=2)
        mid = (MUSIC_X0 + MUSIC_X1) / 2

        self.music_head_id = self.canvas.create_text(
            mid, MUSIC_Y0 + 18, text="NOTHING PLAYING",
            font=self._font(9, True), fill=COL_TEXT_DIM)
        self.music_title_id = self.canvas.create_text(
            mid, MUSIC_Y0 + 46, text="Say \u201cplay hanuman chalisa\u201d",
            font=self._font(10), fill=COL_TEXT_DIM, width=MUSIC_X1 - MUSIC_X0 - 24,
            justify="center")

        px, sx, by = mid - 34, mid + 34, MUSIC_Y1 - 26
        self.play_ring = self.canvas.create_oval(px - 18, by - 18, px + 18, by + 18,
                                                 fill=COL_PANEL, outline=COL_PANEL_EDGE,
                                                 width=2, tags="playpause")
        # Play triangle and pause bars overlap; only one is ever visible.
        self.play_tri = self.canvas.create_polygon(px - 5, by - 8, px + 8, by, px - 5, by + 8,
                                                   fill=COL_TEXT_DIM, outline="", tags="playpause")
        self.pause_bars = [
            self.canvas.create_rectangle(px - 6, by - 8, px - 2, by + 8,
                                         fill=COL_TEXT_DIM, outline="", tags="playpause"),
            self.canvas.create_rectangle(px + 2, by - 8, px + 6, by + 8,
                                         fill=COL_TEXT_DIM, outline="", tags="playpause")
        ]
        self.music_stop_ring = self.canvas.create_oval(sx - 18, by - 18, sx + 18, by + 18,
                                                       fill=COL_PANEL, outline=COL_PANEL_EDGE,
                                                       width=2, tags="musicstop")
        self.music_stop_icon = self.canvas.create_rectangle(sx - 6, by - 6, sx + 6, by + 6,
                                                            fill=COL_TEXT_DIM, outline="",
                                                            tags="musicstop")
        self.canvas.tag_bind("playpause", "<Button-1>", self.toggle_music)
        self.canvas.tag_bind("musicstop", "<Button-1>", self.stop_music)
        self._music_shown = None
        self._refresh_music()

    def _refresh_music(self):
        state, title = music_player.state, music_player.title
        if (state, title) == self._music_shown:
            return
        self._music_shown = (state, title)

        active = state in ("loading", "playing", "paused")
        head = {"loading": "FINDING TRACK", "playing": "NOW PLAYING",
                "paused": "PAUSED"}.get(state, "NOTHING PLAYING")
        shade = COL_MUSIC if active else _mix(COL_TEXT_DIM, COL_BG, 0.55)

        self.canvas.itemconfig(self.music_head_id, text=head,
                               fill=COL_MUSIC if active else COL_TEXT_DIM)
        self.canvas.itemconfig(self.music_title_id,
                               text=title or "Say \u201cplay hanuman chalisa\u201d",
                               fill=COL_TEXT if active else COL_TEXT_DIM,
                               font=self._font(10 if len(title) < 30 else 9))

        # Playing -> offer pause; otherwise offer play.
        playing = state == "playing"
        self.canvas.itemconfig(self.play_tri, state="hidden" if playing else "normal", fill=shade)
        for bar in self.pause_bars:
            self.canvas.itemconfig(bar, state="normal" if playing else "hidden", fill=shade)
        self.canvas.itemconfig(self.play_ring,
                               outline=COL_MUSIC if active else COL_PANEL_EDGE)
        self.canvas.itemconfig(self.music_stop_ring,
                               outline=COL_MUSIC if active else COL_PANEL_EDGE)
        self.canvas.itemconfig(self.music_stop_icon, fill=shade)

    def toggle_music(self, event=None):
        music_player.toggle()
        self._refresh_music()
        return "break"

    def stop_music(self, event=None):
        music_player.stop()
        self._refresh_music()
        return "break"

    # ---------- full-screen video ----------
    def _build_video_overlay(self):
        """A black frame covering the whole window, hidden until a video plays.

        It is a separate widget rather than a canvas item so that showing it also
        hides the animated blob underneath: while a video is on screen the whole
        Tk canvas stops being redrawn, which is CPU this Pi needs for decoding.
        """
        self.video_visible = False
        self._video_photo = None
        self._video_frame = None
        self._video_title = ""
        self._video_draw_queued = False
        self._video_lock = threading.Lock()

        self.video_frame_widget = tk.Frame(self.root, bg="#000000",
                                           width=UI_W, height=UI_H)
        self.video_label = tk.Label(self.video_frame_widget, bg="#000000", bd=0,
                                    highlightthickness=0)
        self.video_label.place(relx=0.5, rely=0.5, anchor="center")
        self.video_caption = tk.Label(self.video_frame_widget, bg="#000000",
                                      fg=COL_TEXT_DIM, bd=0,
                                      font=self._font(10, True), text="")
        self.video_caption.place(relx=0.5, y=UI_H - 16, anchor="s")

        # Anywhere on the video closes it, matching the "tap to wake" idiom.
        for widget in (self.video_frame_widget, self.video_label, self.video_caption):
            widget.bind("<Button-1>", self.close_video)

    def show_video(self, title=""):
        self.video_visible = True
        self._video_photo = None
        self._video_frame = None
        self._video_title = title or ""
        self.video_label.configure(image="", text="Loading video…",
                                   fg=COL_TEXT_DIM, font=self._font(12))
        self.video_caption.configure(text=self._video_title, fg=COL_TEXT_DIM)
        self.video_frame_widget.place(x=0, y=0, width=UI_W, height=UI_H)
        self.video_frame_widget.lift()

    def set_video_title(self, title):
        self._video_title = title or ""
        if self.video_visible:
            self.video_caption.configure(text=self._video_title)

    def set_video_paused(self, paused):
        """The picture freezes when she takes the microphone, so say why."""
        if not self.video_visible:
            return
        self.video_caption.configure(
            text="PAUSED · LISTENING" if paused else self._video_title,
            fg=COL_MIC if paused else COL_TEXT_DIM)

    def hide_video(self):
        self.video_visible = False
        self.video_frame_widget.place_forget()
        self.video_label.configure(image="")
        self._video_photo = None
        self._video_frame = None

    def close_video(self, event=None):
        video_player.stop()
        return "break"          # a tap on the video must not also wake her

    def push_video_frame(self, size, data):
        """Called from the decoder thread. Latest frame wins: if Tk has not drawn
        the previous one yet, this overwrites it instead of queueing another
        redraw, so a slow frame can never build a backlog of stale pictures."""
        with self._video_lock:
            self._video_frame = (size, data)
            if self._video_draw_queued:
                return
            self._video_draw_queued = True
        ui_call(self._draw_video_frame)

    def _draw_video_frame(self):
        with self._video_lock:
            frame = self._video_frame
            self._video_draw_queued = False
        if frame is None or not self.video_visible:
            return

        size, data = frame
        try:
            image = Image.frombytes("RGB", size, data)
            # Repainting the existing photo is markedly cheaper than building a new
            # one 30 times a second, so it is only rebuilt when the size changes.
            if (self._video_photo is None
                    or (self._video_photo.width(), self._video_photo.height()) != size):
                self._video_photo = ImageTk.PhotoImage(image)
                self.video_label.configure(image=self._video_photo, text="")
            else:
                self._video_photo.paste(image)
        except Exception as exc:
            print(f"[VIDEO UI ERROR] {exc}", flush=True)

    def _build_blob(self):
        self.rings = [self.canvas.create_oval(0, 0, 1, 1, outline=COL_PANEL_EDGE, width=1)
                      for _ in range(3)]
        # Outermost glow first so the solid body lands on top of it.
        self.blob_layers = [self.canvas.create_polygon(0, 0, 1, 1, 2, 2, smooth=True, outline="")
                            for _ in range(len(BLOB_LAYERS))]
        self.highlights = [
            self.canvas.create_oval(0, 0, 1, 1, fill="", outline=""),
            self.canvas.create_oval(0, 0, 1, 1, fill="", outline=""),
        ]

        eye_dx, eye_dy = 25, 14
        self.eyes_open = [
            self.canvas.create_oval(0, 0, 1, 1, fill="#140F2E", outline=""),
            self.canvas.create_oval(0, 0, 1, 1, fill="#140F2E", outline=""),
        ]
        self.eyes_happy = [
            self.canvas.create_arc(0, 0, 1, 1, start=0, extent=180, style="arc",
                                   outline="#140F2E", width=3),
            self.canvas.create_arc(0, 0, 1, 1, start=0, extent=180, style="arc",
                                   outline="#140F2E", width=3),
        ]
        self.eye_offset = (eye_dx, eye_dy)
        self.mouth = self.canvas.create_arc(0, 0, 1, 1, start=200, extent=140, style="arc",
                                            outline="#140F2E", width=3)
        self.mouth_open = self.canvas.create_oval(0, 0, 1, 1, fill="#140F2E", outline="")

    def _mode_glyph(self, kind, cx, cy, colour):
        c = self.canvas
        items = [c.create_oval(cx - 19, cy - 19, cx + 19, cy + 19,
                               fill=_mix(colour, COL_BG, 0.74), outline="")]
        if kind == "TUTOR":                      # mortarboard
            items.append(c.create_polygon(cx, cy - 9, cx + 13, cy - 3, cx, cy + 3, cx - 13, cy - 3,
                                          fill=colour, outline=""))
            items.append(c.create_rectangle(cx - 7, cy + 1, cx + 7, cy + 8, fill=colour, outline=""))
            items.append(c.create_line(cx + 13, cy - 3, cx + 13, cy + 8, fill=colour, width=2))
        elif kind == "CO-TELL":                  # two chat bubbles
            items.append(self._round_rect(cx - 14, cy - 12, cx + 4, cy + 1, 4,
                                          fill=colour, outline=""))
            items.append(self._round_rect(cx - 3, cy - 3, cx + 14, cy + 10, 4,
                                          fill=_mix(colour, "#FFFFFF", 0.35), outline=""))
        else:                                    # head speaking
            items.append(c.create_oval(cx - 12, cy - 11, cx + 2, cy + 3, fill=colour, outline=""))
            items.append(c.create_polygon(cx - 12, cy + 2, cx + 2, cy + 2, cx + 2, cy + 11,
                                          cx - 12, cy + 11, fill=colour, outline=""))
            for i, r in enumerate((6, 10)):
                items.append(c.create_arc(cx + 2 - r, cy - r, cx + 2 + r, cy + r,
                                          start=-55, extent=110, style="arc",
                                          outline=colour, width=2))
        return items

    def _build_mode_cards(self):
        self.canvas.create_text(CARD_X0 + (CARD_X1 - CARD_X0) / 2, 26, text="CHOOSE MODE",
                                font=self._font(12, True), fill=COL_TEXT)
        self.cards = []
        for i, mode in enumerate(self.modes):
            y0 = CARD_Y0 + i * (CARD_H + CARD_GAP)
            y1 = y0 + CARD_H
            accent = MODE_ACCENTS[mode]
            tag = f"mode{i}"
            mid_y = (y0 + y1) / 2

            body = self._round_rect(CARD_X0, y0, CARD_X1, y1, 12,
                                    fill=COL_PANEL, outline=COL_PANEL_EDGE, width=2, tags=tag)
            glyph = self._mode_glyph(mode, CARD_X0 + 34, mid_y, accent)
            title = self.canvas.create_text(CARD_X0 + 64, mid_y, text=f"{mode} MODE",
                                            anchor="w", font=self._font(13, True),
                                            fill=accent, tags=tag)
            for item in glyph:
                self.canvas.itemconfig(item, tags=tag)
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, idx=i: self.set_mode(idx))
            self.cards.append({"body": body, "title": title, "accent": accent})

    # ---------- buttons ----------
    def _icon_mic(self, cx, cy, tag):
        """Microphone: capsule, cradle arc, stand."""
        return [
            self._round_rect(cx - 4, cy - 9, cx + 4, cy + 2, 4, fill="", outline="", tags=tag),
            self.canvas.create_arc(cx - 8, cy - 6, cx + 8, cy + 6, start=200, extent=140,
                                   style="arc", outline="", width=2, tags=tag),
            self.canvas.create_line(cx, cy + 6, cx, cy + 10, fill="", width=2, tags=tag),
        ]

    def _icon_mic_off(self, cx, cy, tag):
        items = self._icon_mic(cx, cy, tag)
        # The slash is what makes this read as "off" rather than "on".
        items.append(self.canvas.create_line(cx - 10, cy + 9, cx + 10, cy - 11,
                                             fill="", width=2, tags=tag))
        return items

    def _icon_stop(self, cx, cy, tag):
        return [self._round_rect(cx - 7, cy - 7, cx + 7, cy + 7, 3,
                                 fill="", outline="", tags=tag)]

    def _build_buttons(self):
        """Three pill buttons filling the rest of the mode column.

        Each one keeps its own item ids so _animate can recolour it: the icons are
        drawn with empty fills here and get their colour from the state pass, which
        is also what dims a button that would do nothing if pressed.
        """
        specs = [
            ("mic",   "TAP TO SPEAK",   COL_MIC,   self._icon_mic,     self.wake_up),
            ("sleep", "STOP LISTENING", COL_SLEEP, self._icon_mic_off, self.stop_listening),
            ("stop",  "STOP TALKING",   COL_STOP,  self._icon_stop,    self.stop_speaking),
        ]

        self.buttons = {}
        for index, (tag, label, accent, draw_icon, handler) in enumerate(specs):
            y0 = BTN_Y0 + index * (BTN_H + BTN_GAP)
            y1 = y0 + BTN_H
            mid_y = (y0 + y1) / 2

            body = self._round_rect(BTN_X0, y0, BTN_X1, y1, BTN_H / 2,
                                    fill=COL_PANEL, outline=COL_PANEL_EDGE,
                                    width=2, tags=tag)
            halo = self._round_rect(BTN_X0 - 3, y0 - 3, BTN_X1 + 3, y1 + 3, (BTN_H + 6) / 2,
                                    fill="", outline="", width=2, tags=tag)
            self.canvas.tag_lower(halo, body)
            icon = draw_icon(BTN_X0 + 34, mid_y, tag)
            text = self.canvas.create_text(BTN_X0 + 62, mid_y, text=label, anchor="w",
                                           font=self._font(11, True), fill=COL_TEXT_DIM,
                                           tags=tag)

            self.canvas.tag_bind(tag, "<ButtonPress-1>",
                                 lambda e, t=tag: self._press_button(t, True))
            self.canvas.tag_bind(tag, "<ButtonRelease-1>",
                                 lambda e, t=tag: self._press_button(t, False))
            self.canvas.tag_bind(tag, "<Button-1>", handler, add="+")

            self.buttons[tag] = {"body": body, "halo": halo, "icon": icon, "text": text,
                                 "accent": accent, "pressed": False}

    def _press_button(self, tag, down):
        """Visible feedback on touch, so a press never feels like it was missed."""
        button = self.buttons.get(tag)
        if button is None: return
        button["pressed"] = down
        self._refresh_buttons()      # repaint them all, so enabled states stay right

    def _paint_button(self, tag, enabled=True, active=False):
        """One button's colours for the current frame.

        enabled=False means pressing it would do nothing right now, so it fades
        back to the panel; active is the pulsing "this is what is happening" look.
        """
        button = self.buttons[tag]
        accent = button["accent"]
        pressed = button["pressed"]

        if not enabled:
            body_fill, edge, ink = COL_PANEL, COL_PANEL_EDGE, _mix(COL_TEXT_DIM, COL_BG, 0.5)
        elif pressed:
            body_fill, edge, ink = _mix(accent, COL_BG, 0.55), accent, COL_TEXT
        elif active:
            body_fill, edge, ink = _mix(accent, COL_BG, 0.72), accent, COL_TEXT
        else:
            body_fill, edge, ink = COL_PANEL, _mix(accent, COL_BG, 0.45), accent

        self.canvas.itemconfig(button["body"], fill=body_fill, outline=edge)
        self.canvas.itemconfig(button["text"], fill=ink)
        for item in button["icon"]:
            # An arc drawn in "arc" style is stroked, so its colour is the outline;
            # lines and filled shapes both take it on the fill.
            if self.canvas.type(item) == "arc":
                self.canvas.itemconfig(item, outline=ink)
            else:
                self.canvas.itemconfig(item, fill=ink)
        return button

    # ---------- runtime ----------
    def _blob_pts(self, cx, cy, r, phase, wobble):
        pts = []
        for i in range(self.BLOB_POINTS):
            a = 2 * math.pi * i / self.BLOB_POINTS
            rr = r * (1 + wobble * (0.62 * math.sin(3 * a + phase)
                                    + 0.38 * math.sin(5 * a - phase * 1.27)))
            pts.append(cx + rr * math.cos(a))
            pts.append(cy + rr * math.sin(a) * 1.12)
        return pts

    def _animate(self):
        # The video covers every one of these shapes, so redrawing them is pure
        # waste at the exact moment the decoder wants the CPU. Keep the loop alive
        # at a slow tick so it resumes the moment the video goes away.
        if self.video_visible:
            self.root.after(FRAME_MS * 4, self._animate)
            return

        label, colour, wobble, speed, activity = STATE_STYLE.get(
            self.current_state, STATE_STYLE["idle"])
        self.phase += speed
        self.frame += 1

        breathe = 1 + 0.035 * math.sin(self.phase * 0.85)
        bob = 3.0 * math.sin(self.phase * 0.7)
        cy = BLOB_CY + bob

        for item, (scale, blend, offx, offy) in zip(self.blob_layers, BLOB_LAYERS):
            self.canvas.coords(item, self._blob_pts(BLOB_CX + offx, cy + offy,
                                                    BLOB_R * scale * breathe,
                                                    self.phase + scale, wobble))
            target = COL_BG if blend >= 0 else "#FFFFFF"
            self.canvas.itemconfig(item, fill=_mix(colour, target, abs(blend)))

        for i, ring in enumerate(self.rings):
            rw = BLOB_R * (1.45 + i * 0.32)
            rh = 11 + i * 7
            ry = cy + BLOB_R * 1.25
            self.canvas.coords(ring, BLOB_CX - rw, ry - rh, BLOB_CX + rw, ry + rh)
            self.canvas.itemconfig(ring, outline=_mix(colour, COL_BG, 0.62 + i * 0.12))

        hx, hy = BLOB_CX - BLOB_R * 0.42, cy - BLOB_R * 0.52
        self.canvas.coords(self.highlights[0], hx - 11, hy - 8, hx + 11, hy + 8)
        self.canvas.itemconfig(self.highlights[0], fill=_mix(colour, "#FFFFFF", 0.62))
        self.canvas.coords(self.highlights[1], hx + 15, hy - 15, hx + 22, hy - 8)
        self.canvas.itemconfig(self.highlights[1], fill=_mix(colour, "#FFFFFF", 0.45))

        self._draw_face(cy, colour, activity)

        peak = 0.0
        for i, bar in enumerate(self.bars):
            swing = math.sin(self.phase * 2.3 + i * 0.62) ** 2
            h = 2 + 15 * activity * (0.28 + 0.72 * swing)
            peak = max(peak, h)
            x0, _, x1, _ = self.canvas.coords(bar)
            self.canvas.coords(bar, x0, WAVE_Y - h / 2, x1, WAVE_Y + h / 2)
            self.canvas.itemconfig(bar, fill=_mix(colour, COL_BG, 0.55 - 0.4 * swing * activity))

        self.canvas.itemconfig(self.state_text_id, text=label,
                               fill=_mix(colour, COL_TEXT, 0.35))

        self._refresh_music()
        self._refresh_buttons()

        self.root.after(FRAME_MS, self._animate)

    def _refresh_buttons(self):
        """Each button shows whether pressing it would do anything right now."""
        listening = self.current_state == "listening"
        talking = playback_active.is_set() or not audio_queue.empty()
        awake = listening or talking or self.current_state == "thinking"

        self._paint_button("mic", enabled=True, active=listening)
        # Nothing to stop listening to until she is actually awake.
        self._paint_button("sleep", enabled=awake, active=False)
        self._paint_button("stop", enabled=talking, active=talking)

        # A soft pulse around the live button, so "she is listening" reads from
        # across the room rather than needing the label to be studied.
        pulse = 0.5 + 0.5 * math.sin(self.phase * 1.6)
        for tag, lit in (("mic", listening), ("stop", talking)):
            button = self.buttons[tag]
            self.canvas.itemconfig(
                button["halo"],
                outline=_mix(button["accent"], COL_BG, 0.30 + 0.55 * (1 - pulse)) if lit else "")

    def _draw_face(self, cy, colour, activity):
        dx, dy = self.eye_offset
        ey = cy - dy
        happy = self.current_state in ("idle", "warmup", "speaking")
        blink = happy and (self.frame % 78) < 4

        for i, sign in enumerate((-1, 1)):
            cxe = BLOB_CX + sign * dx
            self.canvas.coords(self.eyes_open[i], cxe - 5, ey - 7, cxe + 5, ey + 7)
            self.canvas.coords(self.eyes_happy[i], cxe - 9, ey - 6, cxe + 9, ey + 10)
            show_happy = happy or blink
            self.canvas.itemconfig(self.eyes_open[i], state="hidden" if show_happy else "normal")
            self.canvas.itemconfig(self.eyes_happy[i], state="normal" if show_happy else "hidden")

        my = cy + 16
        if self.current_state == "speaking":
            gap = 4 + 7 * abs(math.sin(self.phase * 2.6))
            self.canvas.coords(self.mouth_open, BLOB_CX - 11, my - gap / 2, BLOB_CX + 11, my + gap / 2)
            self.canvas.itemconfig(self.mouth_open, state="normal")
            self.canvas.itemconfig(self.mouth, state="hidden")
        else:
            self.canvas.itemconfig(self.mouth_open, state="hidden")
            self.canvas.itemconfig(self.mouth, state="normal")
            if self.current_state == "thinking":
                self.canvas.coords(self.mouth, BLOB_CX - 10, my - 8, BLOB_CX + 10, my + 4)
                self.canvas.itemconfig(self.mouth, start=20, extent=140)
            elif self.current_state == "error":
                self.canvas.coords(self.mouth, BLOB_CX - 11, my - 2, BLOB_CX + 11, my + 14)
                self.canvas.itemconfig(self.mouth, start=20, extent=140)
            else:
                self.canvas.coords(self.mouth, BLOB_CX - 13, my - 12, BLOB_CX + 13, my + 6)
                self.canvas.itemconfig(self.mouth, start=200, extent=140)

    def _refresh_cards(self):
        for i, card in enumerate(self.cards):
            chosen = i == self.current_mode_index
            accent = card["accent"]
            self.canvas.itemconfig(card["body"],
                                   fill=_mix(accent, COL_BG, 0.86) if chosen else COL_PANEL,
                                   outline=accent if chosen else COL_PANEL_EDGE,
                                   width=2 if chosen else 1)
            self.canvas.itemconfig(card["title"],
                                   fill=accent if chosen else _mix(accent, COL_BG, 0.35))

    def set_weather(self, reading):
        self.canvas.itemconfig(self.temp_id, text=f"{reading['temp']}\u00b0C")
        self.canvas.itemconfig(self.desc_id,
                               text=f"{reading['desc']}, feels {reading['feels']}\u00b0")
        self.canvas.itemconfig(self.city_id,
                               text=f"{reading['city']}  \u00b7  {reading['humidity']}% humidity")
        self._draw_weather_glyph(reading["icon"])

    def _tick_clock(self):
        now = datetime.now()
        self.canvas.itemconfig(self.clock_id, text=now.strftime("%H:%M"))
        self.canvas.itemconfig(self.date_id, text=now.strftime("%A, %d %B"))
        self.root.after(1000, self._tick_clock)

    def set_state(self, state, caption=None):
        if state not in STATE_STYLE:
            state = "idle"

        if state in ["idle", "listening", "warmup"] and (playback_active.is_set() or not audio_queue.empty()):
            return

        self.current_state = state

    # Tapping these must never wake her; STOP LISTENING setting wake_event on the way
    # through is what made the session restart a moment after it was dismissed.
    NON_WAKE_TAGS = ("sleep", "stop", "playpause", "musicstop")

    def _tap_anywhere(self, event=None):
        if any(tag in self.NON_WAKE_TAGS for tag in self.canvas.gettags("current")):
            return
        self.wake_up(event)

    def wake_up(self, event=None):
        print("[UI] Screen tapped! Waking up...", flush=True)
        wake_event.set()

    def stop_listening(self, event=None):
        print("[UI] Stop listening, going back to standby.", flush=True)
        sleep_event.set()
        return "break"          # do not let the tap fall through and re-wake her

    def stop_speaking(self, event=None):
        if playback_active.is_set() or not audio_queue.empty():
            print("[UI] Stop tapped, cutting the reply short.", flush=True)
            interrupt_playback()
        return "break"          # do not let the tap fall through and re-wake her

    def set_mode(self, index):
        if index == self.current_mode_index:
            return
        self.current_mode_index = index
        self.current_mode = self.modes[index]
        self._refresh_cards()

        interrupt_playback()
        audio_queue.put(MODE_INTROS[self.current_mode])
        audio_queue.put("[END_OF_RESPONSE]")

    def cycle_mode(self, event=None):
        self.set_mode((self.current_mode_index + 1) % len(self.modes))

class HeadlessUI:
    def __init__(self):
        self.current_state = "idle"
        self.current_mode = "TUTOR"
        self.video_visible = False

    def set_state(self, state_type, caption=None): self.current_state = state_type

    # There is no screen to draw on; VideoPlayer.can_show() refuses video before it
    # gets this far, and these keep any stray call from raising.
    def show_video(self, title=""): pass
    def set_video_title(self, title): pass
    def set_video_paused(self, paused): pass
    def hide_video(self): pass
    def push_video_frame(self, size, data): pass

# ==========================================
# Core AI Functions
# ==========================================
# Bilingual seed so Whisper is not biased towards English on the first turn.
STT_SEED_PROMPT = ("Hey Liza, explain the concept clearly. नमस्ते लीज़ा, यह concept समझाओ। "
                   "Play Tum Hi Ho by Arijit Singh. हनुमान चालीसा बजाओ।")

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
    r'three, four|assistant is a professor|avoid casual|thanks for watching|'
    r'सब्सक्राइब करें|वीडियो पसंद आया',
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

MODE_INSTRUCTIONS = {
    "TUTOR": """TUTOR MODE ACTIVE: You are a subject expert answering a student out loud.

ANSWER FIRST, ALWAYS. Your opening sentence must be the direct answer to what was asked. Never open with preamble, never restate the question, never define the topic before answering it.

MATCH THE LENGTH TO THE QUESTION. This is the most important rule:
- Quick questions (conversions, arithmetic, spelling, dates, definitions, yes or no, greetings, small talk) get ONE sentence and then you STOP. "180 centimetres is about 5 feet 11 inches." That is the entire answer. Do not explain the method unless asked.
- Only when the student actually asks to understand something ("how does X work", "why does X happen", "explain X") do you add up to 3 more sentences: how it works, and one concrete example.

NEVER announce your structure. Do not say "The core principle is", "The mechanism is", "In a real-world example", and do not number your points. Just answer the way a knowledgeable person would in conversation.

If a question needs a fact you are not certain of, use the SEARCH protocol below. Guessing is the worst possible answer.""",
    
    "CO-TELL": "CO-TELL MODE ACTIVE: You are a collaborative study partner. STRICT RULE: YOU MUST SPEAK A MAXIMUM OF 2 SENTENCES TOTAL. Sentence 1: A brief validation or partial hint. Sentence 2: Ask the user a specific question to test their knowledge. NEVER explain the full concept. Wait for them to answer.",
    
    "RE-TELL": """RE-TELL MODE ACTIVE: You are an examiner evaluating the user step-by-step as they teach you. 
    STRICT RULE: YOU MUST SPEAK A MAXIMUM OF 2 SENTENCES TOTAL.
    Analyze the user's latest explanation:
    - IF CORRECT: Sentence 1: Briefly validate that they are right. Sentence 2: Ask them to elaborate, provide an example, or explain the next logical step to keep them talking.
    - IF INCORRECT OR INCOMPLETE: Sentence 1: Gently point out the specific mistake or missing detail. Sentence 2: Tell them exactly which area they need to focus on or correct."""
}

LANGUAGE_INSTRUCTIONS = {
    "en": "DETECTED LANGUAGE: ENGLISH. Reply in English only.",

    "hi": "DETECTED LANGUAGE: HINDI. Reply in Hindi, written in Devanagari script only. "
          "NEVER write Hindi words in Latin letters. Common English technical terms may stay in Latin script.",

    "hinglish": "DETECTED LANGUAGE: HINGLISH (Hindi mixed with English). Reply in the same natural Hinglish mix. "
                "CRITICAL SCRIPT RULE: write every Hindi word in Devanagari and keep English words in Latin script, "
                "for example: 'यह concept बहुत simple है, इसे ऐसे समझो.' NEVER write Hindi words in Latin letters."
}

MEDIA_REPLIES = {
    "en": {"play": "Playing {query}.", "stop": "Stopped the music.",
           "pause": "Paused.", "resume": "Resuming.",
           "video": "Playing a video of {query}.", "video_stop": "Closed the video.",
           "no_video": "I can't show video on this screen, sorry."},
    "hi": {"play": "{query} चला रहे हैं।", "stop": "संगीत बंद कर दिया।",
           "pause": "रोक दिया।", "resume": "फिर से चला रहे हैं।",
           "video": "{query} का वीडियो चला रहे हैं।", "video_stop": "वीडियो बंद कर दिया।",
           "no_video": "इस स्क्रीन पर वीडियो नहीं दिखा सकते।"},
    "hinglish": {"play": "{query} play कर रहे हैं।", "stop": "Music बंद कर दिया।",
                 "pause": "Pause कर दिया।", "resume": "फिर से play कर रहे हैं।",
                 "video": "{query} का video play कर रहे हैं।",
                 "video_stop": "Video बंद कर दिया।",
                 "no_video": "इस screen पर video नहीं दिखा सकते।"}
}

SEARCH_NOTICES = {
    "en": "Let me check the web for {query}.",
    "hi": "एक सेकंड, वेब पर देखते हैं।",
    "hinglish": "एक सेकंड, web पर check करते हैं।"
}

UNIVERSAL_SYSTEM_PROMPT = """You are "Liza", an advanced, highly capable AI Assistant. You have LIVE internet access.
CURRENT SYSTEM TIME & DATE: {system_time}

============================================================
1. CURRENT TEACHING MODE (CRITICAL OVERRIDE)
============================================================
{domain_guidelines}

============================================================
2. LANGUAGE MIRRORING (CRITICAL OVERRIDE)
============================================================
{language_guidelines}
- Your answer is read aloud by a voice that picks its language from the script you write in, so the script rule above is not cosmetic. Getting it wrong makes you unintelligible.
- You may ONLY write in Devanagari or Latin script. NEVER answer in Urdu/Arabic script, nor in Bengali, Telugu, Tamil or any other script, even if the student's message reaches you written in one. A message in Urdu script is a student speaking Hindi: answer it in Devanagari.
- Mirror the student every single turn. If they switch language mid-conversation, you switch on your very next reply, no matter what language the earlier turns used.
- NEVER mention language, script or translation. NEVER repeat the same answer in a second language.

============================================================
3. UNIVERSAL BEHAVIORAL MATRIX
============================================================
- The STT Forgiveness Rule (CRITICAL): The user is speaking through a microphone. Ignore all typos, phonetic misspellings, and grammar issues. NEVER correct the user. Just infer the meaning and answer.
- The Time Override: You already know the exact time and date from the SYSTEM TIME provided above. DO NOT search the web for the time or date. 
- The Knowledge Fallback: You MUST NEVER say "I don't have real-time access", "I cannot browse the internet", or "I am an AI". 

============================================================
4. MEDIA PROTOCOL (STRICT)
============================================================
If the student is asking you to PLAY a song, bhajan, mantra, aarti or any music, you MUST bypass normal conversation and output EXACTLY AND ONLY this:
MUSIC: <song name and artist>

If the student asks to WATCH or SEE something, or says the word "video", they want a video instead. Output EXACTLY AND ONLY this:
VIDEO: <what the video should show>

The difference matters: MUSIC plays sound only, VIDEO fills the screen with a picture. Listen for the word "video", "dekhna", "dikhao" or "watch" to tell them apart. Strip the word "video" itself out of the tag: it is a search term, not part of the subject.

The microphone mangles these requests badly, so read through the noise: "pili kong hihobay arijit singh" is "play Tum Hi Ho by Arijit Singh", and "प्ले हनुमान चालिजा" is "play Hanuman Chalisa". If the student clearly wants to hear or watch something, emit the tag.

NEVER tell a student to search YouTube themselves, and never explain how to find a song or a video. You can play it. Emit the tag.

CRITICAL EXAMPLES:
User: "play tum hi ho by arijit singh on youtube"
Your Output: MUSIC: Tum Hi Ho Arijit Singh

User: "प्रे हनुमान चालीसा"
Your Output: MUSIC: Hanuman Chalisa

User: "play video of the solar system"
Your Output: VIDEO: solar system explained

User: "मुझे प्रकाश संश्लेषण का वीडियो दिखाओ"
Your Output: VIDEO: photosynthesis explained in Hindi

============================================================
5. SEARCH PROTOCOL (STRICT)
============================================================
If you need to trigger a search, you MUST NOT use the EMOTION or ANSWER tags. You must bypass normal conversation and output EXACTLY AND ONLY this:
SEARCH: <your optimized query>

YOU MUST SEARCH, NOT GUESS, whenever the answer depends on:
- A specific named organisation, school, college, company, shop or product
- A specific person who is not world famous
- Anything local, current or changing: prices, scores, results, news, timings, opening hours, releases
- Any fact you would be filling in from plausibility rather than knowledge

Inventing plausible-sounding specifics is the single worst failure you can make. If you catch yourself about to name a placement company, a founding year, a fee, a rank, a score or a statistic that you are not certain of, STOP and output the SEARCH tag instead.

CRITICAL EXAMPLES OF SEARCHING:
User: "What is the temperature in New Delhi?"
Your Output: SEARCH: current temperature in New Delhi weather

User: "Tell me something about ABS Engineering College."
Your Output: SEARCH: ABS Engineering College courses admission details

User: "Who won the match yesterday?"
Your Output: SEARCH: match result yesterday

DO NOT add conversational filler. ONLY output the SEARCH tag.

============================================================
6. VOICE & FORMATTING CONSTRAINTS
============================================================
- You are a VOICE assistant. Your output must be spoken aloud.
- Stop talking the moment the question is answered. A short answer is a good answer; padding a one-line reply into a paragraph is a failure, not thoroughness.
- DO NOT use bullet points, numbered lists, markdown formatting, or complex punctuation.
- If you are NOT searching, ALWAYS start your response EXACTLY like this:
EMOTION: [emotion]
ANSWER: <your spoken answer>
"""

def ai_loop(ui, headless=False):
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
        recognizer.pause_threshold = 1.5
        recognizer.non_speaking_duration = 0.5
        with mic_lock, mic_device as source:
            recognizer.adjust_for_ambient_noise(source, duration=1.5)
            recognizer.dynamic_energy_threshold = False
            if recognizer.energy_threshold < 1500: recognizer.energy_threshold = 1500
        quiet_room_threshold = recognizer.energy_threshold

        # Started only now, so it inherits the threshold this room was calibrated to.
        if BARGE_IN_ENABLED:
            threading.Thread(target=barge_in_worker,
                             args=(mic_device, recognizer.energy_threshold),
                             daemon=True).start()
            print("[STATE] Barge-in listening enabled.", flush=True)

    chat_history = load_history()
    if not chat_history: chat_history = []
    session_active = False
    silence_counter = 0
    pending_question = pending_language = ""

    while True:
        if not headless:
            
            # --- STANDBY LOOP: waits for screen tap, uses 0% CPU! ---
            if not session_active:
                
                # FIX: Thread-safe state update for Tkinter!
                if hasattr(ui, 'root'):
                    ui.root.after(0, lambda: ui.set_state("idle"))
                else:
                    ui.set_state("idle")
                    
                # Standby releases the speaker: anything that was paused for the
                # conversation picks up where it left off.
                music_player.duck(False)
                video_player.duck(False)

                print("[STATE] In Standby Mode. "
                      + ("Say 'Hey Liza' or tap the screen..." if WAKE_WORD_ENABLED
                         else "Tap the screen to wake up..."), flush=True)

                while not wake_event.is_set() and not pending_question:
                    if not WAKE_WORD_ENABLED:
                        time.sleep(0.1)
                        continue

                    # While a track plays, the wake word is the only way in. Ask for
                    # a louder voice then, since the speaker is holding the room
                    # above the quiet-room threshold all by itself.
                    over_media = music_player.is_active() or video_player.is_active()
                    recognizer.energy_threshold = (quiet_room_threshold * BARGE_MEDIA_GAIN
                                                   if over_media else quiet_room_threshold)

                    woke, pending_question, pending_language = listen_for_wake_word(
                        recognizer, mic_device, over_media=over_media)
                    if woke:
                        break

                recognizer.energy_threshold = quiet_room_threshold
                wake_event.clear()
                sleep_event.clear()
                # Our turn on the speaker and the microphone: whatever is playing
                # holds until the conversation is over.
                music_player.duck(True)
                video_player.duck(True)
                session_active = True
                silence_counter = 0

            # Only show listening state if Liza is completely done talking
            # --- FIX: PREVENT SELF-TALKING LOOP ---
            # If Liza is currently speaking, skip the microphone entirely.
            # This saves Pi CPU, prevents ALSA underruns, and stops the infinite loop.
            if sleep_event.is_set():
                sleep_event.clear()
                wake_event.clear()      # a tap on the button must not double as a wake
                session_active = False
                pending_question = pending_language = ""
                interrupt_playback()
                continue

            if playback_active.is_set() or not audio_queue.empty():
                time.sleep(0.2)
                continue

            if pending_question:
                # Said in the same breath as the wake word, so skip straight to answering.
                text, stt_language = pending_question, pending_language
                pending_question = pending_language = ""
                silence_counter = 0
                print(f"[TRANSCRIPT] {text}", flush=True)
            else:
                ui.set_state("listening")
                # Something is paused waiting for this, so the microphone gives it
                # back quickly instead of sitting open for the usual half minute.
                media_waiting = music_player.is_active() or video_player.is_active()
                listen_timeout = MEDIA_SILENCE_S if media_waiting else 5
                print(f"[STATE] Listening for speech..."
                      f"{' (media paused)' if media_waiting else ''}", flush=True)

                try:
                    # Held only for the recording itself: the barge-in listener needs
                    # the device back the moment Liza starts speaking again.
                    with mic_lock, mic_device as source:
                        audio = recognizer.listen(source, timeout=listen_timeout,
                                                  phrase_time_limit=25)

                    wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
                    silence_counter = 0 # Reset silence timer when sound is heard

                    dynamic_stt_prompt = STT_SEED_PROMPT
                    for msg in reversed(chat_history):
                        if msg["role"] == "assistant":
                            clean_prompt_text = re.sub(r'EMOTION:\s*\[?[a-zA-Z]+\]?', '', msg["content"])
                            clean_prompt_text = clean_prompt_text.replace('ANSWER:', '').strip()
                            # Never prime Whisper with a script it should not be producing,
                            # otherwise one Urdu reply drags every later turn into Urdu too.
                            if not RE_UNREADABLE_SCRIPT.search(clean_prompt_text):
                                recent = " ".join(clean_prompt_text.split()[-30:])
                                # Keep the seed alongside it, otherwise Whisper loses the
                                # music vocabulary as soon as there is any chat history.
                                dynamic_stt_prompt = f"{STT_SEED_PROMPT} {recent}"
                            break

                    text, stt_language = transcribe(wav_data, dynamic_stt_prompt)

                    # A transcript of nothing but zero-width marks is silence, not a
                    # question, so strip them before anything else looks at the text.
                    text = RE_INVISIBLE.sub("", text).strip()

                    # Hindi heard as Urdu (or any other Indic script): re-read the same audio
                    # forced to Hindi so we get Devanagari the voice can actually speak.
                    if RE_UNREADABLE_SCRIPT.search(text):
                        print(f"[STT] Heard '{stt_language}' in an unreadable script, re-reading as Hindi...", flush=True)
                        text, stt_language = transcribe(wav_data, STT_SEED_PROMPT, language="hi")

                    lower_text = text.lower().strip()

                    if lower_text in HALLUCINATIONS or RE_HALLUCINATION.search(lower_text):
                        text = ""

                    # --- ACOUSTIC ECHO CANCELLATION & INTERRUPTION ---
                    if playback_active.is_set() and text:
                        if _is_echo(text):
                            print(f"[ECHO DETECTED] Ignoring speaker bleed: {text}", flush=True)
                            continue

                        print(f"[INTERRUPT DETECTED] User said: {text}", flush=True)
                        interrupt_playback()

                    print(f"[TRANSCRIPT] {text if text else '[empty]'}", flush=True)
                    if not text: continue

                except sr.WaitTimeoutError:
                    if playback_active.is_set() or not audio_queue.empty():
                        continue

                    # Nothing said while a track waits: give it the speaker back
                    # immediately rather than making the student sit through the
                    # full standby timeout with the picture frozen.
                    if media_waiting:
                        print(f"[STATE] Silent for {MEDIA_SILENCE_S}s, resuming playback...",
                              flush=True)
                        session_active = False
                        silence_counter = 0
                        continue

                    silence_counter += 1
                    if silence_counter >= 6: # ~30 seconds of quiet thinking time
                        print("[STATE] No interaction for 30 seconds. Returning to Standby Mode...", flush=True)
                        session_active = False
                        silence_counter = 0
                    continue
                except Exception as e:
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

        user_language = detect_user_language(text, stt_language)

        # --- STOP commands: never reach the model ---
        # The barge-in listener catches these mid-playback; this is the same
        # decision for the ordinary case where she was already waiting for a turn.
        # "stop listening" leaves sleep_event set for the top of the loop to act on,
        # so the button and the spoken command drop the session by the same path.
        interrupt = detect_interrupt(text)
        if interrupt:
            outcome = handle_interrupt(interrupt, language=user_language)
            if outcome == "media":
                # Silencing a track ends the turn. Staying in the session would
                # leave her listening at somebody who only wanted quiet -- and the
                # wake word fires on "stop the music, Liza", so this path is
                # reached by a plain request to stop, not just mid-conversation.
                audio_queue.put("[END_OF_RESPONSE]")
                session_active = False
                continue
            if outcome:
                continue

        # --- MUSIC & VIDEO: handled locally, never reach the model ---
        command = detect_media_command(text, music_player.is_active(),
                                       video_player.is_active())
        if command:
            action, query = command
            print(f"[MEDIA] Command: {action} {query!r}", flush=True)
            if action == "play_video":
                if not video_player.can_show():
                    audio_queue.put(MEDIA_REPLIES[user_language]["no_video"])
                else:
                    video_player.play(query)
                    audio_queue.put(MEDIA_REPLIES[user_language]["video"].format(query=query))
            elif action == "play":
                music_player.play(query)
                audio_queue.put(MEDIA_REPLIES[user_language]["play"].format(query=query))
            elif action == "stop":
                music_player.stop()
                audio_queue.put(MEDIA_REPLIES[user_language]["stop"])
            elif action == "stop_video":
                video_player.stop()
                audio_queue.put(MEDIA_REPLIES[user_language]["video_stop"])
            else:
                music_player.set_paused(action == "pause")
                audio_queue.put(MEDIA_REPLIES[user_language][action])
            audio_queue.put("[END_OF_RESPONSE]")
            session_active = False          # hand the speaker back to the media
            continue

        # --- 2. THINK & STREAM ---
        ui.set_state("thinking")
        mode_instruction = MODE_INSTRUCTIONS.get(ui.current_mode, MODE_INSTRUCTIONS["TUTOR"])

        print(f"[LANGUAGE] heard={stt_language or 'n/a'} -> replying in {user_language}", flush=True)

        current_time = datetime.now().strftime("%I:%M %p, %A, %B %d, %Y")
        dynamic_system_prompt = UNIVERSAL_SYSTEM_PROMPT.format(
            domain_guidelines=mode_instruction,
            language_guidelines=LANGUAGE_INSTRUCTIONS[user_language],
            system_time=current_time
        )

        if chat_history and chat_history[0].get("role") == "system":
            chat_history[0]["content"] = dynamic_system_prompt
        else:
            chat_history.insert(0, {"role": "system", "content": dynamic_system_prompt})

        chat_history.append({"role": "user", "content": f"Student: {text}"})
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
                    is_music = False
                    is_video = False

                    for chunk in response_stream:
                        if stop_playback_event.is_set():
                            break

                        delta = chunk.choices[0].delta.content
                        if delta is None: continue
                        full_response += delta

                        if "VIDEO:" in full_response:
                            is_video = True
                            continue

                        if "MUSIC:" in full_response:
                            is_music = True
                            continue

                        if "SEARCH:" in full_response:
                            is_searching = True
                            continue

                        if is_music or is_video:
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

                    if is_music or is_video:
                        tag = "VIDEO:" if is_video else "MUSIC:"
                        wanted = full_response.split(tag)[1].strip()
                        wanted = re.sub(r'(?:EMOTION|ANSWER):.*', '', wanted, flags=re.IGNORECASE)
                        wanted = wanted.replace('[', '').replace(']', '').strip(" \"'.,!?।")
                        if not wanted:
                            audio_queue.put("I couldn't work out what to play.")
                        elif is_video:
                            print(f"[VIDEO] Model resolved request to {wanted!r}", flush=True)
                            if video_player.can_show():
                                video_player.play(wanted)
                                audio_queue.put(
                                    MEDIA_REPLIES[user_language]["video"].format(query=wanted))
                            else:
                                audio_queue.put(MEDIA_REPLIES[user_language]["no_video"])
                        else:
                            print(f"[MUSIC] Model resolved request to {wanted!r}", flush=True)
                            music_player.play(wanted)
                            audio_queue.put(
                                MEDIA_REPLIES[user_language]["play"].format(query=wanted))
                        if not is_search_loop:
                            result_holder['status'] = 'media'
                            result_holder['text'] = full_response
                        return

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
                            "content": f"Here are the live web search results:\n{search_context}\n\nBased ONLY on this information, answer the question. If the results do not contain the answer, just say 'I couldn't find the exact data online right now.' DO NOT guess or change the subject. The results may be in English, but you MUST still obey the LANGUAGE MIRRORING rules and answer in the student's language and script. Provide the final spoken answer starting with EMOTION: and ANSWER:"
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
                if result_holder.get('status') == 'media':
                    chat_history.pop()          # keep the mangled request out of the history
                    session_active = False      # hand the speaker back to the media
                else:
                    full_response = result_holder.get('text', '').strip()
                    chat_history.append({"role": "assistant", "content": full_response})
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

    if not CARTESIA_API_KEY:
        print("[WARNING] CARTESIA_API_KEY is not set. Liza will not be able to speak.", flush=True)
    if not CARTESIA_VOICE_ID and not all(VOICE_IDS.values()):
        print("[WARNING] No Cartesia voice configured. Set CARTESIA_VOICE_ID in .env "
              "(see `python assist.py --list-voices`).", flush=True)

    player_thread = threading.Thread(target=audio_player_worker, daemon=True)
    player_thread.start()

    if WEATHER_API_KEY:
        threading.Thread(target=weather_worker, daemon=True).start()
    else:
        print("[WARNING] WEATHER_API_KEY is not set; the weather panel will stay blank.", flush=True)

    HEADLESS = ("--headless" in sys.argv) or (os.getenv("HEADLESS") == "1")
    HEADLESS_MODE = HEADLESS          # VideoPlayer.can_show() reads this

    if VIDEO_ENABLED and not PIL_AVAILABLE and not HEADLESS:
        print("[WARNING] Pillow is not installed, so videos cannot be shown. "
              "Fix with: assist/bin/pip install pillow", flush=True)

    if HEADLESS:
        app_ui = HeadlessUI()
        ui_instance = app_ui
        ai_thread = threading.Thread(target=ai_loop, args=(app_ui,), daemon=True)
        ai_thread.start()
        try:
            while True: time.sleep(1)
        except KeyboardInterrupt: pass
    else:
        root = tk.Tk()
        app_ui = TutorUI(root)
        ui_instance = app_ui
        ai_thread = threading.Thread(target=ai_loop, args=(app_ui,), daemon=True)
        ai_thread.start()
        root.mainloop()

