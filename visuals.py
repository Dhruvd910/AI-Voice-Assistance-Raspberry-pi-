"""Something to LOOK at, for the questions words are the wrong answer to.

"Show me the diagram", "what is the equation", "draw the graph" -- asked of a
device whose only output was a voice, and answered every time with "I can't
actually show you a picture". This is the half that was missing.

WHY MOST OF THIS IS DRAWN HERE RATHER THAN FETCHED OR GENERATED
    The panel it lands on is 301x186. A photograph of a labelled butterfly
    life-cycle diagram, scaled to fit that, is a grey smudge with unreadable
    text -- the picture is there and the lesson is not. So the model sends the
    STRUCTURE ("Egg;Caterpillar;Chrysalis;Butterfly") and the drawing happens
    here, at a size that is legible on this screen, in this palette.

    It is also instant, free, and works with the wifi down, which for three of
    the five kinds is worth more than photorealism.

    The one kind that genuinely needs the world is a PICTURE of a real thing --
    a toucan, the Taj Mahal -- and that is the one kind that goes to the network.

NOTHING THE MODEL SENDS IS EVALUATED. An earlier shape of this took an
expression in x and plotted it, which is `eval` on the output of a language
model, on a device in a child's bedroom. The model emits POINTS instead. It is
perfectly able to work them out, and there is now no path from a reply to the
interpreter.
"""

import io
import math
import os
import re
import time

from PIL import Image, ImageDraw, ImageFilter

# The size everything is drawn at. The board shows it scaled down and the
# full-screen view shows it nearly one-to-one, so one render serves both and the
# big one is not a blow-up of a thumbnail.
RENDER_W, RENDER_H = 760, 420

# Same family as the rest of the screens, so a diagram looks like part of the
# device rather than like something pasted onto it.
INK        = "#1E2233"
INK_DIM    = "#5A6478"
ACCENT     = "#17408B"
ACCENT_SOFT= "#DCE6F7"
PAPER      = "#FFFFFF"
RULE       = "#C8D3E8"
# One colour per stage, so a cycle reads as four things and not as one shape.
STAGE_COLOURS = ["#1D4ED8", "#0F766E", "#B45309", "#BE185D", "#6D28D9", "#047857"]

# How long a picture may take to arrive before she gives up on it. The student
# is looking at the board waiting; past this it is kinder to say it did not work.
FETCH_TIMEOUT_S = float(os.getenv("VISUAL_FETCH_TIMEOUT_S", "8"))
# A cap on what will be pulled down and decoded. A 40MB panorama is not a
# teaching picture and decoding one on a Pi is a stall.
FETCH_MAX_BYTES = int(os.getenv("VISUAL_FETCH_MAX_BYTES", str(6 * 1024 * 1024)))

# Where a rendered visual is kept so the screen can pick it up. One file, the
# same one every time: only the newest is ever shown, and a folder that grows
# one PNG per question would fill a Pi.
VISUAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pictures")
VISUAL_PATH = os.path.join(VISUAL_DIR, ".visual.png")


# ---------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------
_font_cache = {}
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_FONT_CANDIDATES_REGULAR = [p.replace("-Bold", "").replace("Bold", "Regular")
                            for p in _FONT_CANDIDATES]


def _font(size, bold=True):
    key = (size, bold)
    if key not in _font_cache:
        from PIL import ImageFont
        for path in (_FONT_CANDIDATES if bold else _FONT_CANDIDATES_REGULAR):
            try:
                _font_cache[key] = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def _text_size(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _wrap(draw, text, font, width):
    """Greedy wrap. Returns the lines, never fewer than one."""
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if _text_size(draw, trial, font)[0] <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [text]


def _fitted_lines(draw, text, width, height, sizes):
    """The largest font from `sizes` whose wrapped text fits the box."""
    for size in sizes:
        font = _font(size)
        lines = _wrap(draw, text, font, width)
        line_h = _text_size(draw, "Ag", font)[1] + 4
        # BOTH dimensions. Checking only the height let a single word longer
        # than the box through at full size -- _wrap keeps one word per line
        # however wide it is -- so "Evaporation" ran out through both ends of
        # its own box while the wrapper reported it as fitting.
        if (len(lines) * line_h <= height
                and all(_text_size(draw, line, font)[0] <= width for line in lines)):
            return font, lines, line_h
    font = _font(sizes[-1])
    return font, _wrap(draw, text, font, width), _text_size(draw, "Ag", font)[1] + 4


def _canvas(title=None):
    """A blank sheet with the title along the top, and the y to draw under."""
    image = Image.new("RGB", (RENDER_W, RENDER_H), PAPER)
    draw = ImageDraw.Draw(image)
    top = 18
    if title:
        font, lines, line_h = _fitted_lines(draw, title, RENDER_W - 60, 80,
                                            (34, 30, 26, 22))
        for line in lines:
            width = _text_size(draw, line, font)[0]
            draw.text(((RENDER_W - width) / 2, top), line, font=font, fill=ACCENT)
            top += line_h
        top += 6
        draw.line([(60, top), (RENDER_W - 60, top)], fill=RULE, width=2)
        top += 18
    return image, draw, top


def _rounded(draw, box, radius, fill, outline=None, width=3):
    draw.rounded_rectangle(box, radius, fill=fill, outline=outline, width=width)


def _label_in_box(draw, box, text, colour, sizes=(24, 21, 18, 16, 14)):
    x0, y0, x1, y1 = box
    font, lines, line_h = _fitted_lines(draw, text, (x1 - x0) - 18, (y1 - y0) - 12,
                                        sizes)
    y = (y0 + y1) / 2 - len(lines) * line_h / 2
    for line in lines:
        width = _text_size(draw, line, font)[0]
        draw.text(((x0 + x1 - width) / 2, y), line, font=font, fill=colour)
        y += line_h


def _arrow(draw, start, end, colour, width=5, head=15):
    draw.line([start, end], fill=colour, width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    for side in (2.6, -2.6):
        draw.line([end, (end[0] + head * math.cos(angle + side),
                         end[1] + head * math.sin(angle + side))],
                  fill=colour, width=width)


# ---------------------------------------------------------------------------
# the five kinds
# ---------------------------------------------------------------------------
def _split_labels(payload, limit=7):
    parts = [p.strip() for p in re.split(r'[;|\n]', payload) if p.strip()]
    return parts[:limit]


def _cycle(payload):
    """Stages round a ring, on a circle that runs behind them.

    THE CIRCLE IS DRAWN WHOLE, then the boxes are laid on top with an opaque
    fill, so what shows is exactly the arc in each gap. Two earlier attempts
    tried to compute those gaps -- straight chords, then trimmed arcs -- and
    both spent their length underneath the boxes, leaving arrowheads floating
    with nothing joining them. Letting the boxes do the masking cannot get it
    wrong, whatever the number of stages.

    Four is the most that fits round a ring on a 760x420 sheet with labels
    anybody can read. Five or more goes to _steps with a loop arrow, which is
    still a cycle and is still legible; a ring of six is neither.
    """
    parts = _split_labels(payload)
    title, labels = (parts[0], parts[1:]) if len(parts) > 3 else (None, parts)
    if len(labels) < 2:
        return None, "a cycle needs at least two stages"
    if len(labels) > 4:
        return _steps(payload, loop=True)
    image, draw, top = _canvas(title)

    cx, cy = RENDER_W / 2, top + (RENDER_H - top) / 2
    box_h = 64
    radius = min((RENDER_H - top) / 2 - box_h / 2 - 20, 132)
    # Two neighbours on a ring sit 2R.sin(pi/N) apart; the box has to be
    # narrower than that with room to spare, or they touch.
    box_w = int(min(190, 2 * radius * math.sin(math.pi / len(labels)) - 30))
    step = 2 * math.pi / len(labels)
    # Starting at the top and going clockwise, which is how a cycle is read.
    angles = [-math.pi / 2 + index * step for index in range(len(labels))]

    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                 outline=ACCENT, width=5)
    # One arrowhead per gap, pointing the way round. Placed at the middle of the
    # gap, where nothing is ever going to cover it.
    for angle in angles:
        mid = angle + step / 2
        tip = (cx + radius * math.cos(mid), cy + radius * math.sin(mid))
        heading = mid + math.pi / 2          # tangent, clockwise
        for side in (2.5, -2.5):
            draw.line([tip, (tip[0] + 18 * math.cos(heading + side),
                             tip[1] + 18 * math.sin(heading + side))],
                      fill=ACCENT, width=5)

    for index, angle in enumerate(angles):
        x, y = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        colour = STAGE_COLOURS[index % len(STAGE_COLOURS)]
        box = (x - box_w / 2, y - box_h / 2, x + box_w / 2, y + box_h / 2)
        _rounded(draw, box, 16, PAPER, colour, 4)
        _label_in_box(draw, box, labels[index], colour, (22, 19, 17, 15, 13, 11))
    return image, ""


def _steps(payload, loop=False):
    """One step after another, left to right, wrapping only when it must.

    `loop` draws a return arrow from the last box back to the first, which is
    how a cycle with more stages than fit round a ring is shown.

    Four across rather than two-by-two. A grid loses the ORDER -- the reader has
    to guess whether it goes across or down, and the arrow that would have told
    them falls in the gap between the rows. At 760 wide, four boxes of 165 and
    three gaps of 30 come to 750, so four fit and only five or six ever wrap.
    """
    parts = _split_labels(payload)
    title, labels = (parts[0], parts[1:]) if len(parts) > 2 else (None, parts)
    if not labels:
        return None, "there were no steps to draw"
    image, draw, top = _canvas(title)

    per_row = len(labels) if len(labels) <= 4 else (len(labels) + 1) // 2
    rows = [labels[i:i + per_row] for i in range(0, len(labels), per_row)]
    gap = 34 if per_row >= 4 else 46
    # A loop needs a lane down the left-hand side to come back along, so the
    # boxes give up 60px of width for it.
    margin = 108 if loop else 48
    box_w = min(214, int((RENDER_W - margin - gap * (per_row - 1)) / per_row))
    box_h = min(108, int((RENDER_H - top - 34) / len(rows)) - 22)
    y = top + ((RENDER_H - top) - len(rows) * (box_h + 22)) / 2
    first_y = y
    # Every row starts at the same x, including a short last one. Centring each
    # row on its own width puts the second row's first box somewhere the wrap
    # arrow does not point at, which is worse than a ragged right edge.
    widest = per_row * box_w + (per_row - 1) * gap
    left = (RENDER_W - widest) / 2
    index = 0
    for row_index, row in enumerate(rows):
        total = len(row) * box_w + (len(row) - 1) * gap
        x = left
        for position, label in enumerate(row):
            colour = STAGE_COLOURS[index % len(STAGE_COLOURS)]
            box = (x, y, x + box_w, y + box_h)
            _rounded(draw, box, 14, PAPER, colour, 4)
            _label_in_box(draw, box, label, colour, (20, 18, 16, 14, 12))
            if position < len(row) - 1:
                _arrow(draw, (x + box_w + 6, y + box_h / 2),
                       (x + box_w + gap - 6, y + box_h / 2), ACCENT, width=5)
            x += box_w + gap
            index += 1
        # The wrap arrow: down the right, back along the gap, into the start of
        # the next row. Without it a second row is a second diagram.
        if row_index < len(rows) - 1:
            right = left + total
            mid = y + box_h + 11
            draw.line([(right, y + box_h), (right, mid)], fill=ACCENT, width=5)
            draw.line([(right, mid), (left + box_w / 2, mid)], fill=ACCENT, width=5)
            _arrow(draw, (left + box_w / 2, mid),
                   (left + box_w / 2, y + box_h + 22), ACCENT, width=5)
        y += box_h + 22
    if loop:
        # From under the LAST box, down, out to a lane on the left, up the side
        # and back into the FIRST -- which is in the top row, not the bottom one.
        # Routed outside the boxes rather than through them: a return line that
        # cuts across the second row reads as another step.
        last_row = rows[-1]
        last_x = left + (len(last_row) - 1) * (box_w + gap) + box_w / 2
        last_bottom = y - 22
        lane_x = max(14, left - 30)
        lane_y = last_bottom + 14
        first_mid = first_y + box_h / 2
        draw.line([(last_x, last_bottom), (last_x, lane_y)], fill=ACCENT, width=4)
        draw.line([(last_x, lane_y), (lane_x, lane_y)], fill=ACCENT, width=4)
        draw.line([(lane_x, lane_y), (lane_x, first_mid)], fill=ACCENT, width=4)
        _arrow(draw, (lane_x, first_mid), (left - 6, first_mid), ACCENT, width=4)
        font = _font(15)
        note = "and round again"
        draw.text((last_x - _text_size(draw, note, font)[0] - 12, lane_y + 6),
                  note, font=font, fill=INK_DIM)
    return image, ""


def _points(payload):
    """(title, [(x, y)...], is_bar, [labels]) from the model's payload.

    Two shapes only, and neither is an expression -- see the module docstring.
        Distance; 0,0; 1,5; 2,20
        Rainfall; Mon=3; Tue=5; Wed=2
    """
    parts = _split_labels(payload, limit=40)
    if not parts:
        return None, [], False, []
    title, rest = parts[0], parts[1:]
    if not rest:                      # no title given, it was all data
        title, rest = None, parts

    bars, xy, names = [], [], []
    for part in rest:
        named = re.match(r'^\s*([^=]+?)\s*=\s*(-?\d+(?:\.\d+)?)\s*$', part)
        if named:
            names.append(named.group(1))
            bars.append(float(named.group(2)))
            continue
        pair = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$', part)
        if pair:
            xy.append((float(pair.group(1)), float(pair.group(2))))
    if bars:
        return title, list(enumerate(bars)), True, names
    return title, xy, False, []


def _graph(payload):
    parts = _split_labels(payload, limit=40)
    if parts and re.match(r'^\s*(?:bar|line|plot)\s*$', parts[0], re.IGNORECASE):
        payload = "; ".join(parts[1:])
    title, data, is_bar, names = _points(payload)
    if len(data) < 2:
        return None, "there were not enough points to plot"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return None, f"the plotting library is not available ({exc})"

    figure, axes = plt.subplots(figsize=(RENDER_W / 100, RENDER_H / 100), dpi=100)
    xs = [p[0] for p in data]
    ys = [p[1] for p in data]
    if is_bar:
        axes.bar(xs, ys, color=STAGE_COLOURS[0], width=0.62)
        axes.set_xticks(xs)
        axes.set_xticklabels(names, fontsize=15)
    else:
        axes.plot(xs, ys, color=STAGE_COLOURS[0], linewidth=4,
                  marker="o", markersize=9)
    if title:
        axes.set_title(title, fontsize=22, color=ACCENT, pad=14)
    axes.tick_params(labelsize=15, colors=INK_DIM)
    axes.grid(True, color=RULE, linewidth=1)
    for edge in ("top", "right"):
        axes.spines[edge].set_visible(False)
    for edge in ("left", "bottom"):
        axes.spines[edge].set_color(RULE)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=PAPER)
    plt.close(figure)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB"), ""


# A backslash that arrived as the character it escapes.
#
# The model writes \frac and it reaches here as a FORM FEED followed by "rac" --
# seen once in four asks, giving "F = G rac{m_1 m_2}{r^2}", which is not an
# equation and fails to set. The seven control characters below are exactly the
# ones a backslash-plus-letter collapses to, and not one of them has any
# business in maths, so putting the backslash back cannot break a good payload.
_ESCAPED_BACKSLASH = (("\x07", "a"), ("\x08", "b"), ("\x0b", "v"),
                      ("\x0c", "f"), ("\r", "r"), ("\n", "n"), ("\t", "t"))


def _repair_latex(latex):
    fixed = latex
    for char, letter in _ESCAPED_BACKSLASH:
        # Only where a LETTER follows. A bare newline in the payload is
        # whitespace; a newline glued to "u" is \nu with its backslash eaten.
        fixed = re.sub(re.escape(char) + r'(?=[A-Za-z])',
                       lambda match, l=letter: "\\" + l, fixed)
    fixed = re.sub(r'[\x00-\x1f]', ' ', fixed)
    if fixed != latex:
        print(f"[VISUAL] Put back a backslash the equation lost: {fixed!r}",
              flush=True)
    return fixed


def _equation(payload):
    """Set the maths properly, with matplotlib's own TeX renderer.

    mathtext rather than a real LaTeX install: it needs no system packages,
    handles everything a school equation contains, and draws it as an image
    instead of as characters a proportional font would space wrongly.
    """
    latex = _repair_latex(payload).strip().strip("$").strip()
    if not latex:
        return None, "there was no equation to write"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return None, f"the maths renderer is not available ({exc})"

    for size in (46, 40, 34, 28, 24):
        figure = plt.figure(figsize=(RENDER_W / 100, RENDER_H / 100), dpi=100)
        try:
            figure.text(0.5, 0.5, f"${latex}$", fontsize=size, ha="center",
                        va="center", color=INK)
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", facecolor=PAPER,
                           bbox_inches="tight", pad_inches=0.35)
        except Exception as exc:
            plt.close(figure)
            # A malformed expression is the model's mistake, not the student's.
            # The reason is SPOKEN, so the parser's own complaint -- newlines, a
            # caret pointing at a column, the exception class -- cannot go in
            # it. That belongs in the log, where somebody can read it.
            print(f"[VISUAL] Bad LaTeX {latex!r}: {exc}", flush=True)
            return None, "I could not write that equation out"
        plt.close(figure)
        buffer.seek(0)
        drawn = Image.open(buffer).convert("RGB")
        if drawn.width <= RENDER_W and drawn.height <= RENDER_H:
            sheet = Image.new("RGB", (RENDER_W, RENDER_H), PAPER)
            sheet.paste(drawn, ((RENDER_W - drawn.width) // 2,
                                (RENDER_H - drawn.height) // 2))
            return sheet, ""
    return None, "that equation is too long to fit on the board"


def _fit_photo(source):
    """A downloaded picture, letterboxed onto the sheet on a soft ground."""
    # FLATTENED ONTO WHITE FIRST. Cut-out PNGs are most of what an image search
    # returns for "a toucan on a plain background", and convert("RGB") fills
    # their transparency with BLACK -- so the best results came back as a bird
    # in a black box, which is the one thing the search was trying to avoid.
    if source.mode in ("RGBA", "LA", "P"):
        source = source.convert("RGBA")
        sheet = Image.new("RGB", source.size, PAPER)
        sheet.paste(source, (0, 0), source)
        source = sheet
    source = source.convert("RGB")
    fitted = source.copy()
    fitted.thumbnail((RENDER_W - 16, RENDER_H - 16), Image.LANCZOS)
    # The ground is the picture itself, blown up and blurred, so a portrait
    # photo does not sit in two white bars.
    ground = source.resize((RENDER_W, RENDER_H), Image.LANCZOS)
    ground = ground.filter(ImageFilter.GaussianBlur(24))
    ground = Image.blend(ground, Image.new("RGB", ground.size, PAPER), 0.62)
    ground.paste(fitted, ((RENDER_W - fitted.width) // 2,
                          (RENDER_H - fitted.height) // 2))
    return ground


# ---------------------------------------------------------------------------
# Seedream -- the drawing hand, when there is a network
# ---------------------------------------------------------------------------
# WHAT CHANGED AND WHAT IT COSTS. Everything above draws on the Pi. Seedream
# draws in a datacentre, and it is better looking than anything in this file --
# a life cycle it renders has real artwork in it, not four rectangles. It is
# now the FIRST thing tried for every kind, with the local renderers kept
# underneath as the fallback.
#
# The fallback is not politeness. Seedream needs the wifi, takes several
# seconds, and costs a fraction of a cent a go, and this device lives in a
# bedroom where the first of those three fails regularly. When the call does
# not come back, the local drawing goes up instead and the student sees a
# diagram rather than an apology.
#
# THE PART TO KNOW ABOUT: an image model DRAWS a picture of a graph, it does
# not PLOT one. Ask it for y = x^2 and you get a convincing parabola whose
# points are wherever the brush went, and ask it for an equation and roughly
# one in five comes back with a letter subtly wrong. That was said plainly
# before this was wired up and the answer was to route everything through it
# anyway, so everything is routed through it. VISUAL_SEEDREAM_KINDS is how you
# take a kind back off it later without touching this file: set it to
# "picture,cycle,steps" and equations and graphs return to being drawn here,
# where the numbers are the numbers.
#
# The exception that is not configurable is the LIVE graph -- y = x^2 with the
# sliders under it. That one redraws on every pixel of a finger drag and could
# never be a network round trip. See plot_spec() further down.
FAL_KEY = os.getenv("FAL_KEY", "")
SEEDREAM_MODEL = os.getenv("SEEDREAM_MODEL",
                           "fal-ai/bytedance/seedream/v4/text-to-image")
SEEDREAM_TIMEOUT_S = float(os.getenv("SEEDREAM_TIMEOUT_S", "20"))
# Seedream will not go below 1024 on a side. 1280x720 is the nearest thing to
# the 760x420 sheet the rest of this file composes for, so what comes back
# needs letterboxing rather than cropping.
SEEDREAM_W = int(os.getenv("SEEDREAM_W", "1280"))
SEEDREAM_H = int(os.getenv("SEEDREAM_H", "720"))
_ALL_KINDS = "cycle,steps,flow,diagram,graph,plot,chart,bar,equation,formula,maths,picture,photo,image"
SEEDREAM_KINDS = {k.strip().lower()
                  for k in os.getenv("VISUAL_SEEDREAM_KINDS", _ALL_KINDS).split(",")
                  if k.strip()}

# What every prompt ends with. The screen is 301x186 and a busy illustration is
# a smudge at that size, so the flatness and the empty background are not taste,
# they are the only way the thing is legible when it lands.
SEEDREAM_STYLE = ("Flat vector educational illustration for a children's "
                  "textbook. Plain white background, bold clean shapes, deep "
                  "blue and teal palette, generous spacing, no shadows, no "
                  "border, no watermark.")


def _seedream_prompt(kind, payload):
    """One English sentence describing the picture, per kind of visual."""
    kind = kind.lower()
    parts = _split_labels(payload, limit=40)
    if kind in ("picture", "photo", "image"):
        subject = re.sub(r'\s+', ' ', payload).strip(" ;|.")
        return (f"A clear, friendly educational illustration of {subject}, "
                f"centred, filling the frame. {SEEDREAM_STYLE}")
    if kind in ("equation", "formula", "maths"):
        latex = _repair_latex(payload).strip().strip("$").strip()
        return (f"The single mathematical formula {latex} written out large and "
                f"centred in correct typeset mathematics, exactly as given, "
                f"spelled letter for letter, nothing else on the page. "
                f"{SEEDREAM_STYLE}")
    if kind in ("graph", "plot", "chart", "bar"):
        title, data, is_bar, names = _points(payload)
        shape = "bar chart" if is_bar else "line graph"
        listed = ("; ".join(f"{n}={v:g}" for n, (_, v) in zip(names, data))
                  if is_bar else
                  "; ".join(f"({x:g}, {y:g})" for x, y in data))
        return (f"A {shape} titled \"{title or 'Graph'}\" on labelled axes with "
                f"a light grid, plotting exactly these values and no others: "
                f"{listed}. {SEEDREAM_STYLE}")
    title, labels = ((parts[0], parts[1:]) if len(parts) > 2 else (None, parts))
    joined = " then ".join(labels)
    if kind == "cycle":
        return (f"A circular cycle diagram titled \"{title or 'Cycle'}\": "
                f"{len(labels)} labelled stages arranged in a ring with curved "
                f"arrows running clockwise between them, one small picture per "
                f"stage. The stages, in order and spelled exactly: {joined}. "
                f"{SEEDREAM_STYLE}")
    return (f"A left-to-right flow diagram titled \"{title or 'Steps'}\": "
            f"{len(labels)} labelled boxes joined by arrows, one small picture "
            f"per box. The steps, in order and spelled exactly: {joined}. "
            f"{SEEDREAM_STYLE}")


def _seedream(kind, payload):
    """A PIL image from Seedream, or None if it could not be had.

    None is never an error the student hears about -- the caller falls through
    to the renderer below it and draws the thing locally instead.
    """
    if not FAL_KEY:
        return None
    if kind.lower() not in SEEDREAM_KINDS:
        return None
    import base64
    import json
    import requests
    started = time.time()
    try:
        reply = requests.post(
            f"https://fal.run/{SEEDREAM_MODEL}",
            headers={"Authorization": f"Key {FAL_KEY}",
                     "Content-Type": "application/json"},
            data=json.dumps({"prompt": _seedream_prompt(kind, payload),
                             "image_size": {"width": SEEDREAM_W,
                                            "height": SEEDREAM_H},
                             "num_images": 1,
                             "enable_safety_checker": True}),
            timeout=SEEDREAM_TIMEOUT_S)
        reply.raise_for_status()
        body = reply.json()
    except Exception as exc:
        print(f"[VISUAL] Seedream did not answer ({exc}); drawing it here.",
              flush=True)
        return None
    # fal returns {"images": [{"url": ...}]}, and has at times returned a bare
    # {"image": {...}}. Both are read rather than one, because a shape change
    # on their side should cost a fallback, not a traceback.
    images = body.get("images") or ([body["image"]] if body.get("image") else [])
    for entry in images:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if not url:
            continue
        try:
            if url.startswith("data:"):
                blob = base64.b64decode(url.split(",", 1)[1])
                drawn = Image.open(io.BytesIO(blob))
            else:
                drawn = _download(url)
            print(f"[VISUAL] Seedream drew the {kind} in "
                  f"{time.time() - started:.1f}s.", flush=True)
            return drawn
        except Exception as exc:
            print(f"[VISUAL] Seedream's picture would not open ({exc}).",
                  flush=True)
    return None


# ---------------------------------------------------------------------------
# a picture of a real thing -- the one kind that needs the world
# ---------------------------------------------------------------------------
# Search FIRST, generate second, and that order is deliberate. A photograph of a
# toucan is free, arrives in a second, and is a real toucan; a generated one
# costs a fraction of a cent, takes several seconds on this connection, and is a
# plausible bird. Generation earns its place only when the search comes back
# with nothing, which on a home connection it sometimes does.
VISUAL_IMAGE_MODEL = os.getenv("VISUAL_IMAGE_MODEL", "google/gemini-2.5-flash-image")
VISUAL_IMAGE_GEN = os.getenv("VISUAL_IMAGE_GEN", "1") != "0"
# Added to every search, because what is wanted here is a clear picture OF a
# thing against nothing, not a photograph of a scene containing one.
PICTURE_HINT = "clear photo on plain background"


def _download(url):
    import requests
    with requests.get(url, timeout=FETCH_TIMEOUT_S, stream=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as reply:
        reply.raise_for_status()
        kind = reply.headers.get("Content-Type", "")
        if kind and not kind.startswith("image/"):
            raise ValueError(f"that link is {kind}, not a picture")
        blob, size = io.BytesIO(), 0
        for block in reply.iter_content(65536):
            size += len(block)
            if size > FETCH_MAX_BYTES:
                raise ValueError("that picture is too big to open here")
            blob.write(block)
    blob.seek(0)
    return Image.open(blob)


def _search_picture(query):
    """The first result that actually downloads and opens. None if none does."""
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.images(f"{query} {PICTURE_HINT}", max_results=6))
    except Exception as exc:
        print(f"[VISUAL] Picture search failed ({exc}).", flush=True)
        return None
    for result in results:
        url = result.get("image") or result.get("thumbnail")
        if not url:
            continue
        try:
            return _download(url)
        except Exception as exc:
            print(f"[VISUAL] Skipped {url[:60]}... ({exc}).", flush=True)
    return None


def _generate_picture(query):
    """Ask the model for one, when the web had nothing. None if that fails too."""
    if not VISUAL_IMAGE_GEN:
        return None
    try:
        import base64
        import assistant
        done = assistant.openrouter_client.with_options(max_retries=0).chat.completions.create(
            model=VISUAL_IMAGE_MODEL,
            messages=[{"role": "user", "content":
                       f"A clear, friendly educational illustration of {query}, "
                       f"on a plain white background, no text or labels."}],
            modalities=["image", "text"],
        )
        for image in (getattr(done.choices[0].message, "images", None) or []):
            url = (image.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
            if url:
                return _download(url)
    except Exception as exc:
        print(f"[VISUAL] Could not draw one either ({exc}).", flush=True)
    return None


def _picture(payload):
    query = re.sub(r'\s+', ' ', payload).strip(" ;|.")
    if not query:
        return None, "there was nothing to look for"
    started = time.time()
    source = _search_picture(query) or _generate_picture(query)
    if source is None:
        return None, "I could not find a picture of that"
    print(f"[VISUAL] Picture of {query!r} in {time.time() - started:.1f}s.",
          flush=True)
    return _fit_photo(source), ""


# ---------------------------------------------------------------------------
# the live graph -- y = x^2, with the numbers in it on sliders
# ---------------------------------------------------------------------------
# WHY THIS IS NOT A PICTURE. Every other kind in this file is answered with a
# PNG: rendered once, put on the board, and that is the end of it. "Show me
# y = x squared" is the one ask where the answer is not a picture but a THING,
# because the next sentence out of a student looking at a parabola is always
# "what if it was x cubed" -- and the whole lesson is in watching the curve move
# while the number changes, not in being shown a second picture afterwards.
#
# So a function graph returns a SPEC instead of a path, the screen draws it with
# canvas lines, and every knob in the expression gets a slider under it. A drag
# resamples and moves the line, which on this Pi is about a millisecond. There
# is no network in that loop and there could not be: Seedream takes four
# seconds, and a slider that answers in four seconds is a broken slider.
#
# AND NOTHING HERE IS EVALUATED, which is the rule the top of this file sets and
# the reason an earlier shape of the graph took points rather than a formula.
# The expression is parsed to an AST and the AST is WALKED -- see _evaluate. No
# compile(), no eval(), no attribute access, no subscripts, no names but x, the
# declared parameters, pi and e, and no calls but the twenty in _FUNCTIONS. A
# model that returns __import__("os").system("...") gets a rejected expression
# and a spoken "I could not plot that", because there is no node type in that
# string this walker will step into.
import ast
import copy

_FUNCTIONS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "sqrt": math.sqrt, "exp": math.exp, "log": math.log,
    "log10": math.log10, "log2": math.log2, "abs": abs,
    "floor": math.floor, "ceil": math.ceil, "round": round,
    "degrees": math.degrees, "radians": math.radians,
    "min": min, "max": max,
}
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_OPERATORS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b, ast.FloorDiv: lambda a, b: a // b,
}
# x ** 5000 is not a graph, it is a hang followed by a number with four thousand
# digits in it. School maths lives well inside this.
MAX_POWER = 64
# How many points the curve is sampled at. 240 is one per pixel of the board's
# width, so the line is smooth there and still smooth enlarged.
SAMPLES = 240


class _BadExpression(Exception):
    """The expression is not one this will plot. Never shown to the student."""


def _check(node, names):
    """Walk the tree once, refusing anything that is not arithmetic.

    Runs BEFORE any value is computed, so an expression that would reach for
    the interpreter is rejected while it is still a tree.
    """
    if isinstance(node, ast.Expression):
        return _check(node.body, names)
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise _BadExpression("only numbers")
        return
    if isinstance(node, ast.Name):
        if node.id not in names and node.id not in _CONSTANTS:
            raise _BadExpression(f"unknown name {node.id}")
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise _BadExpression("bad unary operator")
        return _check(node.operand, names)
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, tuple(_OPERATORS) + (ast.Pow,)):
            raise _BadExpression("bad operator")
        _check(node.left, names)
        return _check(node.right, names)
    if isinstance(node, ast.Call):
        if (not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS
                or node.keywords):
            raise _BadExpression("bad call")
        for argument in node.args:
            _check(argument, names)
        return
    raise _BadExpression(f"bad node {type(node).__name__}")


def _evaluate(node, x, knobs):
    """The value of the tree at x. `knobs` overrides what the sliders own.

    Returns None instead of a number wherever the curve genuinely has no value
    -- a negative square root, a division by zero, a number too big to hold --
    and the screen breaks the line there rather than drawing through it.
    """
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, x, knobs)
    if isinstance(node, ast.Constant):
        index = getattr(node, "knob_index", None)
        return knobs[index] if index is not None else node.value
    if isinstance(node, ast.Name):
        if node.id == "x":
            return x
        if node.id in knobs:
            return knobs[node.id]
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, x, knobs)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, x, knobs)
        right = _evaluate(node.right, x, knobs)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_POWER:
                    return None
                # A negative base to a fractional power is a complex number,
                # which is a hole in the curve and not an answer to draw.
                if left < 0 and right != int(right):
                    return None
                return float(left) ** float(right)
            return _OPERATORS[type(node.op)](left, right)
        except (ZeroDivisionError, ValueError, OverflowError):
            return None
    if isinstance(node, ast.Call):
        arguments = [_evaluate(a, x, knobs) for a in node.args]
        if any(a is None for a in arguments):
            return None
        try:
            return float(_FUNCTIONS[node.func.id](*arguments))
        except (ValueError, OverflowError, ZeroDivisionError, TypeError):
            return None
    return None


# "y = 2x^2" is what a person writes and "2*x**2" is what the parser reads.
# Neither the model nor the student should have to know that, so the three
# differences are closed here rather than in the prompt.
def _normalise(text):
    text = text.strip()
    text = re.sub(r'^\s*[yf]\s*(?:\(\s*x\s*\))?\s*=\s*', '', text)
    text = text.replace("^", "**").replace("\u00d7", "*").replace("\u00f7", "/")
    # An implied multiplication: 2x, 3(x+1), x(x-1), 2pi, 2sin(x).
    #
    # The lookbehind is doing the real work. Without it the 10 in log10(x) is a
    # number followed by a bracket like any other and the expression becomes
    # log10*(x), which is a name this does not know multiplied by x -- so the
    # one function whose name ends in a digit stopped plotting. A number only
    # takes an implied * when nothing lettered or numeric runs into its front,
    # which is exactly what distinguishes the 10 in "10x" from the 10 in
    # "log10".
    text = re.sub(r'(?<![A-Za-z_0-9.])(\d+(?:\.\d+)?)\s*(?=[A-Za-z(])',
                  r'\1*', text)
    text = re.sub(r'\b(x|pi|e)\s*\(', r'\1*(', text)
    text = re.sub(r'\)\s*(?=[\dxA-Za-z(])', r')*', text)
    return text


# One slider per number in the expression, and the position of the number is
# what decides how it behaves. An EXPONENT snaps to whole numbers, because the
# whole reason a student drags that one is to get from x^2 to x^3 and stopping
# at 2.7 on the way is noise. Everything else moves continuously.
def _knob_range(value, is_exponent):
    if is_exponent:
        low, high = min(-3.0, value - 2), max(6.0, value + 2)
        return low, high, 1.0
    span = max(5.0, abs(value) * 3)
    return value - span, value + span, 0.1


def _find_knobs(tree, parameters):
    """The sliders, in reading order. Named parameters win over bare numbers.

    A model that writes "y = a*x^2 + b; a=1; b=0" has said which numbers are
    the interesting ones, so those are the sliders. A model that just writes
    "y = x^2" has not, so EVERY number in it becomes one -- which for that
    expression is the exponent, and dragging it is exactly the thing that was
    asked for.
    """
    knobs = []
    if parameters:
        for name, value in parameters.items():
            low, high, step = _knob_range(value, False)
            knobs.append({"key": name, "label": name, "value": value,
                          "low": low, "high": high, "step": step})
        return knobs[:3]
    exponents = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exponents.add(id(node.right))
    # In the order they are WRITTEN, not the order ast.walk reaches them. That
    # walk is breadth-first, so "2x^2 + 3" handed back the 3 before either 2 and
    # the sliders came out in an order that matched nothing on the screen.
    numbers = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Constant)),
                     key=lambda n: (n.lineno, n.col_offset))
    for index, node in enumerate(numbers[:3]):
        is_exponent = id(node) in exponents
        node.knob_index = index
        value = float(node.value)
        low, high, step = _knob_range(value, is_exponent)
        knobs.append({"key": index, "label": "power" if is_exponent else "number",
                      "value": value, "low": low, "high": high, "step": step})
    # Two sliders both reading "number" is worse than no label at all, so they
    # are numbered -- but only within their own kind, and only when there is
    # more than one of that kind. One power slider stays "power".
    for role in ("number", "power"):
        same = [k for k in knobs if k["label"] == role]
        if len(same) > 1:
            for position, knob in enumerate(same, 1):
                knob["label"] = f"{role} {position}"
    return knobs


def _looks_like_a_function(part):
    """True for "y = x^2" and "sin(x)", false for "Mon=3" and "1,5"."""
    return bool(re.search(r'(?<![A-Za-z])x(?![A-Za-z])', part))


def plot_spec(kind, payload):
    """A live-graph spec for "y = x^2", or None if this is not that.

    Returned to actions.show_visual_action, which sends it to the screen
    instead of rendering a PNG. None means "this was not a formula" and the
    ordinary picture path takes over, so bar data and x,y points are untouched.
    """
    if (kind or "").strip().lower() not in ("graph", "plot", "chart", "bar"):
        return None
    parts = _split_labels(payload or "", limit=12)
    formula_at = next((i for i, p in enumerate(parts)
                       if _looks_like_a_function(p)), None)
    if formula_at is None:
        return None
    title = "; ".join(parts[:formula_at]).strip() or None
    expression = _normalise(parts[formula_at])
    low, high = -5.0, 5.0
    parameters = {}
    for part in parts[formula_at + 1:]:
        window = re.match(r'^\s*x\s*(?:range)?\s*[:=]\s*(-?[\d.]+)\s*(?:\.\.|to|,)'
                          r'\s*(-?[\d.]+)\s*$', part, re.IGNORECASE)
        if window:
            low, high = float(window.group(1)), float(window.group(2))
            continue
        named = re.match(r'^\s*([a-zA-Z]\w*)\s*=\s*(-?\d+(?:\.\d+)?)\s*$', part)
        if named and named.group(1) != "x":
            parameters[named.group(1)] = float(named.group(2))
    try:
        tree = ast.parse(expression, mode="eval")
        _check(tree, set(parameters) | {"x"})
    except (SyntaxError, ValueError, _BadExpression) as exc:
        print(f"[VISUAL] Not plotting {expression!r}: {exc}", flush=True)
        return None
    if low >= high:
        low, high = -5.0, 5.0
    return {"title": title, "source": parts[formula_at].strip(), "tree": tree,
            "knobs": _find_knobs(tree, parameters), "x_low": low, "x_high": high}


def sample(spec, values=None):
    """[(x, y) or (x, None)] across the window, for the knobs as they stand.

    The Nones are the holes -- 1/x at zero, sqrt of a negative -- and the screen
    lifts the pen at each of them rather than joining across.
    """
    knobs = {}
    for index, knob in enumerate(spec["knobs"]):
        current = knob["value"] if values is None else values[index]
        knobs[knob["key"]] = current
    low, high = spec["x_low"], spec["x_high"]
    step = (high - low) / (SAMPLES - 1)
    points = []
    for index in range(SAMPLES):
        x = low + index * step
        value = _evaluate(spec["tree"], x, knobs)
        if value is None or value != value or value in (float("inf"), float("-inf")):
            points.append((x, None))
        else:
            points.append((x, float(value)))
    return points


def describe(spec, values=None):
    """What is on the board, in one line, for the model's device-state block.

    This has to say what the STUDENT can see, not what they originally asked
    for. Drag the power on y = x^2 up to six and the board is showing y = x^6;
    a state line still reading "y = x^2" would have the model correcting a
    graph that is not there, which is worse than telling it nothing.

    So a named parameter is reported as a value beside the formula, and a bare
    number is written back INTO the formula -- which is what the student sees
    and how they will refer to it next.
    """
    knobs = spec["knobs"]
    current = [k["value"] if values is None else values[index]
               for index, k in enumerate(knobs)]
    if not knobs or any(isinstance(k["key"], str) for k in knobs):
        text = spec["source"]
        for knob, value in zip(knobs, current):
            if isinstance(knob["key"], str):
                text += f", {knob['key']}={value:g}"
        return text
    tree = copy.deepcopy(spec["tree"])
    for node in ast.walk(tree):
        index = getattr(node, "knob_index", None)
        if index is not None:
            value = current[index]
            node.value = int(value) if value == int(value) else round(value, 4)
    try:
        # Back to the caret. The model is told to WRITE x^2 and is about to be
        # shown this line as the thing to change, so handing it Python's ** is
        # asking it to translate between two spellings of the same graph.
        return "y = " + ast.unparse(tree).replace(" ** ", "^")
    except Exception:
        return spec["source"]


# ---------------------------------------------------------------------------
# the kinds a picture cannot be wrong about
# ---------------------------------------------------------------------------
# Everything from here to the end of this section is drawn ON THE PI and never
# sent to Seedream, and that is the point of it rather than an oversight.
#
# An image model draws a convincing number line with the 7 missing and two 4s
# on it, a times table whose products do not multiply, and a clock whose hands
# say something other than the time underneath it. Those are not cosmetic
# misses -- they are the answer being wrong, on the one channel a child trusts
# more than the voice, and a six-year-old counting along a line has no way to
# know the line is lying. So every kind below is one where the NUMBERS ARE THE
# CONTENT, and every one of them is composed here where they are exact.
#
# See _ALL_KINDS: none of these names is in it, which is what keeps them local.
_NUM = r'-?\d+(?:\.\d+)?(?:\s*/\s*\d+)?'


def _value(text):
    """A number written any of the ways a person says one. None if it is not one.

    "1/2" has to work: a number line of halves is a Class 3 lesson, and the
    model writes the step as the fraction rather than as 0.5.
    """
    text = (text or "").strip()
    match = re.match(r'^(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$', text)
    if match and float(match.group(2)):
        return float(match.group(1)) / float(match.group(2))
    try:
        return float(text)
    except ValueError:
        return None


def _tidy(value):
    """4.0 -> "4", 2.5 -> "2.5". Numbers on a board are read, not computed."""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:g}"


def _as_fraction(value, denominator):
    """`value` written over `denominator`, reduced. "3/4", "1", "1 1/2"."""
    numerator = round(value * denominator)
    if denominator <= 1 or numerator % denominator == 0:
        return _tidy(value)
    sign = "-" if numerator < 0 else ""
    numerator = abs(numerator)
    common = math.gcd(numerator, int(denominator))
    top, bottom = numerator // common, int(denominator) // common
    whole, top = divmod(top, bottom)
    if whole and top:
        return f"{sign}{whole} {top}/{bottom}"
    if whole:
        return f"{sign}{whole}"
    return f"{sign}{top}/{bottom}"


def _number_line(payload):
    """A ruler across the sheet, with the numbers written under the ticks.

    The thing a Class 6 child asked for and did not get -- see the media.py
    note about what happened to "नंबर लाइन का ग्राफ़ बनाकर दिखाओ" instead.

    It is deliberately not a graph. A graph has two axes and a curve through
    them; a number line is one axis, and what is taught ON it is where a number
    sits, which numbers are being marked, and -- for addition and subtraction --
    the HOP from one number to another. So it takes marks and jumps as well as
    a range:

        Number line; 0..10
        Counting to ten; 0..10; mark 3; mark 7
        Adding 2 and 3; 0..10; jump 0->2; jump 2->5
        Halves; 0..2; step 1/2
    """
    parts = [p.strip() for p in re.split(r'[;|\n]', payload) if p.strip()]
    title = None
    low = high = None
    step = None
    marks, jumps = [], []

    for index, part in enumerate(parts):
        span = re.search(rf'({_NUM})\s*(?:\.\.\.?|→|->|\bto\b|\bसे\b)\s*({_NUM})',
                         part, re.IGNORECASE)
        jump = re.match(rf'^\s*(?:jump|hop|arrow|कूद)\s*[:= ]\s*({_NUM})\s*'
                        rf'(?:\.\.\.?|→|->|\bto\b)\s*({_NUM})', part, re.IGNORECASE)
        if jump:
            start, end = _value(jump.group(1)), _value(jump.group(2))
            if start is not None and end is not None:
                jumps.append((start, end))
            continue
        marked = re.match(r'^\s*(?:mark|marks|show|point|दिखाओ)\s*[:= ]\s*(.+)$',
                          part, re.IGNORECASE)
        if marked:
            marks += [v for v in (_value(t) for t in re.split(r'[, ]+', marked.group(1)))
                      if v is not None]
            continue
        sized = re.match(rf'^\s*(?:step|gap|interval|by)\s*[:= ]\s*({_NUM})\s*$',
                         part, re.IGNORECASE)
        if sized:
            step = _value(sized.group(1))
            continue
        if span and low is None:
            low, high = _value(span.group(1)), _value(span.group(2))
            continue
        if index == 0:
            title = part
        else:
            # A bare number this far down is a mark somebody wrote without the
            # word. "0..10; 3; 7" is what half the models produce.
            value = _value(part)
            if value is not None:
                marks.append(value)
            elif title is None:
                title = part

    if low is None or high is None:
        return None, "tell me where the number line should start and end"
    if high < low:
        low, high = high, low
    if high == low:
        high = low + 10

    if not step or step <= 0:
        # One tick per whole number as far as that stays readable, then coarser.
        # 21 is what fits across 660px with the labels still legible at 17pt.
        step = 1.0
        while (high - low) / step > 20:
            step *= 2 if step in (1.0, 5.0) else 2.5
    ticks = int(round((high - low) / step))
    if ticks > 60:                       # a step the model got badly wrong
        return None, "that number line has too many marks to fit on the board"
    # How the labels are written. A fractional step means fractions under every
    # tick -- "0.25" under a line of quarters is the wrong lesson.
    denominator = 1
    if abs(step - round(step)) > 1e-9:
        for candidate in (2, 3, 4, 5, 6, 8, 10, 12, 16):
            if abs(step * candidate - round(step * candidate)) < 1e-9:
                denominator = candidate
                break

    image, draw, top = _canvas(title)
    left, right = 70, RENDER_W - 70
    # A line with hops over it has to sit low enough to leave them room; a line
    # with none sits in the middle of what is left, because a ruler pinned to
    # the bottom of an otherwise empty sheet looks like a mistake.
    axis = (max(top + 130, RENDER_H - 150) if jumps
            else top + (RENDER_H - top) / 2 - 10)
    at = lambda value: left + (right - left) * (value - low) / (high - low)

    # The line itself, with an arrowhead at each end: a number line does not
    # stop at 10, it carries on, and drawing it stopped is the commonest thing
    # wrong with the ones in workbooks.
    draw.line([(left - 22, axis), (right + 22, axis)], fill=INK, width=5)
    for end, direction in ((left - 22, -1), (right + 22, 1)):
        for side in (1, -1):
            draw.line([(end, axis), (end - direction * 14, axis + side * 9)],
                      fill=INK, width=5)

    label_font = _font(18 if ticks <= 12 else 15)
    small = _font(13)
    for index in range(ticks + 1):
        value = low + index * step
        x = at(value)
        draw.line([(x, axis - 13), (x, axis + 13)], fill=INK, width=4)
        text = _as_fraction(value, denominator)
        font = label_font if len(text) <= 4 else small
        width = _text_size(draw, text, font)[0]
        draw.text((x - width / 2, axis + 22), text, font=font, fill=INK)

    for value in marks:
        if not (low <= value <= high):
            continue
        x = at(value)
        draw.ellipse([x - 13, axis - 13, x + 13, axis + 13],
                     fill=STAGE_COLOURS[3], outline=PAPER, width=3)

    # The hop, drawn as an arc OVER the line, which is how it is taught: you
    # count the jump, you do not measure it.
    for order, (start, end) in enumerate(jumps[:6]):
        x0, x1 = at(start), at(end)
        colour = STAGE_COLOURS[order % len(STAGE_COLOURS)]
        height = 46 + 22 * (order % 2)
        draw.arc([min(x0, x1), axis - height, max(x0, x1), axis + height],
                 start=180, end=360, fill=colour, width=5)
        # The head sits on the arc's own end, pointing the way it travelled.
        tip = (x1, axis - 4)
        _arrow(draw, (x1 - (6 if x1 > x0 else -6), axis - 22), tip, colour,
               width=5, head=12)
        count = _as_fraction(end - start, denominator)
        note = f"+{count}" if end >= start else count
        width = _text_size(draw, note, small)[0]
        draw.text(((x0 + x1) / 2 - width / 2, axis - height - 20), note,
                  font=small, fill=colour)
    return image, ""


def _table(payload):
    """Rows and columns. Cells are separated by | and rows by ; -- the first row
    is the heading.

        Times table of 3; 3 x 1 | 3; 3 x 2 | 6; 3 x 3 | 9
        Metals and non-metals; Metal | Non-metal; Iron | Sulphur

    A table is where an image model does its most confident damage, so this one
    is laid out by measurement: every column is as wide as its widest cell, the
    text is shrunk until the whole grid fits, and nothing is ever cropped.
    """
    rows = [[cell.strip() for cell in row.split("|")]
            for row in re.split(r'[;\n]', payload) if row.strip()]
    title = None
    if rows and len(rows[0]) == 1 and len(rows) > 1:
        title = rows[0][0]
        rows = rows[1:]
    if len(rows) < 2:
        return None, "a table needs a heading row and at least one row under it"
    columns = max(len(row) for row in rows)
    rows = [row + [""] * (columns - len(row)) for row in rows[:10]]

    image, draw, top = _canvas(title)
    left, right = 40, RENDER_W - 40
    height = min(52, (RENDER_H - top - 30) / len(rows))
    for size in (24, 21, 19, 17, 15, 13, 11):
        font = _font(size)
        widths = [max(_text_size(draw, row[c], font)[0] for row in rows) + 28
                  for c in range(columns)]
        if sum(widths) <= right - left:
            break
    spare = (right - left) - sum(widths)
    widths = [w + spare / columns for w in widths]

    y = top
    for index, row in enumerate(rows):
        x = left
        for column, cell in enumerate(row):
            box = (x, y, x + widths[column], y + height)
            if index == 0:
                draw.rectangle(box, fill=ACCENT_SOFT)
            draw.rectangle(box, outline=RULE, width=2)
            text_width, text_height = _text_size(draw, cell, font)
            draw.text((x + (widths[column] - text_width) / 2,
                       y + (height - text_height) / 2 - 2), cell, font=font,
                      fill=ACCENT if index == 0 else INK)
            x += widths[column]
        y += height
    # The heading is underlined heavily rather than boxed, so the eye takes the
    # first row as a heading and not as data.
    draw.line([(left, top + height), (right, top + height)], fill=ACCENT, width=4)
    return image, ""


def _timeline(payload):
    """When things happened, along one line. "1947 = Independence".

    Labels alternate above and below the line. Stacking them all on one side
    means the long ones collide, and a history timeline is nearly all long
    ones.
    """
    parts = _split_labels(payload, limit=12)
    events, title = [], None
    for index, part in enumerate(parts):
        match = re.match(r'^\s*(-?\d{1,4}\s*(?:BC|BCE|AD|CE)?)\s*[=:–-]\s*(.+)$',
                         part, re.IGNORECASE)
        if match:
            events.append((match.group(1).strip(), match.group(2).strip()))
        elif index == 0:
            title = part
    if len(events) < 2:
        return None, "a timeline needs at least two dates on it"

    image, draw, top = _canvas(title)
    left, right = 80, RENDER_W - 80
    axis = (top + RENDER_H - 20) / 2
    draw.line([(left - 20, axis), (right + 20, axis)], fill=RULE, width=6)
    _arrow(draw, (right, axis), (right + 24, axis), RULE, width=6, head=14)

    gap = (right - left) / max(len(events) - 1, 1)
    year_font = _font(20 if len(events) <= 6 else 16)
    for index, (year, label) in enumerate(events):
        x = left + index * gap
        colour = STAGE_COLOURS[index % len(STAGE_COLOURS)]
        above = index % 2 == 0
        draw.ellipse([x - 11, axis - 11, x + 11, axis + 11], fill=colour,
                     outline=PAPER, width=3)
        stem = 34 if above else 34
        y = axis - stem if above else axis + stem
        draw.line([(x, axis), (x, y)], fill=colour, width=3)
        width = _text_size(draw, year, year_font)[0]
        year_y = y - 26 if above else y + 4
        draw.text((x - width / 2, year_y), year, font=year_font, fill=colour)
        # The first and last captions are centred on an event that sits at the
        # very end of the line, so half of each hangs off the sheet unless the
        # centre is pulled back inside it. "First war of independence" under
        # 1857 was losing its first two letters.
        x = min(max(x, 30 + gap / 2), RENDER_W - 30 - gap / 2)
        # The caption gets the room between this event and the next one, and is
        # wrapped into it rather than trusted to be short.
        font, lines, line_h = _fitted_lines(draw, label, gap + 24, 70,
                                            (16, 15, 14, 13, 12))
        text_y = year_y - 4 - line_h * len(lines) if above else year_y + 26
        for line in lines:
            width = _text_size(draw, line, font)[0]
            draw.text((x - width / 2, text_y), line, font=font, fill=INK)
            text_y += line_h
    return image, ""


def _compare(payload):
    """Two overlapping circles: what each has, and what they share.

        Plants and animals; A = Plant cell; B = Animal cell;
        A: cell wall, chloroplast; B: centriole; both: nucleus, DNA
    """
    parts = _split_labels(payload, limit=12)
    title, names, sides = None, {"A": "A", "B": "B"}, {"A": [], "B": [], "both": []}
    for index, part in enumerate(parts):
        named = re.match(r'^\s*([AB])\s*=\s*(.+)$', part, re.IGNORECASE)
        if named:
            names[named.group(1).upper()] = named.group(2).strip()
            continue
        listed = re.match(r'^\s*(A|B|both|common|shared|दोनों)\s*:\s*(.+)$', part,
                          re.IGNORECASE)
        if listed:
            key = listed.group(1).upper()
            key = "both" if key not in ("A", "B") else key
            sides[key] += [item.strip() for item in listed.group(2).split(",")
                           if item.strip()]
            continue
        if index == 0:
            title = part
    if not any(sides.values()):
        return None, "tell me what goes in each side of the comparison"

    image, draw, top = _canvas(title)
    room = RENDER_H - top - 24
    radius = min(150, room / 2)
    centre_y = top + room / 2
    # Overlapping by two thirds of a radius: enough of a lens to write two
    # short lines in, and still two circles anybody can see are two circles.
    left_x, right_x = RENDER_W / 2 - radius * 0.62, RENDER_W / 2 + radius * 0.62
    for x, colour in ((left_x, STAGE_COLOURS[0]), (right_x, STAGE_COLOURS[1])):
        draw.ellipse([x - radius, centre_y - radius, x + radius, centre_y + radius],
                     outline=colour, width=5)

    heading = _font(19)
    for x, key, colour in ((left_x, "A", STAGE_COLOURS[0]),
                           (right_x, "B", STAGE_COLOURS[1])):
        text = names[key]
        width = _text_size(draw, text, heading)[0]
        anchor = x - radius * 0.55 if key == "A" else x + radius * 0.55
        draw.text((anchor - width / 2, centre_y - radius - 30), text,
                  font=heading, fill=colour)

    def write(items, x, width_limit, colour):
        """The items down the middle of one region of the diagram.

        Five is the most a lens or a crescent holds at a size anybody reads
        from across a room; anything past that belongs in what she SAYS.
        """
        items = items[:5]
        if not items:
            return
        font = _font(16 if max((len(i) for i in items), default=0) < 14 else 13)
        line_h = _text_size(draw, "Ag", font)[1] + 5
        lines = [line for item in items
                 for line in _wrap(draw, item, font, width_limit)[:2]]
        y = centre_y - line_h * len(lines) / 2
        for line in lines:
            text_width = _text_size(draw, line, font)[0]
            draw.text((x - text_width / 2, y), line, font=font, fill=colour)
            y += line_h

    write(sides["A"], left_x - radius * 0.45, radius * 0.95, INK)
    write(sides["B"], right_x + radius * 0.45, radius * 0.95, INK)
    write(sides["both"], RENDER_W / 2, radius * 0.9, ACCENT)
    return image, ""


def _tree(payload):
    """A hierarchy, written as edges: "Living things > Plants".

        Classification; Living things > Plants; Living things > Animals;
        Animals > Vertebrates; Animals > Invertebrates
    """
    parts = _split_labels(payload, limit=16)
    title, edges, order = None, [], []
    for index, part in enumerate(parts):
        if ">" in part or "→" in part:
            parent, child = [p.strip()
                             for p in re.split(r'>|→', part, maxsplit=1)[:2]]
            if parent and child:
                edges.append((parent, child))
                for node in (parent, child):
                    if node not in order:
                        order.append(node)
            continue
        if index == 0:
            title = part
    if not edges:
        return None, "a tree needs at least one line like parent > child"

    children = {}
    for parent, child in edges:
        children.setdefault(parent, []).append(child)
    has_parent = {child for _parent, child in edges}
    roots = [node for node in order if node not in has_parent] or [order[0]]

    # Breadth first, so every node lands on the row its depth says it should be
    # on however the edges were written down.
    levels, seen, frontier, depth = [], set(), roots, 0
    while frontier and depth < 4:
        row = [node for node in frontier if node not in seen]
        if not row:
            break
        seen.update(row)
        levels.append(row)
        frontier = [child for node in row for child in children.get(node, [])]
        depth += 1

    image, draw, top = _canvas(title)
    room = RENDER_H - top - 24
    row_gap = room / max(len(levels), 1)
    placed = {}
    for depth, row in enumerate(levels):
        y = top + row_gap * depth + row_gap / 2
        width = min(210, (RENDER_W - 40) / max(len(row), 1) - 14)
        for index, node in enumerate(row):
            x = (RENDER_W / (len(row) + 1)) * (index + 1)
            box = (x - width / 2, y - 28, x + width / 2, y + 28)
            placed[node] = (x, y, box)
    for parent, child in edges:
        if parent in placed and child in placed:
            draw.line([(placed[parent][0], placed[parent][2][3]),
                       (placed[child][0], placed[child][2][1])],
                      fill=RULE, width=3)
    for depth, row in enumerate(levels):
        colour = STAGE_COLOURS[depth % len(STAGE_COLOURS)]
        for node in row:
            _x, _y, box = placed[node]
            _rounded(draw, box, 14, fill=PAPER, outline=colour, width=4)
            _label_in_box(draw, box, node, INK, sizes=(19, 17, 15, 13, 11))
    return image, ""


def _grid(payload):
    """Rows of dots: what "3 times 4" actually looks like.

        Three fours; 3 x 4
    """
    parts = _split_labels(payload, limit=4)
    title, rows, columns = None, None, None
    for index, part in enumerate(parts):
        match = re.search(r'(\d{1,2})\s*(?:x|×|\*|by|into)\s*(\d{1,2})', part,
                          re.IGNORECASE)
        if match:
            rows, columns = int(match.group(1)), int(match.group(2))
        elif index == 0:
            title = part
    if not rows or not columns:
        return None, "tell me how many rows and how many in each row"
    if rows * columns > 144:
        return None, "that array is too big to count on the board"

    image, draw, top = _canvas(title or f"{rows} rows of {columns}")
    room_w, room_h = RENDER_W - 120, RENDER_H - top - 70
    pitch = min(room_w / columns, room_h / rows)
    radius = min(pitch * 0.32, 22)
    x0 = (RENDER_W - pitch * (columns - 1)) / 2
    y0 = top + (room_h - pitch * (rows - 1)) / 2
    for row in range(rows):
        for column in range(columns):
            x, y = x0 + pitch * column, y0 + pitch * row
            draw.ellipse([x - radius, y - radius, x + radius, y + radius],
                         fill=STAGE_COLOURS[row % len(STAGE_COLOURS)])
    sum_font = _font(26)
    note = f"{rows} x {columns} = {rows * columns}"
    width = _text_size(draw, note, sum_font)[0]
    draw.text(((RENDER_W - width) / 2, RENDER_H - 48), note, font=sum_font,
              fill=ACCENT)
    return image, ""


def _fraction(payload):
    """One or two fractions, as circles cut into slices with the part filled.

        Three quarters; 3/4
        Which is bigger; 3/4; 2/3
    """
    parts = _split_labels(payload, limit=5)
    title, fractions = None, []
    for index, part in enumerate(parts):
        for match in re.finditer(r'(\d{1,2})\s*/\s*(\d{1,2})', part):
            top_number, bottom = int(match.group(1)), int(match.group(2))
            if 0 < bottom <= 24 and top_number <= bottom:
                fractions.append((top_number, bottom))
        if index == 0 and not re.search(r'\d\s*/\s*\d', part):
            title = part
    if not fractions:
        return None, "tell me the fraction, like three over four"
    fractions = fractions[:2]

    image, draw, top = _canvas(title)
    room = RENDER_H - top - 30
    radius = min(140, room / 2 - 26)
    centres = ([RENDER_W / 2] if len(fractions) == 1
               else [RENDER_W / 3, RENDER_W * 2 / 3])
    centre_y = top + room / 2 - 14
    label_font = _font(30)
    for index, (top_number, bottom) in enumerate(fractions):
        cx = centres[index]
        box = [cx - radius, centre_y - radius, cx + radius, centre_y + radius]
        colour = STAGE_COLOURS[index % len(STAGE_COLOURS)]
        # Slices from twelve o'clock, clockwise, which is how a child is shown
        # to shade them in.
        for slice_index in range(bottom):
            start = -90 + 360 * slice_index / bottom
            end = -90 + 360 * (slice_index + 1) / bottom
            draw.pieslice(box, start, end,
                          fill=colour if slice_index < top_number else PAPER,
                          outline=INK, width=3)
        text = f"{top_number}/{bottom}"
        width = _text_size(draw, text, label_font)[0]
        draw.text((cx - width / 2, centre_y + radius + 12), text,
                  font=label_font, fill=colour)
    return image, ""


def _clock(payload):
    """A clock face reading the time it is labelled with. "quarter to four".

        Quarter past three; 3:15
    """
    parts = _split_labels(payload, limit=4)
    title, hour, minute = None, None, 0
    for index, part in enumerate(parts):
        match = re.search(r'(\d{1,2})\s*[:.]\s*(\d{2})', part)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
        elif index == 0:
            title = part
    if hour is None:
        return None, "tell me the time to put on the clock"
    hour, minute = hour % 12, minute % 60

    image, draw, top = _canvas(title)
    radius = min(150, (RENDER_H - top - 40) / 2)
    cx, cy = RENDER_W / 2, top + radius + 10
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                 fill=PAPER, outline=ACCENT, width=6)
    number_font = _font(24)
    for mark in range(60):
        angle = math.radians(-90 + mark * 6)
        outer = radius - 6
        inner = radius - (20 if mark % 5 == 0 else 11)
        draw.line([(cx + outer * math.cos(angle), cy + outer * math.sin(angle)),
                   (cx + inner * math.cos(angle), cy + inner * math.sin(angle))],
                  fill=INK if mark % 5 == 0 else RULE, width=4 if mark % 5 == 0 else 2)
    for number in range(1, 13):
        angle = math.radians(-90 + number * 30)
        x = cx + (radius - 44) * math.cos(angle)
        y = cy + (radius - 44) * math.sin(angle)
        text = str(number)
        width, height = _text_size(draw, text, number_font)
        draw.text((x - width / 2, y - height / 2 - 2), text, font=number_font,
                  fill=INK)
    # The hour hand moves WITH the minutes. Drawn on the hour exactly, "3:45"
    # shows a clock that a child reading it properly would call three o'clock.
    hour_angle = math.radians(-90 + (hour + minute / 60) * 30)
    minute_angle = math.radians(-90 + minute * 6)
    draw.line([(cx, cy), (cx + radius * 0.52 * math.cos(hour_angle),
                          cy + radius * 0.52 * math.sin(hour_angle))],
              fill=INK, width=11)
    draw.line([(cx, cy), (cx + radius * 0.78 * math.cos(minute_angle),
                          cy + radius * 0.78 * math.sin(minute_angle))],
              fill=STAGE_COLOURS[3], width=7)
    draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=INK)
    return image, ""


def _angle(payload):
    """Two rays from a point, opened by however many degrees it says.

        A right angle; 90
    """
    parts = _split_labels(payload, limit=4)
    title, degrees = None, None
    for index, part in enumerate(parts):
        match = re.search(r'(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg|°)?', part)
        if match and re.search(r'\d', part):
            degrees = float(match.group(1))
        elif index == 0:
            title = part
    if degrees is None or not 0 < degrees < 360:
        return None, "tell me how many degrees the angle is"

    image, draw, top = _canvas(title)
    length = min(300, RENDER_H - top - 90)
    vertex = (RENDER_W / 2 - length / 2, top + length + 20)
    end_a = (vertex[0] + length, vertex[1])
    radians = math.radians(degrees)
    end_b = (vertex[0] + length * math.cos(-radians),
             vertex[1] + length * math.sin(-radians))
    for end in (end_a, end_b):
        draw.line([vertex, end], fill=INK, width=6)
        draw.ellipse([end[0] - 7, end[1] - 7, end[0] + 7, end[1] + 7], fill=INK)
    arc_r = 70
    if abs(degrees - 90) < 0.5:
        # The square, not an arc. A right angle is marked with a box, and a
        # picture that marks it with an arc is teaching the wrong notation.
        draw.rectangle([vertex[0], vertex[1] - 34, vertex[0] + 34, vertex[1]],
                       outline=STAGE_COLOURS[3], width=5)
    else:
        draw.arc([vertex[0] - arc_r, vertex[1] - arc_r,
                  vertex[0] + arc_r, vertex[1] + arc_r],
                 start=-degrees, end=0, fill=STAGE_COLOURS[3], width=5)
    label = f"{_tidy(degrees)}°"
    font = _font(30)
    half = math.radians(degrees / 2)
    draw.text((vertex[0] + (arc_r + 26) * math.cos(-half) - 10,
               vertex[1] + (arc_r + 26) * math.sin(-half) - 16),
              label, font=font, fill=STAGE_COLOURS[3])
    draw.ellipse([vertex[0] - 8, vertex[1] - 8, vertex[0] + 8, vertex[1] + 8],
                 fill=INK)
    return image, ""


# The shapes _shape can draw, as the fraction of a unit box each corner sits at.
# Held as data rather than as a branch per shape so adding one is a line.
_POLYGONS = {
    "triangle":      [(0.5, 0.0), (1.0, 1.0), (0.0, 1.0)],
    "right triangle": [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)],
    "square":        [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
    "rectangle":     [(0.0, 0.15), (1.0, 0.15), (1.0, 0.85), (0.0, 0.85)],
    "rhombus":       [(0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)],
    "parallelogram": [(0.22, 0.1), (1.0, 0.1), (0.78, 0.9), (0.0, 0.9)],
    "trapezium":     [(0.22, 0.1), (0.78, 0.1), (1.0, 0.9), (0.0, 0.9)],
    "pentagon":      [(0.5, 0.0), (1.0, 0.38), (0.81, 1.0), (0.19, 1.0), (0.0, 0.38)],
    "hexagon":       [(0.25, 0.0), (0.75, 0.0), (1.0, 0.5), (0.75, 1.0),
                      (0.25, 1.0), (0.0, 0.5)],
    "octagon":       [(0.29, 0.0), (0.71, 0.0), (1.0, 0.29), (1.0, 0.71),
                      (0.71, 1.0), (0.29, 1.0), (0.0, 0.71), (0.0, 0.29)],
}
_POLYGON_ALIASES = {
    "equilateral triangle": "triangle", "isosceles triangle": "triangle",
    "right angled triangle": "right triangle",
    "right-angled triangle": "right triangle",
    "trapezoid": "trapezium", "diamond": "rhombus", "quadrilateral": "square",
    "पंचभुज": "pentagon", "षट्भुज": "hexagon", "त्रिभुज": "triangle",
    "वर्ग": "square", "आयत": "rectangle",
}


# What a measurement is CALLED decides where it goes. Labelling the sides in
# the order they were written put "base = 6 cm" on a triangle's right-hand edge
# and "height = 4 cm" along its bottom -- both drawn beautifully, both on the
# wrong line, which is a worse answer than no labels at all because it looks
# authoritative. A child copying that into an exercise book copies it wrong.
_SIDE_WORDS = {
    "base": "bottom", "bottom": "bottom", "length": "bottom", "width": "bottom",
    "आधार": "bottom",
    "height": "height", "altitude": "height", "perpendicular": "height",
    "ऊँचाई": "height", "ऊंचाई": "height",
    "side": "side", "slant": "side", "hypotenuse": "side", "भुजा": "side",
    "breadth": "left", "depth": "left",
}


def _measure_polygon(draw, points, notes, x0, y0, size):
    """Write the measurements on the edges they name. Returns what is left over.

    `height` is not an edge at all -- it is the perpendicular from the top of
    the shape down to its base, and it is drawn as the dashed line a textbook
    draws, because a triangle's height written along one of its sides is the
    single commonest thing wrong with a hand-drawn diagram.
    """
    font = _font(20)
    centre = (x0 + size / 2, y0 + size / 2)
    # Which index is which edge, measured from the points themselves rather
    # than assumed: the bottom edge is the one whose midpoint sits lowest.
    edges = [(index, ((points[index][0] + points[(index + 1) % len(points)][0]) / 2,
                      (points[index][1] + points[(index + 1) % len(points)][1]) / 2))
             for index in range(len(points))]
    bottom = max(edges, key=lambda edge: edge[1][1])[0]
    used, leftover = {bottom: False}, []

    def write_at(mid, text):
        width, height = _text_size(draw, text, font)
        away = (mid[0] - centre[0], mid[1] - centre[1])
        span = math.hypot(*away) or 1
        draw.text((mid[0] + away[0] / span * 26 - width / 2,
                   mid[1] + away[1] / span * 26 - height / 2), text,
                  font=font, fill=INK)

    free = [index for index, _mid in edges if index != bottom]
    for note in notes:
        if "=" not in note:
            leftover.append(note)
            continue
        name, _, value = note.partition("=")
        name, value = name.strip().lower(), value.strip()
        role = _SIDE_WORDS.get(re.sub(r'^(?:the|its)\s+', '', name))
        if role == "height":
            # Down from the highest corner to the base, dashed, with a right
            # angle box where it lands.
            apex = min(points, key=lambda point: point[1])
            foot = (apex[0], y0 + size)
            for step in range(int(apex[1]), int(foot[1]), 14):
                draw.line([(apex[0], step), (apex[0], min(step + 7, foot[1]))],
                          fill=INK_DIM, width=3)
            draw.rectangle([foot[0], foot[1] - 14, foot[0] + 14, foot[1]],
                           outline=INK_DIM, width=2)
            draw.text((apex[0] + 12, (apex[1] + foot[1]) / 2), value, font=font,
                      fill=INK_DIM)
            continue
        if role == "bottom" and not used[bottom]:
            used[bottom] = True
            write_at(edges[bottom][1], value)
            continue
        if free:
            write_at(edges[free.pop(0)][1], value)
        else:
            leftover.append(note)
    return leftover


def _shape(payload):
    """A flat shape, drawn big, with whatever measurements were given on it.

        Triangle; base = 6 cm; height = 4 cm
        Circle; radius = 5 cm
        Hexagon

    Measurements land on the sides in the order they are written, which is the
    order a textbook writes them in. Anything that is not side = value is
    written underneath instead of being dropped.
    """
    parts = _split_labels(payload, limit=8)
    if not parts:
        return None, "tell me which shape to draw"
    wanted = re.sub(r'\s+', ' ', parts[0]).strip().lower().strip(".")
    wanted = re.sub(r'^(?:a|an|the)\s+', '', wanted)
    wanted = re.sub(r'\s*(?:shape|figure)$', '', wanted)
    name = _POLYGON_ALIASES.get(wanted, wanted)
    notes = parts[1:]
    title = parts[0].strip()

    image, draw, top = _canvas(title[:1].upper() + title[1:])
    room_h = RENDER_H - top - (70 if notes else 30)
    size = min(RENDER_W - 260, room_h)
    x0 = (RENDER_W - size) / 2
    y0 = top + (room_h - size) / 2
    colour = STAGE_COLOURS[0]

    if name in ("circle", "वृत्त"):
        draw.ellipse([x0, y0, x0 + size, y0 + size], fill=ACCENT_SOFT,
                     outline=colour, width=6)
        centre = (x0 + size / 2, y0 + size / 2)
        draw.line([centre, (x0 + size, centre[1])], fill=colour, width=4)
        draw.ellipse([centre[0] - 6, centre[1] - 6, centre[0] + 6, centre[1] + 6],
                     fill=colour)
    elif name in ("oval", "ellipse"):
        draw.ellipse([x0 - 60, y0 + size * 0.15, x0 + size + 60, y0 + size * 0.85],
                     fill=ACCENT_SOFT, outline=colour, width=6)
    elif name in ("semicircle", "half circle"):
        draw.pieslice([x0, y0, x0 + size, y0 + size * 2], 180, 360,
                      fill=ACCENT_SOFT, outline=colour, width=6)
    elif name in _POLYGONS:
        points = [(x0 + fx * size, y0 + fy * size) for fx, fy in _POLYGONS[name]]
        draw.polygon(points, fill=ACCENT_SOFT, outline=colour)
        # Pillow's polygon outline is one pixel whatever you ask, so the edges
        # are drawn again as lines to get a width that reads on the board.
        for index in range(len(points)):
            draw.line([points[index], points[(index + 1) % len(points)]],
                      fill=colour, width=6)
        notes = _measure_polygon(draw, points, notes, x0, y0, size)
    else:
        return None, f"I have not learned to draw a {wanted} yet"

    if notes:
        note_font = _font(20)
        text = "   ".join(notes)
        width = _text_size(draw, text, note_font)[0]
        draw.text(((RENDER_W - width) / 2, RENDER_H - 46), text, font=note_font,
                  fill=INK_DIM)
    return image, ""


def _written(payload):
    """The last resort: whatever they asked for, WRITTEN on the board.

    Reached when a kind is not one this file knows and the network could not
    draw it either -- see render_visual. It is not a good picture and it is not
    meant to be one. It is the difference between a student who asked to see
    something and got a card with the words on it, and a student who asked and
    was told no; on a device whose whole promise is "ask for anything and it
    goes on the board", the second one is the failure worth engineering away.
    """
    parts = _split_labels(payload, limit=8)
    if not parts:
        return None, "there was nothing to write on the board"
    title, lines = (parts[0], parts[1:]) if len(parts) > 1 else (None, parts)
    image, draw, top = _canvas(title)
    if not lines:
        return image, ""
    box = (50, top, RENDER_W - 50, RENDER_H - 30)
    height = (box[3] - box[1]) / len(lines)
    for index, line in enumerate(lines):
        y = box[1] + height * index
        colour = STAGE_COLOURS[index % len(STAGE_COLOURS)]
        draw.ellipse([box[0], y + height / 2 - 7, box[0] + 14, y + height / 2 + 7],
                     fill=colour)
        _label_in_box(draw, (box[0] + 30, y, box[2], y + height), line, INK,
                      sizes=(26, 23, 20, 18, 16, 14))
    return image, ""


# ---------------------------------------------------------------------------
# the one way in
# ---------------------------------------------------------------------------
KINDS = {
    "cycle": _cycle, "steps": _steps, "flow": _steps, "diagram": _steps,
    "graph": _graph, "plot": _graph, "chart": _graph, "bar": _graph,
    "equation": _equation, "formula": _equation, "maths": _equation,
    "picture": _picture, "photo": _picture, "image": _picture,
    # Drawn here and never by Seedream -- see "the kinds a picture cannot be
    # wrong about" above for why.
    "number_line": _number_line, "numberline": _number_line,
    "table": _table, "grid_table": _table,
    "timeline": _timeline,
    "compare": _compare, "venn": _compare,
    "tree": _tree, "hierarchy": _tree, "classification": _tree,
    "array": _grid, "grid": _grid, "dots": _grid,
    "fraction": _fraction, "fractions": _fraction,
    "clock": _clock, "time": _clock,
    "angle": _angle,
    "shape": _shape, "geometry": _shape, "figure": _shape,
    # Not a picture of anything, just the words on a card. Never chosen by the
    # model -- render_visual falls back to it when nothing else can draw the
    # thing that was asked for.
    "written": _written, "list": _written, "note": _written,
}

# What a student's own word for a drawing means here. The model is told the
# names in KINDS, and then says "draw a bar diagram" or "show a flowchart"
# anyway, because those are the words the textbook uses. Mapping them is a line
# each; refusing them costs the student the picture.
KIND_ALIASES = {
    "numberline": "number_line", "number-line": "number_line",
    "ruler": "number_line", "scale": "number_line",
    "flowchart": "steps", "flow_chart": "steps", "process": "steps",
    "stages": "steps", "sequence": "steps", "lifecycle": "cycle",
    "life_cycle": "cycle", "circle_diagram": "cycle",
    "bargraph": "graph", "bar_graph": "graph", "histogram": "graph",
    "linegraph": "graph", "line_graph": "graph", "pie": "fraction",
    "piechart": "fraction", "pie_chart": "fraction",
    "venn_diagram": "compare", "comparison": "compare",
    "family_tree": "tree", "mindmap": "tree", "map": "picture",
    "drawing": "picture", "illustration": "picture", "sketch": "picture",
    "triangle": "shape", "square": "shape", "rectangle": "shape",
    "circle": "shape", "polygon": "shape", "solid": "shape",
    "multiplication": "array", "times_table": "table",
    "chart_table": "table", "data": "table",
}


def _save(image):
    """The one file the screen reads. True if it is now on disk."""
    try:
        os.makedirs(VISUAL_DIR, exist_ok=True)
        image.save(VISUAL_PATH)
        return True
    except Exception as exc:
        print(f"[VISUAL] Could not save it ({exc}).", flush=True)
        return False


def resolve_kind(kind):
    """The name in KINDS this request is really asking for.

    An unknown word is NOT an error. "Draw me a kite", "show a food chain",
    "make a poster of the water cycle" all arrive as a kind nobody wrote a
    renderer for, and the honest answer to every one of them is a picture of
    the thing -- which is a kind that exists. So anything unrecognised becomes
    `picture`, and the payload is whatever they asked for.
    """
    kind = re.sub(r'[\s-]+', "_", (kind or "").strip().lower()).strip("_")
    if kind in KINDS:
        return kind
    if kind in KIND_ALIASES:
        return KIND_ALIASES[kind]
    # "bar_chart", "venn_diagram_of_plants" -- the useful word is in there
    # somewhere, so the longest known name that appears in it wins.
    for name in sorted(list(KINDS) + list(KIND_ALIASES), key=len, reverse=True):
        if name in kind:
            return KIND_ALIASES.get(name, name)
    if kind:
        print(f"[VISUAL] No renderer called {kind!r}; drawing it as a picture.",
              flush=True)
    return "picture"


def render_visual(kind, payload):
    """(path to a PNG, "") for one show_visual tag, or (None, why not).

    The reason is a phrase, not a stack trace: it is read out to the student
    when a visual cannot be made, so it has to be a thing a person would say.

    Seedream first for every kind it is switched on for, and the local drawing
    underneath when it does not answer. Both ends of that are silent: the
    student is told a picture failed only when BOTH have failed.

    ASKED FOR SOMETHING, THEY GET SOMETHING. Three things used to end in an
    apology instead of a drawing: a kind with no renderer, a renderer that
    could not read its payload, and one that threw. All three are now a fall
    BACKWARDS -- to a picture of what they asked for, and past that to the
    words on a card -- because "show me a number line" answered with "I do not
    know how to draw a number_line" is the device failing at the one thing it
    promises, and a plain drawing of roughly the right thing is not.
    """
    asked = (kind or "").strip().lower()
    kind = resolve_kind(kind)
    payload = payload or ""
    try:
        drawn = _seedream(kind, payload)
    except Exception as exc:
        print(f"[VISUAL] Seedream blew up ({exc}); drawing it here.", flush=True)
        drawn = None
    if drawn is not None and _save(_fit_photo(drawn)):
        return VISUAL_PATH, ""

    # Each attempt in turn, first one that draws wins. The reason from the
    # FIRST attempt is the one worth speaking if every attempt fails: it is
    # about the thing they actually asked for.
    attempts = [(kind, payload)]
    if kind != "picture":
        # What they asked for, as a picture of it. The payload is turned back
        # into a phrase -- a picture search cannot use semicolons.
        subject = re.sub(r'\s*[;|]\s*', ", ", payload).strip(" ,.")
        attempts.append(("picture", f"{subject or asked}"))
    if kind != "written":
        attempts.append(("written", payload))

    first_reason = ""
    for attempt_kind, attempt_payload in attempts:
        handler = KINDS.get(attempt_kind)
        if handler is None:
            continue
        try:
            image, reason = handler(attempt_payload)
        except Exception as exc:
            print(f"[VISUAL] {attempt_kind} failed: {exc}", flush=True)
            image, reason = None, ""
        if image is None:
            first_reason = first_reason or reason
            if attempt_kind != attempts[-1][0]:
                print(f"[VISUAL] {attempt_kind} could not draw it "
                      f"({reason or 'no reason given'}); trying the next way.",
                      flush=True)
            continue
        try:
            os.makedirs(VISUAL_DIR, exist_ok=True)
            image.save(VISUAL_PATH)
        except Exception as exc:
            print(f"[VISUAL] Could not save it ({exc}).", flush=True)
            return None, "that did not come out right"
        return VISUAL_PATH, ""
    return None, first_reason or "that did not come out right"
