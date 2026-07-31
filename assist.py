import os
import sys
import random
import io
import json
import queue
import signal
import atexit
import subprocess
import time
import tkinter as tk
from PIL import Image, ImageTk
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

wake_event = threading.Event()
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

def _clear_ui_on_interrupt(ui):
    ui.current_subtitle = ""
    ui.clear_next_word = False

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

    ui_call(lambda: _clear_ui_on_interrupt(ui_instance))


    stop_playback_event.clear()

# ==========================================
# Language Routing (Hindi / English / Hinglish)
# ==========================================
def detect_user_language(text, stt_language=None):
    """What the student just spoke, used to steer the reply: 'hi', 'en' or 'hinglish'."""
    has_devanagari = bool(RE_DEVANAGARI.search(text))
    has_latin_words = bool(RE_LATIN_WORD.search(text))
    stt_language = (stt_language or "").strip().lower()
    stt_says_hindi = stt_language.startswith("hi") or stt_language == "hindi"

    if has_devanagari and has_latin_words: return "hinglish"
    if has_devanagari: return "hi"
    # Whisper heard Hindi but wrote it in Latin letters, i.e. romanised Hinglish.
    if stt_says_hindi: return "hinglish"
    return "en"

def detect_tts_language(text):
    """Which Cartesia voice language a sentence should be spoken in."""
    return "hi" if RE_DEVANAGARI.search(text) else "en"

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
            pending_words = []
            words_lock = threading.Lock()
            generation_done = threading.Event()
            cancelled = threading.Event()

            def speak_sentence(sentence):
                language = detect_tts_language(sentence)
                offset = clock["generated"]
                got_timestamps = False

                # `with` so a barge-in releases the HTTP connection instead of leaking it.
                with cartesia_client.tts.generate_sse(
                    model_id=CARTESIA_MODEL,
                    transcript=sentence,
                    voice={"mode": "id", "id": cartesia_voice_id(language)},
                    language=language,
                    output_format={"container": "raw", "encoding": "pcm_s16le", "sample_rate": CARTESIA_SAMPLE_RATE},
                    add_timestamps=True,
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

                        elif event_type == "timestamps":
                            stamps = event.word_timestamps
                            got_timestamps = True
                            with words_lock:
                                pending_words.extend(
                                    (offset + start, word) for start, word in zip(stamps.start, stamps.words)
                                )

                        elif event_type == "error":
                            print(f"TTS Error: {getattr(event, 'error', event)}", flush=True)

                # Fallback if the model returned audio without timestamps: pace the
                # words evenly across however long this sentence turned out to be.
                if not got_timestamps and not cancelled.is_set():
                    words = sentence.split()
                    span = max(clock["generated"] - offset, 0.001)
                    with words_lock:
                        for i, word in enumerate(words):
                            pending_words.append((offset + span * i / len(words), word))

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

            def push_subtitles():
                def flush_word():
                    word = pending_words.pop(0)[1]
                    ui_call(lambda x=word: ui_instance.push_word(x))

                while not (stop_playback_event.is_set() or cancelled.is_set()):
                    if clock["start"]:
                        elapsed = time.time() - clock["start"]
                        with words_lock:
                            while pending_words and pending_words[0][0] <= elapsed:
                                flush_word()

                    if generation_done.is_set():
                        with words_lock:
                            if not pending_words or not clock["start"]:
                                break
                        # Audio finished but words are still queued: dump the rest.
                        if (time.time() - clock["start"]) > clock["generated"] + 2.0:
                            with words_lock:
                                while pending_words:
                                    flush_word()
                            break

                    time.sleep(0.05)

            generator_thread = threading.Thread(target=generate_audio, daemon=True)
            subtitle_thread = threading.Thread(target=push_subtitles, daemon=True)
            generator_thread.start()
            subtitle_thread.start()
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
            subtitle_thread.join()
            aplay_proc.wait()

        except Exception as e:
            print(f"TTS Error: {e}", flush=True)
        finally:
            ui_call(lambda: ui_instance.set_state("idle"))
            active_subprocesses.clear()
            playback_active.clear()

# ==========================================
# Full-Screen UI Class 
# ==========================================
class TutorUI:
    def __init__(self, root):
        self.root = root
        self.root.title("AI Tutor")
        self.root.geometry("800x480")
        self.root.attributes('-fullscreen', True)
        self.root.configure(bg="#000000")
        
        self.modes = ["TUTOR", "CO-TELL", "RE-TELL"]
        self.mode_colors = {"TUTOR": "#FF69B4", "CO-TELL": "#50E3C2", "RE-TELL": "#B8E986"}
        self.current_mode_index = 0
        self.current_mode = self.modes[self.current_mode_index]
        self.current_state = "warmup"

        self.face_canvas = tk.Canvas(root, width=800, height=480, bd=0, highlightthickness=0, bg="#000000")
        self.face_canvas.place(x=0, y=0)
        self.bg_image_id = self.face_canvas.create_image(400, 240, image=None)

        self.state_text_id = self.face_canvas.create_text(
            400, 25, text="• WARMUP •", font=("Helvetica", 12, "bold"), fill="#2C3E50"
        )
        self.intro_text_id = self.face_canvas.create_text(
            400, 65, text="Tap anywhere to wake me up!", font=("Helvetica", 20, "bold"), fill="#2C3E50", state="hidden"
        )
        self.current_subtitle = ""
        self.clear_next_word = False

        self.mode_tag = tk.Label(root, text=f"• {self.current_mode} MODE •", font=("Helvetica", 16, "bold"), fg="#FFFFFF", bg=self.mode_colors[self.current_mode], padx=30, pady=5, bd=4, relief="raised", cursor="hand2")
        self.mode_tag.place(relx=0.5, rely=0.95, anchor="s")
        
        self.mode_tag.bind("<Button-1>", self.cycle_mode)
        self.root.bind("<Escape>", lambda event: self.root.attributes("-fullscreen", False))
        
        # Binds a left-click anywhere on the app to instantly wake Liza up
        self.root.bind("<Button-1>", self.wake_up)

        self.animations = {}
        self.current_frame_index = 0
        
        self.load_animations()
        self.update_animation()

    def load_animations(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        base_path = os.path.join(current_dir, "faces")
        states = ["warmup", "idle", "listening", "thinking", "speaking", "capturing", "error"] 
        
        for state in states:
            folder = os.path.join(base_path, state)
            self.animations[state] = []
            
            if os.path.exists(folder):
                files = sorted([f for f in os.listdir(folder) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif'))])
                for f in files:
                    try:
                        img = Image.open(os.path.join(folder, f)).resize((800, 480))
                        self.animations[state].append(ImageTk.PhotoImage(img))
                    except Exception: pass
            
            if not self.animations[state]:
                blank = Image.new('RGB', (800, 480), color='#000000')
                self.animations[state].append(ImageTk.PhotoImage(blank))

    def update_animation(self):
        frames = self.animations.get(self.current_state, []) or self.animations.get("idle", [])
        if frames:
            if self.current_state == "speaking" and len(frames) > 1:
                self.current_frame_index = random.randint(0, len(frames) - 1)
            else:
                self.current_frame_index = (self.current_frame_index + 1) % len(frames)
                
            self.face_canvas.itemconfig(self.bg_image_id, image=frames[self.current_frame_index])
        
        speed = 100 if self.current_state == "speaking" else 300
        self.root.after(speed, self.update_animation)
    
    def push_word(self, word):
        if not playback_active.is_set() and self.current_state in ["idle", "warmup", "listening"]: 
            return

        if self.clear_next_word:
            self.current_subtitle = ""
            self.clear_next_word = False

        self.current_subtitle += word + " "

        if word.endswith(('.', '?', '!')) or len(self.current_subtitle.split()) >= 16:
            self.clear_next_word = True

    def set_state(self, state, caption=None):
        if state not in self.animations:
            state = "idle"
            
        if state in ["idle", "listening", "warmup"] and (playback_active.is_set() or not audio_queue.empty()):
            return

        if self.current_state != state:
            self.current_state = state
            self.current_frame_index = 0 
            
        self.face_canvas.itemconfig(self.state_text_id, text=f"• {state.upper()} •")
            
        if state == "idle":
            try: self.face_canvas.itemconfig(self.intro_text_id, state="normal")
            except Exception: pass
        else:
            try: self.face_canvas.itemconfig(self.intro_text_id, state="hidden")
            except Exception: pass

        if state in ["idle", "warmup", "listening"]:
            self.current_subtitle = ""

    # FIX: No matter what state the UI is in, a tap wakes her up instantly!
    def wake_up(self, event):
        print("[UI] Screen tapped! Waking up...", flush=True)
        wake_event.set()

    def cycle_mode(self, event):
        self.current_mode_index = (self.current_mode_index + 1) % len(self.modes)
        self.current_mode = self.modes[self.current_mode_index]
        self.mode_tag.config(text=f"• {self.current_mode} MODE •", bg=self.mode_colors[self.current_mode])
        
        interrupt_playback()
        
        intros = {
            "TUTOR": "You are in tutor mode.",
            "CO-TELL": "You are in co-tell mode. Let's study together!",
            "RE-TELL": "You are in re-tell mode. Tell me what you have learned, I am ready to listen."
        }
        audio_queue.put(intros[self.current_mode])
        audio_queue.put("[END_OF_RESPONSE]")

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
    clean = RE_GREETING_PREFIX.sub('', clean)
    clean = RE_EMOJI.sub('', clean)
    return clean.replace('*', '').replace('_', '').replace('#', '').replace('`', '').replace('[', '').replace(']', '').strip()

MODE_INSTRUCTIONS = {
    "TUTOR": "TUTOR MODE ACTIVE: You are a subject expert. Explain the concept clearly using a maximum of 4 sentences. Follow this sequence: 1. Core principle. 2. Mechanism. 3. Real-world example.",
    
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

CRITICAL EXAMPLES OF SEARCHING:
User: "What is the temperature in New Delhi?"
Your Output: SEARCH: current temperature in New Delhi weather

DO NOT add conversational filler. ONLY output the SEARCH tag.

============================================================
5. VOICE & FORMATTING CONSTRAINTS
============================================================
- You are a VOICE assistant. Your output must be spoken aloud.
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

    while True:
        if not headless:
            
            # --- STANDBY LOOP: waits for screen tap, uses 0% CPU! ---
            if not session_active:
                
                # FIX: Thread-safe state update for Tkinter!
                if hasattr(ui, 'root'):
                    ui.root.after(0, lambda: ui.set_state("idle"))
                else:
                    ui.set_state("idle")
                    
                print("[STATE] In Standby Mode. Tap the screen to wake up...", flush=True)
                
                while not wake_event.is_set():
                    time.sleep(0.1)
                    
                wake_event.clear()
                session_active = True
                silence_counter = 0

            # Only show listening state if Liza is completely done talking
            # --- FIX: PREVENT SELF-TALKING LOOP ---
            # If Liza is currently speaking, skip the microphone entirely.
            # This saves Pi CPU, prevents ALSA underruns, and stops the infinite loop.
            if playback_active.is_set() or not audio_queue.empty():
                time.sleep(0.2)
                continue

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
                            dynamic_stt_prompt = " ".join(clean_prompt_text.split()[-40:])
                            break

                    # No `language` argument: Whisper detects Hindi vs English on its own,
                    # and verbose_json reports back what it heard.
                    transcription = groq_client.audio.transcriptions.create(
                        file=("temp.wav", wav_data),
                        model=STT_MODEL,
                        response_format="verbose_json",
                        temperature=0.0,
                        prompt=dynamic_stt_prompt
                    )

                    text = (transcription.text or "").strip()
                    stt_language = getattr(transcription, "language", "") or ""
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

        # --- 2. THINK & STREAM ---
        ui.set_state("thinking")
        mode_instruction = MODE_INSTRUCTIONS.get(ui.current_mode, MODE_INSTRUCTIONS["TUTOR"])

        user_language = detect_user_language(text, stt_language)
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
                        model="openai/gpt-oss-120b", messages=chat_history, stream=True, max_tokens=512, temperature=0.7
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

