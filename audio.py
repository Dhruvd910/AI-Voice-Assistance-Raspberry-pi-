"""Getting words out of the speaker, and the subtitles that follow them.

One aplay per response, with every sentence of that response streamed into it,
so the seam between two sentences is a breath rather than a process starting.
The captions are cued off the same stream, which is why they are here and not
with the screen: only this file knows when a word actually left the speaker.

NOTE the imports. `from config import ...` and never `import config`, because
audio_player_worker has a local called config -- the Cartesia generation config
for one sentence -- and a module of that name would be shadowed inside the one
function that matters most here.
"""

import queue
import re
import subprocess
import threading
import time

import spoken
import state
from config import (AUDIO_OUTPUT_DEVICE, BYTES_PER_SEC, CARTESIA_MODEL,
                    CARTESIA_SAMPLE_RATE, CARTESIA_SPEED, CARTESIA_VOICE_ID,
                    SPEAKER_VOLUME, VOICE_IDS, cartesia_client)
from state import (audio_queue, caption_lock, caption_state, note_spoken,
                   playback_active, speaker_live, stop_playback_event)
from uibridge import ui_call

def set_speaker_volume():
    """Put the output card at SPEAKER_VOLUME. At every start, because the
    card's level is whatever was last left on it -- a replug, or someone at
    alsamixer, and barge-in quietly stops working. Best effort: the control is
    "Speaker" on this dongle and "PCM" on most others; a card with neither is
    left alone."""
    card = re.search(r"CARD=(\w+)", AUDIO_OUTPUT_DEVICE or "")
    if not SPEAKER_VOLUME or not card:
        return
    for control in ("Speaker", "PCM"):
        try:
            done = subprocess.run(["amixer", "-c", card.group(1), "sset", control, SPEAKER_VOLUME],
                                  capture_output=True, text=True, timeout=5)
        except Exception as exc:
            print(f"[AUDIO] Could not reach amixer to set the volume: {exc}", flush=True)
            return
        if done.returncode == 0:
            print(f"[AUDIO] Speaker at {SPEAKER_VOLUME} on card {card.group(1)}.", flush=True)
            return
    print(f"[AUDIO] No volume control found on card {card.group(1)}; left as it was.", flush=True)


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


# ==========================================
# Bulletproof Audio + Word-Timestamped Subtitles
# ==========================================
def spoken_parts(item):
    """(text, generation_config) for one queue item.

    Everything on audio_queue used to be a bare string. A line may now arrive as
    (text, config) to give Cartesia a delivery as well as words -- see kg_say.
    Kept as one small reader so every consumer agrees on the shape, and so the
    bare-string form keeps working untouched.
    """
    if isinstance(item, tuple):
        return item[0], item[1]
    return item, None


# ---------- captions for a spoken response ----------
# The story screen shows the line she is SAYING, not the whole story at once. A
# block of text a pre-reader cannot read is wallpaper; one line arriving exactly
# as it is spoken is something they can follow. It is also what makes the acting
# legible -- the slow, quiet, frightened beat is ON THE SCREEN while it is being
# said slowly and quietly.
#
# The timing has to come from the player, because a guess is wrong by
# construction: every beat is generated at its own speed (0.72..1.20 -- see
# KG_EMOTIONS) and Cartesia decides its real duration. clock["generated"] is how
# many seconds of audio have been handed to aplay, so it IS the position of the
# next beat on the playback timeline, and clock["start"] is when the speaker
# began. start + offset is the wall-clock instant that line becomes audible.
#
# Sessions exist so a torn-down response cannot caption the one that replaced it:
# the player captures the session number when it picks a response up, and a cue
# under a stale number is dropped.


def caption_begin():
    """Open a caption session for the response about to be queued.

    Call it BEFORE queuing, so the player picks up the new number and anything
    still draining from the old response is left behind on the old one.
    """
    with caption_lock:
        caption_state["session"] += 1
        caption_state["start"] = 0.0
        caption_state["cues"] = []
        return caption_state["session"]


def caption_session():
    with caption_lock:
        return caption_state["session"]


def caption_open(session):
    """Claim the current session for the response starting now.

    A session whose clock has already run belonged to the PREVIOUS response, so
    its cues are stale. Clearing them here is also what stops a session left open
    behind a screen nobody is watching from growing one cue for every line she
    speaks for the rest of the run.
    """
    with caption_lock:
        if session == caption_state["session"] and caption_state["start"]:
            caption_state["start"] = 0.0
            caption_state["cues"] = []


def caption_cue(session, text, offset):
    with caption_lock:
        if session == caption_state["session"]:
            caption_state["cues"].append((offset, text))


def caption_clock_start(session, when):
    with caption_lock:
        if session == caption_state["session"] and not caption_state["start"]:
            caption_state["start"] = when


def caption_now(session):
    """(index, text) of the line that should be on screen right now.

    (-1, None) before the first sound reaches the speaker, and for any session
    that is no longer the current one.
    """
    with caption_lock:
        if session != caption_state["session"] or not caption_state["start"]:
            return -1, None
        elapsed = time.time() - caption_state["start"]
        shown, text = -1, None
        for index, (offset, cue) in enumerate(caption_state["cues"]):
            if offset > elapsed:
                break
            shown, text = index, cue
        return shown, text


# Every line of the response now playing, in the order it was queued -- the
# same order its caption cues are stamped in, so the index caption_now() gives
# is an index into this. Read by unspoken_lines() when barge-in cuts a reply.
_response_lock = threading.Lock()
_response = {"session": None, "lines": [], "ends": []}


def unspoken_lines():
    """The queue items of the playing response from the SENTENCE that is
    audible now to the end, that sentence included, since it was cut part-way.

    A line can hold several sentences -- the streaming splitter sends them in
    batches so the speaker stays fed -- and resuming at the start of the line
    repeated everything already said in it. So the audible line is cut down to
    the sentence the speaker had reached, judged by how far through the line's
    audio playback was.

    Lines still sitting on audio_queue, not yet picked up by the player, are
    included too. Call it BEFORE interrupt_playback(), which throws them away.
    """
    with _response_lock:
        session = _response["session"]
        lines, ends = list(_response["lines"]), list(_response["ends"])
    shown, _text = caption_now(session) if session is not None else (-1, None)
    shown = max(shown, 0)
    remaining = lines[shown:]
    if remaining and shown < len(ends):
        with caption_lock:
            start = caption_state["start"]
            cues = list(caption_state["cues"])
        if start and shown < len(cues):
            begin = cues[shown][0]
            length = ends[shown] - begin
            done = (time.time() - start - begin) / length if length > 0 else 0.0
            text, config = spoken_parts(remaining[0])
            sentences = [x for x in re.split(r'(?<=[.?!।])\s+', text) if x.strip()]
            if len(sentences) > 1 and done > 0:
                total = sum(len(x) for x in sentences)
                reached, index = 0, 0
                for index, sentence in enumerate(sentences):
                    reached += len(sentence)
                    if reached / total > done:
                        break
                rest = " ".join(sentences[index:])
                remaining[0] = (rest, config) if config is not None else rest
    with audio_queue.mutex:
        waiting = list(audio_queue.queue)
    for item in waiting:
        if item is None or (not isinstance(item, tuple) and item == "[END_OF_RESPONSE]"):
            break
        remaining.append(item)
    return remaining


def audio_player_worker():
    while True:
        first_item = audio_queue.get()
        if first_item is None: break
        if first_item == "[END_OF_RESPONSE]":
            audio_queue.task_done()
            continue
        if stop_playback_event.is_set():
            audio_queue.task_done()
            continue

        state.playback_started_at = time.time()
        state.begin_spoken_response()
        playback_active.set()
        # Captured once for the whole response; see caption_begin.
        this_caption = caption_session()
        caption_open(this_caption)
        sentence_queue = queue.Queue()
        sentence_queue.put(first_item)
        with _response_lock:
            _response["session"], _response["lines"] = this_caption, [first_item]
            # Where each line ENDS on the playback timeline, in seconds, filled
            # in as each one finishes generating. With the caption cues (where
            # each starts) this says how far into a line the speaker had got.
            _response["ends"] = []

        try:
            aplay_proc = subprocess.Popen(
                ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(CARTESIA_SAMPLE_RATE),
                 "-c", "1", "-D", AUDIO_OUTPUT_DEVICE],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
            )
            state.active_subprocesses = [aplay_proc]

            # "generated" is how many seconds of audio have been handed to aplay so far,
            # so it doubles as the offset of the next sentence on the playback timeline.
            clock = {"start": 0.0, "generated": 0.0}
            generation_done = threading.Event()
            cancelled = threading.Event()

            def speak_sentence(sentence, config=None):
                # Last resort: neither voice can read Urdu or other Indic scripts, and
                # sending it anyway produces noise rather than speech.
                if assistant.RE_UNREADABLE_SCRIPT.search(sentence):
                    print(f"[TTS] Skipping unreadable script: {sentence}", flush=True)
                    return

                language = assistant.detect_tts_language(sentence)

                # `with` so a barge-in releases the HTTP connection instead of leaking it.
                with cartesia_client.tts.generate_sse(
                    model_id=CARTESIA_MODEL,
                    # Said, not read: "H 2 S O 4", "x squared", "metres per
                    # second". The caption keeps the symbols; see spoken.py.
                    transcript=spoken.speakable(sentence, language),
                    voice={"mode": "id", "id": cartesia_voice_id(language)},
                    language=language,
                    output_format={"container": "raw", "encoding": "pcm_s16le", "sample_rate": CARTESIA_SAMPLE_RATE},
                    # generation_config carries its own float speed, and the two
                    # settings are different types for the same knob, so only one
                    # is ever sent.
                    **({"generation_config": config} if config
                       else {"speed": CARTESIA_SPEED}),
                ) as stream:
                    for event in stream:
                        if stop_playback_event.is_set() or cancelled.is_set(): break
                        event_type = getattr(event, "type", "")

                        if event_type == "chunk":
                            chunk = event.audio
                            if not chunk: continue
                            if not clock["start"]:
                                clock["start"] = time.time()
                                caption_clock_start(this_caption, clock["start"])
                                # Her voice exists from here on, not from when
                                # playback_active went up. See state.speaker_live.
                                state.speaker_live_at = clock["start"]
                                speaker_live.set()
                                ui_call(lambda: state.ui_instance.set_state("speaking"))

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
                        item = sentence_queue.get()
                        if item is None: break
                        if stop_playback_event.is_set() or cancelled.is_set(): break
                        sentence, config = spoken_parts(item)

                        print(f"Liza (speaking): {sentence}", flush=True)
                        # Recorded here rather than at each call site so mode
                        # intros and media confirmations are covered too, not
                        # just LLM answers. The transcript panel is flipped over
                        # to her side from the same spot and for the same
                        # reason: this is the one point every spoken line passes
                        # through, so nothing she says can miss the screen.
                        note_spoken(sentence)
                        # The BOARD is not written here any more. This point is
                        # one whole sentence of generation ahead of the speaker,
                        # so writing the panel from it put the text in front of
                        # the voice. The screen polls caption_now instead and
                        # shows the line that is actually audible; the cue below
                        # is what it reads.
                        # Stamped BEFORE the sentence is generated, because
                        # clock["generated"] is then exactly the audio that
                        # precedes it -- which is where it lands on the timeline.
                        caption_cue(this_caption, sentence, clock["generated"])
                        try:
                            speak_sentence(sentence, config)
                        except Exception as exc:
                            if not (stop_playback_event.is_set() or cancelled.is_set()):
                                print(f"TTS Error: {exc}", flush=True)
                        with _response_lock:
                            _response["ends"].append(clock["generated"])
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
                if not isinstance(sentence, tuple) and sentence == "[END_OF_RESPONSE]":
                    audio_queue.task_done()
                    break
                if stop_playback_event.is_set():
                    audio_queue.task_done()
                    break

                sentence_queue.put(sentence)
                with _response_lock:
                    _response["lines"].append(sentence)
                audio_queue.task_done()

            sentence_queue.put(None)
            generator_thread.join()
            aplay_proc.wait()

        except Exception as e:
            print(f"TTS Error: {e}", flush=True)
        finally:
            ui_call(lambda: state.ui_instance.set_state("idle"))
            state.active_subprocesses.clear()
            # Re-stamped at the true end of playback, so the echo window is
            # measured from when sound actually stopped.
            state.last_spoken_at = time.time()
            state.end_spoken_response()
            speaker_live.clear()
            playback_active.clear()


# ---------------------------------------------------------------------------
# Bound LAST so that this module and the assistant can be imported in either
# order -- see the same note in ui.py. Only two names are read off it, both at
# call time: RE_UNREADABLE_SCRIPT and detect_tts_language, which belong in a
# language module that does not exist yet.
import assistant
