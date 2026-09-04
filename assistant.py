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
import random
import collections
import itertools
import contextlib
from datetime import datetime
from PIL import Image, ImageDraw, ImageFilter, ImageTk

import profiles
import kg_content
import store


# .env loading and the tunables it feeds live in config.py, which reads the
# file on import -- so this import has to come before any os.getenv below.
import config  # noqa: F401  (imported for the .env it loads)

# The screen. ui.py imports THIS module back, and that cycle is safe only
# because nothing in it touches assist.* at import time; see ui.py's header
# before changing either import.
# Shared state lives in state.py so that ui.py, the media player and the
# action tags can all reach it without importing each other. The names
# imported here are never reassigned, only mutated in place, which is what
# makes importing them BY NAME safe; everything that IS reassigned is
# reached as state.<name>. See state.py's header before moving one group
# into the other.
import state
from state import get_device_state, note_media_started, set_playing_state
# Talking to the screen from another thread. In uibridge rather than here so
# that media.py and the action tags can reach the UI without importing the
# assistant, which is what kept those seams two-way.
from uibridge import ui_call, ui_invoke
# Music and video. media.py imports nothing of ours but state, config and
# uibridge, so it is a leaf: this import only goes one way.
from config import AUDIO_OUTPUT_DEVICE, MPV_AUDIO_DEVICE
from media import (MEDIA_STOPPED_ACKS, MEDIA_WAKE_PHRASE_S, MEDIA_WAKE_TIMEOUT_S,
                   RE_STOP_MEDIA_PHRASE, _die_with_parent, detect_play_media,
                   media_duck_volume, media_is_paused, media_restore_volume,
                   media_set_pause, search_first_video, start_media_playback,
                   stop_media_playback)
# What she can do on the device. actions.py reaches four names back through
# this module at call time; see its header for which and why.
from actions import (ACTION_DATA_PREFIX, CLOSED_FILE_ACKS, FILE_CANCEL_ACKS,
                     FILE_FOUND_ACKS, FILE_MISSING_ACKS, IMMEDIATE_ACTIONS,
                     OPENING_ACKS, RE_CLOSE_FILE_PHRASE, RE_CONFIRM_NO,
                     RE_CONFIRM_YES, action_failure_sentence, device_state_block,
                     execute_action, file_query_topic, find_files,
                     note_learning, open_file_action, parse_action,
                     phrase_action_result, student_profile_block)
# Everything she is told to be, and every fixed line she says. A leaf: it
# imports nothing but re, so this only goes one way.
from prompts import (AGENTIC_ACTIONS, ASSISTANT_SCOPE, EMOTION_PERSONA,
                     LANGUAGE_INSTRUCTIONS, LLM_BUSY, LLM_UNREACHABLE,
                     MODE_INSTRUCTIONS, RETELL_ACKS, RETELL_EVALUATE_AFTER_S,
                     RETELL_EVALUATION_PROMPT, RETELL_MIN_LISTEN_S, RETELL_NUDGES,
                     RETELL_NUDGE_AFTER_S, RETELL_PHRASE_LIMIT_S, RE_RETELL_MARK_NOW,
                     SEARCH_NOTICES, UNIVERSAL_SYSTEM_PROMPT)
# The voice. audio.py reads two names back through this module at call time.
# The Kindergarten flow. kg.py reads a few names back through this module at
# call time; see its foot for which.
from kg import (kg_handle_doubt, kg_holds_microphone, kg_listen_waiting,
                kg_serve_listen)
from audio import audio_player_worker, list_cartesia_voices
from config import (BYTES_PER_SEC, CARTESIA_API_KEY, CARTESIA_MODEL,
                    CARTESIA_SAMPLE_RATE, CARTESIA_SPEED, CARTESIA_VOICE_ID,
                    VOICE_IDS, cartesia_client)
from state import note_spoken



from state import (_kg_listen_lock, _kg_listen_next, _kg_listen_valid_from, audio_queue,
                   caption_lock, caption_state, device_state_lock, kg_active,
                   kg_listen_requests, kg_listen_results, media_active, playback_active,
                   sleep_event, stop_playback_event, subprocess_lock, wake_event)

from ui import HeadlessUI, TutorUI
# Suppress ONNX Runtime warnings
os.environ["ORT_LOGGING_LEVEL"] = "3"

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

# ALSA's "default" device does not resolve without a configured ~/.asoundrc, so
# playback is pointed at a specific card.
#
# dmix rather than plughw: plughw takes the card EXCLUSIVELY, so whichever of
# Liza's voice or the music player opens it second just fails ("Device or
# resource busy") and dies silently. dmix mixes instead, and the plug: wrapper
# is needed because dmix itself is stereo-only while the TTS stream is mono.
# The inner quotes are part of the device name -- ALSA misparses it without
# them. Check `aplay -L` if this needs to change.


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
# Whisper's initial_prompt is a bias, not a hint: handed ambiguous audio it will
# return the prompt itself. Seeding every wake check with the wake phrase is
# therefore the one thing guaranteed to make room noise transcribe AS the wake
# phrase, and logs/liza.log shows exactly that -- a clean "हे लीज़ा।" out of a
# room where nobody addressed her, indistinguishable from a real wake because it
# IS the seed coming back.
#
# Awake that is an acceptable trade: the seed is what makes the name survive a
# noisy room, and a spurious wake while she is already listening costs a
# discarded turn. From SLEEP it is not. Sleep is an explicit "leave me alone",
# the Speak button is always right there, and this is the failure the student
# actually reported -- she came out of sleep on her own. So the sleep path asks
# Whisper to transcribe what it really heard, with no phrase suggested to it.
#
# Set WAKE_SEED_ASLEEP to a prompt to put the old behaviour back.
WAKE_SEED_PROMPT_ASLEEP = os.getenv("WAKE_SEED_ASLEEP", "")
# Devanagari gives \b nothing to anchor to -- the script has no case, and its
# vowel signs are combining marks rather than word characters -- so every name
# spelling below matched happily INSIDE a longer word. Observed waking her on
# "हे लीज़ाश देखे", where लीज़ा is merely the front of लीज़ाश. This asserts that
# nothing which would CONTINUE the word follows: a consonant, a nukta, or a
# virama. A danda or a space may, which is how a real utterance ends.
NAME_END = r'(?![क-ह़्])'
RE_WAKE_WORD = re.compile(
    # English spellings Whisper produces for the name.
    # "a" was in this list and never belonged: it is an ordinary English article,
    # and the whole group is optional anyway, so it added no reachable match
    # beyond the bare name while turning "a Lisa" in any sentence into a wake.
    r'\b(?:hey|hi|hello|ok|okay|hay)?\s*'
    # Spellings observed from Whisper for the same spoken name. None of these is
    # an ordinary English word, so a bare match is safe without a greeting.
    r'(?:liza|lisa|leeza|leesa|lizza|lyza|eliza|elisa|lija|leza|laiza|liesa|lizah|luiza)\b'
    # Devanagari. The seed prompt is bilingual, so Whisper often writes the name
    # in Devanagari, and its spelling varies far more than a fixed list can cover
    # -- "हे लागा", "हे लगा" and "हे लाजा" were all observed for "Hey Liza". The
    # consonant skeleton is matched instead. A greeting is REQUIRED for this
    # branch because some of those forms (लगा, "felt") are ordinary Hindi words
    # that must not trigger a wake on their own.
    #
    # The FINAL vowel sign is required, where it used to be optional. The name is
    # two syllables and ends in one -- लीज़ा, लागा, लगा all do. Optional, this
    # also matched every consonant-final word built on the same skeleton, and
    # "हे लाग" (ल + ा + ग, no trailing vowel) was the single commonest false wake
    # in logs/liza.log: seven of them, none remotely the wake word. Requiring the
    # vowel costs nothing real, because it is present in every spelling of the
    # name this branch was written to catch.
    r'|(?:हे|अरे|ओके|हाय|सुनो|हैलो)\s*ल[ािीुू]?[जगसझशद]़?[ािी]' + NAME_END +
    # Unambiguous spellings still wake her with no greeting at all.
    r'|(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)' + NAME_END,
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
    # The greeting is REQUIRED here, not optional. It was written with a `?`,
    # which made this branch match a bare "लिज़ा" anywhere -- the exact hole the
    # comment above claims is closed, and the one RE_WAKE_WORD_OVER_MEDIA was
    # later added to work around. Observed in logs/liza.log waking her out of
    # sleep on "है लिज़ा।" -- ambient Hindi where "है" is the verb "is", not a
    # greeting at all.
    r'|(?:हे|अरे|ओके|हाय|सुनो|हैलो)\s*(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)' + NAME_END,
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
    r'|(?:हे|अरे|ओके|हाय|सुनो|हैलो)\s*(?:लीज़ा|लिज़ा|लीजा|लिजा|लीसा)' + NAME_END,
    re.IGNORECASE)

# WHERE the name falls in the utterance, which the patterns above cannot express.
#
# Every pattern here is used with .search(), so until now a hit ANYWHERE in the
# transcript woke her -- including thirty words into a sentence that merely
# mentioned the name. That is the false-wake reported from a room with an
# ordinary Hindi conversation going on in it, and it is not a spelling problem,
# so no amount of tightening the alternations above can reach it.
#
# Two structural facts separate a real wake from a mention. A person addressing
# the device says the name FIRST -- "Hey Liza, what is photosynthesis" -- so a
# match buried mid-sentence is somebody talking ABOUT her, not TO her. And
# WAKE_SEED_PROMPT primes Whisper with the wake phrase, which is what makes it
# reach for that phrase on ambiguous audio, so a regex hit on its own is weaker
# evidence than it looks.
WAKE_MAX_LEAD_WORDS = int(os.getenv("WAKE_MAX_LEAD_WORDS", "2"))
# From sleep the bar is higher again, for the reason RE_WAKE_WORD_ASLEEP already
# gives: sleep is an explicit "leave me alone", the Speak button is always right
# there, and an accidental wake is the worse failure. So from sleep the wake
# word must be substantially the WHOLE utterance -- "Hey Liza", not a sentence
# that happens to open with it. Generous enough to keep "Hey Liza, what is a
# cell" working, far short of the ambient sentences that caused this.
WAKE_SLEEP_MAX_WORDS = int(os.getenv("WAKE_SLEEP_MAX_WORDS", "6"))
# A bare name with no greeting -- "Liza", "लिज़ा" -- is a legitimate way to
# address her, so RE_WAKE_WORD allows it. But it is also the single commonest
# false positive, because it is exactly what an ordinary sentence ABOUT her
# contains: "Lisa said the report was fine", "मैं लिज़ा से बात कर रहा हूँ".
# Both open with the name and so clear the lead-word gate above.
#
# What separates them is what comes AFTER. "Liza" addressed to her is the whole
# utterance; the name inside a sentence is followed by the rest of the sentence.
# So a bare name has to stand alone, while a greeting -- which no one says by
# accident -- buys the right to keep talking: "Hey Liza, what is photosynthesis".
WAKE_BARE_NAME_MAX_WORDS = int(os.getenv("WAKE_BARE_NAME_MAX_WORDS", "3"))
# (?!\w) rather than \b: a Devanagari greeting ends in a vowel SIGN ("हे" is
# ह + े), which is a combining mark and not a word character, so there is no
# word boundary after it and \b silently never matches the Hindi half of this
# list. The lookahead asks the question that was actually meant -- that the
# greeting is not just the front of a longer word.
RE_WAKE_GREETING = re.compile(r'^\s*(?:hey|hi|hello|ok|okay|hay|हे|अरे|ओके|हाय|सुनो|हैलो)(?!\w)',
                              re.IGNORECASE)

def wake_word_match(text, pattern, asleep=False):
    """The regex hit, but only when it sits where a real wake word sits.

    Returns the match object, or None. See the constants above for why a bare
    pattern hit is not enough on its own."""
    if not text:
        return None
    match = pattern.search(text)
    if not match:
        return None
    words = text.split()
    lead_words = len(text[:match.start()].split())
    if lead_words > WAKE_MAX_LEAD_WORDS:
        print(f"[WAKE] Ignored (name {lead_words} words in, not addressed to her): "
              f"{text!r}", flush=True)
        return None
    if asleep and len(words) > WAKE_SLEEP_MAX_WORDS:
        print(f"[WAKE] Ignored (asleep; {len(words)} words is a conversation, "
              f"not a wake): {text!r}", flush=True)
        return None
    if not RE_WAKE_GREETING.match(match.group(0)):
        # A bare name has to OPEN the utterance, not merely sit near the front.
        # The lead-word gate above allows two words before the match, which is
        # right for a greeting ("ok then, Liza") but wrong with no greeting at
        # all: "आप लिज़ा।" -- "you, Liza" -- cleared it and woke her, and so
        # would "मैंने लिज़ा से". Addressing her by name alone means starting
        # with the name; anything in front of it is a sentence about her.
        if lead_words:
            print(f"[WAKE] Ignored (bare name {lead_words} word(s) in, "
                  f"no greeting): {text!r}", flush=True)
            return None
        if len(words) > WAKE_BARE_NAME_MAX_WORDS:
            print(f"[WAKE] Ignored (bare name inside a {len(words)}-word sentence, "
                  f"no greeting): {text!r}", flush=True)
            return None
    return match

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

HISTORY_FILE = os.getenv("HISTORY_FILE", "chat_history.json")
MAX_HISTORY_TURNS = 6



# ---------- student profiles and the Kindergarten flow ----------
# Set while a KG student is on the device. ai_loop parks on this instead of
# listening: a pre-reader gets the spelling and story screens and nothing else,
# so the open microphone and the whole question-and-answer path stay shut. It is
# an Event rather than a bool because ai_loop waits on it, and .wait() with a
# timeout is what keeps that loop off the CPU while a child taps at letters.

# The KG screens run on the Tk thread; the microphone belongs to ai_loop, which
# opens it once and never lets go (see HeldMicrophone / VoiceListener -- this
# dongle wedges if a stream is re-opened per listen). So a KG screen cannot just
# listen for itself without standing up a second audio stack, which is the one
# thing the brief for this feature ruled out.
#
# Instead ai_loop, which is otherwise PARKED for the whole of KG mode, services
# listen requests from these two queues. The Tk side asks and then polls for the
# answer with root.after, so no Tk call is ever made off the Tk thread.

# A request and its answer used to be two unrelated queues, with the screen
# simply taking whatever turned up next. That held together only while every
# listen was short. It stopped holding the moment the spelling and counting
# screens were given the patience they actually need: ai_loop can now be inside
# one listen for the better part of a minute, so a child who taps Back and
# starts something else leaves a listen in flight, and the answer to it arrives
# long after the screen that asked has gone. The new screen then takes it, and
# ITS answer -- the one the child is sitting there waiting for -- is the one
# that gets dropped. That is the test sitting on "I'm listening..." forever.
#
# So every listen now carries an id, an answer carries the id of the request it
# belongs to, and a screen accepts only its own. Asking for a new listen
# abandons every older one, which also reaches INTO the listen ai_loop is
# blocked in and ends it early -- see the cancel argument to wait_for_utterance.



# ---------- device state (rule 7, the agentic actions) ----------
# What the device is actually DOING, as opposed to what it has been told. Handed
# to the model at the bottom of every system prompt by device_state_block(), so
# "stop the music" with nothing playing is answered with "there's nothing
# playing" instead of a confident stop of nothing.
#
# Locked because these are written from three different threads: ai_loop when it
# executes an action, the Tk thread when a button is tapped, and the media
# watcher when a song simply ends on its own.



ECHO_GUARD_SEC = 2.5      # treat mic input as suspect this long after speaking
MIC_SETTLE_SEC = 0.4      # let the speaker drain before opening the mic


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


# How much of a KG answer has to be Liza's own words before it is thrown away,
# and how many words it must have before it is judged at all.
KG_ECHO_MIN_WORDS = 3
KG_ECHO_RATIO = 0.6


def sounds_like_her_own_prompt(text):
    """True when a KG answer is really Liza's own question coming back.

    The conversation path has guarded against her own voice since the echo work
    went in; the KG screens never did, and they are where it does the most
    damage. logs/liza.log has "How many do you see?" and "What is this?" arriving
    off the microphone and being marked as the child's answer to those very
    questions -- so the child is told they are wrong for something they never
    said.

    Only answers of three real words or more are judged, and single letters do
    not count as words. A KG answer is a word, a number, or a run of letters, and
    a short answer that appears in her prompt is the COMMON case rather than an
    echo: "cat" is exactly what a child says when she has just asked them to
    spell cat, and "C A T" must survive for the same reason.
    """
    heard = [w for w in RE_ECHO_TOKEN.findall((text or "").lower()) if len(w) > 1]
    if len(heard) < KG_ECHO_MIN_WORDS:
        return False
    hers = set(RE_ECHO_TOKEN.findall((state.last_spoken_text or "").lower()))
    if not hers:
        return False
    return sum(1 for word in heard if word in hers) / len(heard) >= KG_ECHO_RATIO

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

# 0.55s is right for a finished sentence and wrong for a sentence still being
# assembled. A student mid-question -- "what is the difference between a... "
# -- pauses for longer than that reaching for the next word, and the phrase was
# being closed on them and the half-question answered as if it were the whole
# one. Rather than charge every turn a slower threshold, the pause stays short
# and an utterance that READS unfinished buys one extra listening window; see
# looks_unfinished() and capture_continuation().
CONTINUATION_WAIT_S = float(os.getenv("CONTINUATION_WAIT", "2.2"))
CONTINUATION_MAX_ROUNDS = int(os.getenv("CONTINUATION_MAX_ROUNDS", "2"))


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

# One history per student, not one per device.
#
# Three children sharing this Pi were sharing a single chat_history.json, which
# means each one's lesson few-shot the next one's replies: a Class 11 discussion
# of gravitational fields sat in the window while a Class 5 child asked what
# gravity is, and the model follows the examples in front of it over any
# instruction. Separate files also mean "remember where we got to" is per child,
# which is the point of having profiles at all.
#
# The unprofiled path still reads and writes the original HISTORY_FILE, so a
# device nobody has set up behaves exactly as before.
HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history")

# Which student the in-memory history belongs to. Set by ai_loop when it loads
# one, and read by save_history so the four call sites that save a turn do not
# each have to carry the id -- and so a save can never write one child's turn
# into another child's file.
_history_owner = None


def history_path(user_id):
    if not user_id:
        return HISTORY_FILE
    return os.path.join(HISTORY_DIR, f"{user_id}.json")


# How many turns already in the store when this session started. Everything
# after that index is new and is what gets appended -- the store keeps an
# append-only log of turns, while chat_history in memory is a rolling window
# that trim_history() shortens, so writing the whole window back every turn
# would duplicate rows the log already has.
_history_written = 0


def load_history(user_id=None):
    global _history_owner, _history_written
    _history_owner = user_id
    rows = store.load_messages(user_id, limit=MAX_HISTORY_TURNS * 2) if user_id else None
    if rows is not None:
        _history_written = len(rows)
        return rows
    # No store, or no student: the JSON files the device used before.
    _history_written = 0
    path = history_path(user_id)
    if os.path.exists(path):
        with open(path, "r") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return []
    return []


def save_history(chat_history, user_id=None):
    """Persist the turns added since the last save.

    Writes to PostgreSQL when it is up and to the JSON file when it is not, and
    keeps the JSON copy either way: it is what the device falls back to, and a
    fallback that has been stale since the database came up is not a fallback.
    """
    global _history_written
    owner = _history_owner if user_id is None else user_id
    # A profile deleted from the UI while ai_loop still holds their turns in
    # memory would otherwise be written straight back on the next save -- the
    # database row is gone, so the insert fails, but the JSON fallback copy would
    # be recreated and the "deleted" child would reappear the next time the store
    # was down. No known profile means nothing to save.
    if owner and not any(p.get("user_id") == owner for p in profiles.list_profiles()):
        return
    turns = [m for m in chat_history if m.get("role") in ("user", "assistant")]
    if owner:
        new_turns = turns[_history_written:]
        if new_turns and store.append_messages(owner, new_turns):
            _history_written = len(turns)
    path = history_path(owner)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except (OSError, ValueError):
        pass
    with open(path, "w") as f:
        json.dump(chat_history, f)


def active_user_id():
    profile = profiles.active_profile()
    return profile.get("user_id") if profile else None

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

def _index_can_record(index):
    """True when this PortAudio index is actually a capture device.

    A pinned index is a position in PortAudio's list, not a name, and that list
    is rebuilt on every boot -- so a USB dongle re-enumerating shifts every index
    after it. Observed on 2026-08-29: after a reboot, index 0 became the HDMI
    output, which has no input channels at all, and the pin quietly pointed the
    microphone at it. The only symptom was six failed open attempts and a device
    that never heard anything again.
    """
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        try:
            if not 0 <= index < pa.get_device_count():
                return False
            return int(pa.get_device_info_by_index(index)["maxInputChannels"]) > 0
        finally:
            pa.terminate()
    except Exception:
        # Cannot tell, so do not overrule the pin on a guess.
        return True


def detect_microphone_index():
    if MIC_DEVICE_INDEX:
        try:
            pinned = int(MIC_DEVICE_INDEX)
        except ValueError:
            print(f"[MIC] MIC_DEVICE_INDEX={MIC_DEVICE_INDEX!r} is not a number; "
                  f"falling back to auto-detection.", flush=True)
        else:
            if _index_can_record(pinned):
                return pinned
            print(f"[MIC] MIC_DEVICE_INDEX={pinned} cannot record -- it is not a "
                  f"capture device any more. The USB dongles have almost certainly "
                  f"renumbered across a reboot. Auto-detecting instead; run "
                  f"`--list-mics` and re-pin it in .env.", flush=True)
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

    def wait_for_utterance(self, timeout, phrase_limit, end_silence, preroll_ms=None,
                           cancel=None):
        """Block until a phrase starts and finishes. sr.AudioData, or None.

        Same contract as recognizer.listen(): `timeout` bounds only the wait for
        speech to BEGIN, `phrase_limit` caps the phrase itself, and `end_silence`
        is the pause that ends it (pause_threshold). Returning sr.AudioData is
        deliberate -- audio_rms(), audio_seconds() and get_wav_data() all work on
        it unchanged, so nothing downstream of the microphone had to move.

        `cancel` is a predicate checked while waiting; True abandons the read and
        returns None. It exists because the KG screens can wait the better part
        of a minute for a child who is counting, and a child who taps Back in the
        middle of that must not leave the only thread that may touch the
        microphone blocked until the phrase limit runs out -- the next screen's
        question would sit there unheard for the whole of it."""
        if self._vad is None:
            return None
        if cancel is not None and cancel():
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
            if cancel is not None and cancel():
                return None
            frame = self._next_frame(0.2)
            if frame is None:
                # No audio arrived. Before the phrase opens, the timeout ends the
                # wait; after it, the phrase limit does -- measured from the wall
                # clock rather than from frames, because frames are exactly what
                # has stopped arriving. Without that second bound a phrase that
                # opened just as the capture stream died waited for ever, and
                # ai_loop with it.
                if not triggered and time.time() - started >= timeout:
                    return None
                if triggered and time.time() - speech_at >= phrase_limit:
                    break
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
        for proc in state.active_subprocesses:
            try: proc.terminate()
            except Exception: pass
        state.active_subprocesses.clear()
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
    stop_playback_event.set()
    
    with subprocess_lock:
        for proc in state.active_subprocesses:
            try: proc.terminate()
            except Exception: pass
        state.active_subprocesses.clear()
    
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

def segment_logprob(result):
    """The WORST avg_logprob across a verbose_json response, or None.

    The worst rather than the mean: a real question with one invented tail
    segment is still a question with an invention stapled to it, and the tail is
    what would be answered.
    """
    scores = []
    for segment in (getattr(result, "segments", None) or []):
        data = segment if isinstance(segment, dict) else vars(segment)
        score = data.get("avg_logprob")
        if score is not None:
            scores.append(score)
    return min(scores) if scores else None


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
            text = (result.text or "").strip()
            spoken_language = getattr(result, "language", "") or ""
            # Judged HERE, in the one funnel every transcription passes through,
            # for the same reason clamp_stt_prompt is: the wake path, the KG
            # screens and the conversation path each need this and only one of
            # them ever had it. A KG child was being marked wrong for an answer
            # Whisper invented out of the room, and none of that ever reached the
            # code that knew what a hallucination looked like.
            invented, why = looks_hallucinated(text, segment_logprob(result))
            if invented:
                print(f"[STT] Dropped ({why}): {text!r}", flush=True)
                return "", spoken_language
            return text, spoken_language
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

# Words that CANNOT end a sentence. If the transcript stops on one of these the
# student was still mid-thought when the pause threshold closed their turn --
# "what is the difference between", "can you explain how the", "I want to know
# about". Answering that is answering half a question, which is exactly what was
# reported. Deliberately a tail-word test rather than an LLM call: it costs
# nothing, it runs on the Pi, and it is wrong only in the harmless direction
# (one extra listening window that returns silence).
UNFINISHED_TAIL_WORDS = {
    # conjunctions and connectives
    "and", "or", "but", "so", "because", "since", "although", "though",
    "while", "whereas", "unless", "until", "if", "then", "than", "that",
    # prepositions
    "of", "in", "on", "at", "to", "for", "with", "from", "by", "about",
    "into", "onto", "over", "under", "between", "among", "through",
    "during", "before", "after", "like", "as", "per", "via", "upon",
    # articles, determiners and possessives
    "a", "an", "the", "my", "your", "his", "her", "its", "our", "their",
    "this", "these", "those", "some", "any", "every", "each", "another",
    # auxiliaries and copulas left dangling
    "is", "are", "was", "were", "am", "be", "been", "being", "do", "does",
    "did", "have", "has", "had", "will", "would", "shall", "should", "can",
    "could", "may", "might", "must",
    # question openers with nothing after them yet
    "what", "why", "how", "when", "where", "which", "who", "whom", "whose",
    # fillers -- a student audibly thinking
    "um", "uh", "umm", "uhh", "er", "erm", "hmm", "mmm", "actually", "basically",
    # the Hindi equivalents, since half the questions here arrive in Hindi
    "aur", "ya", "lekin", "kyunki", "ke", "ki", "ka", "ko", "se", "mein",
    "par", "kya", "kaise", "kyun", "kab", "kahan", "kaun", "kitna", "matlab",
    "bhi", "toh", "phir", "agar", "jab", "jo",
    "और", "या", "लेकिन", "क्योंकि", "के", "की", "का", "को", "से", "में",
    "पर", "क्या", "कैसे", "क्यों", "कब", "कहाँ", "कौन", "कितना", "मतलब",
    "कि", "भी", "तो", "फिर", "अगर", "जब", "जो", "वो", "यह", "एक",
}

# Sentence-final punctuation means the speaker landed somewhere. Whisper puts it
# in when the prosody falls, so it is real evidence and not just formatting.
RE_SENTENCE_END = re.compile(r"[.!?\u0964\u2026]\s*$")


def looks_unfinished(text, truncated=False):
    """True when this transcript is probably the FIRST HALF of what was said.

    `truncated` is the phrase_time_limit having fired, which is unfinished by
    definition -- the microphone closed while they were still talking.
    """
    if truncated:
        return True
    stripped = (text or "").strip()
    if not stripped:
        return False
    if RE_SENTENCE_END.search(stripped):
        return False
    words = re.findall(r"[\w\u0900-\u097F']+", stripped.lower())
    if not words:
        return False
    # A single bare word is either a command ("stop", "next") or a fragment, and
    # the tail list is what tells them apart -- so a one-word command is never
    # made to wait, which is the whole point of a one-word command.
    if len(words) == 1:
        return words[0] in UNFINISHED_TAIL_WORDS
    # More than one word and no full stop at all. Whisper punctuates an
    # utterance it heard END; every truncated line in logs/liza.log came back
    # bare and every complete one came back with a stop. So the missing stop is
    # the evidence, not a guess: "Well, what you need to do is", "Yes, and now
    # if you take a look at the bottom we have", "...look at the edge pieces and
    # find" -- all of them the front half of a sentence.
    return True


def capture_continuation(listener, recognizer, source, endpointed, pause_threshold,
                         phrase_limit, stt_prompt, text, truncated=False):
    """Hold the floor open for a student who has not finished their sentence.

    Returns the text with whatever they said next appended. Costs nothing on a
    finished sentence, because looks_unfinished() says no and this returns
    immediately; costs one short silent wait on a false positive. The alternative
    -- simply raising PAUSE_THRESHOLD_NORMAL -- charges that wait to every single
    turn, including all the ones that were already fine.
    """
    rounds = 0
    while rounds < CONTINUATION_MAX_ROUNDS and looks_unfinished(text, truncated):
        rounds += 1
        print(f"[CONTINUATION] '{text}' sounds unfinished; holding the floor "
              f"open for {CONTINUATION_WAIT_S:.1f}s.", flush=True)
        try:
            if endpointed:
                more_audio = listener.wait_for_utterance(
                    CONTINUATION_WAIT_S, phrase_limit, pause_threshold)
            else:
                more_audio = recognizer.listen(source, timeout=CONTINUATION_WAIT_S,
                                               phrase_time_limit=phrase_limit)
        except sr.WaitTimeoutError:
            more_audio = None
        except Exception as exc:
            print(f"[CONTINUATION] Listen failed: {exc}", flush=True)
            return text
        if more_audio is None:
            # They really had stopped. Their pause was a full stop after all.
            return text
        if not is_probably_speech(more_audio, "CONT", endpointed):
            return text
        wav = more_audio.get_wav_data(convert_rate=16000, convert_width=2)
        # Seeded with what they have already said, so the second half is read in
        # the context of the first -- the same trick the main path uses with her
        # own last reply.
        more_text, _lang = transcribe(wav, f"{stt_prompt} {text}".strip())
        more_text = (more_text or "").strip()
        if not more_text:
            return text
        lowered = more_text.lower().strip()
        if lowered in HALLUCINATIONS or RE_HALLUCINATION.search(lowered):
            return text
        print(f"[CONTINUATION] ...and the rest: {more_text!r}", flush=True)
        text = f"{text.rstrip()} {more_text}".strip()
        truncated = audio_seconds(more_audio) >= phrase_limit - 0.5
    return text


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

# "Liza stop" comes back from Whisper as "Lisa's top": the S of the command is
# heard as a possessive on her name, so stripping the name leaves "'s top" --
# which is not a command any pattern matches. Seen in logs/liza.log with a video
# playing, which is the one moment "stop" has to work. Putting the consonant back
# is safe because the device has no notion of anything BELONGING to Liza, so a
# possessive on her name is always this mistake.
RE_ABSORBED_CONSONANT = re.compile(r"^['\u2019]?([sd])\s+(\w)", re.IGNORECASE)

# One word is normally a fragment rather than a question -- but the commands that
# matter most are exactly one word, and they were being thrown away with the
# fragments. Over playing media that is not a small loss: the track is paused,
# nothing is heard in its place, and it RESUMES, so "Liza, stop" does nothing at
# all. Devanagari and the romanised Hindi are here for the same reason they are
# in RE_STOP_MEDIA_PHRASE -- Whisper romanises Hindi constantly.
RE_ONE_WORD_COMMAND = re.compile(
    r'^(?:stop|pause|resume|play|continue|next|skip|mute|unmute|quiet|silence|'
    r'louder|softer|quieter|volume|sleep|repeat|again|'
    r'band|chup|ruko|rukiye|rok|baji|'
    r'\u0930\u0941\u0915\u094b|\u092c\u0902\u0926|\u091a\u0941\u092a|\u0906\u0917\u0947|\u092b\u093f\u0930)$',
    re.IGNORECASE)


def rejoin_absorbed_consonant(command):
    """Put back a consonant Whisper glued onto her name as a possessive."""
    return RE_ABSORBED_CONSONANT.sub(lambda m: m.group(1) + m.group(2), command, count=1)


def listen_for_wake_word(recognizer, mic_device, asleep=False, listener=None,
                         pattern=None, seed=None, timeout=None, phrase_limit=None,
                         cancel=None):
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
        #
        # `timeout` and `phrase_limit` are honoured here as well as on the
        # fallback path below. They used to be read only by the fallback, so the
        # short window the media branch passes -- the one that decides how long a
        # track stays ducked -- was silently ignored on the path this device
        # actually takes, and every duck lasted the full ten seconds instead of
        # two. `cancel` lets a KG screen take the microphone back without waiting
        # that window out.
        audio = listener.wait_for_utterance(
            WAKE_LISTEN_TIMEOUT_S if timeout is None else timeout,
            WAKE_PHRASE_LIMIT_S if phrase_limit is None else phrase_limit,
            PAUSE_THRESHOLD_NORMAL, cancel=cancel)
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
        if seed is None:
            # Empty from sleep, on purpose -- see WAKE_SEED_PROMPT_ASLEEP. The
            # seed is what teaches Whisper to hand the wake phrase back on room
            # noise, and from sleep a false wake is the worse failure.
            seed = WAKE_SEED_PROMPT_ASLEEP if asleep else WAKE_SEED_PROMPT
        text, language = transcribe(wav_data, seed, model=WAKE_STT_MODEL)
        if is_repeated_hallucination(text):
            # "हे लीज़ा। हे लीज़ा। हे लीज़ा।" -- nobody says the wake word three
            # times in one breath. Whisper looping a short phrase is one of its
            # best-known tells on audio that is not speech, and over a track it
            # loops the one phrase the seed prompt taught it.
            print(f"[WAKE] Ignored (looped phrase, not speech): {text!r}", flush=True)
            return False, "", ""
        if pattern is None:
            pattern = RE_WAKE_WORD_ASLEEP if asleep else RE_WAKE_WORD
        match = wake_word_match(text, pattern, asleep=asleep)
        if match:
            print(f"[WAKE] Heard{' (from sleep)' if asleep else ''}: {text}", flush=True)
            # "Hey Liza, what is photosynthesis?" said in one breath: keep the question
            # instead of making the student repeat it.
            question = re.sub(r'\s+', ' ', text[:match.start()] + " " + text[match.end():])
            question = question.strip(" ,.!?।-")
            # Before anything tries to read it as a command; see the regex.
            question = rejoin_absorbed_consonant(question).strip(" ,.!?।-")
            if len(question.split()) < 2 and not RE_ONE_WORD_COMMAND.match(question):
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
                ui_call(lambda r=reading: state.ui_instance.set_weather(r))
        except Exception as exc:
            print(f"[WEATHER ERROR] {exc}", flush=True)
        time.sleep(WEATHER_REFRESH_S)

# ==========================================
# Full-Screen UI
# ==========================================
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
# Whisper does not return nothing for a room with nobody in it -- it returns its
# best guess at words, fluently and with punctuation. Every entry below was
# actually observed in logs/liza.log coming out of silence or room tone on this
# device. The list is the last line of defence rather than the first: see
# looks_hallucinated() for the two checks that do the real work, because a
# blocklist can only ever catch the inventions somebody has already seen.
HALLUCINATIONS = {
    "thank you.", "thank you", "thanks.", "thanks", "thanks for watching.",
    "you", "why?", ".", "..", "...", "bye.", "bye", "[empty]", "",
    "so,", "so.", "so",
    "i'm not sure if i can do it.", "i'm not sure.", "i'm not sure",
    "so, i'm going to go to the next slide.", "i'm going to go to the next slide.",
    "i'm not sure what you're doing.", "i'm not sure if you're a cat.",
    "yes.", "yeah.", "okay.", "ok.", "okay", "ok",
    # Observed on this device, out of an empty room -- counts in logs/liza.log
    # over one afternoon: "I'm going to go." 21, "Thank you." 16, "Okay." 9.
    "i'm going to go.", "i'm going to go", "i'm going.", "i'm sorry.",
    "i'm sorry", "i", "i.", "oh", "oh.", "oh, my god.", "oh my god.",
    "yeah", "yes", "mm-hmm.", "mm-hmm", "mmm.", "hmm.", "hmm", "uh.", "um.",
    "please.", "please", "right.", "sure.", "good.", "well.", "and.", "the.",
    "he.", "it.", "no.", "nope.", "what?", "huh?", "hey.", "hi.", "hello.",
    "धन्यवाद।", "धन्यवाद", "शुक्रिया।", "शुक्रिया", "नमस्ते।", "नमस्कार।",
    "जी हाँ।", "हाँ।", "जी।", "ठीक है।", "अच्छा।", "।", "है।", "है", "हुआ",
}

# Scripts nobody in this room is speaking. Whisper wanders into Japanese, Korean
# and Chinese on noise -- logs/liza.log has "はい" eleven times and "バター"
# (Japanese for "butter") twice, out of a room where only English and Hindi are
# ever spoken. Arabic script is deliberately NOT here: Whisper reports Hindi as
# Urdu routinely, and that is a real sentence to be re-read, not an invention.
RE_IMPOSSIBLE_SCRIPT = re.compile(
    "["
    "\u3040-\u30ff"      # hiragana, katakana
    "\u3400-\u4dbf"      # CJK extension A
    "\u4e00-\u9fff"      # CJK unified ideographs
    "\uac00-\ud7af"      # hangul
    "\u0e00-\u0e7f"      # thai
    "\u0400-\u04ff"      # cyrillic
    "\u0370-\u03ff"      # greek
    "\u0590-\u05ff"      # hebrew
    "]")

# Below this, a transcript is Whisper guessing rather than reading.
#
# MEASURED on this device against this model, not guessed. Real speech -- the
# Cartesia voice degraded to a quarter volume with room hiss added, including the
# hard cases (single words, spelled letters, Hindi) -- bottomed out at avg_logprob
# -0.65. Non-speech -- digital silence and hiss at five amplitudes, sent with
# both of the seed prompts this device uses -- topped out at -0.69. So there is a
# real gap, and this sits inside it, one notch to the SAFE side: at -0.70 nothing
# real was lost and nine of ten inventions were caught.
#
# Every drop is logged with its score, so if this ever starts eating real speech
# the log says so immediately and the number can be moved from .env.
STT_MIN_LOGPROB = float(os.getenv("STT_MIN_LOGPROB", "-0.70"))

# no_speech_prob is deliberately NOT used. It reads 0.045 on pure digital
# silence when a prompt is supplied -- the prompt suppresses it -- which makes it
# worse than useless here: highest confidence exactly where the invention is.


def looks_hallucinated(text, logprob=None):
    """(True, reason) when this transcript is Whisper's imagination.

    Ordered cheapest-first, and the confidence check is last because it is the
    only one that can be wrong about real speech.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False, ""
    lowered = stripped.lower()
    # Whisper's punctuation on an invented phrase is arbitrary: the same nothing
    # comes back as "है", "है।" and "है.". Matching the words alone keeps the
    # list from needing a row per full stop -- which it was already missing.
    bare = lowered.strip(" .!?,;:\u0964\u0965\u2026\"'-")
    if not bare:
        return True, "punctuation only"
    if lowered in HALLUCINATIONS or bare in HALLUCINATIONS:
        return True, "known phrase"
    if RE_HALLUCINATION.search(lowered):
        return True, "known phrase family"
    if RE_IMPOSSIBLE_SCRIPT.search(stripped):
        return True, "a script nobody here speaks"
    if is_repeated_hallucination(stripped):
        return True, "one phrase looped"

    # THE SEED COMING BACK. The spelling screens prime Whisper with the alphabet
    # so that a child saying letters is read as letters -- and on room tone it
    # hands the alphabet straight back: "L K L M N O P Q R S T U V W X Y Z" out
    # of digital silence, measured. Length and ORDER are what separate that from
    # a child: the longest word in SPELLING_WORDS is four letters, and no child
    # spells six or more of them in alphabetical order.
    letters = [t for t in re.findall(r"[A-Za-z]+", lowered) if len(t) == 1]
    if len(letters) >= 6 and len(letters) == len(re.findall(r"[A-Za-z]+", lowered)):
        rising = sum(1 for a, b in zip(letters, letters[1:]) if b >= a)
        if rising >= 0.8 * (len(letters) - 1):
            return True, "the alphabet seed read back"

    # One letter, alone, is the smallest thing Whisper can invent rather than
    # return nothing -- observed as a bare "Q" out of hiss, at a confidence just
    # inside the threshold. No answer this device asks for is a single letter:
    # spelling wants the whole word, and the Hindi letter question is answered in
    # Devanagari, which this deliberately does not touch.
    if len(bare) == 1 and bare.isascii() and bare.isalpha():
        return True, "a single letter, alone"
    if logprob is not None and logprob < STT_MIN_LOGPROB:
        return True, f"low confidence {logprob:.2f}"
    return False, ""
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
def ai_loop(ui, headless=False):
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

    chat_history = load_history(active_user_id())
    if not chat_history: chat_history = []
    # Whose history is in memory. Compared against the active profile at the top
    # of every turn so Switch User swaps the conversation as well as the band.
    history_for = active_user_id()
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
        # NOTHING TO LISTEN FOR RIGHT NOW.
        #
        # Either a KG student is on the device -- they get the spelling and story
        # screens, which speak through audio_queue on the UI thread and never
        # need the microphone -- or a profile screen is covering the canvas.
        #
        # Parking the whole loop rather than filtering further down is deliberate:
        # it means the mic is never opened and no wake word can be heard, so a
        # pre-reader cannot fall into the open chat flow by accident. The overlay
        # half matters for the same reason -- without it a stray "Hey Liza" during
        # setup would wake her behind a modal screen the child is still using, and
        # she would sit there listening to a room that is not talking to her.
        # Switch User has to change WHO she is talking to, not just how deep she
        # pitches it. Checked here because it is the one point every turn passes
        # through before the prompt is built or the history is read.
        current_user = active_user_id()
        if current_user != history_for:
            save_history(chat_history, history_for)
            chat_history = load_history(current_user)
            history_for = current_user
            print(f"[PROFILE] Switched to a different student's history "
                  f"({len(chat_history)} messages).", flush=True)

        if kg_holds_microphone(ui):
            session_active = False
            silence_counter = 0
            retell_buffer, retell_silence_from, retell_nudged = [], 0.0, False
            wake_event.clear()
            sleep_event.clear()
            # Parked, but not deaf: the spelling screen asks the child to SAY the
            # letters, and this is the only thread that may touch the microphone.
            # Nothing else here opens it, so a KG listen cannot race the wake word.
            if kg_serve_listen(recognizer, mic_device, listener):
                continue

            # No screen was waiting for anything, so the microphone is free --
            # and that gap is where a child's own question used to go to die.
            # They would ask it out loud and the device never opened the
            # microphone at all: standby is the only place the wake word was
            # ever heard, and a KG screen is precisely what keeps this loop out
            # of standby. So the wake word is served HERE as well, on the same
            # thread, in the gaps where nothing else is listening.
            #
            # Never while she is speaking. The STANDBY comment below records
            # what that costs when it is got wrong: wake-word transcripts of
            # her own story narration, taken off the microphone mid-lesson.
            if (WAKE_WORD_ENABLED and not kg_listen_waiting()
                    and not playback_active.is_set() and audio_queue.empty()):
                listen_started[:] = [time.time(),
                                     WAKE_LISTEN_TIMEOUT_S + WAKE_PHRASE_LIMIT_S]
                try:
                    woke, doubt, doubt_language = listen_for_wake_word(
                        recognizer, mic_device, asleep=False, listener=listener,
                        # A screen that asks a question of its own mid-read gets
                        # the microphone back within a frame or two, rather than
                        # after the ten seconds this read is allowed to run.
                        cancel=kg_listen_waiting)
                finally:
                    listen_started[:] = [0.0, 0.0]
                if woke:
                    kg_handle_doubt(ui, doubt, doubt_language,
                                    recognizer, mic_device, listener)
                continue
            time.sleep(0.4)
            continue

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
        if state.pending_mode_intro:
            intro, state.pending_mode_intro = state.pending_mode_intro, None
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

                # STANDBY MUST NOT BE A ONE-WAY DOOR.
                #
                # ai_loop is the only thread allowed to touch the microphone, so
                # a KG listen is served from the park branch at the top of this
                # loop and nowhere else. This inner loop had no exit but the wake
                # word -- so once the device dropped into standby with a KG child
                # on it, ai_loop never got back to the top and never served a
                # single KG request. The spelling and test screens then sat on
                # "I'm listening..." for ever, waiting for an answer from a
                # thread that was busy listening for "Hey Liza" instead. And a
                # pre-reader never says "Hey Liza", so nothing ever released it.
                #
                # It is reached on every start, before the first screen has even
                # been routed, which is why it looked intermittent: whether KG
                # worked at all came down to whether ai_loop or the Tk thread got
                # there first. logs/liza.log has the proof either way -- wake-word
                # transcripts of Liza's own story narration, taken off the
                # microphone while a KG story screen was on the display.
                kg_took_the_microphone = False
                if WAKE_WORD_ENABLED:
                    print(f"[STATE] In {'Sleep' if ui.asleep else 'Standby'} Mode. "
                          f"Say 'Hey Liza' or tap Speak...", flush=True)
                    while not wake_event.is_set():
                        if kg_holds_microphone(ui):
                            kg_took_the_microphone = True
                            break
                        # Timed like every other read. Standby is where the
                        # device spends most of its life, so a wedge here is the
                        # single most likely one -- and it was the one place the
                        # watchdog could not see, because listen_started was
                        # never stamped on this path.
                        listen_started[:] = [time.time(),
                                             WAKE_LISTEN_TIMEOUT_S + WAKE_PHRASE_LIMIT_S]
                        try:
                            woke, pending_question, pending_language = listen_for_wake_word(
                                recognizer, mic_device, asleep=ui.asleep, listener=listener,
                                # A screen that opens mid-read gets the device
                                # back within a frame or two, instead of after
                                # the ten-second window this read is allowed.
                                cancel=lambda: kg_holds_microphone(ui))
                        finally:
                            listen_started[:] = [0.0, 0.0]
                        if woke:
                            break
                else:
                    print("[STATE] In Standby Mode. Tap the screen to wake up...", flush=True)
                    while not wake_event.is_set():
                        if kg_holds_microphone(ui):
                            kg_took_the_microphone = True
                            break
                        time.sleep(0.1)

                if kg_took_the_microphone:
                    # Straight back to the top, where the park branch serves it.
                    # NOT through the lines below: those declare a session awake
                    # and would have her answering a child who only tapped a tile.
                    print("[STATE] A Kindergarten screen needs the microphone; "
                          "leaving standby.", flush=True)
                    continue

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
                    ui_call(lambda: state.ui_instance.set_state("listening"))
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
                if time.time() - state.playback_started_at < BARGE_IN_LEAD_S:
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
                ui_call(lambda: state.ui_instance.set_state("listening"))
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
                if sleep_event.is_set() or state.pending_mode_intro:
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
                if time.time() - state.media_started_at < MEDIA_START_GRACE_S:
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
                ui_call(lambda t=text: state.ui_instance.set_transcript(t, "user"))
            else:
                # The ALSA buffer keeps playing briefly after playback_active
                # clears; opening the mic immediately records Liza's own tail.
                #
                # Skipped when an interruption is already in hand: she has just
                # been cut off mid-sentence, so there is no tail worth waiting
                # out, and the student is still talking.
                carrying = listener is not None and listener.has_carry()
                if not carrying:
                    settle = MIC_SETTLE_SEC - (time.time() - state.last_spoken_at)
                    if settle > 0:
                        time.sleep(settle)

                # Waiting costs no audio any more -- the reader thread buffers
                # straight through it -- but the pre-roll must not reach back
                # past the moment she stopped speaking, or her own tail becomes
                # the opening of the student's sentence. Capping it at the time
                # since she stopped is what keeps both properties at once: the
                # student's first word is recovered, hers is not.
                preroll_ms = min(float(VAD_PREROLL_MS),
                                 max(0.0, (time.time() - state.last_spoken_at) * 1000.0))

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
                        if sleep_event.is_set() or state.pending_mode_intro:
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
                                             or (speech_started_at - state.last_spoken_at) < ECHO_GUARD_SEC)
                        if speaking_recently and text:
                            ai_words = echo_words(state.last_spoken_text)
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
                    
                        # A student who was still mid-question when the pause
                        # threshold fired gets the rest of their sentence back.
                        # Deliberately AFTER the echo guard: the first chunk has
                        # already been shown to be theirs rather than her own
                        # voice bleeding back, so there is nothing to extend on
                        # a turn that was never real. Never in RE-TELL, which
                        # already holds the floor open on its own clock.
                        if (text and not in_retell and not playback_active.is_set()
                                and looks_unfinished(text, phrase_truncated)):
                            # Back to "listening" for the extra window, so the
                            # screen is not claiming to be thinking about an
                            # answer while it is in fact still waiting on them.
                            ui.set_state("listening")
                            ui_call(lambda t=text: state.ui_instance.set_transcript(t, "user"))
                            # Re-armed for the fallback path, where a blocking
                            # recognizer.listen() is about to run again and the
                            # watchdog has nothing else to watch. On the VAD path
                            # listener.read_state covers it either way.
                            listen_started[:] = [
                                time.time(),
                                CONTINUATION_MAX_ROUNDS
                                * (CONTINUATION_WAIT_S + phrase_limit)]
                            text = capture_continuation(
                                listener, recognizer, source, endpointed,
                                recognizer.pause_threshold, phrase_limit,
                                dynamic_stt_prompt, text, phrase_truncated)
                            listen_started[:] = [0.0, 0.0]
                            ui.set_state("thinking")

                        print(f"[TRANSCRIPT] {text if text else '[empty]'}", flush=True)
                        if text:
                            ui_call(lambda t=text: state.ui_instance.set_transcript(t, "user"))
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
            ui_call(lambda q=media_query: state.ui_instance.set_now_playing(q, loading=True))
            print(f"[MEDIA] {media_kind} request: {media_query}", flush=True)
            hit = search_first_video(media_query, media_kind)
            reply = f"Playing {hit['title']}." if hit else f"I couldn't find a {media_kind} for that."
            if not hit:
                ui_call(lambda: state.ui_instance.set_now_playing(None))

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
                    ui_call(lambda: state.ui_instance.set_now_playing(None))

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

        # Checked again HERE, not only at the top of the loop. Switch User is a
        # tap on the Tk thread and lands whenever it lands -- typically while
        # ai_loop is already inside a turn, blocked on the microphone. The top
        # of the loop had then already passed, so the answer was built with the
        # PREVIOUS student's conversation still in the window; logs/liza.log
        # shows the swap logging after the reply had been spoken. The band was
        # right, because student_profile_block reads the store live, but the
        # history was not, which is the whole thing per-student history exists
        # to prevent. This is the last point before the prompt is assembled.
        current_user = active_user_id()
        if current_user != history_for:
            save_history(chat_history, history_for)
            chat_history = load_history(current_user)
            history_for = current_user
            print(f"[PROFILE] Switched student mid-turn; loaded their history "
                  f"({len(chat_history)} messages).", flush=True)

        # One concept note per answered turn. Here rather than at the eight
        # save_history sites because this is the single point every answered
        # question passes through with `text` still in hand, and because a note
        # is worth taking only for a turn that actually became a lesson.
        note_learning(text)

        current_time = datetime.now().strftime("%I:%M %p, %A, %B %d, %Y")
        dynamic_system_prompt = UNIVERSAL_SYSTEM_PROMPT.format(
            education_scope=ASSISTANT_SCOPE,
            emotion_persona=EMOTION_PERSONA,
            agentic_actions=AGENTIC_ACTIONS,
            # Above the mode because it changes LESS often than one -- a mode is
            # a tap away, a band only moves on Switch User -- so this ordering
            # keeps the whole fixed prefix cacheable across a mode change. See
            # the section-order note above UNIVERSAL_SYSTEM_PROMPT.
            grade_guidelines=student_profile_block(),
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
                            state.current_ai_response = full_response 

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

                    # THE RAW MODEL OUTPUT, before any cleaning touches it.
                    # Without this a wrong answer cannot be told apart from a
                    # right one that clean_text_for_tts mangled, and that is
                    # exactly the question the log could not answer when she
                    # opened an answer about the shape of the Earth with
                    # "सपाट है." -- "it is flat". Only the reply is logged, not
                    # the prompt: the prompt is the same every turn and the
                    # answer is the part that varies.
                    if full_response.strip():
                        print(f"[LLM RAW] {full_response.strip()[:700]!r}", flush=True)

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

                        ui_call(lambda: state.ui_instance.set_state("thinking", f"Searching for: {search_query}..."))

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
def main():
    """Start the assistant. Called by assist.py, which is the file the
    launcher runs."""
    # ui_instance is the module global that every ui_call() and ui_invoke()
    # reads to find the screen. Without this it would bind a LOCAL here, the
    # global would stay None, and every update sent from the ai thread would
    # be dropped without a word -- see ui_call().
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
        state.ui_instance = app_ui
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
        state.ui_instance = app_ui
        # THE ROUTING DECISION, made once, before the first turn can happen.
        # No profile at all means a device nobody has set up, so it opens on the
        # picker; a KG profile goes straight to the spelling and story screens
        # and never reaches the normal flow; everyone else lands on the usual
        # screen with their band already in the prompt. Scheduled through
        # root.after so it runs inside the mainloop, where the canvas is real.
        def open_first_screen():
            profile = profiles.active_profile()
            if profile is None:
                print("[PROFILE] No student set up yet; opening the picker.", flush=True)
                app_ui.show_profile_picker()
            else:
                profiles.touch_active()
                print(f"[PROFILE] Active: {profile.get('name')} "
                      f"(Class {profile.get('class')}).", flush=True)
                app_ui.route_for_profile(profile)
        root.after(600, open_first_screen)
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

