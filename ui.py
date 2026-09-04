"""Everything that draws on the screen.

Lifted out of the assistant so the look of the device can be changed without
reading the assistant, and the assistant without reading the drawing code. The
seam is a real one rather than a tidy-up: measured with the AST, only four
names crossed back the other way, and exactly one piece of shared mutable state
(pending_mode_intro).

HOW THIS TALKS TO THE ASSISTANT
    `import assistant` binds the MODULE, and every call through it -- kg.kg_say,
    profiles.active_profile -- happens when a button is tapped, never while this
    file is being imported. That is what makes the cycle safe: assistant.py imports
    this file at its top, and by the time any of these run, both halves are
    fully built. Do not change any of those to `from assistant import ...`; that
    form is resolved at import time and would need assist to be finished before
    this file had started.

    Anything genuinely needed AT import time lives in config.py instead, which
    is why the KG patience numbers moved there.
"""

import glob
import math
import os
import queue
import random
import re
import threading
import time
import tkinter as tk

from datetime import datetime
from PIL import Image, ImageDraw, ImageFilter, ImageTk

# In-place objects by name -- a local called `state` cannot shadow these, and
# this file has four of them (set_state's own parameter among them). The one
# value that is REASSIGNED has to go through the module; see state.py.
import state as app_state
from state import (audio_queue, kg_ask_cancel, kg_ask_event,
                   kg_listen_results, media_active,
                   playback_active, sleep_event, stop_playback_event,
                   wake_event)
import kg_content
import media
import profiles
import store
from config import (KG_COUNT_END_SILENCE_S, KG_COUNT_PHRASE_LIMIT_S,
                    KG_COUNT_START_TIMEOUT_S, KG_SPELL_END_SILENCE_S,
                    KG_SPELL_PHRASE_LIMIT_S, KG_SPELL_START_TIMEOUT_S)

UI_W, UI_H = 800, 480
FRAME_MS = 60

COL_BG        = "#F3F5FD"
COL_FRAME     = "#E1E5F4"
COL_CARD      = "#FFFFFF"
COL_CARD_EDGE = "#EAEDF8"
COL_SHADOW    = "#B4BCDD"
COL_TEXT      = "#1E2233"
COL_TEXT_DIM  = "#8891A8"
COL_TEXT_FAINT= "#B6BDD0"
COL_TRACK     = "#E6E9F5"
COL_INDIGO    = "#6366F1"
COL_STOP      = "#F43F5E"

MODE_ACCENTS = {"TUTOR": "#7C3AED", "CO-TELL": "#14B8A6", "RE-TELL": "#F59E0B"}
MODE_TINTS   = {"TUTOR": "#F3EEFF", "CO-TELL": "#E6FAF6", "RE-TELL": "#FFF4E6"}
# Kept short on purpose: the mode rows are a narrow column now, and a blurb
# that wraps to four lines in a 78px row is not read, it is just texture.
MODE_BLURBS = {
    "TUTOR": "Concepts and solutions",
    "CO-TELL": "Talk it through together",
    "RE-TELL": "You explain, I correct"
}
MODE_INTROS = {
    "TUTOR": "You are in tutor mode. Ask me anything from your studies.",
    "CO-TELL": "You are in co-tell mode. Name a topic and we will work through it together.",
    "RE-TELL": "You are in re-tell mode. Teach me what you have learned. "
               "I will listen without interrupting, and when you finish I will tell you how you did."
}

# The three pill buttons along the bottom: label, sub-label, gradient, icon.
ACTIONS = [
    ("SPEAK", "Tap to speak",      "#5AA7FF", "#3D6FE8", "#E7F0FF", "mic"),
    ("STOP",  "Tap to stop listen", "#FF5E7D", "#F0355F", "#FFE3EA", "stop"),
    ("SLEEP", "Put me to sleep",   "#9061F9", "#6D3FE0", "#EDE4FF", "moon"),
]

# label, colour, waveform activity (0..1)
STATE_STYLE = {
    "warmup":    ("Waking up",         "#7C6AE0", 0.10),
    "idle":      ("Tap to speak",      "#4C6FFF", 0.08),
    "sleeping":  ("Sleeping",          "#8B5CF6", 0.03),
    "listening": ("Listening",         "#14B8A6", 1.00),
    "thinking":  ("Thinking",          "#F59E0B", 0.35),
    "speaking":  ("Speaking",          "#EC4899", 0.80),
    "capturing": ("Looking",           "#22C55E", 0.25),
    "error":     ("Something's wrong", "#F43F5E", 0.12),
}

# How Liza is feeling, as she reports it herself on the EMOTION: line. The chip
# under her is the only place this shows on screen, so an unknown word from the
# model falls back to neutral rather than blanking the chip or crashing.
MASCOT_FOR_STATE = {
    "warmup": "idle", "idle": "idle", "capturing": "idle", "error": "idle",
    "sleeping": "idle",
    "listening": "listening", "thinking": "thinking", "speaking": "speaking",
}

# Nothing on screen is Hindi today, but prefer a family that covers Devanagari so
# any Hindi text added later is readable. Install one with:
#   sudo apt install fonts-noto-devanagari
FONT_PREFERENCE = ("Noto Sans", "Noto Sans Devanagari", "Lohit Devanagari",
                   "Mukta", "Samyak Devanagari", "FreeSans", "DejaVu Sans", "Helvetica")

# ---------- layout ----------
# Three bands: a header of small cards, a working middle, and the three big
# actions along the bottom.
#
# Everything that is only REFERENCE -- the time, the weather, who is using the
# device, what is playing -- is pushed up into the header and given one shared
# height, so the middle band belongs entirely to the three things the student
# actually looks at: the modes they can choose, the character, and the board her
# words appear on. The board is the widest single element on the screen because
# it is the one that has to be read.
# ONE margin and ONE gap, used everywhere. Every edge below is derived from
# these two numbers rather than nudged by eye, which is what stops the spacing
# drifting a pixel or two per card until nothing lines up with anything.
PAD = 8

# These are measured FROM UI/Home/Home.png, which is a 2x render of the screen
# this artwork was drawn for: each asset was matched against it to find where it
# belongs, rather than guessed. The three header cards are one size (204x62) and
# evenly spaced, which is why they no longer reach the edges.
TOP_Y0, TOP_Y1 = 17, 79
CLOCK_X0, CLOCK_X1 = 99, 303
WHO_X0, WHO_X1 = 322, 526
MUSIC_X0, MUSIC_X1 = 541, 745

# The left column: the three modes, one under another. Narrower and shorter
# than the board on purpose -- it is a menu that is glanced at, not something
# that gets read, so it should not take the same room as the thing that does.
MODES_X0, MODES_X1 = 14, 173
MODES_Y0, MODES_Y1 = 124, 363
MODE_CARD_X0, MODE_CARD_X1 = 21, 166
MODE_CARD_Y0, MODE_CARD_H, MODE_CARD_GAP = 159, 62, 4

# The board on the right, which is where the conversation is read.
BOARD_X0, BOARD_X1 = 454, 783
BOARD_Y0, BOARD_Y1 = 109, 375

# She stands in the gap between the two, on the grass.
#
# Measured against the wallpaper, not chosen by eye: the sky/grass boundary
# under her sits at y=382 (found by scanning down the column at MASCOT_CX for
# the first sustained run of green -- clouds, stars and the rainbow all produce
# short green stretches higher up and will fool a simpler test). The cached
# frames have zero transparent padding below the feet, so the bottom edge of the
# image IS the feet, and centring a MASCOT_H frame at MASCOT_CY lands them two
# pixels into the grass, which reads as standing on it rather than as a seam.
# Between the mode panel and the board, nudged left of the exact midpoint to
# sit where she stands in the mockup.
MASCOT_CX, MASCOT_CY = 292, 382 - 250 // 2
# The state caption, in the clear strip between the header cards and her ears.
STATE_LABEL_Y = 106

# Three lights that pulse with how busy she is. Positions are stored per dot
# rather than computed in a row, so they can be scattered around her the way the
# sparkles in the artwork are.
HEAD_DOT_SPOTS = ((MASCOT_CX - 100, 150), (MASCOT_CX + 100, 168),
                  (MASCOT_CX - 96, 300), (MASCOT_CX + 96, 286))

# The action bar is a centred group rather than a full-width row. At 254 wide
# each button was mostly empty to the right of its own words, and three of them
# edge to edge read as a toolbar rather than as three things to press.
BTN_Y0, BTN_H, BTN_W, BTN_GAP = 397, 69, 154, 5
BTN_XS = [169, 328, 489]

# ---------- 3D mascot animation ----------
# The source clips are 1920x1080 RGBA at ~200 frames each; decoding all four
# in full would need several GB of RAM. On first run each one is cropped to
# the character, downsized, and cached to disk as small PNG frames, and only
# that small cache is ever loaded into memory.
MASCOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Animations")
MASCOT_CACHE_DIR = os.path.join(MASCOT_DIR, "cache")
MASCOT_SOURCES = {"idle": "Idle.gif", "listening": "Listen.gif",
                   "thinking": "Think.gif", "speaking": "Talking.gif"}
# Union bounding box of the character across all 4 clips, measured from the
# source frames rather than guessed: the artwork sits in a 1600x960 canvas and
# is nowhere near filling it, so cropping to the character is what stops her
# being rendered as a thumbnail with 60% empty space around her. Re-measure if
# the clips are ever replaced -- the old value here was for 1920x1080 sources
# and is meaningless against these.
MASCOT_CROP = (534, 40, 1145, 855)
MASCOT_H = 250
MASCOT_W = round(MASCOT_H * (MASCOT_CROP[2] - MASCOT_CROP[0]) / (MASCOT_CROP[3] - MASCOT_CROP[1]))
MASCOT_STEP = 2  # keep every 2nd frame: still smooth, halves memory and disk

# 3D-only mode: the same frames, upscaled to most of the 480px panel. Built
# lazily, one animation at a time, and only if the student ever asks for the
# mode -- all four buckets at this size is ~220MB of PhotoImage, which is not
# worth holding for a mode that may never be used.
MASCOT_3D_H = 440
MASCOT_3D_W = round(MASCOT_3D_H * (MASCOT_CROP[2] - MASCOT_CROP[0]) / (MASCOT_CROP[3] - MASCOT_CROP[1]))
MASCOT_3D_CY = 228

# The artwork for the screen. Every one of these is optional: ui_asset returns
# None when a file is missing and each builder falls back to what it drew
# before, so half a UI folder is a half-restyled screen rather than a dead one.
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "UI")
UI_BG_IMAGE = os.path.join(UI_DIR, "Home", "Background.png")
if not os.path.exists(UI_BG_IMAGE):
    UI_BG_IMAGE = os.path.join(MASCOT_DIR, "AI Background.png")

_ui_assets = {}

def ui_asset(*parts, size=None):
    """A PNG from the UI folder, as RGBA, or None if it is not there.

    `size` gives a square version, for the avatars, which are drawn at 110 and
    used smaller. Cached by size as well: these are placed on every redraw of a
    screen, and decoding and resampling a PNG each time is not free on a Pi.
    None rather than an exception because a missing file must cost a nicer
    button, not the screen."""
    key = (parts, size)
    if key not in _ui_assets:
        path = os.path.join(UI_DIR, *parts)
        try:
            image = Image.open(path).convert("RGBA")
            if size:
                image = image.resize((size, size), Image.LANCZOS)
            _ui_assets[key] = image
        except Exception:
            print(f"[UI] No artwork at {os.path.join(*parts)}; drawing it instead.",
                  flush=True)
            _ui_assets[key] = None
    return _ui_assets[key]


# How many Profile_NN.png there are to choose from.
PROFILE_AVATARS = 12

def profile_avatar(profile, size=64):
    """The animal for a child, or None if the artwork is missing.

    Derived from the user_id rather than stored, so it needs no change to the
    profile format and no screen asking a four-year-old to pick a picture --
    and it is STABLE: the same child gets the same animal on every screen, for
    as long as that profile exists, which is the only property that matters
    when the picture is how a pre-reader finds their own name.
    """
    ident = (profile or {}).get("user_id") or (profile or {}).get("name") or ""
    index = (sum(ord(c) for c in str(ident)) % PROFILE_AVATARS) + 1
    return ui_asset("Profile", f"Profile_{index:02d}.png", size=size)

def _background_image(w, h):
    """The wallpaper, scaled to COVER w*h and centre-cropped.

    Cover rather than fit: the art is 16:9 and this panel is 5:3, so stretching
    it to the exact shape squashes everything in it by about 7%, which is very
    visible on the round things (clouds, flowers, the rainbow). Cropping a
    little off the sides costs nothing because the subject is central."""
    image = Image.open(UI_BG_IMAGE).convert("RGB")
    scale = max(w / image.width, h / image.height)
    image = image.resize((max(w, round(image.width * scale)),
                          max(h, round(image.height * scale))), Image.LANCZOS)
    left, top = (image.width - w) // 2, (image.height - h) // 2
    return image.crop((left, top, left + w, top + h))

def _build_mascot_cache(state, fname):
    out_dir = os.path.join(MASCOT_CACHE_DIR, state)
    os.makedirs(out_dir, exist_ok=True)
    src = Image.open(os.path.join(MASCOT_DIR, fname))
    kept = 0
    for i in range(0, src.n_frames, MASCOT_STEP):
        src.seek(i)
        src.convert("RGBA").crop(MASCOT_CROP).resize((MASCOT_W, MASCOT_H), Image.LANCZOS) \
           .save(os.path.join(out_dir, f"f{kept:04d}.png"))
        kept += 1
    print(f"[MASCOT] Cached {kept} frames for '{state}'.", flush=True)

def _load_mascot_frames():
    """{state: [ImageTk.PhotoImage, ...]}, building the on-disk cache first if needed."""
    frames = {}
    for state, fname in MASCOT_SOURCES.items():
        out_dir = os.path.join(MASCOT_CACHE_DIR, state)
        if not (os.path.isdir(out_dir) and os.listdir(out_dir)):
            src_path = os.path.join(MASCOT_DIR, fname)
            if not os.path.exists(src_path):
                print(f"[MASCOT WARN] {fname} not found; '{state}' animation will be blank.", flush=True)
                frames[state] = []
                continue
            print(f"[MASCOT] Preparing '{state}' animation (first run only)...", flush=True)
            _build_mascot_cache(state, fname)
        paths = sorted(glob.glob(os.path.join(out_dir, "*.png")))
        frames[state] = [ImageTk.PhotoImage(_fit_mascot_frame(Image.open(p)))
                         for p in paths]
    return frames


def _fit_mascot_frame(image):
    """Bring a cached frame to MASCOT_H, keeping its shape.

    The cache is built once from the source clips, which is minutes of work and
    gigabytes of memory, and it is keyed only by state -- so without this a
    change to MASCOT_H for the sake of the layout would either be ignored or
    force that whole rebuild. Resizing the small cached PNG instead costs a few
    hundred milliseconds at startup and makes the height a free choice.
    """
    if image.height == MASCOT_H:
        return image
    width = max(1, round(image.width * MASCOT_H / image.height))
    return image.resize((width, MASCOT_H), Image.LANCZOS)

# ---------- PIL-drawn chrome ----------
# Tk's canvas has neither alpha nor blur, and in this design the cards and
# buttons are defined by their soft shadow and gradient more than by any
# outline, so those two pieces are rendered in PIL and placed as images.
SHADOW_PAD = 10

# ---------------------------------------------------------------------------
# Emoji pictures for the KG screens
# ---------------------------------------------------------------------------
# "A for Apple" needs an apple, and a child who cannot read needs it more than
# the word. These come from Noto Color Emoji rendered to a bitmap by Pillow
# rather than from a folder of downloaded pictures: nothing to license, nothing
# to ship, nothing to fetch, and it keeps working with the network unplugged.
#
# Tk cannot draw colour emoji from a font itself -- it would show boxes, which
# is why the rest of the UI has none -- so the glyph is rasterised here and put
# on the canvas as an image instead.
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
# Noto Color Emoji is a CBDT bitmap font with ONE strike, at 109px. Asking
# FreeType for any other size raises "invalid pixel size", so every glyph is
# rendered at 109 and resized down afterwards.
EMOJI_STRIKE = 109
_emoji_cache = {}


# Pictures that are not emoji. Some words a child is actually taught have no
# emoji at all -- Yak, अनार, इमली, ईख, ओखली, लट्टू, षट्कोण -- and the nearest
# emoji is the wrong animal or the wrong object, which on a chart a child is
# learning from is worse than no picture. Dropping a PNG in here fills the gap:
# name the entry "yak.png" instead of an emoji character and it is used instead.
PICTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pictures")
_PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def picture_image(spec, px):
    """A PIL image for a picture spec at `px`: a file in pictures/, or an emoji.

    None when there is nothing to draw, which every caller already handles by
    showing the letter on its own -- so a missing file degrades to exactly the
    same screen as no picture at all, rather than to a broken one.
    """
    if not spec:
        return None
    if spec.lower().endswith(_PICTURE_SUFFIXES):
        return _file_picture(spec, px)
    return emoji_image(spec, px)


def _file_picture(name, px):
    key = ("file:" + name, px)
    if key in _emoji_cache:
        return _emoji_cache[key]
    path = os.path.join(PICTURE_DIR, os.path.basename(name))
    image = None
    try:
        if os.path.exists(path):
            source = Image.open(path).convert("RGBA")
            # Fitted into a square and centred rather than stretched, so a
            # non-square drawing keeps its proportions next to the letter.
            source.thumbnail((px, px), Image.LANCZOS)
            image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
            image.paste(source, ((px - source.width) // 2,
                                 (px - source.height) // 2), source)
    except Exception as exc:
        print(f"[PICTURE] Could not load {path} ({exc}).", flush=True)
        image = None
    _emoji_cache[key] = image
    return image


def emoji_image(char, px):
    """A PIL image of one emoji at `px`, or None if it cannot be drawn."""
    if not char:
        return None
    key = (char, px)
    if key in _emoji_cache:
        return _emoji_cache[key]
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(EMOJI_FONT, EMOJI_STRIKE)
        canvas = Image.new("RGBA", (EMOJI_STRIKE + 40, EMOJI_STRIKE + 40), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((20, 20), char, font=font, embedded_color=True)
        box = canvas.getbbox()
        if box is None:                      # font has no glyph for it
            _emoji_cache[key] = None
            return None
        image = canvas.crop(box).resize((px, px), Image.LANCZOS)
    except Exception as exc:
        print(f"[EMOJI] Could not render {char!r} ({exc}).", flush=True)
        image = None
    _emoji_cache[key] = image
    return image


def _rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))

def _mix(colour, target, t):
    """Blend two #rrggbb colours; Tk canvas has no alpha so glows are faked this way."""
    t = max(0.0, min(1.0, t))
    a, b = _rgb(colour), _rgb(target)
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _drop_shadow(size, box, radius, alpha, blur, offset):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (box[0], box[1] + offset, box[2], box[3] + offset), radius,
        fill=_rgb(COL_SHADOW) + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(blur))

def _card_image(w, h, radius, fill=COL_CARD, edge=COL_CARD_EDGE, alpha=60, blur=5, offset=3):
    """A rounded card with a soft drop shadow. Anchor the result at NW,
    offset by -SHADOW_PAD, so the card itself lands on its nominal box."""
    p = SHADOW_PAD
    size = (w + p * 2, h + p * 2)
    box = (p, p, p + w, p + h)
    img = _drop_shadow(size, box, radius, alpha, blur, offset)
    ImageDraw.Draw(img).rounded_rectangle(
        box, radius, fill=_rgb(fill) + (255,),
        outline=(_rgb(edge) + (255,)) if edge else None, width=1)
    return img

def _gradient(w, h, c0, c1):
    """Left-to-right gradient, built one pixel row wide and stretched."""
    a, b = _rgb(c0), _rgb(c1)
    row = Image.new("RGB", (w, 1))
    px = row.load()
    for x in range(w):
        t = x / max(1, w - 1)
        px[x, 0] = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return row.resize((w, h), Image.BILINEAR)

def _draw_action_icon(draw, kind, cx, cy):
    white = (255, 255, 255, 255)
    if kind == "mic":
        draw.rounded_rectangle((cx - 4, cy - 10, cx + 4, cy + 1), 4, fill=white)
        draw.arc((cx - 8, cy - 6, cx + 8, cy + 6), 0, 180, fill=white, width=2)
        draw.line((cx, cy + 6, cx, cy + 10), fill=white, width=2)
        draw.line((cx - 4, cy + 10, cx + 4, cy + 10), fill=white, width=2)
    elif kind == "stop":
        draw.rounded_rectangle((cx - 6, cy - 6, cx + 6, cy + 6), 2, fill=white)

def _action_image(w, h, radius, c0, c1, icon):
    """One of the bottom pill buttons: gradient body, soft shadow, ringed icon."""
    p = SHADOW_PAD
    size = (w + p * 2, h + p * 2)
    box = (p, p, p + w, p + h)
    img = _drop_shadow(size, box, radius, 90, 6, 4)

    body = Image.new("RGBA", size, (0, 0, 0, 0))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
    body.paste(_gradient(w, h, c0, c1).convert("RGBA"), (p, p), mask.crop((p, p, p + w, p + h)))
    img = Image.alpha_composite(img, body)

    cx, cy = p + 40, p + h // 2
    draw = ImageDraw.Draw(img)
    draw.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), outline=(255, 255, 255, 150), width=2)
    if icon == "moon":
        # Carved rather than drawn: a crescent is a circle minus an offset
        # circle, and ImageDraw replaces alpha instead of compositing it, so
        # the bite has to be taken out on a scratch layer of its own.
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        cut = ImageDraw.Draw(layer)
        cut.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(255, 255, 255, 255))
        cut.ellipse((cx - 4, cy - 12, cx + 14, cy + 6), fill=(0, 0, 0, 0))
        img = Image.alpha_composite(img, layer)
    else:
        _draw_action_icon(draw, icon, cx, cy)
    return img

def _album_art_image(size, radius=10):
    """Placeholder cover art: nothing upstream gives us a real thumbnail."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    img.paste(_gradient(size, size, "#A78BFA", "#F0A6D0").convert("RGBA"), (0, 0), mask)

    draw = ImageDraw.Draw(img)
    cx, cy, white = size * 0.42, size * 0.62, (255, 255, 255, 235)
    draw.ellipse((cx - 7, cy - 5, cx + 3, cy + 5), fill=white)
    draw.line((cx + 3, cy, cx + 3, cy - 16), fill=white, width=2)
    draw.line((cx + 3, cy - 16, cx + 13, cy - 13), fill=white, width=3)
    return img

def _fmt_clock(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"

class TutorUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Liza")
        self.root.geometry(f"{UI_W}x{UI_H}")
        self.root.attributes('-fullscreen', True)
        self.root.configure(bg=COL_BG)

        self.modes = ["TUTOR", "CO-TELL", "RE-TELL"]
        self.mode_colors = MODE_ACCENTS
        self.current_mode_index = 0
        self.current_mode = self.modes[0]
        self.current_state = "warmup"
        self.transcript = ""
        self.speaker = "user"
        self.asleep = False

        self.phase = 0.0
        self.frame = 0
        self.mascot_index = 0
        self._mascot_bucket = None
        self._state_caption = None
        self._photos = []       # Tk keeps no reference of its own to images
        self._font_cache = {}
        self.media = {"title": None, "artist": "", "pos": 0.0, "dur": 0.0, "paused": False}
        # Agentic UI state: "normal" (every card) or "3d" (mascot and mood only).
        self.ui_mode = "normal"
        self._hidden_items = []     # exactly what 3D mode hid, to put back
        self.emotion = None
        self.mascot_frames_big = {} # bucket -> upscaled frames, built on demand
        self._big_pending = set()

        # Profile / Kindergarten screens. `overlay` is the name of the modal
        # screen currently covering the canvas, or None for the normal UI, and
        # several handlers key off it -- see tap_to_wake().
        self.overlay = None
        self._overlay_seq = 0
        self._overlay_photos = []
        self._setup_name = ""
        self._setup_class = None
        self._setup_board = None
        # Words and stories already used, so a child is not handed "cat" four
        # times in a row. Cleared once the bank is exhausted.
        self._kg_seen_words = set()
        self._kg_seen_stories = set()
        self._kg_word = None
        self._kg_story = None
        self._kg_typed = ""
        self._kg_feedback = ""
        self._kg_story_asked = False
        # Set here as well as in show_kg_spelling, so _draw_kg_spelling is safe
        # to call from any entry point rather than only after a word is chosen.
        self._kg_stage = "write"
        self._kg_heard = ""
        self._kg_listening = False

        self.font_family = self._pick_font()
        self.canvas = tk.Canvas(root, width=UI_W, height=UI_H, bd=0,
                                highlightthickness=0, bg=COL_BG)
        self.canvas.place(x=0, y=0)

        print("[MASCOT] Loading character animations...", flush=True)
        self.mascot_frames = _load_mascot_frames()

        self._build_frame()
        self._build_clock_card()
        self._build_music_card()
        self._build_mascot()
        self._build_mode_cards()
        self._build_transcript_panel()
        self._build_buttons()
        self._build_profile_chip()
        self._refresh_cards()
        self.set_now_playing(None)
        self.refresh_profile_chip()

        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.bind("<Button-1>", self.tap_to_wake)

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
                return name
        return "Helvetica"

    def _font(self, size, bold=False):
        """Real Font objects rather than tuples, so widths can be measured."""
        key = (size, bold)
        if key not in self._font_cache:
            from tkinter import font as tkfont
            self._font_cache[key] = tkfont.Font(
                family=self.font_family, size=size,
                weight="bold" if bold else "normal")
        return self._font_cache[key]

    def _ellipsize(self, text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def _place_emoji(self, char, cx, cy, px):
        """Draw one picture centred at (cx, cy). Returns the item id, or None."""
        image = picture_image(char, px)
        if image is None:
            return None
        photo = ImageTk.PhotoImage(image)
        # Tk keeps no reference of its own; _clear_overlay empties this list, so
        # a long alphabet session cannot accumulate 42 dead bitmaps.
        self._overlay_photos.append(photo)
        return self.canvas.create_image(cx, cy, image=photo, anchor="center",
                                        tags=self.OVERLAY_TAG)

    def _place_overlay_asset(self, image, x, y, tag=None):
        """Artwork on a modal screen. Cleared with the rest of the overlay.

        _overlay_photos rather than _photos: _clear_overlay empties that list, so
        a child walking through forty letters cannot accumulate forty dead
        bitmaps."""
        photo = ImageTk.PhotoImage(image)
        self._overlay_photos.append(photo)
        tags = (self.OVERLAY_TAG, tag) if tag else (self.OVERLAY_TAG,)
        return self.canvas.create_image(x, y, image=photo, anchor="nw", tags=tags)

    def _place_photo(self, image, x, y, tags=None):
        """Photos are anchored NW and pulled back by the shadow padding, so
        callers can pass the card's own top-left corner."""
        photo = ImageTk.PhotoImage(image)
        self._photos.append(photo)
        kw = {"tags": tags} if tags else {}
        return self.canvas.create_image(x - SHADOW_PAD, y - SHADOW_PAD,
                                        image=photo, anchor="nw", **kw)

    def _place_asset(self, image, x, y, tags=None):
        """Place artwork with its top-left exactly at (x, y).

        Not _place_photo, which pulls back by SHADOW_PAD: the cards THAT places
        are PIL-drawn with their shadow baked into a margin. These have no such
        margin -- the coordinates were measured off the mockup and mean what
        they say."""
        photo = ImageTk.PhotoImage(image)
        self._photos.append(photo)
        kw = {"tags": tags} if tags else {}
        return self.canvas.create_image(x, y, image=photo, anchor="nw", **kw)

    def _card(self, x0, y0, x1, y1, r=16, fill=COL_CARD, edge=COL_CARD_EDGE, tags=None):
        return self._place_photo(
            _card_image(int(x1 - x0), int(y1 - y0), r, fill, edge), x0, y0, tags)

    def _bars_glyph(self, cx, cy, colour, heights=(5, 9, 13, 9, 5)):
        """The little waveform mark that heads the music and transcribe cards."""
        items = []
        x = cx - (len(heights) * 3 - 1) / 2
        for h in heights:
            items.append(self.canvas.create_line(x, cy - h / 2, x, cy + h / 2,
                                                 fill=colour, width=2, capstyle="round"))
            x += 3
        return items

    # ---------- static chrome ----------
    def _build_frame(self):
        # First item on the canvas, so every card, the mascot and all the text
        # stack above it. Tk has no z-index worth the name -- items are drawn in
        # creation order -- so "first" is the whole mechanism here.
        self.bg_photo = None
        self.bg_item = None
        try:
            self.bg_photo = ImageTk.PhotoImage(_background_image(UI_W, UI_H))
            self.bg_item = self.canvas.create_image(0, 0, anchor="nw",
                                                    image=self.bg_photo)
        except Exception as exc:
            print(f"[UI] Background image unavailable ({exc}); "
                  f"falling back to the flat colour.", flush=True)

        self._round_rect(4, 4, UI_W - 4, UI_H - 4, 18, fill="", outline=COL_FRAME, width=1)

        # Scattered around her rather than in a row above her head: the header
        # is full of cards now, and the artwork already has sparkles in it, so
        # these read as part of the picture while still pulsing with how busy
        # she is. Each keeps its own centre, since they are no longer a row.
        self.head_dots = []
        palette = ("#FBBF24", "#7C5CFF", "#22D3EE", "#FBBF24")
        for (x, y), colour in zip(HEAD_DOT_SPOTS, palette):
            self.head_dots.append((self.canvas.create_oval(
                x - 4, y - 4, x + 4, y + 4, fill=colour, outline=""), x, y))

        self._build_confetti()

    def _build_confetti(self):
        """Sparkles in the strip she stands in.

        Confined to the column between the mode card and the board: everything
        this used to draw across the middle of the panel is now underneath a
        card, so it was paying for items nobody could see.
        """
        for x, y in ((MODES_X1 + 16, 200), (BOARD_X0 - 18, 168),
                     (MODES_X1 + 24, 320), (BOARD_X0 - 14, 330)):
            self.canvas.create_line(x - 4, y, x + 4, y, fill="#FFFFFF", width=2)
            self.canvas.create_line(x, y - 4, x, y + 4, fill="#FFFFFF", width=2)

        for x, y, colour in ((MODES_X1 + 30, 128, "#FDE68A"),
                             (BOARD_X0 - 30, 244, "#FDE68A")):
            self.canvas.create_polygon(
                x, y - 7, x + 2, y - 2, x + 7, y, x + 2, y + 2,
                x, y + 7, x - 2, y + 2, x - 7, y, x - 2, y - 2,
                fill=colour, outline="", smooth=False)

    # ---------- clock + weather ----------
    def _build_clock_card(self):
        """Time on the left of the card, weather on the right, place underneath.

        One card rather than two stacked halves: at header height there is no
        room for two, and the two readings are glanced at together anyway.
        """
        art = ui_asset("Home", "Weather_bg.png")
        if art is not None:
            self._place_asset(art, CLOCK_X0, TOP_Y0)
        else:
            self._card(CLOCK_X0, TOP_Y0, CLOCK_X1, TOP_Y1, 16)
        cx, cy, r = CLOCK_X0 + 30, TOP_Y0 + 30, 19

        self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                fill="#EEF3FF", outline=COL_INDIGO, width=2)
        for i in range(12):
            a = math.pi * i / 6
            self.canvas.create_line(cx + (r - 5) * math.sin(a), cy - (r - 5) * math.cos(a),
                                    cx + (r - 3) * math.sin(a), cy - (r - 3) * math.cos(a),
                                    fill="#C3CBEA", width=1)
        self.hour_hand = self.canvas.create_line(cx, cy, cx, cy - 8,
                                                 fill=COL_TEXT, width=2, capstyle="round")
        self.minute_hand = self.canvas.create_line(cx, cy, cx, cy - 12,
                                                   fill=COL_INDIGO, width=2, capstyle="round")
        self.canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=COL_TEXT, outline="")
        self._clock_centre = (cx, cy)

        self.clock_id = self.canvas.create_text(
            CLOCK_X0 + 56, TOP_Y0 + 24, text="--:--", anchor="w",
            font=self._font(19, True), fill=COL_TEXT)
        self.meridiem_id = self.canvas.create_text(
            CLOCK_X0 + 56, TOP_Y0 + 29, text="", anchor="w",
            font=self._font(8, True), fill=COL_TEXT_DIM)
        self.date_id = self.canvas.create_text(
            CLOCK_X0 + 56, TOP_Y0 + 44, text="", anchor="w",
            font=self._font(7), fill=COL_TEXT_DIM)

        # The weather half, divided off rather than boxed: a second outline
        # inside a card this small reads as clutter.
        self.canvas.create_line(CLOCK_X0 + 136, TOP_Y0 + 12, CLOCK_X0 + 136, TOP_Y0 + 48,
                                fill=COL_CARD_EDGE)
        self.weather_glyph = []
        self.weather_glyph_at = (CLOCK_X0 + 158, TOP_Y0 + 26)
        self.temp_id = self.canvas.create_text(
            CLOCK_X0 + 178, TOP_Y0 + 22, text="--", anchor="w",
            font=self._font(15, True), fill=COL_TEXT)
        self.desc_id = self.canvas.create_text(
            CLOCK_X0 + 178, TOP_Y0 + 40, text="", anchor="w",
            font=self._font(7), fill=COL_INDIGO)

        # High and low go on the bottom row rather than beside the temperature:
        # at this width "34°C" and "35°" ran into each other.
        self._arrow(CLOCK_X1 - 60, TOP_Y1 - 14, up=True)
        self.high_id = self.canvas.create_text(
            CLOCK_X1 - 52, TOP_Y1 - 14, text="--", anchor="w",
            font=self._font(7, True), fill=COL_TEXT_DIM)
        self._arrow(CLOCK_X1 - 28, TOP_Y1 - 14, up=False)
        self.low_id = self.canvas.create_text(
            CLOCK_X1 - 20, TOP_Y1 - 14, text="--", anchor="w",
            font=self._font(7, True), fill=COL_TEXT_DIM)
        self.city_id = self.canvas.create_text(
            CLOCK_X0 + 14, TOP_Y1 - 14,
            text=assistant.WEATHER_CITY if assistant.WEATHER_API_KEY else "Weather unavailable",
            anchor="w", font=self._font(7), fill=COL_TEXT_FAINT)
        self._draw_weather_glyph("01d")

    def _arrow(self, x, y, up):
        tip = y - 6 if up else y + 6
        colour = "#EF4444" if up else "#3B82F6"
        self.canvas.create_line(x, y + (6 if up else -6), x, tip, fill=colour, width=2)
        self.canvas.create_polygon(x - 4, tip + (4 if up else -4), x + 4, tip + (4 if up else -4),
                                   x, tip, fill=colour, outline="")

    def _draw_weather_glyph(self, code):
        for item in self.weather_glyph:
            self.canvas.delete(item)
        self.weather_glyph = []

        cx, cy = self.weather_glyph_at
        c = self.canvas
        kind = code[:2]
        sun = "#FBBF24"
        cloud = "#A9B2C8"
        add = self.weather_glyph.append

        if kind == "01":                                   # clear
            add(c.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, fill=sun, outline=""))
            for i in range(8):
                a = math.pi * i / 4
                add(c.create_line(cx + 14 * math.cos(a), cy + 14 * math.sin(a),
                                  cx + 19 * math.cos(a), cy + 19 * math.sin(a),
                                  fill=sun, width=2))
            return
        if kind == "02":                                   # sun behind cloud
            add(c.create_oval(cx - 2, cy - 18, cx + 16, cy, fill=sun, outline=""))
        if kind == "13":                                   # snow
            for i in range(3):
                sx = cx - 9 + i * 9
                add(c.create_line(sx - 3, cy + 11, sx + 3, cy + 18, fill="#60A5FA", width=2))
                add(c.create_line(sx + 3, cy + 11, sx - 3, cy + 18, fill="#60A5FA", width=2))
        elif kind in ("09", "10"):                         # rain
            for i in range(3):
                sx = cx - 9 + i * 9
                add(c.create_line(sx, cy + 11, sx - 3, cy + 19, fill="#60A5FA", width=2))
        elif kind == "11":                                 # storm
            add(c.create_polygon(cx + 2, cy + 9, cx - 5, cy + 20, cx, cy + 20,
                                 cx - 4, cy + 29, cx + 7, cy + 16, cx + 2, cy + 16,
                                 fill="#FBBF24", outline=""))
        elif kind == "50":                                 # mist
            for i in range(3):
                add(c.create_line(cx - 14, cy + 3 + i * 6, cx + 14, cy + 3 + i * 6,
                                  fill=cloud, width=2))
            return

        add(c.create_oval(cx - 16, cy - 5, cx + 1, cy + 9, fill=cloud, outline=""))
        add(c.create_oval(cx - 6, cy - 12, cx + 12, cy + 7, fill=cloud, outline=""))
        add(c.create_rectangle(cx - 14, cy + 1, cx + 11, cy + 9, fill=cloud, outline=""))

    # ---------- music player ----------
    def _build_music_card(self):
        """The player, laid out across the header rather than down a column.

        Wider than it is tall now, so the transport moves to the right of the
        title instead of under it, and the progress bar runs the full width
        along the bottom where there is nothing to compete with it.
        """
        art = ui_asset("Home", "Music_bg.png")
        if art is not None:
            self._place_asset(art, MUSIC_X0, TOP_Y0)
        else:
            self._card(MUSIC_X0, TOP_Y0, MUSIC_X1, TOP_Y1, 16)

        art = ImageTk.PhotoImage(_album_art_image(38, 10))
        self._photos.append(art)
        self.canvas.create_image(MUSIC_X0 + 12, TOP_Y0 + 11, image=art, anchor="nw")

        self.canvas.create_text(MUSIC_X0 + 60, TOP_Y0 + 15, text="MUSIC PLAYER",
                                anchor="w", font=self._font(7, True), fill=COL_INDIGO)
        self.track_id = self.canvas.create_text(
            MUSIC_X0 + 60, TOP_Y0 + 30, text="", anchor="w",
            font=self._font(9, True), fill=COL_TEXT)
        self.artist_id = self.canvas.create_text(
            MUSIC_X0 + 60, TOP_Y0 + 45, text="", anchor="w",
            font=self._font(7), fill=COL_TEXT_DIM)

        # Transport on the right, with the play/pause ring biggest: it is the
        # one of the three that is pressed, and the only one that changes shape.
        cy = TOP_Y0 + 30
        pcx = MUSIC_X1 - 60
        self.prev_items = self._skip_glyph(pcx - 34, cy, forward=False)
        self.next_items = self._skip_glyph(pcx + 34, cy, forward=True)
        # Shuffle and repeat have nowhere to go at this height and were never
        # wired to anything, so the transport is only what actually works.
        self.shuffle_items = []
        self.repeat_items = []

        self.play_ring = self.canvas.create_oval(pcx - 16, cy - 16, pcx + 16, cy + 16,
                                                 fill=COL_INDIGO, outline="", tags="playpause")
        self.play_left = self.canvas.create_rectangle(pcx - 5, cy - 6, pcx - 2, cy + 6,
                                                      fill=COL_CARD, outline="", tags="playpause")
        self.play_right = self.canvas.create_rectangle(pcx + 2, cy - 6, pcx + 5, cy + 6,
                                                       fill=COL_CARD, outline="", tags="playpause")
        self.play_tri = self.canvas.create_polygon(pcx - 5, cy - 7, pcx + 7, cy, pcx - 5, cy + 7,
                                                   fill=COL_CARD, outline="", state="hidden",
                                                   tags="playpause")
        self.canvas.tag_bind("playpause", "<Button-1>", self.toggle_media_pause)

        self._bars_glyph(MUSIC_X1 - 16, TOP_Y0 + 16, COL_INDIGO, (4, 7, 10, 7, 4))

        bx0, bx1, by = MUSIC_X0 + 58, MUSIC_X1 - 46, TOP_Y1 - 15
        self._progress_span = (bx0, bx1, by)
        self._round_rect(bx0, by - 2, bx1, by + 2, 2, fill=COL_TRACK, outline="")
        self.progress_fill = self._round_rect(bx0, by - 2, bx0 + 1, by + 2, 2,
                                              fill=COL_INDIGO, outline="")
        self.progress_knob = self.canvas.create_oval(bx0 - 4, by - 4, bx0 + 4, by + 4,
                                                     fill=COL_INDIGO, outline=COL_CARD, width=2)
        self.elapsed_id = self.canvas.create_text(bx0 - 6, by, text="00:00", anchor="e",
                                                  font=self._font(6), fill=COL_TEXT_DIM)
        self.duration_id = self.canvas.create_text(bx1 + 6, by, text="00:00", anchor="w",
                                                   font=self._font(6), fill=COL_TEXT_DIM)

    def _shuffle_glyph(self, cx, cy):
        c, w = self.canvas, 2
        return [
            c.create_line(cx - 8, cy - 4, cx - 2, cy - 4, cx + 3, cy + 4, cx + 8, cy + 4,
                          fill=COL_TEXT_FAINT, width=w, smooth=False),
            c.create_line(cx - 8, cy + 4, cx - 2, cy + 4, cx + 3, cy - 4, cx + 8, cy - 4,
                          fill=COL_TEXT_FAINT, width=w, smooth=False),
            c.create_polygon(cx + 5, cy - 7, cx + 9, cy - 4, cx + 5, cy - 1,
                             fill=COL_TEXT_FAINT, outline=""),
            c.create_polygon(cx + 5, cy + 1, cx + 9, cy + 4, cx + 5, cy + 7,
                             fill=COL_TEXT_FAINT, outline=""),
        ]

    def _skip_glyph(self, cx, cy, forward):
        c, s = self.canvas, 1 if forward else -1
        return [
            c.create_polygon(cx - 7 * s, cy - 7, cx + 3 * s, cy, cx - 7 * s, cy + 7,
                             fill=COL_TEXT_FAINT, outline=""),
            c.create_rectangle(cx + 3 * s, cy - 7, cx + 6 * s, cy + 7,
                               fill=COL_TEXT_FAINT, outline=""),
        ]

    def _repeat_glyph(self, cx, cy):
        c = self.canvas
        return [
            c.create_arc(cx - 8, cy - 7, cx + 8, cy + 7, start=20, extent=300,
                         style="arc", outline=COL_TEXT_FAINT, width=2),
            c.create_polygon(cx + 4, cy - 9, cx + 9, cy - 5, cx + 3, cy - 2,
                             fill=COL_TEXT_FAINT, outline=""),
        ]

    def _build_mascot(self):
        # Sits in the clear strip between the header cards and the top of her
        # ears, so it reads as hers without covering the character.
        self.state_pill = self._round_rect(MASCOT_CX - 40, STATE_LABEL_Y - 11,
                                           MASCOT_CX + 40, STATE_LABEL_Y + 11, 11,
                                           fill=COL_TRACK, outline="")
        self.state_text_id = self.canvas.create_text(
            MASCOT_CX, STATE_LABEL_Y, text="", font=self._font(9, True), fill=COL_TEXT_DIM)
        self.mascot_item = self.canvas.create_image(MASCOT_CX, MASCOT_CY)

    # ---------- mode cards ----------
    def _mode_glyph(self, kind, cx, cy, colour, tint):
        c = self.canvas
        items = [self._round_rect(cx - 15, cy - 15, cx + 15, cy + 15, 9,
                                  fill=tint, outline="")]
        if kind == "TUTOR":                      # mortarboard over books
            items.append(c.create_polygon(cx, cy - 9, cx + 10, cy - 4, cx, cy + 1, cx - 10, cy - 4,
                                          fill=colour, outline=""))
            items.append(c.create_rectangle(cx - 5, cy, cx + 5, cy + 4, fill=colour, outline=""))
            items.append(c.create_line(cx + 10, cy - 4, cx + 10, cy + 4, fill=colour, width=2))
            items.append(self._round_rect(cx - 9, cy + 6, cx + 9, cy + 11, 2,
                                          fill=_mix(colour, "#FFFFFF", 0.45), outline=""))
        elif kind == "CO-TELL":                  # two chat bubbles
            items.append(self._round_rect(cx - 11, cy - 10, cx + 4, cy + 1, 4,
                                          fill=colour, outline=""))
            items.append(self._round_rect(cx - 3, cy - 1, cx + 11, cy + 10, 4,
                                          fill=_mix(colour, "#FFFFFF", 0.45), outline=""))
        else:                                    # head speaking
            items.append(c.create_oval(cx - 10, cy - 9, cx + 1, cy + 2, fill=colour, outline=""))
            items.append(c.create_polygon(cx - 10, cy + 1, cx + 1, cy + 1, cx + 1, cy + 9,
                                          cx - 10, cy + 9, fill=colour, outline=""))
            for r in (5, 8):
                items.append(c.create_arc(cx + 1 - r, cy - r, cx + 1 + r, cy + r,
                                          start=-55, extent=110, style="arc",
                                          outline=colour, width=2))
        return items

    # One image per mode, each carrying its own title, blurb, avatar and arrow.
    MODE_ART = {"TUTOR": "Tutor.png", "CO-TELL": "Co_tell.png",
                "RE-TELL": "Re_tell.png"}

    def _build_mode_cards(self):
        panel = ui_asset("Home", "Mode BG.png")
        if panel is not None:
            self._place_asset(panel, MODES_X0, MODES_Y0)
            # The panel art carries no wording, so the heading is still drawn --
            # over the rainbow, which is why it is dark rather than tinted.
            self.canvas.create_text((MODES_X0 + MODES_X1) / 2, MODES_Y0 + 19,
                                    text="MODE", font=self._font(15, True),
                                    fill="#3A2E6E")
        else:
            self._card(MODES_X0, MODES_Y0, MODES_X1, MODES_Y1, 16)
            self.canvas.create_text((MODES_X0 + MODES_X1) / 2, MODES_Y0 + 18,
                                    text="CHOOSE MODE", font=self._font(9, True),
                                    fill=COL_TEXT)
        for dx, dy, s in ((-10, -5, 4), (-2, 3, 3)):
            x, y = MODES_X1 - 18 + dx, MODES_Y0 + 18 + dy
            self.canvas.create_polygon(x, y - s, x + s * 0.35, y - s * 0.35, x + s, y,
                                       x + s * 0.35, y + s * 0.35, x, y + s,
                                       x - s * 0.35, y + s * 0.35, x - s, y,
                                       x - s * 0.35, y - s * 0.35,
                                       fill="#A78BFA", outline="")

        self.cards = []
        for i, mode in enumerate(self.modes):
            y0 = MODE_CARD_Y0 + i * (MODE_CARD_H + MODE_CARD_GAP)
            y1 = y0 + MODE_CARD_H
            accent = MODE_ACCENTS[mode]
            tag = f"mode{i}"

            art = ui_asset("Home", self.MODE_ART[mode])
            if art is not None:
                self._place_asset(art, MODE_CARD_X0, y0, tag)
                # Which mode is chosen cannot be shown by recolouring a picture,
                # so it is a ring around the chosen one. Created for every row
                # and hidden until _refresh_cards picks one.
                ring = self._round_rect(MODE_CARD_X0 - 2, y0 - 2,
                                        MODE_CARD_X1 + 2, y1 + 2, 14,
                                        fill="", outline=accent, width=3, tags=tag)
                self.canvas.itemconfigure(ring, state="hidden")
                self.canvas.tag_bind(tag, "<Button-1>",
                                     lambda e, idx=i: self.set_mode(idx))
                self.cards.append({"body": None, "title": None, "blurb": None,
                                   "chevron": None, "ring": ring,
                                   "accent": accent, "tint": MODE_TINTS[mode]})
                continue
            body = self._round_rect(MODE_CARD_X0, y0, MODE_CARD_X1, y1, 12,
                                    fill=MODE_TINTS[mode], outline=MODE_TINTS[mode],
                                    width=1, tags=tag)
            # The glyph sits in its own white tile at the top-left of the row,
            # with the wording under it rather than beside it: 158px of row is
            # not enough to put an icon, a title, a two-line blurb and a chevron
            # side by side without the blurb collapsing to one word per line.
            glyph = self._mode_glyph(mode, MODE_CARD_X0 + 20, y0 + 21,
                                     accent, "#FFFFFF")
            title = self.canvas.create_text(MODE_CARD_X0 + 38, y0 + 21,
                                            text=f"{mode} MODE", anchor="w",
                                            font=self._font(8, True), fill=accent, tags=tag)
            # On the title's row, not the bottom corner: down there it sat
            # against the second line of the blurb, and in a row this size the
            # two were touching.
            chevron = self.canvas.create_line(
                MODE_CARD_X1 - 13, y0 + 16,
                MODE_CARD_X1 - 8, y0 + 21,
                MODE_CARD_X1 - 13, y0 + 26,
                fill=accent, width=2, capstyle="round", joinstyle="round", tags=tag)
            blurb = self.canvas.create_text(MODE_CARD_X0 + 11, y0 + 36, text=MODE_BLURBS[mode],
                                            anchor="nw", justify="left",
                                            width=MODE_CARD_X1 - MODE_CARD_X0 - 22,
                                            font=self._font(7), fill=COL_TEXT_DIM, tags=tag)
            for item in glyph:
                self.canvas.itemconfig(item, tags=tag)
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, idx=i: self.set_mode(idx))
            self.cards.append({"body": body, "title": title, "blurb": blurb,
                               "chevron": chevron, "ring": None,
                               "accent": accent, "tint": MODE_TINTS[mode]})

    # ---------- transcript ----------
    def _build_transcript_panel(self):
        """The board she writes on -- the biggest single thing on the screen.

        It was a 192x134 strip in the right-hand column, which fitted three
        lines of a spoken sentence and clipped the rest. Given half the width of
        the panel it holds a real answer, which is the point of showing it.
        """
        art = ui_asset("Home", "Transcribe Board.png")
        if art is not None:
            # The card, its heading and the little stars are all painted into
            # this one, so none of that chrome is drawn again below.
            self._place_asset(art, BOARD_X0, BOARD_Y0)
        else:
            self._card(BOARD_X0, BOARD_Y0, BOARD_X1, BOARD_Y1, 18)
        pad = 18
        # Everything in this block is the heading the ARTWORK already carries --
        # the dot grid, the title, its underline, the little stars. Drawn again
        # on top of it they would simply double up.
        if art is None:
            for gy in range(int(BOARD_Y0) + 40, int(BOARD_Y1) - 12, 14):
                for gx in range(int(BOARD_X0) + pad, int(BOARD_X1) - pad, 14):
                    self.canvas.create_oval(gx, gy, gx + 1, gy + 1,
                                            fill="#E7EAF6", outline="")

            self.canvas.create_text((BOARD_X0 + BOARD_X1) / 2, BOARD_Y0 + 22,
                                    text="Transcribe Board", font=self._font(14, True),
                                    fill="#7C3AED")
            self.canvas.create_line((BOARD_X0 + BOARD_X1) / 2 - 74, BOARD_Y0 + 36,
                                    (BOARD_X0 + BOARD_X1) / 2 + 74, BOARD_Y0 + 36,
                                    fill="#C4B5FD", width=2, capstyle="round")
            for x, y, s in ((BOARD_X0 + 30, BOARD_Y0 + 22, 6),
                            (BOARD_X1 - 34, BOARD_Y0 + 20, 7),
                            (BOARD_X1 - 18, BOARD_Y0 + 32, 4)):
                self.canvas.create_polygon(
                    x, y - s, x + s * 0.34, y - s * 0.34, x + s, y,
                    x + s * 0.34, y + s * 0.34, x, y + s,
                    x - s * 0.34, y + s * 0.34, x - s, y,
                    x - s * 0.34, y - s * 0.34, fill="", outline="#C4B5FD", width=1)

        # Below the painted heading rather than beside it: the artwork's title
        # and rule occupy the top ~50px of the card.
        head = 62 if art is not None else 54
        self.panel_bars = self._bars_glyph(BOARD_X0 + pad + 4, BOARD_Y0 + 22, COL_INDIGO,
                                           (5, 9, 13, 9, 5))
        if art is not None:
            for item in self.panel_bars:
                self.canvas.itemconfigure(item, state="hidden")
        self.speaker_id = self.canvas.create_text(
            BOARD_X0 + pad, BOARD_Y0 + head, text="You said:", anchor="w",
            font=self._font(8, True), fill=COL_INDIGO)
        self.transcript_id = self.canvas.create_text(
            BOARD_X0 + pad, BOARD_Y0 + head + 16,
            text="Tap SPEAK or say “Hey Liza” to begin.",
            anchor="nw", justify="left", width=BOARD_X1 - BOARD_X0 - pad * 2,
            font=self._font(11), fill=COL_TEXT_DIM)

        self.panel_status_id = self.canvas.create_text(
            BOARD_X1 - pad - 30, BOARD_Y1 - 16, text="", anchor="e",
            font=self._font(8), fill=COL_TEXT_DIM)
        self.status_dots = []
        for i in range(3):
            x = BOARD_X1 - pad - 22 + i * 8
            self.status_dots.append(self.canvas.create_oval(
                x - 3, BOARD_Y1 - 19, x + 3, BOARD_Y1 - 13, fill=COL_TRACK, outline=""))

    # ---------- action buttons ----------
    BUTTON_ART = {"SPEAK": "Speak Button.png", "STOP": "Stop Button.png",
                  "SLEEP": "Sleep Button.png"}

    def _build_buttons(self):
        self.buttons = {}
        handlers = {"SPEAK": self.wake_up, "STOP": self.stop_speaking, "SLEEP": self.go_to_sleep}
        for (label, sub, c0, c1, sub_col, icon), x0 in zip(ACTIONS, BTN_XS):
            tag = f"btn{label}"
            art = ui_asset("Home", self.BUTTON_ART[label])
            if art is not None:
                # Label, sub-label and icon are painted into the artwork, so
                # nothing is written over it.
                self._place_asset(art, x0, BTN_Y0, tag)
            else:
                self._place_photo(_action_image(BTN_W, BTN_H, 20, c0, c1, icon), x0, BTN_Y0, tag)
                # 70, not 92: the icon ring ends 57px in, so this is the same
                # clearance beside a narrower button.
                self.canvas.create_text(x0 + 70, BTN_Y0 + 26, text=label, anchor="w",
                                        font=self._font(15, True), fill="#FFFFFF", tags=tag)
                self.canvas.create_text(x0 + 70, BTN_Y0 + 45, text=sub, anchor="w",
                                        font=self._font(7), fill=sub_col, tags=tag)
            self.canvas.tag_bind(tag, "<Button-1>", handlers[label])
            self.buttons[label] = tag

    # ---------- runtime ----------
    def _animate(self):
        label, colour, activity = STATE_STYLE.get(self.current_state, STATE_STYLE["idle"])
        self.phase += 0.12
        self.frame += 1

        bucket = MASCOT_FOR_STATE.get(self.current_state, "idle")
        if bucket != self._mascot_bucket:
            self._mascot_bucket = bucket
            self.mascot_index = 0
        frames = None
        if self.ui_mode == "3d":
            frames = self.mascot_frames_big.get(bucket)
            if frames is None:
                # A state change inside 3D mode: this bucket may not be built yet.
                self._ensure_big_frames(bucket)
        # The two sets come from the same PNGs, so mascot_index is valid in
        # either and the animation does not jump when the big set arrives.
        frames = frames or self.mascot_frames.get(bucket) or []
        if frames:
            # Asleep she should look settled, not idling, so the loop is parked.
            if self.current_state != "sleeping":
                self.mascot_index = (self.mascot_index + 1) % len(frames)
            self.canvas.itemconfig(self.mascot_item, image=frames[self.mascot_index])

        for i, (dot, dx, dy) in enumerate(self.head_dots):
            swing = math.sin(self.phase * 2.3 + i * 0.9) ** 2
            r = 2.5 + 3 * activity * swing
            self.canvas.coords(dot, dx - r, dy - r, dx + r, dy + r)

        for i, bar in enumerate(self.panel_bars):
            swing = math.sin(self.phase * 2.6 + i * 0.62) ** 2
            h = 3 + 12 * (0.25 + 0.75 * swing) * max(activity, 0.15)
            x = self.canvas.coords(bar)[0]
            self.canvas.coords(bar, x, BOARD_Y0 + 22 - h / 2, x, BOARD_Y0 + 22 + h / 2)
            self.canvas.itemconfig(bar, fill=_mix(colour, COL_TRACK, 0.5 - 0.4 * swing * activity))

        caption = label.upper()
        if caption != self._state_caption:
            # Only remeasured when the wording changes; the pill hugs captions
            # as different as "LISTENING" and "SOMETHING'S WRONG".
            self._state_caption = caption
            half = self._font(9, True).measure(caption) / 2 + 12
            self.canvas.coords(self.state_pill, *self._round_rect_points(
                MASCOT_CX - half, STATE_LABEL_Y - 10, MASCOT_CX + half, STATE_LABEL_Y + 10, 10))
            self.canvas.itemconfig(self.state_text_id, text=caption)
        self.canvas.itemconfig(self.state_text_id, fill=_mix(colour, COL_TEXT, 0.2))
        self.canvas.itemconfig(self.state_pill, fill=_mix(colour, "#FFFFFF", 0.86))

        busy = self.current_state in ("listening", "thinking", "speaking")
        self.canvas.itemconfig(self.panel_status_id, text=label if busy else "",
                               fill=colour)
        for i, dot in enumerate(self.status_dots):
            lit = busy and (self.frame // 6) % 3 == i
            self.canvas.itemconfig(dot, fill=colour if lit else COL_TRACK)

        self._refresh_media_controls()
        self.root.after(FRAME_MS, self._animate)

    def _refresh_cards(self):
        for i, card in enumerate(self.cards):
            chosen = i == self.current_mode_index
            accent = card["accent"]
            if card.get("ring") is not None:
                # A picture cannot be tinted, so the chosen one wears a ring.
                self.canvas.itemconfigure(card["ring"],
                                          state="normal" if chosen else "hidden")
                continue
            self.canvas.itemconfig(card["body"],
                                   fill=_mix(card["tint"], "#FFFFFF", 0.35) if chosen else card["tint"],
                                   outline=accent if chosen else card["tint"],
                                   width=2 if chosen else 1)
            self.canvas.itemconfig(card["blurb"], fill=COL_TEXT if chosen else COL_TEXT_DIM)

    # ---------- music ----------
    def set_now_playing(self, title, loading=False):
        """YouTube titles are usually "Artist - Track (Official Video)", so the
        card shows the two halves separately when that shape is recognisable.

        `loading` covers the gap between asking for something and hearing it:
        yt-dlp has to fetch the page, solve YouTube's JS challenge and buffer
        before mpv renders a thing, which runs to ten seconds or more on this
        Pi. Without it the screen sits on the idle UI the whole time and a
        request that is working looks like one that was ignored."""
        if title:
            artist, _, track = title.partition(" - ")
            if not track:
                artist, track = "", title
        else:
            artist, track = "", "Nothing playing"

        self.media.update({"title": title, "artist": artist.strip(),
                           "pos": 0.0, "dur": 0.0, "paused": False})
        self.canvas.itemconfig(
            self.track_id,
            text=self._ellipsize(track.strip(), self._font(9, True),
                                 MUSIC_X1 - MUSIC_X0 - 170),
            fill=COL_TEXT if title else COL_TEXT_DIM)
        self.canvas.itemconfig(
            self.artist_id,
            text=self._ellipsize("Loading…" if loading else
                                 (artist.strip() or ("—" if title else "Ask me to play a song")),
                                 self._font(7), MUSIC_X1 - MUSIC_X0 - 170))
        self.set_media_progress(0.0, 0.0, False)

    def set_media_progress(self, pos, dur, paused):
        self.media.update({"pos": pos or 0.0, "dur": dur or 0.0, "paused": bool(paused)})
        bx0, bx1, by = self._progress_span
        fraction = (pos / dur) if dur else 0.0
        fraction = max(0.0, min(1.0, fraction))
        x = bx0 + (bx1 - bx0) * fraction

        self.canvas.coords(self.progress_fill, *self._round_rect_points(bx0, by - 2,
                                                                       max(bx0 + 1, x), by + 2, 2))
        self.canvas.coords(self.progress_knob, x - 5, by - 5, x + 5, by + 5)
        self.canvas.itemconfig(self.elapsed_id, text=_fmt_clock(pos))
        self.canvas.itemconfig(self.duration_id, text=_fmt_clock(dur))

    def _round_rect_points(self, x0, y0, x1, y1, r):
        return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
                x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]

    def _refresh_media_controls(self):
        # 3D mode hides the music card, and this runs every frame: without this
        # line it puts the play/pause glyphs straight back on an otherwise empty
        # screen, sixteen times a second.
        if self.ui_mode == "3d":
            return
        live = media_active.is_set()
        shade = COL_INDIGO if live else COL_TEXT_FAINT
        self.canvas.itemconfig(self.play_ring, fill=shade)
        paused = self.media["paused"]
        self.canvas.itemconfig(self.play_tri, state="normal" if paused else "hidden")
        for item in (self.play_left, self.play_right):
            self.canvas.itemconfig(item, state="hidden" if paused else "normal")
        self.canvas.itemconfig(self.progress_fill, fill=shade)
        self.canvas.itemconfig(self.progress_knob, fill=shade)

    def toggle_media_pause(self, event=None):
        if media_active.is_set():
            media.mpv_command(["cycle", "pause"])
        return "break"

    # ---------- emotion ----------
    def set_emotion(self, mood):
        """Record how she says she is feeling. Nothing is drawn for it any more.

        The chip that used to sit under her (CURIOUS, CALM, HAPPY...) was removed
        on request. The EMOTION: line is still parsed and kept here, so anything
        that wants it later -- a log, an expression, a different treatment --
        still has it; it simply has no pill on screen."""
        mood = (mood or "").strip().lower()
        if mood:
            self.emotion = mood

    # ---------- 3D-only mode ----------
    def set_ui_mode(self, mode):
        """Hide every widget but the mascot, or put them all back.

        Everything on screen is one canvas, so this is done by item state rather
        than by tearing anything down: nothing is rebuilt, nothing loses its
        contents, and music carries on playing behind it."""
        mode = "3d" if mode == "3d" else "normal"
        if mode == self.ui_mode:
            return
        # The wallpaper has to survive 3D mode too. This set is what is NOT
        # hidden, and everything else on the canvas is -- so leaving the
        # background out of it blanked the whole picture the moment she went
        # 3D-only, which is the one mode where she is all there is to look at.
        keep = {self.mascot_item}
        if self.bg_item is not None:
            keep.add(self.bg_item)

        if mode == "3d":
            self._hidden_items = []
            for item in self.canvas.find_all():
                if item in keep:
                    continue
                # Only what is visible NOW is recorded. The media buttons are
                # already shown and hidden every frame by _refresh_media_controls,
                # and force-showing those on the way back would put a play
                # triangle on screen over a track that is not paused.
                if self.canvas.itemcget(item, "state") == "hidden":
                    continue
                self.canvas.itemconfigure(item, state="hidden")
                self._hidden_items.append(item)
            self.canvas.coords(self.mascot_item, UI_W // 2, MASCOT_3D_CY)
            self._ensure_big_frames(self._mascot_bucket or "idle")
        else:
            for item in self._hidden_items:
                try:
                    self.canvas.itemconfigure(item, state="normal")
                except tk.TclError:
                    pass
            self._hidden_items = []
            self.canvas.coords(self.mascot_item, MASCOT_CX, MASCOT_CY)

        self.ui_mode = mode
        # The SPEAK button is hidden in 3D mode, but the root tap-to-wake binding
        # is not, so a tap anywhere still wakes her. Nothing here is a dead end.
        print(f"[UI] Mode -> {mode}.", flush=True)

    def _ensure_big_frames(self, bucket):
        """Upscale one animation for 3D mode, without stalling the UI.

        The resize is the expensive half and runs on a worker; the PhotoImage
        half has to happen on the Tk thread, so it is fed back in small batches.
        Until it lands, _animate falls back to the normal-size frames -- 3D mode
        is never made to wait for this."""
        if not bucket or bucket in self.mascot_frames_big or bucket in self._big_pending:
            return
        paths = sorted(glob.glob(os.path.join(MASCOT_CACHE_DIR, bucket, "*.png")))
        if not paths:
            return
        self._big_pending.add(bucket)
        holder = {"images": None}

        def work():
            images = []
            for path in paths:
                try:
                    images.append(Image.open(path).convert("RGBA")
                                  .resize((MASCOT_3D_W, MASCOT_3D_H), Image.LANCZOS))
                except Exception:
                    pass
            # A plain assignment and nothing else. This thread must not touch Tk
            # at all -- not even root.after -- so the Tk side polls for it below.
            holder["images"] = images

        threading.Thread(target=work, daemon=True).start()
        self.root.after(150, lambda: self._absorb_big_frames(bucket, holder, []))

    def _absorb_big_frames(self, bucket, holder, done):
        """Tk-thread half: PhotoImages, a few per tick so nothing stutters."""
        images = holder["images"]
        if images is None:
            self.root.after(150, lambda: self._absorb_big_frames(bucket, holder, done))
            return
        for image in images[len(done):len(done) + 8]:
            done.append(ImageTk.PhotoImage(image))
        if len(done) < len(images):
            self.root.after(16, lambda: self._absorb_big_frames(bucket, holder, done))
            return
        self.mascot_frames_big[bucket] = done
        self._big_pending.discard(bucket)
        print(f"[MASCOT] 3D frames ready for '{bucket}' ({len(done)}).", flush=True)

    def set_weather(self, reading):
        self.canvas.itemconfig(self.temp_id, text=f"{reading['temp']}°C")
        self.canvas.itemconfig(self.desc_id, text=reading["desc"])
        self.canvas.itemconfig(self.high_id, text=f"{reading['high']}°")
        self.canvas.itemconfig(self.low_id, text=f"{reading['low']}°")
        self.canvas.itemconfig(self.city_id,
                               text=f"{reading['city']}  •  Humidity {reading['humidity']}%")
        self._draw_weather_glyph(reading["icon"])

    def _tick_clock(self):
        now = datetime.now()
        text = now.strftime("%I:%M").lstrip("0")
        self.canvas.itemconfig(self.clock_id, text=text)
        # Placed by measurement rather than a fixed offset: "9:05" and "12:45"
        # are very different widths and the meridiem has to sit against both.
        self.canvas.coords(self.meridiem_id,
                           CLOCK_X0 + 60 + self._font(19, True).measure(text), TOP_Y0 + 29)
        self.canvas.itemconfig(self.meridiem_id, text=now.strftime("%p"))
        self.canvas.itemconfig(self.date_id, text=now.strftime("%a, %d %b %Y"))

        cx, cy = self._clock_centre
        minute = math.pi * now.minute / 30
        hour = math.pi * ((now.hour % 12) + now.minute / 60) / 6
        self.canvas.coords(self.minute_hand, cx, cy,
                           cx + 14 * math.sin(minute), cy - 14 * math.cos(minute))
        self.canvas.coords(self.hour_hand, cx, cy,
                           cx + 9 * math.sin(hour), cy - 9 * math.cos(hour))
        self.root.after(1000, self._tick_clock)

    def set_state(self, state, caption=None):
        if state not in STATE_STYLE:
            state = "idle"

        # Sleep is a deliberate instruction and outranks whatever is playing;
        # everything else has to wait for the speaker to fall quiet, or the
        # mascot flips to "listening" over the top of her own voice.
        if state != "sleeping" and self.asleep:
            return
        if state in ("idle", "listening", "warmup") and (
                playback_active.is_set() or not audio_queue.empty() or media_active.is_set()):
            return

        self.current_state = state

    # create_text's `width` wraps the transcript horizontally but does nothing
    # about its height, so a long line simply kept growing downwards -- out
    # through the bottom of its own card and over the buttons underneath. The
    # panel is 192x134 with a status row across the bottom, which leaves exactly
    # three lines at this font; a spoken sentence routinely needs six. Measured,
    # not guessed: "Computers are electronic devices that process data according
    # to a set of instructions..." laid out to y=416 against a card ending at 392.
    def _fit_transcript(self, text):
        """The longest leading part of `text` that stays inside the card."""
        # Clear of the status dots, which start at BOARD_Y1 - 19.
        bottom = BOARD_Y1 - 24
        def fits(candidate):
            """Lay `candidate` out in the real item and see where it ends."""
            self.canvas.itemconfig(self.transcript_id, text=candidate)
            box = self.canvas.bbox(self.transcript_id)
            return box is None or box[3] <= bottom

        if fits(text):
            return text
        # Binary search rather than dropping a word at a time: the question is
        # how tall the text lands once WRAPPED, and the only way to answer that
        # is to lay it out and measure it, so the aim is to do it ~8 times
        # instead of ~40.
        low, high, cut = 0, len(text), 0
        while low <= high:
            mid = (low + high) // 2
            if fits(text[:mid].rstrip() + "…"):
                cut, low = mid, mid + 1
            else:
                high = mid - 1
        # Snap back to a word boundary -- only ever shortens it, so what fitted
        # still fits. A single unbroken word longer than the card has no space
        # to snap to and is left cut mid-word, which is the best available.
        head = text[:cut].rstrip()
        if " " in head:
            head = head[:head.rindex(" ")].rstrip()
        return (head + "…") if head else text[:cut].rstrip() + "…"

    def set_transcript(self, text, speaker="user"):
        text = (text or "").strip()
        if not text:
            return
        # The FULL line is kept here and only the drawn copy is clipped.
        self.transcript = text
        self.speaker = speaker
        liza = speaker == "liza"
        self.canvas.itemconfig(self.speaker_id,
                               text="Liza said:" if liza else "You said:",
                               fill=STATE_STYLE["speaking"][1] if liza else COL_INDIGO)
        self.canvas.itemconfig(self.transcript_id, text=self._fit_transcript(text),
                               fill=COL_TEXT)

    def wake_up(self, event=None):
        """The Speak button. Wakes her from anything, including sleep."""
        print("[UI] Speak tapped. Waking up...", flush=True)
        self.asleep = False
        sleep_event.clear()
        wake_event.set()
        return "break"

    def tap_to_wake(self, event=None):
        """Any tap on the screen away from a control.

        Ignored while she is asleep, which is the whole point of the Sleep
        button: this panel reports stray touches on its own, and treating
        those as a wake would put her straight back to listening. Asleep,
        only the Speak button or the wake word count.
        """
        if self.asleep or self.overlay:
            # An overlay owns the whole screen, and every control on it is a
            # canvas item with its own binding. This handler is bound to the
            # ROOT window, so it fires on those taps as well -- without this,
            # picking a class would also wake her and start the microphone
            # behind the screen the child is still looking at.
            return
        return self.wake_up(event)

    def stop_speaking(self, event=None):
        # Music/video first: audio-only playback shows no window of its own, so
        # this button is the only way to stop a song.
        if media_active.is_set():
            print("[UI] Stop tapped, stopping media playback.", flush=True)
            assistant.stop_media_playback()
        if playback_active.is_set() or not audio_queue.empty():
            print("[UI] Stop tapped, cutting the reply short.", flush=True)
            assistant.interrupt_playback()
        return "break"          # do not let the tap fall through and re-wake her

    def go_to_sleep(self, event=None):
        print("[UI] Sleep tapped. Going to standby...", flush=True)
        self.asleep = True
        wake_event.clear()
        sleep_event.set()
        if media_active.is_set():
            assistant.stop_media_playback()
        if playback_active.is_set() or not audio_queue.empty():
            assistant.interrupt_playback()
        self.current_state = "sleeping"
        return "break"          # otherwise the root tap-to-wake binding undoes this

    def set_mode(self, index):
        if index == self.current_mode_index:
            return
        self.current_mode_index = index
        self.current_mode = self.modes[index]
        self._refresh_cards()

        # Deliberately does NOT call interrupt_playback() here. This runs on the
        # Tk thread, which can fire while ai_loop has the microphone open inside
        # a blocking PyAudio read; tearing down aplay and starting new playback
        # underneath that read wedges the capture stream and the read never
        # returns, which froze the assistant permanently after every mode
        # change. ai_loop picks this up and speaks it once the mic is closed.
        #
        # Setting stop_playback_event here IS safe though -- it is a plain
        # Event flag, not a teardown of any subprocess or audio device. Without
        # it, a mode tap during an LLM reply sat invisible until that whole
        # reply finished generating and speaking (worker.join() in ai_loop
        # blocks on it), so switching modes felt like it required tapping Stop
        # first -- that tap was the thing actually breaking the reply early.
        # Setting the event here makes the in-flight stream_hf()/speak_sentence()
        # loops notice and bail out within one chunk, same as a Stop tap does,
        # so ai_loop reaches the top of its loop -- and this pending intro --
        # right away. ai_loop's own pending_mode_intro handler still does the
        # real interrupt_playback() sweep and clears this event again.
        stop_playback_event.set()
        app_state.pending_mode_intro = MODE_INTROS[self.current_mode]

    def cycle_mode(self, event=None):
        self.set_mode((self.current_mode_index + 1) % len(self.modes))

    # ==========================================
    # Student profiles and the Kindergarten screens
    # ==========================================
    # Drawn as full-screen OVERLAYS on the same canvas rather than as separate Tk
    # windows or frames. Tk has no z-index -- items stack in creation order -- so
    # an opaque rectangle created last covers everything under it, and covered
    # items stop receiving taps because the canvas dispatches to the topmost item
    # only. That gets modal screens for the price of one create_rectangle, with
    # the whole existing layout left untouched underneath.
    #
    # Every item carries OVERLAY_TAG, so dismissing a screen is one delete call
    # and there is no partial teardown to get wrong.
    OVERLAY_TAG = "overlay"

    # Where a KG lesson is drawn: the same column the Transcribe Board occupies
    # on the normal screen. Content sits here rather than across the middle of
    # the canvas so that the mascot beside it stays uncovered.
    KG_X0, KG_X1 = BOARD_X0, BOARD_X1
    KG_CX = (BOARD_X0 + BOARD_X1) / 2

    def _clear_overlay(self):
        self.canvas.delete(self.OVERLAY_TAG)
        self._overlay_photos = []
        self.overlay = None

    # The lesson screens, where tapping her means "stop, I want to ask you
    # something". Not the keyboard screens: she is behind the backing there and
    # cannot be tapped at all, and not the pickers, where nothing is being said.
    KG_ASK_SCREENS = {"kg_alpha", "kg_count", "kg_story"}

    # Where the Ask button sits on the lesson screens. The left column is empty
    # on all of them -- the mascot starts at 180 and the lesson itself is in the
    # board column from 402 -- so it costs no layout and sits far from both, and
    # far from the row along the bottom.
    #
    # That distance is the point. This was bound to the MASCOT once, and she is
    # 187 by 250 pixels in the middle of the screen: logs/liza.log recorded nine
    # taps in one session and not one question after them, because every one was
    # somebody brushing past her. A button a child has to reach for cannot be
    # pressed by accident, which is the whole difference between an interruption
    # they asked for and one they did not.
    KG_ASK_BOX = (22, 196, 166, 306)
    # The story screen has no free column -- its captions run the full width --
    # so it takes the gap in the bottom row instead.
    KG_ASK_BOX_STORY = (190, 414, 300, 462)

    # Its own tag, because this button is redrawn several times on one screen --
    # pressed, stopped, finished -- and each redraw must REPLACE the last. Drawn
    # over the top instead, the old label stays on the canvas underneath a live
    # button, and every press leaves another one behind.
    KG_ASK_TAG = "kg_ask_button"

    def _kg_ask_button(self, box=None):
        """Draw the Ask button, or the Listening state once it has been used."""
        self.canvas.delete(self.KG_ASK_TAG)
        x0, y0, x1, y1 = box or self.KG_ASK_BOX
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        roomy = (y1 - y0) >= 80

        if getattr(self, "_kg_asking", False):
            # Drawn over the button rather than beside it: a child who has just
            # pressed it needs to see that it worked, while she is still drawing
            # breath to ask them what they wanted.
            #
            # And it is still a BUTTON. It was a painted pill once, with no
            # binding on it at all, which left one press per screen and no way
            # to change your mind -- you either asked or you waited for the
            # lesson to move on. Pressing it again stops her.
            tag = self._overlay_button(
                x0, y0, x1, y1, "Listening", self._kg_ask,
                fill="#FEF3C7", text_colour="#92400E", size=13,
                sub="tap to stop" if roomy else None,
                label_frac=0.58 if roomy else None, sub_frac=0.80)
            self.canvas.addtag_withtag(self.KG_ASK_TAG, tag)
            return

        tag = self._overlay_button(
            x0, y0, x1, y1, "Ask me", self._kg_ask, fill="#F59E0B", size=14,
            sub="I'll stop and listen" if roomy else None,
            label_frac=0.66 if roomy else None, sub_frac=0.85)
        if roomy:
            # Same tag as the button, so the mark is part of the target rather
            # than a hole in the middle of it.
            self.canvas.create_text(cx, y0 + 34, text="?",
                                    font=self._font(30, True), fill="#FFFFFF",
                                    tags=(self.OVERLAY_TAG, tag))
        self.canvas.addtag_withtag(self.KG_ASK_TAG, tag)

    def _kg_ask_box(self):
        return (self.KG_ASK_BOX_STORY if self.overlay == "kg_story"
                else self.KG_ASK_BOX)

    def _kg_ask(self):
        """The Ask button, both ways round: start listening, or stop.

        ai_loop answers kg_ask_event before anything else in its KG branch, and
        abandons whatever question a screen was waiting for -- they have stopped
        answering that and asked something of their own.

        The second press matters as much as the first. By the time it happens
        ai_loop is already blocked inside the microphone read, so stopping is
        not a matter of not-starting: kg_ask_cancel is what reaches into that
        read and ends it, and kg_handle_doubt then says nothing at all, because
        answering somebody who has stopped listening is worse than silence.
        """
        if self.overlay not in self.KG_ASK_SCREENS:
            return None
        if getattr(self, "_kg_asking", False):
            print("[UI] Ask pressed again; stopping listening.", flush=True)
            self._kg_asking = False
            kg_ask_event.clear()
            kg_ask_cancel.set()
            assistant.interrupt_playback()
            self.set_state("idle")
        else:
            print("[UI] Ask tapped mid-lesson; stopping to listen.", flush=True)
            self._kg_asking = True
            kg_ask_cancel.clear()
            kg_ask_event.set()
            assistant.interrupt_playback()
            self.set_state("listening")
        self._kg_ask_button(self._kg_ask_box())
        return "break"

    def kg_ask_finished(self):
        """ai_loop has finished with a press: put the button back.

        Called through ui_invoke from the KG branch, in a finally, because the
        button is PAINTED as "Listening" and nothing else undoes that. Without
        it the screen went on saying she was listening long after she had
        stopped, and the next press was the one that put it right -- which is
        the "it only works once" this was reported as.
        """
        if not getattr(self, "_kg_asking", False):
            return
        self._kg_asking = False
        if self.overlay in self.KG_ASK_SCREENS:
            self._kg_ask_button(self._kg_ask_box())

    def _kg_keep_mascot(self):
        """Lift the mascot above the overlay backing, so she is IN the lesson.

        The backing _overlay_screen paints covers all 800x480, and that is what
        used to hide her: the alphabet arrived as a worksheet with nobody
        holding it. Raising the existing canvas item rather than drawing a new
        one means _animate goes on driving the same frames, so she blinks and
        breathes through a lesson instead of freezing into a still. The cached
        frames are RGBA, so she cuts out cleanly against the screen tint.

        Only screens that leave the mascot's rectangle clear may call this --
        see KG_CX. The keyboard screens still need the full width and so still
        cover her.
        """
        item = getattr(self, "mascot_item", None)
        if item is None:
            return
        try:
            self.canvas.tag_raise(item)
        except tk.TclError:
            # A canvas rebuild between screens; the next redraw lifts her again.
            pass

    def _overlay_screen(self, name, title, subtitle=None, tint=COL_BG,
                        backdrop=None, backdrop_at=(0, 0)):
        """The opaque backing every profile and KG screen is built on.

        `name` is set here, AFTER the clear, and not by the callers: _clear_overlay
        resets it to None, so a caller that set it first had it wiped again on
        every redraw. That left self.overlay permanently None, which silently
        disabled both of the guards that read it -- the story's comprehension
        question and the move to the next spelling word, each of which checks
        which screen is still up before firing.
        """
        # Moving to a DIFFERENT screen (not just redrawing this one) means the
        # child has walked away from whatever was being asked, so the microphone
        # should stop waiting for an answer to it. Without this, tapping Back
        # during a counting question left ai_loop -- the only thread that may
        # touch the microphone -- inside that listen for another forty seconds,
        # and the next screen's question went unheard for the whole of it.
        if self.overlay != name:
            kg.kg_cancel_listen()
        self._clear_overlay()
        self.overlay = name
        backing = self.canvas.create_rectangle(0, 0, UI_W, UI_H, fill=tint,
                                               outline="", tags=self.OVERLAY_TAG)
        # The backing must SWALLOW taps, not merely cover the screen.
        #
        # A canvas dispatches a click to the topmost item that has a binding for
        # it -- not to the topmost item. With no binding here, every tap on an
        # empty part of a profile or KG screen fell straight through to whatever
        # sat underneath, which is the full assistant UI. logs/liza.log shows the
        # result: twenty "[UI] Speak tapped" while a profile screen was open,
        # then "[MODE] Now in TUTOR mode" and an unasked-for mode intro spoken
        # over a child choosing their name. Binding it makes the overlay behave
        # like the modal screen it always looked like.
        self.canvas.tag_bind(backing, "<Button-1>", lambda event: "break")
        # Every screen starts with the mascot BEHIND the backing, and the ones
        # with room for her lift her out again with _kg_keep_mascot(). The reset
        # belongs here rather than in those screens: tag_raise is permanent, so
        # without it she stayed up from the last alphabet screen and reappeared
        # standing on top of the spelling keyboard, whose keys run the full
        # width and straight through where she stands.
        # A press belongs to the screen it was made on.
        self._kg_asking = False
        mascot = getattr(self, "mascot_item", None)
        if mascot is not None:
            try:
                self.canvas.tag_lower(mascot, backing)
            except tk.TclError:
                pass
        # Placed here, between the backing and the wording, because a panel put
        # down by the caller lands on TOP of the title -- which is how the
        # greeting went missing from the KG screen the first time.
        if backdrop is not None:
            art = ui_asset(*backdrop)
            if art is not None:
                self._place_overlay_asset(art, *backdrop_at)
        self.canvas.create_text(UI_W / 2, 40, text=title, font=self._font(20, True),
                                fill=COL_TEXT, tags=self.OVERLAY_TAG)
        if subtitle:
            self.canvas.create_text(UI_W / 2, 68, text=subtitle, font=self._font(10),
                                    fill=COL_TEXT_DIM, tags=self.OVERLAY_TAG)

    def _overlay_button(self, x0, y0, x1, y1, label, command, fill=COL_INDIGO,
                        text_colour="#FFFFFF", radius=12, size=13, sub=None,
                        label_frac=None, sub_frac=0.68):
        self._overlay_seq += 1
        tag = f"ovbtn{self._overlay_seq}"
        tags = (self.OVERLAY_TAG, tag)
        height = y1 - y0
        self._round_rect(x0, y0, x1, y1, radius, fill=fill, outline=fill, tags=tags)
        if label_frac is not None:
            label_y = y0 + height * label_frac
        else:
            label_y = (y0 + y1) / 2 if not sub else y0 + height * 0.36
        self.canvas.create_text((x0 + x1) / 2, label_y, text=label,
                                font=self._font(size, True), fill=text_colour, tags=tags)
        if sub:
            self.canvas.create_text((x0 + x1) / 2, y0 + height * sub_frac, text=sub,
                                    font=self._font(8), fill=text_colour, tags=tags)
        if command is not None:
            self.canvas.tag_bind(tag, "<Button-1>", lambda e: command())
        return tag

    # ---------- the chip that shows who is using the device ----------
    def _build_profile_chip(self):
        """Who is using the device, as the middle card of the header.

        It is the only control for WHO she is talking to, and the whole card is
        the tap target -- 212x78 rather than the 210x26 chip it replaces, which
        was under half what a fingertip reliably hits on this panel.
        """
        tags = (self.OVERLAY_TAG + "_never", "profilechip")
        art = ui_asset("Home", "Profile_bg.png")
        if art is not None:
            self.profile_chip_bg = self._place_asset(art, WHO_X0, TOP_Y0, tags)
        else:
            self.profile_chip_bg = self._card(WHO_X0, TOP_Y0, WHO_X1, TOP_Y1, 16,
                                              tags=tags)

        cx, cy = WHO_X0 + 34, TOP_Y0 + 39
        # The child's own animal, the same one their row wears in the picker, so
        # a pre-reader can tell at a glance whose device this currently is. The
        # image is set in refresh_profile_chip, because who is using it changes
        # while this item does not.
        self.profile_chip_face = None
        if profile_avatar({}, 46) is not None:
            self.profile_chip_face = self.canvas.create_image(
                cx, cy, anchor="center", tags=tags)
        else:
            self.canvas.create_oval(cx - 21, cy - 21, cx + 21, cy + 21,
                                    fill="#7C3AED", outline="", tags=tags)
            self.canvas.create_oval(cx - 7, cy - 10, cx + 7, cy + 4,
                                    fill="#FFFFFF", outline="", tags=tags)
            self.canvas.create_arc(cx - 13, cy - 1, cx + 13, cy + 24, start=0, extent=180,
                                   fill="#FFFFFF", outline="", tags=tags)

        # NAME ON ONE LINE, CLASS ON THE NEXT.
        #
        # It used to be "Welcome," above "Sahil - Class 6" on a single line, and
        # on this card that line has 204px to live in with an avatar in front of
        # it -- so it came out as "Sahil - Clas...", which is the one thing the
        # card exists to say. Splitting the two gives the name the whole width
        # and drops the greeting, which was never the information.
        text_x = WHO_X0 + 62
        self.profile_chip_text = self.canvas.create_text(
            text_x, TOP_Y0 + 25, text="Tap to set up",
            anchor="w", font=self._font(12, True), fill=COL_TEXT_DIM, tags=tags)
        self.profile_chip_class = self.canvas.create_text(
            text_x, TOP_Y0 + 45, text="", anchor="w",
            font=self._font(9), fill=COL_TEXT_DIM, tags=tags)

        # The artwork has its own decoration; these would land on top of it.
        if art is None:
            for x, y, s in ((WHO_X1 - 24, TOP_Y0 + 20, 7), (WHO_X1 - 40, TOP_Y0 + 58, 4)):
                self.canvas.create_polygon(
                    x, y - s, x + s * 0.34, y - s * 0.34, x + s, y,
                    x + s * 0.34, y + s * 0.34, x, y + s,
                    x - s * 0.34, y + s * 0.34, x - s, y,
                    x - s * 0.34, y - s * 0.34, fill="#FBBF24", outline="", tags=tags)

        self.canvas.tag_bind("profilechip", "<Button-1>",
                             lambda e: self.show_profile_picker())

    def refresh_profile_chip(self):
        profile = profiles.active_profile()
        room = WHO_X1 - (WHO_X0 + 62) - 10
        if profile:
            label = profile.get("name", "Student")
            klass = f"Class {profile.get('class')}"
            colour = COL_TEXT
        else:
            label, klass, colour = "Tap to set up", "", COL_TEXT_DIM
        self.canvas.itemconfig(self.profile_chip_text,
                               text=self._ellipsize(label, self._font(12, True), room),
                               fill=colour)
        self.canvas.itemconfig(self.profile_chip_class,
                               text=self._ellipsize(klass, self._font(9), room))
        if getattr(self, "profile_chip_face", None) is not None:
            face = profile_avatar(profile, 46) if profile else None
            if face is None:
                self.canvas.itemconfigure(self.profile_chip_face, state="hidden")
            else:
                # Tk keeps no reference of its own, and this one is replaced
                # every time Switch User is used, so it is held here rather than
                # in _photos -- which is never emptied.
                self._chip_face_photo = ImageTk.PhotoImage(face)
                self.canvas.itemconfigure(self.profile_chip_face,
                                          image=self._chip_face_photo,
                                          state="normal")

    # ---------- who is using the device ----------
    def _art_button(self, at, asset, label, command, text_colour, size):
        """A button whose face is artwork, with the wording drawn on it.

        These cards come blank -- unlike the action buttons, whose labels are
        painted in -- so the text goes on top. False when the file is missing,
        which is the caller's cue to draw the old one instead.
        """
        art = ui_asset(*asset)
        if art is None:
            return False
        x, y = at
        self._overlay_seq += 1
        tag = f"artbtn{self._overlay_seq}"
        self._place_overlay_asset(art, x, y, tag)
        self.canvas.create_text(x + art.width / 2, y + art.height / 2, text=label,
                                font=self._font(size, True), fill=text_colour,
                                tags=(self.OVERLAY_TAG, tag))
        self.canvas.tag_bind(tag, "<Button-1>", lambda e: command())
        return True

    def show_profile_picker(self):
        """Existing profiles plus a way to add one. The Switch User screen.

        Also the first screen on a device nobody has set up yet, where it has no
        profiles to list and so shows only Add a student.
        """
        people = profiles.list_profiles()
        self._overlay_screen("picker", "Who's learning today?",
                             "Tap your name, or add a new student.")
        active_id = (profiles.active_profile() or {}).get("user_id")
        card = ui_asset("Profile", "Profile_bg.png")
        chosen_card = ui_asset("Profile", "Select_bg.png")
        for index, profile in enumerate(people[:6]):
            col, row = index % 3, index // 3
            klass = profile.get("class")
            tint = "#FFF4E6" if klass == profiles.KG_CLASS else "#F3EEFF"
            accent = "#F59E0B" if klass == profiles.KG_CLASS else "#7C3AED"
            name = profile.get("name", "Student")
            if card is None:
                x0 = 40 + col * 246
                y0 = 110 + row * 96
                self._overlay_button(x0, y0, x0 + 226, y0 + 78,
                                     self._ellipsize(name, self._font(13, True), 200),
                                     lambda p=profile: self.choose_profile(p),
                                     fill=tint, text_colour=accent, size=13,
                                     sub=f"Class {klass}")
                continue
            # 219x76 to the artwork's own size, three across. Select_bg is the
            # same card in the "this is the one in use" colour.
            x0 = 53 + col * 237
            y0 = 120 + row * 96
            self._overlay_seq += 1
            tag = f"pick{self._overlay_seq}"
            art = chosen_card if (chosen_card is not None
                                  and profile.get("user_id") == active_id) else card
            self._place_overlay_asset(art, x0, y0, tag)
            face = profile_avatar(profile, 62)
            if face is not None:
                # The picture is how a child who cannot read finds their own
                # row, so it leads and the name follows it.
                self._place_overlay_asset(face, x0 + 7, y0 + 7, tag)
            text_x = x0 + (78 if face is not None else 16)
            self.canvas.create_text(
                text_x, y0 + 30,
                text=self._ellipsize(name, self._font(13, True),
                                     x0 + 210 - text_x),
                anchor="w", font=self._font(13, True), fill=accent,
                tags=(self.OVERLAY_TAG, tag))
            self.canvas.create_text(text_x, y0 + 51, text=f"Class {klass}",
                                    anchor="w", font=self._font(9),
                                    fill=COL_TEXT_DIM, tags=(self.OVERLAY_TAG, tag))
            self.canvas.tag_bind(tag, "<Button-1>",
                                 lambda e, p=profile: self.choose_profile(p))
        active = profiles.active_profile()
        if active:
            # Two buttons once somebody is set up: add a NEW child, or correct
            # the one already in use. Side by side rather than one centred, and
            # both still 56px tall, which is well past what a fingertip needs.
            if not self._art_button((219, 396), ("Profile", "Add new.png"),
                                    "+  Add a student", self.show_profile_setup,
                                    "#FFFFFF", 13):
                self._overlay_button(130, 396, 410, 452, "+  Add a student",
                                     self.show_profile_setup, fill=COL_INDIGO, size=13)
            if not self._art_button(
                    (443, 396), ("Profile", "Edit.png"), "Edit",
                    lambda: self.show_profile_setup(profiles.active_profile()),
                    COL_TEXT, 12):
                self._overlay_button(426, 396, 670, 452, "Edit this student",
                                     lambda: self.show_profile_setup(profiles.active_profile()),
                                     fill="#E6E9F5", text_colour=COL_TEXT, size=12,
                                     sub=self._ellipsize(
                                         f"{active.get('name', 'Student')} · "
                                         f"Class {active.get('class')}",
                                         self._font(9), 214))
        else:
            if not self._art_button((UI_W // 2 - 102, 396),
                                    ("Profile", "Add new.png"), "+  Add a student",
                                    self.show_profile_setup, "#FFFFFF", 13):
                self._overlay_button(UI_W / 2 - 150, 396, UI_W / 2 + 150, 452,
                                     "+  Add a student", self.show_profile_setup,
                                     fill=COL_INDIGO, size=13)
        if people:
            self._overlay_button(628, 20, 780, 56, "Close",
                                 self.dismiss_overlay, fill="#E6E9F5",
                                 text_colour=COL_TEXT, size=10)

    def choose_profile(self, profile):
        profiles.set_active_profile(profile["user_id"])
        print(f"[PROFILE] Active: {profile.get('name')} (Class {profile.get('class')})",
              flush=True)
        self.refresh_profile_chip()
        self.route_for_profile(profile)

    def dismiss_overlay(self):
        """Back to whichever screen the ACTIVE profile belongs on.

        This used to clear the overlay and set_kg_active(False) unconditionally,
        on the assumption it was only ever offered to a non-KG profile. The KG
        home offers Switch user, so a pre-reader could reach the picker and tap
        Close -- which dropped them into the open chat screen with the microphone
        live, the one place the KG routing exists to keep them out of. Routing on
        the active profile makes Close mean "back", not "leave KG".
        """
        self.route_for_profile(profiles.active_profile())

    # ---------- creating and editing a profile ----------
    def show_profile_setup(self, profile=None):
        """The name-and-class screen. With `profile`, edits it instead of
        creating a new one.

        The same screen for both on purpose: a child who was set up as Class 5
        when they meant Class 6 needs exactly the fields they were first asked
        for, and a second screen that showed the same three things would drift
        out of step with this one the first time either changed.
        """
        self.overlay = "setup"
        self._setup_editing = (profile or {}).get("user_id")
        self._setup_name = (profile or {}).get("name", "") or ""
        self._setup_class = (profile or {}).get("class")
        self._setup_board = (profile or {}).get("board")
        self._draw_setup()

    def _draw_setup(self):
        editing = getattr(self, "_setup_editing", None)
        self._overlay_screen(
            "setup",
            "Edit student" if editing else "New student",
            "Change the name or class, then save." if editing
            else "Type a name, then tap a class.",
            backdrop=("KG Activity", "Glass_BG.png"), backdrop_at=(19, 20))
        # The panel ends at 461, and this row used to run to 462.
        glass = ui_asset("KG Activity", "Glass_BG.png")
        row_y = 406 if glass is not None else 416

        # Name field
        self._round_rect(40, 84, 760, 124, 10, fill="#FFFFFF",
                         outline=COL_CARD_EDGE, tags=self.OVERLAY_TAG)
        shown = self._setup_name or "Name"
        self.canvas.create_text(54, 104, text=shown, anchor="w",
                                font=self._font(13, True),
                                fill=COL_TEXT if self._setup_name else COL_TEXT_FAINT,
                                tags=self.OVERLAY_TAG)

        # Keyboard. A-Z on three rows, plus space and backspace: a name is all
        # this ever has to type, so there are no digits and no symbols to hunt
        # through. Keys are 46px wide, which is comfortably past the ~40px a
        # fingertip needs on this panel.
        rows = ["ABCDEFGHIJ", "KLMNOPQRST", "UVWXYZ"]
        for r, row in enumerate(rows):
            width, gap = 68, 4
            total = len(row) * width + (len(row) - 1) * gap
            x = (UI_W - total) / 2 if r < 2 else 40
            y = 136 + r * 46
            for letter in row:
                self._overlay_button(x, y, x + width, y + 42, letter,
                                     lambda c=letter: self._setup_key(c),
                                     fill="#FFFFFF", text_colour=COL_TEXT,
                                     radius=8, size=12)
                x += width + gap
            if r == 2:
                self._overlay_button(x, y, x + 140, y + 42, "SPACE",
                                     lambda: self._setup_key(" "),
                                     fill="#FFFFFF", text_colour=COL_TEXT,
                                     radius=8, size=10)
                x += 144
                self._overlay_button(x, y, x + 140, y + 42, "DELETE",
                                     self._setup_backspace,
                                     fill="#E6E9F5", text_colour=COL_TEXT,
                                     radius=8, size=10)

        # Class picker: KG and 1-12, thirteen big targets on one row each half.
        # Darker over the panel, which is a picture rather than a flat tint.
        label_col = "#3A2E6E" if glass is not None else COL_TEXT_DIM
        self.canvas.create_text(40, 292, text="CLASS", anchor="w",
                                font=self._font(9, True), fill=label_col,
                                tags=self.OVERLAY_TAG)
        # Thirteen targets in the 40..540 strip, which is everything left of the
        # board column at 560. Seven per row at 66px wide clears the ~40px a
        # fingertip needs and still leaves the board its half of the screen.
        for index, value in enumerate(profiles.CLASS_VALUES):
            col, row = index % 7, index // 7
            x0 = 40 + col * 72
            y0 = 306 + row * 52
            chosen = self._setup_class == value
            self._overlay_button(x0, y0, x0 + 66, y0 + 46,
                                 "KG" if value == profiles.KG_CLASS else value,
                                 lambda v=value: self._setup_pick_class(v),
                                 fill=COL_INDIGO if chosen else "#FFFFFF",
                                 text_colour="#FFFFFF" if chosen else COL_TEXT,
                                 radius=10, size=13)

        # Board is explicitly optional, so it never blocks Save.
        self.canvas.create_text(556, 292, text="BOARD (OPTIONAL)", anchor="w",
                                font=self._font(9, True), fill=label_col,
                                tags=self.OVERLAY_TAG)
        for index, board in enumerate(profiles.BOARDS):
            # 556 and 104 wide, not 560 and 108: the second column used to end
            # at 784 and the panel behind it ends at 781.
            x0, y0 = 556 + (index % 2) * 112, 306 + (index // 2) * 52
            chosen = self._setup_board == board
            self._overlay_button(x0, y0, x0 + 104, y0 + 46, board,
                                 lambda b=board: self._setup_pick_board(b),
                                 fill="#14B8A6" if chosen else "#FFFFFF",
                                 text_colour="#FFFFFF" if chosen else COL_TEXT,
                                 radius=10, size=10)

        ready = bool(self._setup_class)
        save_label = "Save changes" if editing else "Save and start"
        # The artwork card only when Save is actually available: it is a solid
        # navy button and there is no greyed version of it, and a button that
        # looks live but does nothing is worse than one that looks dead.
        if not (ready and self._art_button((40, row_y), ("Profile", "Add new.png"),
                                           save_label, self._setup_save,
                                           "#FFFFFF", 13)):
            self._overlay_button(40, row_y, 300, row_y + 46, save_label,
                                 self._setup_save if ready else None,
                                 fill=COL_INDIGO if ready else "#C7CEF0", size=13)
        if profiles.list_profiles():
            if not self._art_button((262, row_y), ("Profile", "Edit.png"), "Back",
                                    self.show_profile_picker, COL_TEXT, 11):
                self._overlay_button(316, row_y, 500, row_y + 46, "Back",
                                     self.show_profile_picker, fill="#E6E9F5",
                                     text_colour=COL_TEXT, size=11)
        if editing:
            # Only when editing, and set well away from Save: the two buttons do
            # opposite things and one of them cannot be undone. Left drawn on
            # purpose -- there is no red card in the folder, and this one should
            # not look like the others.
            self._overlay_button(600, row_y, 760, row_y + 46, "Delete",
                                 lambda: self.confirm_delete_profile(editing),
                                 fill="#FEE2E2", text_colour="#B91C1C", size=11)

    def confirm_delete_profile(self, user_id):
        """Ask before deleting. This removes their conversation as well.

        A separate screen rather than a second tap on the same button, because
        the thing being destroyed is everything the device knows about a child
        and the two taps would sit in the same place.
        """
        profile = next((p for p in profiles.list_profiles()
                        if p.get("user_id") == user_id), None)
        if profile is None:
            self.show_profile_picker()
            return
        name = profile.get("name", "this student")
        self._overlay_screen("confirm_delete", f"Delete {name}?",
                             "This cannot be undone.", tint="#FFF5F5")
        self.canvas.create_text(
            UI_W / 2, 150,
            text=f"{name} (Class {profile.get('class')}) will be removed,\n"
                 f"along with everything they have asked and learned.",
            justify="center", font=self._font(12), fill=COL_TEXT,
            tags=self.OVERLAY_TAG)
        self._overlay_button(120, 250, 380, 310, "Keep",
                             lambda: self.show_profile_setup(profile),
                             fill="#E6E9F5", text_colour=COL_TEXT, size=13)
        self._overlay_button(420, 250, 680, 310, f"Delete {name}",
                             lambda: self.delete_profile_now(user_id),
                             fill="#DC2626", size=13)

    def delete_profile_now(self, user_id):
        profile = next((p for p in profiles.list_profiles()
                        if p.get("user_id") == user_id), None)
        name = (profile or {}).get("name", user_id)
        profiles.delete_profile(user_id)
        print(f"[PROFILE] Deleted {name} and their history.", flush=True)
        self.refresh_profile_chip()
        remaining = profiles.list_profiles()
        if not remaining:
            # Nobody left: the device is back to its first-run state, so it asks
            # who is using it rather than dropping into a nameless session.
            kg.set_kg_active(False)
            self.show_profile_setup()
            return
        # delete_profile has already moved the active pointer to a survivor;
        # route on whoever that now is so a KG child lands on the KG screens.
        self.show_profile_picker()

    def _setup_key(self, char):
        if len(self._setup_name) < 18:
            self._setup_name += char
            self._draw_setup()

    def _setup_backspace(self):
        self._setup_name = self._setup_name[:-1]
        self._draw_setup()

    def _setup_pick_class(self, value):
        self._setup_class = value
        self._draw_setup()

    def _setup_pick_board(self, board):
        # Tapping the chosen board again clears it -- it is optional, so there
        # has to be a way back out of having picked one.
        self._setup_board = None if self._setup_board == board else board
        self._draw_setup()

    def _setup_save(self):
        editing = getattr(self, "_setup_editing", None)
        if editing:
            profile = profiles.update_profile(editing,
                                              name=self._setup_name.title().strip(),
                                              class_value=self._setup_class,
                                              board=self._setup_board)
            if profile is None:
                # The profile was deleted from under this screen. Nothing to
                # write back to, so fall back to the picker rather than saving
                # a ghost or crashing on None.
                print("[PROFILE] The profile being edited no longer exists.", flush=True)
                self.show_profile_picker()
                return
            # Editing implies using: the class that was just corrected has to be
            # the one the next answer is pitched at, and update_profile does not
            # change which profile is active.
            profiles.set_active_profile(profile["user_id"])
            print(f"[PROFILE] Updated {profile['name']} (Class {profile['class']}).",
                  flush=True)
        else:
            profile = profiles.create_profile(self._setup_name.title().strip(),
                                              self._setup_class,
                                              board=self._setup_board)
            print(f"[PROFILE] Created {profile['name']} (Class {profile['class']}).",
                  flush=True)
        self.refresh_profile_chip()
        # Routed either way, so changing a class INTO or OUT OF KG moves the
        # child to the right screen immediately -- editing Class 5 to KG has to
        # land on the spelling and story picker, not leave them in open chat.
        self.route_for_profile(profile)

    # ---------- the single routing decision ----------
    def route_for_profile(self, profile):
        """KG goes to the spelling and story screens; everyone else to the
        normal flow. The one place that decision is made."""
        if profile and profiles.is_kindergarten(profile.get("class")):
            kg.set_kg_active(True)
            self.show_kg_home(greet=True)
        else:
            kg.set_kg_active(False)
            self._clear_overlay()

    # ---------- Kindergarten ----------
    def show_kg_home(self, greet=False):
        kg.set_kg_active(True)
        profile = profiles.active_profile() or {}
        name = profile.get("name", "")
        self._overlay_screen("kg_home", f"Hello {name}!" if name else "Hello!",
                             "What would you like to do?", tint="#FFF9F0",
                             backdrop=("KG Activity", "Glass_BG.png"),
                             backdrop_at=(19, 20))
        # Six activities in a 3x2 grid rather than two big cards. Each tile is
        # 236x142, which is far past what a fingertip needs, and each one leads
        # with a BIG GLYPH the child can recognise -- the words underneath are
        # for whoever is sitting with them, because the child this is built for
        # cannot read "Hindi alphabet".
        tiles = [
            ("Spell a Word",  "Say it, then write it", "#7C3AED", "ABC",
             self.show_kg_spelling, "Learn to Epell.png"),
            ("A B C",         "English letters",        "#2563EB", "Aa",
             lambda: self.show_kg_alphabet("en"), "Eng Alp.png"),
            ("क ख ग",         "हिंदी अक्षर",             "#DB2777", "अ",
             lambda: self.show_kg_alphabet("hi"), "Hindi Alp.png"),
            ("1 2 3",         "Counting",               "#059669", "12",
             self.show_kg_counting, "Numbers.png"),
            ("A to Z",        "Put them in order",      "#D97706", "A?",
             self.show_kg_order, "Order.png"),
            ("Story Time",    "Sit back and listen",    "#F59E0B", "book",
             self.show_kg_story_picker, "Story.png"),
        ]
        # Laid down by _overlay_screen above, before the greeting, so the
        # wording sits on the panel rather than under it.
        glass = ui_asset("KG Activity", "Glass_BG.png")
        for index, (label, sub, colour, glyph, command, art_name) in enumerate(tiles):
            col, row = index % 3, index // 3
            art = ui_asset("KG Activity", art_name)
            if art is not None:
                # 229x148 each, and each carries its own wording and picture, so
                # nothing is written over them. Spaced to the artwork's size
                # rather than the drawn tile's.
                x0, y0 = 30 + col * 250, 92 + row * 156
                self._overlay_seq += 1
                tag = f"kgtile{self._overlay_seq}"
                self._place_overlay_asset(art, x0, y0, tag)
                self.canvas.tag_bind(tag, "<Button-1>", lambda e, c=command: c())
                continue
            x0 = 28 + col * 252
            y0 = 96 + row * 158
            tag = self._overlay_button(x0, y0, x0 + 236, y0 + 142, label, command,
                                       fill=colour, size=15, sub=sub,
                                       label_frac=0.62, sub_frac=0.82)
            self._kg_tile_glyph(glyph, x0 + 118, y0 + 42, tag)

        # Test sits apart from the six learning tiles, wide and on its own row,
        # because it is a different kind of thing: the tiles teach, this one
        # asks. Mixing it into the grid would have made it look like a seventh
        # activity to wander into.
        # Kept inside the panel: it ends at 461, and the row used to run to 464.
        row_y = 406 if glass is not None else 412
        self._overlay_button(30, row_y, 560, row_y + 50, "Test yourself",
                             self.show_kg_test_picker, fill="#0EA5E9", size=15,
                             sub="See what you have learned",
                             label_frac=0.42, sub_frac=0.74)
        self._overlay_button(580, row_y, 770, row_y + 50, "Switch user",
                             self.show_profile_picker, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)
        if greet and name:
            kg.kg_say(f"Hello {name}! What would you like to do today?", "warm")

    def _kg_tile_glyph(self, kind, cx, cy, tag):
        """The picture on a home tile. Drawn under the label, same tag, so
        tapping the picture is tapping the button."""
        tags = (self.OVERLAY_TAG, tag)
        if kind == "book":
            self._kg_book_glyph(cx, cy + 4, tag)
            return
        if kind == "ABC":
            self._kg_blocks_glyph(cx, cy + 4, tag)
            return
        self.canvas.create_text(cx, cy, text=kind, font=self._font(26, True),
                                fill="#FFFFFF", tags=tags)

    # ----- alphabets -----
    def show_kg_alphabet(self, language="en", index=0):
        """One letter at a time, said aloud with its example word.

        Hindi and English share this screen because the activity is identical --
        only the bank and the voice change, and the voice picks itself from the
        script (see detect_tts_language). Two screens would have been two places
        to fix the next layout bug.
        """
        bank = (kg_content.HINDI_ALPHABET if language == "hi"
                else kg_content.ENGLISH_ALPHABET)
        self._kg_alpha_lang = language
        self._kg_alpha_index = max(0, min(index, len(bank) - 1))
        entry = bank[self._kg_alpha_index]
        letter, word = entry[0], entry[1]
        if language == "hi":
            reading, picture = entry[2], entry[3]
        else:
            reading, picture = "", entry[2]

        title = "हिंदी अक्षर" if language == "hi" else "A B C"
        self._overlay_screen("kg_alpha", title,
                             f"{self._kg_alpha_index + 1} of {len(bank)}",
                             tint="#EFF6FF" if language == "en" else "#FDF2F8")
        accent = "#2563EB" if language == "en" else "#DB2777"

        # With a picture the letter moves left and they sit side by side, which
        # is how a wall chart does it. Without one the letter takes the middle,
        # so a letter whose traditional word has no emoji still looks deliberate
        # rather than like something failed to load.
        self._kg_keep_mascot()
        self._kg_ask_button()
        # 120 rather than 132, and +/-100 rather than +/-140: the letter and its
        # picture now share a 390px column instead of half of an 800px screen.
        has_picture = picture_image(picture, 120) is not None if picture else False
        letter_x = self.KG_CX - 100 if has_picture else self.KG_CX
        self.canvas.create_text(letter_x, 186, text=letter,
                                font=self._font(76, True), fill=accent,
                                tags=self.OVERLAY_TAG)
        if reading:
            # The Latin reading is for the adult sitting alongside, not the child.
            self.canvas.create_text(letter_x, 244, text=f"({reading})",
                                    font=self._font(11), fill=COL_TEXT_DIM,
                                    tags=self.OVERLAY_TAG)
        if has_picture:
            self._place_emoji(picture, self.KG_CX + 100, 186, 120)
        self.canvas.create_text(self.KG_CX, 306, text=word,
                                font=self._font(22, True), fill=COL_TEXT,
                                tags=self.OVERLAY_TAG)

        self._overlay_button(20, 396, 170, 452, "Back",
                             self.show_kg_home, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)
        if self._kg_alpha_index > 0:
            self._overlay_button(200, 396, 360, 452, "Previous",
                                 lambda: self.show_kg_alphabet(
                                     language, self._kg_alpha_index - 1),
                                 fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(390, 396, 560, 452, "Say it again",
                             lambda: self._kg_say_letter(letter, word, language),
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        if self._kg_alpha_index < len(bank) - 1:
            self._overlay_button(590, 396, 780, 452, "Next", lambda:
                                 self.show_kg_alphabet(language,
                                                       self._kg_alpha_index + 1),
                                 fill=accent, size=13)
        else:
            self._overlay_button(590, 396, 780, 452, "Start again",
                                 lambda: self.show_kg_alphabet(language, 0),
                                 fill=accent, size=12)
        self._kg_say_letter(letter, word, language)

    def _kg_say_letter(self, letter, word, language):
        if language == "hi":
            kg.kg_say_many([(f"{letter}", "curious"), (f"{letter} से {word}।", "warm")])
        else:
            kg.kg_say_many([(f"{letter}.", "curious"),
                         (f"{letter} for {word}.", "warm")])

    # ----- counting -----
    def show_kg_counting(self, value=1, grew=False):
        """Count up one number at a time, showing where the number COMES FROM.

        A numeral and its name is a label, not an explanation -- and reading a
        number aloud in two languages is two labels rather than an idea. What a
        child this age is actually learning is that each number is the one
        before it plus one more, so the screen and the voice now say exactly
        that: there were two apples, here is one more apple, two and one more
        makes three. The apple that was just added is drawn ringed, so the "one
        more" is a thing they can point at rather than a word she said.

        She asks before going past each block of twenty rather than marching to
        fifty, because a KG child finishing at twenty has finished something.
        """
        self._kg_count = max(1, min(value, kg_content.COUNT_MAX))
        n = self._kg_count
        # A jump -- Previous, Start again, answering a milestone -- has no "one
        # more" to show, so it is never dressed up as one.
        grew = bool(grew) and n > 1
        self._kg_count_grew = grew
        english = kg_content.number_name(n, "en")

        self._overlay_screen("kg_count", "Counting",
                             f"{n} of {kg_content.COUNT_MAX}", tint="#ECFDF5")
        self._kg_keep_mascot()
        self._kg_ask_button()
        self.canvas.create_text(self.KG_CX, 138, text=str(n),
                                font=self._font(70, True), fill="#059669",
                                tags=self.OVERLAY_TAG)
        self.canvas.create_text(self.KG_CX, 198, text=english,
                                font=self._font(20, True), fill=COL_TEXT,
                                tags=self.OVERLAY_TAG)
        # The sum in the same words she speaks, so the child being read to and
        # the child starting to read see one sentence rather than two.
        if grew:
            self.canvas.create_text(
                self.KG_CX, 236, text=f"{n - 1}   and 1 more   makes   {n}",
                font=self._font(15, True), fill="#047857", tags=self.OVERLAY_TAG)

        # Something to actually count. Apples rather than dots: a child counts
        # things, and "five apples" is a sentence they can check against the
        # numeral. Two rows past ten so twenty still fits across 800px.
        if n <= 20:
            # Five or seven to a row, not ten. The apples share their column
            # with the mascot standing beside them now rather than having the
            # whole 800px, and a row of ten at the old size ran 392px wide -- two
            # pixels past the column on its own. Three rows is the most that
            # still clears the button strip at 396.
            per_row = 5 if n <= 10 else 7
            size = 34 if n <= 10 else 28
            gap = 8 if n <= 10 else 6
            rows = [list(range(min(per_row, n - r * per_row)))
                    for r in range((n + per_row - 1) // per_row)]
            top = 302 if len(rows) == 1 else 290 - (len(rows) - 2) * 16
            index = 0
            for row_index, row in enumerate(rows):
                total = len(row) * size + (len(row) - 1) * gap
                x = self.KG_CX - total / 2 + size / 2
                y = top + row_index * (size + 6)
                for _ in row:
                    index += 1
                    if grew and index == n:
                        # The one that was just added, ringed -- "one more" has
                        # to be visible and not only spoken.
                        half = size / 2 + 5
                        self.canvas.create_oval(x - half, y - half, x + half,
                                                y + half, outline="#F59E0B",
                                                width=3, tags=self.OVERLAY_TAG)
                    self._place_emoji(kg_content.COUNT_EMOJI, x, y, size)
                    x += size + gap

        self._overlay_button(20, 396, 170, 452, "Back", self.show_kg_home,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        if n > 1:
            self._overlay_button(200, 396, 360, 452, "Previous",
                                 lambda: self.show_kg_counting(n - 1),
                                 fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(390, 396, 560, 452, "Say it again",
                             lambda: self._kg_say_number(n, grew),
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        if n >= kg_content.COUNT_MAX:
            self._overlay_button(590, 396, 780, 452, "Start again",
                                 lambda: self.show_kg_counting(1),
                                 fill="#059669", size=12)
        else:
            self._overlay_button(590, 396, 780, 452, "Next",
                                 lambda: self._kg_count_next(n),
                                 fill="#059669", size=13)
        self._kg_say_number(n, grew)

    def _kg_say_number(self, n, grew=None):
        """Say the number as a story about the one before it.

        English only. The Hindi name used to be read straight after the English
        one and it taught nothing the English name had not: a second label for
        the same picture, with no reason given for either. Hindi belongs in the
        STORIES, where it carries meaning, rather than stapled to every numeral.
        """
        if grew is None:
            grew = getattr(self, "_kg_count_grew", False)
        english = kg_content.number_name(n, "en").lower()

        if n == 1:
            kg.kg_say_many([("One.", "excited"),
                         ("Here is one apple.", "warm"),
                         ("Just one!", "curious")])
            return

        if not grew:
            lines = [(f"{english.capitalize()}.", "excited")]
            if n <= 20:
                lines.append((f"There are {english} apples.", "warm"))
            kg.kg_say_many(lines)
            return

        previous = kg_content.number_name(n - 1, "en").lower()
        was = "was" if n - 1 == 1 else "were"
        thing = "apple" if n - 1 == 1 else "apples"
        if n <= 20:
            kg.kg_say_many([
                (f"There {was} {previous} {thing}.", "curious"),
                ("Now we add one more apple.", "encouraging"),
                (f"{previous.capitalize()}, and one more, makes {english}.",
                 "storyteller"),
                (f"So now there are {english} apples!", "excited"),
            ])
        else:
            kg.kg_say_many([
                (f"We had {previous}.", "curious"),
                ("And one more.", "encouraging"),
                (f"{previous.capitalize()}, and one more, makes {english}.",
                 "excited"),
            ])

    def _kg_count_next(self, n):
        """Ask before starting each new block of twenty."""
        if n % kg_content.COUNT_BLOCK == 0:
            self._kg_count_milestone(n)
            return
        self.show_kg_counting(n + 1, grew=True)

    def _kg_count_milestone(self, n):
        self._overlay_screen("kg_count_more", f"You counted to {n}!",
                             "Shall we keep going?", tint="#ECFDF5")
        self.canvas.create_text(UI_W / 2, 170, text="\u2b50",
                                font=self._font(58, True), fill="#F59E0B",
                                tags=self.OVERLAY_TAG)
        self.canvas.create_text(
            UI_W / 2, 240,
            text=f"That is all the way to {kg_content.number_name(n, 'en').lower()}.",
            font=self._font(14), fill=COL_TEXT, tags=self.OVERLAY_TAG)
        self._overlay_button(140, 300, 380, 360, "Yes, keep going!",
                             lambda: self.show_kg_counting(n + 1, grew=True),
                             fill="#059669", size=14)
        self._overlay_button(420, 300, 660, 360, "That's enough",
                             self.show_kg_home, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=13)
        kg.kg_say_many([(f"Wow! You counted all the way to {n}!", "excited"),
                     ("Shall we keep going?", "curious")])

    # ----- put the letters in A-Z order -----
    # Five letters, drawn from a window of the alphabet rather than at random
    # across it: ordering C D E F G teaches the sequence, while ordering B K Q
    # only tests whether they already know it.
    KG_ORDER_COUNT = 5

    def show_kg_order(self):
        letters = [entry[0] for entry in kg_content.ENGLISH_ALPHABET]
        start = random.randint(0, len(letters) - self.KG_ORDER_COUNT)
        self._kg_order_target = letters[start:start + self.KG_ORDER_COUNT]
        pool = list(self._kg_order_target)
        # Shuffle until it is not already in order, or the puzzle is not one.
        for _ in range(10):
            random.shuffle(pool)
            if pool != self._kg_order_target:
                break
        self._kg_order_pool = pool
        self._kg_order_picked = []
        self._kg_order_done = False
        self._draw_kg_order()
        kg.kg_say_many([("Put the letters in order!", "encouraging"),
                     ("Tap them from A to Z.", "curious")])

    def _draw_kg_order(self):
        self._overlay_screen("kg_order", "A to Z",
                             "Tap the letters in the right order",
                             tint="#FFFBEB")
        # What they have chosen so far, left to right.
        slot, gap = 84, 12
        total = len(self._kg_order_target) * slot + (len(self._kg_order_target) - 1) * gap
        x = (UI_W - total) / 2
        for index in range(len(self._kg_order_target)):
            picked = (self._kg_order_picked[index]
                      if index < len(self._kg_order_picked) else "")
            self._round_rect(x, 100, x + slot, 100 + 76, 12, fill="#FFFFFF",
                             outline="#F5D9A0", tags=self.OVERLAY_TAG)
            self.canvas.create_text(x + slot / 2, 138, text=picked,
                                    font=self._font(30, True), fill="#D97706",
                                    tags=self.OVERLAY_TAG)
            x += slot + gap

        if self._kg_order_done:
            self.canvas.create_text(UI_W / 2, 214, text="Perfect! A to Z!",
                                    font=self._font(17, True), fill="#059669",
                                    tags=self.OVERLAY_TAG)
        else:
            # The letters still to place.
            remaining = [c for c in self._kg_order_pool
                         if c not in self._kg_order_picked]
            total = len(remaining) * slot + max(0, len(remaining) - 1) * gap
            x = (UI_W - total) / 2
            for letter in remaining:
                self._overlay_button(x, 226, x + slot, 226 + 76, letter,
                                     lambda c=letter: self._kg_order_tap(c),
                                     fill="#FFFFFF", text_colour="#92400E",
                                     radius=12, size=26)
                x += slot + gap

        self._overlay_button(20, 396, 170, 452, "Back", self.show_kg_home,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(320, 396, 480, 452, "Start over",
                             self._kg_order_reset, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)
        self._overlay_button(610, 396, 780, 452, "New letters",
                             self.show_kg_order, fill="#D97706", size=12)

    def _kg_order_reset(self):
        self._kg_order_picked = []
        self._kg_order_done = False
        self._draw_kg_order()

    def _kg_order_tap(self, letter):
        expected = self._kg_order_target[len(self._kg_order_picked)]
        if letter != expected:
            # Wrong one. Say which letter actually comes next rather than only
            # that this one is wrong -- "not that one" tells a child nothing
            # about the alphabet.
            kg.kg_say_many([("Not that one.", "gentle"),
                         (f"After {self._kg_order_picked[-1]}, comes {expected}."
                          if self._kg_order_picked
                          else f"{expected} comes first.", "curious")])
            return
        self._kg_order_picked.append(letter)
        if len(self._kg_order_picked) < len(self._kg_order_target):
            self._draw_kg_order()
            kg.kg_say(letter, "curious")
            return
        self._kg_order_done = True
        self._draw_kg_order()
        kg.kg_say_many([(random.choice(kg_content.PRAISE), "proud"),
                     (" ".join(self._kg_order_target) + ".", "excited")])
        self.root.after(4500, self._kg_order_next_if_still_here)

    def _kg_order_next_if_still_here(self):
        if self.overlay == "kg_order":
            self.show_kg_order()

    # ----- which language should the story be in? -----
    def show_kg_story_picker(self):
        self._overlay_screen("kg_story_lang", "Story Time",
                             "Which language would you like?", tint="#FFF6E8")
        english = self._overlay_button(60, 130, 380, 340, "English",
                                       lambda: self.show_kg_story("en"),
                                       fill="#F59E0B", size=22,
                                       sub="A story in English",
                                       label_frac=0.56, sub_frac=0.74)
        self._kg_book_glyph(220, 196, english)
        hindi = self._overlay_button(420, 130, 740, 340, "हिंदी",
                                     lambda: self.show_kg_story("hi"),
                                     fill="#DB2777", size=24,
                                     sub="हिंदी में कहानी",
                                     label_frac=0.56, sub_frac=0.74)
        self._kg_book_glyph(580, 196, hindi)
        self._overlay_button(300, 396, 500, 448, "Back", self.show_kg_home,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        kg.kg_say_many([("Would you like a story in English,", "curious"),
                     ("या हिंदी में?", "curious")])

    # ----- tests -----
    # A test is five questions, each in two parts: NAME the picture, then spell
    # it (English) or say its first letter (Hindi). Counting asks how many, then
    # for the same number in the other language.
    #
    # Both parts are answered by voice through the same ai_loop bridge the
    # spelling screen uses, so there is still only one audio stack. Every screen
    # also carries a tap-through, because a test a child cannot leave when the
    # room is too loud is a trap rather than a test.
    KG_TEST_QUESTIONS = 5
    KG_TEST_LISTEN_S = 8.0
    # Naming a picture is one word, but a five-year-old reaches for it -- "it's
    # a... a... kite" -- so even the quick answers get more room than the
    # conversational pause. Spelling and counting get far more; see
    # KG_SPELL_* and KG_COUNT_* for why.
    KG_TEST_END_SILENCE_S = 1.4

    def _kg_listen_budget(self, kind, spelling):
        """(wait for them to start, how long they may take, pause that ends it)."""
        if kind == "count":
            return (KG_COUNT_START_TIMEOUT_S, KG_COUNT_PHRASE_LIMIT_S,
                    KG_COUNT_END_SILENCE_S)
        if spelling and kind == "en":
            return (KG_SPELL_START_TIMEOUT_S, KG_SPELL_PHRASE_LIMIT_S,
                    KG_SPELL_END_SILENCE_S)
        return (self.KG_TEST_LISTEN_S, self.KG_TEST_LISTEN_S,
                self.KG_TEST_END_SILENCE_S)

    def show_kg_test_picker(self):
        self._overlay_screen("kg_test_pick", "Test yourself",
                             "What would you like to be tested on?",
                             tint="#EFF6FF")
        options = [
            ("A B C", "English letters", "#2563EB", "en"),
            ("क ख ग", "हिंदी अक्षर", "#DB2777", "hi"),
            ("1 2 3", "Counting", "#059669", "count"),
        ]
        for index, (label, sub, colour, kind) in enumerate(options):
            x0 = 40 + index * 250
            tag = self._overlay_button(x0, 130, x0 + 230, 330, label,
                                       lambda k=kind: self.start_kg_test(k),
                                       fill=colour, size=22, sub=sub,
                                       label_frac=0.58, sub_frac=0.78)
            self._kg_tile_glyph(label.split()[0], x0 + 115, 176, tag)
        self._overlay_button(300, 396, 500, 452, "Back", self.show_kg_home,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        kg.kg_say("What would you like to be tested on?", "curious")

    def start_kg_test(self, kind):
        kg.kg_cancel_listen()
        self._kg_test_kind = kind
        self._kg_test_qs = kg_content.test_questions(kind, self.KG_TEST_QUESTIONS)
        self._kg_test_at = 0
        self._kg_test_score = 0
        self._kg_test_round = getattr(self, "_kg_test_round", 0) + 1
        self._kg_ask_test_question()

    def _kg_test_stale(self, token):
        return (self.overlay != "kg_test"
                or token != getattr(self, "_kg_test_round", 0))

    def _kg_ask_test_question(self):
        if self._kg_test_at >= len(self._kg_test_qs):
            self._kg_test_finish()
            return
        self._kg_test_stage = "name"
        self._kg_test_heard = ""
        self._kg_test_note = ""
        self._kg_test_listening = False
        self._draw_kg_test()
        question = self._kg_test_qs[self._kg_test_at]
        if question["kind"] == "count":
            # Saying "count them out loud" is not decoration: it tells the child
            # that counting aloud IS the answer, and it tells the microphone --
            # which now waits out the gaps between numbers -- what to expect.
            kg.kg_say_many([("Count the apples out loud.", "encouraging"),
                         ("How many are there?", "curious")])
        elif question["kind"] == "hi":
            kg.kg_say_many([("यह क्या है?", "curious")])
        else:
            kg.kg_say_many([("What is this?", "curious")])
        self._kg_after_speaking(
            lambda r=self._kg_test_round: self._kg_test_listen(r))

    def _draw_kg_test(self):
        question = self._kg_test_question()
        if question is None:
            return
        self._overlay_screen(
            "kg_test", f"Question {self._kg_test_at + 1} of {len(self._kg_test_qs)}",
            "Listening..." if self._kg_test_listening else " ", tint="#F0F9FF")

        if question["kind"] == "count":
            # The thing being counted IS the question, so it is drawn large.
            # In the second half the apple that changes is SHOWN changing --
            # ringed in gold when it is put on the table, crossed out in red when
            # it is taken off -- because "one more" and "one less" are questions
            # a five-year-old answers by looking, not by doing arithmetic in
            # their head. The apple being taken away stays on the screen with a
            # line through it rather than vanishing: a child cannot count what is
            # no longer there, and seeing WHICH one went is the whole lesson.
            second_half = self._kg_test_stage == "spell"
            step = question.get("step", 1)
            value = question["value"]
            # How many are drawn: adding puts one more out, taking away leaves
            # the same apples on the table with one of them struck through.
            n = value + 1 if (second_half and step > 0) else value
            marked = n if second_half else 0
            per_row = 10
            size = 44 if n <= 10 else 34
            gap = 8
            rows = [min(per_row, n - r * per_row)
                    for r in range((n + per_row - 1) // per_row)]
            top = 150 if len(rows) == 1 else 132
            index = 0
            for row_index, count in enumerate(rows):
                total = count * size + (count - 1) * gap
                x = (UI_W - total) / 2 + size / 2
                y = top + row_index * (size + 8)
                for _ in range(count):
                    index += 1
                    half = size / 2 + 5
                    if index == marked and step > 0:
                        self.canvas.create_oval(x - half, y - half, x + half,
                                                y + half, outline="#F59E0B",
                                                width=3, tags=self.OVERLAY_TAG)
                    self._place_emoji(kg_content.COUNT_EMOJI, x, y, size)
                    if index == marked and step < 0:
                        # Drawn OVER the apple, so it reads as struck out rather
                        # than as one more thing on the table to be counted.
                        self.canvas.create_oval(x - half, y - half, x + half,
                                                y + half, outline="#E11D48",
                                                width=3, tags=self.OVERLAY_TAG)
                        offset = half * 0.72
                        self.canvas.create_line(x - offset, y - offset,
                                                x + offset, y + offset,
                                                fill="#E11D48", width=4,
                                                capstyle="round",
                                                tags=self.OVERLAY_TAG)
                    x += size + gap
        else:
            self._place_emoji(question["picture"], UI_W / 2, 168, 150)

        prompt = {"name": {"count": "Count them out loud.  How many?",
                           "hi": "यह क्या है?"}.get(
                      question["kind"], "What is this?"),
                  "spell": {"count": self._kg_step_prompt(question.get("step", 1)),
                            "hi": "किस अक्षर से शुरू होता है?"}.get(
                      question["kind"], "Now spell it")}[self._kg_test_stage]
        self.canvas.create_text(UI_W / 2, 268, text=prompt,
                                font=self._font(17, True), fill=COL_TEXT,
                                tags=self.OVERLAY_TAG)
        if self._kg_test_listening:
            self._round_rect(UI_W / 2 - 130, 292, UI_W / 2 + 130, 336, 22,
                             fill="#E0F2FE", outline="#0EA5E9",
                             tags=self.OVERLAY_TAG)
            self.canvas.create_text(UI_W / 2, 314, text="I'm listening...",
                                    font=self._font(13, True), fill="#075985",
                                    tags=self.OVERLAY_TAG)
        elif self._kg_test_note:
            self.canvas.create_text(UI_W / 2, 314, text=self._kg_test_note,
                                    font=self._font(14, True), fill=COL_TEXT,
                                    tags=self.OVERLAY_TAG)

        self.canvas.create_text(60, 40, text=f"Score {self._kg_test_score}",
                                font=self._font(11, True), fill="#0EA5E9",
                                tags=self.OVERLAY_TAG)
        self._overlay_button(20, 400, 170, 452, "Stop", self.show_kg_home,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(330, 400, 470, 452, "Say again",
                             self._kg_test_repeat, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)
        self._overlay_button(620, 400, 780, 452, "Skip",
                             self._kg_test_skip, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)

    @staticmethod
    def _kg_step_prompt(step, spoken=False):
        """What the second half of a counting question asks, in words.

        One place, because the screen, the spoken line and the Say again button
        all have to agree about which way round this question goes -- and a child
        told "one more" while looking at a crossed-out apple learns nothing but
        that the device is unreliable.
        """
        if step < 0:
            return ("Now, if we take one apple away, how many will be left?"
                    if spoken else "Take one away!  How many now?")
        return ("Now, if we add one more apple, how many will there be?"
                if spoken else "And one more!  How many now?")

    @staticmethod
    def _kg_praise_for(kind):
        """Praise that fits the question. "You got every letter" is the right
        thing to say about a spelling and the wrong thing about sixteen apples."""
        return kg_content.COUNT_PRAISE if kind == "count" else kg_content.PRAISE

    def _kg_test_repeat(self):
        question = self._kg_test_question()
        if question is None:
            return
        if self._kg_test_stage == "name":
            text = {"count": "Count the apples out loud. How many are there?",
                    "hi": "यह क्या है?"}.get(question["kind"], "What is this?")
        else:
            text = {"count": self._kg_step_prompt(question.get("step", 1),
                                                 spoken=True),
                    "hi": "यह किस अक्षर से शुरू होता है?"}.get(
                        question["kind"], "Now spell it.")
        kg.kg_say(text, "curious")
        self._kg_after_speaking(
            lambda r=self._kg_test_round: self._kg_test_listen(r))

    def _kg_test_skip(self):
        kg.kg_cancel_listen()
        self._kg_test_at += 1
        self._kg_ask_test_question()

    def _kg_test_question(self):
        """The question on display, clamped. Belt and braces for the redraw gap
        above: a drawing routine must never be the thing that raises."""
        if not self._kg_test_qs:
            return None
        return self._kg_test_qs[min(self._kg_test_at, len(self._kg_test_qs) - 1)]

    def _kg_test_listen(self, token):
        if self._kg_test_stale(token):
            return
        self._kg_test_listening = True
        self._draw_kg_test()
        question = self._kg_test_question()
        kind = question["kind"] if question else "en"
        spelling = self._kg_test_stage == "spell"
        if kind == "hi":
            # Hindi answers were being forced through language="en", which turns
            # कबूतर into nonsense before it is ever compared.
            seed, language = "", "hi"
        else:
            seed, language = (kg.KG_SEED_LETTERS if spelling and kind == "en"
                              else ""), "en"
        start_s, phrase_limit, end_silence = self._kg_listen_budget(kind, spelling)
        # One generation per listen. "Say again" can be tapped while a listen is
        # already polling, and two poll loops on one queue means whichever loses
        # the result goes on rescheduling itself for as long as the screen is up.
        self._kg_test_gen = getattr(self, "_kg_test_gen", 0) + 1
        self._kg_begin_listen(start_s, seed=seed, language=language,
                              phrase_limit=phrase_limit, end_silence=end_silence)
        self._kg_test_poll(token, self._kg_test_gen)

    def _kg_test_poll(self, token, generation):
        if (self._kg_test_stale(token)
                or generation != getattr(self, "_kg_test_gen", 0)):
            return
        heard = self._kg_take_listen_result()
        if heard is None:
            self.root.after(150, lambda r=token, g=generation:
                            self._kg_test_poll(r, g))
            return
        self._kg_test_listening = False
        self._kg_test_judge(heard)

    def _kg_test_judge(self, heard):
        """Mark one answer, say why, and move on.

        A point per half -- naming and spelling -- so a child who knows the
        picture but not the spelling still scores, which is the honest reading
        of what they know and keeps a five-year-old in the game.
        """
        question = self._kg_test_question()
        if question is None:
            return
        kind, stage = question["kind"], self._kg_test_stage
        self._kg_test_heard = heard or ""

        if stage == "name":
            if kind == "count":
                right = kg_content.number_matches(heard, question["value"])
                answer = kg_content.number_name(question["value"], "en")
            else:
                right = kg_content.matches_answer(heard, question["word"])
                answer = question["word"]
            if right:
                self._kg_test_score += 1
                self._kg_test_note = f"Yes! {answer}"
                lines = [(random.choice(self._kg_praise_for(kind)), "proud"),
                         (f"It is {answer}.", "warm")]
            else:
                # Say what they said before the answer, the same way the spelling
                # screen does: a child needs to hear the difference, not just the
                # right answer on its own.
                self._kg_test_note = f"It is {answer}"
                said = f"You said {heard}. " if heard.strip() else ""
                lines = [("Not quite.", "gentle"),
                         (f"{said}This is {answer}.", "curious")]
            self._kg_test_stage = "spell"
            self._draw_kg_test()
            # The counting half used to ask for the same number in Hindi, which
            # tests a second name for a thing they have just named rather than
            # anything about number. One step off the number they just counted is
            # the actual next idea -- and which way that step goes is chosen per
            # question, so the answer cannot be guessed from the shape of it.
            lines.append(({"count": self._kg_step_prompt(question.get("step", 1),
                                                         spoken=True),
                           "hi": "यह किस अक्षर से शुरू होता है?"}.get(
                              kind, f"Now spell {answer}."), "encouraging"))
            kg.kg_say_many(lines)
            self._kg_after_speaking(
                lambda r=self._kg_test_round: self._kg_test_listen(r))
            return

        # ----- the second half -----
        if kind == "count":
            wanted = question["value"] + question.get("step", 1)
            right = kg_content.number_matches(heard, wanted)
            answer = kg_content.number_name(wanted, "en")
        elif kind == "hi":
            # Which अक्षर does it start with. Devanagari has no letter-by-letter
            # spelling a KG child is taught, so the first letter is the skill.
            right = question["letter"] in (heard or "")
            answer = question["letter"]
        else:
            verdict, _letters = kg_content.heard_spelling(
                heard, kg_content.spelling_target(question["word"]))
            right = verdict == "correct"
            answer = kg_content.spell_out(question["word"])

        if right:
            self._kg_test_score += 1
            self._kg_test_note = "Correct!"
            lines = [(random.choice(self._kg_praise_for(kind)), "proud")]
        else:
            self._kg_test_note = f"It is {answer}"
            lines = [("Not quite.", "gentle"), (f"It is {answer}", "curious")]
        self._draw_kg_test()
        kg.kg_say_many(lines)
        # The index moves when the NEXT question actually starts, not here.
        # Incrementing now left a 3.2s gap in which the screen still on display
        # belonged to a question the index had already passed -- and on the last
        # one, any redraw in that gap (tapping "Say again") indexed off the end.
        self.root.after(3200, lambda r=self._kg_test_round: self._kg_test_advance(r))

    def _kg_test_advance(self, token):
        if self._kg_test_stale(token):
            return
        self._kg_test_at += 1
        self._kg_ask_test_question()

    def _kg_test_finish(self):
        total = len(self._kg_test_qs) * 2          # two marks per question
        score = self._kg_test_score
        share = score / total if total else 0
        self._overlay_screen("kg_test_done", "All done!",
                             f"You scored {score} out of {total}", tint="#EFF6FF")
        self._place_emoji("\U0001F31F" if share >= 0.8 else "\U0001F44F", UI_W / 2, 160, 108)
        if share >= 0.8:
            message, tone = "Brilliant! You really know these.", "excited"
        elif share >= 0.5:
            message, tone = "Well done! Keep practising.", "encouraging"
        else:
            message, tone = "Good try! Let's learn some more together.", "warm"
        self.canvas.create_text(UI_W / 2, 254, text=message,
                                font=self._font(16, True), fill=COL_TEXT,
                                tags=self.OVERLAY_TAG)
        self._overlay_button(140, 300, 380, 358, "Try again",
                             lambda: self.start_kg_test(self._kg_test_kind),
                             fill="#0EA5E9", size=14)
        self._overlay_button(420, 300, 660, 358, "Back",
                             self.show_kg_home, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=13)
        kg.kg_say_many([(f"You scored {score} out of {total}!", "excited"),
                     (message, tone)])
        # Recorded against the knowledge graph, so a parent switching to the
        # graded flow later sees that this child has met these at all.
        # Only the counting test has a concept in the graph. The letter tests
        # were being filed under "counting" as well, which put a maths mark on a
        # child's record for reciting the alphabet -- and that mark is what the
        # graded flow later reads to decide what they are ready for. The second
        # half of the counting test is now "one more", so it earns "addition"
        # too.
        slugs = []
        if self._kg_test_kind == "count":
            slugs = ["counting"]
            steps = {q.get("step", 1) for q in self._kg_test_qs}
            if 1 in steps:
                slugs.append("addition")
            if -1 in steps:
                slugs.append("subtraction")
        try:
            user_id = assistant.active_user_id()
            level = ("confident" if share >= 0.8
                     else "struggling" if share < 0.5 else "met")
            for slug in slugs if user_id else []:
                store.record_concept(user_id, slug, level)
        except Exception:
            pass

    def _kg_blocks_glyph(self, cx, cy, tag):
        """Three alphabet blocks, for Spelling."""
        tags = (self.OVERLAY_TAG, tag)
        for index, letter in enumerate("ABC"):
            x = cx - 96 + index * 66
            self._round_rect(x, cy - 30, x + 56, cy + 30, 10,
                             fill="#FFFFFF", outline="#FFFFFF", tags=tags)
            self.canvas.create_text(x + 28, cy, text=letter,
                                    font=self._font(20, True), fill="#7C3AED", tags=tags)

    def _kg_book_glyph(self, cx, cy, tag):
        """An open book, for Story Time."""
        tags = (self.OVERLAY_TAG, tag)
        for side in (-1, 1):
            self.canvas.create_polygon(
                cx, cy - 26, cx + side * 88, cy - 16, cx + side * 88, cy + 30,
                cx, cy + 22, fill="#FFFFFF", outline="#FFFFFF", tags=tags)
            for row in range(3):
                y = cy - 6 + row * 11
                self.canvas.create_line(cx + side * 14, y, cx + side * 72, y + side * 0,
                                        fill="#F59E0B", width=3, capstyle="round", tags=tags)
        self.canvas.create_line(cx, cy - 26, cx, cy + 22, fill="#F59E0B",
                                width=3, capstyle="round", tags=tags)

    # ----- spelling -----
    # A word is done in two stages: SAY the letters, then WRITE them.
    #
    # Saying it first is how spelling is actually taught and tested aloud at this
    # age, and it also means the child has already committed to an answer before
    # the keyboard appears to help them. The spoken stage is deliberately
    # FORGIVING -- Whisper transcribing a four-year-old spelling out loud is near
    # its worst case, so a miss there earns a nudge, never a mark. The written
    # stage is the one that counts.
    # Bounds only the wait for the child to START; how long they may take over
    # the letters themselves, and the pause that ends their turn, are
    # KG_SPELL_PHRASE_LIMIT_S and KG_SPELL_END_SILENCE_S.
    KG_SAY_SECONDS = KG_SPELL_START_TIMEOUT_S
    # Consecutive listens that came back with nothing before she stops asking.
    KG_UNHEARD_LIMIT = 3

    def show_kg_spelling(self):
        # A new word, on the same screen: whatever was being listened for
        # belonged to the word before it.
        kg.kg_cancel_listen()
        self._kg_word = kg_content.random_word(self._kg_seen_words)
        self._kg_seen_words.add(self._kg_word["word"])
        if len(self._kg_seen_words) >= len(kg_content.SPELLING_WORDS):
            self._kg_seen_words.clear()
        self._kg_typed = ""
        self._kg_feedback = ""
        self._kg_stage = "say"
        self._kg_heard = ""
        self._kg_listening = False
        # How many times they have had a go at saying this word's letters. The
        # help escalates with it; the word does not move on without it.
        self._kg_say_tries = 0
        # Counted apart from tries, because hearing nothing is the room's fault
        # and getting it wrong is not. A wrong answer is asked again forever; an
        # empty one is not, or a child who has walked away leaves the device
        # asking an empty chair for the rest of the afternoon.
        self._kg_unheard = 0
        # Every deferred step below is scheduled with root.after, and a child
        # taps Next Word long before those fire. Without a token, the previous
        # word's "now write it" lands on the NEW word and skips its speaking
        # stage entirely -- seen in testing as a word going straight to the
        # keyboard. Each callback checks the round it was born in.
        self._kg_round = getattr(self, "_kg_round", 0) + 1
        self._draw_kg_spelling()
        word = self._kg_word["word"]
        kg.kg_say_many([
            ("Spell this word.", "encouraging"),
            (f"{word}.", "excited"),
            (f"{self._kg_word['hint']}.", "gentle"),
            ("Say the letters out loud.", "curious"),
        ])
        # After she finishes asking, not before, or the microphone opens while
        # she is still talking and records her own voice saying the word.
        self._kg_after_speaking(lambda r=self._kg_round: self._kg_start_listening(r))

    # How long past a listen's own budget the screen keeps waiting before giving
    # up on it. ai_loop still has to upload the clip to Whisper and read the
    # answer back after the microphone closes, and on a Pi over home wifi with a
    # long counting clip that is not instant. This is only a backstop -- it
    # exists so that a lost answer costs one question rather than leaving the
    # screen on "I'm listening..." for ever, which is what it used to do.
    KG_LISTEN_MARGIN_S = 25.0

    def _kg_begin_listen(self, seconds, seed="", language="en",
                         phrase_limit=None, end_silence=None):
        """Ask for one listen and remember which answer belongs to us."""
        self._kg_listen_id = kg.kg_request_listen(
            seconds, seed=seed, language=language,
            phrase_limit=phrase_limit, end_silence=end_silence)
        self._kg_listen_deadline = (time.time() + seconds
                                    + (phrase_limit or seconds)
                                    + self.KG_LISTEN_MARGIN_S)
        return self._kg_listen_id

    def _kg_take_listen_result(self):
        """Our answer, "" if the deadline passed, or None to keep waiting.

        Answers that belong to a different listen are dropped rather than used.
        Before ids existed this took whatever turned up, so a listen still in
        flight from a screen the child had left was answered by the screen they
        moved to -- and the answer that screen was actually waiting for was
        never read at all.
        """
        while True:
            try:
                answer = kg_listen_results.get_nowait()
            except queue.Empty:
                break
            if (isinstance(answer, dict)
                    and answer.get("id") == getattr(self, "_kg_listen_id", None)):
                return answer.get("text", "")
        if time.time() > getattr(self, "_kg_listen_deadline", float("inf")):
            print("[KG] No answer came back in time; carrying on without one.",
                  flush=True)
            return ""
        return None

    def _kg_after_speaking(self, callback, settle_ms=400):
        """Run `callback` once Liza has ACTUALLY stopped talking.

        Every KG listen used to start on a fixed root.after guess -- 2.6s, 3.8s,
        6.2s -- which is a bet on how long a line takes to speak. It lost
        constantly: logs/liza.log has the microphone returning "Read the letters
        out loud." and "This is juice. Now spell juice.", which are Liza's own
        prompts recorded because the mic opened while she was still saying them.
        Every one of those was then marked as the child's wrong answer.

        playback_active and the queue tell us the truth, so wait on them instead
        of guessing, plus a short settle for the tail of the audio to leave the
        speaker before the microphone opens.
        """
        if playback_active.is_set() or not audio_queue.empty():
            self.root.after(120, lambda: self._kg_after_speaking(callback, settle_ms))
            return
        self.root.after(settle_ms, callback)

    def _kg_stale(self, round_token):
        return (self.overlay != "kg_spell"
                or round_token != getattr(self, "_kg_round", 0))

    def _kg_start_listening(self, round_token):
        if self._kg_stale(round_token) or self._kg_stage != "say":
            return
        self._kg_listening = True
        # A round token is not enough now that the word can be asked several
        # times WITHIN one round: tapping "Say it again" while a listen was
        # already polling left two poll loops on the same queue, and whichever
        # lost the result would go on scheduling itself forever. One generation
        # per listen, and only the newest one is allowed to read the answer.
        self._kg_listen_gen = getattr(self, "_kg_listen_gen", 0) + 1
        self._draw_kg_spelling()
        self._kg_begin_listen(self.KG_SAY_SECONDS, seed=kg.KG_SEED_LETTERS,
                              language="en",
                              phrase_limit=KG_SPELL_PHRASE_LIMIT_S,
                              end_silence=KG_SPELL_END_SILENCE_S)
        self._kg_poll_listen(round_token, self._kg_listen_gen)

    def _kg_listen_again(self, round_token):
        """Ask for the letters once more, once she has stopped talking."""
        if self._kg_stale(round_token) or self._kg_stage != "say":
            return
        self._kg_after_speaking(lambda r=round_token: self._kg_start_listening(r))

    def _kg_poll_listen(self, round_token, generation):
        """Wait for ai_loop's answer without blocking the Tk thread."""
        if (self._kg_stale(round_token) or self._kg_stage != "say"
                or generation != getattr(self, "_kg_listen_gen", 0)):
            return
        heard = self._kg_take_listen_result()
        if heard is None:
            self.root.after(150, lambda r=round_token, g=generation:
                            self._kg_poll_listen(r, g))
            return
        self._kg_listening = False
        self._kg_judge_spoken(heard)

    def _kg_judge_spoken(self, heard):
        """Mark one spoken attempt. Only a CORRECT one opens the keyboard.

        This used to correct the child and move straight on to writing whatever
        they said, which meant the correction was never actually practised: they
        heard the right letters once, in the same breath as being sent to a
        different task. Now the word stays put until they say it right, and the
        help escalates with each attempt -- read the letters back, then spell it
        for them to copy, then say it together. The "Write it" button is still
        there for a child who has had enough, because the way out of this must be
        a choice they make and not a silence the room made for them.
        """
        word = self._kg_word["word"]
        verdict, letters = kg_content.heard_spelling(heard, word)
        self._kg_heard = letters.upper()

        if verdict == "correct":
            praise = random.choice(kg_content.PRAISE)
            self._kg_feedback = praise
            self._draw_kg_spelling()
            kg.kg_say_many([(praise, "proud"),
                         ("Now write it.", "encouraging")])
            self.root.after(300, lambda r=self._kg_round: self._kg_to_writing(r))
            return

        if verdict == "unclear":
            # Nothing heard. Never counted as an attempt -- a quiet child or a
            # noisy room is not a spelling mistake, and charging them a try for
            # it would be the device's fault landing on them.
            self._kg_unheard = getattr(self, "_kg_unheard", 0) + 1
            if self._kg_unheard >= self.KG_UNHEARD_LIMIT:
                # Nobody is answering. Teach the word and hand them the keyboard
                # rather than going on asking an empty room.
                self._kg_feedback = "Let's write it together."
                self._draw_kg_spelling()
                kg.kg_say_many([("Let's write it together.", "warm"),
                             (f"{word} is {kg_content.spell_out(word)}", "curious"),
                             ("Now you write it.", "encouraging")])
                self.root.after(300, lambda r=self._kg_round: self._kg_to_writing(r))
                return
            self._kg_feedback = "I didn't hear you. Have another go!"
            self._draw_kg_spelling()
            kg.kg_say_many([("I didn't quite catch that.", "gentle"),
                         (f"The word is {word}.", "warm"),
                         ("Say the letters for me.", "encouraging")])
            self._kg_listen_again(self._kg_round)
            return

        # They spoke, so the room is not the problem.
        self._kg_unheard = 0

        self._kg_say_tries += 1
        tries = self._kg_say_tries

        # THE SAME NEAR MISS TWICE IS THE MICROPHONE, NOT THE CHILD.
        #
        # By now they have had the right letters read back to them once. A child
        # who does not know the word says something different the second time.
        # Whisper says B for their D every single time -- logs/liza.log has it
        # doing exactly that three times running while a child spelled HAND
        # correctly, and the screen has no way out of that loop, because the
        # next attempt is misheard identically. "Nobody fails their way out of
        # this screen" was written about children, not about the microphone.
        #
        # So the second one is taken. The WRITTEN stage immediately after is the
        # real assessment, and it is the one that cannot be misheard.
        if verdict == "near" and tries >= 2:
            praise = random.choice(kg_content.PRAISE)
            self._kg_feedback = praise
            self._draw_kg_spelling()
            kg.kg_say_many([(praise, "proud"),
                            ("Now write it.", "encouraging")])
            self.root.after(300, lambda r=self._kg_round: self._kg_to_writing(r))
            return

        if verdict == "said_the_word" and tries == 1:
            # They said the word rather than spelling it. Not wrong, just not
            # the question -- so ask the question again instead of marking it.
            # Only the first time: a child who says the word a second time is
            # not misunderstanding the question, they are stuck, and they drop
            # into the help below like anyone else.
            self._kg_feedback = "Yes! Now the letters."
            self._draw_kg_spelling()
            kg.kg_say_many([(f"Yes, the word is {word}.", "warm"),
                         ("Now say the letters, one by one.", "encouraging")])
        elif verdict == "jumbled":
            # They HAVE the letters, in the wrong order. Saying the letters again
            # would teach nothing, because the letters were never the problem --
            # so name what actually went wrong and put the order side by side.
            self._kg_feedback = "Right letters, wrong order! Try again."
            self._draw_kg_spelling()
            kg.kg_say_many([
                ("Ooh, so close!", "encouraging"),
                (f"You said {kg_content.spell_out(letters)}", "gentle"),
                ("You have all the right letters, but they are in a different order.",
                 "gentle"),
                (f"Listen. {word} is {kg_content.spell_out(word)}", "curious"),
                ("Now you say it.", "encouraging"),
            ])
        elif tries == 1:
            # Say what they said before saying what is right. A child who is told
            # only the answer does not learn which part of theirs was wrong, and
            # hearing their own attempt read back is what makes the difference
            # audible.
            nudge = random.choice(kg_content.ENCOURAGEMENT)
            self._kg_feedback = f"{nudge} Try again."
            self._draw_kg_spelling()
            kg.kg_say_many([
                (nudge, "encouraging"),
                (f"You said {kg_content.spell_out(letters)}", "gentle"),
                (f"But {word} is {kg_content.spell_out(word)}", "curious"),
                ("Now you say it.", "encouraging"),
            ])
        elif tries == 2:
            # Second miss: stop expecting recall and give them something to copy.
            # The letters are already on the screen in front of them.
            self._kg_feedback = "Say it after me!"
            self._draw_kg_spelling()
            kg.kg_say_many([
                ("Not yet. Let me help you.", "gentle"),
                (f"Look at the letters on the screen. {word}.", "curious"),
                (kg_content.spell_out(word), "storyteller"),
                ("Now you say it, just like that.", "encouraging"),
            ])
        else:
            # Third and after: say it WITH them. Nobody fails their way out of
            # this screen -- they either get it or they tap Write it.
            self._kg_feedback = "Let's say it together!"
            self._draw_kg_spelling()
            kg.kg_say_many([
                ("Let's say it together.", "warm"),
                (f"{word}.", "excited"),
                (kg_content.spell_out(word), "storyteller"),
                ("Your turn. Say the letters.", "encouraging"),
            ])
        self._kg_listen_again(self._kg_round)

    def _kg_repeat_word(self):
        """Say the word again AND re-open the microphone behind it."""
        kg.kg_say_many([(f"{self._kg_word['word']}.", "excited"),
                     (f"{self._kg_word['hint']}.", "gentle"),
                     ("Say the letters out loud.", "curious")])
        self._kg_listen_again(self._kg_round)

    def _kg_to_writing(self, round_token=None):
        # None when a child tapped the Write it button, which is always for the
        # word in front of them.
        if round_token is not None and self._kg_stale(round_token):
            return
        if self.overlay != "kg_spell":
            return
        # It is their hands' turn, not their voice's. The screen name does not
        # change between the two stages, so _overlay_screen cannot see this one.
        kg.kg_cancel_listen()
        self._kg_stage = "write"
        self._kg_typed = ""
        self._draw_kg_spelling()

    def _draw_kg_spelling(self):
        saying = getattr(self, "_kg_stage", "write") == "say"
        if saying:
            if self._kg_listening:
                subtitle = ("Listening... say the letters again"
                            if getattr(self, "_kg_say_tries", 0)
                            else "Listening... say the letters")
            else:
                subtitle = self._kg_word["hint"]
        else:
            subtitle = f"Now write it:  {self._kg_word['hint']}"
        self._overlay_screen("kg_spell", "Spell a Word", subtitle, tint="#F6F2FF")

        if saying:
            self._draw_kg_saying()
            return

        # What they have tapped so far, as one box per letter of the answer, so
        # the child can see how many letters are still to come.
        target = self._kg_word["word"]
        slot, gap = 56, 10
        total = len(target) * slot + (len(target) - 1) * gap
        x = (UI_W - total) / 2
        for index in range(len(target)):
            typed = self._kg_typed[index] if index < len(self._kg_typed) else ""
            self._round_rect(x, 96, x + slot, 96 + 64, 10, fill="#FFFFFF",
                             outline="#D9D2F5", tags=self.OVERLAY_TAG)
            self.canvas.create_text(x + slot / 2, 128, text=typed.upper(),
                                    font=self._font(22, True), fill="#7C3AED",
                                    tags=self.OVERLAY_TAG)
            x += slot + gap

        if self._kg_feedback:
            self.canvas.create_text(UI_W / 2, 180, text=self._kg_feedback,
                                    font=self._font(12, True), fill=COL_TEXT,
                                    tags=self.OVERLAY_TAG)

        for r, row in enumerate(["ABCDEFGHI", "JKLMNOPQR", "STUVWXYZ"]):
            width, gap = 76, 5
            total = len(row) * width + (len(row) - 1) * gap
            x = (UI_W - total) / 2
            y = 200 + r * 60
            for letter in row:
                self._overlay_button(x, y, x + width, y + 54, letter,
                                     lambda c=letter: self._kg_letter(c),
                                     fill="#FFFFFF", text_colour="#4C3A8F",
                                     radius=10, size=16)
                x += width + gap

        self._overlay_button(20, 396, 150, 448, "Undo", self._kg_undo,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(166, 396, 316, 448, "Say it again",
                             lambda: kg.kg_say(f"{self._kg_word['word']}. "
                                            f"{self._kg_word['hint']}."),
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(484, 396, 634, 448, "Next word",
                             self.show_kg_spelling, fill="#7C3AED", size=11)
        self._overlay_button(650, 396, 780, 448, "Back",
                             self.show_kg_home, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)

    def _draw_kg_saying(self):
        """The SAY stage. No keyboard at all -- it is not their turn to write.

        Showing the word in letters they can see while they say it is deliberate:
        this stage is about hearing the letters, not recalling them, and the
        recall test is the written stage immediately after.
        """
        word = self._kg_word["word"].upper()
        self.canvas.create_text(UI_W / 2, 150, text=word, font=self._font(44, True),
                                fill="#7C3AED", tags=self.OVERLAY_TAG)
        self.canvas.create_text(UI_W / 2, 208, text="  ".join(word),
                                font=self._font(16, True), fill="#A78BFA",
                                tags=self.OVERLAY_TAG)

        if self._kg_listening:
            self._round_rect(UI_W / 2 - 150, 250, UI_W / 2 + 150, 300, 24,
                             fill="#EDE7FF", outline="#7C3AED",
                             tags=self.OVERLAY_TAG)
            self.canvas.create_text(UI_W / 2, 275, text="I'm listening...",
                                    font=self._font(15, True), fill="#5B21B6",
                                    tags=self.OVERLAY_TAG)
        elif self._kg_feedback:
            self.canvas.create_text(UI_W / 2, 275, text=self._kg_feedback,
                                    font=self._font(15, True), fill=COL_TEXT,
                                    tags=self.OVERLAY_TAG)

        if self._kg_heard:
            self.canvas.create_text(UI_W / 2, 322,
                                    text=f"I heard:  {'  '.join(self._kg_heard)}",
                                    font=self._font(11), fill=COL_TEXT_DIM,
                                    tags=self.OVERLAY_TAG)

        # A way past the microphone, always. If the room is loud, or the child
        # will not speak, or Whisper simply never returns, this stage must not be
        # a dead end -- so writing is one tap away at every moment.
        # Repeating the word and then NOT listening left the child talking to a
        # closed microphone -- they hear the word, spell it, and nothing happens.
        self._overlay_button(166, 396, 396, 448, "Say it again",
                             self._kg_repeat_word,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(412, 396, 634, 448, "Write it",
                             self._kg_to_writing, fill="#7C3AED", size=12)
        self._overlay_button(650, 396, 780, 448, "Back",
                             self.show_kg_home, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)

    def _kg_letter(self, letter):
        target = self._kg_word["word"]
        if len(self._kg_typed) >= len(target):
            return
        self._kg_typed += letter.lower()
        if len(self._kg_typed) < len(target):
            self._kg_feedback = ""
            self._draw_kg_spelling()
            return
        # The word is now full length, so it can be marked.
        if self._kg_typed == target:
            praise = random.choice(kg_content.PRAISE)
            self._kg_feedback = praise
            self._draw_kg_spelling()
            kg.kg_say(f"{praise} {target}. {kg_content.spell_out(target)}")
            # Long enough for the praise to finish before the next word starts.
            self.root.after(4200, self._kg_next_if_still_spelling)
        else:
            nudge = random.choice(kg_content.ENCOURAGEMENT)
            self._kg_feedback = nudge
            self._kg_typed = ""
            self._draw_kg_spelling()
            kg.kg_say(f"{nudge} {target} is spelled {kg_content.spell_out(target)}. "
                   f"Now you try. {target}.")

    def _kg_next_if_still_spelling(self):
        # The child may have tapped Back or Story Time while the praise played.
        if self.overlay == "kg_spell":
            self.show_kg_spelling()

    def _kg_undo(self):
        self._kg_typed = self._kg_typed[:-1]
        self._kg_feedback = ""
        self._draw_kg_spelling()

    # ----- stories -----
    # The story arrives as CAPTIONS: one beat on the screen at a time, put there
    # at the instant it is spoken. The whole story printed at once was wallpaper
    # to a pre-reader -- nothing on it moved, so nothing on it connected to the
    # voice. One line at a time is followable, and it is also what makes the
    # acting visible: the slow, quiet, frightened beat is on the screen while it
    # is being said slowly and quietly. See caption_begin() for where the timing
    # comes from, and KG_EMOTIONS for how a tone becomes a speed and a volume.

    # Each tone gets a colour and a size, so the screen carries the same feeling
    # as the voice. Deliberately the same tone names the segments already use --
    # a story never has to say what colour it is.
    # How long to wait for the first sound of a story before giving up on the
    # audio and letting the screen carry on without it.
    KG_NARRATE_GRACE_S = 12.0

    KG_CAPTION_STYLE = {
        "excited":     ("#EA580C", 21),
        "amazed":      ("#C2410C", 20),
        "proud":       ("#B45309", 20),
        "encouraging": ("#B45309", 19),
        "curious":     ("#4C3A8F", 19),
        "storyteller": ("#3F3D56", 19),
        "warm":        ("#9D174D", 19),
        "gentle":      ("#4B5563", 18),
        "sad":         ("#1D4ED8", 18),
        "mysterious":  ("#4C1D95", 18),
    }

    def show_kg_story(self, language=None):
        # Remembered so Next story stays in the language they chose rather than
        # dropping back to English on the second story.
        if language:
            self._kg_story_lang = language
        language = getattr(self, "_kg_story_lang", "en")
        self._kg_story = kg_content.random_story_in(language, self._kg_seen_stories)
        self._kg_seen_stories.add(self._kg_story["title"])
        if len(self._kg_seen_stories) >= len(kg_content.stories_for(language)):
            self._kg_seen_stories.clear()
        self._kg_narrate()

    def _kg_narrate(self):
        """Read the story as its beats, each with its own delivery, and caption it.

        One kg_say_many call rather than one per beat, so the whole story is a
        single response and the audio runs continuously -- see kg_say_many for
        why per-line calls would put a gap at every seam.
        """
        story = self._kg_story
        self._kg_story_asked = False
        self._kg_story_line = -1
        self._kg_story_round = getattr(self, "_kg_story_round", 0) + 1
        round_token = self._kg_story_round
        # Torn down FIRST and the session opened on the quiet, so a story still
        # draining from a previous tap cannot caption this one.
        assistant.interrupt_playback()
        self._kg_caption = audio.caption_begin()
        self._kg_narrate_at = time.time()
        # The QUESTION goes in the same response as the story, as its last line.
        #
        # It used to be spoken separately, once the story was judged to have
        # finished -- and every way of judging that is a guess about what the
        # speaker is doing, made from another thread. kg_say interrupts playback
        # before it speaks, so a guess that lands early does not merely ask
        # early: it TEARS THE STORY DOWN and asks instead, which is exactly the
        # "it asked the question before the story" that was reported. One
        # response cannot interrupt itself, so there is no longer a guess to get
        # wrong -- the question is simply the line after the last beat, and the
        # buttons appear when its own caption does.
        kg.kg_say_many([(f"{story['title']}.", "storyteller")]
                    + list(story["segments"])
                    + [(story["question"], "curious")])
        self._draw_kg_story()
        self._kg_follow_captions(round_token)

    def _kg_stale_story(self, round_token):
        return (self.overlay != "kg_story"
                or round_token != getattr(self, "_kg_story_round", 0))

    def _kg_follow_captions(self, round_token):
        """Keep the screen on the line she is saying. Tk thread, root.after only.

        Polling rather than a callback from the player because every Tk call has
        to happen on the Tk thread, and the player runs on its own -- the same
        reason the KG listens are polled instead of pushed.
        """
        if self._kg_stale_story(round_token):
            return
        # Cue 0 is the title, 1..N are the beats, and N+1 is the question.
        last = len(self._kg_story["segments"]) + 1
        index, _text = audio.caption_now(self._kg_caption)
        if index != getattr(self, "_kg_story_line", -1):
            self._kg_story_line = index
            # The question is on the screen exactly when it is in the air.
            self._kg_story_asked = index >= last
            self._draw_kg_story()
        if index >= last:
            return                      # the question is up; nothing left to follow
        # No audio ever arrived -- Cartesia is down, or the network is. Without
        # this the poll runs for as long as the screen is up and the question is
        # never asked, so the story screen becomes a dead end on a bad network.
        if (index < 0 and not playback_active.is_set() and audio_queue.empty()
                and time.time() - getattr(self, "_kg_narrate_at", 0)
                > self.KG_NARRATE_GRACE_S):
            print("[KG] The story never reached the speaker; showing the question.",
                  flush=True)
            self._kg_story_asked = True
            self._kg_story_line = last
            self._draw_kg_story()
            return
        self.root.after(100, lambda r=round_token: self._kg_follow_captions(r))

    def _kg_caption_at(self, line):
        """(text, tone) of beat `line`, or (None, None). Line -1 is the title."""
        segments = self._kg_story["segments"]
        if 0 <= line < len(segments):
            return segments[line]
        return None, None

    def _draw_kg_story(self):
        # _kg_story_line counts CUES, and cue 0 is the title, so beat n is cue
        # n + 1. Before any sound, nothing but the title is on the screen.
        beat = getattr(self, "_kg_story_line", -1) - 1
        segments = self._kg_story["segments"]
        asked = self._kg_story_asked

        # The card fills the screen now that it holds one line instead of a
        # wall of text: a caption a child is meant to follow has to be the
        # biggest thing on the screen, not a strip along the top of it.
        subtitle = "What do you think?" if asked else "Listen to the story"
        self._overlay_screen("kg_story", self._kg_story["title"], subtitle,
                             tint="#FFF6E8")
        self._round_rect(40, 92, 760, 388, 16, fill="#FFFFFF",
                         outline="#F3E2C6", tags=self.OVERLAY_TAG)

        if asked:
            self.canvas.create_text(UI_W / 2, 168, text=self._kg_story["question"],
                                    width=640, justify="center",
                                    font=self._font(18, True), fill=COL_TEXT,
                                    tags=self.OVERLAY_TAG)
            self._overlay_button(180, 240, 380, 330, "YES",
                                 lambda: self._kg_answer(True),
                                 fill="#14B8A6", size=20)
            self._overlay_button(420, 240, 620, 330, "NO",
                                 lambda: self._kg_answer(False),
                                 fill="#F43F5E", size=20)
        else:
            # The line just gone, small and faded, so the story has somewhere to
            # have come from and the current line is unmistakably the current one.
            previous, _tone = self._kg_caption_at(beat - 1)
            if previous:
                self.canvas.create_text(UI_W / 2, 138, text=previous, width=640,
                                        justify="center", font=self._font(11),
                                        fill="#B9AE99", tags=self.OVERLAY_TAG)

            text, tone = self._kg_caption_at(beat)
            if text is None:
                text, tone = self._kg_story["title"], "storyteller"
            colour, size = self.KG_CAPTION_STYLE.get(tone, (COL_TEXT, 19))
            self.canvas.create_text(UI_W / 2, 232, text=text, width=660,
                                    justify="center", font=self._font(size, True),
                                    fill=colour, tags=self.OVERLAY_TAG)

            # One dot per beat, filled as far as she has read. A pre-reader
            # cannot read "beat 4 of 10", but they can see four lit dots.
            if segments:
                gap = min(22, 640 / max(len(segments), 1))
                x = UI_W / 2 - gap * (len(segments) - 1) / 2
                for index in range(len(segments)):
                    done = index <= beat
                    r = 5 if done else 3
                    self.canvas.create_oval(x - r, 350 - r, x + r, 350 + r,
                                            fill="#F59E0B" if done else "#EFE3CC",
                                            outline="", tags=self.OVERLAY_TAG)
                    x += gap

        self._kg_ask_button(self.KG_ASK_BOX_STORY)
        self._overlay_button(20, 414, 170, 462, "Read again",
                             self._kg_narrate,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)
        self._overlay_button(320, 414, 480, 462, "Next story",
                             self.show_kg_story, fill="#F59E0B", size=11)
        self._overlay_button(630, 414, 780, 462, "Back",
                             self.show_kg_story_picker,
                             fill="#E6E9F5", text_colour=COL_TEXT, size=11)

    def _kg_answer(self, said_yes):
        # Answered in the story's own language: praise in English after a Hindi
        # story breaks the spell for the child who was just listening to it.
        language = getattr(self, "_kg_story_lang", "en")
        correct = said_yes == self._kg_story["answer"]
        if correct:
            kg.kg_say(kg_content.story_praise(language), "proud")
        else:
            kg.kg_say(kg_content.story_verdict(self._kg_story, language), "gentle")
        self._kg_story_asked = False
        # Back to the last line of the story rather than a blank card.
        self._kg_story_line = len(self._kg_story["segments"])
        self._draw_kg_story()

class HeadlessUI:
    def __init__(self):
        self.current_state = "idle"
        self.overlay = None     # there are no overlays without a screen
        self.modes = ["TUTOR", "CO-TELL", "RE-TELL"]
        self.current_mode_index = 0
        self.current_mode = self.modes[0]
        self.asleep = False
        self.ui_mode = "normal"
        self.emotion = None
    def set_state(self, state_type, caption=None): self.current_state = state_type
    def set_transcript(self, text, speaker="user"): pass
    def set_weather(self, reading): pass
    def set_now_playing(self, title, loading=False): pass
    def set_media_progress(self, pos, dur, paused): pass
    def set_ui_mode(self, mode):
        # No widgets to hide with no screen, but the state still has to agree
        # with what the model is told, or it will keep re-issuing the action.
        self.ui_mode = "3d" if mode == "3d" else "normal"
    def set_emotion(self, mood): self.emotion = (mood or "").strip().lower() or None
    def go_to_sleep(self, event=None):
        self.asleep = True
        wake_event.clear()
        sleep_event.set()
        if media_active.is_set(): assistant.stop_media_playback()
        if playback_active.is_set() or not audio_queue.empty(): assistant.interrupt_playback()


# ---------------------------------------------------------------------------
# The back-reference, bound LAST on purpose.
#
# assistant.py imports names FROM this file, and this file calls back into the
# assistant. With `import assistant` up with the other imports, that cycle only
# resolved when the assistant happened to be imported first: `import ui` on
# its own ran this module as far as that line, handed control to the assistant,
# and the assistant's `from ui import ...` then found a module that had not
# defined anything yet. A plain ImportError, and only for whoever opened this
# file to work on it.
#
# Down here every name above is already defined, so the import resolves whichever
# module is asked for first. It binds the MODULE only -- nothing is read off it
# until something is actually called -- which is what makes that legal at all.
import assistant
# The Kindergarten flow underneath these screens; bound here for the same
# import-order reason as the line above it.
import kg
# Captions are cued by the player, so they come from audio.py; bound here for
# the same reason as the line above it.
import audio
