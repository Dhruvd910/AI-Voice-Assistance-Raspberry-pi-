"""The .env, and the numbers a person might want to change without
reading the program.

This module OWNS loading the .env, and does it on import. That is not tidiness:
ui.py reads the KG patience numbers at import time, and every one of them is an
os.getenv call, so whichever module got imported first would otherwise decide
whether the .env was in effect yet. Owning it here means the answer cannot
depend on import order.
"""

import os
import re

from cartesia import Cartesia

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
            # AN INLINE COMMENT IS NOT PART OF THE VALUE.
            #
            #   CARTESIA_API_KEY=sk_car_xxxxxxxx #mine
            #
            # was read as a key with " #mine" stuck on the end of it, and
            # Cartesia answered 401 Invalid API key -- which reads like a bad
            # key rather than a bad line, and sent somebody back to the
            # dashboard to generate another one. The key was fine both times.
            #
            # Only WHITESPACE-then-hash ends a value, so a '#' that is genuinely
            # part of one survives as long as nothing separates it. A quoted
            # value is left alone entirely; the quotes already say where it ends.
            if value[:1] not in "\"'":
                for at in range(1, len(value)):
                    if value[at] == "#" and value[at - 1] in " \t":
                        value = value[:at].rstrip()
                        break
            # Only strip a MATCHED surrounding pair. Stripping quote characters
            # unconditionally corrupts any value that legitimately ends in one,
            # such as the ALSA device name plug:'dmix:CARD=Device_1,DEV=0'.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and not os.getenv(key):
                os.environ[key] = value

# Read the .env before anything below, or anything in another module, calls
# os.getenv. See the note at the top of this file.
load_dotenv()

# The KG screens ask questions that are answered slowly and in pieces, so they
# get their own patience rather than the conversational one. See
# kg_request_listen for what each of the three numbers actually bounds.
#
# Spelling: a child says the letters one at a time with a think between each.
KG_SPELL_START_TIMEOUT_S = float(os.getenv("KG_SPELL_START_TIMEOUT", "10.0"))
KG_SPELL_PHRASE_LIMIT_S = float(os.getenv("KG_SPELL_PHRASE_LIMIT", "22.0"))
KG_SPELL_END_SILENCE_S = float(os.getenv("KG_SPELL_END_SILENCE", "2.2"))

# Counting: a child counting fifteen apples touches each one on the screen and
# says the number, and the gap between "seven" and "eight" is a real pause. At
# the conversational threshold the answer captured was "one, two" -- which is
# the reported bug, not a child who cannot count.
KG_COUNT_START_TIMEOUT_S = float(os.getenv("KG_COUNT_START_TIMEOUT", "12.0"))
KG_COUNT_PHRASE_LIMIT_S = float(os.getenv("KG_COUNT_PHRASE_LIMIT", "45.0"))
KG_COUNT_END_SILENCE_S = float(os.getenv("KG_COUNT_END_SILENCE", "3.0"))


# Where sound goes. The same ALSA device twice: mpv prefixes ALSA names with
# "alsa/", aplay does not.
AUDIO_OUTPUT_DEVICE = os.getenv("AUDIO_OUTPUT_DEVICE", "plug:'dmix:CARD=Device_1,DEV=0'")
# Same device, but mpv prefixes ALSA names with "alsa/".
MPV_AUDIO_DEVICE = os.getenv("MPV_AUDIO_DEVICE", "alsa/plug:'dmix:CARD=Device_1,DEV=0'")


# 2. The Voice (Cartesia API)
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
cartesia_client = Cartesia(api_key=CARTESIA_API_KEY or None)

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


# Devanagari gives \b nothing to anchor to -- the script has no case, and its
# vowel signs are combining marks rather than word characters -- so every name
# spelling below matched happily INSIDE a longer word. Observed waking her on
# "हे लीज़ाश देखे", where लीज़ा is merely the front of लीज़ाश. This asserts that
# nothing which would CONTINUE the word follows: a consonant, a nukta, or a
# virama. A danda or a space may, which is how a real utterance ends.
NAME_END = r'(?![क-ह़्])'

# Bounds on the speech-detection threshold. See clamp_energy() for why both ends
# are needed; `--calibrate-mic` measures the right values for a room.
MIC_ENERGY_FLOOR = int(os.getenv("MIC_ENERGY_FLOOR", "1000"))


# ===========================================================================
# Listening: the microphone, the voice detector, the wake word and Whisper
# ===========================================================================
# Every number here was arrived at against this dongle, in this room, with a
# child in front of it. The prose is kept with each one because that is what
# says which way to move it and what breaks when you do.

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
# PIPEWIRE'S module-echo-cancel WAS THEN TRIED TOO, AND IS NOT THE ANSWER
# EITHER -- not as this device is wired. Everything needed is already on the
# image: libpipewire-module-echo-cancel, libspa-aec-webrtc, and
# libwebrtc-audio-processing. Loaded against the two dongles, with a signal
# played through the virtual sink and both the raw and the cancelled capture
# recorded SIMULTANEOUSLY so that the room noise is identical in each:
#
#   silent floor        raw 384      cancelled 340
#   during playback     raw 781      cancelled 602
#   echo above floor    raw +397     cancelled +261      -3.6 dB
#
# Better than Speex's -2.5 dB and nowhere near the 20-30 dB of a canceller that
# has locked. PipeWire does resample both sides onto a common clock, which was
# the missing piece -- but the residual drift between two independent PCM2902s
# still destroys the sample-level phase an adaptive filter needs.
#
# WHAT WOULD ACTUALLY FIX IT IS A CABLE, NOT A SETTING.
#
# Card 3, the dongle the SPEAKER is on, has a microphone input as well, and
# PipeWire offers it a duplex profile: "Analog Stereo Output + Analog Mono
# Input" (profile index 1 on that card). Both sides on that one dongle is one
# clock domain, which is the condition every attempt so far has lacked.
#
# It was set up and measured, and could not be finished: card 3's microphone
# jack is EMPTY -- its input reads RMS 24, silence -- because the microphone is
# plugged into the other dongle. Moving it across is the experiment, and it
# costs nothing to try:
#
#   1. Plug the microphone into the dongle the speaker is on (card 3,
#      "Device_1"), leaving the other one empty or unplugged.
#   2. wpctl set-profile <card 3 device id> 1
#   3. Load module-echo-cancel with capture.props node.target pointed at
#      alsa_input...Sound_Device-00.2.analog-mono, and measure again the same
#      way. If it locks, the number moves by tens of dB, not by three.
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

# 0.55s is right for a finished sentence and wrong for a sentence still being
# assembled. A student mid-question -- "what is the difference between a... "
# -- pauses for longer than that reaching for the next word, and the phrase was
# being closed on them and the half-question answered as if it were the whole
# one. Rather than charge every turn a slower threshold, the pause stays short
# and an utterance that READS unfinished buys one extra listening window; see
# looks_unfinished() and capture_continuation().
CONTINUATION_WAIT_S = float(os.getenv("CONTINUATION_WAIT", "2.2"))

CONTINUATION_MAX_ROUNDS = int(os.getenv("CONTINUATION_MAX_ROUNDS", "2"))

# Groq rejects a longer STT prompt outright, with a 400.
#
# "Both fit inside Whisper's prompt window" was true of Whisper and false of
# this API. The seed is 443 characters and her last 40 words were appended to
# it, so any reply of ordinary length pushed the total past the cap -- observed
# at 905. And because a 400 is deterministic, the retry could not help and every
# single utterance failed: she went completely deaf, in a way that looks from
# the outside exactly like the microphone having died.
STT_PROMPT_MAX_CHARS = 896

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

MIC_ENERGY_CEILING = int(os.getenv("MIC_ENERGY_CEILING", "1300"))

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


# Whether "Hey Liza" is listened for DURING a Kindergarten lesson. On, but it
# took two goes to make that safe.
#
# It was turned off after one session in logs/liza.log sent 137 reads to
# Whisper, fired 25 times, and 21 of those were "हे लीज़ा" -- what the ambient
# Hindi conversation near this device transcribes as. Three of the 25 produced
# any question at all and none was a question. The other 22 got "What would you
# like to ask me?" followed by "I did not quite catch that": a lesson stopped
# twice by nobody, over and over.
#
# What was wrong there was not the listening, it was the TALKING. A wake word is
# a guess, and this room makes it a bad one, so the guess must be cheap to get
# wrong. It is now: the KG wake path passes announce=False, and she says nothing
# unless something was actually heard. A false wake costs a listen nobody
# notices instead of two spoken sentences.
#
# The read also stands down the moment she starts speaking, which is what
# stopped thirteen of her own sentences being transcribed as somebody trying to
# wake her.
#
# What it still costs is a Whisper call per utterance in the room, the same as
# standby. Set KG_WAKE_WORD=0 if that matters more than hands-free asking; the
# Ask button on the lesson screens does not depend on it.
KG_WAKE_WORD_ENABLED = os.getenv("KG_WAKE_WORD", "1") != "0"

