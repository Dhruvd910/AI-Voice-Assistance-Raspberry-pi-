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
import shutil
import tempfile
import wave
import io
from datetime import datetime

from flask import Flask, Response, jsonify, request


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
            value = value.strip().strip("\"'")
            if key and not os.getenv(key):
                os.environ[key] = value


# Suppress ONNX Runtime warnings
os.environ["ORT_LOGGING_LEVEL"] = "3"
load_dotenv()

import speech_recognition as sr
from faster_whisper import WhisperModel
from groq import Groq
from ddgs import DDGS
from piper.voice import PiperVoice, SynthesisConfig

# 1. The Ears & Brain (Groq API)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
groq_client = Groq(api_key=GROQ_API_KEY)

# ==========================================
# Configuration & Setup
# ==========================================
PIPER_MODEL_PATH = os.getenv("PIPER_MODEL_PATH", "/home/pi/test/piper/en_US-lessac-medium.onnx")
ACTIVE_PIPER_MODEL = PIPER_MODEL_PATH
PIPER_ESPEAK_DATA_DIR = os.getenv("PIPER_ESPEAK_DATA_DIR", "/home/pi/test/piper/espeak-ng-data")
PIPER_RATE = int(os.getenv("PIPER_SAMPLE_RATE", "22050"))
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "tiny")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_MODEL = None
WHISPER_MODEL_LOCK = threading.Lock()
PIPER_VOICE = None
PIPER_VOICE_LOCK = threading.Lock()
PIPER_SYNTHESIS_CONFIG = SynthesisConfig(length_scale=0.85, normalize_audio=True, volume=1.0)

try:
    RAM_DISK_PATH = os.getenv("RAM_DISK_PATH", "/dev/shm")
    if os.path.exists(RAM_DISK_PATH):
        model_name = os.path.basename(PIPER_MODEL_PATH)
        json_name = model_name + ".json"
        ram_model = os.path.join(RAM_DISK_PATH, model_name)
        ram_json = os.path.join(RAM_DISK_PATH, json_name)
        
        if not os.path.exists(ram_model):
            print("[OPTIMIZATION] Moving voice model to RAM...", flush=True)
            shutil.copy(PIPER_MODEL_PATH, ram_model)
            shutil.copy(PIPER_MODEL_PATH + ".json", ram_json)
        ACTIVE_PIPER_MODEL = ram_model
except Exception as e:
    pass

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
RE_SENTENCE_SPLIT = re.compile(r'(?<=[.?!])\s+')

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
    try: ui.face_canvas.itemconfig(ui.transcript_text_id, text="", state="hidden")
    except Exception: pass

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
        
    if ui_instance:
        ui_instance.root.after(0, lambda: _clear_ui_on_interrupt(ui_instance))
        
    stop_playback_event.clear()


def get_cached_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        with WHISPER_MODEL_LOCK:
            if WHISPER_MODEL is None:
                model_name = os.getenv("WHISPER_MODEL_SIZE", "tiny")
                compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
                print(f"[MODEL] Loading Whisper {model_name} into memory...", flush=True)
                WHISPER_MODEL = WhisperModel(
                    model_name,
                    device=os.getenv("WHISPER_DEVICE", "cpu"),
                    compute_type=compute_type,
                    cpu_threads=max(1, os.cpu_count() or 1),
                )
                print("[MODEL] Whisper model cached in memory.", flush=True)
    return WHISPER_MODEL


def get_cached_piper_voice():
    global PIPER_VOICE
    if PIPER_VOICE is None:
        with PIPER_VOICE_LOCK:
            if PIPER_VOICE is None:
                voice_model_path = ACTIVE_PIPER_MODEL
                voice_config_path = voice_model_path + ".json"
                print(f"[MODEL] Loading Piper voice into memory from {voice_model_path}...", flush=True)
                PIPER_VOICE = PiperVoice.load(
                    voice_model_path,
                    config_path=voice_config_path,
                    espeak_data_dir=PIPER_ESPEAK_DATA_DIR,
                )
                print("[MODEL] Piper voice cached in memory.", flush=True)
    return PIPER_VOICE


def transcribe_with_cached_whisper(wav_data, prompt=""):
    model = get_cached_whisper_model()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        temp_path = tmp.name

    try:
        with wave.open(temp_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(wav_data)

        segments, _ = model.transcribe(
            temp_path,
            beam_size=1,
            language="en",
            task="transcribe",
            initial_prompt=prompt,
            vad_filter=True,
        )
        return " ".join(segment.text for segment in segments).strip()
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass


def synthesize_text_with_piper(text):
    if not text or not text.strip():
        return b""
    voice = get_cached_piper_voice()
    chunks = []
    for chunk in voice.synthesize(text, syn_config=PIPER_SYNTHESIS_CONFIG):
        chunks.append(chunk.audio_int16_bytes)
    return b"".join(chunks)


def synthesize_text_to_wav_bytes(text):
    pcm_bytes = synthesize_text_with_piper(text)
    if not pcm_bytes:
        return b""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(PIPER_RATE)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


HTTP_TTS_APP = Flask(__name__)

@HTTP_TTS_APP.route("/health", methods=["GET"])
def http_tts_health():
    return jsonify({"status": "ok", "model_loaded": PIPER_VOICE is not None})

@HTTP_TTS_APP.route("/tts", methods=["GET", "POST"])
def http_tts_endpoint():
    text = None
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        text = payload.get("text") or payload.get("message")
    if text is None:
        text = request.args.get("text", "")

    if not text or not str(text).strip():
        return jsonify({"error": "missing text"}), 400

    wav_bytes = synthesize_text_to_wav_bytes(str(text).strip())
    if not wav_bytes:
        return jsonify({"error": "tts synthesis failed"}), 500

    return Response(wav_bytes, mimetype="audio/wav")


def start_http_tts_server(host="0.0.0.0", port=5001):
    get_cached_piper_voice()
    HTTP_TTS_APP.run(host=host, port=port, threaded=True)

# ==========================================
# Bulletproof Audio + Byte-Synced Subtitles
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
        try:
            aplay_proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(PIPER_RATE), "-c", "1"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
            )
            active_subprocesses = [aplay_proc]

            def push_words_with_delay(text, duration):
                words = text.split()
                if not words:
                    return
                if ui_instance is not None:
                    ui_instance.root.after(0, lambda: ui_instance.set_state("speaking"))
                per_word_delay = max(duration / max(len(words), 1), 0.03)
                for word in words:
                    if stop_playback_event.is_set():
                        break
                    if ui_instance is not None:
                        ui_instance.root.after(0, lambda x=word: ui_instance.push_word(x))
                    time.sleep(per_word_delay)

            print(f"Liza (speaking): {first_item}", flush=True)
            pcm_bytes = synthesize_text_with_piper(first_item)
            if pcm_bytes:
                aplay_proc.stdin.write(pcm_bytes)
                aplay_proc.stdin.flush()
                duration = max(len(pcm_bytes) / (2 * PIPER_RATE), 0.05)
                push_words_with_delay(first_item, duration)
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

                print(f"Liza (speaking): {sentence}", flush=True)
                try:
                    pcm_bytes = synthesize_text_with_piper(sentence)
                    if pcm_bytes:
                        aplay_proc.stdin.write(pcm_bytes)
                        aplay_proc.stdin.flush()
                        duration = max(len(pcm_bytes) / (2 * PIPER_RATE), 0.05)
                        push_words_with_delay(sentence, duration)
                except Exception: pass
                audio_queue.task_done()

            try:
                aplay_proc.stdin.close()
            except Exception:
                pass
            aplay_proc.wait(timeout=5)

        except Exception as e:
            print(f"TTS Error: {e}", flush=True)
        finally:
            if ui_instance is not None:
                ui_instance.root.after(0, lambda: ui_instance.set_state("idle"))
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
        self.transcript_text_id = self.face_canvas.create_text(
            400, 45, text="", font=("Helvetica", 14, "bold"), fill="#2C3E50", 
            width=700, justify="center", anchor="n", state="hidden"
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
        try:
            self.face_canvas.itemconfig(self.transcript_text_id, text=self.current_subtitle.strip(), state="normal")
        except Exception: pass

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
            try: self.face_canvas.itemconfig(self.transcript_text_id, text="", state="hidden")
            except Exception: pass

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

UNIVERSAL_SYSTEM_PROMPT = """You are "Liza", an advanced, highly capable AI Assistant. You have LIVE internet access.
CURRENT SYSTEM TIME & DATE: {system_time}

============================================================
1. CURRENT TEACHING MODE (CRITICAL OVERRIDE)
============================================================
{domain_guidelines}

============================================================
2. UNIVERSAL BEHAVIORAL MATRIX
============================================================
- The STT Forgiveness Rule (CRITICAL): The user is speaking through a microphone. Ignore all typos, phonetic misspellings, and grammar issues. NEVER correct the user. Just infer the meaning and answer.
- The Time Override: You already know the exact time and date from the SYSTEM TIME provided above. DO NOT search the web for the time or date. 
- The Knowledge Fallback: You MUST NEVER say "I don't have real-time access", "I cannot browse the internet", or "I am an AI". 

============================================================
3. SEARCH PROTOCOL (STRICT)
============================================================
If you need to trigger a search, you MUST NOT use the EMOTION or ANSWER tags. You must bypass normal conversation and output EXACTLY AND ONLY this:
SEARCH: <your optimized query>

CRITICAL EXAMPLES OF SEARCHING:
User: "What is the temperature in New Delhi?"
Your Output: SEARCH: current temperature in New Delhi weather

DO NOT add conversational filler. ONLY output the SEARCH tag.

============================================================
4. VOICE & FORMATTING CONSTRAINTS
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
                    
                    dynamic_stt_prompt = "Hey Liza. Explain the concept clearly."
                    for msg in reversed(chat_history):
                        if msg["role"] == "assistant":
                            clean_prompt_text = re.sub(r'EMOTION:\s*\[?[a-zA-Z]+\]?', '', msg["content"])
                            clean_prompt_text = clean_prompt_text.replace('ANSWER:', '').strip()
                            dynamic_stt_prompt = " ".join(clean_prompt_text.split()[-40:])
                            break

                    text = transcribe_with_cached_whisper(wav_data, dynamic_stt_prompt).strip()
                    lower_text = text.lower().strip()
                    hallucinations = [
                        "thank you.", "thank you", "thanks.", "thanks", "thanks for watching.", 
                        "you", "why?", ".", "bye.", "[empty]", "", 
                        "so,", "so.", "so", 
                        "i'm not sure if i can do it.", "i'm not sure.", "i'm not sure",
                        "so, i'm going to go to the next slide.", "i'm going to go to the next slide.",
                        "i'm not sure what you're doing.", "i'm not sure if you're a cat.",
                        "yes.", "yeah.", "okay."
                    ]
                    
                    if lower_text in hallucinations or "three, four" in lower_text or "assistant is a professor" in lower_text or "avoid casual" in lower_text:
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
            stop_playback_event.clear()

        # --- 2. THINK & STREAM ---
        ui.set_state("thinking")
        mode_instruction = MODE_INSTRUCTIONS.get(ui.current_mode, MODE_INSTRUCTIONS["TUTOR"])
        
        current_time = datetime.now().strftime("%I:%M %p, %A, %B %d, %Y")
        dynamic_system_prompt = UNIVERSAL_SYSTEM_PROMPT.format(domain_guidelines=mode_instruction, system_time=current_time)
        
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
                        search_msg = f"Let me check the web for {clean_speech_query}."
                        audio_queue.put(search_msg)
                        audio_queue.put("[END_OF_RESPONSE]") 
                        
                        if ui_instance is not None:
                            ui_instance.root.after(0, lambda: ui_instance.set_state("thinking", f"Searching for: {search_query}..."))
                        
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
                            "content": f"Here are the live web search results:\n{search_context}\n\nBased ONLY on this information, answer the question. If the results do not contain the answer, just say 'I couldn't find the exact data online right now.' DO NOT guess or change the subject. Provide the final spoken answer starting with EMOTION: and ANSWER:"
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
    player_thread = threading.Thread(target=audio_player_worker, daemon=True)
    player_thread.start()

    get_cached_whisper_model()
    get_cached_piper_voice()

    HEADLESS = ("--headless" in sys.argv) or (os.getenv("HEADLESS") == "1")
    HTTP_TTS = ("--tts-http" in sys.argv) or (os.getenv("ENABLE_HTTP_TTS") == "1")

    if HTTP_TTS:
        print("[HTTP TTS] Starting cached Piper HTTP service on port 5001", flush=True)
        start_http_tts_server()
        sys.exit(0)

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

