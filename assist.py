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
from datetime import datetime

# Suppress ONNX Runtime warnings
os.environ["ORT_LOGGING_LEVEL"] = "3"

import speech_recognition as sr
from groq import Groq
from ddgs import DDGS
from cartesia import Cartesia

# 1. Groq API (Ears & Brain)
GROQ_API_KEY = "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"
groq_client = Groq(api_key=GROQ_API_KEY)

# 2. Cartesia API (Voice / Multilingual TTS - Hindi & English)
CARTESIA_API_KEY = "sk_car_KXLnTcFpK1AmSJGbvftJCe"
CARTESIA_VOICE_ID = "faf0731e-dfb9-4cfc-8119-259a79b27e12" # Replace with your preferred multilingual voice ID
cartesia_client = Cartesia(api_key=CARTESIA_API_KEY)

# ==========================================
# Configuration & Setup
# ==========================================
wake_event = threading.Event()
HISTORY_FILE = "chat_history.json"
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

# ==========================================
# Cartesia Audio Worker + Subtitles Sync
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
            # Popen aplay for playing raw 16-bit 22050Hz PCM Audio
            aplay_proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", "22050", "-c", "1"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
            with subprocess_lock:
                active_subprocesses = [aplay_proc]

            def process_and_play(sentence):
                if not sentence or sentence == "[END_OF_RESPONSE]" or stop_playback_event.is_set():
                    return

                print(f"Liza (speaking): {sentence}", flush=True)

                if ui_instance is not None:
                    ui_instance.root.after(0, lambda: ui_instance.set_state("speaking"))
                    words = sentence.split()
                    for word in words:
                        ui_instance.root.after(0, lambda w=word: ui_instance.push_word(w))

                try:
                    # Fetch TTS response object from Cartesia API
                    response = cartesia_client.tts.generate(
                        model_id="sonic-3.5",
                        transcript=sentence,
                        voice={"mode": "id", "id": CARTESIA_VOICE_ID},
                        output_format={
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": 22050
                        }
                    )
                    
                    if aplay_proc.stdin and not stop_playback_event.is_set():
                        # Safely extract the raw bytes from the BinaryAPIResponse object
                        if hasattr(response, 'read'):
                            audio_bytes = response.read()
                        elif hasattr(response, 'content'):
                            audio_bytes = response.content
                        elif hasattr(response, 'iter_bytes'):
                            audio_bytes = b"".join(response.iter_bytes())
                        else:
                            audio_bytes = response # Fallback
                            
                        aplay_proc.stdin.write(audio_bytes)
                        aplay_proc.stdin.flush()
                        
                except Exception as err:
                    print(f"[Cartesia TTS Error] {err}", flush=True)

            # Process first sentence
            process_and_play(first_item)
            audio_queue.task_done()

            # Process remaining sentences in queue for current turn
            while True:
                if stop_playback_event.is_set():
                    break
                
                try:
                    sentence = audio_queue.get(timeout=0.1)
                except queue.Empty:
                    if not playback_active.is_set():
                        break
                    continue

                if sentence is None:
                    break
                if sentence == "[END_OF_RESPONSE]":
                    audio_queue.task_done()
                    break

                process_and_play(sentence)
                audio_queue.task_done()

            if aplay_proc.stdin:
                try: aplay_proc.stdin.close()
                except Exception: pass
            aplay_proc.wait()

        except Exception as e:
            print(f"Audio Worker Error: {e}", flush=True)
        finally:
            if ui_instance is not None:
                ui_instance.root.after(0, lambda: ui_instance.set_state("idle"))
            with subprocess_lock:
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
    "TUTOR": "TUTOR MODE ACTIVE: You are a subject expert. Explain the concept clearly using a maximum of 4 sentences. You can speak Hindi, English, or Hinglish depending on what language the user speaks.",
    
    "CO-TELL": "CO-TELL MODE ACTIVE: You are a collaborative study partner. STRICT RULE: YOU MUST SPEAK A MAXIMUM OF 2 SENTENCES TOTAL. Sentence 1: A brief validation or partial hint. Sentence 2: Ask the user a specific question to test their knowledge.",
    
    "RE-TELL": """RE-TELL MODE ACTIVE: You are an examiner evaluating the user step-by-step as they teach you. 
    STRICT RULE: YOU MUST SPEAK A MAXIMUM OF 2 SENTENCES TOTAL.
    Analyze the user's latest explanation:
    - IF CORRECT: Sentence 1: Briefly validate that they are right. Sentence 2: Ask them to elaborate or explain the next step.
    - IF INCORRECT OR INCOMPLETE: Sentence 1: Gently point out the specific mistake. Sentence 2: Tell them exactly which area to focus on."""
}

UNIVERSAL_SYSTEM_PROMPT = """You are "Liza", an advanced, highly capable AI Voice Assistant fluent in both Hindi and English.
CURRENT SYSTEM TIME & DATE: {system_time}

============================================================
1. CURRENT TEACHING MODE (CRITICAL OVERRIDE)
============================================================
{domain_guidelines}

============================================================
2. UNIVERSAL BEHAVIORAL MATRIX & LANGUAGE RULE (CRITICAL)
============================================================
- LANGUAGE MATCHING RULE (STRICT): You MUST detect the language of the user's latest input. 
  * If the user speaks or asks in ENGLISH, you must reply entirely in ENGLISH.
  * If the user speaks or asks in HINDI or HINGLISH, you must reply in HINDI/HINGLISH.
- The STT Forgiveness Rule: Ignore all typos, phonetic misspellings, and grammar issues from the microphone transcription.
- The Time Override: You already know the exact time and date from the SYSTEM TIME provided above. DO NOT search the web for time/date.
- The Knowledge Fallback: Never say "I don't have real-time access" or "I am an AI".

============================================================
3. SEARCH PROTOCOL (STRICT)
============================================================
If you need to trigger a search, output EXACTLY AND ONLY:
SEARCH: <your optimized query>

============================================================
4. VOICE & FORMATTING CONSTRAINTS
============================================================
- You are a VOICE assistant. Output must be conversational and spoken aloud.
- DO NOT use bullet points, numbered lists, markdown formatting, or symbols.
- ALWAYS start your response EXACTLY like this:
EMOTION: [emotion]
ANSWER: <your spoken answer in the matching user language>
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
            if not session_active:
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

            if playback_active.is_set() or not audio_queue.empty():
                time.sleep(0.2)
                continue

            ui.set_state("listening")
            print("[STATE] Listening for speech...", flush=True)
                        
            with mic_device as source:
                try:
                    audio = recognizer.listen(source, timeout=5, phrase_time_limit=25)
                    wav_data = audio.get_wav_data(convert_rate=16000, convert_width=2)
                    silence_counter = 0
                    
                    dynamic_stt_prompt = "Hey Liza. Explain the concept clearly."
                    for msg in reversed(chat_history):
                        if msg["role"] == "assistant":
                            clean_prompt_text = re.sub(r'EMOTION:\s*\[?[a-zA-Z]+\]?', '', msg["content"])
                            clean_prompt_text = clean_prompt_text.replace('ANSWER:', '').strip()
                            dynamic_stt_prompt = " ".join(clean_prompt_text.split()[-40:])
                            break

                    # Groq Whisper STT (Auto-detects Hindi & English)
                    transcription = groq_client.audio.transcriptions.create(
                        file=("temp.wav", wav_data),
                        model="whisper-large-v3-turbo", 
                        response_format="text",
                        prompt=dynamic_stt_prompt
                    )
                    
                    text = transcription.strip()
                    lower_text = text.lower().strip()
                    hallucinations = [
                        "thank you.", "thank you", "thanks.", "thanks", "thanks for watching.", 
                        "you", "why?", ".", "bye.", "[empty]", "", 
                        "so,", "so.", "so", 
                        "i'm not sure if i can do it.", "i'm not sure.", "i'm not sure"
                    ]
                    
                    if lower_text in hallucinations:
                        text = ""
                    
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
                    if silence_counter >= 6:
                        print("[STATE] Standby Mode...", flush=True)
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

        # LLM Logic
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
                            "content": f"Here are live search results:\n{search_context}\n\nAnswer concisely based ONLY on this information starting with EMOTION: and ANSWER:"
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
            print(f"LLM API Error: {e}", flush=True)
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

    HEADLESS = ("--headless" in sys.argv)

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