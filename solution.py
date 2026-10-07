"""A worked solution, one step under another, with what was done written beside it.

"Solve this" deserves more than the answer read out once. A child who could
not do it needs to SEE how: each line under the last, the reason next to it,
the answer in a box at the end. So the model sends the working as structure --

    [ACTION: show_visual:solution | Solve 2x + 3 = 7;
     2x + 3 = 7 :: the equation; 2x = 7 - 3 = 4 :: take 3 from both sides;
     x = \\frac{4}{2} = 2 :: divide both sides by 2; answer = x = 2;
     see = graph: y = 2x + 3]

-- and it is set here. The maths by mathtext, a chemistry step through
science.py so H2O keeps its subscripts, the words in whichever script they came
in. One parse, two layouts:

    board_image()   the whole solution as one picture, for the Transcribe Board
                    and its enlarged view -- a kind in visuals.KINDS like any other.
    Panel           the steps as a column beside the student's own writing on
                    the full-screen board, with the step she is saying lit up.

`see` is the one thing worth trying afterwards: the line on a graph with its
numbers on sliders, the projectile with the problem's own angle and speed, the
balanced reaction. It is NOT drawn here. The screen offers it as a button, and
the tap hands it to show_visual like any other tag.

NOTHING THE MODEL SENDS IS EVALUATED, same rule as visuals.py: the maths is
typeset, never computed.

A leaf, like science.py. It reaches visuals' fonts at CALL time (`import
visuals` inside the functions), because visuals imports this module to
register the kind.
"""

import io
import re
import threading

from PIL import Image, ImageDraw

import science

INK = "#1E2233"
INK_DIM = "#5A6478"
ACCENT = "#4F46E5"
ACCENT_SOFT = "#E0E7FF"
LIT = "#EEF2FF"
GOOD = "#15803D"
GOOD_SOFT = "#F0FDF4"
RULE = "#D8E0F0"

# More than this many steps is not working a child can follow, and the board
# has nowhere to put them.
MAX_STEPS = 10

RE_ANSWER = re.compile(r'^\s*(?:final\s+answer|answer|ans|result|उत्तर|जवाब)\s*[:=]\s*(.+)$',
                       re.IGNORECASE | re.DOTALL)
RE_SEE = re.compile(r'^\s*(?:see|try|try\s+it|visual|देखो)\s*[:=]\s*(.+)$',
                    re.IGNORECASE | re.DOTALL)
RE_TITLE = re.compile(r'^\s*(?:title|problem|question)\s*[:=]\s*(.+)$',
                      re.IGNORECASE | re.DOTALL)
# "Step 2:", "2.", "2)" -- the model numbers the steps now and then, and the
# board numbers them itself.
RE_NUMBERING = re.compile(r'^\s*(?:step\s*\d+\s*[:.)\-]?|चरण\s*\d+\s*[:.)\-]?|\d{1,2}\s*[.)]\s+)',
                          re.IGNORECASE)
# What `see` may ask for. Anything else is dropped rather than guessed at: a
# button that opens the wrong thing is worse than no button.
SEE_KINDS = {"graph", "plot", "model3d", "3d", "simulation", "reaction",
             "molecule", "number_line", "forces", "circuit", "equation", "shape",
             "angle", "picture", "cycle", "steps"}
# Simulations rather than models, for the button's wording.


def has_devanagari(text):
    return any("ऀ" <= ch <= "ॿ" for ch in text or "")


# ---------------------------------------------------------------------------
# reading the tag
# ---------------------------------------------------------------------------
class Step:
    """One line of working: the maths as written, and what was done."""

    def __init__(self, raw, note=""):
        self.raw = RE_NUMBERING.sub("", (raw or "").strip()).strip()
        self.note = RE_NUMBERING.sub("", (note or "").strip()).strip()

    @property
    def words_only(self):
        return not self.raw

    def label(self):
        """A few words for the pips under the board picture."""
        return self.note or _plain(self.raw)


class Solution:
    def __init__(self, title, steps, answer=None, see=None):
        self.title = title
        self.steps = steps
        self.answer = answer
        self.see = see          # (kind, payload, [(setting, value), ...]) or None

    @property
    def hindi(self):
        return any(has_devanagari(t) for t in
                   [self.title] + [s.note for s in self.steps])


def _looks_like_maths(part):
    """Maths, or words? "x = 2" and "H2 + O2 -> H2O" are maths; "count the
    atoms on each side" is words even with a 2 in it."""
    if has_devanagari(part) and not re.search(r'[=\\^_<>]', part):
        return False
    return not science._is_title(part)


# The first part is the title -- "Solve 2x + 3 = 7", "Balance H2 + O2 -> H2O",
# "2x + 3 = 7 का हल" -- whenever it opens with a word rather than with the maths
# itself. A function name is not a word here: "sin x = 1/2" is maths.
RE_OPENS_WITH_WORD = re.compile(r'^\s*(?:(?!(?:sin|cos|tan|cot|sec|log|ln|exp|sqrt|lim)\b)'
                                r'[A-Za-z]{3,}|.*[ऀ-ॿ])', re.IGNORECASE)


def _see(text):
    """("graph", "y = 2x + 3", []) out of "graph: y = 2x + 3", or None.

    Inside a solution the parts are already split on ";", so a see payload
    writes its own parts with commas: "model3d: projectile motion, angle = 30,
    speed = 20". For a 3D model the name=value pairs are SETTINGS, carried out
    once the simulation is up; for everything else the commas become the
    semicolons that kind's own payload expects."""
    kind, sep, payload = (text or "").partition(":")
    kind = re.sub(r'[\s-]+', "_", kind.strip().lower())
    if not sep or not payload.strip() or kind not in SEE_KINDS:
        return None
    kind = {"plot": "graph", "3d": "model3d", "simulation": "model3d"}.get(kind, kind)
    pieces = [p.strip() for p in re.split(r"[,;]", payload) if p.strip()]
    if kind == "model3d":
        import models3d
        if models3d.find_solid(pieces[0]) is not None:
            # A solid's measurements are part of WHAT to draw ("Cube; side =
            # 4 cm"), not settings to send a running simulation afterwards.
            return kind, "; ".join(pieces), []
        settings = []
        for piece in pieces[1:]:
            name, sep, value = piece.partition("=")
            value = re.sub(r'[^\d.\-]', "", value)
            if sep and name.strip() and value:
                settings.append((name.strip().lower(), value))
        return kind, pieces[0], settings
    # "y = m*x + c, m=2, c=1" is the graph payload "y = m*x + c; m=2; c=1". No
    # formula the plotter takes has a comma of its own in it.
    return kind, "; ".join(pieces), []


def parse(payload):
    """A Solution, or None when there is no working in it at all."""
    import visuals as v
    payload = v._repair_latex(payload or "")
    title, steps, answer, see = "", [], None, None
    parts = science._parts(payload)
    index = -1
    while index + 1 < len(parts):
        index += 1
        part = parts[index]
        match = RE_SEE.match(part)
        if match:
            # `see` is the last thing, and its own payload may have parts:
            # "see = forces: Box; left = 4 N; right = 10 N". Everything after
            # it belongs to it -- read as steps, those came out as a stray
            # "left = 4 N" at the foot of the working -- up to an answer the
            # model put after it anyway.
            rest = [match.group(1)]
            while index + 1 < len(parts) and not RE_ANSWER.match(parts[index + 1]):
                index += 1
                rest.append(parts[index])
            see = _see("; ".join(rest)) or see
            continue
        match = RE_ANSWER.match(part)
        if match:
            raw, _, note = match.group(1).partition("::")
            if _looks_like_maths(raw):
                answer = Step(raw, note)
            else:
                answer = Step("", raw)
            continue
        match = RE_TITLE.match(part)
        if match and not title:
            title = match.group(1).strip()
            continue
        if "::" in part:
            raw, _, note = part.partition("::")
            steps.append(Step(raw, note) if _looks_like_maths(raw) or not raw.strip()
                          else Step("", f"{raw.strip()} -- {note.strip()}".strip(" -")))
            continue
        if index == 0 and not title and (not _looks_like_maths(part)
                                         or RE_OPENS_WITH_WORD.match(part)):
            title = RE_NUMBERING.sub("", part).strip()
            continue
        steps.append(Step(part) if _looks_like_maths(part) else Step("", part))
    steps = [s for s in steps if s.raw or s.note][:MAX_STEPS]
    if not steps and answer is None:
        return None
    return Solution(title or "Step by step", steps, answer, see)


def describe(solution):
    """The working, for ON_BOARD: what the student can see, step by step."""
    lines = [f"{i}) {_plain(s.raw)}" + (f" ({s.note})" if s.note and s.raw else s.note)
             for i, s in enumerate(solution.steps, 1)]
    text = f"a worked solution of {solution.title}: " + "; ".join(lines)
    if solution.answer is not None:
        text += f"; answer: {_plain(solution.answer.raw) or solution.answer.note}"
    return text[:900]


_SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
_SUPERSCRIPT = str.maketrans("0123456789-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


def pretty_title(solution):
    """The title as it should read. A chemistry title keeps its formulas'
    subscripts and gets a real arrow: "Balance H₂ + O₂ → H₂O", not "H2 + O2 ->
    H2O", which is how the model has to type it; a power is raised, x², not
    x^2."""
    title = solution.title.replace("<=>", "⇌")
    chemistry = any(s.raw and science.looks_like_reaction(s.raw) for s in solution.steps)
    title = re.sub(r'\s*(?:-+>|=>)\s*', " → ", title)
    title = re.sub(r'\^\{?(-?\d+)\}?', lambda m: m.group(1).translate(_SUPERSCRIPT), title)
    title = re.sub(r'(?<=\d)\s*\*\s*(?=\d)', " × ", title)
    if chemistry:
        # A digit straight after an element symbol or a bracket is a subscript;
        # a coefficient stands before its formula and is left alone.
        title = re.sub(r'(?<=[A-Za-z)\]])(\d+)', lambda m: m.group(1).translate(_SUBSCRIPT),
                       title)
    return title


def step_labels(solution):
    """The pips under the board picture: one per step, then the answer."""
    labels = [s.label()[:60] for s in solution.steps]
    if solution.answer is not None:
        labels.append(("उत्तर: " if solution.hindi else "Answer: ")
                      + (_plain(solution.answer.raw) or solution.answer.note)[:50])
    return labels


def see_label(solution):
    """The wording on the button that opens `see`, or "" when there is none."""
    if not solution.see:
        return ""
    import visuals as v
    kind, payload, _ = solution.see
    return v.offer_label(kind, payload, solution.hindi)[0]


# ---------------------------------------------------------------------------
# what she says, matched to a step
# ---------------------------------------------------------------------------
# Too common to say which step a sentence is about.
_STOPWORDS = {"the", "and", "of", "a", "an", "in", "to", "it", "is", "its",
              "with", "into", "then", "from", "for", "on", "are", "was", "this",
              "that", "so", "we", "now", "you", "get", "gets", "our", "both",
              "sides", "side", "equals", "equal", "which", "what", "here", "next",
              "है", "को", "से", "में", "और", "का", "की", "के", "तो", "अब", "हम", "यह", "ये"}
RE_TOKEN = re.compile(r'[^\s.,;:!?।()\[\]{}=+\-*/^\\|"\'`]+')


def tokens(text):
    """The words and numbers in `text` that could tell one step from another."""
    found = set()
    for token in RE_TOKEN.findall((text or "").lower()):
        token = token.strip("_")
        if token in _STOPWORDS or token.startswith(("frac", "mathrm", "text", "times")):
            continue
        if token.isdigit() or len(token) >= 3 or has_devanagari(token):
            found.add(token)
    return found


def step_tokens(solution):
    """One set per step, then one for the answer -- the order Panel lights."""
    rows = [tokens(f"{s.note} {_plain(s.raw)}") for s in solution.steps]
    if solution.answer is not None:
        rows.append(tokens(f"{solution.answer.note} {_plain(solution.answer.raw)}")
                    | {"answer", "उत्तर", "जवाब"})
    return rows


def match_step(rows, current, line):
    """Which step the spoken `line` is about, or `current` when it is no
    clearer than that.

    Ties go FORWARD, because a worked solution is explained in order: "take 3
    from both sides, so 2x is 4" names the numbers of two steps, and the one
    she is arriving at is the one worth lighting. Going back takes a clear
    win, so a sentence that mentions the start again does not drag the light
    back to step one."""
    said = tokens(line)
    if not said or not rows:
        return current
    best, best_score = current, 0.0
    for index, row in enumerate(rows):
        score = float(len(said & row))
        if index == current + 1:
            score += 0.6
        elif index < current:
            score -= 0.8
        elif index == current:
            score += 0.2
        if score > best_score:
            best, best_score = index, score
    return best if best_score >= 1.6 else current


# ---------------------------------------------------------------------------
# setting the maths
# ---------------------------------------------------------------------------
def _plain(raw):
    """Maths as a person would read it off the board, for the model and the
    pips: no backslashes, no braces."""
    text = raw or ""
    text = re.sub(r'\\(?:boxed|cancel|bcancel|xcancel|underline|fbox)\s*\{([^{}]*)\}',
                  r'\1', text)
    text = re.sub(r'\^\s*\{?\\circ\}?', "°", text)
    text = re.sub(r'\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'(\1)/(\2)', text)
    text = re.sub(r'\\sqrt\s*\{([^{}]*)\}', r'√(\1)', text)
    text = re.sub(r'\\(?:mathrm|text|mathbf|operatorname)\s*\{([^{}]*)\}', r'\1', text)
    text = text.replace(r"\times", "×").replace(r"\cdot", "·").replace(r"\div", "÷")
    text = text.replace(r"\approx", "≈").replace(r"\pm", "±").replace(r"\circ", "°")
    text = text.replace(r"\rightarrow", "→").replace(r"\to", "→").replace("->", "→")
    text = re.sub(r'\\([A-Za-z]+)', r'\1', text)
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r'\s+', " ", text).strip()


# Words in maths. mathtext sets "Force left = 4 N" as one italic word,
# "Forceleft=4N", because a space means nothing in maths -- and the model writes
# exactly that kind of step for physics. So a word is set upright and keeps
# the space before the next one; a unit after a number gets its gap; and a
# function or a Greek letter written out becomes the real thing.
_FUNCTIONS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp", "lim",
              "max", "min", "det", "gcd"}
_GREEK = {"alpha", "beta", "gamma", "delta", "theta", "lambda", "mu", "pi", "rho",
          "sigma", "tau", "phi", "omega", "epsilon", "eta", "nu", "Delta", "Omega",
          "Sigma", "Theta", "Lambda", "Phi", "Gamma"}
_UNITS = (r"(?:kg|mg|km|cm|mm|nm|ms|min|kN|kJ|kW|mV|mA|kPa|Pa|Hz|kHz|mol|mL|ml|"
          r"cal|eV|dB|rad|N|m|s|g|h|J|W|V|A|K|L|C|T|Ω)")
# But "at" in v = u + at is a times t, and "mgh" is three quantities: a short
# run of letters is maths unless it is one of these, and a longer one is a
# word only if it has a vowel to say it with.
_SHORT_WORDS = {"or", "is", "of", "to", "in", "on", "by", "if", "so", "as", "an",
                "no", "it", "be", "we"}
RE_WORD = re.compile(r'(?<![\\A-Za-z])([A-Za-z]{2,})(?![A-Za-z])')
RE_UNIT = re.compile(rf'(?<=\d)\s+({_UNITS})(?![A-Za-z])')
RE_KEEP = re.compile(r'\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{[^{}]*\}')


def _words_upright(latex):
    kept = []

    def keep(match):
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"
    latex = RE_KEEP.sub(keep, latex)
    latex = RE_UNIT.sub(lambda m: "\\ \x01" + m.group(1) + "\x02", latex)

    def word(match):
        text = match.group(1)
        if text in _FUNCTIONS or text in _GREEK:
            return "\\" + text + " "
        if (text.lower() in _SHORT_WORDS
                or (len(text) >= 3 and re.search(r'[aeiouyAEIOUY]', text))):
            return "\x01" + text + "\x02"
        return text
    latex = RE_WORD.sub(word, latex)
    # A space beside a word is a real space -- "Force left", "2 or x". Nowhere
    # else: after \sin or \approx, mathtext's own spacing is the right one.
    latex = re.sub(r'(?<=\x02)\s+(?=[\w\x01\\])|(?<=[\w}])\s+(?=\x01)', r'\\ ', latex)
    latex = latex.replace("\x01", r"\mathrm{").replace("\x02", "}")
    return re.sub(r'\x00(\d+)\x00', lambda m: kept[int(m.group(1))], latex)


def _mathtext(raw):
    """`raw` as mathtext that will set, or None for words."""
    text = (raw or "").strip()
    if not text or has_devanagari(text):
        return None
    try:
        if science.looks_like_reaction(text):
            return " ".join(science.Reaction(text).latex_lines())
    except ValueError:
        pass
    formula = science._formula_line(text)
    if formula:
        return formula
    latex = science._clean_latex(text)
    # What mathtext cannot set, unwrapped to what it can: a box or a
    # crossing-out keeps its contents, a colour is dropped.
    latex = re.sub(r'\\color\s*\{[^}]*\}', '', latex)
    latex = re.sub(r'\\(?:boxed|cancel|bcancel|xcancel|underline|fbox|bm|boldsymbol)\s*\{',
                   '{', latex)
    latex = re.sub(r'\\textbf\s*\{', r'\\mathbf{', latex)
    latex = re.sub(r'\\operatorname\s*\{', r'\\mathrm{', latex)
    latex = latex.replace(r"\implies", r"\Rightarrow").replace(r"\iff", r"\Leftrightarrow")
    latex = re.sub(r'\\q?quad\b', r'\\ \\ ', latex)
    return _words_upright(latex)


# pyplot keeps global state and is not safe across threads, and a solution can
# be set on ai_loop's thread while a "See it" tap is drawing on another. So the
# figures here are made directly, and one at a time.
_mpl_lock = threading.Lock()
_math_cache = {}


def math_image(latex, size, colour=INK):
    """One line of mathtext as a tight transparent image, or None."""
    key = (latex, size, colour)
    if key in _math_cache:
        return _math_cache[key]
    image = None
    with _mpl_lock:
        try:
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.figure import Figure
            figure = Figure(figsize=(0.01, 0.01), dpi=100)
            FigureCanvasAgg(figure)
            figure.text(0, 0, f"${latex}$", fontsize=size, color=colour)
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", transparent=True,
                           bbox_inches="tight", pad_inches=0.04)
            buffer.seek(0)
            image = Image.open(buffer).convert("RGBA")
        except Exception as exc:
            first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            print(f"[SOLUTION] Could not set {latex!r}: {first}", flush=True)
    if len(_math_cache) > 400:
        _math_cache.clear()
    _math_cache[key] = image
    return image


def text_image(text, size, colour=INK, bold=False, width=None, max_lines=None):
    """Words as a transparent image, wrapped to `width`, in a face that can
    draw them -- Devanagari included. None for nothing to draw."""
    import visuals as v
    text = (text or "").strip()
    if not text:
        return None
    font = v._script_font(text, size, bold)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = v._wrap(probe, text, font, width) if width else [text]
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;") + "…"
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + 2
    widest = max(int(font.getlength(line)) for line in lines) + 4
    image = Image.new("RGBA", (max(1, widest), line_h * len(lines) + 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for row, line in enumerate(lines):
        draw.text((1, row * line_h), line, font=font, fill=colour)
    return image


def _fit(image, width):
    """Shrink an image to `width` if it is wider; never enlarge it."""
    if image is None or image.width <= width:
        return image
    height = max(1, round(image.height * width / image.width))
    return image.resize((width, height), Image.LANCZOS)


def maths_or_words(raw, size, colour=INK, width=None):
    """The step's maths set as maths -- or, when it will not set, as words,
    so a step is never simply missing from the board."""
    latex = _mathtext(raw)
    image = math_image(latex, size, colour) if latex else None
    if image is None:
        image = text_image(_plain(raw) if latex else raw, int(size * 1.35),
                           colour, width=width)
    return _fit(image, width) if width else image


# ---------------------------------------------------------------------------
# the column beside their writing
# ---------------------------------------------------------------------------
class Panel:
    """The steps as a column `width` pixels wide, for the full-screen board.

    Everything slow -- mathtext above all -- happens here, in the constructor,
    on whichever thread made it. image() only arranges what is already set,
    which is what lets the screen relight a step as she speaks without
    stalling a frame."""

    GUTTER = 36          # the step number's column
    PAD_Y = 5
    GAP = 4              # between one step and the next

    def __init__(self, solution, width):
        self.solution = solution
        self.width = width
        self.see = solution.see
        self.see_text = see_label(solution)
        self.tokens = step_tokens(solution)
        room = width - self.GUTTER - 12
        self.title = text_image(pretty_title(solution), 15, ACCENT, bold=True,
                                width=width - 64, max_lines=2)
        self.rows = []
        for step in solution.steps:
            maths = maths_or_words(step.raw, 14, INK, room) if step.raw else None
            note = text_image(step.note, 12 if maths is not None else 14,
                              INK_DIM if maths is not None else INK, width=room)
            self.rows.append((maths, note))
        self.answer = None
        if solution.answer is not None:
            answer = solution.answer
            label = text_image("उत्तर" if solution.hindi else "Answer", 12, GOOD,
                               bold=True)
            # The label beside the result rather than over it: the answer is
            # the line most worth keeping in view without scrolling.
            room_after = room + self.GUTTER - 30 - label.width
            maths = (maths_or_words(answer.raw, 16, INK, room_after) if answer.raw
                     else text_image(answer.note, 15, INK, bold=True, width=room_after))
            note = (text_image(answer.note, 12, INK_DIM, width=room - 4)
                    if answer.raw and answer.note else None)
            self.answer = (label, maths, note)

    def count(self):
        """How many things can be lit: the steps, then the answer."""
        return len(self.rows) + (1 if self.answer else 0)

    def image(self, current=-1):
        """(RGBA column, [(top, bottom) of each step, then of the answer])."""
        pieces = []
        for index, (maths, note) in enumerate(self.rows):
            content = [p for p in (maths, note) if p is not None]
            height = max(28, sum(p.height for p in content) + 2 * (len(content) - 1))
            pieces.append(("step", index, content, height + self.PAD_Y * 2))
        if self.answer:
            label, maths, note = self.answer
            line = max(label.height, maths.height if maths is not None else 0)
            pieces.append(("answer", len(self.rows), self.answer,
                           line + (note.height + 4 if note is not None else 0) + 16))
        total = sum(height for *_, height in pieces) + self.GAP * len(pieces) + 8
        column = Image.new("RGBA", (self.width, max(total, 10)), (255, 255, 255, 255))
        draw = ImageDraw.Draw(column)
        number_font = _number_font()
        y, spans = 4, []
        for kind, index, content, height in pieces:
            lit = index == current
            if kind == "step":
                if lit:
                    draw.rounded_rectangle((2, y, self.width - 3, y + height), 10,
                                           fill=LIT, outline="#C7D2FE")
                    draw.rounded_rectangle((2, y + 6, 6, y + height - 6), 2, fill=ACCENT)
                cx, cy, r = self.GUTTER / 2 + 2, y + self.PAD_Y + 12, 11
                draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                             fill=ACCENT if lit else ACCENT_SOFT)
                number = str(index + 1)
                w = draw.textlength(number, font=number_font)
                draw.text((cx - w / 2, cy - 8), number, font=number_font,
                          fill="#FFFFFF" if lit else ACCENT)
                py = y + self.PAD_Y
                for piece in content:
                    column.alpha_composite(piece, (self.GUTTER + 2, int(py)))
                    py += piece.height + 2
            else:
                label, maths, note = content
                box = (4, y + 2, self.width - 5, y + height - 2)
                draw.rounded_rectangle(box, 10, fill=GOOD_SOFT,
                                       outline=GOOD if lit else "#86EFAC",
                                       width=3 if lit else 2)
                line = max(label.height, maths.height if maths is not None else 0)
                column.alpha_composite(label, (16, int(y + 8 + (line - label.height) / 2)))
                if maths is not None:
                    column.alpha_composite(maths, (26 + label.width,
                                                   int(y + 8 + (line - maths.height) / 2)))
                if note is not None:
                    column.alpha_composite(note, (16, int(y + 12 + line)))
            spans.append((y, y + height))
            y += height + self.GAP
        return column, spans


_number_fonts = {}


def _number_font():
    import visuals as v
    if "n" not in _number_fonts:
        _number_fonts["n"] = v._font(14, True)
    return _number_fonts["n"]


# ---------------------------------------------------------------------------
# the whole solution as one picture, for the Transcribe Board
# ---------------------------------------------------------------------------
def board_image(payload):
    """[ACTION: show_visual:solution | ...] as a 760x420 picture -- visuals.KINDS.

    The maths down the left, what was done beside it on the right, the answer
    boxed underneath. Sizes come down together until every step fits: a long
    solution set small is still one the student can enlarge and read, and one
    with its last steps cut off is not a solution at all."""
    import visuals as v
    solution = parse(payload)
    if solution is None:
        return None, "there was no working to write out"
    W, H = v.RENDER_W, v.RENDER_H
    for maths_size, note_size, title_size in ((24, 18, 26), (21, 16, 24), (18, 15, 22),
                                              (16, 14, 20), (14, 13, 18)):
        image = Image.new("RGB", (W, H), v.PAPER)
        draw = ImageDraw.Draw(image)
        title = text_image(pretty_title(solution), title_size, v.ACCENT, bold=True,
                           width=W - 80, max_lines=2)
        y = 14
        if title is not None:
            image.paste(title, (int((W - title.width) / 2), y), title)
            y += title.height + 6
        draw.line([(40, y), (W - 40, y)], fill=v.RULE, width=2)
        y += 10
        # The maths gets the room it needs, up to two thirds of the width,
        # and the words what is left -- measured, so a long reaction never
        # runs into the reason written beside it.
        maths_all = [maths_or_words(s.raw, maths_size, INK, int(W * 0.66) - 96)
                     if s.raw else None for s in solution.steps]
        widest = max((m.width for m in maths_all if m is not None), default=0)
        split = int(min(W * 0.66, max(W * 0.42, 80 + widest + 24)))
        rows = []
        for step, maths in zip(solution.steps, maths_all):
            if maths is not None:
                note = text_image(step.note, note_size, INK_DIM, width=W - split - 30)
            else:
                note = text_image(step.note, note_size + 1, INK, width=W - 110)
            rows.append((maths, note))
        answer = None
        if solution.answer is not None:
            a = solution.answer
            answer = (maths_or_words(a.raw, maths_size + 2, INK, W - 220) if a.raw
                      else text_image(a.note, note_size + 2, INK, bold=True, width=W - 220))
        heights = [max(30, max((p.height for p in r if p is not None), default=0)) + 8
                   for r in rows]
        need = sum(heights) + ((answer.height + 26) if answer is not None else 0)
        if y + need <= H - 10 or maths_size == 14:
            break
    number_font = v._font(max(14, note_size), True)
    for index, ((maths, note), height) in enumerate(zip(rows, heights)):
        if y + height > H - ((answer.height + 26) if answer is not None else 6):
            break
        cx, cy, r = 54, y + min(height, 34) / 2, 13
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT_SOFT)
        number = str(index + 1)
        w = draw.textlength(number, font=number_font)
        draw.text((cx - w / 2, cy - number_font.size * 0.6), number,
                  font=number_font, fill=ACCENT)
        if maths is not None:
            image.paste(maths, (80, int(y + (height - 8 - maths.height) / 2)), maths)
            if note is not None:
                image.paste(note, (split, int(y + (height - 8 - note.height) / 2)), note)
        elif note is not None:
            image.paste(note, (80, int(y + (height - 8 - note.height) / 2)), note)
        y += height
    if answer is not None:
        label = text_image("उत्तर" if solution.hindi else "Answer", 18, GOOD, bold=True)
        box_w = min(W - 80, answer.width + label.width + 64)
        x0, y0 = (W - box_w) / 2, min(y + 4, H - answer.height - 22)
        draw.rounded_rectangle((x0, y0, x0 + box_w, y0 + answer.height + 16), 14,
                               fill=GOOD_SOFT, outline=GOOD, width=3)
        image.paste(label, (int(x0 + 18), int(y0 + 8 + (answer.height - label.height) / 2)),
                    label)
        image.paste(answer, (int(x0 + 36 + label.width), int(y0 + 8)), answer)
    return image, ""
