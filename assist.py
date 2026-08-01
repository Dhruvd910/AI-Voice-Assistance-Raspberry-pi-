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
RE_WAKE_WORD = re.compile(
    r'\b(?:hey|hi|hello|ok|okay|hay|a)?\s*(?:liza|lisa|leeza|lizza|eliza|elisa|lija)\b'
    r'|(?:हे|अरे|ओके|हाय|सुनो)?\s*(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)',
    re.IGNORECASE
)

# Music playback sample rate. PyAV decodes YouTube audio and pipes raw PCM to aplay.
MUSIC_SAMPLE_RATE = int(os.getenv("MUSIC_SAMPLE_RATE", "44100"))

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

def listen_for_wake_word(recognizer, mic_device):
    """(heard, question, language). recognizer.listen blocks on silence, so audio is
    only sent to Whisper when somebody actually speaks near the device.

    Every path must return the full triple: the caller unpacks it, and a silent room
    takes the timeout branch every few seconds.
    """
    try:
        with mic_device as source:
            audio = recognizer.listen(source, timeout=4, phrase_time_limit=4)
    except sr.WaitTimeoutError:
        return False, "", ""
    except Exception as exc:
        print(f"[WAKE ERROR] {exc}", flush=True)
        time.sleep(0.5)
        return False, "", ""

    try:
        wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
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
RE_MUSIC_PLAY = re.compile(
    r'^(?:please\s+|zara\s+|ज़रा\s+)?(?:play|put\s+on|start\s+playing)\s+(?P<a>.{2,80}?)[\s,.!?।]*$'
    r'|^(?P<b>.{2,80}?)\s+(?:baja\s*do|bajao|chala\s*do|chalao|laga\s*do|lagao)[\s,.!?।]*$'
    r'|^(?P<c>.{2,80}?)\s+(?:बजा\s*दो|बजाओ|चला\s*दो|चलाओ|लगा\s*दो|लगाओ)[\s,.!?।]*$',
    re.IGNORECASE
)
RE_MUSIC_STOP = re.compile(
    r'\b(?:stop|turn\s+off|shut)\b|\bband\s*kar|\bbandh\s*kar|बंद\s*कर|रोक\s*दो|रोको',
    re.IGNORECASE
)
RE_MUSIC_PAUSE = re.compile(r'\b(?:pause|hold\s+on|wait)\b|रोक(?:िए)?\s*ज़रा|पॉज़', re.IGNORECASE)
RE_MUSIC_RESUME = re.compile(r'\b(?:resume|continue|carry\s+on|play\s+again|unpause)\b|फिर\s*से\s*चला|जारी\s*रखो', re.IGNORECASE)
# A question is never a music command, even if it happens to end in a play verb.
RE_QUESTION = re.compile(r'\?|\bwh(?:at|y|o|en|ere|ich)\b|\bhow\b|\bkya\b|\bkyu|\bkaise\b|क्या|क्यों|कैसे|कौन', re.IGNORECASE)

MUSIC_STOPWORDS = {"music", "song", "songs", "a song", "some music", "something",
                   "gaana", "gana", "गाना", "संगीत", "कुछ"}

def detect_music_command(text, music_active):
    """('play', query) | ('stop'|'pause'|'resume', '') | None."""
    stripped = text.strip()

    if music_active:
        if RE_MUSIC_STOP.search(stripped): return ("stop", "")
        if RE_MUSIC_PAUSE.search(stripped): return ("pause", "")
        if RE_MUSIC_RESUME.search(stripped): return ("resume", "")

    if RE_QUESTION.search(stripped):
        return None

    match = RE_MUSIC_PLAY.match(stripped)
    if not match:
        return None

    query = next((g for g in match.groups() if g), "").strip(" ,.!?।")
    if not query or query.lower() in MUSIC_STOPWORDS:
        return None
    return ("play", query)

YDL_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "format": "bestaudio[abr<=96]/bestaudio/best"}
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

def resolve_track(query):
    """Find something playable for `query`, or None.

    DuckDuckGo is tried first, but it rate-limits hard and raises rather than
    returning an empty list, so YouTube's own search is the fallback. Several
    candidates are attempted because the top hit is regularly a live stream, a
    region-blocked upload or an entry with no audio stream, and one bad hit used
    to mean nothing played at all.
    """
    candidates = []
    try:
        with DDGS() as ddgs:
            for result in ddgs.videos(query, max_results=10):
                url = result.get("content") or ""
                if "youtube.com/watch" in url or "youtu.be/" in url:
                    candidates.append((url, result.get("title") or ""))
    except Exception as exc:
        print(f"[MUSIC] DuckDuckGo unavailable ({exc})", flush=True)

    # Prefer the hits whose title actually looks like what was asked for.
    candidates.sort(key=lambda c: -_title_score(query, c[1]))
    targets = [url for url, _ in candidates[:MUSIC_TRIES]]
    targets.append(f"ytsearch{MUSIC_TRIES}:{query}")

    for target in targets:
        try:
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:
            print(f"[MUSIC] Candidate failed ({str(exc)[:90]})", flush=True)
            continue

        entries = info.get("entries") if isinstance(info, dict) else None
        for candidate in (entries or [info]):
            if _playable(candidate):
                return candidate
            if candidate:
                print(f"[MUSIC] Skipping {candidate.get('title', '?')[:40]!r} "
                      f"(live or no audio)", flush=True)
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
MODE_BLURBS = {
    "TUTOR": "Concepts explained\nstep by step.",
    "CO-TELL": "We talk it through\ntogether.",
    "RE-TELL": "You teach me,\nI correct you."
}
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
INFO_X0, INFO_Y0, INFO_X1, INFO_Y1 = 16, 104, 234, 340
CARD_X0, CARD_X1, CARD_Y0, CARD_H, CARD_GAP = 548, 786, 48, 90, 10
MUSIC_X0, MUSIC_Y0, MUSIC_X1, MUSIC_Y1 = 16, 352, 234, 464
COL_MUSIC = "#34D399"
MIC_CX, MIC_CY, MIC_R = 292, 392, 24
SLEEP_CX = 392
STOP_CX, STOP_CY = 492, 392
COL_STOP = "#FB7185"

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
        self._refresh_cards()

        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.bind("<Button-1>", self.wake_up)

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
            mid, INFO_Y0 + 46, text="--:--", font=self._font(38, True), fill=COL_TEXT)
        self.date_id = self.canvas.create_text(
            mid, INFO_Y0 + 78, text="", font=self._font(10), fill=COL_TEXT_DIM)

        self.canvas.create_line(INFO_X0 + 22, INFO_Y0 + 100, INFO_X1 - 22, INFO_Y0 + 100,
                                fill=COL_PANEL_EDGE)

        self.weather_glyph = []
        self.weather_glyph_at = (INFO_X0 + 48, INFO_Y0 + 142)
        self.temp_id = self.canvas.create_text(
            INFO_X0 + 86, INFO_Y0 + 132, text="--", anchor="w",
            font=self._font(26, True), fill=COL_TEXT)
        self.desc_id = self.canvas.create_text(
            INFO_X0 + 86, INFO_Y0 + 160, text="", anchor="w",
            font=self._font(10), fill=COL_TEXT_DIM)
        self.city_id = self.canvas.create_text(
            mid, INFO_Y1 - 22, text="Weather unavailable" if not WEATHER_API_KEY else WEATHER_CITY,
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

            body = self._round_rect(CARD_X0, y0, CARD_X1, y1, 12,
                                    fill=COL_PANEL, outline=COL_PANEL_EDGE, width=2, tags=tag)
            glyph = self._mode_glyph(mode, CARD_X0 + 34, y0 + CARD_H / 2, accent)
            title = self.canvas.create_text(CARD_X0 + 62, y0 + 26, text=f"{mode} MODE",
                                            anchor="w", font=self._font(12, True),
                                            fill=accent, tags=tag)
            blurb = self.canvas.create_text(CARD_X0 + 62, y0 + 50, text=MODE_BLURBS[mode],
                                            anchor="w", justify="left",
                                            font=self._font(9), fill=COL_TEXT_DIM, tags=tag)
            for item in glyph:
                self.canvas.itemconfig(item, tags=tag)
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, idx=i: self.set_mode(idx))
            self.cards.append({"body": body, "title": title, "blurb": blurb, "accent": accent})

    def _build_buttons(self):
        # Speak
        self.mic_glow = self.canvas.create_oval(MIC_CX - 34, MIC_CY - 34, MIC_CX + 34, MIC_CY + 34,
                                                fill="", outline="", width=2, tags="mic")
        self.mic_ring = self.canvas.create_oval(MIC_CX - MIC_R, MIC_CY - MIC_R,
                                                MIC_CX + MIC_R, MIC_CY + MIC_R,
                                                fill=COL_PANEL, outline=COL_PANEL_EDGE,
                                                width=2, tags="mic")
        self._round_rect(MIC_CX - 5, MIC_CY - 12, MIC_CX + 5, MIC_CY + 2, 5,
                         fill=COL_TEXT, outline="", tags="mic")
        self.canvas.create_arc(MIC_CX - 11, MIC_CY - 8, MIC_CX + 11, MIC_CY + 8,
                               start=200, extent=140, style="arc",
                               outline=COL_TEXT, width=2, tags="mic")
        self.canvas.create_line(MIC_CX, MIC_CY + 8, MIC_CX, MIC_CY + 13,
                                fill=COL_TEXT, width=2, tags="mic")
        self.canvas.create_text(MIC_CX, MIC_CY + 42, text="TAP TO SPEAK",
                                font=self._font(9, True), fill=COL_TEXT_DIM, tags="mic")
        self.canvas.tag_bind("mic", "<Button-1>", self.wake_up)

        # Stop listening: drop the session and go back to standby.
        self.sleep_ring = self.canvas.create_oval(SLEEP_CX - MIC_R, MIC_CY - MIC_R,
                                                  SLEEP_CX + MIC_R, MIC_CY + MIC_R,
                                                  fill=COL_PANEL, outline=COL_PANEL_EDGE,
                                                  width=2, tags="sleep")
        self._round_rect(SLEEP_CX - 5, MIC_CY - 12, SLEEP_CX + 5, MIC_CY + 2, 5,
                         fill=COL_TEXT_DIM, outline="", tags="sleep")
        self.canvas.create_arc(SLEEP_CX - 11, MIC_CY - 8, SLEEP_CX + 11, MIC_CY + 8,
                               start=200, extent=140, style="arc",
                               outline=COL_TEXT_DIM, width=2, tags="sleep")
        self.sleep_slash = self.canvas.create_line(SLEEP_CX - 13, MIC_CY + 12,
                                                   SLEEP_CX + 13, MIC_CY - 14,
                                                   fill=COL_STOP, width=3, tags="sleep")
        self.sleep_label = self.canvas.create_text(SLEEP_CX, MIC_CY + 42, text="STOP LISTENING",
                                                   font=self._font(9, True),
                                                   fill=COL_TEXT_DIM, tags="sleep")
        self.canvas.tag_bind("sleep", "<Button-1>", self.stop_listening)

        # Stop: only meaningful while Liza is talking, so it stays dimmed otherwise.
        self.stop_ring = self.canvas.create_oval(STOP_CX - MIC_R, STOP_CY - MIC_R,
                                                 STOP_CX + MIC_R, STOP_CY + MIC_R,
                                                 fill=COL_PANEL, outline=COL_PANEL_EDGE,
                                                 width=2, tags="stop")
        self.stop_icon = self._round_rect(STOP_CX - 8, STOP_CY - 8, STOP_CX + 8, STOP_CY + 8, 3,
                                          fill=COL_TEXT_DIM, outline="", tags="stop")
        self.stop_label = self.canvas.create_text(STOP_CX, STOP_CY + 42, text="TAP TO STOP",
                                                  font=self._font(9, True),
                                                  fill=COL_TEXT_DIM, tags="stop")
        self.canvas.tag_bind("stop", "<Button-1>", self.stop_speaking)

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

        pulse = 0.5 + 0.5 * math.sin(self.phase * 1.6)
        listening = self.current_state == "listening"
        self.canvas.itemconfig(self.mic_glow,
                               outline=_mix(colour, COL_BG, 0.25 + 0.5 * (1 - pulse)) if listening else "")
        self.canvas.itemconfig(self.mic_ring,
                               outline=colour if listening else COL_PANEL_EDGE)

        self._refresh_music()

        talking = playback_active.is_set() or not audio_queue.empty()
        stop_shade = COL_STOP if talking else _mix(COL_TEXT_DIM, COL_BG, 0.55)
        self.canvas.itemconfig(self.stop_ring,
                               outline=COL_STOP if talking else COL_PANEL_EDGE)
        self.canvas.itemconfig(self.stop_icon, fill=stop_shade)
        self.canvas.itemconfig(self.stop_label, fill=stop_shade)

        self.root.after(FRAME_MS, self._animate)

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
            self.canvas.itemconfig(card["blurb"],
                                   fill=COL_TEXT if chosen else COL_TEXT_DIM)

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
    def __init__(self): self.current_state = "idle"
    def set_state(self, state_type, caption=None): self.current_state = state_type

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

MUSIC_REPLIES = {
    "en": {"play": "Playing {query}.", "stop": "Stopped the music.",
           "pause": "Paused.", "resume": "Resuming."},
    "hi": {"play": "{query} चला रहे हैं।", "stop": "संगीत बंद कर दिया।",
           "pause": "रोक दिया।", "resume": "फिर से चला रहे हैं।"},
    "hinglish": {"play": "{query} play कर रहे हैं।", "stop": "Music बंद कर दिया।",
                 "pause": "Pause कर दिया।", "resume": "फिर से play कर रहे हैं।"}
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
4. SEARCH PROTOCOL (STRICT)
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
5. VOICE & FORMATTING CONSTRAINTS
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
        with mic_device as source:
            recognizer.adjust_for_ambient_noise(source, duration=1.5)
            recognizer.dynamic_energy_threshold = False
            if recognizer.energy_threshold < 1500: recognizer.energy_threshold = 1500

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
                    
                music_player.duck(False)          # standby: hand the speaker back

                print("[STATE] In Standby Mode. "
                      + ("Tap the screen to talk..." if music_player.is_active()
                         else "Say 'Hey Liza' or tap the screen..." if WAKE_WORD_ENABLED
                         else "Tap the screen to wake up..."), flush=True)

                while not wake_event.is_set() and not pending_question:
                    # The wake word cannot run while music plays: the mic would just
                    # hear the song and every loop would spend a Whisper call on it.
                    if WAKE_WORD_ENABLED and not music_player.is_active():
                        woke, pending_question, pending_language = listen_for_wake_word(
                            recognizer, mic_device)
                        if woke:
                            break
                    else:
                        time.sleep(0.1)

                wake_event.clear()
                sleep_event.clear()
                music_player.duck(True)           # our turn on the speaker and mic
                session_active = True
                silence_counter = 0

            # Only show listening state if Liza is completely done talking
            # --- FIX: PREVENT SELF-TALKING LOOP ---
            # If Liza is currently speaking, skip the microphone entirely.
            # This saves Pi CPU, prevents ALSA underruns, and stops the infinite loop.
            if sleep_event.is_set():
                sleep_event.clear()
                session_active = False
                pending_question = pending_language = ""
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
                print("[STATE] Listening for speech...", flush=True)
                        
                        
                with mic_device as source:
                    try:
                        audio = recognizer.listen(source, timeout=5, phrase_time_limit=25)
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
                                    dynamic_stt_prompt = " ".join(clean_prompt_text.split()[-40:])
                                break

                        text, stt_language = transcribe(wav_data, dynamic_stt_prompt)

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
                            global current_ai_response
                            ai_words = set(current_ai_response.lower().replace('.', '').replace(',', '').split())
                            user_words = set(lower_text.replace('.', '').replace(',', '').split())
                        
                            if user_words:
                                overlap = len(user_words.intersection(ai_words))
                                overlap_ratio = overlap / len(user_words)
                            
                                if overlap_ratio > 0.4:
                                    print(f"[ECHO DETECTED] Ignoring speaker bleed: {text}", flush=True)
                                    continue 
                            
                                print(f"[INTERRUPT DETECTED] User said: {text}", flush=True)
                                interrupt_playback()
                    
                        print(f"[TRANSCRIPT] {text if text else '[empty]'}", flush=True)
                        if not text: continue
                
                    except sr.WaitTimeoutError:
                        if playback_active.is_set() or not audio_queue.empty():
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

        # --- MUSIC: handled locally, never reaches the model ---
        command = detect_music_command(text, music_player.is_active())
        if command:
            action, query = command
            print(f"[MUSIC] Command: {action} {query!r}", flush=True)
            if action == "play":
                music_player.play(query)
                audio_queue.put(MUSIC_REPLIES[user_language]["play"].format(query=query))
            elif action == "stop":
                music_player.stop()
                audio_queue.put(MUSIC_REPLIES[user_language]["stop"])
            else:
                music_player.set_paused(action == "pause")
                audio_queue.put(MUSIC_REPLIES[user_language][action])
            audio_queue.put("[END_OF_RESPONSE]")
            session_active = False          # hand the speaker back to the music
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

