"""The Kindergarten flow: how a pre-reader is listened to, and how she answers.

The screens themselves are in ui.py. What is here is everything underneath
them -- asking ai_loop for a listen and matching the answer to the question that
asked for it, the delivery tones her voice is given, and answering a question a
child stops the lesson to ask.

ai_loop is the only thread allowed near the microphone, so nothing here opens
one. A screen posts a request and polls for the answer; ai_loop serves it from
the park branch. That indirection is the whole design, and kg_serve_listen and
kg_holds_microphone are the two ends of it.
"""

import queue
import random
import time

import speech_recognition as sr

import profiles
import state
from media import stop_media_playback
from state import (_kg_listen_lock, _kg_listen_next, _kg_listen_valid_from,
                   audio_queue, kg_active, kg_listen_requests, kg_listen_results,
                   playback_active)
from uibridge import ui_invoke

def kg_next_listen_id():
    """Claim an id, and abandon every listen still outstanding.

    One KG screen asks one question at a time, so a new question always means
    the previous one no longer matters -- there is no case where two KG listens
    are both wanted at once.
    """
    with _kg_listen_lock:
        listen_id = _kg_listen_next[0]
        _kg_listen_next[0] += 1
        _kg_listen_valid_from[0] = listen_id
        return listen_id


def kg_cancel_listen():
    """Abandon every outstanding listen without starting a new one.

    Called when a screen is left: the child has walked away from the question,
    so the microphone should stop waiting for an answer to it rather than
    holding ai_loop for another forty seconds.
    """
    with _kg_listen_lock:
        _kg_listen_valid_from[0] = _kg_listen_next[0]


def kg_listen_abandoned(listen_id):
    with _kg_listen_lock:
        return listen_id < _kg_listen_valid_from[0]


# Seeding Whisper with the alphabet is right for "spell it" and WRONG for
# "what is this?" -- logs/liza.log shows a child answering "crown" coming back
# as 'Q R O N' and "kite" as 'K I T', because the seed taught it to expect
# letters. So the seed now follows the question rather than being fixed.
KG_SEED_LETTERS = "A B C D E F G H I J K L M N O P Q R S T U V W X Y Z"


def kg_request_listen(seconds=6.0, seed="", language="en", phrase_limit=None,
                      end_silence=None):
    """Ask ai_loop for one transcribed utterance. Answer arrives on kg_listen_results.

    `seconds` bounds only the wait for the child to START, `phrase_limit` caps
    how long they may go on for, and `end_silence` is the pause that ends their
    turn. All three used to be one number, which is what made the spelling
    screen cut children off: a child spelling out loud says "C", thinks, says
    "A", thinks, says "T", and every one of those thinks is longer than the
    conversational 0.55s pause that closed the phrase. The transcript then held
    a single letter and was marked as a wrong spelling. Counting aloud is slower
    still. So each KG screen now says how patient its own question deserves to be.
    """
    listen_id = kg_next_listen_id()
    kg_listen_requests.put({"id": listen_id,
                            "seconds": seconds, "seed": seed, "language": language,
                            "phrase_limit": phrase_limit or seconds,
                            "end_silence": end_silence or assistant.PAUSE_THRESHOLD_NORMAL})
    return listen_id


def kg_serve_listen(recognizer, mic_device, listener):
    """Run one pending KG listen, if any. Called from ai_loop's KG park branch.

    Returns True when it did something, so the caller can skip its idle sleep.
    """
    try:
        request = kg_listen_requests.get_nowait()
    except queue.Empty:
        return False
    listen_id = request.get("id", 0)
    # The screen that asked this has already gone. Nothing to listen for, and
    # nobody left to hear the answer.
    if kg_listen_abandoned(listen_id):
        print(f"[KG] Listen {listen_id} was dropped before it began.", flush=True)
        return True
    abandoned = lambda: kg_listen_abandoned(listen_id)
    text = ""
    try:
        seconds = float(request.get("seconds", 6.0))
        text = kg_capture_utterance(
            recognizer, mic_device, listener, seconds,
            float(request.get("phrase_limit") or seconds),
            float(request.get("end_silence") or assistant.PAUSE_THRESHOLD_NORMAL),
            # Never seeded with the answer itself: priming Whisper with the
            # target word would have it hand that word back on noise, which is
            # the same trap WAKE_SEED_PROMPT set for the wake word. The caller
            # says whether it expects letters or a word, and in which language.
            seed=request.get("seed", ""), language=request.get("language") or None,
            cancel=abandoned, label=f"KG] Listen {listen_id}")
        print(f"[KG] Listen {listen_id} heard: {text!r}", flush=True)
    except Exception as exc:
        print(f"[KG] Listen {listen_id} failed: {exc}", flush=True)
    if abandoned():
        # Nobody is waiting for this any more, and nobody can be: every id still
        # in play is newer than this one. Posting it would only leave litter on
        # the queue for the next screen to sort through.
        print(f"[KG] Listen {listen_id} was dropped; the screen had moved on.",
              flush=True)
        return True
    kg_listen_results.put({"id": listen_id, "text": text or ""})
    return True


def kg_capture_utterance(recognizer, mic_device, listener, seconds, phrase_limit,
                         end_silence, seed="", language=None, cancel=None,
                         label="KG"):
    """One transcribed utterance from the microphone. "" when nothing usable came.

    Lifted out of kg_serve_listen so that a question the child asks for
    THEMSELVES is captured on exactly the same terms as an answer to a question
    she asked them: same speech check, same echo guard, same cancel. Two copies
    of this would have been two places to fix the next thing Whisper does to a
    four-year-old.
    """
    cancel = cancel or (lambda: False)
    text = ""
    audio = None
    if listener is not None and listener.available:
        audio = listener.wait_for_utterance(seconds, phrase_limit, end_silence,
                                            cancel=cancel)
    elif mic_device is not None:
        previous_pause = recognizer.pause_threshold
        try:
            recognizer.pause_threshold = end_silence
            with mic_device as source:
                audio = recognizer.listen(source, timeout=seconds,
                                          phrase_time_limit=phrase_limit)
        except sr.WaitTimeoutError:
            audio = None
        finally:
            recognizer.pause_threshold = previous_pause
    if audio is not None and not cancel() and assistant.is_probably_speech(audio, label, True):
        wav = audio.get_wav_data(convert_rate=16000, convert_width=2)
        text, _lang = assistant.transcribe(wav, seed, language=language or None)
    if text and assistant.sounds_like_her_own_prompt(text):
        print(f"[{label}] was her own voice coming back, not an answer: {text!r}",
              flush=True)
        text = ""
    return text


def kg_listen_waiting():
    """True when a KG screen has a question of its own waiting to be heard.

    The wake word may only use the microphone in the gaps between those, so
    this is what stands the wake read down again -- see the park branch.
    """
    return not kg_listen_requests.empty()


def kg_wait_until_quiet(settle_s=0.4, limit_s=30.0):
    """Block until she has ACTUALLY stopped talking.

    ai_loop's side of TutorUI._kg_after_speaking, and for the same reason: a
    microphone opened while she is still speaking records her own prompt and
    hands it straight back as the child's answer. playback_active and the queue
    tell the truth, so wait on them rather than guessing a duration.
    """
    deadline = time.time() + limit_s
    while ((playback_active.is_set() or not audio_queue.empty())
           and time.time() < deadline):
        time.sleep(0.1)
    time.sleep(settle_s)


# A child asking their own question needs far longer than a conversational
# turn. They start, stop, think, and start again -- the same pauses that made
# the spelling screen cut them off mid-word before each KG screen was allowed
# to say how patient its own question deserved to be.
KG_DOUBT_WAIT_S = 8.0        # how long to wait for them to start at all
KG_DOUBT_PHRASE_S = 14.0     # how long they may then go on for
KG_DOUBT_PAUSE_S = 1.4       # the silence that ends their turn

KG_DOUBT_PROMPT = (
    "You are Liza, teaching a child of about four to six. The child is in the "
    "middle of a lesson and has stopped to ask you something of their own. "
    "Answer THAT question in one or two short sentences, in words a child that "
    "age already has. Speak it -- no lists, no markdown, no brackets, nothing a "
    "voice cannot read aloud. If nobody really knows the answer, say so; that "
    "is a real answer and children can hear it. Never tell them the question "
    "was silly, or wrong, or not what you were doing -- a child told that once "
    "stops asking.\n\n"
    "Answer in ENGLISH, or in HINDI WRITTEN IN DEVANAGARI. Those are the only "
    "two her voice can read, and anything else is not spoken at all -- to the "
    "child that is the device ignoring them. If the question reaches you in "
    "some other script it was misheard on the way in, most likely Hindi "
    "transcribed as Urdu; answer it in Hindi, in Devanagari. If it is too "
    "garbled to answer, say in Hindi that you did not catch it and ask them to "
    "say it again.")


def kg_answer_doubt(question, language="en"):
    """One short answer to a question the child asked for themselves.

    A single call rather than a turn through ai_loop's history: the alphabet is
    still on the screen behind this, and the answer has to arrive in a breath or
    two rather than after a full conversational turn. Same shape as
    phrase_action_result, and for the same reason.
    """
    question = (question or "").strip()
    if not question:
        return ""
    profile = profiles.active_profile() or {}
    who = profile.get("name") or "A child"
    try:
        done = assistant.openrouter_client.with_options(max_retries=0).chat.completions.create(
            model=assistant.LLM_MODEL,
            messages=[{"role": "system", "content": KG_DOUBT_PROMPT},
                      {"role": "user", "content": f"{who} asks: {question}"}],
            max_tokens=180, temperature=0.5,
            extra_body={"reasoning": {"enabled": False}})
        answer = (done.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"[KG] Could not answer the doubt ({exc}).", flush=True)
        return ""
    # The prompt asks for English or Devanagari; this is what happens when it
    # does not get it. Observed in logs/liza.log: an Urdu question drew an Urdu
    # answer, and the player dropped it with "Skipping unreadable script" -- so
    # a child asked something and Liza simply went quiet. Silence is the one
    # answer that teaches them not to ask again.
    if answer and assistant.RE_UNREADABLE_SCRIPT.search(answer):
        print(f"[KG] Answer came back in a script the voice cannot read; "
              f"asking again in English: {answer!r}", flush=True)
        try:
            done = assistant.openrouter_client.with_options(max_retries=0).chat.completions.create(
                model=assistant.LLM_MODEL,
                messages=[{"role": "system", "content": KG_DOUBT_PROMPT},
                          {"role": "user", "content":
                           f"{who} asks: {question}\n\nAnswer in English only."}],
                max_tokens=180, temperature=0.5,
                extra_body={"reasoning": {"enabled": False}})
            answer = (done.choices[0].message.content or "").strip()
        except Exception as exc:
            print(f"[KG] The retry failed too ({exc}).", flush=True)
            return ""
        if assistant.RE_UNREADABLE_SCRIPT.search(answer):
            return ""
    return answer


def kg_handle_doubt(ui, question, language, recognizer, mic_device, listener):
    """Answer a question asked mid-lesson, without ending the lesson.

    The screen never changes. They are looking at M is for Moon while they ask
    why the moon is white, and taking the letter away to answer would lose the
    very thing they asked about.

    Handled here rather than by handing the microphone back to ai_loop's normal
    turn, because that path ends in standby -- and standby with a KG screen up
    is the deadlock the park branch exists to avoid. See the STANDBY MUST NOT BE
    A ONE-WAY DOOR comment below for what that looked like when it happened.
    """
    if not question:
        # They said her name and stopped, which is most of the time at this age.
        kg_say("Yes? What would you like to ask me?", "curious")
        kg_wait_until_quiet()
        question = kg_capture_utterance(
            recognizer, mic_device, listener, KG_DOUBT_WAIT_S,
            KG_DOUBT_PHRASE_S, KG_DOUBT_PAUSE_S,
            cancel=kg_listen_waiting, label="KG-DOUBT")
    question = (question or "").strip()
    if not question:
        kg_say("I did not quite catch that. Ask me again whenever you like.",
               "gentle")
        return
    print(f"[KG] Doubt asked: {question!r}", flush=True)
    ui_invoke("set_state", "thinking")
    answer = kg_answer_doubt(question, language)
    if not answer:
        kg_say("I am not sure about that one. Let us try again in a moment.",
               "gentle")
        return
    print(f"[KG] Doubt answered: {answer!r}", flush=True)
    # No tone: detect_tts_language picks the voice off the script, so an answer
    # that came back in Hindi is spoken in Hindi without being told to.
    kg_say(answer, "warm")


def kg_holds_microphone(ui):
    """True when a KG or profile screen owns the microphone.

    ai_loop is the only thread allowed near the capture device, so a KG listen
    is served from its park branch and nowhere else. Anything that keeps ai_loop
    away from the top of its loop therefore stops KG hearing anything at all --
    which is why this condition is a named function rather than an expression
    repeated in one place and forgotten in the other.
    """
    return bool(kg_active.is_set() or getattr(ui, "overlay", None))


def set_kg_active(active):
    if active:
        # Whatever she was doing was for the previous, older student.
        kg_active.set()
        assistant.interrupt_playback()
        stop_media_playback()
    else:
        kg_active.clear()


# Cartesia's own enum, not free text. The emotion is a GENERATION parameter, so
# it changes how the line is delivered and is never part of the transcript --
# measured: the same sentence at emotion=excited and emotion=mysterious returned
# an identical word list and different durations (3.20s / 3.28s / 3.60s). That is
# the whole point. Writing "happily," into the sentence would make her SAY the
# word; this makes her SOUND it.
# Speed and volume move WITH the emotion, because that is how a person reads to a
# child: the scary bit is slow and quiet, the exciting bit is fast and loud. The
# first version of this table span only 0.85..1.05 and every tone sounded the
# same. Measured on the same sentence, speed 0.6 gives 4.24s against 2.80s at
# 1.3, and saturates past about 1.3 -- so this uses 0.7..1.2, which is the range
# that is actually audible.
KG_EMOTIONS = {
    # fast and loud -- the payoff moments
    "excited":       {"emotion": "excited",       "speed": 1.20, "volume": 1.3},
    "encouraging":   {"emotion": "enthusiastic",  "speed": 1.05, "volume": 1.15},
    "proud":         {"emotion": "proud",         "speed": 1.00, "volume": 1.2},
    # middle -- narration and asking
    "amazed":        {"emotion": "amazed",        "speed": 1.00, "volume": 1.25},
    "curious":       {"emotion": "curious",       "speed": 0.95, "volume": 1.0},
    "storyteller":   {"emotion": "contemplative", "speed": 0.90, "volume": 1.0},
    "warm":          {"emotion": "affectionate",  "speed": 0.90, "volume": 1.0},
    # slow and quiet -- worry, suspense, kindness
    "gentle":        {"emotion": "calm",          "speed": 0.85, "volume": 0.9},
    "sad":           {"emotion": "sad",           "speed": 0.80, "volume": 0.85},
    "mysterious":    {"emotion": "mysterious",    "speed": 0.72, "volume": 0.8},
}


def kg_delivery(name):
    """The Cartesia generation_config for a KG tone name, or None."""
    return KG_EMOTIONS.get(name)


def kg_say(text, tone=None):
    """Speak one line on the KG screens, through the one existing TTS pipeline.

    Same audio_queue every other spoken line goes through, so the ducking and
    the barge-in machinery all behave exactly as they do elsewhere. The
    interrupt first is what makes the buttons feel responsive: a child taps Next
    Word three times, and without it they would queue up three words deep.

    `tone` names an entry in KG_EMOTIONS and changes how the line is DELIVERED.
    """
    kg_say_many([(text, tone)])


def kg_say_many(segments):
    """Speak several lines, each with its own delivery, as ONE response.

    All of them go in before the single [END_OF_RESPONSE], which matters: the
    player starts one aplay per response and streams every sentence into it, so
    ending the response per line would tear a story into separate playbacks with
    a process start-up gap at each seam. One response means the emotion changes
    between sentences while the audio stays continuous.
    """
    assistant.interrupt_playback()
    for text, tone in segments:
        if not text:
            continue
        audio_queue.put((text, kg_delivery(tone)) if tone else text)
    audio_queue.put("[END_OF_RESPONSE]")

# ---------------------------------------------------------------------------
# Bound LAST so this module and the assistant import in either order; see the
# same note in ui.py. What comes back through it -- transcribe,
# is_probably_speech, sounds_like_her_own_prompt, interrupt_playback and the
# model client -- belongs in the speech, audio and llm modules that have not
# been split out yet.
import assistant
