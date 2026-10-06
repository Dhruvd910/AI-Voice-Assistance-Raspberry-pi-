"""The camera: what Liza sees, live on the Transcribe Board.

Turned on, it stays on -- by voice ("camera on", "कैमरा ऑन करो"), by the camera
button on the board, or by simply asking her about something ("ये क्या है?",
"read this page"), which turns it on if it was off. While it is on, the live
picture is on the board and every question goes to the model with what the
camera sees at that moment, so "what is this?" is answered at once about
whatever is being held up, and "and this one?" about the next thing. It goes
off by voice, by the cross on the picture, and by itself on Sleep, when another
screen covers the board, when she draws something on the board, or after
CAMERA_IDLE_OFF_S with nobody asking anything.

Two kinds of thing are held up to it: an OBJECT -- a leaf, a toy, a coin -- or
a PAGE -- a textbook, a notebook, a worksheet -- to be read, solved or checked.
Both go to the same model (Gemini, through OpenRouter), which reads print and
handwriting and names objects far better than anything that would run on the
Pi; nothing is recognised here.

WHY rpicam-vid IN A SUBPROCESS, not picamera2. The venv has no libcamera
bindings -- they exist only for the system python -- and a camera that wedges
(an unseated ribbon, a sensor that stops answering) is then a process that can
be killed, rather than a thread inside Liza that cannot. One MJPEG stream does
both jobs: every frame is a JPEG, so the preview is cheap to decode at board
size, and the picture she is sent is the sharpest of the last half-second of
frames at full size. About a tenth of one core at 8 fps, measured.

Nothing here talks to the model and nothing imports the assistant. ai_loop
calls camera_command and wants_a_look to decide, look() for the frame, and
attach() to put it on the outgoing message.
"""

import base64
import collections
import io
import os
import re
import shutil
import subprocess
import threading
import time

from PIL import Image, ImageFilter, ImageOps, ImageStat

import state
from config import (CAMERA_ENABLED, CAMERA_FPS, CAMERA_HEIGHT, CAMERA_HFLIP,
                    CAMERA_IDLE_OFF_S, CAMERA_INDEX, CAMERA_KEEP_PHOTOS,
                    CAMERA_PREVIEW_MIRROR, CAMERA_ROTATION, CAMERA_SEND_MAX,
                    CAMERA_SETTLE_S, CAMERA_VFLIP, CAMERA_WIDTH)
from media import _die_with_parent
from uibridge import ui_call, ui_invoke

PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "pictures", "camera")

# Sensors with a focus motor. Only these are given the autofocus options: a
# fixed-focus module (v1, v2, the HQ camera's sensor) has no such control.
AUTOFOCUS_SENSORS = ("imx708", "imx519", "arducam_64mp", "imx682")

# How long the camera has to produce its first frame. rpicam-vid takes about a
# second and a half to start on this Pi; four is a camera that is not going to.
FIRST_FRAME_TIMEOUT_S = 4.0
# The preview's size when the screen does not say (TutorUI.VIEWFINDER_SIZE).
PREVIEW_BOX = (760, 400)

# What state.current_visual holds while the camera is on the board. One object,
# so "is the camera what is on the board" is an identity check: anything else
# drawn there is a new dict.
LIVE_BOARD = {"kind": "camera", "steps": [], "title": "the live camera view"}


# ------------------------------------------------------------------ is there one
_probe = {"at": 0.0, "sensor": None}
_probe_lock = threading.Lock()
RE_CAMERA_LINE = re.compile(r"^\s*(\d+)\s*:\s*(\S+)\s*\[", re.MULTILINE)


def sensor():
    """The sensor's name ("imx708", "imx219", "ov5647"), or None with no camera.

    A camera is found once and believed for the rest of the run: a ribbon cable
    is not hot-pluggable, so one that was there at boot is there now. No camera
    is asked again after a minute, in case it was a USB one plugged in since.
    """
    if not CAMERA_ENABLED:
        return None
    with _probe_lock:
        if _probe["sensor"] or time.time() - _probe["at"] < 60:
            return _probe["sensor"]
        _probe["at"] = time.time()
        tool = shutil.which("rpicam-hello")
        if not tool:
            print("[CAMERA] rpicam-apps is not installed.", flush=True)
            return None
        try:
            out = subprocess.run([tool, "--list-cameras"], capture_output=True,
                                 text=True, timeout=8)
            listed = out.stdout + out.stderr
        except Exception as exc:
            print(f"[CAMERA] Could not list cameras ({exc}).", flush=True)
            return None
        found = {int(i): name for i, name in RE_CAMERA_LINE.findall(listed)}
        _probe["sensor"] = found.get(CAMERA_INDEX)
        if _probe["sensor"]:
            print(f"[CAMERA] Camera {CAMERA_INDEX} is an {_probe['sensor']}.",
                  flush=True)
        elif found:
            print(f"[CAMERA] No camera {CAMERA_INDEX}; libcamera has "
                  f"{found}. Set CAMERA_INDEX.", flush=True)
        else:
            print("[CAMERA] No camera found. Check the ribbon: power off, "
                  "reseat both ends, contacts the right way round.", flush=True)
        return _probe["sensor"]


# ------------------------------------------------------------------ the stream
class Stream:
    """rpicam-vid writing MJPEG to a pipe, and the last few frames out of it."""

    def __init__(self, on_frame=None):
        self.on_frame = on_frame
        self.frames = collections.deque(maxlen=4)   # (time, jpeg bytes)
        self.first_frame = threading.Event()
        self.first_at = 0.0
        self.proc = None
        self.error = ""
        self._lock = threading.Lock()

    def start(self, name):
        tool = shutil.which("rpicam-vid")
        if not tool:
            self.error = "rpicam-vid is not installed"
            return False
        cmd = [tool, "-t", "0", "-n", "--camera", str(CAMERA_INDEX),
               "--codec", "mjpeg", "-q", "90", "--flush",
               "--width", str(CAMERA_WIDTH), "--height", str(CAMERA_HEIGHT),
               "--framerate", str(CAMERA_FPS), "-o", "-"]
        if CAMERA_ROTATION == 180:
            cmd += ["--rotation", "180"]
        if CAMERA_HFLIP:
            cmd.append("--hflip")
        if CAMERA_VFLIP:
            cmd.append("--vflip")
        if name and name.startswith(AUTOFOCUS_SENSORS):
            # Continuous, so a book brought in from arm's length is in focus
            # by the time it is asked about, without anyone asking for that.
            cmd += ["--autofocus-mode", "continuous"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0,
                                         preexec_fn=_die_with_parent)
        except Exception as exc:
            self.error = str(exc)
            return False
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc,),
                         daemon=True).start()
        return True

    def running(self):
        proc = self.proc
        return proc is not None and proc.poll() is None

    def _read(self, proc):
        """Cut the pipe into JPEGs. A frame runs from FFD8 to the next FFD9:
        inside the compressed data a real FF is always followed by 00 or a
        restart marker, so FFD9 cannot occur before the end of the frame."""
        buf = b""
        out = proc.stdout
        while True:
            try:
                chunk = out.read(65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    buf = buf[start:]
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                with self._lock:
                    self.frames.append((time.time(), frame))
                if not self.first_frame.is_set():
                    self.first_at = time.time()
                    self.first_frame.set()
                if self.on_frame is not None:
                    try:
                        self.on_frame(frame)
                    except Exception as exc:
                        print(f"[CAMERA] Preview failed ({exc}).", flush=True)

    def _drain_stderr(self, proc):
        """Keep the last thing it complained about, for the log."""
        try:
            for line in proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text and ("ERROR" in text or "rror" in text):
                    self.error = text[-200:]
        except Exception:
            pass

    def latest(self):
        with self._lock:
            return list(self.frames)

    def stop(self):
        """Measured at 15ms for rpicam-vid to exit on SIGTERM, which is why
        the screen's own thread can call this from a button."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def sharpness(jpeg):
    """How much edge there is in a frame. Higher is sharper; a frame taken
    while the book was still moving scores low. Measured on an eighth-size
    decode, which JPEG gives almost for free."""
    image = Image.open(io.BytesIO(jpeg))
    image.draft("L", (image.width // 8, image.height // 8))
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    return ImageStat.Stat(edges).var[0]


def preview_image(jpeg, box=PREVIEW_BOX):
    """A frame decoded at a size the screen can show, off the Tk thread.
    draft() lets the JPEG decoder skip straight to the nearest power-of-two
    reduction at least `box` big, which is most of the work saved."""
    image = Image.open(io.BytesIO(jpeg))
    image.draft("RGB", box)
    image = image.convert("RGB")
    image.thumbnail(box, Image.BILINEAR)
    if CAMERA_PREVIEW_MIRROR:
        image = ImageOps.mirror(image)
    return image


# ------------------------------------------------------------------ the photo
class Photo:
    """What the camera saw when one question was asked."""

    def __init__(self, jpeg, asked):
        self.taken_at = time.time()
        self.asked = asked
        image = Image.open(io.BytesIO(jpeg)).convert("RGB")
        self.size = image.size
        if max(image.size) > CAMERA_SEND_MAX:
            image.thumbnail((CAMERA_SEND_MAX, CAMERA_SEND_MAX), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, "JPEG", quality=85)
        self.jpeg = out.getvalue()
        self.path = _save(self.jpeg)

    def data_url(self):
        return "data:image/jpeg;base64," + base64.b64encode(self.jpeg).decode()


def _save(jpeg):
    """Keep what she was sent, so a strange answer can be checked against
    what she actually saw -- only the last few: they are pictures of a
    child's room."""
    try:
        os.makedirs(PHOTO_DIR, exist_ok=True)
        path = os.path.join(PHOTO_DIR, time.strftime("look-%Y%m%d-%H%M%S.jpg"))
        with open(path, "wb") as f:
            f.write(jpeg)
        old = sorted(n for n in os.listdir(PHOTO_DIR) if n.startswith("look-"))
        for name in old[:-max(1, CAMERA_KEEP_PHOTOS)]:
            os.remove(os.path.join(PHOTO_DIR, name))
        return path
    except OSError:
        return ""


# ------------------------------------------------------------------ live
_live = {"stream": None, "used_at": 0.0}
_live_lock = threading.RLock()


def live():
    """Whether the camera is on right now."""
    stream = _live["stream"]
    return stream is not None and stream.running()


def start_live():
    """Turn the camera on, live on the board. True once it is on -- or was
    already. Safe from the Tk thread: nothing in it waits on the camera."""
    with _live_lock:
        if live():
            _live["used_at"] = time.time()
            return True
        if _live["stream"] is not None:     # it died on its own; tidy up first
            stop_live("the camera had stopped")
        name = sensor()
        if not name:
            return False
        box = getattr(state.ui_instance, "VIEWFINDER_SIZE", PREVIEW_BOX)
        # Only the newest frame is ever waiting for the screen. Frames arrive
        # faster than Tk turns them into PhotoImages when the UI is busy, and a
        # queue of them would show the student where the book WAS.
        pending = {"busy": False}

        def show(jpeg):
            if pending["busy"]:
                return
            try:
                image = preview_image(jpeg, box)
            except Exception:
                return          # one torn frame; the next one will do
            pending["busy"] = True

            def paint():
                pending["busy"] = False
                ui = state.ui_instance
                if ui is not None and hasattr(ui, "viewfinder_frame"):
                    ui.viewfinder_frame(image)
            ui_call(paint)

        stream = Stream(on_frame=show)
        # Whatever was on the board belonged to the last question, and the
        # camera is going over it; taking it down keeps ON_BOARD honest.
        ui_invoke("clear_visual")
        ui_invoke("open_viewfinder")
        if not stream.start(name):
            print(f"[CAMERA] Could not start the camera: {stream.error}", flush=True)
            ui_invoke("close_viewfinder")
            return False
        _live.update(stream=stream, used_at=time.time())
        state.current_visual = LIVE_BOARD
        state.current_graph = None
        ui_invoke("set_camera_button", True)
        threading.Thread(target=_watch, args=(stream,), daemon=True).start()
        print("[CAMERA] On, live on the board.", flush=True)
        return True


def stop_live(why=""):
    """Turn the camera off. True if it was on."""
    with _live_lock:
        stream, _live["stream"] = _live["stream"], None
    if stream is None:
        return False
    stream.stop()
    ui_invoke("close_viewfinder")
    ui_invoke("set_camera_button", False)
    if state.current_visual is LIVE_BOARD:
        state.current_visual = None
    print(f"[CAMERA] Off{f' ({why})' if why else ''}.", flush=True)
    return True


def toggle_live():
    """The camera button on the board."""
    if live():
        stop_live("the button")
        return False
    return start_live()


def _watch(stream):
    """Put the camera away when nothing needs it. Checked once a second for as
    long as this stream is the live one."""
    while _live["stream"] is stream:
        time.sleep(1.0)
        if _live["stream"] is not stream:
            return
        ui = state.ui_instance
        why = None
        if not stream.running():
            why = f"the camera stopped: {stream.error or 'no reason given'}"
        elif state.sleep_event.is_set():
            why = "asleep"
        elif getattr(ui, "overlay", None):
            why = "another screen is over the board"
        elif time.time() - _live["used_at"] > CAMERA_IDLE_OFF_S:
            why = f"nobody asked anything for {CAMERA_IDLE_OFF_S / 60:.0f} minutes"
        if why:
            stop_live(why)
            return


def look(asked):
    """What the camera sees right now, for this question: (Photo, "") -- or
    (None, why), why being "no_camera", "cancelled" or "failed". Turns the
    camera on if it was off, and then waits for the first frames and for the
    exposure to settle; already on, it returns at once."""
    if not live() and not start_live():
        return None, ("no_camera" if not sensor() else "failed")
    stream = _live["stream"]
    if stream is None:
        return None, "cancelled"
    _live["used_at"] = time.time()
    if not stream.first_frame.wait(FIRST_FRAME_TIMEOUT_S):
        print(f"[CAMERA] No picture from the camera "
              f"({stream.error or 'nothing on the pipe'}).", flush=True)
        stop_live("no picture")
        return None, "failed"
    # Just turned on: the first frames are dark and the wrong colour until the
    # exposure and white balance have caught up with the room.
    while time.time() < stream.first_at + CAMERA_SETTLE_S:
        if _live["stream"] is not stream:
            return None, "cancelled"        # the cross, while it was settling
        time.sleep(0.05)
    frames = stream.latest()
    if not frames:
        return None, "failed"
    try:
        # The sharpest of the last half second, so a book still settling in
        # the hand does not decide it. The newest wins a tie.
        best = max(reversed(frames), key=lambda f: sharpness(f[1]))[1]
        photo = Photo(best, asked)
    except Exception as exc:
        print(f"[CAMERA] Could not read the frame ({exc}).", flush=True)
        return None, "failed"
    ui_invoke("viewfinder_flash")
    print(f"[CAMERA] Looked: {photo.size[0]}x{photo.size[1]}, sending "
          f"{len(photo.jpeg) // 1024} KB.", flush=True)
    return photo, ""


def state_line():
    """CAMERA: for the device state block, so she knows whether she can see."""
    if not CAMERA_ENABLED:
        return "turned off on this device (you cannot see)"
    if live():
        return ("ON, live on the board. What it sees right now comes with their "
                "message -- see section M")
    # Not "off": told the camera was off, she said so and asked whether to
    # turn it on, instead of turning it on -- which the tag does by itself.
    return ("ready -- you see the moment you tag [ACTION: look], which turns it "
            "on by itself; never ask whether to turn it on (section M)" if sensor()
            else "not connected (you cannot see right now)")


# ------------------------------------------------------------------ the message
def attach(messages, photo, note):
    """`messages` with the photo on the last user turn. A copy: the history
    itself never holds an image -- it is saved to disk on every turn, and is
    resent in full, so a photo in it would ride along with every question
    afterwards whether or not it had anything to do with them."""
    if photo is None:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            text = messages[i].get("content")
            if not isinstance(text, str):
                return messages
            # The picture first, then the words: Gemini's own guidance for a
            # single image, and it puts the note -- which ends on the reply
            # format -- last, where it is read last.
            turn = {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": photo.data_url()}},
                {"type": "text", "text": f"{text}\n\n{note}"},
            ]}
            return messages[:i] + [turn] + messages[i + 1:]
    return messages


# ------------------------------------------------------------------ when to look
# STRONG: unmistakably asking her to look. Taken whatever is on the board.
#
# Built from what students say, and against the room: logs/liza.log has adults
# near the device saying "यहाँ देखो, इन 3 डोट को क्लिक कर देखो" to each other,
# about the screen. So a bare "देखो" / "look" opens nothing -- it is the most
# common filler in spoken Hindi -- and there must be a THIS, a book, a hand or a
# camera in it as well.
_EN_STRONG = (
    r"\b(?:look|looking)\s+at\s+(?:this|that|these|my|it\b|what\s+i)",
    # "what" only when it is not "what I mean" -- that is a figure of speech.
    r"\b(?:can|could|do)\s+you\s+see\s+(?:this|that|these|it\b|my|me\b|"
    r"what(?!\s+i\s+mean))",
    r"\bwhat\s+(?:can|do)\s+you\s+see\b",
    r"\bsee\s+(?:this|my)\s+(?:book|page|copy|notebook|question|answer|drawing|homework)",
    r"\bread\s+(?:this|these|that|my|the\s+(?:page|question|line|paragraph|text)\b|"
    r"what(?:'s|\s+is)\s+(?:written|here))",
    r"\bwhat\s+(?:does|do)\s+(?:this|it|that|these)\s+(?:say|says|read)\b",
    r"\bwhat(?:'s|\s+is)\s+written\b",
    r"\b(?:solve|check|explain)\s+(?:this|these|that)\s+(?:question|problem|sum|"
    r"one|equation|exercise|page|answer)",
    r"\bcheck\s+my\s+(?:answer|answers|work|homework|copy|notebook|drawing|writing)",
    # A homework question is on a page in front of them. Told only that the
    # camera was available, she asked them to read it out instead.
    r"\b(?:my|this|the)\s+homework\s+(?:question|problem|sum|exercise)s?\b",
    r"\bin\s+my\s+(?:hand|hands)\b",
    r"\bwhat\s+am\s+i\s+(?:holding|showing)",
    r"\b(?:i(?:'m|\s+am)\s+showing|let\s+me\s+show|can\s+i\s+show|i(?:'ll|\s+will)\s+show)"
    r"\s+you\b",
    r"\b(?:take|click)\s+a\s+(?:photo|picture|pic|snap)\b",
    r"\b(?:use|through|with|from|on)\s+(?:your|the)\s+camera\b",
    r"\bin\s+front\s+of\s+(?:your|the)\s+camera\b",
    r"\bidentify\s+(?:this|that|it\b)",
    r"\b(?:this|that)\s+(?:page|question)\s+(?:in|of|from)\s+my\b",
    # "example 5 from my book", "question 3 in my notebook": a numbered thing
    # on a page they have open in front of them.
    r"\b(?:question|example|exercise|sum|problem)\s*(?:no\.?\s*|number\s*)?\d+\S*"
    r".{0,40}?\b(?:in|from|of|on)\s+(?:my|this)\s+(?:book|textbook|notebook|copy|"
    r"page|worksheet|homework)",
)
_HI_STRONG = (
    # "ये देखो", "इसे देखो", "इसको पढ़ो" -- a THIS with see or read
    r"(?:^|\s)(?:ये|यह|इसे|इसको|इन्हें|इनको|इसमें|इस\s+(?:पेज|page|सवाल|प्रश्न|"
    r"question|किताब|कॉपी|चीज़|चीज))\s+(?:को\s+)?(?:ज़रा\s+|जरा\s+)?"
    r"(?:देखो|देखिए|देखना|पढ़ो|पढ़िए|पढ़ना|पढ़\s+के|पढ़कर|पहचानो|हल\s+करो|solve)",
    r"(?:देखो|पढ़ो|पढ़िए|देखिए)\s+(?:ये|यह|इसे|इसको)(?:\s|$|[?।,!])",
    # "मेरी किताब / कॉपी / होमवर्क देखो"
    r"मेरी\s+(?:किताब|कॉपी|बुक|book|नोटबुक|ड्रॉइंग)\s+(?:में\s+)?(?:\S+\s+)?"
    r"(?:देखो|देखिए|पढ़ो|पढ़िए|चेक)",
    r"मेरा\s+(?:होमवर्क|homework|जवाब|answer)\s+(?:चेक|check|देखो)",
    r"(?:होमवर्क|homework)\s+(?:का|के|की|में)\s+(?:\S+\s+)?(?:सवाल|प्रश्न|question)",
    # "क्या लिखा है", "क्या दिख रहा है", "मेरे हाथ में क्या है"
    r"क्या\s+लिखा\s+(?:है|हुआ)",
    r"(?:तुम्हें|तुमको|आपको|तुझे)\s+(?:क्या\s+)?(?:दिख|दिखाई)",
    r"क्या\s+(?:दिख|दिखाई\s+दे)\s+रहा",
    r"हाथ\s+में\s+क्या",
    # "क्या तुम ये देख सकती हो?" -- can you see. A question with क्या, or with
    # what is to be seen right before देख; never a bare "आप देख सकते हैं",
    # which is how somebody showing the device to a visitor talks.
    r"क्या\s+(?:तुम|आप)\s+(?:\S+\s+){0,2}?देख\s+(?:सकती|सकते|पा\s+रही|पा\s+रहे)",
    r"(?:तुम|आप)\s+(?:मुझे|ये|यह|इसे|इसको)\s+देख\s+(?:सकती|सकते|पा\s+रही|पा\s+रहे)",
    r"(?:अपना|अपने)\s+कैमरा\s+से",
    r"कैमरे?\s+(?:से|में|के\s+सामने)",
    r"(?:तुम्हें|तुमको|आपको)\s+(?:कुछ\s+|ये\s+|यह\s+)?दिखा(?:ता|ती|ऊँ|ऊं|ना|ती\s+हूँ)",
    r"(?:फ़ोटो|फोटो|photo)\s+(?:लो|खींचो|ले\s+लो)",
    # "मेरी किताब का सवाल नंबर तीन", "इस पेज का question 4"
    r"(?:मेरी|इस)\s+(?:किताब|कॉपी|बुक|book|पेज|page|नोटबुक)\s+(?:का|की|के|में)\s+"
    r"(?:\S+\s+){0,3}?(?:सवाल|प्रश्न|question|example|उदाहरण|exercise)",
)
_HINGLISH_STRONG = (
    r"\b(?:ye|yeh|ise|isko|isse|isme|is\s+page|is\s+question)\s+(?:ko\s+)?"
    r"(?:dekho|dekhiye|padho|padhiye|pehchano|solve\s+karo)\b",
    r"\bkya\s+likha\s+(?:hai|hua)\b",
    r"\bkya\s+dikh\s+raha\b",
    r"\bkya\s+(?:tum|aap)\s+(?:\S+\s+){0,2}?dekh\s+(?:sakti|sakte|pa\s+rahi)\b",
    r"\bhaath\s+(?:me|mein)\s+kya\b",
    r"\bcamera\s+(?:se|me|mein|ke\s+samne|ke\s+saamne)\b",
    r"\bmeri\s+(?:kitab|book|copy|notebook)\s+(?:\S+\s+)?(?:dekho|padho|check)\b",
)
# WEAK: "what is this?" -- a look if nothing of hers is on the board, and a
# question ABOUT the board if something is. Only the short, whole-utterance
# form: "what is this formula" is about the formula, and goes to the model.
_WEAK = (
    r"^(?:(?:hey\s+)?liza[,\s]*)?(?:(?:and|so|okay|ok|now|then|um|uh|hmm|liza)[,\s]+)*"
    r"(?:(?:tell\s+me|can\s+you\s+tell\s+me|do\s+you\s+know)\s+)?"
    r"what(?:'s|\s+is|\s+are)\s+(?:this|that|these|those|it)"
    r"(?:\s+(?:thing|one|object|called|here|i\s+have|i'm\s+holding))?"
    r"(?:[,\s]+liza)?\s*[?.!]*\s*$",
    r"^(?:(?:हे\s+)?लिज़ा[,\s]*|लीज़ा[,\s]*)?(?:अच्छा|तो|और|ओके|okay)?[,\s]*"
    r"(?:ये|यह|ये\s+वाला|यह\s+वाला|ये\s+चीज़|यह\s+चीज़|ये\s+सब)\s+क्या\s+(?:है|हैं|होता\s+है)"
    r"(?:[,\s]+(?:लिज़ा|लीज़ा))?\s*[?।.!]*\s*$",
    r"^(?:ye|yeh)\s+(?:wala\s+)?kya\s+(?:hai|he)\s*[?.!]*\s*$",
)
RE_LOOK_STRONG = re.compile("|".join(_EN_STRONG + _HI_STRONG + _HINGLISH_STRONG),
                            re.IGNORECASE)
RE_LOOK_WEAK = re.compile("|".join(_WEAK), re.IGNORECASE)
# What the camera is never for, even when the words fit: their screen, and
# what is drawn on it. "Look at this graph" with a graph up is about the graph.
RE_ABOUT_THE_SCREEN = re.compile(
    r"\b(?:board|screen|graph|model|diagram|picture\s+you|image\s+you|3d|"
    r"equation\s+you|transcribe)\b|बोर्ड|स्क्रीन|ग्राफ|मॉडल|डायग्राम", re.IGNORECASE)


# The camera itself: on and off. Checked before anything else looks at the
# sentence, and answered without the model. "कैमरा ओन करो" is how Whisper
# spelled "ऑन" in logs/liza.log, 17:16:54, and बन्द is its other बंद.
RE_CAMERA_ON = re.compile("|".join((
    r"\b(?:turn|switch|put)\s+on\s+(?:the\s+|your\s+)?camera\b",
    r"\b(?:turn|switch)\s+(?:the\s+|your\s+)?camera\s+on\b",
    r"\b(?:open|start)\s+(?:the\s+|your\s+)?camera\b",
    r"\bcamera\s+(?:on|chalu|kholo|khol\s+do|start)\b",
    r"कैमरा\s+(?:खोलो|खोल\s+दो|चालू|ऑन|ओन|on|शुरू|स्टार्ट|start)(?:\s+(?:करो|कर\s+दो|कीजिए))?",
    r"(?:खोलो|चालू\s+करो|ऑन\s+करो|ओन\s+करो)\s+(?:अपना\s+|अपने\s+)?कैमरा",
)), re.IGNORECASE)
RE_CAMERA_OFF = re.compile("|".join((
    r"\b(?:turn|switch|shut)\s+off\s+(?:the\s+|your\s+)?camera\b",
    r"\b(?:turn|switch|shut)\s+(?:the\s+|your\s+)?camera\s+off\b",
    r"\b(?:close|stop|hide)\s+(?:the\s+|your\s+)?camera\b",
    r"\bcamera\s+(?:off|band|hatao)\b",
    r"कैमरा\s+(?:बंद|बन्द|ऑफ|ओफ|off|हटाओ)",
    r"(?:बंद|बन्द|ऑफ)\s+करो\s+(?:अपना\s+|अपने\s+)?कैमरा",
)), re.IGNORECASE)
# "Wipe out the board" -- said to her again and again in logs/liza.log -- is
# the camera off while the camera is what is on the board.
RE_CLEAR_BOARD = re.compile(
    r"\b(?:wipe|clear|clean|erase)\s+(?:out\s+|off\s+|up\s+)?(?:the\s+|this\s+|that\s+)?"
    r"(?:board|screen)\b|(?:बोर्ड|स्क्रीन)\s+(?:साफ|साफ़|क्लियर|clear)", re.IGNORECASE)
# What may sit round "camera on" without making it a question as well.
_COMMAND_FILLER = {
    "hey", "liza", "lisa", "please", "can", "could", "would", "you", "and",
    "now", "just", "ok", "okay", "so", "the", "your", "my", "for", "me",
    "करो", "कर", "दो", "दीजिए", "कीजिए", "ज़रा", "जरा", "प्लीज़", "प्लीज", "अपना",
    "अपने", "लिज़ा", "लीज़ा", "लिजा", "और", "अब", "ओके", "हे", "तो", "please",
}


def camera_command(text):
    """"off", "on", or "look" when the sentence is about the camera itself --
    "look" being "turn on the camera and tell me what this is", which turns
    it on AND asks. None for anything else."""
    text = (text or "").strip()
    if not text or len(text) > 220:
        return None
    if RE_CAMERA_OFF.search(text):
        return "off"
    if live() and RE_CLEAR_BOARD.search(text):
        return "off"
    match = RE_CAMERA_ON.search(text)
    if not match:
        return None
    rest = (text[:match.start()] + " " + text[match.end():]).split()
    words = [w for w in (w.strip("?,.!।\"'").lower() for w in rest)
             if w and w not in _COMMAND_FILLER]
    return "look" if len(words) >= 2 else "on"


def wants_a_look(text, board):
    """Whether this turn should be answered by looking: True or False.

    `board` is state.current_visual. The camera's own view on it does not
    count as the board being busy, and neither does a photo: "what is this?"
    then means whatever they are holding up now."""
    text = (text or "").strip()
    if not text or len(text) > 220:
        return False
    if RE_ABOUT_THE_SCREEN.search(text):
        return False
    drawn = board is not None and board.get("kind") not in ("photo", "camera")
    if RE_LOOK_STRONG.search(text):
        return True
    return bool(RE_LOOK_WEAK.search(text)) and not drawn
