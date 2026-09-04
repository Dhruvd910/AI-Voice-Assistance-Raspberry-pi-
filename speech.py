"""Hearing: the microphone, the voice detector, the wake word and Whisper.

The one dongle on this device wedges if its stream is re-opened per listen, so
HeldMicrophone opens it once and never lets go, and VoiceListener reads frames
off it continuously. That is also why ai_loop is the only thread allowed in
here: two readers on one held stream is the failure this whole design avoids.

Every number these read lives in config.py, with the prose explaining what
moving it costs.
"""

import audioop
import collections
import os
import queue
import re
import subprocess
import threading
import time

import pyaudio
import speech_recognition as sr
try:
    import webrtcvad
except ImportError:
    # Not fatal: VoiceListener says so once at startup and the old
    # energy-threshold path is used instead. `pip install webrtcvad-wheels`.
    webrtcvad = None

from config import (BARGE_IN_DEBUG, BARGE_IN_ENABLED, BARGE_IN_LEAD_S,
                    BARGE_IN_MARGIN, BARGE_IN_MS, BARGE_IN_WARMUP_FRAMES,
                    CAPTURE_RATE, CONTINUATION_MAX_ROUNDS, CONTINUATION_WAIT_S,
                    HALLUCINATIONS, MEDIA_BARGE_IN_MARGIN, MIC_CHUNK,
                    MIC_DEVICE_INDEX, MIC_ENERGY_CEILING, MIC_ENERGY_FLOOR,
                    MIC_MAX_GAIN, MIC_TARGET_PEAK, MIN_SPEECH_RMS,
                    MIN_SPEECH_SEC, PAUSE_THRESHOLD_NORMAL, PREFERRED_MIC_NAMES,
                    RE_HALLUCINATION, RE_IMPOSSIBLE_SCRIPT, RE_WAKE_GREETING,
                    RE_WAKE_WORD, RE_WAKE_WORD_ASLEEP, STT_MIN_LOGPROB,
                    STT_MODEL, STT_PROMPT_MAX_CHARS, STT_SEED_PROMPT,
                    VAD_AGGRESSIVENESS, VAD_ENABLED, VAD_FRAME_BYTES,
                    VAD_FRAME_MS, VAD_MIN_RMS, VAD_PREROLL_MS,
                    VAD_RATE, VAD_START_MS, VAD_TAIL_MS,
                    WAKE_BARE_NAME_MAX_WORDS, WAKE_LISTEN_TIMEOUT_S, WAKE_MAX_LEAD_WORDS,
                    WAKE_PHRASE_LIMIT_S, WAKE_SEED_PROMPT, WAKE_SEED_PROMPT_ASLEEP,
                    WAKE_SLEEP_MAX_WORDS, WAKE_STT_MODEL)
from state import media_active, playback_active

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
    clients = assistant.groq_key_order()
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

# ---------------------------------------------------------------------------
# Bound LAST so this module and the assistant import in either order; see the
# same note in ui.py. Only groq_key_order comes back through it -- the rotation
# over the Groq keys, which belongs with the clients in config.
import assistant
