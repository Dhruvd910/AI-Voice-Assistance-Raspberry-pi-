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
import signal
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
import camera
import kg_content
import media
import profiles
import chatlog
import solution
import store
import visuals
import geometry
import whiteboard
from config import (BOARD_ENABLED, BOARD_READ_LIVE, BOARD_READ_PAUSE_S,
                    KG_COUNT_END_SILENCE_S, KG_COUNT_PHRASE_LIMIT_S,
                    KG_COUNT_START_TIMEOUT_S, KG_SPELL_END_SILENCE_S,
                    KG_SPELL_PHRASE_LIMIT_S, KG_SPELL_START_TIMEOUT_S)
from prompts import (BOARD_ASK_QUESTIONS, BOARD_SOLVE_QUESTIONS, GEOMETRY_ABOUT_QUESTIONS,
                     GEOMETRY_ASK_QUESTIONS)

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

# TEXT ON THE THREE HEADER CARDS, WHICH DO NOT HAVE A WHITE GROUND ANY MORE.
#
# Their artwork is a saturated blue with a white glow burnt through it, and the
# greys the rest of the screen uses for secondary text do not survive it:
# COL_TEXT_DIM measures 1.4:1 against that blue and COL_INDIGO 1.9:1, which is
# not "a bit low", it is gone. White is no better -- it is 2.3:1 on the blue and
# invisible on the glow. Dark is the only value that holds up over BOTH grounds,
# which is what any one label on these cards has to do, since the glow moves
# through the card and the labels do not.
#
# Measured against the DARKEST pixel in that artwork, #1E8CC5, and not against
# the blue it looks like -- the card deepens toward its bottom edge, which is
# exactly where the smallest labels sit. Calibrating against the mid blue put
# the city line at 2.6:1 with 8% of its box under 3:1, and it was the last row
# of the card doing it every time.
#
# There is no room for a third tone here. Anything light enough to read as
# "secondary" against the glow fails against that dark blue, so the two values
# below are both near-black and the hierarchy on these cards comes from SIZE,
# the way it does on the mode buttons.
COL_CARD_TEXT = COL_TEXT      # 4.3:1 on the darkest blue, 16:1 on the glow
COL_CARD_DIM  = "#101F31"     # 4.4:1 on the darkest blue, 17:1 on the glow
# 4.5:1 -- the usual bar for text this small -- needs a luminance under 0.012
# against that blue, which is black and not a colour. 4:1 is the honest bar on
# this artwork, and it is what the card is tested to.
# The size of a header card, and the shape any replacement artwork is fitted to.
CARD_BOX = (204, 62)

MODE_ACCENTS = {"TUTOR": "#7C3AED", "CO-TELL": "#14B8A6", "RE-TELL": "#F59E0B"}
MODE_TINTS   = {"TUTOR": "#F3EEFF", "CO-TELL": "#E6FAF6", "RE-TELL": "#FFF4E6"}
# ONE LINE EACH, AND SHORT ENOUGH TO SET AT A SIZE THAT CAN BE READ.
#
# These were "Concepts and solutions" and friends, painted into the card art at
# 7px and wrapped to two lines. Nobody could read them. The card gives its
# wording an 88x20 box beside the icon, and measured in the real font that is
# one line of 9pt -- so the copy is cut to fit the box rather than the box being
# asked to hold the copy. Widths at 9pt: 71px, 80px, 76px, against 88 available.
MODE_BLURBS = {
    "TUTOR": "Ask anything",
    "CO-TELL": "Talk it through",
    "RE-TELL": "You teach me"
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

# The clock-and-weather card's grid. Three rows and two columns, and BOTH
# COLUMNS ARE THE SAME SHAPE -- an icon and a headline on the top row, then two
# plain rows under it. That is what makes 204x62 hold this much. The two halves
# used to be laid out independently against nothing in particular, and neither
# of them fitted: the temperature ran 19px off the card, the meridiem crossed
# the divider, the condition sat on top of the low, and the place line was drawn
# straight through the middle of the dial.
#
# THE CARD IS 62px TALL AND ONLY 55 OF THEM ARE CARD.
#
# Measured off the artwork rather than assumed from the box: the fill is fully
# opaque only from y+1 to y+56, and the five rows under that are its soft bottom
# edge and shadow. Sitting a line of text on them puts it half on the card and
# half on the wallpaper, which is what the arrows on the bottom row were doing.
CARD_INK_TOP = TOP_Y0 + 1        # 18
CARD_INK_BOTTOM = TOP_Y0 + 56    # 73
# Row centres, not tops, and they have to hold three line boxes inside those 55
# pixels: 16pt is 30, 7pt is 13, 6pt is 12. That comes to 55 exactly, which is
# why the headline is 16 and not the 17 it was when this was being fitted to the
# nominal 62.
CARD_ROW1 = TOP_Y0 + 16          # time          | glyph + temperature
CARD_ROW2 = TOP_Y0 + 38          # date          | condition
CARD_ROW3 = TOP_Y0 + 50          # place         | high and low
# The split. With the dial gone the left column holds three left-aligned lines
# and nothing else, and the widest of them is the place line at 84px, so 100
# from the card's edge covers it with room to spare. That hands 30px straight to
# the weather side, which is the half with four things in it -- and it is what
# lets the temperature match the time at 17pt instead of trailing it at 15.
CLOCK_SPLIT = CLOCK_X0 + 101
# The condition glyphs are drawn at the size they were designed at -- the
# clear-sky sun is 38px across its rays and a storm hangs 47px top to bottom --
# and scaled about their own centre afterwards, so the shapes keep their
# proportions and only one number changes when the layout does.
#
# 0.62 is set by the sun, which is the widest of the nine by a long way: it has
# 28px between the column's edge and the temperature, and 38 * 0.62 plus the 2px
# its rays are stroked at comes to 25.6. The other eight would take 0.85 quite
# happily -- but the width goes to the CONDITION instead, which has to hold
# "Thunderstorm" at a size somebody can read, and a smaller mark costs less than
# a smaller word.
WEATHER_GLYPH_SCALE = 0.62
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

def ui_asset(*parts, size=None, box=None):
    """A PNG from the UI folder, as RGBA, or None if it is not there.

    `size` gives a square version, for the avatars, which are drawn at 110 and
    used smaller. Cached by size as well: these are placed on every redraw of a
    screen, and decoding and resampling a PNG each time is not free on a Pi.
    None rather than an exception because a missing file must cost a nicer
    button, not the screen.

    `box` is (w, h) and makes the artwork exactly that size. It is for the three
    header cards, whose position and width are fixed by the layout and by every
    coordinate drawn on top of them: a replacement exported at 242x62 instead of
    204x62 would otherwise be placed at its own size and run 38px into the card
    beside it. Squashed rather than cropped or letterboxed, because what these
    hold is a rounded rectangle and a soft glow -- 15% of horizontal scale costs
    a slightly oval corner and nothing else. Do not reach for it on artwork with
    a figure or lettering in it."""
    key = (parts, size, box)
    if key not in _ui_assets:
        path = os.path.join(UI_DIR, *parts)
        try:
            image = Image.open(path).convert("RGBA")
            if size:
                image = image.resize((size, size), Image.LANCZOS)
            elif box and image.size != tuple(box):
                print(f"[UI] {os.path.join(*parts)} is {image.width}x{image.height}; "
                      f"fitting it to the {box[0]}x{box[1]} card.", flush=True)
                image = image.resize(tuple(box), Image.LANCZOS)
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

# How much of a screen's own colour is washed over the wallpaper behind it.
# The profile and Kindergarten screens are meant to read as COLOURED GLASS over
# the same garden the home screen shows, so this one number is what decides
# whether they look like tinted glass or like a flat panel with a picture stuck
# behind it. Measured against the artwork: below about 0.5 the foliage around
# the edge of the wallpaper fights the wording, above about 0.7 the wallpaper
# stops being visible at all and we are back to the flat fill this replaces.
OVERLAY_TINT_ALPHA = 0.58

# One composited backing per screen colour, built on first use and kept for the
# life of the process. There are a dozen tints, each is a full 800x480 bitmap,
# and a lesson screen redraws on every letter -- recompositing the wallpaper
# each time is a visible stutter on this Pi.
_overlay_backings = {}

def _overlay_backing(tint):
    """The wallpaper with `tint` washed over it at UI size, or None.

    A Tk canvas rectangle has no alpha, so every modal screen used to be backed
    by a FLAT OPAQUE fill -- which is the plain white strip showing around the
    Kindergarten artwork, because the glass panel is 762x441 on an 800x480
    screen and the fill is what fills the margin. Compositing the wash into a
    bitmap here gets the translucency these screens were drawn for without
    reaching for a second toolkit, and it costs one PhotoImage per colour.

    None when the wallpaper cannot be read, which is the caller's cue to draw
    the flat fill it always drew."""
    if tint not in _overlay_backings:
        try:
            wall = _background_image(UI_W, UI_H)
            wash = Image.new("RGB", wall.size, tint)
            _overlay_backings[tint] = ImageTk.PhotoImage(
                Image.blend(wall, wash, OVERLAY_TINT_ALPHA))
        except Exception as exc:
            print(f"[UI] Overlay wallpaper unavailable ({exc}); "
                  f"falling back to the flat tint.", flush=True)
            _overlay_backings[tint] = None
    return _overlay_backings[tint]


# ---------------------------------------------------------------------------
# The classroom
# ---------------------------------------------------------------------------
# The alphabet, spelling and A-to-Z screens are drawn as a classroom rather than
# as a tinted worksheet. This is not decoration. A four-year-old recognises a
# blackboard long before they can read a heading, so putting the lesson ON one
# tells them what kind of thing they are looking at without a word of it being
# read to them -- and it puts the mascot in the room with them rather than on a
# blank page beside it.
#
# The artwork is 800x480 and fully opaque, so it replaces the tinted wallpaper
# outright; the tint passed to _overlay_screen still matters only as the colour
# of the screens that have no classroom.
KG_CLASS_BG = ("KG Activity", "A_Z", "Class_bg.png")

# The green writing surface inside the wooden frame, measured off the artwork
# with a coordinate grid rather than chosen by eye: x 212..606, y 70..258, with
# the frame's bottom lip carrying on to 274. Everything the lesson SAYS is
# written inside that box; anything that will not fit -- the spelling keyboard
# -- goes on the wall below it, where there is nothing to fight with.
#
# Note BOARD_MID is 409 and not 400: the board is not centred in the room, and
# wording centred on the screen sits visibly left of centre on the board.
BOARD_L, BOARD_R = 212, 606
BOARD_MID = (BOARD_L + BOARD_R) / 2

# Chalk, and chalk that has been rubbed once. Both are measured against the
# LIGHTEST pixel inside the writing surface (#54705A, where the overhead light
# falls on it), which is the worst case and the only one worth checking:
# 5.0:1 and 4.8:1, so both clear AA for body text at the sizes used here.
CHALK      = "#F3F7F0"
CHALK_DIM  = "#E8F2E5"
# What she is doing right now, as opposed to what the board says -- the
# listening line, the feedback, the "Perfect!". Warm so that it reads as live
# rather than as more of the lesson. 4.4:1 against that same worst case, so it
# is only ever used at 14pt bold or larger, where AA asks for 3:1.
CHALK_WARM = "#FDE68A"

# The pills on the floor. Navy on white for anything that leaves or repeats,
# white on navy for the one button that carries the lesson forward, so a child
# who cannot read the words can still find the one that means "go on".
CLASS_BLUE = "#17408B"
CLASS_INK  = "#1D4ED8"
CLASS_PILL = "#FFFFFF"
# Counting keeps the green it has worn everywhere else in the app, darkened to
# 7.7:1 against the frosted card it is now written on.
COUNT_GREEN = "#065F46"

# Coloured chalk, for the story captions. They carried an ink colour per tone
# on the white card they used to be printed on, and dropping all ten to one
# white would have flattened a storyteller's voice into a worksheet's. These are
# the colours a box of chalk actually comes in, and each is checked against the
# LIGHTEST pixel of the board: 5.0, 4.6 and 4.5 to one.
CHALK_YELLOW = "#FDEBA8"
CHALK_BLUE   = "#DCEBFB"


_glass_cards = {}

def glass_card(box, radius=22, wash=0.46):
    """A frosted panel cut out of the classroom, as RGBA. None if the art is missing.

    A Tk canvas item has no alpha, so a translucent panel cannot be DRAWN -- it
    has to be composited into a bitmap first, the same trick _overlay_backing
    uses for the tinted wallpaper. What sits behind this panel is known here,
    because the classroom never moves, so the blur is done once per box and
    cached for the life of the process.

    `wash` is how much white is mixed into the blur. It is the only dial worth
    turning: too little and white lettering on the pale right-hand end of the
    card stops being readable, too much and the panel stops looking like glass
    and starts looking like a hole.
    """
    key = (box, radius, wash)
    if key not in _glass_cards:
        room = ui_asset(*KG_CLASS_BG)
        if room is None:
            _glass_cards[key] = None
            return None
        x0, y0, x1, y1 = box
        crop = room.crop((int(x0), int(y0), int(x1), int(y1))).convert("RGB")
        crop = crop.filter(ImageFilter.GaussianBlur(10))
        crop = Image.blend(crop, Image.new("RGB", crop.size, "#FFFFFF"), wash)
        card = crop.convert("RGBA")
        # Rounded by an alpha mask rather than by drawing corners on: the panel
        # has to let the room show at its edges, and a painted corner would be
        # a wrong-coloured triangle over whatever it happened to land on.
        mask = Image.new("L", crop.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, crop.width - 1, crop.height - 1), radius, fill=255)
        card.putalpha(mask)
        # A lit rim, so the panel has an edge against the wall rather than
        # fading into it. Two pixels; one disappears on this screen.
        ImageDraw.Draw(card).rounded_rectangle(
            (0, 0, crop.width - 1, crop.height - 1), radius,
            outline=(255, 255, 255, 205), width=2)
        _glass_cards[key] = card
    return _glass_cards[key]


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
# The English alphabet is now drawn artwork rather than emoji, and that artwork
# ships with the rest of the KG screens rather than in pictures/. Both folders
# are searched, in this order, so pictures/ stays the place to override one
# drawing without touching the set it came in.
PICTURE_DIRS = (PICTURE_DIR, os.path.join(UI_DIR, "KG Activity", "A_Z"))
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
    image = None
    for folder in PICTURE_DIRS:
        path = os.path.join(folder, os.path.basename(name))
        if not os.path.exists(path):
            continue
        try:
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
        break
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


# Hindi has to be drawn by Pillow, not by Tk. Tk 8.6 on X11 puts characters down
# in the order they are stored and does no shaping at all, so the ि in तितली
# lands after its consonant instead of before it and the ट्ट in लट्टू comes out as
# two whole letters with a halant hanging between them. Measured on this Pi: Tk
# drew लट्टू 142px wide where the shaped word is 96px. To a child learning to
# read, that is a misspelling. Pillow with libraqm shapes it properly, so text
# with any Devanagari in it is rasterised here and placed as an image instead.
DEVANAGARI_FONTS = {
    False: "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    True: "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
}
# WHAT TO USE WHEN THE SAME LINE IS IN TWO SCRIPTS.
#
# The Noto Devanagari faces above have no Latin letters in them at all, and
# Pillow has no font fallback -- so a mixed line comes out with a tofu box per
# Latin character. "हिंदी letter क, as in कमल" rendered as "हिंदी ⬜⬜⬜⬜⬜⬜ क,
# ⬜⬜ ⬜⬜ कमल" on the progress screen, which is the whole line unreadable to
# fix half of it.
#
# Lohit carries both scripts and shapes Devanagari properly -- conjuncts and
# all, checked against विद्यार्थी, which is where the cheaper Latin-plus-
# Devanagari faces come apart. It is the fallback and not the default because
# Noto is the better Devanagari face, and mixed lines are the exception on
# these screens rather than the rule.
MIXED_SCRIPT_FONT = "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf"
_text_cache = {}


def has_devanagari(text):
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


def has_latin(text):
    return any(("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in text or "")


def _shaping_font(text, bold):
    """The face to set `text` in. See MIXED_SCRIPT_FONT."""
    if has_latin(text) and os.path.exists(MIXED_SCRIPT_FONT):
        return MIXED_SCRIPT_FONT
    return DEVANAGARI_FONTS[bold]


def _wrap_shaped(text, font, width, max_lines=None):
    """Lines of `text` no wider than `width`, broken at spaces like Tk's width=.

    Past `max_lines` the last kept line ends in an ellipsis, trimmed a WORD at a
    time: cutting Devanagari by code point can strand a matra or a halant."""
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}" if line else word
            if line and font.getlength(trial) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        words = lines[-1].split()
        while len(words) > 1 and font.getlength(" ".join(words) + "…") > width:
            words.pop()
        lines[-1] = " ".join(words) + "…"
    return lines


def devanagari_image(text, px, bold, fill, width=None, justify="center",
                     max_lines=None):
    """Shaped text as a transparent PIL image, sized like Tk's line box, or None.

    `width`, `justify` and `max_lines` do what create_text's width= and
    justify= do, plus _ellipsize's job when max_lines is set."""
    key = (text, px, bold, fill, width, justify, max_lines)
    if key in _text_cache:
        return _text_cache[key]
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(_shaping_font(text, bold), px,
                                  layout_engine=ImageFont.Layout.RAQM)
        lines = _wrap_shaped(text, font, width, max_lines) if width else [text]
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        boxes = [font.getbbox(line or " ", anchor="ls") for line in lines]
        lengths = [max(box[2], font.getlength(line)) for box, line in zip(boxes, lines)]
        above = max(ascent, -boxes[0][1])
        below = max(descent, boxes[-1][3])
        x_off = 2 - min(0, min(box[0] for box in boxes))
        inner = math.ceil(max(lengths))
        image = Image.new("RGBA", (inner + x_off + 2,
                                   above + line_h * (len(lines) - 1) + below),
                          (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for row, (line, length) in enumerate(zip(lines, lengths)):
            x = x_off + {"left": 0, "right": inner - length}.get(
                justify, (inner - length) / 2)
            draw.text((x, above + row * line_h), line, font=font, fill=fill,
                      anchor="ls")
    except Exception as exc:
        print(f"[TEXT] Could not shape {text!r} ({exc}).", flush=True)
        image = None
    _text_cache[key] = image
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

# Where the wording sits on a mode card. Measured off the three PNGs rather than
# guessed, and identical on all of them: the name's ink runs rows 15..24, the
# two-line blurb 30..43, the chevron 40..50 at x=128, and the card body is pure
# white right through the block between them. The erase box stops at x=138 and
# row 52, which is inside the body on every row it touches -- the rounded
# corners pull the card in to x=139 at row 52 and further above and below.
MODE_ART_BLURB = (50, 27, 138, 53)   # the old two-line blurb and the chevron
# The box left for the replacement line, and where it is centred. Wide because
# it takes the chevron's room as well: 88px is what makes 9pt possible.
MODE_BLURB_BOX = 88
MODE_BLURB_CX  = 94
MODE_BLURB_CY  = 38

_mode_faces = {}

def mode_card_face(fname):
    """A mode card with the baked-in blurb and chevron erased, or None.

    The wording is PAINTED INTO these PNGs, so "stop drawing the blurb" was
    never available -- it has to come out of the picture. Two lines of 7px
    explanation in a 62px row is texture rather than text at arm's length on
    this panel, and it is replaced by one line set in Tk at 9pt, which can
    actually be read. The chevron goes with it: it sat exactly where that line
    now runs, and its 12px were the difference between fitting the wording at
    9pt and having to drop back to 8.

    The NAME is left exactly where the artwork puts it. It is lettered in three
    different colours and a serif face that self._font() has no way to match, so
    re-setting it in Tk would cost more than it gained."""
    if fname not in _mode_faces:
        art = ui_asset("Home", fname)
        if art is None:
            _mode_faces[fname] = None
            return None
        card = art.copy()
        card.paste((255, 255, 255, 255), MODE_ART_BLURB)
        _mode_faces[fname] = card
    return _mode_faces[fname]

# How far the chosen card's glow reaches past its edge.
MODE_GLOW_PAD = 10
_mode_marks = {}

def mode_chosen_marks(size, accent):
    """(glow, ring) for the chosen mode card, as RGBA the card's size plus
    MODE_GLOW_PAD all round.

    The glow goes BEHIND the card and the ring and tick ON it, so the card art
    is never touched and the press animation keeps working on it unchanged.
    Both in the mode's own colour: purple, teal or orange is the one thing a
    child who cannot read "CO-TELL" can still match to the card.

    Not the ring that was rejected before. That was a thin line around the
    card with nothing else, and it read as a stray rectangle behind it; this
    lights the card up from behind and puts a tick on it, which reads as
    "this one is on"."""
    key = (size, accent)
    if key not in _mode_marks:
        w, h = size
        g = MODE_GLOW_PAD
        box = (g, g, g + w - 1, g + h - 1)
        rgb = _rgb(accent)

        glow = Image.new("RGBA", (w + 2 * g, h + 2 * g), rgb + (0,))
        mask = Image.new("L", glow.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3), 14, fill=230)
        glow.putalpha(mask.filter(ImageFilter.GaussianBlur(4)))

        ring = Image.new("RGBA", glow.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(ring)
        draw.rounded_rectangle(box, 11, outline=rgb + (255,), width=3)
        # The tick, on the top-right corner where the artwork has nothing.
        cx, cy, r = box[2] - 3, box[1] + 3, 9
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb + (255,),
                     outline=(255, 255, 255, 255), width=2)
        draw.line((cx - 4, cy, cx - 1, cy + 3, cx + 4, cy - 3),
                  fill=(255, 255, 255, 255), width=2, joint="curve")
        _mode_marks[key] = (glow, ring)
    return _mode_marks[key]

def _pressed_image(image):
    """The same button face, dimmed, for the moment a finger is on it.

    RGB only, with the original alpha put back afterwards: blending an RGBA
    image toward black moves the ALPHA band too, which turns every transparent
    pixel around a rounded button into opaque black -- a dark square where the
    button used to be."""
    rgb = image.convert("RGB")
    dark = Image.blend(rgb, Image.new("RGB", rgb.size, (0, 0, 0)), 0.16)
    out = dark.convert("RGBA")
    if "A" in image.getbands():
        out.putalpha(image.getchannel("A"))
    return out

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

def _same_thing(a, b):
    """"Cube" and "a cube", "y = x^2 - 3" and "y = x² − 3": one button's worth."""
    def key(text):
        text = (text or "").lower().replace("²", "^2").replace("³", "^3").replace("−", "-")
        text = re.sub(r"\b(?:a|an|the)\b", " ", text)
        return re.sub(r"[\s*]+", "", text)
    return key(a) == key(b)


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
        # The picture currently on the board, at full render size, so the
        # enlarged view is the real thing rather than a blown-up thumbnail.
        self._visual_source = None
        self._visual_steps = []       # the stages of the diagram on the board
        self._step_current = -1       # which of them she is talking about
        self._step_views = {}         # the pips, per view, so both can light up
        self._graph = None            # the live graph, when one is up
        self._visual_photos = []
        self._big_visual_photos = []

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
        self._overlay_widgets = []
        self._tap_handled = None
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
        self._wb_init()
        # NO POINTER. This is a touch screen, and X draws the arrow wherever the
        # last finger landed and leaves it there, over her face or the board.
        # Set on the root and the canvas; the chat panes and everything else
        # inside inherit it, because Tk leaves a widget's cursor unset.
        root.configure(cursor="none")
        self.canvas = tk.Canvas(root, width=UI_W, height=UI_H, bd=0,
                                highlightthickness=0, bg=COL_BG, cursor="none")
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
        self._build_exit_button()
        self._build_profile_chip()
        self._build_chats_button()
        self._refresh_cards()
        self.set_now_playing(None)
        self.refresh_profile_chip()

        # Escape leaves fullscreen for good, so _hold_fullscreen stops re-asking.
        self._fullscreen_wanted = True
        self.root.bind("<Escape>", self._leave_fullscreen)
        self.root.bind("<Button-1>", self.tap_to_wake)

        self._animate()
        self._tick_clock()
        self._follow_spoken_captions()
        self.root.after(400, self._hold_fullscreen)

    # The fullscreen request at the top of __init__ is made before the window
    # exists, and labwc (through Xwayland) honours that only some of the time. On
    # the Pi 4, where startup is slow, she came up as an ordinary 800x418 window
    # parked under the 62px taskbar -- xwininfo showed no _NET_WM_STATE_FULLSCREEN
    # -- while the start before it had been fullscreen. So once the window is up,
    # check what the screen actually gave her and ask again until it is all of it.
    FULLSCREEN_TRIES = 10

    def _hold_fullscreen(self, attempt=0):
        if not self._fullscreen_wanted:
            return
        root = self.root
        try:
            filled = (root.winfo_ismapped() and root.winfo_rootx() <= 0
                      and root.winfo_rooty() <= 0
                      and root.winfo_width() >= root.winfo_screenwidth()
                      and root.winfo_height() >= root.winfo_screenheight())
            if filled:
                if attempt:
                    print(f"[UI] Fullscreen after {attempt} re-request(s).", flush=True)
                return
            if attempt >= self.FULLSCREEN_TRIES:
                print("[UI] The window manager would not make Liza fullscreen "
                      f"(window {root.winfo_width()}x{root.winfo_height()}"
                      f"+{root.winfo_rootx()}+{root.winfo_rooty()}).", flush=True)
                return
            # Off, then on: Tk sends nothing for a state it believes it is in.
            root.attributes("-fullscreen", False)
            root.after(60, lambda: self._fullscreen_wanted
                       and root.attributes("-fullscreen", True))
        except tk.TclError:
            return
        root.after(800, lambda: self._hold_fullscreen(attempt + 1))

    def _leave_fullscreen(self, event=None):
        self._fullscreen_wanted = False
        self.root.attributes("-fullscreen", False)

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

    def _round_rect(self, x0, y0, x1, y1, r, canvas=None, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
               x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return (canvas or self.canvas).create_polygon(pts, smooth=True, **kw)

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

    def _overlay_text(self, x, y, text, size, bold=False, fill=COL_TEXT,
                      anchor="center", tags=None, width=None, justify="center",
                      max_lines=None, canvas=None, photos=None):
        """canvas.create_text for modal screens, except Hindi comes out spelled
        right -- see devanagari_image. Latin text is left to Tk unchanged.

        `width` wraps like create_text's; `max_lines=1` cuts to one line with an
        ellipsis, which is what _ellipsize does for Tk text.

        `canvas` draws on a different canvas -- the scrolling panes of the chat
        history, which are canvases of their own inside this one.

        `photos` is the list that keeps a Hindi line's image alive, for text
        that is not on a modal screen and so is not swept up with one."""
        tags = tags or self.OVERLAY_TAG
        canvas = canvas or self.canvas
        if has_devanagari(text):
            px = max(1, round(size * self.root.winfo_fpixels("1p")))
            image = devanagari_image(text, px, bold, fill, width=width,
                                     justify=justify, max_lines=max_lines)
            if image is not None:
                photo = ImageTk.PhotoImage(image)
                (self._overlay_photos if photos is None else photos).append(photo)
                return canvas.create_image(x, y, image=photo, anchor=anchor,
                                           tags=tags)
        font = self._font(size, bold)
        if width and max_lines == 1:
            text, width = self._ellipsize(text, font, width), None
        return canvas.create_text(x, y, text=text, font=font, fill=fill,
                                       anchor=anchor, tags=tags, width=width or 0,
                                       justify=justify)

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

        Laid out on CARD_ROW1..3 and CLOCK_SPLIT, which is where the reasoning
        for the numbers lives. Both halves take the same shape -- icon and
        headline, then two plain rows -- so the card reads as one thing with two
        readings in it rather than as two crowded panels sharing an edge.
        """
        art = ui_asset("Home", "Weather_bg.png", box=CARD_BOX)
        if art is not None:
            self._place_asset(art, CLOCK_X0, TOP_Y0)
        else:
            self._card(CLOCK_X0, TOP_Y0, CLOCK_X1, TOP_Y1, 16)

        # ---- left: time, date, place ----
        # THERE IS NO ANALOGUE DIAL. It read the same time as the digits beside
        # it and cost 38px of a 204px card to do it -- the only thing on here
        # carrying no reading of its own. Without it all three lines start from
        # the card's own margin, which is why they line up now.
        #
        # 16pt: three line boxes have to fit the 55px the card is actually
        # opaque for, and 16/7/6 is 30+13+12 = 55. See CARD_INK_TOP.
        self.clock_id = self.canvas.create_text(
            CLOCK_X0 + 8, CARD_ROW1, text="--:--", anchor="w",
            font=self._font(16, True), fill=COL_CARD_TEXT)
        # Dropped a little, so it sits on the time's baseline rather than its
        # middle. Placed by measurement in _tick_clock; see there.
        self.meridiem_id = self.canvas.create_text(
            CLOCK_X0 + 8, CARD_ROW1 + 5, text="", anchor="w",
            font=self._font(8, True), fill=COL_CARD_DIM)
        self.date_id = self.canvas.create_text(
            CLOCK_X0 + 8, CARD_ROW2, text="", anchor="w",
            font=self._font(7), fill=COL_CARD_DIM)
        self.city_id = self.canvas.create_text(
            CLOCK_X0 + 8, CARD_ROW3, anchor="w",
            text=assistant.WEATHER_CITY if assistant.WEATHER_API_KEY else "No weather",
            font=self._font(6), fill=COL_CARD_DIM)

        # ---- the divider ----
        # A rule rather than a second box: an outline inside a card this small
        # reads as clutter. Full height now, because both columns have something
        # on all three rows.
        self.canvas.create_line(CLOCK_SPLIT, TOP_Y0 + 7, CLOCK_SPLIT, TOP_Y1 - 7,
                                fill=COL_CARD_EDGE)

        # ---- right: the condition glyph, on the headline row ----
        self.weather_glyph = []
        # THE GLYPH IS TWO ROWS TALL, and that is what fills the space the
        # condition used to leave. The condition was left-aligned under the
        # glyph while the temperature sat off to the right, so the card had a
        # visible hole under its biggest number. Moving the condition beneath
        # the temperature closes it and leaves this column free, so the glyph
        # takes the height instead of a 30px mark floating in a 44px space.
        #
        # Anchored on ROW1 rather than centred between the rows: the shapes hang
        # BELOW their anchor (rain to +19, a storm bolt to +29) and reach only
        # -18 above it, so an anchor at the visual centre would push the bottom
        # of a storm past the card's ink.
        self.weather_glyph_at = (CLOCK_SPLIT + 15, CARD_ROW1 + 3)


        # ---- right: temperature, condition, high and low ----
        # 16pt, the same as the time. It was cut to 12 to survive the old layout,
        # where the clear-sky sun reached to within 3px of where this starts, and
        # the width the dial gave up is what pays for the rest. Matching the time
        # is deliberate: this card has two readings on it, not a heading and a
        # footnote, and "12:34" is five characters against three so the time
        # still carries the eye.
        self.temp_id = self.canvas.create_text(
            CLOCK_SPLIT + 32, CARD_ROW1, text="--", anchor="w",
            font=self._font(16, True), fill=COL_CARD_TEXT)
        # DIRECTLY UNDER THE TEMPERATURE, sharing its left edge. Left-aligned to
        # the column it sat beneath the glyph instead, which left the space
        # below the largest number on the card empty and made the two halves of
        # the weather reading look unrelated.
        self.desc_id = self.canvas.create_text(
            CLOCK_SPLIT + 32, CARD_ROW2, text="", anchor="w",
            font=self._font(7), fill=COL_CARD_DIM)

        # High and low share the bottom row, mirroring the place line opposite.
        # 6pt, matching the place line opposite: the bottom row has 12px of ink
        # to sit in and 7pt needs 13. The arrows are drawn +/-6 about the row, so
        # this is also what lifted them off the card's bottom edge.
        self._arrow(CLOCK_SPLIT + 12, CARD_ROW3, up=True)
        self.high_id = self.canvas.create_text(
            CLOCK_SPLIT + 19, CARD_ROW3, text="--", anchor="w",
            font=self._font(6, True), fill=COL_CARD_DIM)
        self._arrow(CLOCK_SPLIT + 48, CARD_ROW3, up=False)
        self.low_id = self.canvas.create_text(
            CLOCK_SPLIT + 55, CARD_ROW3, text="--", anchor="w",
            font=self._font(6, True), fill=COL_CARD_DIM)
        self._draw_weather_glyph("01d")

    def _arrow(self, x, y, up):
        tip = y - 6 if up else y + 6
        # The "low" arrow was #3B82F6, which is a mid blue on what is now a mid
        # blue card -- the shape simply stopped being there. Darkened until it
        # reads against both the artwork and the white glow that crosses it.
        colour = "#D42A2A" if up else "#12459E"
        self.canvas.create_line(x, y + (6 if up else -6), x, tip, fill=colour, width=2)
        self.canvas.create_polygon(x - 4, tip + (4 if up else -4), x + 4, tip + (4 if up else -4),
                                   x, tip, fill=colour, outline="")

    def _draw_weather_glyph(self, code):
        """The condition mark. Drawn full size, then scaled to its row.

        Every shape below is in the coordinates it was designed at -- the sun is
        11 across with rays to 19, a storm bolt hangs to +29 -- which is 38x47
        and does not fit a 32px row in a 78px column. Scaling afterwards, about
        the glyph's own centre, keeps all of that arithmetic readable and puts
        the whole adjustment in one constant. Tk scales coordinates and not line
        widths, which is what we want: the 2px strokes stay legible at 62%.
        """
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
            self._scale_weather_glyph()
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
            self._scale_weather_glyph()
            return

        add(c.create_oval(cx - 16, cy - 5, cx + 1, cy + 9, fill=cloud, outline=""))
        add(c.create_oval(cx - 6, cy - 12, cx + 12, cy + 7, fill=cloud, outline=""))
        add(c.create_rectangle(cx - 14, cy + 1, cx + 11, cy + 9, fill=cloud, outline=""))
        self._scale_weather_glyph()

    def _scale_weather_glyph(self):
        """Shrink whatever was just drawn to WEATHER_GLYPH_SCALE, in place.

        Called from each of the three exits of _draw_weather_glyph rather than
        wrapped around it, because two of the branches return early and a glyph
        that skipped this would be drawn at full size -- 38px across a 78px
        column, straight through the temperature beside it."""
        cx, cy = self.weather_glyph_at
        for item in self.weather_glyph:
            self.canvas.scale(item, cx, cy, WEATHER_GLYPH_SCALE, WEATHER_GLYPH_SCALE)

    # ---------- music player ----------
    def _build_music_card(self):
        """The player, laid out across the header rather than down a column.

        Wider than it is tall now, so the transport moves to the right of the
        title instead of under it, and the progress bar runs the full width
        along the bottom where there is nothing to compete with it.
        """
        card_art = ui_asset("Home", "Music_bg.png", box=CARD_BOX)
        if card_art is not None:
            self._place_asset(card_art, MUSIC_X0, TOP_Y0)
        else:
            self._card(MUSIC_X0, TOP_Y0, MUSIC_X1, TOP_Y1, 16)

        # THIS CARD LOST A THIRD OF ITS WIDTH.
        #
        # The old contents -- album art, a MUSIC PLAYER caption, a title, an
        # artist line and the transport, side by side -- were laid out for a
        # 315px card. This one is 204, and all of it landed on top of itself.
        # The mockup gives the whole card to the transport and a progress bar,
        # which leaves no room for four labels, so what is kept is what somebody
        # actually needs from across a room: WHAT is playing, how far through it
        # is, and the three buttons. The artist, the caption and the album
        # thumbnail go; the elapsed and duration figures go with them, because
        # the bar already says that and the title is worth more than the digits.
        self._music_compact = card_art is not None
        if self._music_compact:
            self.track_id = self.canvas.create_text(
                MUSIC_X0 + 10, TOP_Y1 - 15, text="", anchor="w",
                font=self._font(8, True), fill=COL_TEXT)
            # Still created, because set_now_playing and set_media_progress
            # write to them; simply never shown on this layout.
            self.artist_id = self.canvas.create_text(
                MUSIC_X0 + 10, TOP_Y1 - 15, text="", anchor="w",
                font=self._font(7), fill=COL_CARD_DIM, state="hidden")
        else:
            album = ImageTk.PhotoImage(_album_art_image(38, 10))
            self._photos.append(album)
            self.canvas.create_image(MUSIC_X0 + 12, TOP_Y0 + 11, image=album, anchor="nw")
            self.canvas.create_text(MUSIC_X0 + 60, TOP_Y0 + 15, text="MUSIC PLAYER",
                                    anchor="w", font=self._font(7, True), fill=COL_CARD_DIM)
            self.track_id = self.canvas.create_text(
                MUSIC_X0 + 60, TOP_Y0 + 30, text="", anchor="w",
                font=self._font(9, True), fill=COL_TEXT)
            self.artist_id = self.canvas.create_text(
                MUSIC_X0 + 60, TOP_Y0 + 45, text="", anchor="w",
                font=self._font(7), fill=COL_CARD_DIM)

        # Transport on the right, with the play/pause ring biggest: it is the
        # one of the three that is pressed, and the only one that changes shape.
        cy = TOP_Y0 + 30
        pcx = MUSIC_X1 - 60
        # Measured off the mockup: prev at 601, play/pause at 644, next at 690,
        # all centred on y40. The skip icon is one asset used twice, mirrored
        # for prev, because the folder ships the forward one only.
        skip = ui_asset("Home", "Next_button.png")
        self.play_art = None
        if skip is not None:
            self.prev_items = [self._place_asset(
                skip.transpose(Image.FLIP_LEFT_RIGHT), 595, 34, "prevbtn")]
            self.next_items = [self._place_asset(skip, 684, 34, "nextbtn")]
        else:
            self.prev_items = self._skip_glyph(pcx - 34, cy, forward=False)
            self.next_items = self._skip_glyph(pcx + 34, cy, forward=True)
        # Shuffle and repeat have nowhere to go at this height and were never
        # wired to anything, so the transport is only what actually works.
        self.shuffle_items = []
        self.repeat_items = []

        pause_art = ui_asset("Home", "Pause_button.png")
        if pause_art is not None:
            # One item whose picture is swapped, rather than a ring with two
            # shapes shown and hidden over it: the artwork carries its own
            # circle, so there is nothing left to draw on top of.
            self.play_art = self._place_asset(pause_art, 632, 28, "playpause")
            self.play_ring = self.play_left = self.play_right = self.play_tri = None
        else:
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

        if not self._music_compact:
            self._bars_glyph(MUSIC_X1 - 16, TOP_Y0 + 16, COL_CARD_DIM, (4, 7, 10, 7, 4))

        if self._music_compact:
            # Right of the title, along the bottom, as the mockup has it.
            bx0, bx1, by = MUSIC_X0 + 105, MUSIC_X1 - 10, TOP_Y1 - 14
        else:
            bx0, bx1, by = MUSIC_X0 + 58, MUSIC_X1 - 46, TOP_Y1 - 15
        self._progress_span = (bx0, bx1, by)
        self._round_rect(bx0, by - 2, bx1, by + 2, 2, fill=COL_TRACK, outline="")
        self.progress_fill = self._round_rect(bx0, by - 2, bx0 + 1, by + 2, 2,
                                              fill=COL_INDIGO, outline="")
        self.progress_knob = self.canvas.create_oval(bx0 - 4, by - 4, bx0 + 4, by + 4,
                                                     fill=COL_INDIGO, outline=COL_CARD, width=2)
        hide = "hidden" if self._music_compact else "normal"
        self.elapsed_id = self.canvas.create_text(bx0 - 6, by, text="00:00", anchor="e",
                                                  font=self._font(6), fill=COL_CARD_DIM,
                                                  state=hide)
        self.duration_id = self.canvas.create_text(bx1 + 6, by, text="00:00", anchor="w",
                                                   font=self._font(6), fill=COL_CARD_DIM,
                                                   state=hide)

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
        # THE HEADING IS WHERE THE CURRENT MODE IS NAMED.
        #
        # It used to read a fixed "MODE", and which of the three was in use was
        # marked on the cards themselves -- first with a ring around the chosen
        # one, then by washing the other two toward white. Neither survived
        # contact with the device: at 145x62 a card has no room to give away to
        # a border, and the wash read as blur rather than as "not this one".
        # Saying it here instead costs the cards nothing and is the one place on
        # this panel that was already just a label.
        #
        # 13pt, not the 15 the fixed word was set at: "CO-TELL MODE" measures
        # 129px against the 153px the panel is actually opaque for at this row,
        # and 15pt puts it at 146px, which touches both edges.
        if panel is not None:
            self._place_asset(panel, MODES_X0, MODES_Y0)
            # The panel art carries no wording, so the heading is still drawn --
            # over the rainbow, which is why it is dark rather than tinted.
            self.mode_heading = self.canvas.create_text(
                (MODES_X0 + MODES_X1) / 2, MODES_Y0 + 19,
                text=f"{self.current_mode} MODE", font=self._font(13, True),
                fill="#3A2E6E")
        else:
            self._card(MODES_X0, MODES_Y0, MODES_X1, MODES_Y1, 16)
            self.mode_heading = self.canvas.create_text(
                (MODES_X0 + MODES_X1) / 2, MODES_Y0 + 18,
                text=f"{self.current_mode} MODE", font=self._font(9, True),
                fill=COL_TEXT)
        # The two little diamonds that used to sit up here are gone with the
        # short heading. They lived at x=141..156 on the heading's own row,
        # which is inside the width the mode name now needs; decoration that
        # only fitted while the label said nothing is not worth the collision.

        self.cards = []
        for i, mode in enumerate(self.modes):
            y0 = MODE_CARD_Y0 + i * (MODE_CARD_H + MODE_CARD_GAP)
            y1 = y0 + MODE_CARD_H
            accent = MODE_ACCENTS[mode]
            tag = f"mode{i}"

            art = mode_card_face(self.MODE_ART[mode])
            if art is not None:
                # EVERY CARD AT FULL STRENGTH. The chosen one used to be marked
                # by washing the other two toward white, and before that by a
                # ring around it. Both were rejected for the same reason from
                # opposite directions -- the ring read as a stray rectangle
                # behind the button, and the wash made two of the three look
                # blurred, as though something translucent had been laid over
                # them. There is no marking on the cards now; see MODE_BLURBS
                # for the wording that replaced the unreadable painted-in one.
                item = self._place_asset(art, MODE_CARD_X0, y0, tag)
                # Set in Tk rather than painted into the picture: this is the
                # line that has to be READ, and Tk is the only half of this
                # screen that can lay type out at a size chosen against the
                # measured width of the box it has to sit in.
                self.canvas.create_text(
                    MODE_CARD_X0 + MODE_BLURB_CX, y0 + MODE_BLURB_CY,
                    text=MODE_BLURBS[mode], anchor="center",
                    width=MODE_BLURB_BOX, justify="center",
                    font=self._font(9), fill=COL_TEXT, tags=tag)
                # The chosen card's glow and tick, hidden on the other two. The
                # card's own tag, so they dip with it under a finger and a tap
                # on the ring still lands on the card.
                glow_img, ring_img = mode_chosen_marks(art.size, accent)
                at = (MODE_CARD_X0 - MODE_GLOW_PAD, y0 - MODE_GLOW_PAD)
                glow = self._place_asset(glow_img, *at, tags=(tag,))
                ring = self._place_asset(ring_img, *at, tags=(tag,))
                blurb = self.canvas.find_withtag(tag)[1]
                self._press_feedback(tag, item, art,
                                     lambda e, idx=i: self._mode_tapped(e, idx))
                self.cards.append({"body": None, "title": None, "blurb": blurb,
                                   "chevron": None, "item": item,
                                   "glow": glow, "ring": ring,
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
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, idx=i: self._mode_tapped(e, idx))
            self.cards.append({"body": body, "title": title, "blurb": blurb,
                               "chevron": chevron, "item": None,
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
        # Only with a camera to switch: a button that can only ever say "not
        # connected" is one more thing on the screen that does nothing.
        if camera.sensor():
            self.set_camera_button(False)
        # And in the other corner, the way to write on it: the whole board,
        # full screen. See the whiteboard section.
        self.set_board_button()

    # ---------- action buttons ----------
    BUTTON_ART = {"SPEAK": "Speak Button.png", "STOP": "Stop Button.png",
                  "SLEEP": "Sleep Button.png"}

    # How far a button sinks under a finger. Small on purpose: the artwork has
    # its own drop shadow painted in, so a big movement reads as the button
    # coming apart rather than as it going down.
    PRESS_DIP = 2
    # A press that never gets its release puts the button back anyway. Touch
    # drivers do lose one now and then, and a button left dimmed and 2px low
    # looks broken for as long as the screen is up.
    PRESS_RESET_MS = 600

    def _press_feedback(self, tag, item, face, command, restore=None):
        """Make one button look pressed while a finger is on it, then act.

        A canvas image does not change under a touch by itself, and with no
        cursor and no hover on this panel that left the three big buttons
        feeling dead -- the only sign a tap had landed was whatever Liza did
        several seconds later. The face is swapped for a dimmed copy and the
        whole button dropped PRESS_DIP pixels, and put back on release.

        The action is called from HERE rather than bound separately, because
        <Button-1> and <ButtonPress-1> are the same Tk event: a second
        tag_bind for it REPLACES this one instead of running beside it.

        `restore` puts the face back on release. It is a callable rather than
        the remembered image because a mode card's action CHANGES which face it
        should be showing -- restoring the one captured on the way down would
        undo the selection the press just made.
        """
        pressed = ImageTk.PhotoImage(_pressed_image(face))
        self._photos.append(pressed)
        if restore is None:
            was = self.canvas.itemcget(item, "image")
            restore = lambda: self.canvas.itemconfigure(item, image=was)
        sunk = []

        def up(event=None):
            if sunk:
                sunk.clear()
                restore()
                # By tag, not by item: on the drawn fallback the label and
                # sub-label are separate items on the same button.
                self.canvas.move(tag, 0, -self.PRESS_DIP)

        def down(event=None):
            if not sunk:
                sunk.append(True)
                self.canvas.itemconfigure(item, image=pressed)
                self.canvas.move(tag, 0, self.PRESS_DIP)
                self.root.after(self.PRESS_RESET_MS, up)
            return command(event)

        self.canvas.tag_bind(tag, "<ButtonPress-1>", down)
        self.canvas.tag_bind(tag, "<ButtonRelease-1>", up)

    def _build_buttons(self):
        self.buttons = {}
        handlers = {"SPEAK": self.wake_up, "STOP": self.stop_speaking, "SLEEP": self.go_to_sleep}
        for (label, sub, c0, c1, sub_col, icon), x0 in zip(ACTIONS, BTN_XS):
            tag = f"btn{label}"
            art = ui_asset("Home", self.BUTTON_ART[label])
            if art is not None:
                # Label, sub-label and icon are painted into the artwork, so
                # nothing is written over it.
                face = art
                item = self._place_asset(art, x0, BTN_Y0, tag)
            else:
                face = _action_image(BTN_W, BTN_H, 20, c0, c1, icon)
                item = self._place_photo(face, x0, BTN_Y0, tag)
                # 70, not 92: the icon ring ends 57px in, so this is the same
                # clearance beside a narrower button.
                self.canvas.create_text(x0 + 70, BTN_Y0 + 26, text=label, anchor="w",
                                        font=self._font(15, True), fill="#FFFFFF", tags=tag)
                self.canvas.create_text(x0 + 70, BTN_Y0 + 45, text=sub, anchor="w",
                                        font=self._font(7), fill=sub_col, tags=tag)
            self._press_feedback(tag, item, face, handlers[label])
            self.buttons[label] = tag

    # Two taps, because this panel reports stray touches on its own (see
    # tap_to_wake) and one of those should not be able to shut her down.
    EXIT_CONFIRM_MS = 4000

    def _build_exit_button(self):
        """The way off the screen: she is fullscreen, with no title bar and no
        keyboard, so without this only the Stop Liza desktop icon (which she
        covers) or `liza stop` from another machine could close her."""
        x0, y0, x1, y1 = 660, BTN_Y0 + 14, 784, BTN_Y0 + BTN_H - 14
        self._exit_armed = None
        self._exit_box = self._round_rect(x0, y0, x1, y1, 18, fill="#1E293B",
                                          outline="#FFFFFF", width=2, tags="btnEXIT")
        self._exit_label = self.canvas.create_text(
            (x0 + x1) // 2, (y0 + y1) // 2, text="✕  EXIT",
            font=self._font(13, True), fill="#FFFFFF", tags="btnEXIT")
        self.canvas.tag_bind("btnEXIT", "<ButtonPress-1>", self._exit_tapped)

    def _exit_tapped(self, event=None):
        if self._exit_armed is None:
            self.canvas.itemconfigure(self._exit_box, fill="#DC2626")
            self.canvas.itemconfigure(self._exit_label, text="Tap again",
                                      font=self._font(11, True))
            self._exit_armed = self.root.after(self.EXIT_CONFIRM_MS, self._disarm_exit)
            return "break"
        self.root.after_cancel(self._exit_armed)
        self.canvas.itemconfigure(self._exit_label, text="Closing…")
        print("[UI] Exit tapped twice. Closing Liza...", flush=True)
        # SIGTERM, not root.destroy(): assistant._cleanup_and_exit is what
        # releases the microphone and stops mpv, exactly as `liza stop` does.
        self.root.after(150, lambda: os.kill(os.getpid(), signal.SIGTERM))
        return "break"

    def _disarm_exit(self):
        self._exit_armed = None
        self.canvas.itemconfigure(self._exit_box, fill="#1E293B")
        self.canvas.itemconfigure(self._exit_label, text="✕  EXIT",
                                  font=self._font(13, True))

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
        # Under the full-screen page she cannot be seen, and every new frame
        # would only make Tk repaint the page over her.
        if frames and not self._wb_open:
            # Asleep she should look settled, not idling, so the loop is parked.
            if self.current_state != "sleeping":
                self.mascot_index = (self.mascot_index + 1) % len(frames)
            self.canvas.itemconfig(self.mascot_item, image=frames[self.mascot_index])
        if self._wb_open:
            self._wb_tick_state()
            if self.frame % 8 == 0:
                self._wb_guard()

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
        # Which mode is in use is said in the panel heading; see _build_mode_cards.
        heading = getattr(self, "mode_heading", None)
        if heading is not None:
            self.canvas.itemconfigure(heading, text=f"{self.current_mode} MODE")
        for i, card in enumerate(self.cards):
            chosen = i == self.current_mode_index
            accent = card["accent"]
            if card.get("body") is None:
                # An artwork card. The card itself is never restyled -- all
                # three stay at full strength, since washing the other two out
                # read as blur -- the chosen one is lit from behind and ticked.
                shown = "normal" if chosen else "hidden"
                self.canvas.itemconfigure(card["glow"], state=shown)
                self.canvas.itemconfigure(card["ring"], state=shown)
                if chosen:
                    # Under all three cards, so it glows round the edge of its
                    # own card without covering either neighbour.
                    self.canvas.tag_lower(card["glow"], self.cards[0]["item"])
                    self.canvas.tag_raise(card["ring"], card["blurb"])
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
        # 170 was the room the album art and the transport left on the old,
        # wider card. On the narrow one the title sits alone on the bottom row,
        # left of the progress bar, and has 89px rather than 34.
        if getattr(self, "_music_compact", False):
            room, face = 89, self._font(8, True)
        else:
            room, face = MUSIC_X1 - MUSIC_X0 - 170, self._font(9, True)
        self.canvas.itemconfig(
            self.track_id, text=self._ellipsize(track.strip(), face, room),
            fill=COL_CARD_TEXT if title else COL_CARD_DIM)
        self.canvas.itemconfig(
            self.artist_id,
            text=self._ellipsize("Loading…" if loading else
                                 (artist.strip() or ("—" if title else "Ask me to play a song")),
                                 self._font(7), MUSIC_X1 - MUSIC_X0 - 170))
        self.set_media_progress(0.0, 0.0, False)

    def set_media_progress(self, pos, dur, paused):
        self.media.update({"pos": pos or 0.0, "dur": dur or 0.0, "paused": bool(paused)})
        bx0, bx1, by = self._progress_span
        # mpv reports no position for a moment at the start of a stream.
        fraction = ((pos or 0.0) / dur) if dur else 0.0
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
        paused = self.media["paused"]
        if getattr(self, "play_art", None) is not None:
            # Paused shows PLAY, because that is what pressing it will do.
            art = ui_asset("Home", "Play_button.png" if paused else "Pause_button.png")
            if art is not None and art is not getattr(self, "_play_art_shown", None):
                self._play_art_photo = ImageTk.PhotoImage(art)
                self.canvas.itemconfigure(self.play_art, image=self._play_art_photo)
                self._play_art_shown = art
        else:
            self.canvas.itemconfig(self.play_ring, fill=shade)
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
            # She fills the screen in 3D mode; the page cannot be under her.
            self.close_whiteboard()
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
        # "26°" and not "26°C": at 15pt bold the unit made this 46px wide from
        # x+177, which is 322 on a card that ends at 303 -- the reading ran off
        # the edge onto the wallpaper the moment real weather arrived, and the
        # placeholder "--" is why that never showed up in a screenshot. Dropping
        # the C fits it inside and matches the high and low below, which have
        # always been bare degrees.
        self.canvas.itemconfig(self.temp_id, text=f"{reading['temp']}°")
        # Clipped to the column. OpenWeather says "Thunderstorm" as readily as
        # "Rain", and that is 58px at 7pt against the 70 this column has -- fine
        # here, but the next word longer would have run onto the profile card.
        self.canvas.itemconfig(self.desc_id, text=self._ellipsize(
            reading["desc"], self._font(7), CLOCK_X1 - 6 - (CLOCK_SPLIT + 32)))
        self.canvas.itemconfig(self.high_id, text=f"{reading['high']}°")
        self.canvas.itemconfig(self.low_id, text=f"{reading['low']}°")
        # Single spaces around the bullet, and clipped to the column: the
        # double-spaced form is 89px at 6pt and the column holds 87, so Delhi in
        # monsoon -- 100% humidity, the widest this line ever gets -- was the one
        # reading that would have run into the divider.
        self.canvas.itemconfig(self.city_id, text=self._ellipsize(
            f"{reading['city']} • Humidity {reading['humidity']}%",
            self._font(6), CLOCK_SPLIT - 6 - (CLOCK_X0 + 8)))
        self._draw_weather_glyph(reading["icon"])

    def _tick_clock(self):
        now = datetime.now()
        text = now.strftime("%I:%M").lstrip("0")
        self.canvas.itemconfig(self.clock_id, text=text)
        # Placed by measurement rather than a fixed offset: "9:05" and "12:45"
        # are very different widths and the meridiem has to sit against both.
        self.canvas.coords(self.meridiem_id,
                           CLOCK_X0 + 12 + self._font(16, True).measure(text),
                           CARD_ROW1 + 5)
        self.canvas.itemconfig(self.meridiem_id, text=now.strftime("%p"))
        self.canvas.itemconfig(self.date_id, text=now.strftime("%a, %d %b %Y"))

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
        # Clear of the status dots, which start at BOARD_Y1 - 19 -- and of the
        # buttons she has offered, when there are any.
        bottom = BOARD_Y1 - (58 if self._offers else 24)
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

    # How many of the caption poller's ticks have gone by; only used to keep the
    # (session, index) pair it last showed.
    _caption_shown = (0, -1)

    def _follow_spoken_captions(self):
        """Put the line she is SAYING on the board, not the line she is about to.

        The panel used to be written from the audio worker, at the moment a
        sentence was handed to Cartesia. That is one whole sentence of
        generation AHEAD of the speaker, so the board ran in front of the voice
        -- and on a long answer it was showing the last sentence while the first
        was still playing, which is what makes it read as a wall of text rather
        than as a caption.

        The player already timestamps every sentence onto the playback timeline
        for the story screen (see audio.caption_cue). This reads the same clock,
        so the board and the voice are the same thing.
        """
        try:
            session = audio.caption_session()
            index, text = audio.caption_now(session)
            if index >= 0 and text and (session, index) != self._caption_shown:
                self._caption_shown = (session, index)
                self.set_transcript(text, "liza")
                # The same line that reaches the board decides which stage is
                # lit, so the pip moves exactly when she says the word.
                self._match_step(text)
                self._wb_match_step(text)
        except Exception:
            # A caption is never worth taking the screen down for.
            pass
        try:
            self._update_stop_pill()
        except Exception:
            pass
        self.root.after(120, self._follow_spoken_captions)

    # A STOP BUTTON OVER WHATEVER IS FULL SCREEN, WHILE SHE TALKS.
    #
    # The STOP control is on the home screen, and a 3D model, a picture or a
    # graph opened full screen covers all of it -- so with a model up there was
    # no way at all to cut her off: "the user has to wait until Liza completes".
    # Shown only while she is speaking, top centre, clear of the title on the
    # left and the close cross on the right. The microphone is already live, so
    # stopping her is also the way to start talking.
    STOP_PILL_TAG = "stoppill"
    FULL_SCREEN_TAGS = ("big3d", "bigvisual", "biggraph", "wboard", "geolab")

    def _update_stop_pill(self):
        speaking = playback_active.is_set() or not audio_queue.empty()
        covered = any(self.canvas.find_withtag(t) for t in self.FULL_SCREEN_TAGS)
        shown = bool(self.canvas.find_withtag(self.STOP_PILL_TAG))
        if not (speaking and covered):
            if shown:
                self.canvas.delete(self.STOP_PILL_TAG)
            return
        if not shown:
            cx, cy, half = UI_W / 2, 22, 78
            self._round_rect(cx - half, cy - 17, cx + half, cy + 17, 16, fill="#DC2626",
                             outline="#FCA5A5", tags=self.STOP_PILL_TAG)
            self.canvas.create_text(cx, cy, text="\u25A0  Stop talking", fill="#FFFFFF",
                                    font=self._font(11, bold=True), tags=self.STOP_PILL_TAG)
            self.canvas.tag_bind(self.STOP_PILL_TAG, "<Button-1>", self.stop_speaking)
        # A frame of the model or a redrawn view can land on top of it.
        self.canvas.tag_raise(self.STOP_PILL_TAG)

    # Where a diagram lands on the board: the whole of the writing area, from
    # under the painted heading to just above the status dots. 309x184, which is
    # why visuals.py draws at 760x420 and this scales down -- a diagram composed
    # for this box would be unreadable enlarged, and one composed large reads at
    # both sizes.
    VISUAL_BOX = (BOARD_X0 + 10, BOARD_Y0 + 56, BOARD_X1 - 10, BOARD_Y1 - 26)
    VISUAL_TAG = "boardvisual"
    BIG_VISUAL_TAG = "bigvisual"

    # The board picture is 309x184 and the way into it used to be seven pixels
    # of faint grey reading "tap to enlarge", which is not an affordance so much
    # as a rumour. These are drawn from canvas primitives rather than PNG art so
    # that a missing asset cannot leave a picture with no visible way to open it.
    ICON_R = 15          # half the tap target: 30px square, sized for a child
    ICON_GLYPH = 9       # half the glyph inside it
    ICON_ARM = 6         # length of each corner-bracket arm

    def _expand_icon(self, cx, cy, tag):
        """Four corner brackets -- the fullscreen glyph -- on a white chip.

        The chip is not decoration: the glyph sits on top of whatever the
        picture happens to be, and indigo on a dark photograph is unreadable.
        """
        self._round_rect(cx - self.ICON_R, cy - self.ICON_R,
                         cx + self.ICON_R, cy + self.ICON_R, 8,
                         fill="#FFFFFF", outline="#D8E0F0", tags=tag)
        g, a = self.ICON_GLYPH, self.ICON_ARM
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x, y = cx + g * sx, cy + g * sy
            self.canvas.create_line(x, y, x - a * sx, y, fill=COL_INDIGO,
                                    width=2, capstyle="round", tags=tag)
            self.canvas.create_line(x, y, x, y - a * sy, fill=COL_INDIGO,
                                    width=2, capstyle="round", tags=tag)

    def _close_icon(self, cx, cy, tag):
        """A cross, in the same rose the Stop button uses.

        Whatever else a child cannot read yet, they can read this one.
        """
        self._round_rect(cx - self.ICON_R, cy - self.ICON_R,
                         cx + self.ICON_R, cy + self.ICON_R, 8,
                         fill="#FFFFFF", outline="#D8E0F0", tags=tag)
        g = self.ICON_GLYPH - 1
        for dx in (-1, 1):
            self.canvas.create_line(cx - g * dx, cy - g, cx + g * dx, cy + g,
                                    fill=COL_STOP, width=3, capstyle="round",
                                    tags=tag)

    def show_visual(self, path, steps=None, enlarge=None):
        """Put a rendered diagram, equation, graph or picture on the board.

        `enlarge` opens it full screen as well. Left as None, that happens when
        the board is open full screen to write on, where a picture put on the
        small board underneath would never be seen.

        `steps` are the stages the diagram was asked to show, in order. They are
        drawn as a strip of pips under the picture and one of them lights up as
        she talks about it -- see _match_step. A Seedream diagram is flat pixels
        with nothing addressable inside it, so the strip is the only place a
        highlight can live.

        Drawn as ordinary canvas items with their own tag rather than through
        the overlay machinery, and that is not a style choice: _overlay_screen
        sets self.overlay, kg_holds_microphone reads it, and ai_loop parks the
        microphone for as long as it is set. A picture on the board that stopped
        her listening would be a strange kind of answer.
        """
        try:
            image = Image.open(path).convert("RGB")
        except Exception as exc:
            print(f"[UI] Could not open the visual ({exc}).", flush=True)
            return
        # The board is wanted for this, and the camera's picture would sit on
        # top of it. The student turns it back on by asking her to look.
        camera.stop_live("a picture is going on the board")
        self.canvas.delete(self.VISUAL_TAG)
        self._drop_graph()
        self._drop_geometry()
        app_state.current_graph = None
        self._visual_source = image
        self._visual_steps = list(steps or [])
        self._step_current = -1
        self._step_views = {}
        x0, y0, x1, y1 = self.VISUAL_BOX
        if self._offers:
            # Buttons she offered are standing along the foot of the board:
            # the picture goes above them rather than under them.
            y1 = self.OFFER_ROW[1] - 8
        strip = self.STEP_STRIP_H if self._visual_steps else 0
        fitted = image.copy()
        fitted.thumbnail((x1 - x0, y1 - y0 - strip), Image.LANCZOS)
        photo = ImageTk.PhotoImage(fitted)
        self._visual_photos = [photo]        # Tk keeps no reference of its own
        cx, cy = (x0 + x1) / 2, (y0 + y1 - strip) / 2
        self._round_rect(cx - fitted.width / 2 - 4, cy - fitted.height / 2 - 4,
                         cx + fitted.width / 2 + 4, cy + fitted.height / 2 + 4,
                         10, fill="#FFFFFF", outline="#D8E0F0",
                         tags=self.VISUAL_TAG)
        self.canvas.create_image(cx, cy, image=photo, anchor="center",
                                 tags=self.VISUAL_TAG)
        # Drawn after the image so it sits on top of it, and inside VISUAL_TAG
        # so the tag_bind below makes it tappable and clear_visual sweeps it up.
        # The whole picture stays tappable too -- this marks the door, it does
        # not narrow it.
        self._expand_icon(cx + fitted.width / 2 - 17,
                          cy - fitted.height / 2 + 17, self.VISUAL_TAG)
        if self._visual_steps:
            self._draw_step_strip((x0, y1 - strip, x1, y1), self.VISUAL_TAG,
                                  small=True)
        self.canvas.tag_bind(self.VISUAL_TAG, "<Button-1>", self._enlarge_visual)
        print(f"[UI] Visual on the board: {os.path.basename(path)}"
              + (f", {len(self._visual_steps)} stages" if self._visual_steps else ""),
              flush=True)
        if enlarge or (enlarge is None and self._wb_open):
            self._enlarge_visual()
        self._restack()

    def _enlarge_visual(self, event=None):
        """The same picture, over the whole screen, until it is tapped again.

        Deliberately NOT an overlay: see show_visual. It is one raised image and
        a caption, and the next tap anywhere takes it away.
        """
        image = getattr(self, "_visual_source", None)
        if image is None:
            return "break"
        self.canvas.delete(self.BIG_VISUAL_TAG)
        self._step_views.pop(self.BIG_VISUAL_TAG, None)
        strip = self.BIG_STEP_STRIP_H if self._visual_steps else 0
        big = image.copy()
        big.thumbnail((UI_W - 40, UI_H - 54 - strip), Image.LANCZOS)
        photo = ImageTk.PhotoImage(big)
        self._big_visual_photos = [photo]
        self.canvas.create_rectangle(0, 0, UI_W, UI_H, fill="#0B1020",
                                     outline="", tags=self.BIG_VISUAL_TAG)
        self.canvas.create_image(UI_W / 2, (UI_H - 26 - strip) / 2, image=photo,
                                 anchor="center", tags=self.BIG_VISUAL_TAG)
        self.canvas.create_text(UI_W / 2, UI_H - 16, text="tap anywhere to close",
                                font=self._font(9), fill="#8891A8",
                                tags=self.BIG_VISUAL_TAG)
        if self._visual_steps:
            self._draw_step_strip((0, UI_H - 30 - strip, UI_W, UI_H - 30),
                                  self.BIG_VISUAL_TAG, small=False)
        # Tapping anywhere still closes this. The cross is for the child who
        # does not know that yet and is looking for the way out.
        self._close_icon(UI_W - 26, 26, self.BIG_VISUAL_TAG)
        self.canvas.tag_raise(self.BIG_VISUAL_TAG)
        self.canvas.tag_bind(self.BIG_VISUAL_TAG, "<Button-1>",
                             lambda e: self.hide_big_visual())
        return "break"

    def hide_big_visual(self, event=None):
        self.canvas.delete(self.BIG_VISUAL_TAG)
        self._step_views.pop(self.BIG_VISUAL_TAG, None)
        self._big_visual_photos = []
        return "break"

    # -----------------------------------------------------------------
    # the live camera
    # -----------------------------------------------------------------
    # On the Transcribe Board, live, for as long as the camera is on -- see
    # camera.py for what turns it on and off. On the board rather than full
    # screen because the student asked for it there, and so Liza and the
    # buttons stay in view while she looks. Not an overlay, for the reason
    # show_visual gives, and its own tag so close_viewfinder takes away exactly
    # this. camera.py drives all of it through ui_invoke and ui_call.
    CAMERA_TAG = "camview"
    CAMERA_X_TAG = "camview_x"
    CAMERA_BTN_TAG = "cambutton"
    # The live picture's box: the same place and size as a picture that
    # show_visual puts on the board (VISUAL_BOX), short of it at the bottom.
    CAMERA_BOX = (BOARD_X0 + 10, BOARD_Y0 + 56, BOARD_X1 - 10, BOARD_Y1 - 30)
    # Read by camera.py, which decodes each frame at this size off the Tk
    # thread; a full-size frame resized here would cost the screen its frame rate.
    VIEWFINDER_SIZE = (CAMERA_BOX[2] - CAMERA_BOX[0], CAMERA_BOX[3] - CAMERA_BOX[1])

    def _camera_centre(self):
        x0, y0, x1, y1 = self.CAMERA_BOX
        return (x0 + x1) / 2, (y0 + y1) / 2

    def _camera_span(self, width):
        """Left and right of a picture `width` wide, centred in the box."""
        cx, _ = self._camera_centre()
        return cx - width / 2, cx + width / 2

    def open_viewfinder(self):
        # The live picture is on the small board; a page over it would hide
        # what she is looking at from the one person holding it up.
        self.close_whiteboard()
        self.close_viewfinder()
        self._camera_open = True
        self._camera_photo = None
        tag = self.CAMERA_TAG
        x0, y0, x1, y1 = self.CAMERA_BOX
        cx, cy = self._camera_centre()
        # A clean sheet over the writing area, so the last answer underneath
        # does not show round the edges of the picture.
        self._round_rect(BOARD_X0 + 8, BOARD_Y0 + 50, BOARD_X1 - 8, BOARD_Y1 - 6,
                         12, fill="#FFFFFF", outline="", tags=tag)
        # Where the picture will be until the camera's first frame fills it --
        # a second and a half on this Pi, long enough to look broken if blank.
        width = (y1 - y0) * 4 / 3
        left, right = self._camera_span(width)
        self._camera_frame_box = self.canvas.create_rectangle(
            left - 2, y0 - 2, right + 2, y1 + 2, fill="#161C30",
            outline="#D8E0F0", width=2, tags=tag)
        self._camera_item = self.canvas.create_image(cx, cy, anchor="center",
                                                     tags=tag)
        # LIVE, on a chip over the corner of the picture: a camera frame can be
        # any colour, and red text on a red tablecloth is not a label.
        self._round_rect(left + 6, y0 + 6, left + 62, y0 + 26, 9,
                         fill="#FFFFFF", outline="#D8E0F0", tags=tag)
        self.canvas.create_oval(left + 13, y0 + 12, left + 21, y0 + 20,
                                fill=COL_STOP, outline="", tags=tag)
        self.canvas.create_text(left + 26, y0 + 16, text="LIVE", anchor="w",
                                font=self._font(8, True), fill=COL_STOP, tags=tag)
        self._overlay_text(cx, BOARD_Y1 - 16, "ask me about anything you show me",
                           7, fill=COL_TEXT_DIM, tags=tag)
        # The cross turns the camera off. In the corner the board's own
        # enlarge control uses on a picture.
        self._close_icon(right - 17, y0 + 17, (tag, self.CAMERA_X_TAG))
        self.canvas.tag_bind(tag, "<Button-1>", self._viewfinder_tapped)
        self.canvas.tag_raise(tag)
        self._restack()

    def _viewfinder_tapped(self, event=None):
        """The cross turns the camera off. The picture itself does nothing --
        but it is in tap_to_wake's list, so touching it does not wake her."""
        if self.CAMERA_X_TAG in self.canvas.gettags("current"):
            camera.stop_live("the cross")
        return "break"

    def viewfinder_frame(self, image):
        """One live frame, already decoded and sized by camera.preview_image."""
        if not getattr(self, "_camera_open", False):
            return
        photo = ImageTk.PhotoImage(image)
        self._camera_photo = photo           # Tk keeps no reference of its own
        self.canvas.itemconfigure(self._camera_item, image=photo)
        _, cy = self._camera_centre()
        left, right = self._camera_span(image.width)
        self.canvas.coords(self._camera_frame_box, left - 2,
                           cy - image.height / 2 - 2, right + 2,
                           cy + image.height / 2 + 2)

    def viewfinder_flash(self):
        """She has just looked: the frame round the picture lights up for a
        moment, so the student knows which moment she answered about."""
        if not getattr(self, "_camera_open", False):
            return
        box = self._camera_frame_box
        self.canvas.itemconfigure(box, outline=COL_INDIGO, width=4)

        def back():
            if getattr(self, "_camera_open", False):
                self.canvas.itemconfigure(box, outline="#D8E0F0", width=2)
        self.root.after(350, back)

    def close_viewfinder(self):
        self._camera_open = False
        self._camera_photo = None
        self.canvas.delete(self.CAMERA_TAG)

    def set_camera_button(self, on):
        """The camera switch in the corner of the board's heading: indigo on
        white when it is off, white on rose while it is on."""
        self.canvas.delete(self.CAMERA_BTN_TAG)
        cx, cy, r = BOARD_X0 + 28, BOARD_Y0 + 26, self.ICON_R
        tag = self.CAMERA_BTN_TAG
        fill, ink = (COL_STOP, "#FFFFFF") if on else ("#FFFFFF", COL_INDIGO)
        self._round_rect(cx - r, cy - r, cx + r, cy + r, 8, fill=fill,
                         outline="#D8E0F0", tags=tag)
        # A camera: a body, a lens, and the bump the shutter sits on.
        self.canvas.create_rectangle(cx - 4, cy - 8, cx + 2, cy - 5, fill=ink,
                                     outline="", tags=tag)
        self.canvas.create_rectangle(cx - 9, cy - 5, cx + 9, cy + 7, outline=ink,
                                     width=2, tags=tag)
        self.canvas.create_oval(cx - 4, cy - 3, cx + 4, cy + 5, outline=ink,
                                width=2, tags=tag)
        self.canvas.tag_bind(tag, "<Button-1>", self._camera_button_tapped)
        # Made fresh, so on top of everything -- the full-screen page included.
        self._wb_restack()

    def _camera_button_tapped(self, event=None):
        camera.toggle_live()
        return "break"

    # -----------------------------------------------------------------
    # the whiteboard: the Transcribe Board, full screen, to write on
    # -----------------------------------------------------------------
    # The board's corner button, or "open the board", lays a white page over
    # the whole screen: four pens, a rubber, undo and clear along the top, what
    # she is saying along the bottom, and everything between them to write and
    # draw on with a finger. whiteboard.py is the half with no screen in it --
    # the picture of the page she is sent, her reading of it, the spoken
    # commands -- and solution.py sets the working she writes beside it.
    #
    # NOT AN OVERLAY, for the reason show_visual gives: an overlay parks the
    # microphone, and the whole point of this page is asking her about it. It
    # is canvas items under one tag stacked over everything, so closing it is
    # one delete and the home screen underneath was never touched. The writing
    # lives in self._wb_strokes, not in the items, so it survives the page
    # being closed and opened again -- until Clear, or a different student.
    WB_TAG = "wboard"
    WB_PAGE = "wbpage"          # the page and its dots: a press here starts a line
    WB_INK = "wbink"            # the lines: a press on one starts a new line too
    WB_TOOLBAR = "wbtoolbar"
    WB_BAR = "wbbar"
    WB_CHIP = "wbchip"
    WB_PANEL = "wbpanel"
    WB_GRAPH_TAG = "wbgraph"    # a graph, as a card on the page beside the writing
    WB_SUGGEST_TAG = "wbsuggest"  # what can be done with the writing, under it
    WB_MEASURE_TAG = "wbmeasure"  # a drawn figure, tidied, its angles written in
    BOARD_BTN_TAG = "boardbtn"
    # The page, between the tools along the top and her words along the bottom.
    WB_TOP, WB_BOTTOM = 52, 432
    # Ink, then blue, red and green for marking things up: a ring round the
    # part they are asking about reads to her as exactly that.
    WB_PENS = ("#1E2233", "#1D4ED8", "#DC2626", "#059669")
    WB_PEN_W = 4
    WB_RUBBER_W = 30
    # The working she writes beside theirs: on the right of the page, or on
    # the left when that is where there is less of their writing to cover.
    WB_PANEL_RIGHT = (446, 60, 792, 426)
    WB_PANEL_LEFT = (8, 60, 354, 426)
    WB_PANEL_W = WB_PANEL_RIGHT[2] - WB_PANEL_RIGHT[0] - 8
    # The narrowest a graph card is made to keep it off the end of a line.
    WB_GRAPH_MIN_W = 250
    WB_HINT_MS = 3500

    def _wb_init(self):
        """The page's state. Called from __init__, before anything is drawn."""
        self._wb_open = False
        self._wb_strokes = []         # {"points", "colour", "width", "erase", "item"}
        self._wb_undo = []            # ("stroke", stroke) | ("clear", [strokes])
        self._wb_current = None       # the line under the finger right now
        self._wb_pen = 0
        self._wb_erasing = False
        self._wb_version = 0          # bumped on every change to the writing
        # What ai_loop is handed: (version, strokes as plain tuples, page size).
        # Replaced whole on the Tk thread and only ever read whole on the other,
        # so the reader never sees half of a change. See board_page.
        self._wb_frozen = (0, (), (UI_W, self.WB_BOTTOM - self.WB_TOP))
        self._wb_read_job = None
        self._wb_read_version = -1
        self._wb_chip = (None, "")
        self._wb_hint_job = None
        self._wb_caption = ("", "hint")
        self._wb_panel = None         # a solution.Panel, beside the writing
        self._wb_panel_open = False
        self._wb_panel_box = self.WB_PANEL_RIGHT
        self._wb_step = -1
        self._wb_scroll = 0
        self._wb_column = None        # (step, column image, spans) last laid out
        self._wb_body_item = None
        self._wb_slots = {}           # PhotoImages by what they belong to
        self._wb_lid = None
        self._wb_shown_state = None
        self._wb_offer_left = UI_W - 14
        self._wb_graph_open = False   # the graph as a card on the page
        self._wb_suggestions = []     # buttons under the writing, from the reading
        self._wb_measure = None       # whiteboard.measure_polygon's, while shown
        # Buttons she has offered: see the "buttons for something worth
        # seeing" section. Here because the transcript is fitted around them.
        self._offers = []
        self._offer_photos = []
        self._wb_reader = whiteboard.Reader(
            lambda version, found: self.root.after(
                0, lambda: self._wb_read_done(version, found)),
            hindi=lambda: self._wb_language() == "hi")

    # ---- the way in, on the board itself ----
    def set_board_button(self):
        """The full-screen switch in the right-hand corner of the board's
        heading, the mirror of the camera switch in the left-hand one."""
        self.canvas.delete(self.BOARD_BTN_TAG)
        if not BOARD_ENABLED:
            return
        cx, cy = BOARD_X1 - 28, BOARD_Y0 + 26
        self._expand_icon(cx, cy, self.BOARD_BTN_TAG)
        # A pencil between the brackets. A picture on the board has the same
        # full-screen brackets on its own corner, a hand's width below this;
        # the pencil is what says this one is for writing on.
        self.canvas.create_line(cx - 3, cy + 3, cx + 4, cy - 4, fill=COL_INDIGO,
                                width=3, capstyle="round", tags=self.BOARD_BTN_TAG)
        self.canvas.create_polygon(cx - 3, cy + 1, cx - 1, cy + 3, cx - 5, cy + 5,
                                   fill=COL_INDIGO, outline="", tags=self.BOARD_BTN_TAG)
        self.canvas.tag_bind(self.BOARD_BTN_TAG, "<ButtonPress-1>",
                             self._board_button_tapped)

    def _board_button_tapped(self, event=None):
        if event is not None:
            self._tap_handled = event.serial
        self.open_whiteboard()
        return "break"

    def _collapse_icon(self, cx, cy, tag):
        """Four corner brackets pointing in: the way back out of full screen."""
        self._round_rect(cx - self.ICON_R, cy - self.ICON_R,
                         cx + self.ICON_R, cy + self.ICON_R, 8,
                         fill="#FFFFFF", outline="#D8E0F0", tags=tag)
        g, a = self.ICON_GLYPH, self.ICON_ARM
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x, y = cx + (g - a) * sx, cy + (g - a) * sy
            self.canvas.create_line(x, y, x + a * sx, y, fill=COL_INDIGO,
                                    width=2, capstyle="round", tags=tag)
            self.canvas.create_line(x, y, x, y + a * sy, fill=COL_INDIGO,
                                    width=2, capstyle="round", tags=tag)

    # ---- open and close ----
    def board_is_open(self):
        """Any thread: is the page up?"""
        return self._wb_open

    def board_page(self):
        """Any thread: (version, strokes, page size) while the page is up, else
        None. The strokes are plain tuples, points from the page's corner."""
        return self._wb_frozen if self._wb_open else None

    def board_buttons(self):
        """Any thread: the buttons under their writing, by label -- "Plot
        them", "Solve it" -- while the page is up."""
        return [o["label"] for o in self._wb_suggestions] if self._wb_open else []

    def board_measured(self):
        """Any thread: the figure measured on the page, in words, or ""."""
        m = self._wb_measure
        if not (self._wb_open and m):
            return ""
        letters = "ABCDEFGH"
        name = re.split(r"[;,(]", m.get("name") or "figure")[0].strip().lower()
        return (f"their {name} has angles "
                + ", ".join(f"{letters[i]} = {a}°" for i, a in enumerate(m["angles"]))
                + f" (adding up to {sum(m['angles'])}°)")

    def board_working(self):
        """Any thread: the worked solution beside their writing, for the model."""
        panel = self._wb_panel
        if not (self._wb_open and self._wb_panel_open and panel is not None):
            return ""
        return solution.describe(panel.solution)

    def board_command(self, command):
        """"open", "close", "clear" or "undo", said rather than tapped."""
        {"open": self.open_whiteboard, "close": self.close_whiteboard,
         "clear": self._wb_clear, "undo": self._wb_undo_last}.get(command, lambda: None)()

    def open_whiteboard(self):
        if self._wb_open or not BOARD_ENABLED:
            return
        # Her own full-screen views go first, and so does the camera: its
        # picture is on the small board, under the page, where nobody can see
        # what it is looking at.
        camera.stop_live("the board is open full screen")
        self.shrink_current()
        self._wb_open = True
        self._wb_caption = ("Write or draw anything, then tap Ask Liza \u2014 or just ask me.",
                            "hint")
        self._wb_draw()
        print(f"[BOARD] Open full screen ({len(self._wb_strokes)} line(s) on it).",
              flush=True)

    def close_whiteboard(self):
        if not self._wb_open:
            return
        self._wb_current = None
        self._wb_open = False
        self._wb_cancel_read()
        if self._wb_hint_job:
            self.root.after_cancel(self._wb_hint_job)
            self._wb_hint_job = None
        self.canvas.delete(self.WB_TAG)
        if self._graph is not None:
            self._graph["views"].pop(self.WB_GRAPH_TAG, None)
        self._wb_slots = {}
        self._wb_body_item = self._wb_lid = None
        self._wb_shown_state = None
        self._wb_offer_left = UI_W - 14
        print("[BOARD] Back to the home screen.", flush=True)

    def _wb_reset(self):
        """A different student: their page is not the last one's."""
        self.close_whiteboard()
        self._wb_strokes, self._wb_undo = [], []
        self._wb_panel, self._wb_panel_open = None, False
        self._wb_graph_open = False
        self._wb_suggestions = []
        self._wb_measure = None
        self._wb_version += 1
        self._wb_freeze()
        self._wb_chip = (None, "")

    # ---- drawing it ----
    def _wb_draw(self):
        """The whole page, from what is remembered."""
        c, tag = self.canvas, self.WB_TAG
        c.delete(tag)
        self._wb_slots = {}
        self._wb_body_item = None
        c.create_rectangle(0, 0, UI_W, UI_H, fill="#FFFFFF", outline="", tags=(tag,))
        c.create_rectangle(0, self.WB_TOP, UI_W, self.WB_BOTTOM, fill="#FFFFFF",
                           outline="", tags=(tag, self.WB_PAGE))
        c.create_image(0, self.WB_TOP, image=self._wb_grid(), anchor="nw",
                       tags=(tag, self.WB_PAGE))
        # Every line goes in just UNDER this, so it lands above the lines before
        # it and below the chip, the working and the bars -- Tk stacks items in
        # the order they were made, and a new line made last would sit on top
        # of all of them.
        self._wb_lid = c.create_line(0, 0, 1, 1, fill="", state="hidden", tags=(tag,))
        for stroke in self._wb_strokes:
            stroke["item"] = self._wb_line(stroke)
        for target in (self.WB_PAGE, self.WB_INK):
            c.tag_bind(target, "<ButtonPress-1>", self._wb_press)
            c.tag_bind(target, "<B1-Motion>", self._wb_drag)
            c.tag_bind(target, "<ButtonRelease-1>", self._wb_lift)
        if self._wb_panel is not None and self._wb_panel_open:
            self._wb_draw_panel()
        elif self._wb_graph_open and self._graph is not None:
            self._wb_draw_graph_card()
        self._wb_draw_measure()
        self._wb_draw_suggestions()
        self._wb_draw_offers()
        self._wb_draw_chip()
        self._wb_draw_toolbar()
        self._wb_draw_bar()
        self._wb_restack()

    def _wb_grid(self):
        """The faint dots a child keeps their writing straight by. One image,
        built once: four hundred little ovals would each be redrawn under every
        stroke."""
        if getattr(self, "_wb_grid_photo", None) is None:
            w, h = UI_W, self.WB_BOTTOM - self.WB_TOP
            image = Image.new("RGB", (w, h), "#FFFFFF")
            draw = ImageDraw.Draw(image)
            for y in range(14, h, 28):
                for x in range(14, w, 28):
                    draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill="#DCE2F0")
            self._wb_grid_photo = ImageTk.PhotoImage(image)
        return self._wb_grid_photo

    def _restack(self):
        """After something is drawn on the board: the buttons she offered stay
        over it -- a picture put up by tapping one of two would otherwise
        cover the other -- and the full-screen page stays over everything."""
        if self.canvas.find_withtag(self.OFFER_TAG):
            self.canvas.tag_raise(self.OFFER_TAG)
        self._wb_restack()
        # The geometry lab, open, is over all of it -- the page too.
        if self.canvas.find_withtag(self.GEO_TAG):
            self.canvas.tag_raise(self.GEO_TAG)
            if self.canvas.find_withtag(self.STOP_PILL_TAG):
                self.canvas.tag_raise(self.STOP_PILL_TAG)

    def _wb_restack(self):
        """The page over everything on the home screen, and under what is
        meant to go over it: her own full-screen views, a modal screen, and
        the Stop pill."""
        if not self._wb_open:
            return
        c = self.canvas
        c.tag_raise(self.WB_TAG)
        for tag in (self.BIG_VISUAL_TAG, self.BIG_GRAPH_TAG, self.BIG_MODEL3D_TAG,
                    self.GEO_TAG, self.OVERLAY_TAG, self.STOP_PILL_TAG):
            if c.find_withtag(tag):
                c.tag_raise(tag)

    def _wb_guard(self):
        """Put the page back on top if something on the home screen has been
        drawn over it -- the weather glyph is redrawn on a timer, the camera
        switch when the camera stops. Cheap: it only walks down from the top
        of the stack until it reaches the page."""
        if not self._wb_open:
            return
        if self._wb_current is not None:
            return      # never mid-line: the restack can wait for the lift
        # What is allowed above the page: everything meant to cover it.
        allowed = {self.WB_TAG, self.BIG_VISUAL_TAG, self.BIG_GRAPH_TAG,
                   self.BIG_MODEL3D_TAG, self.GEO_TAG, self.OVERLAY_TAG, self.STOP_PILL_TAG}
        for item in reversed(self.canvas.find_all()):
            tags = set(self.canvas.gettags(item))
            if self.WB_TAG in tags:
                return
            if not tags & allowed and self.canvas.itemcget(item, "state") != "hidden":
                self._wb_restack()
                return

    def _wb_slot(self, name):
        """A fresh list to hold one element's PhotoImages. Replacing the old
        list is what lets the old images go."""
        self._wb_slots[name] = []
        return self._wb_slots[name]

    def _wb_text(self, x, y, text, size, tags, slot, bold=False, fill=COL_TEXT,
                 anchor="w", width=None, max_lines=None):
        """Words on the page: Hindi shaped (see devanagari_image), English cut
        to `max_lines` with an ellipsis rather than spilling out of its strip."""
        if width and max_lines and not has_devanagari(text):
            text = self._fit_lines(text, self._font(size, bold), width, max_lines)
            width = None
        return self._overlay_text(x, y, text, size, bold=bold, fill=fill,
                                  anchor=anchor, tags=tags, width=width,
                                  justify="left", max_lines=max_lines,
                                  photos=self._wb_slot(slot))

    def _fit_lines(self, text, font, width, max_lines):
        """`text` wrapped into at most `max_lines` lines of `width`, the last
        one ellipsized -- create_text's width= wraps, but never stops."""
        lines, line = [], ""
        for word in text.split():
            trial = f"{line} {word}" if line else word
            if line and font.measure(trial) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self._ellipsize(lines[-1] + " …", font, width)
        return "\n".join(lines)

    def _wb_draw_toolbar(self):
        c = self.canvas
        c.delete(self.WB_TOOLBAR)
        base = (self.WB_TAG, self.WB_TOOLBAR)
        c.create_rectangle(0, 0, UI_W, self.WB_TOP, fill="#FFFFFF", outline="", tags=base)
        c.create_line(0, self.WB_TOP, UI_W, self.WB_TOP, fill=COL_FRAME, width=2, tags=base)
        cy = self.WB_TOP / 2

        def tool(name, cx, draw, command, half=17):
            tag = f"wbtool_{name}"
            tags = base + (tag,)
            # The target is bigger than the glyph: a fingertip is 40px wide.
            c.create_rectangle(cx - half, cy - 20, cx + half, cy + 20, fill="#FFFFFF",
                               outline="", tags=tags)
            draw(cx, tags)
            c.tag_bind(tag, "<ButtonPress-1>",
                       lambda e: self._wb_tapped(e, command))

        for index, colour in enumerate(self.WB_PENS):
            chosen = index == self._wb_pen and not self._wb_erasing

            def pen(cx, tags, colour=colour, chosen=chosen):
                if chosen:
                    c.create_oval(cx - 15, cy - 15, cx + 15, cy + 15, outline=colour,
                                  width=3, tags=tags)
                c.create_oval(cx - 10, cy - 10, cx + 10, cy + 10, fill=colour,
                              outline="", tags=tags)
            tool(f"pen{index}", 26 + index * 36, pen,
                 lambda i=index: self._wb_pick(pen=i))

        def rubber(cx, tags):
            if self._wb_erasing:
                c.create_oval(cx - 16, cy - 16, cx + 16, cy + 16, outline=COL_INDIGO,
                              width=3, tags=tags)
            # A block rubber lying at an angle, its working end in pink.
            body = [(-9, 4), (1, -8), (10, -1), (0, 11)]
            c.create_polygon(*[v for x, y in body for v in (cx + x, cy + y)],
                             fill="#F9A8D4", outline="#9D174D", width=2, tags=tags)
            c.create_line(cx - 4, cy - 2, cx + 5, cy + 5, fill="#9D174D", width=2,
                          tags=tags)
        tool("rubber", 182, rubber, lambda: self._wb_pick(rubber=True))

        live = bool(self._wb_undo)
        ink = COL_INDIGO if live else COL_TEXT_FAINT

        def undo(cx, tags):
            c.create_arc(cx - 9, cy - 8, cx + 9, cy + 10, start=200, extent=-250,
                         style="arc", outline=ink, width=3, tags=tags)
            c.create_polygon(cx - 13, cy - 1, cx - 4, cy - 1, cx - 9, cy + 6,
                             fill=ink, outline="", tags=tags)
        tool("undo", 226, undo, self._wb_undo_last)

        live = bool(self._wb_strokes)
        bin_ink = COL_STOP if live else COL_TEXT_FAINT

        def clear(cx, tags):
            c.create_line(cx - 10, cy - 7, cx + 10, cy - 7, fill=bin_ink, width=2,
                          capstyle="round", tags=tags)
            c.create_line(cx - 3, cy - 10, cx + 3, cy - 10, fill=bin_ink, width=2,
                          capstyle="round", tags=tags)
            c.create_polygon(cx - 7, cy - 4, cx + 7, cy - 4, cx + 6, cy + 11,
                             cx - 6, cy + 11, fill="", outline=bin_ink, width=2,
                             tags=tags)
            for dx in (-3, 0, 3):
                c.create_line(cx + dx, cy - 1, cx + dx, cy + 8, fill=bin_ink,
                              width=1, tags=tags)
        tool("clear", 268, clear, self._wb_clear)

        # The cards that can come back beside the writing: her working, and
        # the graph. Either takes the other's place, so whichever is not up
        # has a button here.
        cards = []
        if self._graph is not None and not self._wb_graph_open:
            cards.append(("graph", "Graph", self._wb_show_graph))
        if self._wb_panel is not None and not self._wb_panel_open:
            cards.append(("steps", "Steps", self._wb_show_panel))

        # The name, where the Stop pill goes while she talks -- unless both
        # cards are waiting, which need its room.
        if len(cards) < 2:
            c.create_text(UI_W / 2, cy, text="Transcribe Board", font=self._font(14, True),
                          fill="#7C3AED", tags=base)

        right = 588
        for name, words, command in cards:
            tag = f"wbtool_{name}"
            self._round_rect(right - 80, cy - 16, right, cy + 16, 14, fill="#FFFFFF",
                             outline=COL_INDIGO, width=2, tags=base + (tag,))
            c.create_text(right - 40, cy, text=words, font=self._font(11, True),
                          fill=COL_INDIGO, tags=base + (tag,))
            c.tag_bind(tag, "<ButtonPress-1>",
                       lambda e, command=command: self._wb_tapped(e, command))
            right -= 92

        tag = "wbtool_ask"
        self._round_rect(602, cy - 18, 738, cy + 18, 16, fill=COL_INDIGO,
                         outline=COL_INDIGO, tags=base + (tag,))
        self._sparkle(626, cy, 7, "#FFFFFF", base + (tag,))
        c.create_text(680, cy, text="Ask Liza", font=self._font(13, True),
                      fill="#FFFFFF", tags=base + (tag,))
        c.tag_bind(tag, "<ButtonPress-1>", lambda e: self._wb_tapped(e, self._wb_ask))

        tag = "wbtool_close"
        self._collapse_icon(UI_W - 28, cy, base + (tag,))
        c.tag_bind(tag, "<ButtonPress-1>",
                   lambda e: self._wb_tapped(e, lambda: self.root.after_idle(
                       self.close_whiteboard)))
        self._wb_restack()

    def _sparkle(self, cx, cy, s, colour, tags):
        """The four-pointed star the board's own artwork is scattered with."""
        self.canvas.create_polygon(
            cx, cy - s, cx + s * 0.3, cy - s * 0.3, cx + s, cy, cx + s * 0.3, cy + s * 0.3,
            cx, cy + s, cx - s * 0.3, cy + s * 0.3, cx - s, cy, cx - s * 0.3, cy - s * 0.3,
            fill=colour, outline="", tags=tags)

    def _wb_tapped(self, event, command):
        """A control on the page. Marked handled, so tap_to_wake leaves it."""
        if event is not None:
            self._tap_handled = event.serial
        command()
        return "break"

    def _wb_pick(self, pen=None, rubber=False):
        if rubber:
            self._wb_erasing = not self._wb_erasing
        else:
            self._wb_pen, self._wb_erasing = pen, False
        self._wb_draw_toolbar()

    def _wb_draw_bar(self):
        """What she is doing and saying, along the bottom: the page covers her
        face and the home screen's board, which is where those used to be."""
        c = self.canvas
        c.delete(self.WB_BAR)
        tags = (self.WB_TAG, self.WB_BAR)
        c.create_rectangle(0, self.WB_BOTTOM, UI_W, UI_H, fill=COL_BG, outline="", tags=tags)
        c.create_line(0, self.WB_BOTTOM, UI_W, self.WB_BOTTOM, fill=COL_FRAME, width=2,
                      tags=tags)
        cx, cy = 30, (self.WB_BOTTOM + UI_H) / 2
        mic = tags + ("wbmic",)
        self._wb_mic = c.create_oval(cx - 19, cy - 19, cx + 19, cy + 19, fill=COL_INDIGO,
                                     outline="", tags=mic)
        self._round_rect(cx - 5, cy - 11, cx + 5, cy + 3, 5, fill="#FFFFFF",
                         outline="#FFFFFF", tags=mic)
        c.create_arc(cx - 9, cy - 6, cx + 9, cy + 8, start=180, extent=180, style="arc",
                     outline="#FFFFFF", width=2, tags=mic)
        c.create_line(cx, cy + 8, cx, cy + 12, fill="#FFFFFF", width=2, tags=mic)
        c.tag_bind("wbmic", "<ButtonPress-1>", lambda e: self._wb_tapped(e, self.wake_up))
        self._wb_state_item = c.create_text(58, cy, text="", anchor="w",
                                            font=self._font(9, True), fill=COL_INDIGO,
                                            tags=tags)
        self._wb_shown_state = None
        self._wb_tick_state()
        self._wb_draw_caption()

    def _wb_draw_caption(self):
        c = self.canvas
        c.delete("wbcaption")
        text, who = self._wb_caption
        if not text:
            return
        cy = (self.WB_BOTTOM + UI_H) / 2
        if who == "hint":
            line, colour = text, COL_TEXT_DIM
        else:
            line = f"{'Liza' if who == 'liza' else 'You'}: {text}"
            colour = COL_TEXT
        self._wb_text(150, cy, line, 11, (self.WB_TAG, self.WB_BAR, "wbcaption"),
                      "caption", fill=colour, width=UI_W - 150 - 14, max_lines=2)

    def _wb_tick_state(self):
        """Her state on the mic button and beside it. From _animate, so it
        follows her the way the caption under her face does."""
        if not self._wb_open or getattr(self, "_wb_state_item", None) is None:
            return
        state_now = self.current_state
        if state_now == self._wb_shown_state:
            return
        self._wb_shown_state = state_now
        label, colour, _ = STATE_STYLE.get(state_now, STATE_STYLE["idle"])
        self.canvas.itemconfig(self._wb_state_item, text=label.upper(), fill=colour)
        self.canvas.itemconfig(self._wb_mic, fill=colour)

    def _wb_set_caption(self, text, speaker):
        self._wb_caption = (text, speaker)
        if self._wb_open:
            self._wb_draw_caption()

    # ---- the ink ----
    def _wb_flat(self, points):
        """Canvas coordinates for a line. A tap is one point, and Tk will not
        draw a line through one point, so it gets a second a hair away."""
        if len(points) == 1:
            x, y = points[0]
            return (x, y, x + 0.5, y + 0.5)
        return tuple(v for point in points for v in point)

    def _wb_line(self, stroke):
        item = self.canvas.create_line(
            *self._wb_flat(stroke["points"]), fill=stroke["colour"],
            width=stroke["width"], capstyle="round", joinstyle="round",
            smooth=True, splinesteps=4, tags=(self.WB_TAG, self.WB_INK))
        if self._wb_lid is not None:
            self.canvas.tag_lower(item, self._wb_lid)
        return item

    def _wb_clamp(self, x, y):
        return (max(2, min(UI_W - 2, x)),
                max(self.WB_TOP + 3, min(self.WB_BOTTOM - 3, y)))

    def _wb_press(self, event):
        self._tap_handled = event.serial
        if not self._wb_open:
            return "break"
        x, y = self._wb_clamp(event.x, event.y)
        erase = self._wb_erasing
        stroke = {"points": [(x, y)], "erase": erase,
                  "colour": "#FFFFFF" if erase else self.WB_PENS[self._wb_pen],
                  "width": self.WB_RUBBER_W if erase else self.WB_PEN_W}
        stroke["item"] = self._wb_line(stroke)
        self._wb_current = stroke
        # They are still writing: whatever she was about to read is out of date,
        # and so are the buttons for what was there -- which would otherwise sit
        # on top of the new line until the finger lifts.
        self._wb_cancel_read()
        self.canvas.delete(self.WB_SUGGEST_TAG)
        return "break"

    def _wb_drag(self, event):
        stroke = self._wb_current
        if stroke is None:
            return "break"
        x, y = self._wb_clamp(event.x, event.y)
        last_x, last_y = stroke["points"][-1]
        # A finger held still still reports a stream of motion; points closer
        # than this only make the line slower to redraw.
        if abs(x - last_x) + abs(y - last_y) < 2:
            return "break"
        stroke["points"].append((x, y))
        self.canvas.coords(stroke["item"], *self._wb_flat(stroke["points"]))
        return "break"

    def _wb_lift(self, event=None):
        stroke, self._wb_current = self._wb_current, None
        if stroke is None:
            return "break"
        self._wb_strokes.append(stroke)
        self._wb_undo.append(("stroke", stroke))
        self._wb_changed()
        return "break"

    def _wb_undo_last(self):
        if not self._wb_undo:
            return
        kind, what = self._wb_undo.pop()
        if kind == "stroke":
            if what in self._wb_strokes:
                self._wb_strokes.remove(what)
            if self._wb_open and what.get("item"):
                self.canvas.delete(what["item"])
        else:
            # A Clear taken back: its lines go back UNDER anything written
            # since, which is where they were.
            self._wb_strokes = what + self._wb_strokes
            if self._wb_open:
                self.canvas.delete(self.WB_INK)
                for stroke in self._wb_strokes:
                    stroke["item"] = self._wb_line(stroke)
        self._wb_changed()

    def _wb_clear(self):
        """Wipe the page. Undo brings it back: a child's whole sum lost to one
        stray tap on the bin would be the one thing worse than no Clear."""
        if not self._wb_strokes:
            return
        self._wb_undo.append(("clear", self._wb_strokes))
        if self._wb_open:
            self.canvas.delete(self.WB_INK)
        self._wb_strokes = []
        # A clean page is for something new: the working and the graph of
        # what was there step aside too, one tap away on Steps and Graph.
        if self._wb_panel_open:
            self._wb_panel_open = False
            self.canvas.delete(self.WB_PANEL)
            self._wb_body_item = None
        self._wb_hide_graph(redraw=False)
        self._wb_changed()
        if self._wb_open:
            self._wb_draw_offers()
            self._wb_draw_chip()

    def _wb_freeze(self):
        top = self.WB_TOP
        strokes = tuple((tuple((x, y - top) for x, y in s["points"]), s["colour"],
                         s["width"], s["erase"]) for s in self._wb_strokes)
        self._wb_frozen = (self._wb_version, strokes,
                           (UI_W, self.WB_BOTTOM - self.WB_TOP))

    def _wb_has_ink(self):
        return whiteboard.ink_box(self._wb_frozen[1]) is not None

    def _wb_changed(self):
        self._wb_version += 1
        self._wb_freeze()
        if self._wb_suggestions:
            self._wb_suggestions = []
            self.canvas.delete(self.WB_SUGGEST_TAG)
        if self._wb_measure is not None:
            # Measured on the figure as it WAS.
            self._wb_measure = None
            self.canvas.delete(self.WB_MEASURE_TAG)
        if not self._wb_open:
            return
        self._wb_draw_toolbar()
        if self._wb_has_ink():
            self._wb_schedule_read()
        else:
            self._wb_cancel_read()
            self._wb_set_chip(None)

    # ---- her reading of it, live ----
    def _wb_cancel_read(self):
        if self._wb_read_job is not None:
            self.root.after_cancel(self._wb_read_job)
            self._wb_read_job = None

    def _wb_schedule_read(self):
        """Read the page once the writing has stopped for BOARD_READ_PAUSE_S.
        Every new line pushes it back, so she reads a pause, not a scribble."""
        if not BOARD_READ_LIVE:
            return
        self._wb_cancel_read()
        self._wb_read_job = self.root.after(int(BOARD_READ_PAUSE_S * 1000),
                                            self._wb_read_now)

    def _wb_read_now(self):
        self._wb_read_job = None
        if not self._wb_open or self._wb_current is not None:
            return
        version, strokes, size = self._wb_frozen
        if version == self._wb_read_version or not self._wb_has_ink():
            return
        self._wb_set_chip("reading")
        self._wb_reader.request(version, strokes, size)

    def _wb_read_done(self, version, found):
        """Tk thread. Dropped if the page has changed since: a reading of the
        page as it WAS is worse than none.

        `found` is whiteboard.read's: the line for the chip, and the buttons
        for what is written -- Plot it, Solve it, See it in 3D -- which go up
        under the writing straight away, with no model between the reading
        and the button. See whiteboard.suggestions."""
        if version != self._wb_version or not self._wb_open:
            return
        self._wb_read_version = version
        text = found.get("read", "")
        self._wb_set_chip("read" if text else None, text)
        self._wb_suggestions = list(found.get("suggest") or [])
        self._wb_draw_suggestions()
        if self._wb_suggestions:
            print("[BOARD] Buttons under the writing: "
                  + ", ".join(o["label"] for o in self._wb_suggestions), flush=True)

    def _wb_set_chip(self, mode, text=""):
        if self._wb_hint_job and mode != "hint":
            self.root.after_cancel(self._wb_hint_job)
            self._wb_hint_job = None
        self._wb_chip = (mode, text)
        if self._wb_open:
            self._wb_draw_chip()

    def _wb_hint(self, text):
        """A word from the page itself, gone again after a few seconds."""
        before = self._wb_chip if self._wb_chip[0] != "hint" else (None, "")
        self._wb_set_chip("hint", text)
        if self._wb_hint_job:
            self.root.after_cancel(self._wb_hint_job)

        def back():
            self._wb_hint_job = None
            if self._wb_chip[0] == "hint":
                self._wb_set_chip(*before)
        self._wb_hint_job = self.root.after(self.WB_HINT_MS, back)

    def _wb_draw_chip(self):
        """"I read: 2x + 3 = 7", on the page, in the corner nobody starts
        writing in."""
        c = self.canvas
        c.delete(self.WB_CHIP)
        mode, text = self._wb_chip
        if mode is None:
            return
        if mode == "reading":
            words, ink, ground = "Reading what you wrote…", COL_TEXT_DIM, "#F3F5FD"
        elif mode == "read":
            words, ink, ground = f"I read:  {text}", "#3730A3", "#EEF2FF"
        else:
            words, ink, ground = text, "#9A3412", "#FFF7ED"
        x0, right = 12, UI_W - 14
        card = self._wb_card_box()
        if card is not None:
            if card[0] < UI_W / 4:
                x0 = card[2] + 12
            else:
                right = card[0] - 14
        # And clear of the buttons she has offered, at the same height.
        right = min(right, self._wb_offer_left - 10)
        cy = self.WB_BOTTOM - 24
        tags = (self.WB_TAG, self.WB_CHIP)
        item = self._wb_text(x0 + 34, cy, words, 11, tags, "chip", bold=(mode == "read"),
                             fill=ink, width=right - x0 - 50, max_lines=1)
        box = c.bbox(item)
        if box is None:
            return
        ground_item = self._round_rect(x0, cy - 16, box[2] + 14, cy + 16, 15, fill=ground,
                                       outline=_mix(ink, "#FFFFFF", 0.6), tags=tags)
        c.tag_lower(ground_item, item)
        # An eye: she is looking at it.
        c.create_oval(x0 + 9, cy - 6, x0 + 27, cy + 6, outline=ink, width=2, tags=tags)
        c.create_oval(x0 + 15, cy - 3, x0 + 21, cy + 3, fill=ink, outline="", tags=tags)
        self._wb_restack()

    # ---- asking her ----
    def _wb_language(self):
        """The language a tapped question is asked in: whatever they asked
        her to use, else whatever they last spoke in."""
        try:
            asked = assistant.language_preference()
        except Exception:
            asked = None
        if asked in BOARD_ASK_QUESTIONS:
            return asked
        text, who = self._wb_caption
        said = text if who == "user" else (self.transcript if self.speaker == "user" else "")
        return "hi" if has_devanagari(said) else "en"

    def _wb_ask(self, what=None):
        """Ask Liza: the page goes to her with a question about it. Handed to
        ai_loop through whiteboard.ask, the way a mode tap hands over its
        intro -- this is the Tk thread, and only ai_loop may speak or listen.
        `what` is a button's own question -- "solve" or "balance"."""
        if not self._wb_has_ink():
            self._wb_hint("Write or draw something first, then tap Ask Liza.")
            return
        language = self._wb_language()
        questions = BOARD_SOLVE_QUESTIONS.get(what, BOARD_ASK_QUESTIONS)
        whiteboard.ask(questions.get(language, questions["en"]), language)
        print(f"[BOARD] {'Ask Liza' if what is None else what.capitalize() + ' it'} tapped.",
              flush=True)
        self.asleep = False
        sleep_event.clear()
        # She stops for it, the way she stops for Speak: the page IS the question.
        if playback_active.is_set() or not audio_queue.empty():
            assistant.interrupt_playback()
        self.set_state("thinking")

    # ---- the working she writes beside it ----
    def show_solution(self, panel):
        """A worked solution from her (a solution.Panel, laid out already on
        the thread that made it). Beside their writing when the page is up;
        otherwise kept, and offered by the Steps button when it next opens."""
        self._wb_panel = panel
        self._wb_step = -1
        self._wb_scroll = 0
        self._wb_column = None
        self._wb_panel_open = self._wb_open
        if self._wb_open:
            self._wb_hide_graph(redraw=False)
            self._wb_panel_box = self._wb_pick_side()
            self._wb_draw_panel()
            self._wb_draw_toolbar()
            self._wb_draw_offers()
            self._wb_draw_chip()
            self._wb_draw_suggestions()

    def _wb_pick_side(self):
        """The half of the page with less of their writing under it, so the
        working covers as little of theirs as it can. Right on a tie."""
        def ink_under(box):
            x0, y0, x1, y1 = box
            return sum(1 for s in self._wb_strokes if not s["erase"]
                       for x, y in s["points"] if x0 <= x <= x1 and y0 <= y <= y1)
        if ink_under(self.WB_PANEL_LEFT) < ink_under(self.WB_PANEL_RIGHT):
            return self.WB_PANEL_LEFT
        return self.WB_PANEL_RIGHT

    def _wb_show_panel(self):
        if self._wb_panel is None:
            return
        self._wb_hide_graph(redraw=False)
        self._wb_panel_box = self._wb_pick_side()
        self._wb_panel_open = True
        self._wb_draw_panel()
        self._wb_draw_toolbar()
        self._wb_draw_offers()
        self._wb_draw_chip()
        self._wb_draw_suggestions()

    def _wb_hide_panel(self):
        self._wb_panel_open = False
        self.canvas.delete(self.WB_PANEL)
        self._wb_body_item = None
        self._wb_draw_toolbar()
        self._wb_draw_offers()
        self._wb_draw_chip()
        self._wb_draw_suggestions()

    def _wb_card_box(self):
        """Where the card beside the writing is -- the working or a graph --
        or None when neither is up."""
        if (self._wb_panel is not None and self._wb_panel_open) or self._wb_graph_open:
            return self._wb_panel_box
        return None

    # ---- a graph, as a card beside the writing ----
    # Like the board in the reference: the curves drawn on the page next to
    # the formulas they come from, with the letters on sliders under them.
    # It is a third VIEW of the one graph (see "the live graph"), so a drag
    # here moves the board's copy and the enlarged one too.
    def _wb_show_graph(self):
        if self._graph is None or not self._wb_open:
            return
        if self._wb_panel_open:
            # The working steps aside; the Steps button brings it back.
            self._wb_panel_open = False
            self.canvas.delete(self.WB_PANEL)
            self._wb_body_item = None
        self._wb_panel_box = self._wb_graph_box()
        self._wb_graph_open = True
        self._wb_draw_graph_card()
        self._wb_draw_toolbar()
        self._wb_draw_offers()
        self._wb_draw_chip()
        self._wb_draw_suggestions()

    def _wb_graph_box(self):
        """Where the graph card goes: the working's place on the side with
        less writing -- narrowed to the room beside the writing when a line
        runs into that side, rather than laid over the end of it, as long as
        what is left is a graph worth reading."""
        side = self._wb_pick_side()
        ink = whiteboard.ink_box(self._wb_frozen[1])
        if ink is None:
            return side
        x0, y0, x1, y1 = side
        if side == self.WB_PANEL_RIGHT and ink[2] + 12 > x0:
            if x1 - (ink[2] + 12) >= self.WB_GRAPH_MIN_W:
                return (ink[2] + 12, y0, x1, y1)
        elif side == self.WB_PANEL_LEFT and ink[0] - 12 < x1:
            if (ink[0] - 12) - x0 >= self.WB_GRAPH_MIN_W:
                return (x0, y0, ink[0] - 12, y1)
        return side

    def _wb_draw_graph_card(self):
        c, tag = self.canvas, self.WB_GRAPH_TAG
        c.delete(tag)
        if self._graph is None:
            return
        self._graph["views"].pop(tag, None)
        x0, y0, x1, y1 = self._wb_panel_box
        self._round_rect(x0 + 2, y0 + 3, x1 + 2, y1 + 4, 16, fill="#D5DBEB",
                         outline="", tags=(self.WB_TAG, tag))
        self._draw_graph(self._wb_panel_box, tag, "card")
        cy = y0 + self.GRAPH_SIZES["card"]["title"] / 2 + 1
        bigger = tag + "big"
        self._expand_icon(x1 - 58, cy, bigger)
        c.addtag_withtag(tag, bigger)
        c.tag_bind(bigger, "<ButtonPress-1>", self._enlarge_graph)
        close = tag + "close"
        self._close_icon(x1 - 22, cy, close)
        c.addtag_withtag(tag, close)
        c.tag_bind(close, "<ButtonPress-1>",
                   lambda e: self._wb_tapped(e, self._wb_hide_graph))
        c.addtag_withtag(self.WB_TAG, tag)
        self._wb_restack()

    def _wb_hide_graph(self, redraw=True):
        if not self._wb_graph_open:
            return
        self._wb_graph_open = False
        self.canvas.delete(self.WB_GRAPH_TAG)
        if self._graph is not None:
            self._graph["views"].pop(self.WB_GRAPH_TAG, None)
        if redraw and self._wb_open:
            self._wb_draw_toolbar()
            self._wb_draw_offers()
            self._wb_draw_chip()
            self._wb_draw_suggestions()

    def _wb_body_box(self):
        x0, y0, x1, y1 = self._wb_panel_box
        panel = self._wb_panel
        head = 18 + (panel.title.height if panel and panel.title else 18)
        foot = 50 if panel is not None and panel.see else 6
        return x0 + 4, y0 + head, x1 - 4, y1 - foot

    def _wb_draw_panel(self):
        c = self.canvas
        c.delete(self.WB_PANEL)
        self._wb_body_item = None
        panel = self._wb_panel
        if panel is None:
            return
        tags = (self.WB_TAG, self.WB_PANEL)
        x0, y0, x1, y1 = self._wb_panel_box
        self._round_rect(x0 + 2, y0 + 3, x1 + 2, y1 + 4, 16, fill="#D5DBEB",
                         outline="", tags=tags)
        self._round_rect(x0, y0, x1, y1, 16, fill="#FFFFFF", outline="#C7D2FE",
                         width=2, tags=tags)
        if panel.title is not None:
            photo = ImageTk.PhotoImage(panel.title)
            self._wb_slot("panel_title").append(photo)
            c.create_image(x0 + 14, y0 + 10, image=photo, anchor="nw", tags=tags)
        close = "wbpanel_close"
        self._close_icon(x1 - 22, y0 + 22, tags + (close,))
        c.tag_bind(close, "<ButtonPress-1>", lambda e: self._wb_tapped(e, self._wb_hide_panel))
        bx0, by0, bx1, by1 = self._wb_body_box()
        c.create_line(x0 + 10, by0 - 3, x1 - 10, by0 - 3, fill=COL_TRACK, width=2, tags=tags)
        if panel.see:
            see = "wbpanel_see"
            sy0, sy1 = y1 - 44, y1 - 8
            self._round_rect(x0 + 12, sy0, x1 - 12, sy1, 16, fill=COL_INDIGO,
                             outline=COL_INDIGO, tags=tags + (see,))
            mid = (sy0 + sy1) / 2
            c.create_polygon(x0 + 30, mid - 8, x0 + 30, mid + 8, x0 + 43, mid,
                             fill="#FFFFFF", outline="", tags=tags + (see,))
            self._wb_text(x0 + 54, mid, panel.see_text, 12, tags + (see,), "panel_see",
                          bold=True, fill="#FFFFFF", width=x1 - x0 - 80, max_lines=1)
            c.tag_bind(see, "<ButtonPress-1>", lambda e: self._wb_tapped(e, self._wb_see))
        self._wb_render_body()
        body = "wbpanel_body"
        c.tag_bind(body, "<ButtonPress-1>", self._wb_panel_press)
        c.tag_bind(body, "<B1-Motion>", self._wb_panel_drag)
        self._wb_restack()

    def _wb_render_body(self, follow=False):
        """The steps, lit where she is, scrolled to `self._wb_scroll` -- or,
        with `follow`, to wherever keeps the lit step in view."""
        panel = self._wb_panel
        if panel is None or not self._wb_open or not self._wb_panel_open:
            return
        if self._wb_column is None or self._wb_column[0] != self._wb_step:
            column, spans = panel.image(self._wb_step)
            self._wb_column = (self._wb_step, column, spans)
        _, column, spans = self._wb_column
        bx0, by0, bx1, by1 = self._wb_body_box()
        height = int(by1 - by0)
        most = max(0, column.height - height)
        if follow and 0 <= self._wb_step < len(spans):
            top, bottom = spans[self._wb_step]
            if top < self._wb_scroll:
                self._wb_scroll = top - 6
            elif bottom > self._wb_scroll + height:
                self._wb_scroll = bottom - height + 6
        self._wb_scroll = int(max(0, min(self._wb_scroll, most)))
        view = Image.new("RGBA", (column.width, height), (255, 255, 255, 255))
        view.alpha_composite(column.crop((0, self._wb_scroll, column.width,
                                          self._wb_scroll + height)))
        photo = ImageTk.PhotoImage(view)
        self._wb_slots["panel_body"] = [photo]
        c = self.canvas
        if self._wb_body_item is None or not c.find_withtag(self._wb_body_item):
            self._wb_body_item = c.create_image(bx0 + 2, by0, image=photo, anchor="nw",
                                                tags=(self.WB_TAG, self.WB_PANEL,
                                                      "wbpanel_body"))
        else:
            c.itemconfig(self._wb_body_item, image=photo)
        c.delete("wbpanel_thumb")
        if most > 0:
            # Where in the working the view is, when there is more than fits.
            length = max(24, height * height / column.height)
            top = by0 + (height - length) * self._wb_scroll / most
            c.create_line(bx1 - 3, top, bx1 - 3, top + length, fill="#C7D2FE",
                          width=4, capstyle="round",
                          tags=(self.WB_TAG, self.WB_PANEL, "wbpanel_thumb"))

    def _wb_panel_press(self, event):
        self._tap_handled = event.serial
        self._wb_drag_from = (event.y, self._wb_scroll)
        return "break"

    def _wb_panel_drag(self, event):
        start = getattr(self, "_wb_drag_from", None)
        if start is None:
            return "break"
        self._wb_scroll = start[1] - (event.y - start[0])
        self._wb_render_body()
        return "break"

    def _wb_match_step(self, line):
        """Light the step she is saying. See solution.match_step."""
        panel = self._wb_panel
        if not (self._wb_open and self._wb_panel_open and panel is not None and line):
            return
        step = solution.match_step(panel.tokens, self._wb_step, line)
        if step != self._wb_step:
            self._wb_step = step
            self._wb_render_body(follow=True)

    def _wb_see(self):
        """The one thing worth trying after the working: the graph with its
        numbers on sliders, the simulation set to the problem's own numbers,
        the balanced reaction. It opens over the page; closing it comes back.
        Run exactly the way an offered button is -- see actions.run_offer."""
        panel = self._wb_panel
        if panel is None or not panel.see:
            return
        kind, payload, settings = panel.see
        print(f"[BOARD] See it: {kind} | {payload}"
              + (f" with {settings}" if settings else ""), flush=True)

        def run():
            import actions
            if not actions.run_offer({"kind": kind, "payload": payload,
                                      "settings": settings}):
                self.root.after(0, lambda: self._wb_hint("I couldn't open that one, sorry."))
        threading.Thread(target=run, daemon=True, name="board-see").start()

    # -----------------------------------------------------------------
    # buttons for something worth seeing
    # -----------------------------------------------------------------
    # She used to ASK -- "Would you like to see a simulation of it?" -- and a
    # child then had to say yes out loud, which the microphone keeps throwing
    # away as noise (two "Yes."es in a row in logs/liza.log before a longer one
    # got through). Now she offers it as a button, [ACTION: offer_visual], and a
    # finger answers. On the home screen the buttons stand along the foot of the
    # Transcribe Board; on the full-screen page, at its bottom right. A tap shows
    # the thing exactly as if she had been asked for it (actions.run_offer).
    OFFER_TAG = "offers"
    WB_OFFER_TAG = "wboffers"
    OFFER_ROW = (BOARD_X0 + 14, BOARD_Y1 - 50, BOARD_X1 - 44, BOARD_Y1 - 14)

    def show_offers(self, offers):
        """Tk thread: these buttons, replacing any there were. [] takes them down."""
        self._offers = list(offers or [])
        self._draw_offers()
        if self._wb_open:
            self._wb_draw_offers()
            self._wb_draw_chip()
        # The words on the board end above the buttons now -- or may have the
        # room back.
        if self.transcript:
            self.canvas.itemconfig(self.transcript_id, text=self._fit_transcript(self.transcript))

    def take_offer(self, index=0):
        """The first button, taken by voice ("yes, show me")."""
        if 0 <= index < len(self._offers):
            self._offer_tapped(None, self._offers[index])

    def _offer_pill(self, box, offer, tags, compact, on_tap=None, photos=None):
        """One button: a play mark, what it shows, and what it is of."""
        photos = self._offer_photos if photos is None else photos
        on_tap = on_tap or self._offer_tapped
        x0, y0, x1, y1 = box
        c = self.canvas
        self._round_rect(x0, y0, x1, y1, min(16, (y1 - y0) / 2), fill="#EEF2FF",
                         outline=COL_INDIGO, width=2, tags=tags)
        cy, r = (y0 + y1) / 2, 9 if compact else 11
        cx = x0 + r + 7
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=COL_INDIGO, outline="", tags=tags)
        c.create_polygon(cx - r * 0.35, cy - r * 0.55, cx - r * 0.35, cy + r * 0.55,
                         cx + r * 0.6, cy, fill="#FFFFFF", outline="", tags=tags)
        text_x = cx + r + 6
        room = max(30, x1 - text_x - 8)
        sub = offer.get("sub") or ""
        self._overlay_text(text_x, cy - (7 if sub else 0), offer["label"], 9 if compact else 10,
                           bold=True, fill=COL_INDIGO, anchor="w", tags=tags, width=room,
                           max_lines=1, justify="left", photos=photos)
        if sub:
            self._overlay_text(text_x, cy + 8, sub, 7 if compact else 8, fill=COL_TEXT_DIM,
                               anchor="w", tags=tags, width=room, max_lines=1,
                               justify="left", photos=photos)
        c.tag_bind(tags[-1], "<ButtonPress-1>", lambda e, o=offer: on_tap(e, o))

    def _draw_offers(self):
        """Along the foot of the Transcribe Board, two at most."""
        c = self.canvas
        c.delete(self.OFFER_TAG)
        self._offer_photos = []
        offers = self._offers[:2]
        if not offers:
            return
        x0, y0, x1, y1 = self.OFFER_ROW
        if len(offers) > 1:
            # Two need the whole width, the status dots' corner too: each word
            # on a half-width button was being cut to "See the reacti...".
            x1 = BOARD_X1 - 14
        # A clean strip: a picture on the board may run under the buttons.
        self._round_rect(x0 - 6, y0 - 6, x1 + 4, y1 + 5, 12, fill="#FFFFFF", outline="",
                         tags=(self.OFFER_TAG,))
        gap = 8
        width = (x1 - x0 - gap * (len(offers) - 1)) / len(offers)
        for index, offer in enumerate(offers):
            left = x0 + index * (width + gap)
            self._offer_pill((left, y0, left + width, y1), offer,
                             (self.OFFER_TAG, f"offer{index}"), compact=len(offers) > 1)
        self._wb_restack()

    def _wb_draw_offers(self):
        """At the bottom right of the full-screen page, beside the reading."""
        c = self.canvas
        c.delete(self.WB_OFFER_TAG)
        self._wb_offer_left = UI_W - 14
        if not self._wb_open:
            return
        panel = self._wb_panel
        offers = [o for o in self._offers
                  # The working's own try-it is on its own button already.
                  if not (panel is not None and self._wb_panel_open and panel.see
                          and (o["kind"], o["payload"]) == (panel.see[0], panel.see[1]))
                  # And so is anything under their writing: one "See it in 3D".
                  and not any(s["kind"] == o["kind"] and _same_thing(s["payload"], o["payload"])
                              for s in self._wb_suggestions)]
        right = UI_W - 14
        card = self._wb_card_box()
        if card is not None and card[0] >= UI_W / 4:
            right = card[0] - 12
        cy = self.WB_BOTTOM - 24
        photos = self._wb_slot("offers")
        keep, self._offer_photos = self._offer_photos, photos
        for index, offer in reversed(list(enumerate(offers[:3]))):
            words = max(self._font(10, True).measure(offer["label"]),
                        self._font(8).measure(offer.get("sub") or ""))
            width = min(230, words + 52)
            if right - width < 160:
                break
            self._offer_pill((right - width, cy - 17, right, cy + 17), offer,
                             (self.WB_TAG, self.WB_OFFER_TAG, f"wboffer{index}"),
                             compact=False)
            right -= width + 8
        self._offer_photos = keep
        self._wb_offer_left = right
        self._wb_restack()

    # ---- buttons under the writing ----
    # What can be done with what is written -- Plot it, Solve it, See it in
    # 3D -- straight from the page's reading, under the writing it is about,
    # the way the board in the reference puts its tools under a selection.
    # No model in between, so they are up a moment after the reading is, and
    # whatever she goes on to say. See whiteboard.suggestions.
    def _wb_draw_suggestions(self):
        c = self.canvas
        c.delete(self.WB_SUGGEST_TAG)
        offers = list(self._wb_suggestions)
        if not (self._wb_open and offers) or self._wb_current is not None:
            return
        box = whiteboard.ink_box(self._wb_frozen[1])
        if box is None:
            return
        ink_x0, ink_y0 = box[0], box[1] + self.WB_TOP
        ink_y1 = box[3] + self.WB_TOP
        label_font, sub_font = self._font(10, True), self._font(8)
        widths = [min(210, max(label_font.measure(o["label"]),
                               sub_font.measure(o.get("sub") or "")) + 52) for o in offers]
        left, right = 10, UI_W - 10
        card = self._wb_card_box()
        if card is not None and card[0] < UI_W / 4:
            left = card[2] + 10
        elif card is not None:
            right = card[0] - 10
        gap, height = 8, 38
        while widths and sum(widths) + gap * (len(widths) - 1) > right - left:
            widths.pop()
        offers = offers[:len(widths)]
        if not offers:
            return
        total = sum(widths) + gap * (len(widths) - 1)
        x = max(left, min(ink_x0, right - total))
        # Under the writing; over it when there is no room under; and when
        # there is room for neither, at the foot of the page, above her chip.
        floor = self.WB_BOTTOM - 50
        y = ink_y1 + 14
        if y + height > floor:
            y = ink_y0 - 14 - height
            if y < self.WB_TOP + 8:
                y = floor - height
        tags = (self.WB_TAG, self.WB_SUGGEST_TAG)
        ground = self.WB_SUGGEST_TAG + "ground"
        self._round_rect(x - 7, y - 7, x + total + 7, y + height + 7, 18, fill="#FFFFFF",
                         outline="#C7D2FE", width=1, tags=tags + (ground,))
        c.tag_bind(ground, "<ButtonPress-1>", self._swallow_press)
        photos = self._wb_slot("suggest")
        for index, (offer, width) in enumerate(zip(offers, widths)):
            self._offer_pill((x, y, x + width, y + height), offer,
                             tags + (f"wbsuggest{index}",), compact=False,
                             on_tap=self._wb_suggestion_tapped, photos=photos)
            x += width + gap
        self._wb_restack()

    def _wb_draw_measure(self):
        """Their figure, tidied: straight sides through the corners found in
        the ink, a letter at each corner, each angle written inside it, and
        what the angles add up to underneath."""
        c = self.canvas
        c.delete(self.WB_MEASURE_TAG)
        m = self._wb_measure
        if not (self._wb_open and m):
            return
        pts = [(x, y + self.WB_TOP) for x, y in m["corners"]]
        n = len(pts)
        tags = (self.WB_TAG, self.WB_MEASURE_TAG)
        ink = "#7C3AED"
        c.create_polygon(*[v for p in pts for v in p], fill="", outline=ink, width=3,
                         joinstyle="round", tags=tags)
        cx, cy = sum(x for x, _ in pts) / n, sum(y for _, y in pts) / n
        shortest = min(math.dist(pts[i], pts[i - 1]) for i in range(n))
        radius = max(10, min(22, shortest * 0.22))
        font = self._font(11 if n <= 4 else 9, True)
        letters = "ABCDEFGH"
        for i, (x, y) in enumerate(pts):
            (ax, ay), (bx, by) = pts[i - 1], pts[(i + 1) % n]
            # Tk measures arcs anticlockwise from east with y UP.
            first = math.degrees(math.atan2(y - ay, ax - x))
            second = math.degrees(math.atan2(y - by, bx - x))
            extent = (second - first) % 360
            start = first
            if extent > 180:
                start, extent = second, 360 - extent
            c.create_arc(x - radius, y - radius, x + radius, y + radius, start=start,
                         extent=extent, style="arc", outline=ink, width=2, tags=tags)
            middle = math.radians(start + extent / 2)
            reach = radius + (16 if extent > 60 else 24)
            lx, ly = x + math.cos(middle) * reach, y - math.sin(middle) * reach
            words = f"{m['angles'][i]}°"
            box = font.measure(words) / 2 + 4
            self._round_rect(lx - box, ly - 10, lx + box, ly + 10, 8, fill="#FFFFFF",
                             outline="", tags=tags)
            c.create_text(lx, ly, text=words, font=font, fill=ink, tags=tags)
            dx, dy = x - cx, y - cy
            away = math.hypot(dx, dy) or 1.0
            c.create_text(x + dx / away * 17, y + dy / away * 17, text=letters[i],
                          font=self._font(12, True), fill=COL_TEXT, tags=tags)
        total = (n - 2) * 180
        words = " + ".join(letters[:n]) + f" = {total}°" if n <= 4 else f"The angles add up to {total}°"
        width = self._font(12, True).measure(words) / 2 + 12

        def inside(px, py):
            # On the inner side of every edge (the corners go clockwise).
            return all((bx - ax) * (py - ay) - (by - ay) * (px - ax) >= 0
                       for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]))
        # Inside the figure when it fits there -- under it is where the
        # buttons for it are -- else over the top of it, else beside it.
        lift = 16 if n == 3 else 0
        x, y = cx, cy + lift
        if not all(inside(x + dx, y + dy) for dx in (-width, width) for dy in (-14, 14)):
            x, y = cx, min(py for _, py in pts) - 34
            if y < self.WB_TOP + 18:
                x, y = max(px for px, _ in pts) + width + 24, cy
        x = max(12 + width, min(UI_W - 12 - width, x))
        self._round_rect(x - width, y - 14, x + width, y + 14, 14, fill="#F5F3FF",
                         outline="#DDD6FE", tags=tags)
        c.create_text(x, y, text=words, font=self._font(12, True), fill=ink, tags=tags)
        # The marks are on THEIR page: a line started on one is a line, like
        # anywhere else on it (and the marks go, the figure having changed).
        c.tag_bind(self.WB_MEASURE_TAG, "<ButtonPress-1>", self._wb_press)
        c.tag_bind(self.WB_MEASURE_TAG, "<B1-Motion>", self._wb_drag)
        c.tag_bind(self.WB_MEASURE_TAG, "<ButtonRelease-1>", self._wb_lift)
        self._wb_restack()

    def _wb_suggestion_tapped(self, event, offer):
        if event is not None:
            self._tap_handled = event.serial
        print(f"[BOARD] Tapped under the writing: {offer['label']} "
              f"({offer['kind']} | {offer['payload'][:60]})", flush=True)
        if offer["kind"] == "ask":
            self._wb_ask(offer["payload"])
            return "break"
        if offer["kind"] == "measure":
            # On and off again: the figure is theirs, the marks are hers.
            self._wb_measure = None if self._wb_measure else offer.get("measure")
            self._wb_draw_measure()
            return "break"
        if offer["kind"] == "explore" and offer.get("measure"):
            measured = offer["measure"]
            figure = geometry.from_drawing(measured["corners"], measured.get("name"))
            app_state.current_visual = {"kind": "geometry", "title": figure.describe(),
                                        "steps": []}
            self.show_geometry(figure)
            return "break"

        def run():
            import actions
            if not actions.run_offer(offer):
                self.root.after(0, lambda: self._wb_open and self._wb_hint(
                    "I couldn't open that one, sorry."))
        threading.Thread(target=run, daemon=True, name="board-button").start()
        return "break"

    def _offer_tapped(self, event, offer):
        if event is not None:
            self._tap_handled = event.serial
        if offer not in self._offers:
            return "break"
        remaining = [o for o in self._offers if o is not offer]
        app_state.current_offers = remaining
        self.show_offers(remaining)
        print(f"[OFFER] Tapped: {offer['label']} ({offer['kind']} | {offer['payload'][:60]})",
              flush=True)

        def run():
            import actions
            if not actions.run_offer(offer):
                self.root.after(0, lambda: self._wb_open and self._wb_hint(
                    "I couldn't open that one, sorry."))
        threading.Thread(target=run, daemon=True, name="offer").start()
        return "break"

    # -----------------------------------------------------------------
    # 3D models
    # -----------------------------------------------------------------
    # "Show me methane in 3D". The drawing itself happens in viewer3d's worker
    # process; this is only the glass it is shown on and the finger that turns
    # it. It opens full screen straight away -- a 3D model is asked for in order
    # to be turned round, and a 309px thumbnail is not where that happens --
    # and leaves a still on the board to come back to, like any other visual.
    #
    # Not an overlay, for the reason show_visual gives: she keeps listening, so
    # "why is it bent?" can be asked while it is being turned.
    MODEL3D_TAG = "board3d"
    BIG_MODEL3D_TAG = "big3d"
    # The picture area of the full-screen view: under the title, above the
    # buttons. Rendered at exactly this size so a frame needs no scaling.
    BIG_MODEL3D_BOX = (0, 62, UI_W, UI_H - 58)
    # Degrees of turn per pixel of drag. VTK's own trackball is about 0.3 at
    # this width; a child's short swipe should get round further than that.
    MODEL3D_DRAG_DEG = 0.45

    def _renderer3d(self):
        """The one viewer3d.Renderer, made on first use."""
        if getattr(self, "_renderer", None) is None:
            import viewer3d
            self._model3d_pending = None
            self._renderer = viewer3d.Renderer(self._model3d_frame_ready,
                                               self._model3d_failed,
                                               self._model3d_moved)
        return self._renderer

    def _model3d_moved(self, answer):
        """From the renderer's thread: the + and - buttons took the student
        into a part's own model (the cell's nucleus) or back out. ON_BOARD has
        to name the new one, and the view its title."""
        def land():
            if answer.get("board"):
                app_state.current_visual = {"kind": "model3d", "title": answer["board"], "steps": []}
            print(f"[3D] Zoomed through to {answer.get('title')}: {answer.get('reply')}", flush=True)
            self.model3d_after_command(answer)
        self.root.after(0, land)

    def _model3d_board_size(self):
        x0, y0, x1, y1 = self.VISUAL_BOX
        return (x1 - x0 - 8, y1 - y0 - 8)

    def _model3d_big_size(self):
        x0, y0, x1, y1 = self.BIG_MODEL3D_BOX
        return (x1 - x0, y1 - y0)

    def show_model3d(self, scene):
        """Put a 3D model up: full screen and turning, with a still on the board."""
        camera.stop_live("a 3D model is going on the board")
        self.clear_visual(keep_renderer=True)
        self._model3d_scene = scene
        self._model3d_big_open = False
        x0, y0, x1, y1 = self.VISUAL_BOX
        self._round_rect(x0, y0, x1, y1, 10, fill="#0B1020", outline="#D8E0F0",
                         tags=self.MODEL3D_TAG)
        self._model3d_board_item = self.canvas.create_image(
            (x0 + x1) / 2, (y0 + y1) / 2, anchor="center", tags=self.MODEL3D_TAG)
        self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                                text="Building the 3D model\u2026", fill="#C9D2EA",
                                font=self._font(10, bold=True),
                                tags=(self.MODEL3D_TAG, self.MODEL3D_TAG + "wait"))
        badge = self.MODEL3D_TAG + "badge"
        self._round_rect(x0 + 8, y0 + 8, x0 + 44, y0 + 30, 8, fill="#6366F1",
                         outline="", tags=(self.MODEL3D_TAG, badge))
        self.canvas.create_text(x0 + 26, y0 + 19, text="3D", fill="#FFFFFF",
                                font=self._font(9, bold=True),
                                tags=(self.MODEL3D_TAG, badge))
        self._expand_icon(x1 - 21, y0 + 21, self.MODEL3D_TAG)
        self.canvas.tag_bind(self.MODEL3D_TAG, "<Button-1>",
                             lambda e: self._open_model3d())
        self._renderer3d().show(scene, self._model3d_big_size())
        self._open_model3d(first=True)
        self._restack()
        print(f"[UI] 3D model: {scene.get('title')}", flush=True)

    def _open_model3d(self, first=False):
        """The full-screen view: drag to turn, buttons to zoom, reset and close."""
        scene = getattr(self, "_model3d_scene", None)
        if scene is None:
            return "break"
        tag = self.BIG_MODEL3D_TAG
        self.canvas.delete(tag)
        self._model3d_big_open = True
        self.canvas.create_rectangle(0, 0, UI_W, UI_H, fill="#0B1020", outline="",
                                     tags=tag)
        self.canvas.create_text(20, 22, text=scene.get("title") or "3D model",
                                anchor="w", fill="#FFFFFF",
                                font=self._font(13, bold=True), tags=tag)
        # The colour key: a dot and a name per element, left to right.
        x = 22
        for _symbol, name, colour in scene.get("legend") or []:
            self.canvas.create_oval(x, 42, x + 12, 54, fill=colour, outline="",
                                    tags=tag)
            label = self.canvas.create_text(x + 17, 48, text=name, anchor="w",
                                            fill="#C9D2EA", font=self._font(9),
                                            tags=tag)
            x = self.canvas.bbox(label)[2] + 16
        x0, y0, x1, y1 = self.BIG_MODEL3D_BOX
        glass = tag + "glass"
        # The touch surface: an empty rectangle the size of the picture, so a
        # drag is caught even before the first frame has arrived.
        self.canvas.create_rectangle(x0, y0, x1, y1, fill="#0B1020", outline="",
                                     tags=(tag, glass))
        self._model3d_big_item = self.canvas.create_image(
            (x0 + x1) / 2, (y0 + y1) / 2, anchor="center", tags=(tag, glass))
        self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                                text="Building the 3D model\u2026", fill="#8891A8",
                                font=self._font(11, bold=True),
                                tags=(tag, tag + "wait"))
        self.canvas.tag_bind(glass, "<ButtonPress-1>", self._model3d_press)
        self.canvas.tag_bind(glass, "<B1-Motion>", self._model3d_drag)
        # A mouse wheel, for whoever plugs one in. Two-finger pinch never
        # reaches Tk -- XWayland hands it one pointer -- hence the buttons.
        self.canvas.tag_bind(glass, "<Button-4>", lambda e: self._model3d_zoom(1.15))
        self.canvas.tag_bind(glass, "<Button-5>", lambda e: self._model3d_zoom(1 / 1.15))

        bottom = UI_H - 29
        self._model3d_button(UI_W / 2 - 150, bottom, "\u2212", lambda: self._model3d_zoom(1 / 1.25))
        self._model3d_button(UI_W / 2 - 80, bottom, "+", lambda: self._model3d_zoom(1.25))
        self._model3d_button(UI_W / 2 + 20, bottom, "Reset", self._model3d_reset, width=96)
        self.canvas.create_text(UI_W - 20, bottom, text="drag to turn it", anchor="e",
                                fill="#8891A8", font=self._font(9), tags=tag)
        if scene.get("formulas"):
            # What a geometry solid is FOR -- its volume and surface area,
            # worked out when the measurements were given -- set big, in the
            # corner under the cross, rather than in the small print below.
            y = 62
            for line in scene["formulas"]:
                item = self.canvas.create_text(UI_W - 20, y, text=line, anchor="ne",
                                               fill="#FFE066", font=self._font(12, bold=True),
                                               tags=tag)
                y = self.canvas.bbox(item)[3] + 4
        if scene.get("note"):
            # "Not to scale", "Spreading exaggerated": the honest small print,
            # in the free corner left of the buttons.
            self.canvas.create_text(16, bottom, text=scene["note"], anchor="w",
                                    width=UI_W / 2 - 180, fill="#8891A8",
                                    font=self._font(8), tags=tag)
        close = tag + "close"
        self._close_icon(UI_W - 26, 24, close)
        self.canvas.addtag_withtag(tag, close)
        # After this tap has finished, not during it: tap_to_wake runs next on
        # the same tap and recognises the 3D view by what is under the finger,
        # so the view must still be there when it looks.
        self.canvas.tag_bind(close, "<Button-1>",
                             lambda e: self.root.after_idle(self.hide_big_model3d))
        self.canvas.tag_raise(tag)
        renderer = self._renderer3d()
        if not first:
            renderer.resize(self._model3d_big_size())
        # Turning on its own says "this moves" before anyone is told so. The
        # first touch stops it: the child has taken over. Orbits and waves run
        # while the view is open -- a model that decides not to spin (the solar
        # system, whose planets move) says so in the scene, and viewer3d obeys.
        renderer.spin(True)
        renderer.animate(True)
        return "break"

    def _model3d_button(self, cx, cy, text, command, width=56):
        tag = f"{self.BIG_MODEL3D_TAG}btn{text}"
        self._round_rect(cx - width / 2, cy - 20, cx + width / 2, cy + 20, 12,
                         fill="#1E2A4A", outline="#3A4A75", tags=(self.BIG_MODEL3D_TAG, tag))
        self.canvas.create_text(cx, cy, text=text, fill="#FFFFFF",
                                font=self._font(14 if len(text) == 1 else 11, bold=True),
                                tags=(self.BIG_MODEL3D_TAG, tag))
        self.canvas.tag_bind(tag, "<Button-1>", lambda e: (command(), "break")[1])

    def _model3d_press(self, event):
        self._model3d_last = (event.x, event.y)
        self._renderer3d().spin(False)
        return "break"

    def _model3d_drag(self, event):
        last = getattr(self, "_model3d_last", None)
        self._model3d_last = (event.x, event.y)
        if last is None:
            return "break"
        dx, dy = event.x - last[0], event.y - last[1]
        # Right drags the front of the model right; down tips its top towards you.
        self._renderer3d().turn(-dx * self.MODEL3D_DRAG_DEG, dy * self.MODEL3D_DRAG_DEG)
        return "break"

    def _model3d_zoom(self, factor):
        self._renderer3d().spin(False)
        self._renderer3d().zoom(factor)
        return "break"

    def _model3d_reset(self):
        self._renderer3d().reset()
        self._renderer3d().spin(True)

    def hide_big_model3d(self, event=None):
        """Back to the board, which keeps a still of the whole model."""
        if not getattr(self, "_model3d_big_open", False):
            return "break"
        self._model3d_big_open = False
        self.canvas.delete(self.BIG_MODEL3D_TAG)
        self._model3d_big_item = None
        if getattr(self, "_renderer", None) is not None:
            self._renderer.spin(False)
            self._renderer.animate(False)
            self._renderer.resize(self._model3d_board_size())
            # The whole molecule on the board, not whatever corner of it the
            # child had zoomed into last.
            self._renderer.reset()
        return "break"

    def _model3d_frame_ready(self, image):
        """From the renderer's thread: keep only the newest frame, and have
        the Tk thread show it. A drag makes frames faster than Tk may want
        them, and a queue of old ones would play on after the finger stops."""
        first = self._model3d_pending is None
        self._model3d_pending = image
        if first:
            self.root.after(0, self._show_model3d_frame)

    def _show_model3d_frame(self):
        image, self._model3d_pending = self._model3d_pending, None
        if image is None or getattr(self, "_model3d_scene", None) is None:
            return
        photo = ImageTk.PhotoImage(image)
        if getattr(self, "_model3d_big_open", False) and self._model3d_big_item:
            self.canvas.delete(self.BIG_MODEL3D_TAG + "wait")
            self.canvas.itemconfig(self._model3d_big_item, image=photo)
            self._model3d_big_photo = photo      # Tk keeps no reference itself
        else:
            self.canvas.delete(self.MODEL3D_TAG + "wait")
            self.canvas.itemconfig(self._model3d_board_item, image=photo)
            self._model3d_board_photo = photo
            # Just over the still, not over everything: to the top of the
            # stack it came up through the full-screen page.
            self.canvas.tag_raise(self.MODEL3D_TAG + "badge", self._model3d_board_item)

    def _model3d_failed(self, reason):
        """From the renderer's thread: the worker could not draw. Say so on the
        screen, where the child is looking, and close the big view."""
        def show():
            if getattr(self, "_model3d_scene", None) is None:
                return
            self.hide_big_model3d()
            self.canvas.itemconfig(self.MODEL3D_TAG + "wait",
                                   text="The 3D model could not be drawn.")
        self.root.after(0, show)

    def _drop_model3d(self, close_worker=True):
        """Take any 3D model away, and let the worker go."""
        self.canvas.delete(self.MODEL3D_TAG)
        self.canvas.delete(self.BIG_MODEL3D_TAG)
        self._model3d_scene = None
        self._model3d_big_open = False
        self._model3d_board_photo = self._model3d_big_photo = None
        if close_worker and getattr(self, "_renderer", None) is not None:
            self._renderer.close()

    # THE 3D ENGINE'S MODELS (see viewer3d.ENGINE_DIR). The same board and big
    # view; what differs is that the engine can be told to do things to the
    # model -- highlight a part, cut it open, go a level deeper.
    def model3d_engine_active(self):
        """Any thread: is the model on the board the engine's?"""
        scene = getattr(self, "_model3d_scene", None)
        return bool(scene and scene.get("kind") == "engine")

    def renderer_ask(self, op, text, timeout=5.0):
        """Any thread (never Tk's: it waits for the worker). None if no answer."""
        renderer = getattr(self, "_renderer", None)
        if renderer is None or not self.model3d_engine_active():
            return None
        return renderer.ask(op, text, timeout)

    def model3d_after_command(self, answer):
        """Tk thread, after the engine carried out a command.

        Opens the big view if the board only had the still -- the student is
        talking about the model, so they should see it -- redraws its title if
        the model changed ("go one level deeper"), and stops the slow spin, or
        the part just highlighted would turn away from them."""
        scene = getattr(self, "_model3d_scene", None)
        if scene is None:
            return
        if answer.get("changed_model") or not getattr(self, "_model3d_big_open", False):
            self._open_model3d()
        if getattr(self, "_renderer", None) is not None:
            self._renderer.spin(False)

    # THE SPOKEN WAY IN AND OUT. A child who wants a closer look says "make it
    # bigger" -- they do not go looking for a control, and on a device answered
    # by talking to it that is the first thing they will try. These take the
    # kind of thing on the board off the model's hands: it asks for bigger, and
    # whichever of the two enlargers applies is picked here.
    def enlarge_current(self):
        if getattr(self, "_geo", None) is not None:
            return self._geo_open()
        if self._graph is not None:
            return self._enlarge_graph()
        if getattr(self, "_model3d_scene", None) is not None:
            # A model opens full screen already, so "zoom it" there means
            # closer -- reopening the same view changed nothing, and the
            # student said so: "No, I did not see any zoom picture."
            if getattr(self, "_model3d_big_open", False):
                return self._model3d_zoom(1.5)
            return self._open_model3d()
        return self._enlarge_visual()

    def shrink_current(self):
        self.hide_big_visual()
        self.hide_big_graph()
        self.hide_big_model3d()
        if getattr(self, "_geo", None) is not None and self._geo["big"]:
            self._geo_close()

    def clear_visual(self, keep_renderer=False):
        """Take the picture off the board. It belonged to the last question.

        `keep_renderer`: a new 3D model is about to replace this one, so the
        3D worker stays up rather than being ended and started again."""
        self.canvas.delete(self.VISUAL_TAG)
        self.canvas.delete(self.BIG_VISUAL_TAG)
        self._visual_photos = []
        self._big_visual_photos = []
        self._visual_source = None
        self._drop_graph()
        self._drop_geometry()
        self._visual_steps = []
        self._step_current = -1
        self._step_views = {}
        self._drop_model3d(close_worker=not keep_renderer)

    # -----------------------------------------------------------------
    # the step strip
    # -----------------------------------------------------------------
    # WHY A STRIP UNDERNEATH RATHER THAN A GLOW ON THE BOX ITSELF. Lighting up
    # the actual "Tadpole" box would be better, and it is not available: the
    # diagram on the board was drawn by Seedream and arrives as a photograph of
    # a diagram, with no boxes in it that this can reach. What it does still
    # have is the LIST -- the stages were sent to Seedream in order and are kept
    # in state.current_visual -- so the order is drawn under the picture as pips
    # and the one she is speaking about is filled in.
    #
    # Pips plus one spelled-out label, rather than a chip per stage with its
    # text in it. The board is 309 wide and "Tadpole with legs" is not going to
    # fit into a fifth of that at any size a person can read.
    STEP_STRIP_H = 22
    BIG_STEP_STRIP_H = 40

    def _draw_step_strip(self, box, tag, small):
        """The pips and the current stage's name, for one view."""
        x0, y0, x1, y1 = box
        labels = self._visual_steps
        if not labels:
            return
        radius = 5 if small else 9
        gap = radius * 2 + (7 if small else 12)
        mid = (y0 + y1) / 2
        left = x0 + (8 if small else 20)
        pips = []
        for index in range(len(labels)):
            cx = left + index * gap
            pips.append(self.canvas.create_oval(
                cx - radius, mid - radius, cx + radius, mid + radius,
                fill=COL_TRACK, outline="", tags=tag))
        name = self.canvas.create_text(
            left + len(labels) * gap + (4 if small else 10), mid, text="",
            anchor="w", font=self._font(7 if small else 13, True),
            fill=COL_INDIGO, tags=tag)
        self._step_views[tag] = {"pips": pips, "name": name,
                                 "width": x1 - (left + len(labels) * gap) - 8,
                                 "small": small}
        self._paint_steps()

    def _paint_steps(self):
        """Put every view's pips and label where _step_current says."""
        labels = self._visual_steps
        for view in self._step_views.values():
            for index, pip in enumerate(view["pips"]):
                self.canvas.itemconfig(
                    pip, fill=COL_INDIGO if index == self._step_current
                    else COL_TRACK)
            if 0 <= self._step_current < len(labels):
                font = self._font(7 if view["small"] else 13, True)
                text = f"{self._step_current + 1}. {labels[self._step_current]}"
                text = self._ellipsize(text, font, max(40, view["width"]))
            else:
                text = ""
            self.canvas.itemconfig(view["name"], text=text)

    # Words too common to identify a stage. "The sun heats the sea" and "the
    # tail is reabsorbed" share three of them, and a match on any one would
    # light up the wrong pip for the whole explanation.
    _STEP_STOPWORDS = {"the", "and", "of", "a", "an", "in", "to", "it", "is",
                       "its", "with", "into", "then", "from", "for", "on",
                       "are", "was", "this", "that", "they", "their"}

    def _step_hit(self, label, spoken):
        """True when this stage is what the spoken line is about."""
        if label.lower() in spoken:
            return True
        return any(word in spoken for word in
                   (w for w in re.findall(r"[a-z]+", label.lower())
                    if len(w) >= 3 and w not in self._STEP_STOPWORDS))

    def _match_step(self, line):
        """Light the stage the line she is speaking belongs to.

        Ties break toward the stage AFTER the current one, because an
        explanation runs forwards: "the tadpole loses its tail and becomes a
        froglet" names two stages, and the one she is arriving at is the one
        worth showing.
        """
        labels = self._visual_steps
        if not labels or not line:
            return
        spoken = line.lower()
        hits = [i for i, label in enumerate(labels) if self._step_hit(label, spoken)]
        if not hits:
            return
        wanted = self._step_current + 1
        best = min(hits, key=lambda i: (abs(i - wanted), -i))
        if best != self._step_current:
            self._step_current = best
            self._paint_steps()

    # -----------------------------------------------------------------
    # the live graph
    # -----------------------------------------------------------------
    # Canvas lines rather than a PNG, and that is the whole point of it. A
    # picture of y = x^2 answers the question; a curve that MOVES while the
    # student drags the power from 2 to 3 answers the next four. Everything
    # here is arithmetic and create_line, so a drag costs about a millisecond
    # and there is no network anywhere in the loop.
    #
    # ONE MODEL, TWO VIEWS. The board shows it small and the enlarged screen
    # shows it big, and both can be up at once -- so the knob values live in
    # self._graph and each view is redrawn from them. Dragging the big slider
    # moves the little one underneath it, which is what makes closing the big
    # view leave the board showing what the student just made.
    GRAPH_TAG = "boardgraph"
    BIG_GRAPH_TAG = "biggraph"
    GRAPH_LINE = "#1D4ED8"
    GRAPH_AXIS = "#98A4BE"
    GRAPH_GRID = "#EDF1FA"
    # Three sizes of the one graph: on the home screen's board ("small"), as a
    # card beside the writing on the full-screen page ("card"), and over the
    # whole screen ("big"). Everything that differs between them is here.
    GRAPH_SIZES = {
        "small": dict(corner=10, title=14, heading=7, pad=8, label=20, axis=6, line=2,
                      legend=13, legend_font=7, row=20, knob=6, track=4, name=7,
                      readout=34, edge=10),
        "card": dict(corner=16, title=34, heading=12, pad=12, label=26, axis=8, line=3,
                     legend=20, legend_font=10, row=32, knob=10, track=6, name=11,
                     readout=46, edge=14),
        "big": dict(corner=16, title=30, heading=12, pad=26, label=34, axis=9, line=4,
                    legend=26, legend_font=12, row=38, knob=11, track=7, name=11,
                    readout=62, edge=30),
    }

    def show_graph(self, spec):
        """Put a formula on the board as a curve with its numbers on sliders --
        several formulas as several curves, each in its own colour. With the
        page open full screen it goes ON the page, as a card beside the
        writing, the way a teacher draws the graph next to the sum."""
        camera.stop_live("a graph is going on the board")
        self.clear_visual()
        self._graph = {"spec": spec,
                       "values": [k["value"] for k in spec["knobs"]],
                       "views": {}}
        app_state.current_graph = visuals.describe(spec, self._graph["values"])
        self._draw_graph(self.VISUAL_BOX, self.GRAPH_TAG, "small")
        print(f"[UI] Live graph on the board: {spec['source']}", flush=True)
        if self._wb_open:
            self._wb_show_graph()
        self._restack()

    def _drop_graph(self):
        """The graph is gone from every view: the board, the big screen, and
        the card on the page."""
        self.canvas.delete(self.GRAPH_TAG)
        self.canvas.delete(self.BIG_GRAPH_TAG)
        self.canvas.delete(self.WB_GRAPH_TAG)
        had_card = self._wb_graph_open
        self._graph = None
        self._wb_graph_open = False
        if had_card and self._wb_open:
            self._wb_draw_toolbar()
            self._wb_draw_offers()
            self._wb_draw_chip()
            self._wb_draw_suggestions()

    def _graph_curves(self):
        spec = self._graph["spec"]
        return spec.get("curves") or [{"label": spec["source"], "colour": self.GRAPH_LINE}]

    def _graph_curve(self, view, curve=0):
        """The screen points for one curve as the knobs stand, in Tk's order.

        A list per unbroken run, so a hole in the function is a gap on the
        screen and not a line drawn straight through the asymptote.
        """
        spec = self._graph["spec"]
        points = visuals.sample(spec, self._graph["values"], curve)
        x0, y0, x1, y1 = view["plot"]
        low, high = view["y_low"], view["y_high"]
        left, right = spec["x_low"], spec["x_high"]
        # CLIPPED TO THE PLOT, and it has to be: a canvas has no clip region,
        # so a point computed outside the box is simply DRAWN outside it. The
        # y window is fixed when the view is built, and dragging the power on
        # y = x^2 up to four takes the curve to 625 on a sheet scaled to 25 --
        # which came out as a parabola straight up through the board's heading
        # and into the music card at the top of the screen.
        #
        # Each run therefore ends ON the edge it leaves through and the next
        # one begins on the edge it comes back through, so the curve goes off
        # the top of the graph the way it does on paper.
        runs, run, edge = [], [], None
        for x, y in points:
            if y is None:                      # a hole: lift the pen
                if len(run) >= 4:
                    runs.append(run)
                run, edge = [], None
                continue
            sx = x0 + (x - left) / (right - left) * (x1 - x0)
            sy = y1 - (y - low) / (high - low) * (y1 - y0)
            if y0 <= sy <= y1:
                if not run and edge:
                    run.extend(edge)
                run.extend((sx, sy))
                edge = None
                continue
            edge = (sx, max(y0, min(y1, sy)))
            if run:
                run.extend(edge)
                if len(run) >= 4:
                    runs.append(run)
                run = []
        if len(run) >= 4:
            runs.append(run)
        return runs

    def _nice_ticks(self, low, high, count=4):
        """Round numbers across a range -- 1, 2, 5 times a power of ten."""
        span = high - low
        if span <= 0:
            return [low]
        rough = max(span / count, 1e-12)
        power = 10 ** math.floor(math.log10(rough))
        for multiple in (1, 2, 2.5, 5, 10):
            step = multiple * power
            if span / step <= count + 1:
                break
        first = math.ceil(low / step) * step
        ticks, value = [], first
        while value <= high + step * 0.01:
            ticks.append(0.0 if abs(value) < step * 1e-6 else value)
            value += step
        return ticks

    def _draw_graph(self, box, tag, size):
        """One view of the current graph: "small", "card" or "big"."""
        graph = self._graph
        if graph is None:
            return
        spec = graph["spec"]
        m = self.GRAPH_SIZES[size]
        c = self.canvas
        x0, y0, x1, y1 = box
        sliders = spec["knobs"]
        curves = self._graph_curves()
        ground = tag + "ground"
        self._round_rect(x0, y0, x1, y1, m["corner"], fill="#FFFFFF",
                         outline="#C7D2FE" if size == "card" else "#D8E0F0",
                         width=2 if size == "card" else 1, tags=(tag, ground))
        heading = spec["title"] or (spec["source"] if len(curves) == 1 else "")
        top = y0 + m["pad"] / 2
        if size == "card":
            # Left-aligned, clear of the enlarge and close buttons on its right.
            words = self._ellipsize(heading or "Graph", self._font(m["heading"], True),
                                    x1 - x0 - 110)
            c.create_text(x0 + 16, y0 + m["title"] / 2 + 2, text=words, anchor="w",
                          font=self._font(m["heading"], True), fill=COL_INDIGO, tags=(tag,))
            top = y0 + m["title"]
        elif heading:
            c.create_text((x0 + x1) / 2, y0 + m["title"] / 2 + 2,
                          text=self._ellipsize(heading, self._font(m["heading"], True),
                                               x1 - x0 - 44),
                          font=self._font(m["heading"], True), fill=COL_INDIGO, tags=(tag,))
            top = y0 + m["title"]
        if len(curves) > 1:
            top = self._draw_graph_legend(curves, (x0 + m["edge"], top, x1 - m["edge"]),
                                          m, tag)
        # The plot keeps everything the sliders do not need. Axis numbers hang
        # below and to the left of it, so it is inset for them rather than
        # sitting on the card's edge.
        label = m["label"]
        plot = (x0 + m["pad"] + label, top + m["pad"] / 2,
                x1 - m["pad"], y1 - m["pad"] - label / 2 - len(sliders) * m["row"])
        view = {"plot": plot, "box": box, "size": size, "small": size == "small",
                "rows": [], "extra": (self.WB_TAG,) if tag == self.WB_GRAPH_TAG else ()}
        graph["views"][tag] = view
        # The same window in every view, whatever its shape: the card, the
        # board and the big screen should be showing one graph, not three.
        view["y_low"], view["y_high"] = visuals.y_window(spec, graph["values"])
        self._draw_graph_frame(view, tag)
        view["lines"] = []
        self._repaint_graph(tag)
        name_font = self._font(m["name"], True)
        view["label_w"] = max([24] + [name_font.measure(k["label"]) + 14 for k in sliders])
        for index, knob in enumerate(sliders):
            self._draw_slider(view, tag, index, knob,
                              y1 - m["pad"] - (len(sliders) - index) * m["row"])
        if size == "small":
            # The same glyph in the same corner as a picture's. From the
            # student's side this IS the picture, and a board that marks the
            # same gesture two different ways teaches them to ignore both.
            #
            # It carries its own binding because the only thing that opens the
            # small graph otherwise is the curve, and a 2px line is not a target
            # a child can hit. Drawn last so its chip covers the tail of a long
            # title rather than fighting with it.
            icon = tag + "expand"
            self._expand_icon(x1 - 17, y0 + 17, icon)
            self.canvas.addtag_withtag(tag, icon)
            self.canvas.tag_bind(icon, "<Button-1>", self._enlarge_graph)
        else:
            # A press on the card is the card's: not a line started under it,
            # and not a tap that wakes her.
            c.tag_bind(ground, "<ButtonPress-1>", self._swallow_press)

    def _swallow_press(self, event):
        """A press on something that is not a button but is in the way of
        the page or the home screen under it: it goes nowhere."""
        self._tap_handled = event.serial
        return "break"

    def _draw_graph_legend(self, curves, row, m, tag):
        """Each formula in its curve's colour, with a stroke of that colour in
        front of it: left to right, onto a second line when the first is full.
        Returns where the plot can start."""
        left, top, right = row
        font = self._font(m["legend_font"], True)
        swatch, gap = (14 if m["legend"] < 16 else 20), 14
        x, y, lines = left, top + m["legend"] / 2, 1
        for curve in curves:
            words = self._ellipsize(curve["label"], font, right - left - swatch - 6)
            width = swatch + 6 + font.measure(words)
            if x > left and x + width > right:
                if lines == 2:
                    break
                x, y, lines = left, y + m["legend"], lines + 1
            self.canvas.create_line(x, y, x + swatch, y, fill=curve["colour"],
                                    width=max(2, m["line"]), capstyle="round", tags=(tag,))
            self.canvas.create_text(x + swatch + 6, y, text=words, anchor="w", font=font,
                                    fill=curve["colour"], tags=(tag,))
            x += width + gap
        return top + lines * m["legend"]

    def _draw_graph_frame(self, view, tag):
        """Grid, axes and the numbers along them. Fixed for the life of a view.

        The y window is settled when the view is built and does not move while
        a slider is dragged, deliberately: a graph whose axis rescales under
        the finger makes every curve look identical and hides the one thing the
        drag was meant to show.
        """
        spec = self._graph["spec"]
        m = self.GRAPH_SIZES[view["size"]]
        px0, py0, px1, py1 = view["plot"]
        low, high = view["y_low"], view["y_high"]
        left, right = spec["x_low"], spec["x_high"]
        size = self._font(m["axis"])
        items = (tag,)
        # A sine wave is marked in π, the way it is on paper: 0, π, 2π.
        x_ticks = spec.get("x_ticks") or [(v, f"{v:g}") for v in self._nice_ticks(left, right)]
        for value, words in x_ticks:
            sx = px0 + (value - left) / (right - left) * (px1 - px0)
            self.canvas.create_line(sx, py0, sx, py1, fill=self.GRAPH_GRID,
                                    tags=items)
            self.canvas.create_text(sx, py1 + m["axis"] + 1, text=words,
                                    font=size, fill=COL_TEXT_DIM, tags=items)
        for value in self._nice_ticks(low, high):
            sy = py1 - (value - low) / (high - low) * (py1 - py0)
            self.canvas.create_line(px0, sy, px1, sy, fill=self.GRAPH_GRID,
                                    tags=items)
            self.canvas.create_text(px0 - 3, sy, text=f"{value:g}", anchor="e",
                                    font=size, fill=COL_TEXT_DIM, tags=items)
        if low <= 0 <= high:
            sy = py1 - (0 - low) / (high - low) * (py1 - py0)
            self.canvas.create_line(px0, sy, px1, sy, fill=self.GRAPH_AXIS,
                                    width=1, tags=items)
        if left <= 0 <= right:
            sx = px0 + (0 - left) / (right - left) * (px1 - px0)
            self.canvas.create_line(sx, py0, sx, py1, fill=self.GRAPH_AXIS,
                                    width=1, tags=items)

    def _repaint_graph(self, tag):
        """Move the curves to wherever the knobs now are. The whole live part."""
        view = self._graph["views"].get(tag)
        if view is None:
            return
        for item in view.get("lines", []):
            self.canvas.delete(item)
        # A new canvas item lands on top of EVERYTHING. Dragging the big slider
        # repaints the board's copy too, so its curve was being drawn over the
        # enlarged graph -- stray lines across the full-screen view, a fresh
        # set on every drag -- and over its own slider rows and expand icon.
        # So the curve is slotted back in just above its view's grid each time.
        # The first repaint comes straight after _draw_graph_frame, so the item
        # on top of this view at that moment IS the last line of the grid.
        if "under" not in view:
            view["under"] = self.canvas.find_withtag(tag)[-1]
        width = self.GRAPH_SIZES[view["size"]]["line"]
        view["lines"] = []
        for index, curve in enumerate(self._graph_curves()):
            view["lines"] += [
                self.canvas.create_line(*run, fill=curve["colour"], smooth=True,
                                        width=width,
                                        tags=(tag, tag + "curve") + view["extra"])
                for run in self._graph_curve(view, index)]
        if view["lines"]:
            self.canvas.tag_raise(tag + "curve", view["under"])
        self.canvas.tag_bind(tag + "curve", "<Button-1>",
                             self._enlarge_graph if view["small"] else
                             self._swallow_press)

    def _draw_slider(self, view, tag, index, knob, top):
        """Label, track and knob for one number, and the taps that move it."""
        m = self.GRAPH_SIZES[view["size"]]
        x0, _, x1, _ = view["box"]
        pad, row = m["edge"], m["row"]
        mid = top + row / 2
        name = self._font(m["name"], True)
        track = (x0 + pad + view["label_w"], x1 - pad - m["readout"])
        row_tag = f"{tag}s{index}"
        # An invisible catcher across the whole row, so a finger that lands
        # anywhere near the track moves the knob. A three-pixel line is not a
        # touch target on a screen operated with a thumb.
        self.canvas.create_rectangle(x0 + pad, top, x1 - pad, top + row,
                                     fill="#FFFFFF", outline="",
                                     tags=(tag, row_tag))
        # A letter's knob is in the colour of the curve it moves.
        colour = knob.get("colour") or COL_INDIGO
        self.canvas.create_text(x0 + pad, mid, text=knob["label"], anchor="w",
                                font=name, fill=colour if knob.get("colour") else COL_TEXT_DIM,
                                tags=(tag, row_tag))
        self.canvas.create_line(track[0], mid, track[1], mid, fill=COL_TRACK,
                                width=m["track"], capstyle="round",
                                tags=(tag, row_tag))
        radius = m["knob"]
        dot = self.canvas.create_oval(0, 0, 0, 0, fill=colour,
                                      outline="#FFFFFF", width=2,
                                      tags=(tag, row_tag))
        readout = self.canvas.create_text(x1 - pad, mid, text="", anchor="e",
                                          font=name, fill=COL_TEXT,
                                          tags=(tag, row_tag))
        view["rows"].append({"track": track, "mid": mid, "dot": dot,
                             "radius": radius, "readout": readout})
        self._place_knob(view, index)
        for sequence in ("<Button-1>", "<B1-Motion>"):
            self.canvas.tag_bind(row_tag, sequence,
                                 lambda event, i=index, t=tag: self._drag_knob(t, i, event))

    def _place_knob(self, view, index):
        """Put one slider's dot and readout where its value says they go."""
        knob = self._graph["spec"]["knobs"][index]
        value = self._graph["values"][index]
        row = view["rows"][index]
        left, right = row["track"]
        fraction = (value - knob["low"]) / (knob["high"] - knob["low"])
        x = left + max(0.0, min(1.0, fraction)) * (right - left)
        radius = row["radius"]
        self.canvas.coords(row["dot"], x - radius, row["mid"] - radius,
                           x + radius, row["mid"] + radius)
        self.canvas.itemconfig(row["readout"], text=f"{value:g}")

    def _drag_knob(self, tag, index, event):
        """A finger on a slider. Every view redraws, not just the touched one."""
        self._tap_handled = event.serial
        graph = self._graph
        if graph is None:
            return "break"
        view = graph["views"].get(tag)
        if view is None:
            return "break"
        knob = graph["spec"]["knobs"][index]
        left, right = view["rows"][index]["track"]
        fraction = max(0.0, min(1.0, (event.x - left) / max(1.0, right - left)))
        value = knob["low"] + fraction * (knob["high"] - knob["low"])
        step = knob["step"]
        value = round(round(value / step) * step, 6)
        if value == graph["values"][index]:
            return "break"
        graph["values"][index] = value
        app_state.current_graph = visuals.describe(graph["spec"], graph["values"])
        for other, panel in list(graph["views"].items()):
            self._place_knob(panel, index)
            self._repaint_graph(other)
        return "break"

    def _enlarge_graph(self, event=None):
        """The same graph over the whole screen, sliders and all."""
        if event is not None:
            self._tap_handled = event.serial
        if self._graph is None:
            return "break"
        self.canvas.delete(self.BIG_GRAPH_TAG)
        self._graph["views"].pop(self.BIG_GRAPH_TAG, None)
        ground = self.BIG_GRAPH_TAG + "edge"
        self.canvas.create_rectangle(0, 0, UI_W, UI_H, fill="#0B1020", outline="",
                                     tags=(self.BIG_GRAPH_TAG, ground))
        self.canvas.tag_bind(ground, "<Button-1>", lambda e: self.hide_big_graph(e))
        # The dark ground around the card is the close button, so it is left
        # wide enough to be hit with a thumb -- 30px up the sides and a clear
        # 46px strip along the bottom, where the words telling you so are.
        self._draw_graph((30, 22, UI_W - 30, UI_H - 46), self.BIG_GRAPH_TAG, "big")
        self.canvas.create_text(UI_W / 2, UI_H - 16, text="tap the dark edge to close",
                                font=self._font(9), fill="#8891A8",
                                tags=(self.BIG_GRAPH_TAG,))
        # "Tap the dark edge" is a rule you have to be told. The cross is one
        # you already know, and it needs its own binding because the close here
        # lives on the backdrop, not on the card or anything drawn over it.
        close = self.BIG_GRAPH_TAG + "close"
        self._close_icon(UI_W - 26, 26, close)
        self.canvas.addtag_withtag(self.BIG_GRAPH_TAG, close)
        self.canvas.tag_bind(close, "<Button-1>", lambda e: self.hide_big_graph(e))
        self.canvas.tag_raise(self.BIG_GRAPH_TAG)
        if self.canvas.find_withtag(self.STOP_PILL_TAG):
            self.canvas.tag_raise(self.STOP_PILL_TAG)
        return "break"

    def hide_big_graph(self, event=None):
        if event is not None:
            self._tap_handled = event.serial
        self.canvas.delete(self.BIG_GRAPH_TAG)
        if self._graph is not None:
            self._graph["views"].pop(self.BIG_GRAPH_TAG, None)
        return "break"

    # -----------------------------------------------------------------
    # the geometry lab: a solid or a figure to turn and tap
    # -----------------------------------------------------------------
    # A cube that turns under a finger, where a tap on edge AE and then on
    # face ABCD says AE is perpendicular to it and draws the right angle where
    # they meet; where three corners tapped are cut through, and a triangle's
    # corner dragged changes its angles while they go on adding up to 180°.
    #
    # Drawn here with canvas items rather than by the 3D worker, and that is
    # what makes the tapping possible: every corner, edge and face is drawn at
    # a place on the screen this knows exactly, so a tap is a pick -- where a
    # frame from the worker is only pixels. The measuring is geometry.py's;
    # this is the glass and the finger. Like the live graph, there are two
    # views of one figure: small on the board, and the lab over everything.
    GEO_TAG = "geolab"
    GEO_SMALL_TAG = "boardgeo"
    GEO_TOP, GEO_BOTTOM = 52, 432
    GEO_SPLIT = 522            # the figure left of this, the readings right of it
    GEO_DRAG_DEG = 0.5         # degrees of turn per pixel of drag
    GEO_FACE = "#DCE7FF"
    GEO_EDGE = "#1E293B"
    GEO_HIDDEN = "#94A3B8"
    GEO_HINT_3D = ("Drag to turn it. Tap a corner, an edge or a face; tap two to see how "
                   "they meet; tap three corners to cut through them.")
    GEO_HINT_FLAT = ("Drag a corner to change the shape. Tap a corner for its angle, two "
                     "sides for the angle between them, two corners for a diagonal.")

    def show_geometry(self, figure, picks=None):
        """Tk thread: the lab, open on this figure, pointing at these picks."""
        camera.stop_live("a figure is going on the board")
        self.clear_visual()
        self._geo = {"figure": figure, "picks": list(picks or []),
                     "yaw": 0.0 if figure.flat else -32.0,
                     "pitch": 0.0 if figure.flat else 22.0,
                     "big": False, "press": None, "frozen": None,
                     "caption": ("", "hint")}
        self._geo_job = None
        self._geo_note()
        self._geo_draw_small()
        self._geo_open()
        print(f"[GEOMETRY] Open: {figure.title}", flush=True)

    def geometry_pick(self, picks):
        """Tk thread: point at these parts of the figure that is up (None
        keeps what was picked), and open the lab if it was shut."""
        geo = getattr(self, "_geo", None)
        if geo is None:
            return
        if picks is not None:
            geo["picks"] = list(picks)
        self._geo_note()
        if geo["big"]:
            self._geo_redraw()
        else:
            self._geo_draw_small()
            self._geo_open()

    def geometry_figure(self):
        """Any thread: the figure in the lab, or None."""
        geo = getattr(self, "_geo", None)
        return geo["figure"] if geo else None

    def geometry_open(self):
        """Any thread: is the lab open full screen?"""
        geo = getattr(self, "_geo", None)
        return bool(geo and geo["big"])

    def _geo_note(self):
        """What the model is told about the lab (state.current_geometry), and
        a line in the log for each change of pick."""
        geo = self._geo
        figure = geo["figure"]
        app_state.current_geometry = geometry.lab_line(figure, geo["picks"])
        if geo["picks"]:
            print(f"[GEOMETRY] {app_state.current_geometry[:220]}", flush=True)

    # ---- seeing it from where the finger has turned it ----
    def _geo_view(self, box, small):
        """Where the figure goes in `box`: its middle, its scale, its turn."""
        geo = self._geo
        if geo["frozen"] is not None and not small:
            return geo["frozen"]
        figure = geo["figure"]
        x0, y0, x1, y1 = box
        points = figure.points
        low = [min(p[k] for p in points) for k in range(3)]
        high = [max(p[k] for p in points) for k in range(3)]
        centre = [(low[k] + high[k]) / 2 for k in range(3)]
        if figure.flat:
            width, height = max(high[0] - low[0], 1e-6), max(high[1] - low[1], 1e-6)
            scale = min((x1 - x0) * 0.7 / width, (y1 - y0) * 0.68 / height)
        else:
            radius = max(math.dist(p, centre) for p in points) or 1.0
            scale = min(x1 - x0, y1 - y0) * 0.43 / radius
        return {"centre": centre, "scale": scale, "mid": ((x0 + x1) / 2, (y0 + y1) / 2),
                "yaw": math.radians(geo["yaw"]), "pitch": math.radians(geo["pitch"])}

    @staticmethod
    def _geo_turn(view, v):
        """A direction turned the way the figure is turned."""
        yaw, pitch = view["yaw"], view["pitch"]
        x, y, z = v
        x, z = x * math.cos(yaw) + z * math.sin(yaw), -x * math.sin(yaw) + z * math.cos(yaw)
        y, z = y * math.cos(pitch) - z * math.sin(pitch), y * math.sin(pitch) + z * math.cos(pitch)
        return x, y, z

    def _geo_project(self, view, p):
        """(screen x, screen y, nearness) of a point of the figure."""
        cx, cy, cz = view["centre"]
        x, y, z = self._geo_turn(view, (p[0] - cx, p[1] - cy, p[2] - cz))
        mx, my = view["mid"]
        return mx + x * view["scale"], my - y * view["scale"], z

    def _geo_draw_figure(self, tags, box, small):
        """The figure, its picks and what they measure, into `box`. Returns
        where everything landed, which is what a tap is matched against."""
        c = self.canvas
        geo = self._geo
        figure = geo["figure"]
        view = self._geo_view(box, small)
        screen = [self._geo_project(view, p) for p in figure.points]
        faces = figure.faces
        if figure.flat:
            visible = [True] * len(faces)
        else:
            visible = [self._geo_turn(view, figure.face_normal(k))[2] > 1e-6
                       for k in range(len(faces))]
        colours = {pick: geometry.PICK_COLOURS[i % len(geometry.PICK_COLOURS)]
                   for i, pick in enumerate(geo["picks"])}
        reading = geometry.measure(figure, geo["picks"])
        light = (-0.35, 0.75, 0.55)

        def flat_coords(indices):
            return [v for i in indices for v in screen[i][:2]]

        # Faces, the far ones first. Only the ones turned this way are filled:
        # the others are behind, and show through as their dashed edges.
        order = sorted(range(len(faces)),
                       key=lambda k: sum(screen[i][2] for i in faces[k]) / len(faces[k]))
        for k in order:
            if not visible[k]:
                continue
            group = figure.groups.get(k)
            picked = colours.get(("surface", group) if group else ("face", k))
            if picked:
                fill = _mix(picked, "#FFFFFF", 0.72)
            elif figure.flat:
                fill = "#EEF2FF"
            else:
                normal = self._geo_turn(view, figure.face_normal(k))
                bright = max(0.0, sum(a * b for a, b in zip(normal, light)))
                fill = _mix(self.GEO_FACE, "#FFFFFF", 0.15 + 0.55 * bright)
            c.create_polygon(*flat_coords(faces[k]), fill=fill, outline="", tags=tags)
        for mark in reading.marks:
            if mark[0] == "polygon":
                coords = [v for p in mark[1] for v in self._geo_project(view, p)[:2]]
                c.create_polygon(*coords, fill=_mix(mark[2], "#FFFFFF", 0.35), stipple="gray50",
                                 outline=mark[2], width=2, tags=tags)
        # Edges: solid where they can be seen, dashed where they are behind.
        for edge in figure.edges:
            a, b = edge
            beside = figure.edge_faces.get(edge, [])
            seen = figure.flat or any(visible[k] for k in beside)
            if edge in figure.smooth:
                # Inside a curved surface: drawn only where it is the outline.
                if len(beside) == 2 and visible[beside[0]] != visible[beside[1]]:
                    c.create_line(*flat_coords((a, b)), fill=self.GEO_EDGE,
                                  width=1 if small else 2, tags=tags)
                continue
            group = figure.groups.get(edge)
            picked = colours.get(("rim", group) if group else ("edge", a, b))
            if picked:
                c.create_line(*flat_coords((a, b)), fill=picked, width=3 if small else 5,
                              dash=() if seen else (7, 4), capstyle="round", tags=tags)
            elif seen:
                c.create_line(*flat_coords((a, b)), fill=self.GEO_EDGE,
                              width=1.5 if small else 2.5, capstyle="round", tags=tags)
            else:
                c.create_line(*flat_coords((a, b)), fill=self.GEO_HIDDEN, width=1 if small else 1.5,
                              dash=(5, 4), tags=tags)
        # A round solid's radius and height, and what the picks drew.
        for g, (_name, a, b) in enumerate(figure.guides):
            picked = colours.get(("guide", g))
            c.create_line(*flat_coords((a, b)), fill=picked or "#64748B",
                          width=(3 if small else 4) if picked else 1.5,
                          dash=() if picked else (4, 3), tags=tags)
        arc_size = (max(math.dist(p, view["centre"]) for p in figure.points) or 1.0) * 0.16
        placed = []     # the boxes of the angles' numbers, kept from landing on each other
        for mark in reading.marks:
            if mark[0] == "segment":
                _kind, p, q, colour, dashed = mark
                c.create_line(*self._geo_project(view, p)[:2], *self._geo_project(view, q)[:2],
                              fill=colour, width=2 if small else 3,
                              dash=(6, 4) if dashed else (), capstyle="round", tags=tags)
            elif mark[0] == "arc" and not small:
                self._geo_draw_arc(view, mark, arc_size, tags, placed)
        # The corners, lettered, the letters pushed out from the middle.
        mx, my = view["mid"]
        font = self._font(8 if small else 13, True)
        for i in sorted(figure.corners):
            label = figure.labels[i]
            sx, sy, _z = screen[i]
            beside = [k for k, face in enumerate(faces) if i in face]
            seen = figure.flat or not beside or any(visible[k] for k in beside)
            picked = colours.get(("vertex", i))
            dot = (5 if picked else 3) if small else (8 if picked else 4.5)
            c.create_oval(sx - dot, sy - dot, sx + dot, sy + dot,
                          fill=picked or (self.GEO_EDGE if seen else self.GEO_HIDDEN),
                          outline="#FFFFFF" if picked else "", width=2, tags=tags)
            if label:
                dx, dy = sx - mx, sy - my
                away = math.hypot(dx, dy) or 1.0
                push = 11 if small else 19
                c.create_text(sx + dx / away * push, sy + dy / away * push, text=label,
                              font=font, fill=picked or (COL_TEXT if seen else COL_TEXT_DIM),
                              tags=tags)
        if (figure.flat and not small and figure.kind == "flat" and len(faces) == 1
                and len(faces[0]) <= 10):
            # A flat figure's sides, measured where they are.
            ring = faces[0]
            for a, b in zip(ring, ring[1:] + ring[:1]):
                (ax, ay, _), (bx, by, _) = screen[a], screen[b]
                ex, ey = (ax + bx) / 2 - mx, (ay + by) / 2 - my
                away = math.hypot(ex, ey) or 1.0
                length = math.dist(figure.points[a], figure.points[b])
                c.create_text((ax + bx) / 2 + ex / away * 16, (ay + by) / 2 + ey / away * 16,
                              text=geometry.number(length), font=self._font(9),
                              fill=COL_TEXT_DIM, tags=tags)
        return {"points": screen, "visible": visible, "order": order, "view": view}

    def _geo_draw_arc(self, view, mark, size, tags, placed=None):
        """An angle, drawn where it is: an arc between the two arms -- or the
        little square of a right angle -- and its size beside it."""
        _kind, centre, u, v, colour, words, facing = mark
        if facing is not None and not self._geo["figure"].flat \
                and self._geo_turn(view, facing)[2] < -0.05:
            return      # drawn in a face on the far side
        dot = sum(a * b for a, b in zip(u, v))
        w = tuple(b - dot * a for a, b in zip(u, v))
        length = math.sqrt(sum(x * x for x in w))
        if length < 1e-9:
            return
        w = tuple(x / length for x in w)
        theta = math.acos(max(-1.0, min(1.0, dot)))

        def at(t, r):
            return self._geo_project(view, tuple(
                centre[k] + r * (math.cos(t) * u[k] + math.sin(t) * w[k]) for k in range(3)))[:2]
        c = self.canvas
        if abs(math.degrees(theta) - 90) < 0.5:
            s = size * 0.6
            corner = [tuple(centre[k] + s * u[k] for k in range(3)),
                      tuple(centre[k] + s * u[k] + s * w[k] for k in range(3)),
                      tuple(centre[k] + s * w[k] for k in range(3))]
            c.create_line(*[v for p in corner for v in self._geo_project(view, p)[:2]],
                          fill=colour, width=2, tags=tags)
        else:
            points = [at(theta * i / 16, size) for i in range(17)]
            c.create_line(*[v for p in points for v in p], fill=colour, width=2, tags=tags)
        # A narrow angle's number goes further out along it, clear of the
        # numbers of the angles beside it at the same corner.
        reach = 1.9 + max(0.0, (50.0 - math.degrees(theta)) / 50.0) * 1.8
        font = self._font(10, True)
        half = font.measure(words) / 2 + 5
        placed = [] if placed is None else placed
        for extra in (0.0, 0.8, 1.6, 2.4, 3.2):
            lx, ly = at(theta / 2, size * (reach + extra))
            box = (lx - half - 2, ly - 12, lx + half + 2, ly + 12)
            if not any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                       for b in placed):
                break
        placed.append(box)
        self._round_rect(lx - half, ly - 10, lx + half, ly + 10, 8, fill="#FFFFFF",
                         outline=_mix(colour, "#FFFFFF", 0.5), tags=tags)
        c.create_text(lx, ly, text=words, font=font, fill=colour, tags=tags)

    # ---- on the board ----
    def _geo_draw_small(self):
        """The figure as it stands, small on the board; a tap opens the lab."""
        c, tag = self.canvas, self.GEO_SMALL_TAG
        c.delete(tag)
        geo = getattr(self, "_geo", None)
        if geo is None:
            return
        x0, y0, x1, y1 = self.VISUAL_BOX
        self._round_rect(x0, y0, x1, y1, 10, fill="#FFFFFF", outline="#D8E0F0", tags=(tag,))
        c.create_text(x0 + 12, y0 + 12, text=self._ellipsize(geo["figure"].title,
                                                             self._font(8, True), x1 - x0 - 60),
                      anchor="w", font=self._font(8, True), fill=COL_INDIGO, tags=(tag,))
        self._geo_draw_figure((tag,), (x0 + 6, y0 + 22, x1 - 6, y1 - 6), small=True)
        self._expand_icon(x1 - 17, y0 + 17, tag)
        c.tag_bind(tag, "<Button-1>", self._geo_open)
        self._restack()

    # ---- the lab ----
    def _geo_open(self, event=None):
        if event is not None:
            self._tap_handled = event.serial
        geo = getattr(self, "_geo", None)
        if geo is None:
            return "break"
        geo["big"] = True
        self._geo_draw_big()
        return "break"

    def _geo_close(self, event=None):
        if event is not None:
            self._tap_handled = event.serial
        geo = getattr(self, "_geo", None)
        self.canvas.delete(self.GEO_TAG)
        if geo is not None:
            geo["big"] = False
            geo["press"] = geo["frozen"] = None
            self._geo_draw_small()
        return "break"

    def _drop_geometry(self):
        """The figure is gone from both views."""
        self.canvas.delete(self.GEO_TAG)
        self.canvas.delete(self.GEO_SMALL_TAG)
        if getattr(self, "_geo_job", None):
            self.root.after_cancel(self._geo_job)
        self._geo_job = None
        self._geo = None
        app_state.current_geometry = None

    def _geo_draw_big(self):
        c, tag = self.canvas, self.GEO_TAG
        c.delete(tag)
        c.create_rectangle(0, 0, UI_W, UI_H, fill="#FFFFFF", outline="", tags=(tag,))
        area = tag + "area"
        c.create_rectangle(0, self.GEO_TOP, self.GEO_SPLIT, self.GEO_BOTTOM, fill="#FFFFFF",
                           outline="", tags=(tag, area))
        fig = tag + "fig"
        for target in (area, fig):
            c.tag_bind(target, "<ButtonPress-1>", self._geo_press)
            c.tag_bind(target, "<B1-Motion>", self._geo_drag)
            c.tag_bind(target, "<ButtonRelease-1>", self._geo_release)
        self._geo_draw_top()
        self._geo_redraw()
        self._geo_draw_bar()
        c.tag_raise(tag)
        if c.find_withtag(self.STOP_PILL_TAG):
            c.tag_raise(self.STOP_PILL_TAG)

    def _geo_redraw(self, figure_only=False):
        """The figure, and -- unless only the view turned -- what it reads."""
        geo = getattr(self, "_geo", None)
        if geo is None or not geo["big"]:
            return
        c, tag = self.canvas, self.GEO_TAG
        fig = tag + "fig"
        c.delete(fig)
        self._geo_screen = self._geo_draw_figure(
            (tag, fig), (0, self.GEO_TOP, self.GEO_SPLIT, self.GEO_BOTTOM), small=False)
        if not figure_only:
            self._geo_draw_panel()
            self._geo_draw_top()
        # New lines land on top of everything; the panel and the bars stay over them.
        for part in ("panel", "top", "bar"):
            if c.find_withtag(tag + part):
                c.tag_raise(tag + part)
        if c.find_withtag(self.STOP_PILL_TAG):
            c.tag_raise(self.STOP_PILL_TAG)

    def _geo_schedule(self):
        """A redraw soon -- one per frame, however many touches came in."""
        if getattr(self, "_geo_job", None) is None:
            self._geo_job = self.root.after(28, self._geo_frame)

    def _geo_frame(self):
        self._geo_job = None
        geo = getattr(self, "_geo", None)
        if geo is not None:
            self._geo_redraw(figure_only=not geo["figure"].flat)

    def _geo_draw_top(self):
        c, tag = self.canvas, self.GEO_TAG
        top = tag + "top"
        c.delete(top)
        geo = self._geo
        tags = (tag, top)
        c.create_rectangle(0, 0, UI_W, self.GEO_TOP, fill="#FFFFFF", outline="", tags=tags)
        c.create_line(0, self.GEO_TOP, UI_W, self.GEO_TOP, fill=COL_FRAME, width=2, tags=tags)
        cy = self.GEO_TOP / 2
        title = self._ellipsize(geo["figure"].title, self._font(13, True),
                                (480 if geo["figure"].flat else 486) - 16)
        c.create_text(16, cy, text=title, anchor="w", font=self._font(13, True),
                      fill=COL_INDIGO, tags=tags)

        def button(name, x0, x1, words, command):
            button_tag = f"{top}_{name}"
            self._round_rect(x0, cy - 17, x1, cy + 17, 14, fill="#FFFFFF", outline=COL_INDIGO,
                             width=2, tags=tags + (button_tag,))
            c.create_text((x0 + x1) / 2, cy, text=words, font=self._font(11, True),
                          fill=COL_INDIGO, tags=tags + (button_tag,))
            c.tag_bind(button_tag, "<ButtonPress-1>",
                       lambda e: self._wb_tapped(e, command))
        if not geo["figure"].flat:
            button("turn", 514, 616, "Turn back", self._geo_reset_view)
        if geo["picks"]:
            button("clear", 626, 712, "Clear", self._geo_clear)
        close = top + "_close"
        self._close_icon(UI_W - 26, cy, tags + (close,))
        c.tag_bind(close, "<ButtonPress-1>",
                   lambda e: self._wb_tapped(e, lambda: self.root.after_idle(self._geo_close)))

    def _geo_draw_panel(self):
        """What the picks measure, in their colours -- or, with nothing
        picked, what there is to know about the figure and how to start."""
        c, tag = self.canvas, self.GEO_TAG
        panel = tag + "panel"
        c.delete(panel)
        geo = self._geo
        figure = geo["figure"]
        tags = (tag, panel)
        x0, y0, x1, y1 = self.GEO_SPLIT, self.GEO_TOP + 2, UI_W, self.GEO_BOTTOM
        # Its ground takes a stray press -- bound on the ground alone: a
        # "break" bound to the whole panel would stop the Ask button's own.
        ground = panel + "_ground"
        c.create_rectangle(x0, y0, x1, y1, fill="#F8FAFF", outline="", tags=tags + (ground,))
        c.tag_bind(ground, "<ButtonPress-1>", self._swallow_press)
        c.create_line(x0, y0, x0, y1, fill=COL_FRAME, width=2, tags=tags)
        left, width = x0 + 16, x1 - x0 - 30
        ask_top = y1 - 52
        y = y0 + 12
        if geo["picks"]:
            # A chip per pick, in its colour: what was tapped, in order.
            x = left
            for i, pick in enumerate(geo["picks"]):
                colour = geometry.PICK_COLOURS[i % len(geometry.PICK_COLOURS)]
                words = figure.element_name(pick)
                chip = self._font(10, True).measure(words) + 22
                if x + chip > x1 - 12:
                    x, y = left, y + 30
                self._round_rect(x, y, x + chip, y + 24, 12, fill=_mix(colour, "#FFFFFF", 0.85),
                                 outline=colour, width=2, tags=tags)
                c.create_text(x + chip / 2, y + 12, text=words, font=self._font(10, True),
                              fill=colour, tags=tags)
                x += chip + 6
            y += 34
            lines = geometry.measure(figure, geo["picks"]).lines
        else:
            lines = [(text, None, i == 0) for i, text in enumerate(self._geo_facts())]
        for text, colour, bold in lines:
            if y > ask_top - 18:
                c.create_text(left, y, text="…", anchor="nw", font=self._font(10, True),
                              fill=COL_TEXT_DIM, tags=tags)
                break
            item = c.create_text(left, y, text=text, anchor="nw", width=width,
                                 font=self._font(11 if bold else 10, bold),
                                 fill=colour or (COL_TEXT if bold else COL_TEXT_DIM), tags=tags)
            y = c.bbox(item)[3] + (8 if bold else 5)
        if not geo["picks"]:
            item = c.create_text(left, y + 4, text=self.GEO_HINT_FLAT if figure.flat
                                 else self.GEO_HINT_3D, anchor="nw", width=width,
                                 font=self._font(10), fill="#7C3AED", tags=tags)
            if c.bbox(item)[3] > ask_top - 6:
                c.delete(item)
        ask = panel + "_ask"
        self._round_rect(left, ask_top, x1 - 14, y1 - 12, 18, fill=COL_INDIGO, outline=COL_INDIGO,
                         tags=tags + (ask,))
        self._sparkle(left + 24, (ask_top + y1 - 12) / 2, 7, "#FFFFFF", tags + (ask,))
        c.create_text((left + x1 - 14) / 2 + 10, (ask_top + y1 - 12) / 2,
                      text="Ask Liza about it" if geo["picks"] else "Ask Liza",
                      font=self._font(12, True), fill="#FFFFFF", tags=tags + (ask,))
        c.tag_bind(ask, "<ButtonPress-1>", lambda e: self._wb_tapped(e, self._geo_ask))

    def _geo_facts(self):
        """With nothing picked: a flat figure's angles as they stand -- live,
        so dragging a corner shows them change and their total not -- and a
        solid's counts and formulas."""
        figure = self._geo["figure"]
        if figure.flat and figure.kind == "flat" and len(figure.faces[0]) <= 10:
            return geometry.live_facts(figure)
        return [figure.title] + figure.facts

    def _geo_draw_bar(self):
        """Along the bottom: how to use it -- or her words, while she talks."""
        c, tag = self.canvas, self.GEO_TAG
        bar = tag + "bar"
        c.delete(bar)
        geo = self._geo
        tags = (tag, bar)
        self._geo_photos = []
        c.create_rectangle(0, self.GEO_BOTTOM, UI_W, UI_H, fill=COL_BG, outline="", tags=tags)
        c.create_line(0, self.GEO_BOTTOM, UI_W, self.GEO_BOTTOM, fill=COL_FRAME, width=2,
                      tags=tags)
        text, who = geo["caption"]
        if not text:
            # How to use it is in the panel; this strip is for her words.
            text, who = "Pick something, then tap Ask Liza to hear why.", "hint"
        colour = {"liza": STATE_STYLE["speaking"][1], "user": COL_INDIGO}.get(who, COL_TEXT_DIM)
        line = text if who == "hint" else ("Liza: " if who == "liza" else "You: ") + text
        self._overlay_text(16, (self.GEO_BOTTOM + UI_H) / 2, line, 11, fill=colour, anchor="w",
                           tags=tags, width=UI_W - 32, max_lines=2, justify="left",
                           photos=self._geo_photos)
        c.tag_bind(bar, "<ButtonPress-1>", self._swallow_press)

    def _geo_set_caption(self, text, speaker):
        geo = getattr(self, "_geo", None)
        if geo is None:
            return
        geo["caption"] = (text, speaker)
        if geo["big"]:
            self._geo_draw_bar()

    # ---- the finger ----
    def _geo_press(self, event):
        self._tap_handled = event.serial
        geo = self._geo
        press = {"x": event.x, "y": event.y, "last": (event.x, event.y), "moved": False,
                 "corner": None}
        figure = geo["figure"]
        if figure.flat and figure.kind == "flat":
            # A corner under the finger is picked up to be moved, the view
            # held still meanwhile so the corner stays under the finger.
            screen = self._geo_screen["points"]
            near = [(math.hypot(screen[i][0] - event.x, screen[i][1] - event.y), i)
                    for i in figure.corners if figure.labels[i]]
            near = [n for n in near if n[0] < 22]
            if near:
                press["corner"] = min(near)[1]
                geo["frozen"] = self._geo_screen["view"]
        geo["press"] = press
        return "break"

    def _geo_drag(self, event):
        geo = getattr(self, "_geo", None)
        press = geo and geo["press"]
        if not press:
            return "break"
        if not press["moved"] and math.hypot(event.x - press["x"], event.y - press["y"]) < 7:
            return "break"
        press["moved"] = True
        figure = geo["figure"]
        if figure.flat:
            if press["corner"] is not None:
                view = geo["frozen"]
                mx, my = view["mid"]
                sx = min(max(event.x, 20), self.GEO_SPLIT - 20)
                sy = min(max(event.y, self.GEO_TOP + 20), self.GEO_BOTTOM - 20)
                figure.points[press["corner"]] = ((sx - mx) / view["scale"] + view["centre"][0],
                                                  -(sy - my) / view["scale"] + view["centre"][1],
                                                  0.0)
                geometry.reshaped(figure)
                self._geo_schedule()
            return "break"
        lx, ly = press["last"]
        press["last"] = (event.x, event.y)
        geo["yaw"] += (event.x - lx) * self.GEO_DRAG_DEG
        geo["pitch"] = max(-85.0, min(85.0, geo["pitch"] + (event.y - ly) * self.GEO_DRAG_DEG))
        self._geo_schedule()
        return "break"

    def _geo_release(self, event):
        geo = getattr(self, "_geo", None)
        if geo is None:
            return "break"
        press, geo["press"] = geo["press"], None
        reshaped = press is not None and press["moved"] and press["corner"] is not None
        geo["frozen"] = None
        if press is not None and not press["moved"]:
            element = self._geo_hit(event.x, event.y)
            if element is not None:
                geo["picks"] = geometry.add_pick(geo["figure"], geo["picks"], element)
                self._geo_note()
            self._geo_redraw()
        elif reshaped:
            self._geo_note()
            self._geo_redraw()
        return "break"

    def _geo_hit(self, x, y):
        """The corner, edge or face under a tap: a corner within a fingertip
        first, then the nearest line, then the face in front."""
        figure = self._geo["figure"]
        screen = self._geo_screen
        points, visible = screen["points"], screen["visible"]
        best = None
        for i in figure.corners:
            if not figure.labels[i]:
                continue
            d = math.hypot(points[i][0] - x, points[i][1] - y)
            if d < 18 and (best is None or d < best[0]):
                best = (d, ("vertex", i))
        if best:
            return best[1]

        def gap(a, b):
            (ax, ay, _), (bx, by, _) = points[a], points[b]
            dx, dy = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / max(1e-9, dx * dx + dy * dy)))
            return math.hypot(ax + t * dx - x, ay + t * dy - y)
        for edge in figure.edges:
            if edge in figure.smooth:
                continue
            seen = figure.flat or any(visible[k] for k in figure.edge_faces.get(edge, []))
            d = gap(*edge) + (0 if seen else 3)
            if d < 11 and (best is None or d < best[0]):
                group = figure.groups.get(edge)
                best = (d, ("rim", group) if group else ("edge", edge[0], edge[1]))
        for g, (_name, a, b) in enumerate(figure.guides):
            d = gap(a, b)
            if d < 11 and (best is None or d < best[0]):
                best = (d, ("guide", g))
        if best:
            return best[1]
        for k in reversed(screen["order"]):
            if not visible[k]:
                continue
            ring = [points[i] for i in figure.faces[k]]
            inside = False
            for (ax, ay, _), (bx, by, _) in zip(ring, ring[1:] + ring[:1]):
                if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                    inside = not inside
            if inside:
                group = figure.groups.get(k)
                return ("surface", group) if group else ("face", k)
        return None

    def _geo_reset_view(self):
        geo = self._geo
        geo["yaw"], geo["pitch"] = -32.0, 22.0
        self._geo_redraw()

    def _geo_clear(self):
        self._geo["picks"] = []
        self._geo_note()
        self._geo_redraw()

    def _geo_ask(self):
        """Ask Liza about what is picked: handed to ai_loop like the board's
        Ask, with what was picked and what it measures in the question."""
        geo = self._geo
        figure = geo["figure"]
        language = self._wb_language()
        name = re.sub(r"^your\s+", "", figure.name, flags=re.IGNORECASE).lower()
        if geo["picks"]:
            headline = geometry.measure(figure, geo["picks"]).headline()
            question = GEOMETRY_ASK_QUESTIONS.get(language, GEOMETRY_ASK_QUESTIONS["en"]).format(
                figure=name, picks=", ".join(figure.element_name(p) for p in geo["picks"]),
                reading=headline)
        else:
            question = GEOMETRY_ABOUT_QUESTIONS.get(
                language, GEOMETRY_ABOUT_QUESTIONS["en"]).format(figure=name)
        whiteboard.ask(question, language, source="geometry")
        print("[GEOMETRY] Ask Liza tapped.", flush=True)
        self.asleep = False
        sleep_event.clear()
        if playback_active.is_set() or not audio_queue.empty():
            assistant.interrupt_playback()
        self.set_state("thinking")

    def clear_transcript(self):
        """Put the board back to its opening line.

        Called when the device changes hands. Whoever is next did not ask about
        photosynthesis, and leaving the last student's answer up says they did.
        """
        self.transcript = ""
        self.speaker = "user"
        # A new student did not turn the camera on, and should not find it on.
        camera.stop_live("a different student")
        # Nor write on somebody else's page, or tap their buttons.
        self._wb_reset()
        app_state.current_offers = []
        self.show_offers([])
        self.clear_visual()
        app_state.current_graph = None
        app_state.current_visual = None
        # MARK THE LINE THAT IS SHOWING AS ALREADY SHOWN, which is not the same
        # as marking nothing shown, and the difference is a bug you can watch
        # happen. caption_now() has no end: once a reply has finished speaking it
        # goes on returning that reply's last cue for as long as the session
        # stands. So an index of -1 here told _follow_spoken_captions that cue
        # was new, and 120ms after the board was cleared it painted the previous
        # student's answer straight back onto it -- switch from Akshay to Sahil
        # after a question about frogs and Sahil gets the frogs.
        session = audio.caption_session()
        self._caption_shown = (session, audio.caption_now(session)[0])
        self.canvas.itemconfig(self.speaker_id, text="You said:", fill=COL_INDIGO)
        self.canvas.itemconfig(
            self.transcript_id, fill=COL_TEXT_DIM,
            text="Tap SPEAK or say \u201cHey Liza\u201d to begin.")

    def set_transcript(self, text, speaker="user"):
        text = (text or "").strip()
        if not text:
            return
        # A NEW QUESTION USED TO TAKE THE DIAGRAM DOWN WITH IT, and that was
        # wrong for the question students actually ask next. "Can you explain
        # it?" is a question ABOUT the picture, and wiping the picture to answer
        # it leaves them listening to a description of something they can no
        # longer see -- which for a teaching device is the whole value gone.
        #
        # So the board now holds until something replaces it: another visual, a
        # change of student, or her own [ACTION: hide_visual] when the
        # conversation has genuinely moved off it. The model is told what is up
        # there in ON_BOARD and decides which of those it is, because it is the
        # only part of this that can tell "explain it" from "what is gravity".
        # The FULL line is kept here and only the drawn copy is clipped.
        self.transcript = text
        self.speaker = speaker
        liza = speaker == "liza"
        self.canvas.itemconfig(self.speaker_id,
                               text="Liza said:" if liza else "You said:",
                               fill=STATE_STYLE["speaking"][1] if liza else COL_INDIGO)
        self.canvas.itemconfig(self.transcript_id, text=self._fit_transcript(text),
                               fill=COL_TEXT)
        # The page covers this board, so it carries the same words along its
        # own bottom edge -- and so does the geometry lab.
        self._wb_set_caption(text, "liza" if liza else "user")
        self._geo_set_caption(text, "liza" if liza else "user")

    def wake_up(self, event=None):
        """The Speak button. Wakes her from anything, including sleep."""
        print("[UI] Speak tapped. Waking up...", flush=True)
        self.asleep = False
        sleep_event.clear()
        wake_event.set()
        # Said on THIS thread, now, rather than left to ai_loop.
        #
        # ai_loop only repaints the caption when it reaches the top of its
        # standby loop, and the wake-word read it is sitting in runs for up to
        # WAKE_LISTEN_TIMEOUT_S + WAKE_PHRASE_LIMIT_S. So the panel went on
        # saying "Sleeping" for as much as sixteen seconds after the button was
        # pressed, which is far longer than anyone waits before deciding the
        # button is broken and pressing it again. The read is cancelled on
        # wake_event now (see ai_loop's standby branch), but the caption still
        # has to change on the tap itself for the button to feel connected.
        self.set_state("listening")
        return "break"

    def tap_to_wake(self, event=None):
        """Any tap on the screen away from a control.

        Ignored while she is asleep, which is the whole point of the Sleep
        button: this panel reports stray touches on its own, and treating
        those as a wake would put her straight back to listening. Asleep,
        only the Speak button or the wake word count.
        """
        if event is not None and event.serial == self._tap_handled:
            # An overlay button already took this tap -- see _overlay_press.
            return
        tags = self.canvas.gettags("current")
        if self.CHATS_TAG in tags:
            # Opening the chat history is not a request to start listening.
            return
        if "btnEXIT" in tags:
            # Returning "break" from the Exit button's own binding does not
            # stop this root binding, and arming Exit must not start the mic.
            return
        if self.STOP_PILL_TAG in tags:
            # stop_speaking has it; waking on the same tap would only log noise.
            return
        if self.CAMERA_TAG in tags or self.CAMERA_BTN_TAG in tags:
            # The live picture and the camera button are not requests to start
            # listening; waking on them set wake_event in the middle of a look,
            # and the next listen began as if Speak had been pressed.
            return
        if (self.MODEL3D_TAG in tags or self.BIG_MODEL3D_TAG in tags
                or self.GEO_TAG in tags or self.GEO_SMALL_TAG in tags):
            # Turning a 3D model is a dozen touches a minute. Each one waking
            # her would open the microphone on a child who is only looking.
            # The geometry lab is turned and tapped the same way.
            return
        if self.OFFER_TAG in tags:
            # A button she offered: it shows the thing, she need not listen.
            return
        if self.WB_TAG in tags or self.BOARD_BTN_TAG in tags or self._wb_open:
            # Writing on the page is a hundred touches a minute, and none of
            # them is "listen to me" -- that is the page's own mic button.
            return
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
        if self.asleep:
            # ASLEEP MEANS ASLEEP. The intro below is what wakes her: ai_loop
            # speaks it and then opens a session, because a tap on a mode card
            # was taken as proof somebody is there. Tapped after Sleep, that put
            # her back to listening to an empty room behind a screen that still
            # looked asleep, and on 30 Sep she answered a sentence Whisper had
            # made up out of the silence. The card's highlight says which mode it
            # is, which is all a sleeping device needs to say.
            print(f"[MODE] Now in {self.current_mode} mode (asleep, so not "
                  f"announced).", flush=True)
            return

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
        # ONLY WHILE SOMETHING IS ACTUALLY PLAYING.
        #
        # Nothing clears this flag but interrupt_playback(), and the only place
        # that runs for a mode tap is ai_loop's pending_mode_intro handler at
        # the top of its loop -- which standby does not reach. So a mode tapped
        # while she was in standby (which is most of the time) left the flag
        # set, and audio_player_worker drops every item it is handed while it is
        # set: she woke, listened, thought, and then played nothing at all. The
        # mode had in fact changed; she had simply been struck dumb on the way
        # to saying so, which is exactly what "I cannot switch modes" looks like
        # from the outside. Set only when there is a reply to cut short, which
        # is the case the flag was added for.
        if playback_active.is_set() or not audio_queue.empty():
            stop_playback_event.set()
        app_state.pending_mode_intro = MODE_INTROS[self.current_mode]

    def _mode_tapped(self, event, index):
        """A mode card. Not also a wake: set_mode hands ai_loop the intro, and
        that handler opens the session itself -- the same tap reaching
        tap_to_wake as well cut the intro off as "Speak tapped during the
        reply" (logs/liza.log, 30 Sep)."""
        self._tap_handled = event.serial
        self.set_mode(index)

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
        # Deleting a window item only unmaps the widget in it, so the scrolling
        # panes of the chat history would pile up hidden behind every screen.
        for widget in self._overlay_widgets:
            widget.destroy()
        self._overlay_widgets = []
        self._overlay_photos = []
        self.overlay = None

    # The lesson screens, where tapping her means "stop, I want to ask you
    # something". Not the keyboard screens: she is behind the backing there and
    # cannot be tapped at all, and not the pickers, where nothing is being said.
    # kg_story_list is a picker, and it is here anyway: it is the one picker
    # she talks over, because it reads the titles out for the child who cannot
    # read them. A child who wants the third one before she has finished the
    # list needs a way to stop her.
    KG_ASK_SCREENS = {"kg_alpha", "kg_count", "kg_story", "kg_story_list"}

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
    # A pill on the wall to the left of the board, not the 144x110 card it was.
    # Against a painted classroom that card read as a hole cut in the room; the
    # tinted worksheet it was drawn for had nothing behind it to interrupt.
    KG_ASK_BOX = (24, 198, 172, 246)
    # The story screen has no free column -- its caption fills the board -- so
    # it stands in the bottom row instead, in the second of the four slots.
    KG_ASK_BOX_STORY = (204, 394, 366, 442)

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
                radius=12 if roomy else (y1 - y0) // 2,
                sub="tap to stop" if roomy else None,
                label_frac=0.58 if roomy else None, sub_frac=0.80)
            self.canvas.addtag_withtag(self.KG_ASK_TAG, tag)
            return

        tag = self._overlay_button(
            x0, y0, x1, y1, "Ask me", self._kg_ask, fill="#F59E0B", size=14,
            radius=12 if roomy else (y1 - y0) // 2,
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
        # And the panel stops saying she is listening, because she is not. The
        # press set it to "listening" and only a spoken reply ever put it back,
        # so a press that heard nothing at all left the caption up for good.
        # set_state ignores this while she is still talking, which is what makes
        # it safe to call unconditionally.
        self.set_state("idle")
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
                        backdrop=None, backdrop_at=(0, 0), chalk=False):
        """The opaque backing every profile and KG screen is built on.

        `chalk` writes the title and subtitle ON the blackboard instead of at
        the top of the screen -- in chalk, centred on the board rather than on
        the canvas. It is a flag rather than four colour-and-position arguments
        because there is one classroom and every screen in it is written on the
        same board; a caller that wants to place its own wording anywhere else
        should draw it itself.

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
            # A new screen is a new visit, so what it records is recorded
            # again -- see _kg_note.
            self._kg_noted = set()
        self._clear_overlay()
        self.overlay = name
        # The wallpaper first, with this screen's colour washed over it. Held in
        # the module-level cache rather than self._overlay_photos, which
        # _clear_overlay empties -- these are shared between screens and must
        # outlive any one of them.
        wallpaper = _overlay_backing(tint)
        if wallpaper is not None:
            backing = self.canvas.create_image(0, 0, anchor="nw", image=wallpaper,
                                               tags=self.OVERLAY_TAG)
        else:
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
        if chalk:
            self._overlay_text(BOARD_MID, 90, title, 19, bold=True, fill=CHALK)
            if subtitle:
                self._overlay_text(BOARD_MID, 114, subtitle, 10, fill=CHALK_DIM)
            return
        self._overlay_text(UI_W / 2, 40, title, 20, bold=True, fill=COL_TEXT)
        if subtitle:
            self._overlay_text(UI_W / 2, 68, subtitle, 10, fill=COL_TEXT_DIM)

    def _overlay_button(self, x0, y0, x1, y1, label, command, fill=COL_INDIGO,
                        text_colour="#FFFFFF", radius=12, size=13, sub=None,
                        label_frac=None, sub_frac=0.68, wrap=False, label_dx=0):
        """A pill with a word on it.

        The wording goes through _overlay_text rather than straight to
        canvas.create_text, so a Devanagari label comes out SHAPED. Tk cannot
        join Devanagari -- it draws the consonants and the vowel signs as
        separate glyphs in the order they are stored -- and the "हिंदी" button
        on the story language picker has been wrong on this screen since the
        day it was drawn. Latin text takes the same path it always did.

        `wrap` gives the label the pill's own width to wrap inside, for the
        buttons whose text is a title somebody else wrote rather than a word
        chosen to fit. `label_dx` shifts the wording off the pill's centre, for
        the pills with something else standing at one end of them.
        """
        self._overlay_seq += 1
        tag = f"ovbtn{self._overlay_seq}"
        tags = (self.OVERLAY_TAG, tag)
        height = y1 - y0
        self._round_rect(x0, y0, x1, y1, radius, fill=fill, outline=fill, tags=tags)
        if label_frac is not None:
            label_y = y0 + height * label_frac
        else:
            label_y = (y0 + y1) / 2 if not sub else y0 + height * 0.36
        room = (x1 - x0) - 20 - 2 * abs(label_dx)
        self._overlay_text((x0 + x1) / 2 + label_dx, label_y, label, size,
                           bold=True, fill=text_colour, tags=tags,
                           width=room if wrap else None)
        if sub:
            self._overlay_text((x0 + x1) / 2 + label_dx, y0 + height * sub_frac,
                               sub, 8, fill=text_colour, tags=tags,
                               width=room if wrap else None)
        if command is not None:
            self.canvas.tag_bind(tag, "<Button-1>",
                                 lambda e: self._overlay_press(e, command))
        return tag

    def _overlay_press(self, event, command):
        """Run an overlay button, and keep the same tap from waking her.

        tap_to_wake is bound to the ROOT, so it sees every tap after the item's
        own binding has run -- and a button that CLOSES the screen (Close, Done,
        a name in the profile picker) has already cleared self.overlay by then,
        so the guard there let the tap through and the microphone opened on a
        child who had only closed a screen. logs/liza.log has five "Speak
        tapped" lines straight after "[PROFILE] Active". The serial is the X
        event's own, the same for every binding one tap reaches."""
        self._tap_handled = event.serial
        command()

    # ---------- the classroom screens ----------
    # The strip of pills along the floor, and the slots they stand in.
    #
    # FIXED slots, not an even spread over however many buttons a screen has
    # this time. Back belongs at the far left of every screen that has one, and
    # the alphabet screen loses its Previous button on the first letter -- a row
    # that reflows would slide the remaining three under the finger already
    # reaching for one of them, which on a screen built for four-year-olds is
    # the difference between Next and Back.
    KG_ROW_Y0, KG_ROW_Y1 = 394, 442
    KG_ROW_3 = ((44, 204), (320, 480), (596, 756))
    KG_ROW_4 = ((24, 186), (204, 366), (430, 596), (614, 776))

    def _kg_class_screen(self, name, title, subtitle=None):
        """A KG lesson in the classroom, with its heading written on the board."""
        self._overlay_screen(name, title, subtitle,
                             backdrop=KG_CLASS_BG, chalk=True)

    def _kg_class_button(self, slot, label, command, primary=False, arrow=None,
                         size=12):
        """One pill on the classroom floor.

        Capitals, because that is the alphabet these screens are teaching and a
        child part-way through it can pick "NEXT" out of a row where they could
        not pick "Next" -- and because the wording is for the adult beside them
        anyway; the colour is what the child reads.
        """
        x0, x1 = slot
        y0, y1 = self.KG_ROW_Y0, self.KG_ROW_Y1
        fill = CLASS_BLUE if primary else CLASS_PILL
        ink = "#FFFFFF" if primary else CLASS_INK
        # Shrunk to fit rather than trusted to. Every label here is chosen by
        # the screen and none of them is measured by whoever writes it, so a
        # wording change of two characters is enough to push the text out
        # through the ends of its own pill -- which is what "That's enough" did
        # at 118px inside 112. One point at a time down to 9, because a label
        # that has to go smaller than that is the wrong label, not a font
        # problem, and a caller should hear about it.
        text = label.upper()
        room = (x1 - x0) - (52 if arrow else 26)
        while size > 9 and self._font(size, True).measure(text) > room:
            size -= 1
        if self._font(size, True).measure(text) > room:
            print(f"[UI] Button label {label!r} does not fit its pill; "
                  f"shortening it.", flush=True)
            text = self._ellipsize(text, self._font(size, True), room)
        tag = self._overlay_button(x0, y0, x1, y1, text, command,
                                   fill=fill, text_colour=ink,
                                   radius=(y1 - y0) // 2, size=size)
        if arrow:
            # Same tag as the pill, so the mark is part of the target rather
            # than a hole in the middle of it.
            cy = (y0 + y1) / 2
            if arrow == "left":
                x = x0 + 24
                pts = (x + 5, cy - 8, x + 5, cy + 8, x - 6, cy)
            else:
                x = x1 - 24
                pts = (x - 5, cy - 8, x - 5, cy + 8, x + 6, cy)
            self.canvas.create_polygon(*pts, fill=ink, outline=ink,
                                       tags=(self.OVERLAY_TAG, tag))
        return tag

    def _kg_glass_card(self, box, radius=22, wash=0.46):
        """The frosted panel a lesson is presented on. Falls back to a flat card."""
        card = glass_card(tuple(box), radius, wash)
        if card is None:
            self._round_rect(box[0], box[1], box[2], box[3], radius,
                             fill="#EFF3F8", outline="#CBD5E1",
                             tags=self.OVERLAY_TAG)
            return
        self._place_overlay_asset(card, box[0], box[1])

    # ---------- the chip that shows who is using the device ----------
    def _build_profile_chip(self):
        """Who is using the device, as the middle card of the header.

        It is the only control for WHO she is talking to, and the whole card is
        the tap target -- 212x78 rather than the 210x26 chip it replaces, which
        was under half what a fingertip reliably hits on this panel.
        """
        tags = (self.OVERLAY_TAG + "_never", "profilechip")
        art = ui_asset("Home", "Profile_bg.png", box=CARD_BOX)
        if art is not None:
            self.profile_chip_bg = self._place_asset(art, WHO_X0, TOP_Y0, tags)
        else:
            self.profile_chip_bg = self._card(WHO_X0, TOP_Y0, WHO_X1, TOP_Y1, 16,
                                              tags=tags)

        # Centred in the card, not 8px below it. TOP_Y0 + 39 put the middle of a
        # 46px picture at y=56 on a card running 17..79, so its bottom edge
        # landed on the card's bottom edge and the animal read as having slipped
        # off the row rather than as sitting beside the name.
        cx, cy = WHO_X0 + 34, (TOP_Y0 + TOP_Y1) // 2
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
            anchor="w", font=self._font(12, True), fill=COL_CARD_DIM, tags=tags)
        self.profile_chip_class = self.canvas.create_text(
            text_x, TOP_Y0 + 45, text="", anchor="w",
            font=self._font(9), fill=COL_CARD_DIM, tags=tags)

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
            colour = COL_CARD_TEXT
        else:
            label, klass, colour = "Tap to set up", "", COL_CARD_DIM
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
        if active:
            # Top left, opposite Close. This is the only way in for a student
            # past KG: they never see the KG home screen, where the other one
            # is.
            self._overlay_button(20, 20, 172, 56, "Progress", self.show_progress,
                                 fill="#E6E9F5", text_colour=COL_TEXT, size=10)

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

    # ---------- what they have been learning ----------
    # The screen the progress log exists for. Two halves, and the order is the
    # point: what they have DONE across the top in big numbers, and what they
    # did most recently underneath it.
    #
    # A parent is the reader here, and a child is looking over their shoulder --
    # which is why nothing on it is a percentage, nothing is red, and a story
    # never carries a mark. The one thing this screen must not become is a
    # report card for a five-year-old.
    KG_PROGRESS_KINDS = {
        "story":    ("Stories heard",     "#B45309"),
        "letters":  ("Letters practised", "#2563EB"),
        "spelling": ("Words spelled",     "#7C3AED"),
        "counting": ("Counting",          "#059669"),
        "order":    ("Alphabet order",    "#D97706"),
        "test":     ("Tests taken",       "#0EA5E9"),
        "lesson":   ("Topics learned",    "#0F766E"),
        "retell":   ("Topics re-told",    "#BE185D"),
    }

    def show_progress(self):
        profile = profiles.active_profile() or {}
        name = profile.get("name") or "this student"
        user_id = profile.get("user_id")
        try:
            totals = store.progress_totals(user_id) if user_id else []
            recent = store.recent_activities(user_id, limit=6) if user_id else []
        except Exception as exc:
            print(f"[PROGRESS] Could not read the log ({exc}).", flush=True)
            totals, recent = [], []

        self._overlay_screen("progress", f"{name}'s progress",
                             "Everything you have been learning",
                             backdrop=("KG Activity", "Glass_BG.png"),
                             backdrop_at=(19, 20))

        if not totals:
            # An empty log is the normal state of a brand new student, so it
            # gets a sentence rather than an empty grid with a zero in it.
            self._round_rect(120, 150, 680, 280, 14, fill="#FFFFFF",
                             outline=COL_CARD_EDGE, tags=self.OVERLAY_TAG)
            self._overlay_text(UI_W / 2, 215,
                               "Nothing here yet. Spell a word, hear a story or "
                               "take a test, and it will show up here.",
                               12, fill=COL_TEXT_DIM, width=480)
        # Five across is what fits at a size the numbers can be read from
        # arm's length, and five covers everything a KG child does bar one.
        # Anything past the fifth kind is still in the list below.
        for index, row in enumerate(totals[:5]):
            label, colour = self.KG_PROGRESS_KINDS.get(
                row["kind"], (row["kind"].title(), "#475569"))
            x0 = 34 + index * 147
            self._round_rect(x0, 88, x0 + 135, 174, 14, fill="#FFFFFF",
                             outline=COL_CARD_EDGE, tags=self.OVERLAY_TAG)
            # For everything except a test, the interesting number is how many
            # DIFFERENT things -- eighteen letters, not the ninety times they
            # were looked at. A test is the opposite: each sitting counts.
            count = row["times"] if row["kind"] == "test" else row["different"]
            self._overlay_text(x0 + 67, 118, str(count), 26, bold=True,
                               fill=colour)
            self._overlay_text(x0 + 67, 145, label, 9, fill=COL_TEXT, width=124)
            self._overlay_text(x0 + 67, 162, self._progress_note(row), 8,
                               fill=COL_TEXT_DIM, width=124)

        if recent:
            # A card under the list, for the same reason the summary numbers
            # have one: this screen sits on the wallpaper, and small grey text
            # on a rainbow is not text anybody reads.
            self._round_rect(34, 190, 766, 384, 14, fill="#FFFFFF",
                             outline=COL_CARD_EDGE, tags=self.OVERLAY_TAG)
            self._overlay_text(50, 208, "Lately", 10, bold=True,
                               fill=COL_TEXT_DIM, anchor="w")
        for index, row in enumerate(recent[:6]):
            y = 232 + index * 25
            _label, colour = self.KG_PROGRESS_KINDS.get(
                row["kind"], (row["kind"], "#475569"))
            self.canvas.create_oval(50, y - 4, 58, y + 4, fill=colour,
                                    outline="", tags=self.OVERLAY_TAG)
            self._overlay_text(70, y, row["topic"], 10, bold=True,
                               fill=COL_TEXT, anchor="w", width=210,
                               max_lines=1, justify="left")
            self._overlay_text(296, y, row.get("detail") or "", 9,
                               fill=COL_TEXT_DIM, anchor="w", width=370,
                               max_lines=1, justify="left")
            self._overlay_text(750, y, self._when(row.get("created_at")), 9,
                               fill=COL_TEXT_DIM, anchor="e")

        self._overlay_button(UI_W / 2 - 110, 406, UI_W / 2 + 110, 452, "Done",
                             self.dismiss_overlay, fill=COL_INDIGO, size=13)

    def _progress_note(self, row):
        """The small line under a summary card, or "" when it would only
        repeat the big number above it.

        Four different letters over four visits said "4" and then "4 times",
        which is two labels for one fact. It earns its line only when the two
        numbers actually differ -- four letters looked at nine times is worth
        knowing.
        """
        if row.get("available"):
            return f"{row['scored']} of {row['available']} marks"
        times, different = row.get("times") or 0, row.get("different") or 0
        return f"{times} times" if times > different else ""

    def _when(self, value):
        """"today", "yesterday", "3 days ago" -- from either store.

        The database hands back a datetime and the JSON fallback hands back the
        string it was written as, and this screen must not say two different
        things about the same row depending on which one answered.
        """
        if not value:
            return ""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return ""
        try:
            now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
            days = (now.date() - value.date()).days
        except Exception:
            return ""
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days} days ago"
        if days < 60:
            weeks = days // 7
            return "a week ago" if weeks == 1 else f"{weeks} weeks ago"
        return value.strftime("%d %b")

    # ---------- chat history ----------
    # The three-line button in the top-left corner and the screen it opens: the
    # active student's past conversations down the left, newest first under a
    # heading per day, and whichever one is chosen read back on the right -- the
    # shape every chat app has taught children to expect.
    #
    # ONLY THE ACTIVE STUDENT'S. The list comes from chatlog.conversations for
    # the id on the profile chip, and the query under that filters on it, so
    # nothing here can put one child's conversation in front of another.
    CHATS_TAG = "chatsbtn"
    # The corner left of the clock card: 99px of wallpaper, header height.
    CHATS_BOX = (19, TOP_Y0, 81, TOP_Y1)
    CHATS_LIST_BOX = (16, 88, 290, 466)
    CHATS_READ_BOX = (302, 88, 784, 466)
    # How far a finger has to move before a press is a scroll and not a tap.
    CHATS_DRAG_SLOP = 8

    def _build_chats_button(self):
        x0, y0, x1, y1 = self.CHATS_BOX
        w, h = x1 - x0, y1 - y0
        # Cut from the profile card's own artwork, left end and right end joined,
        # so the corners and the glow match the three cards beside it. Squashing
        # the 204px card to 62 would have turned its corners into ovals.
        art = ui_asset("Home", "Profile_bg.png", box=CARD_BOX)
        tags = (self.CHATS_TAG,)
        if art is not None:
            half = w // 2
            face = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            face.paste(art.crop((0, 0, half, h)), (0, 0))
            face.paste(art.crop((art.width - (w - half), 0, art.width, h)), (half, 0))
            item = self._place_asset(face, x0, y0, tags)
        else:
            face = _card_image(w, h, 16)
            item = self._place_photo(face, x0, y0, tags)
        cx = (x0 + x1) / 2
        for dy in (-15, -7, 1):
            self.canvas.create_line(cx - 12, CARD_INK_TOP + 26 + dy,
                                    cx + 12, CARD_INK_TOP + 26 + dy,
                                    fill=COL_CARD_DIM, width=3, capstyle="round",
                                    tags=tags)
        # A word under the mark, for the child who has never met a hamburger.
        self.canvas.create_text(cx, CARD_INK_TOP + 43, text="Chats",
                                font=self._font(8, True), fill=COL_CARD_DIM,
                                tags=tags)
        self._press_feedback(self.CHATS_TAG, item, face,
                             lambda event=None: self.show_chat_history())

    def show_chat_history(self):
        profile = profiles.active_profile() or {}
        user_id = profile.get("user_id")
        name = profile.get("name") or "Your"
        try:
            chats = chatlog.conversations(user_id) if user_id else []
        except Exception as exc:
            print(f"[CHATS] Could not read the history ({exc}).", flush=True)
            chats = None
        print(f"[CHATS] Opened for {profile.get('name') or 'nobody'}: "
              f"{len(chats) if chats is not None else 'no store'} conversations.",
              flush=True)

        self._overlay_screen("chats", f"{name}'s chats" if user_id else "Chats",
                             "Newest first. Tap a chat to read it.")
        self._chats = chats or []
        self._chat_rows = []
        self._chat_selected = None
        self._overlay_button(676, 22, 780, 62, "Close", self._close_chat_history,
                             fill=COL_INDIGO, size=12)

        for box in (self.CHATS_LIST_BOX, self.CHATS_READ_BOX):
            self._round_rect(*box, 16, fill="#FFFFFF", outline=COL_CARD_EDGE,
                             tags=self.OVERLAY_TAG)

        if not self._chats:
            if not user_id:
                why = "Choose who you are on the card at the top first."
            elif chats is None:
                why = "The chat history is not available right now."
            else:
                why = "No chats yet. Say “Hey Liza” and ask anything!"
            self._overlay_text((self.CHATS_LIST_BOX[0] + self.CHATS_READ_BOX[2]) / 2,
                               277, why, 12, fill=COL_TEXT_DIM, width=440)
            return

        self._chat_list = self._chat_pane(self.CHATS_LIST_BOX, self._chat_list_tap)
        self._chat_read = self._chat_pane(self.CHATS_READ_BOX, None)
        self._draw_chat_list()
        self._open_chat(0)

    def _close_chat_history(self):
        profile = profiles.active_profile()
        if profile and profiles.is_kindergarten(profile.get("class")):
            self.show_kg_home()
        else:
            self._clear_overlay()

    def _chat_pane(self, box, on_tap):
        """A canvas of its own inside the card, so a long list can scroll under
        a finger without spilling over the rest of the screen -- Tk cannot clip
        items on one canvas, but a child canvas clips to its own edges."""
        x0, y0, x1, y1 = box
        inset = 6
        pane = tk.Canvas(self.canvas, width=x1 - x0 - 2 * inset,
                         height=y1 - y0 - 2 * inset, bg="#FFFFFF", bd=0,
                         highlightthickness=0)
        self._overlay_widgets.append(pane)
        self.canvas.create_window(x0 + inset, y0 + inset, window=pane, anchor="nw",
                                  tags=self.OVERLAY_TAG)
        pane.view_h = y1 - y0 - 2 * inset
        pane.view_w = x1 - x0 - 2 * inset
        pane.content_h = pane.view_h
        pane.top = 0
        pane.thumb = None
        drag = {}

        def press(event):
            drag.update(y=event.y, top=pane.top, moved=False)

        def motion(event):
            if not drag:
                return
            dy = event.y - drag["y"]
            if abs(dy) > self.CHATS_DRAG_SLOP:
                drag["moved"] = True
            if drag["moved"]:
                self._chat_scroll(pane, drag["top"] - dy)

        def release(event):
            if drag and not drag["moved"] and on_tap is not None:
                on_tap(pane.top + event.y)
            drag.clear()

        pane.bind("<ButtonPress-1>", press)
        pane.bind("<B1-Motion>", motion)
        pane.bind("<ButtonRelease-1>", release)
        return pane

    def _chat_pane_done(self, pane, content_h, top=0):
        """Set how tall what was drawn is, and put a scroll thumb on it if it
        is taller than the card -- the only sign there is more below."""
        pane.content_h = max(pane.view_h, content_h)
        pane.configure(scrollregion=(0, 0, pane.view_w, pane.content_h))
        pane.thumb = None
        if pane.content_h > pane.view_h:
            pane.thumb = pane.create_line(0, 0, 0, 0, fill="#CBD2E6", width=4,
                                          capstyle="round")
        self._chat_scroll(pane, top)

    def _chat_scroll(self, pane, top):
        top = max(0, min(top, pane.content_h - pane.view_h))
        pane.top = top
        pane.yview_moveto(top / pane.content_h)
        if pane.thumb is not None:
            length = max(30, pane.view_h * pane.view_h / pane.content_h)
            y = top + (pane.view_h - length) * top / (pane.content_h - pane.view_h)
            x = pane.view_w - 3
            pane.coords(pane.thumb, x, y + 3, x, y + length - 3)
            pane.tag_raise(pane.thumb)

    @staticmethod
    def _chat_day(when):
        today = datetime.now(when.tzinfo).date()
        days = (today - when.date()).days
        if days <= 0:
            return "Today"
        if days == 1:
            return "Yesterday"
        if days < 7:
            return when.strftime("%A")
        return when.strftime("%d %b %Y").lstrip("0")

    @staticmethod
    def _chat_time(when):
        return when.strftime("%I:%M %p").lstrip("0").lower()

    CHAT_ROW_H = 54
    CHAT_DAY_H = 30

    def _draw_chat_list(self):
        pane = self._chat_list
        pane.delete("all")
        self._chat_rows = []
        w, y, day = pane.view_w, 0, None
        for index, chat in enumerate(self._chats):
            this_day = self._chat_day(chat["start"])
            if this_day != day:
                day = this_day
                pane.create_text(10, y + self.CHAT_DAY_H / 2 + 3, text=day.upper(),
                                 anchor="w", font=self._font(8, True),
                                 fill=COL_TEXT_DIM)
                y += self.CHAT_DAY_H
            y0, y1 = y + 2, y + self.CHAT_ROW_H - 2
            rect = self._round_rect(2, y0, w - 8, y1, 10, canvas=pane,
                                    fill="#FFFFFF", outline="")
            self._overlay_text(12, y0 + 17, chat["title"], 10, bold=True,
                               fill=COL_TEXT, anchor="w", width=w - 30,
                               max_lines=1, justify="left", canvas=pane,
                               tags="row")
            questions = sum(1 for t in chat["turns"] if t["role"] == "user")
            meta = self._chat_time(chat["start"])
            if questions:
                meta += f"  ·  {questions} question" + ("s" if questions > 1 else "")
            pane.create_text(12, y0 + 36, text=meta, anchor="w",
                             font=self._font(8), fill=COL_TEXT_DIM)
            self._chat_rows.append((y0, y1, index, rect))
            y += self.CHAT_ROW_H
        self._chat_pane_done(pane, y + 6)

    def _chat_list_tap(self, y):
        for y0, y1, index, _rect in self._chat_rows:
            if y0 <= y <= y1:
                self._open_chat(index)
                return

    def _open_chat(self, index):
        self._chat_selected = index
        for _y0, _y1, row, rect in self._chat_rows:
            self._chat_list.itemconfigure(
                rect, fill="#EEF0FF" if row == index else "#FFFFFF")

        chat = self._chats[index]
        pane = self._chat_read
        pane.delete("all")
        w = pane.view_w
        bubble_w = int(w * 0.74)
        pad = 10
        start = chat["start"]
        heading = f"{self._chat_day(start)}, {self._chat_time(start)}"
        if start.date() != datetime.now(start.tzinfo).date():
            heading = f"{start.strftime('%a %d %b %Y').replace(' 0', ' ')}, {self._chat_time(start)}"
        pane.create_text(w / 2, 16, text=heading, font=self._font(8, True),
                         fill=COL_TEXT_DIM)
        y = 36
        for turn in chat["turns"]:
            mine = turn["role"] == "user"
            fill = "#6366F1" if mine else "#F1F3FA"
            ink = "#FFFFFF" if mine else COL_TEXT
            # Written first to be measured, then the bubble drawn to fit and
            # sent behind it: the height of a wrapped reply is not known until
            # Tk (or the Hindi shaper) has laid it out.
            x = w - 14 - pad if mine else 6 + pad
            text = self._overlay_text(x, y + pad, turn["text"], 10, fill=ink,
                                      anchor="ne" if mine else "nw",
                                      width=bubble_w - 2 * pad, justify="left",
                                      canvas=pane, tags="msg")
            bx0, _by0, bx1, by1 = pane.bbox(text)
            bubble = self._round_rect(bx0 - pad, y, bx1 + pad, by1 + pad, 12,
                                      canvas=pane, fill=fill, outline="")
            pane.tag_lower(bubble, text)
            who = "You" if mine else "Liza"
            pane.create_text(bx1 + pad if mine else bx0 - pad, by1 + pad + 9,
                             text=f"{who}  ·  {self._chat_time(turn['at'])}",
                             anchor="e" if mine else "w", font=self._font(7),
                             fill=COL_TEXT_DIM)
            y = by1 + pad + 26
        self._chat_pane_done(pane, y)

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
        # Whatever is on the board belonged to whoever was here before. The
        # history behind it is already swapped per student in ai_loop; this is
        # the one copy of the last answer that was being left on the screen.
        self.clear_transcript()
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
        self._overlay_button(30, row_y, 430, row_y + 50, "Test yourself",
                             self.show_kg_test_picker, fill="#0EA5E9", size=15,
                             sub="See what you have learned",
                             label_frac=0.42, sub_frac=0.74)
        # Between the test and Switch user, because that is the order somebody
        # sitting with the child reaches for them: try something, see how it
        # went, hand the device to the next one.
        self._overlay_button(446, row_y, 606, row_y + 50, "My progress",
                             self.show_progress, fill="#4F46E5", size=11)
        self._overlay_button(622, row_y, 770, row_y + 50, "Switch user",
                             self.show_profile_picker, fill="#E6E9F5",
                             text_colour=COL_TEXT, size=11)
        if greet and name:
            kg.kg_say(f"Hello {name}! What would you like to do today?", "warm")

    # ---------- keeping a record of what they did ----------
    def _kg_note(self, kind, topic, detail=None, score=None, out_of=None,
                 once=True):
        """Write one line into this student's progress log. Never raises.

        `once` keeps a screen from filing the same thing twice while the child
        is still on it -- a child pressing Say it again four times on the letter
        A has practised A once, not four times. The set is cleared by
        _overlay_screen, so coming back to a screen later counts again, which is
        the whole point of coming back to it.

        Advisory, like everything else that writes to the store: a lesson must
        never stop because a progress row could not be saved.
        """
        try:
            profile = profiles.active_profile() or {}
            user_id = profile.get("user_id")
            if not user_id:
                return
            if once:
                seen = getattr(self, "_kg_noted", None)
                if seen is None:
                    seen = self._kg_noted = set()
                if (kind, topic) in seen:
                    return
                seen.add((kind, topic))
            store.record_activity(user_id, kind, topic, detail=detail,
                                  score=score, out_of=out_of,
                                  language=getattr(self, "_kg_story_lang", None)
                                  if kind == "story" else None)
        except Exception as exc:
            print(f"[PROGRESS] Could not record {kind} {topic!r} ({exc}).",
                  flush=True)

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

        title = "हिंदी अक्षर" if language == "hi" else "English Alphabets"
        self._kg_class_screen("kg_alpha", title,
                              f"{self._kg_alpha_index + 1} of {len(bank)}")
        # After the screen is named, so the per-visit guard in _kg_note is
        # against THIS visit. once=False because every letter is its own topic
        # and walking the alphabet should show up as walking the alphabet --
        # the guard only stops the same letter being filed twice.
        self._kg_note("letters", letter,
                      detail=f"{'हिंदी' if language == 'hi' else 'English'}"
                             f" letter {letter}, as in {word}")

        # She stands in the room and the letter is held up beside her, on a
        # frosted card over the right-hand end of the board. The card starts at
        # 405 and she ends at 386, so nothing of her is ever behind it.
        self._kg_keep_mascot()
        self._kg_ask_button()
        card = (405, 140, 780, 336)
        self._kg_glass_card(card)

        # With a picture the letter moves left and they sit side by side, which
        # is how a wall chart does it. Without one the letter takes the middle
        # of the card, so a Hindi letter whose word has no picture still looks
        # deliberate rather than like something failed to load.
        has_picture = picture_image(picture, 116) is not None if picture else False
        card_mid = (card[0] + card[2]) / 2
        letter_x = card[0] + 92 if has_picture else card_mid
        letter_y = 206 if reading else 214
        # Navy on the frosted panel rather than the white of the mockup. The
        # card runs from x=405 to x=780 and the board it sits on ends at 606, so
        # its right-hand third is always over the pale wall -- white lettering
        # is legible on the half over the board and washes out completely on the
        # half that is not, and the word is centred right on that seam. Navy is
        # the one colour that holds up across the whole panel, and it is the
        # same navy as the buttons on the floor.
        self._overlay_text(letter_x, letter_y, letter, 56, bold=True,
                           fill=CLASS_BLUE)
        if reading:
            # The Latin reading is for the adult sitting alongside, not the child.
            self.canvas.create_text(letter_x, 252, text=f"({reading})",
                                    font=self._font(10), fill="#3F5C8C",
                                    tags=self.OVERLAY_TAG)
        if has_picture:
            self._place_emoji(picture, card[0] + 268, 210, 116)
        # A rule under both of them, then the word: the card reads as one thing
        # with a caption rather than as two pictures and some text.
        self.canvas.create_line(card[0] + 30, 282, card[2] - 30, 282,
                                fill="#FFFFFF", width=3, tags=self.OVERLAY_TAG)
        self._overlay_text(card_mid, 308, word.upper(), 19, bold=True,
                           fill=CLASS_BLUE)

        self._kg_class_button(self.KG_ROW_4[0], "Back", self.show_kg_home,
                              arrow="left")
        if self._kg_alpha_index > 0:
            self._kg_class_button(self.KG_ROW_4[1], "Previous",
                                  lambda: self.show_kg_alphabet(
                                      language, self._kg_alpha_index - 1))
        self._kg_class_button(self.KG_ROW_4[2], "Say it again",
                              lambda: self._kg_say_letter(letter, word, language),
                              primary=True)
        if self._kg_alpha_index < len(bank) - 1:
            self._kg_class_button(self.KG_ROW_4[3], "Next",
                                  lambda: self.show_kg_alphabet(
                                      language, self._kg_alpha_index + 1),
                                  arrow="right")
        else:
            self._kg_class_button(self.KG_ROW_4[3], "Start again",
                                  lambda: self.show_kg_alphabet(language, 0),
                                  size=11, arrow="right")
        self._kg_say_letter(letter, word, language)

    def _kg_say_letter(self, letter, word, language):
        """Say the letter, then the letter with its word.

        Both lines go through the SOUND of the letter rather than the letter
        itself -- see LETTER_SOUNDS. A bare "A." handed to a multilingual voice
        came back as आ, which is the one thing this screen must not teach. The
        Hindi line takes a danda for the same reason: a lone Devanagari glyph
        with no punctuation was being read as its Latin transliteration.
        """
        if language == "hi":
            kg.kg_say_many([(f"{letter}।", "curious"),
                            (f"{letter} से {word}।", "warm")])
        else:
            sound = kg_content.letter_sound(letter)
            kg.kg_say_many([(f"{sound}.", "curious"),
                            (f"{sound} for {kg_content.spoken_word(word)}.", "warm")])

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

        self._kg_class_screen("kg_count", "Counting",
                              f"{n} of {kg_content.COUNT_MAX}")
        # Every fifth number, and not every number. How far they got is the
        # thing worth keeping; fifty rows saying "the child saw 7, then 8" is
        # not a record of anything, and the topic being the milestone is what
        # makes the highest one they reached readable off the list.
        if n % 5 == 0:
            self._kg_note("counting", f"Counted to {n}",
                          detail=f"Counted all the way up to {english.lower()}")
        self._kg_keep_mascot()
        self._kg_ask_button()
        # Something to actually count. Apples rather than dots: a child counts
        # things, and "five apples" is a sentence they can check against the
        # numeral. Five or seven to a row: the apples share the card with the
        # numeral above them and the mascot standing to its left, so a row has
        # 344 usable pixels rather than the whole screen.
        #
        # Worked out BEFORE anything is drawn because the card is sized to them.
        # A fixed card stood two thirds empty at one apple and at anything past
        # twenty, where there is nothing left to lay out at all.
        per_row = 5 if n <= 10 else 7
        size = 38 if n <= 10 else 28
        gap = 9 if n <= 10 else 6
        rows = (n + per_row - 1) // per_row if n <= 20 else 0
        # The sum line sits between the name and the apples when there is one.
        apples_top = 274 if grew else 258
        card_low = (apples_top + rows * size + (rows - 1) * 7 + 16) if rows else 276

        # The same frosted card the alphabet is held up on, in the same place,
        # so moving between the two lessons does not move the lesson.
        card = (400, 140, 784, min(388, card_low))
        self._kg_glass_card(card)
        mid = (card[0] + card[2]) / 2
        self.canvas.create_text(mid, 186, text=str(n),
                                font=self._font(50, True), fill=COUNT_GREEN,
                                tags=self.OVERLAY_TAG)
        self.canvas.create_text(mid, 232, text=english,
                                font=self._font(18, True), fill=COUNT_GREEN,
                                tags=self.OVERLAY_TAG)
        # The sum in the same words she speaks, so the child being read to and
        # the child starting to read see one sentence rather than two.
        if grew:
            self.canvas.create_text(
                mid, 260, text=f"{n - 1}   and 1 more   makes   {n}",
                font=self._font(12, True), fill="#92400E", tags=self.OVERLAY_TAG)

        index = 0
        for row_index in range(rows):
            count = min(per_row, n - row_index * per_row)
            total = count * size + (count - 1) * gap
            x = mid - total / 2 + size / 2
            y = apples_top + size / 2 + row_index * (size + 7)
            for _ in range(count):
                index += 1
                if grew and index == n:
                    # The one that was just added, ringed -- "one more" has to
                    # be visible and not only spoken.
                    half = size / 2 + 5
                    self.canvas.create_oval(x - half, y - half, x + half,
                                            y + half, outline="#B45309",
                                            width=3, tags=self.OVERLAY_TAG)
                self._place_emoji(kg_content.COUNT_EMOJI, x, y, size)
                x += size + gap

        self._kg_class_button(self.KG_ROW_4[0], "Back", self.show_kg_home,
                              arrow="left")
        if n > 1:
            self._kg_class_button(self.KG_ROW_4[1], "Previous",
                                  lambda: self.show_kg_counting(n - 1))
        self._kg_class_button(self.KG_ROW_4[2], "Say it again",
                              lambda: self._kg_say_number(n, grew), primary=True)
        if n >= kg_content.COUNT_MAX:
            self._kg_class_button(self.KG_ROW_4[3], "Start again",
                                  lambda: self.show_kg_counting(1),
                                  size=11, arrow="right")
        else:
            self._kg_class_button(self.KG_ROW_4[3], "Next",
                                  lambda: self._kg_count_next(n), arrow="right")
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
        # In the same room as the counting it interrupts. It arrives in the
        # middle of a lesson, and a screen that changed the wallpaper to ask one
        # question read as having left the lesson rather than paused it.
        self._kg_class_screen("kg_count_more", f"You counted to {n}!",
                              "Shall we keep going?")
        self.canvas.create_text(BOARD_MID, 162, text="\u2b50",
                                font=self._font(46, True), fill=CHALK_WARM,
                                tags=self.OVERLAY_TAG)
        self.canvas.create_text(
            BOARD_MID, 220, width=366, justify="center",
            text=f"That is all the way to {kg_content.number_name(n, 'en').lower()}.",
            font=self._font(13), fill=CHALK, tags=self.OVERLAY_TAG)
        self._kg_class_button(self.KG_ROW_4[1], "That's enough",
                              self.show_kg_home, size=11, arrow="left")
        self._kg_class_button(self.KG_ROW_4[2], "Yes, keep going!",
                              lambda: self.show_kg_counting(n + 1, grew=True),
                              primary=True, size=11)
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

    # Both rows of cards, sized to the board rather than to the screen: the
    # puzzle is written on the blackboard now, and the blackboard is 394px wide
    # where the screen is 800. Five answer slots at 66 and five letters at 58
    # both clear a fingertip, and the answer row is the larger of the two on
    # purpose -- it is the one being built.
    KG_ORDER_SLOT_W, KG_ORDER_SLOT_H, KG_ORDER_SLOT_Y = 66, 56, 128
    KG_ORDER_TILE_W, KG_ORDER_TILE_H, KG_ORDER_TILE_Y = 58, 48, 196

    @staticmethod
    def _kg_board_row_x(count, width, gap=12):
        """Left edge of a row of `count` cards, centred on the BOARD, not the
        screen. The board's middle is 409; centring on 400 sits it visibly off
        the chalk. Shared by the A-to-Z slots and the spelling answer boxes,
        which are the same row of cards asking two different questions."""
        return BOARD_MID - (count * width + (count - 1) * gap) / 2

    def _draw_kg_order(self):
        self._kg_class_screen("kg_order", "A to Z",
                              "Tap the letters in the right order")
        # What they have chosen so far, left to right.
        slot, high = self.KG_ORDER_SLOT_W, self.KG_ORDER_SLOT_H
        top = self.KG_ORDER_SLOT_Y
        x = self._kg_board_row_x(len(self._kg_order_target), slot)
        for index in range(len(self._kg_order_target)):
            picked = (self._kg_order_picked[index]
                      if index < len(self._kg_order_picked) else "")
            self._round_rect(x, top, x + slot, top + high, 12, fill="#FFFFFF",
                             outline="#FFFFFF", tags=self.OVERLAY_TAG)
            self.canvas.create_text(x + slot / 2, top + high / 2, text=picked,
                                    font=self._font(26, True), fill=CLASS_BLUE,
                                    tags=self.OVERLAY_TAG)
            x += slot + 12

        if self._kg_order_done:
            self.canvas.create_text(BOARD_MID, 218, text="Perfect!  A to Z!",
                                    font=self._font(17, True), fill=CHALK_WARM,
                                    tags=self.OVERLAY_TAG)
        else:
            # The letters still to place.
            remaining = [c for c in self._kg_order_pool
                         if c not in self._kg_order_picked]
            tile, tall = self.KG_ORDER_TILE_W, self.KG_ORDER_TILE_H
            y = self.KG_ORDER_TILE_Y
            x = self._kg_board_row_x(len(remaining), tile)
            for letter in remaining:
                self._overlay_button(x, y, x + tile, y + tall, letter,
                                     lambda c=letter: self._kg_order_tap(c),
                                     fill="#FFFFFF", text_colour=CLASS_INK,
                                     radius=12, size=22)
                x += tile + 12

        self._kg_class_button(self.KG_ROW_3[0], "Back", self.show_kg_home,
                              arrow="left")
        self._kg_class_button(self.KG_ROW_3[1], "Start over",
                              self._kg_order_reset, primary=True)
        self._kg_class_button(self.KG_ROW_3[2], "New letters",
                              self.show_kg_order, arrow="right")

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
            # Letter NAMES, never the bare letters: "B comes first." handed
            # to the voice is a lone Latin glyph it may read as a Hindi sound.
            sound = kg_content.letter_sound
            kg.kg_say_many([("Not that one.", "gentle"),
                         (f"After {sound(self._kg_order_picked[-1])}, comes "
                          f"{sound(expected)}."
                          if self._kg_order_picked
                          else f"{sound(expected)} comes first.", "curious")])
            return
        self._kg_order_picked.append(letter)
        if len(self._kg_order_picked) < len(self._kg_order_target):
            self._draw_kg_order()
            kg.kg_say(f"{kg_content.letter_sound(letter)}.", "curious")
            return
        self._kg_order_done = True
        self._kg_note("order", "".join(self._kg_order_target),
                      detail="Put the letters in order",
                      score=1, out_of=1, once=False)
        self._draw_kg_order()
        kg.kg_say_many([(random.choice(kg_content.PRAISE), "proud"),
                     (" ".join(kg_content.letter_sound(c)
                               for c in self._kg_order_target) + ".", "excited")])
        # Same reason as the spelling screen: five letters read back one at a
        # time is longer than any fixed guess, and the guess was cutting the
        # praise off on the puzzle they had just solved.
        self._kg_after_speaking(self._kg_order_next_if_still_here, settle_ms=900)

    def _kg_order_next_if_still_here(self):
        if self.overlay == "kg_order":
            self.show_kg_order()

    # ----- which language should the story be in? -----
    def show_kg_story_picker(self):
        # Under the board rather than across the middle of the screen: the
        # question is written on the board, and the two answers stand below it
        # where the class can see them. Both cards keep white lettering, so the
        # fills are the darker end of their colours -- amber-500 under white was
        # 2.2:1, which was already too little on a lit room and is less on this
        # one.
        self._kg_class_screen("kg_story_lang", "Story Time",
                              "Which language would you like?")
        english = self._overlay_button(110, 226, 390, 378, "English",
                                       lambda: self.show_kg_story_list("en"),
                                       fill="#B45309", size=20, radius=18,
                                       sub="A story in English",
                                       label_frac=0.68, sub_frac=0.86)
        self._kg_book_glyph(250, 272, english)
        hindi = self._overlay_button(410, 226, 690, 378, "हिंदी",
                                     lambda: self.show_kg_story_list("hi"),
                                     fill="#BE185D", size=22, radius=18,
                                     sub="हिंदी में कहानी",
                                     label_frac=0.68, sub_frac=0.86)
        self._kg_book_glyph(550, 272, hindi)
        self._kg_class_button(self.KG_ROW_3[1], "Back", self.show_kg_home,
                              arrow="left")
        kg.kg_say_many([("Would you like a story in English,", "curious"),
                     ("या हिंदी में?", "curious")])

    # ----- and WHICH story? -----
    # The language picker used to hand straight to a random story, so the child
    # got whichever one the shuffle produced and the only way to reach a
    # particular one was to keep pressing Next. They have favourites. Being
    # handed a different story than the one you were promised is, at five, a
    # real disappointment.
    #
    # So the titles go on the screen and the child picks. Two columns of four:
    # a title is a phrase, not a word, and a four-across grid cuts every one of
    # them to three cramped lines. These are wide enough for "The Ant and the
    # Grasshopper" on one.
    KG_STORY_COLUMNS = 2
    KG_STORY_PER_PAGE = 8
    # One colour per tile, all of them dark enough to carry white lettering at
    # 4.5:1 -- the same rule the language cards above are written to.
    KG_STORY_COLOURS = ["#B45309", "#1D4ED8", "#047857", "#BE185D",
                        "#6D28D9", "#0F766E", "#B91C1C", "#0E7490"]

    def show_kg_story_list(self, language=None, page=0):
        """The titles she knows, to be chosen from."""
        if language:
            self._kg_story_lang = language
        language = getattr(self, "_kg_story_lang", "en")
        titles = kg_content.story_titles(language)
        pages = max(1, -(-len(titles) // self.KG_STORY_PER_PAGE))
        page = page % pages
        self._kg_story_page = page
        showing = titles[page * self.KG_STORY_PER_PAGE:
                         (page + 1) * self.KG_STORY_PER_PAGE]

        hindi = language == "hi"
        self._kg_class_screen(
            "kg_story_list",
            "कौन सी कहानी?" if hindi else "Which story?",
            "जो सुननी है उस पर उँगली रखो" if hindi else "Tap the one you want")

        for index, title in enumerate(showing):
            column, row = index % self.KG_STORY_COLUMNS, index // self.KG_STORY_COLUMNS
            x0 = 40 + column * 375
            y0 = 150 + row * 58
            colour = self.KG_STORY_COLOURS[(page * self.KG_STORY_PER_PAGE + index)
                                           % len(self.KG_STORY_COLOURS)]
            # label_dx clears the number badge below: the wording is centred
            # in what is left of the pill, not in the whole of it, or a long
            # title runs straight under the number.
            tag = self._overlay_button(
                x0, y0, x0 + 345, y0 + 50, title,
                lambda t=title: self.show_kg_story(language, t),
                fill=colour, size=14, radius=14, wrap=True, label_dx=18)
            # The number of the story, in a circle at the left end. A
            # pre-reader cannot read "The Two Goats", but they CAN count to
            # eight -- so she reads the titles out in order below, and the
            # number is how a child holds on to which one they wanted.
            self.canvas.create_oval(x0 + 8, y0 + 11, x0 + 36, y0 + 39,
                                    fill="#FFFFFF", outline="",
                                    tags=(self.OVERLAY_TAG, tag))
            self._overlay_text(x0 + 22, y0 + 25,
                               str(page * self.KG_STORY_PER_PAGE + index + 1),
                               13, bold=True, fill=colour,
                               tags=(self.OVERLAY_TAG, tag))

        self._kg_class_button(self.KG_ROW_4[0], "Back",
                              self.show_kg_story_picker, arrow="left")
        self._kg_ask_button(self.KG_ASK_BOX_STORY)
        self._kg_class_button(self.KG_ROW_4[2], "Surprise me",
                              lambda: self.show_kg_story(language), size=11,
                              primary=True)
        if pages > 1:
            self._kg_class_button(
                self.KG_ROW_4[3], "More stories",
                lambda: self.show_kg_story_list(language, page + 1), size=11,
                arrow="right")
        else:
            self._kg_class_button(self.KG_ROW_4[3], "Read titles",
                                  lambda: self._kg_read_titles(showing), size=11)
        # Read out in full the FIRST time this page is opened, and after that
        # only asked. A child who has just heard a story and pressed All
        # stories does not need all eight titles read to them again to pick the
        # next one -- they were listening ten seconds ago. The button is there
        # for when they do.
        already = getattr(self, "_kg_titles_read", set())
        if (language, page) in already:
            kg.kg_say("कौन सी कहानी सुनोगे?" if hindi
                      else "Which story would you like?", "curious")
        else:
            already.add((language, page))
            self._kg_titles_read = already
            self._kg_read_titles(showing)

    def _kg_read_titles(self, titles):
        """Read the list out, numbered.

        Not decoration. Every other KG screen can be used by a child who cannot
        read a word on it, because the picture or the letter carries it -- a
        list of titles is the one screen here that is pure text. Reading them
        aloud in the order they are numbered is what puts it back within reach
        of the child it was built for.
        """
        hindi = getattr(self, "_kg_story_lang", "en") == "hi"
        opening = ("मेरे पास ये कहानियाँ हैं।" if hindi
                   else "Here are the stories I know.")
        lines = [(opening, "warm")]
        for index, title in enumerate(titles, start=1):
            lines.append((f"{index}. {title}.", "curious"))
        lines.append(("जो सुननी है उसे दबाओ।" if hindi
                      else "Tap the one you would like.", "gentle"))
        kg.kg_say_many(lines)

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
        # The three choices stand under the board, the way the two story
        # languages do, so the two pickers in this app are one screen with
        # different things on it rather than two designs to keep in step.
        #
        # Each fill is a step darker than the colour the same subject wears on
        # the home tiles. All three carry white lettering, and emerald-600 under
        # white is 3.0:1 -- fine for the 22pt label and not for the 8pt line
        # under it.
        self._kg_class_screen("kg_test_pick", "Test yourself",
                              "What would you like to be tested on?")
        options = [
            ("A B C", "English letters", "#1D4ED8", "en"),
            ("क ख ग", "हिंदी अक्षर", "#BE185D", "hi"),
            ("1 2 3", "Counting", "#047857", "count"),
        ]
        for index, (label, sub, colour, kind) in enumerate(options):
            x0 = 35 + index * 250
            tag = self._overlay_button(x0, 226, x0 + 230, 378, label,
                                       lambda k=kind: self.start_kg_test(k),
                                       fill=colour, size=22, radius=18, sub=sub,
                                       label_frac=0.62, sub_frac=0.84)
            self._kg_tile_glyph(label.split()[0], x0 + 115, 268, tag)
        self._kg_class_button(self.KG_ROW_3[1], "Back", self.show_kg_home,
                              arrow="left")
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
        # No subtitle: what she is doing is written beside the question, on the
        # strip below, and saying "Listening..." in two places at once made the
        # board and the strip look like two different answers to it.
        self._kg_class_screen(
            "kg_test", f"Question {self._kg_test_at + 1} of {len(self._kg_test_qs)}")

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
            # Ten to a row still, but sized to the BOARD rather than to 800px:
            # ten at 44 ran 512 wide, and the board's writing surface is 394.
            # Three sizes rather than two, because a question about three apples
            # and a question about twenty-one are both drawn here and the small
            # one should not be drawn to the big one's scale.
            per_row = 10
            size = 44 if n <= 7 else 33 if n <= 10 else 30
            gap = 10 if n <= 7 else 6
            rows = [min(per_row, n - r * per_row)
                    for r in range((n + per_row - 1) // per_row)]
            # Centred in the 124px the board has under its heading, however
            # many rows that turns out to be.
            pitch = size + 6
            top = 190 - (len(rows) - 1) * pitch / 2
            index = 0
            for row_index, count in enumerate(rows):
                total = count * size + (count - 1) * gap
                x = BOARD_MID - total / 2 + size / 2
                y = top + row_index * pitch
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
            self._place_emoji(question["picture"], BOARD_MID, 188, 112)

        # The question itself, and what she is doing about it, on a frosted
        # strip below the board. Not chalked onto the board with the picture,
        # which has the whole of the board's 124 usable pixels -- and not
        # printed straight onto the wall, which runs from plain paint through a
        # noticeboard to a potted plant depending on how long the wording is.
        prompt = {"name": {"count": "Count them out loud.  How many?",
                           "hi": "यह क्या है?"}.get(
                      question["kind"], "What is this?"),
                  "spell": {"count": self._kg_step_prompt(question.get("step", 1)),
                            "hi": "किस अक्षर से शुरू होता है?"}.get(
                      question["kind"], "Now spell it")}[self._kg_test_stage]
        status = self._kg_test_listening or self._kg_test_note
        self._kg_glass_card((150, 276, 650, 352 if status else 326), radius=18)
        self._overlay_text(UI_W / 2, 300, prompt, 16, bold=True, fill=CLASS_BLUE)
        if self._kg_test_listening:
            self.canvas.create_text(UI_W / 2, 332, text="I'm listening...",
                                    font=self._font(13, True), fill="#92400E",
                                    tags=self.OVERLAY_TAG)
        elif self._kg_test_note:
            self._overlay_text(UI_W / 2, 332, self._kg_test_note, 13, bold=True,
                               fill=CLASS_BLUE)

        # The score takes the left column, in the exact box the Ask pill stands
        # in on the lessons -- there is no Ask button during a test, and one
        # thing in that column on every KG screen is worth more than the score
        # being a pixel higher. Not the top corner, which is where the clock is
        # painted on the wall.
        x0, y0, x1, y1 = self.KG_ASK_BOX
        self._round_rect(x0, y0, x1, y1, (y1 - y0) // 2, fill="#FFFFFF",
                         outline="#FFFFFF", tags=self.OVERLAY_TAG)
        self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                                text=f"Score {self._kg_test_score}",
                                font=self._font(12, True), fill=CLASS_INK,
                                tags=self.OVERLAY_TAG)
        self._kg_class_button(self.KG_ROW_3[0], "Stop", self.show_kg_home,
                              arrow="left")
        self._kg_class_button(self.KG_ROW_3[1], "Say again",
                              self._kg_test_repeat, primary=True)
        self._kg_class_button(self.KG_ROW_3[2], "Skip", self._kg_test_skip,
                              arrow="right")

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
        return {"count": kg_content.COUNT_PRAISE,
                "hi": kg_content.HINDI_PRAISE}.get(kind, kg_content.PRAISE)

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
                              phrase_limit=phrase_limit, end_silence=end_silence,
                              accept=self._kg_test_accept(question, spelling))
        self._kg_test_poll(token, self._kg_test_gen)

    @staticmethod
    def _kg_test_accept(question, spelling):
        """What counts as the right answer to this listen, for kg_request_listen's
        `accept`. None for counting: a number is short enough to score well."""
        if not question or question["kind"] == "count":
            return None
        if question["kind"] == "hi":
            if spelling:
                return lambda t, letter=question["letter"]: letter in (t or "")
            return lambda t, word=question["word"]: kg_content.matches_answer(t, word)
        if spelling:
            target = kg_content.spelling_target(question["word"])
            return lambda t: kg_content.heard_spelling(t, target)[0] == "correct"
        return lambda t, word=question["word"]: kg_content.matches_answer(t, word)

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
            if right and kind == "hi":
                # Hindi framing for a Hindi word: "It is कबूतर." is one line
                # the voice can only read in ONE accent, and got wrong.
                self._kg_test_score += 1
                self._kg_test_note = f"Yes! {answer}"
                lines = [(random.choice(self._kg_praise_for(kind)), "proud"),
                         (f"यह {answer} है।", "warm")]
            elif right:
                self._kg_test_score += 1
                self._kg_test_note = f"Yes! {answer}"
                lines = [(random.choice(self._kg_praise_for(kind)), "proud"),
                         (f"It is {answer}.", "warm")]
            elif kind == "hi":
                self._kg_test_note = f"It is {answer}"
                said = f"तुमने कहा, {heard}। " if heard.strip() else ""
                lines = [("कोई बात नहीं।", "gentle"),
                         (f"{said}यह {answer} है।", "curious")]
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
            said = (kg_content.letter_sound(answer) if kind == "en" else answer)
        else:
            verdict, _letters = kg_content.heard_spelling(
                heard, kg_content.spelling_target(question["word"]))
            right = verdict == "correct"
            # Two forms of the same answer: C. A. T. goes on the board, "see.
            # ay. tee." goes to the voice. Reading the written one aloud is how
            # the alphabet screen came to teach आ for A.
            answer = kg_content.spell_out(question["word"])
            said = kg_content.spell_out_spoken(question["word"])

        if right:
            self._kg_test_score += 1
            self._kg_test_note = "Correct!"
            lines = [(random.choice(self._kg_praise_for(kind)), "proud")]
        elif kind == "hi":
            self._kg_test_note = f"It is {answer}"
            lines = [("कोई बात नहीं।", "gentle"),
                     (f"यह {answer} से शुरू होता है।", "curious")]
        else:
            self._kg_test_note = f"It is {answer}"
            lines = [("Not quite.", "gentle"), (f"It is {said}", "curious")]
        self._draw_kg_test()
        kg.kg_say_many(lines)
        # The index moves when the NEXT question actually starts, not here.
        # Incrementing now left a gap in which the screen still on display
        # belonged to a question the index had already passed -- and on the last
        # one, any redraw in that gap (tapping "Say again") indexed off the end.
        #
        # And it waits for her to STOP, rather than for 3.2 seconds. "Not quite.
        # It is see. ay. tee." is longer than that, so the correction was being
        # cut off by the next question on exactly the answers that needed it.
        self._kg_after_speaking(
            lambda r=self._kg_test_round: self._kg_test_advance(r),
            settle_ms=700)

    def _kg_test_advance(self, token):
        if self._kg_test_stale(token):
            return
        self._kg_test_at += 1
        self._kg_ask_test_question()

    def _kg_test_finish(self):
        total = len(self._kg_test_qs) * 2          # two marks per question
        score = self._kg_test_score
        share = score / total if total else 0
        self._kg_class_screen("kg_test_done", "All done!",
                              f"You scored {score} out of {total}")
        self._place_emoji("\U0001F31F" if share >= 0.8 else "\U0001F44F",
                          BOARD_MID, 166, 84)
        if share >= 0.8:
            message, tone = "Brilliant! You really know these.", "excited"
        elif share >= 0.5:
            message, tone = "Well done! Keep practising.", "encouraging"
        else:
            message, tone = "Good try! Let's learn some more together.", "warm"
        self.canvas.create_text(BOARD_MID, 230, text=message, width=366,
                                justify="center", font=self._font(15, True),
                                fill=CHALK, tags=self.OVERLAY_TAG)
        self._kg_class_button(self.KG_ROW_4[1], "Back", self.show_kg_home,
                              arrow="left")
        self._kg_class_button(self.KG_ROW_4[2], "Try again",
                              lambda: self.start_kg_test(self._kg_test_kind),
                              primary=True)
        kg.kg_say_many([(f"You scored {score} out of {total}!", "excited"),
                     (message, tone)])
        self._kg_note("test", {"en": "English letters", "hi": "हिंदी अक्षर",
                               "count": "Counting"}.get(self._kg_test_kind,
                                                        self._kg_test_kind),
                      detail=f"Scored {score} out of {total}",
                      score=score, out_of=total, once=False)
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
                         phrase_limit=None, end_silence=None, accept=None):
        """Ask for one listen and remember which answer belongs to us."""
        self._kg_listen_id = kg.kg_request_listen(
            seconds, seed=seed, language=language,
            phrase_limit=phrase_limit, end_silence=end_silence, accept=accept)
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
        word = self._kg_word["word"]
        self._kg_begin_listen(self.KG_SAY_SECONDS, seed=kg.KG_SEED_LETTERS,
                              language="en",
                              phrase_limit=KG_SPELL_PHRASE_LIMIT_S,
                              end_silence=KG_SPELL_END_SILENCE_S,
                              accept=lambda t: kg_content.heard_spelling(t, word)[0]
                              in ("correct", "said_the_word"))
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
                             (f"{word} is {kg_content.spell_out_spoken(word)}", "curious"),
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
                (f"You said {kg_content.spell_out_spoken(letters)}", "gentle"),
                ("You have all the right letters, but they are in a different order.",
                 "gentle"),
                (f"Listen. {word} is {kg_content.spell_out_spoken(word)}", "curious"),
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
                (f"You said {kg_content.spell_out_spoken(letters)}", "gentle"),
                (f"But {word} is {kg_content.spell_out_spoken(word)}", "curious"),
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
                (kg_content.spell_out_spoken(word), "storyteller"),
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
                (kg_content.spell_out_spoken(word), "storyteller"),
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
        self._kg_class_screen("kg_spell", "Spell a Word", subtitle)

        if saying:
            self._draw_kg_saying()
            return

        # What they have tapped so far, as one box per letter of the answer, so
        # the child can see how many letters are still to come. On the board,
        # where the word they are answering was written a moment ago.
        target = self._kg_word["word"]
        slot, high, top = 62, 58, 140
        x = self._kg_board_row_x(len(target), slot)
        for index in range(len(target)):
            typed = self._kg_typed[index] if index < len(self._kg_typed) else ""
            self._round_rect(x, top, x + slot, top + high, 12, fill="#FFFFFF",
                             outline="#FFFFFF", tags=self.OVERLAY_TAG)
            self.canvas.create_text(x + slot / 2, top + high / 2,
                                    text=typed.upper(),
                                    font=self._font(24, True), fill=CLASS_BLUE,
                                    tags=self.OVERLAY_TAG)
            x += slot + 12

        if self._kg_feedback:
            self.canvas.create_text(BOARD_MID, 226, text=self._kg_feedback,
                                    font=self._font(14, True), fill=CHALK_WARM,
                                    tags=self.OVERLAY_TAG)

        # TWO rows of thirteen, below the board, rather than three rows of nine
        # across the middle of it. The board is 394px wide where the screen is
        # 800, so a keyboard that fits ON it would have 40px keys -- under 11mm
        # on this panel, which is below what a four-year-old's finger reliably
        # hits. Under the board there is the whole width to spend, and the keys
        # come out 54x52. The room shows around them; they are opaque, and the
        # desk they stand over is not something a child needs to see.
        for r, row in enumerate(["ABCDEFGHIJKLM", "NOPQRSTUVWXYZ"]):
            width, gap = 54, 4
            total = len(row) * width + (len(row) - 1) * gap
            x = (UI_W - total) / 2
            # 276, not 268: the board's wooden frame runs to y=274, and a
            # keyboard that starts above it shows the frame's bottom lip in
            # every gap between the keys.
            y = 276 + r * 56
            for letter in row:
                self._overlay_button(x, y, x + width, y + 50, letter,
                                     lambda c=letter: self._kg_letter(c),
                                     fill="#FFFFFF", text_colour=CLASS_INK,
                                     radius=10, size=16)
                x += width + gap

        self._kg_class_button(self.KG_ROW_4[0], "Back", self.show_kg_home,
                              arrow="left")
        self._kg_class_button(self.KG_ROW_4[1], "Undo", self._kg_undo)
        self._kg_class_button(self.KG_ROW_4[2], "Say it again",
                              lambda: kg.kg_say(f"{self._kg_word['word']}. "
                                             f"{self._kg_word['hint']}."),
                              primary=True)
        self._kg_class_button(self.KG_ROW_4[3], "Next word",
                              self.show_kg_spelling, size=11, arrow="right")

    def _draw_kg_saying(self):
        """The SAY stage. No keyboard at all -- it is not their turn to write.

        Showing the word in letters they can see while they say it is deliberate:
        this stage is about hearing the letters, not recalling them, and the
        recall test is the written stage immediately after.
        """
        word = self._kg_word["word"].upper()
        # Written on the board, in chalk, at the size a teacher writes it. The
        # spaced-out copy underneath is the same word as SEPARATE letters, which
        # is the thing being asked for.
        self.canvas.create_text(BOARD_MID, 152, text=word,
                                font=self._font(36, True), fill=CHALK,
                                tags=self.OVERLAY_TAG)
        self.canvas.create_text(BOARD_MID, 194, text="  ".join(word),
                                font=self._font(14, True), fill=CHALK_DIM,
                                tags=self.OVERLAY_TAG)

        # No pill around the listening line any more. A rounded box in a flat
        # colour reads as a button on a screen whose every other rounded box IS
        # one, and this is the one thing here that must not be pressed.
        if self._kg_listening:
            self.canvas.create_text(BOARD_MID, 226, text="I'm listening...",
                                    font=self._font(14, True), fill=CHALK_WARM,
                                    tags=self.OVERLAY_TAG)
        elif self._kg_feedback:
            self.canvas.create_text(BOARD_MID, 226, text=self._kg_feedback,
                                    font=self._font(14, True), fill=CHALK_WARM,
                                    tags=self.OVERLAY_TAG)

        if self._kg_heard:
            self.canvas.create_text(BOARD_MID, 250,
                                    text=f"I heard:  {'  '.join(self._kg_heard)}",
                                    font=self._font(10), fill=CHALK_DIM,
                                    tags=self.OVERLAY_TAG)

        # A way past the microphone, always. If the room is loud, or the child
        # will not speak, or Whisper simply never returns, this stage must not be
        # a dead end -- so writing is one tap away at every moment.
        # Repeating the word and then NOT listening left the child talking to a
        # closed microphone -- they hear the word, spell it, and nothing happens.
        self._kg_class_button(self.KG_ROW_3[0], "Back", self.show_kg_home,
                              arrow="left")
        self._kg_class_button(self.KG_ROW_3[1], "Write it",
                              self._kg_to_writing, primary=True)
        self._kg_class_button(self.KG_ROW_3[2], "Say it again",
                              self._kg_repeat_word, arrow="right")

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
            # The WRITTEN stage is the real assessment -- see the note above
            # about Whisper hearing B for D -- so this is the one that is
            # marked, and the spoken attempt above never is.
            self._kg_note("spelling", target, detail=f"Spelled {target} correctly",
                          score=1, out_of=1)
            self._kg_feedback = praise
            self._draw_kg_spelling()
            kg.kg_say(f"{praise} {target}. {kg_content.spell_out_spoken(target)}")
            # WAIT for her to finish, rather than betting 4.2 seconds on it.
            # "Wonderful! Shoe. ess. aitch. oh. ee." runs past four seconds, and
            # the next word interrupted the praise for it every time -- which is
            # the child being cut off at the exact moment they got it right.
            self._kg_after_speaking(self._kg_next_if_still_spelling,
                                    settle_ms=900)
        else:
            nudge = random.choice(kg_content.ENCOURAGEMENT)
            self._kg_note("spelling", target,
                          detail=f"Still learning to spell {target}",
                          score=0, out_of=1)
            self._kg_feedback = nudge
            self._kg_typed = ""
            self._draw_kg_spelling()
            kg.kg_say(f"{nudge} {target} is spelled {kg_content.spell_out_spoken(target)}. "
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

    # How a beat is written, by the tone it is spoken in. Coloured chalk rather
    # than the ten inks this was on the white card: three chalks is what a
    # classroom has, and every one of them clears 4.5:1 against the lightest
    # pixel of the board where the ten inks were chosen against white paper.
    # Sizes are 4pt down from that card, because the board is 394px wide and
    # not 720 -- and none goes below 14pt bold, which is where AA stops asking
    # 4.5:1 of them and starts asking 3:1.
    KG_CHALK_TONE = {
        "excited":     (CHALK_YELLOW, 17),
        "amazed":      (CHALK_YELLOW, 16),
        "proud":       (CHALK_YELLOW, 16),
        "encouraging": (CHALK_YELLOW, 15),
        "curious":     (CHALK_BLUE, 15),
        "storyteller": (CHALK, 15),
        "warm":        (CHALK_YELLOW, 15),
        "gentle":      (CHALK, 14),
        "sad":         (CHALK_BLUE, 14),
        "mysterious":  (CHALK_BLUE, 14),
    }

    def show_kg_story(self, language=None, title=None):
        """Tell a story. `title` is the one the child chose off the list; with
        no title it is whichever one they have not just had."""
        # Remembered so Next story stays in the language they chose rather than
        # dropping back to English on the second story.
        if language:
            self._kg_story_lang = language
        language = getattr(self, "_kg_story_lang", "en")
        story = kg_content.story_by_title(title, language) if title else None
        # A title that is no longer in the bank falls back to a random one
        # rather than to an empty screen: the child pressed something and a
        # story has to come out of it.
        self._kg_story = story or kg_content.random_story_in(
            language, self._kg_seen_stories)
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

        # The story is written on the blackboard, one line at a time, which is
        # where a story being read to a class is written. It used to be a white
        # card filling the screen, and that card was the right answer while the
        # screen behind it was a blank tint -- here it would have hidden the
        # room the lesson is happening in.
        #
        # The board is 394px wide against the card's 720, so the caption is
        # 15pt rather than 19pt and the longest beat in the bank wraps to three
        # lines instead of two. Measured, not guessed: 84px of text in the 124px
        # the board has under its heading.
        subtitle = "What do you think?" if asked else "Listen to the story"
        self._kg_class_screen("kg_story", self._kg_story["title"], subtitle)

        if asked:
            self._overlay_text(BOARD_MID, 186, self._kg_story["question"], 16,
                               bold=True, fill=CHALK, width=366)
            # Below the board, on the wall, and big: this is the only thing on
            # the screen a child is being asked to press.
            self._overlay_button(209, 278, 397, 362, "YES",
                                 lambda: self._kg_answer(True),
                                 fill="#0F766E", size=20, radius=20)
            self._overlay_button(421, 278, 609, 362, "NO",
                                 lambda: self._kg_answer(False),
                                 fill="#BE123C", size=20, radius=20)
        else:
            # The line just gone, small and faded, so the story has somewhere to
            # have come from and the current line is unmistakably the current
            # one. ONE line of it, cut short if it will not fit: the board has
            # room for the beat being read or for the whole of the beat before
            # it, and it is not a difficult choice between those two.
            previous, _tone = self._kg_caption_at(beat - 1)
            if previous:
                self._overlay_text(BOARD_MID, 138, previous, 9, fill=CHALK_DIM,
                                   width=356, max_lines=1)

            text, tone = self._kg_caption_at(beat)
            if text is None:
                text, tone = self._kg_story["title"], "storyteller"
            colour, size = self.KG_CHALK_TONE.get(tone, (CHALK, 15))
            self._overlay_text(BOARD_MID, 196, text, size, bold=True, fill=colour,
                               width=366)

            # One dot per beat, filled as far as she has read. A pre-reader
            # cannot read "beat 4 of 10", but they can see four lit dots.
            if segments:
                gap = min(20, 360 / max(len(segments), 1))
                x = BOARD_MID - gap * (len(segments) - 1) / 2
                for index in range(len(segments)):
                    done = index <= beat
                    r = 5 if done else 3
                    self.canvas.create_oval(x - r, 246 - r, x + r, 246 + r,
                                            fill=CHALK_WARM if done else "#A8BFA4",
                                            outline="", tags=self.OVERLAY_TAG)
                    x += gap

        # Back goes to the LIST, not to the language question: the child came
        # from the list and that is where "another one" lives now.
        self._kg_class_button(self.KG_ROW_4[0], "All stories",
                              self.show_kg_story_list, arrow="left", size=11)
        # Slot 1 of the same row; see KG_ASK_BOX_STORY.
        self._kg_ask_button(self.KG_ASK_BOX_STORY)
        self._kg_class_button(self.KG_ROW_4[2], "Read again", self._kg_narrate,
                              primary=True)
        self._kg_class_button(self.KG_ROW_4[3], "Next story",
                              self.show_kg_story, size=11, arrow="right")

    def _kg_answer(self, said_yes):
        # Answered in the story's own language: praise in English after a Hindi
        # story breaks the spell for the child who was just listening to it.
        language = getattr(self, "_kg_story_lang", "en")
        correct = said_yes == self._kg_story["answer"]
        # NOT SCORED. A story is something they listened to, and a progress
        # screen that puts a mark against listening teaches a five-year-old
        # that listening was a thing they could have failed. Whether they
        # followed it goes in the wording instead, where a parent can read it
        # and nothing adds it up.
        self._kg_note("story", self._kg_story["title"],
                      detail=("Listened, and answered the question correctly"
                              if correct else "Listened to the whole story"))
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
    def show_visual(self, path, steps=None):
        print(f"[UI] (headless) visual: {path} steps={steps or []}", flush=True)
    def show_graph(self, spec): print(f"[UI] (headless) graph: {spec['source']}", flush=True)
    def show_model3d(self, scene): print(f"[UI] (headless) 3D model: {scene.get('title')}", flush=True)
    def enlarge_current(self): pass
    def shrink_current(self): pass
    def clear_visual(self, keep_renderer=False): pass
    # No screen: the camera still sees, there is just no picture of it.
    def open_viewfinder(self): pass
    def viewfinder_flash(self): pass
    def close_viewfinder(self): pass
    def set_camera_button(self, on): pass
    # No screen, so no page to write on: whiteboard.available() says so.
    def show_solution(self, panel): pass
    # No lab to turn, but she is still told what was asked for.
    def show_geometry(self, figure, picks=None):
        import geometry
        app_state.current_geometry = figure.describe() + (
            "; picked " + ", ".join(figure.element_name(p) for p in picks) + ": "
            + geometry.measure(figure, picks).sentence() if picks else "")
    def geometry_pick(self, picks): pass
    def geometry_figure(self): return None
    def geometry_open(self): return False
    def board_command(self, command): pass
    def show_offers(self, offers): pass
    def take_offer(self, index=0):
        # No button to tap, but "yes, show me" still shows it.
        offers = getattr(app_state, "current_offers", None) or []
        if 0 <= index < len(offers):
            import actions
            app_state.current_offers = offers[:index] + offers[index + 1:]
            threading.Thread(target=actions.run_offer, args=(offers[index],),
                             daemon=True).start()
    def clear_transcript(self): pass
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
