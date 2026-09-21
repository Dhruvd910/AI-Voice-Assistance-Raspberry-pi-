"""Chemistry and physics on the board: reactions, molecules, equations that run
to more than one line, force diagrams and circuits.

WHY THESE ARE DRAWN HERE AND NEVER GENERATED
    Before this file, a chemical reaction had no kind of its own. It fell
    through to `picture`, an AI image model drew "2H2 + O2 -> 2H2O", and what
    came back showed water turning INTO hydrogen and oxygen -- the reaction
    backwards, looking completely confident. Written as an `equation` instead,
    mathtext set it as "2H2 + O2 − > 2H2O": italic, no subscripts, a broken
    arrow. Chemistry and circuit diagrams are exactly the kinds a picture can be
    wrong about in a way the student cannot see, so every one of them is drawn
    from its structure, here, and none of them is ever sent to Seedream.

NOTHING THE MODEL SENDS IS EVALUATED, same rule as visuals.py. Formulas are
parsed against the periodic table, SMILES are read by RDKit, and a name RDKit
cannot read is looked up (a local table first, PubChem second).

A leaf like visuals.py. It reaches visuals' drawing helpers at CALL time
(`import visuals` inside the functions), because visuals imports this module to
register the kinds -- the same foot-of-the-cycle arrangement ARCHITECTURE.md
describes, done per function since there is nothing here to bind at import.
"""

import io
import math
import re
import urllib.parse
import urllib.request

from PIL import Image

# ---------------------------------------------------------------------------
# shared: splitting payloads, and setting maths as an image
# ---------------------------------------------------------------------------
def _parts(payload):
    """Split on ; and newlines, but never inside {...} or [...] -- a LaTeX
    argument or a reaction condition can legitimately hold either."""
    parts, depth, current = [], 0, ""
    for char in payload or "":
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        if char in ";\n" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def _math_image(latex, size, colour):
    """One line of mathtext as a tight RGB image, or None if it will not parse."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import visuals as v
    figure = plt.figure(figsize=(0.01, 0.01), dpi=100)
    try:
        figure.text(0, 0, f"${latex}$", fontsize=size, color=colour)
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", facecolor=v.PAPER,
                       bbox_inches="tight", pad_inches=0.06)
    except Exception as exc:
        print(f"[VISUAL] Could not set {latex!r}: {str(exc).splitlines()[0] if str(exc) else exc}",
              flush=True)
        return None
    finally:
        plt.close(figure)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def _stack(image, top, lines, bottom_room=24, colours=None, sizes=(46, 40, 34, 29, 25, 22, 19)):
    """Set each line of mathtext and stack them centred under `top`.

    The largest size at which EVERY line fits both the width and, together,
    the height. Returns False if even the smallest will not fit, so the caller
    can try splitting a line before giving up.
    """
    import visuals as v
    width = v.RENDER_W - 70
    height = v.RENDER_H - top - bottom_room
    for size in sizes:
        rendered = [_math_image(line, size, (colours or {}).get(i, v.INK))
                    for i, line in enumerate(lines)]
        if any(r is None for r in rendered):
            return None
        gap = max(8, size // 3)
        total = sum(r.height for r in rendered) + gap * (len(rendered) - 1)
        if max(r.width for r in rendered) <= width and total <= height:
            y = top + (height - total) / 2
            for r in rendered:
                image.paste(r, (int((v.RENDER_W - r.width) / 2), int(y)))
                y += r.height + gap
            return True
    return False


def _chip(draw, text, y, colour):
    """A small rounded label centred at y, e.g. "Balanced"."""
    import visuals as v
    font = v._font(20)
    w, h = v._text_size(draw, text, font)
    x0 = (v.RENDER_W - w) / 2 - 16
    draw.rounded_rectangle([x0, y, x0 + w + 32, y + h + 14], 14, fill=colour)
    draw.text((x0 + 16, y + 5), text, font=font, fill="#FFFFFF")


# ---------------------------------------------------------------------------
# chemical formulas
# ---------------------------------------------------------------------------
ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr
Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb
Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt
Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr""".split())

RE_TOKEN = re.compile(r'([A-Z][a-z]?|\(|\)|\[|\]|\d+)')
RE_STATE = re.compile(r'\s*(\((?:s|l|g|aq)\))\s*$', re.IGNORECASE)
RE_GAS_OR_SOLID = re.compile(r'\s*(↑|↓|\\uparrow|\\downarrow)\s*$')
RE_COEFFICIENT = re.compile(r'^(\d+/\d+|\d*\.\d+|\d+)\s*(?=[A-Z(\[]|e\b|e\^|e-)')
# A charge at the end. Written with ^ it is unambiguous: SO4^2-, SO4^{2-}, Fe^3+.
RE_CHARGE = re.compile(r'\^\{?(\d*[+-]|[+-]\d*)\}?$')
# Written bare it is not: Fe3+ is iron(III), but NH4+ is ammonium with a
# subscript 4. So a bare digit is the charge only on a lone element.
RE_BARE_CHARGE = re.compile(r'(?<=[A-Za-z0-9\])])(\d?)([+-])$')

# Arrows, longest spelling first so "<=>" is not read as "=" and "->".
# The condition an arrow may carry ("->[heat]", "\xrightarrow{heat}") is pulled
# off separately; see _split_arrow.
RE_ARROW = re.compile(
    r'\s*(<=>|<-->|<->|⇌|⇄|\\rightleftharpoons|\\leftrightharpoons|\\rightleftarrows'
    r'|\\xrightarrow|\\longrightarrow|\\rightarrow|\\to(?![a-z])|-->|->|→|⟶|=>'
    r'|(?<![<>=!])=(?![=>]))\s*')
REVERSIBLE = {"<=>", "<-->", "<->", "⇌", "⇄", r"\rightleftharpoons",
              r"\leftrightharpoons", r"\rightleftarrows"}


def _formula_tokens(text):
    """Tokens of a formula like Ca(OH)2, or None if it is not one."""
    tokens = RE_TOKEN.findall(text)
    if "".join(tokens) != text or not tokens:
        return None
    if tokens[0].isdigit():
        return None
    depth = 0
    for index, token in enumerate(tokens):
        if token in "([":
            depth += 1
        elif token in ")]":
            depth -= 1
            if depth < 0:
                return None
        elif token.isdigit():
            # A count belongs to the element or bracket just before it.
            if tokens[index - 1].isdigit() or tokens[index - 1] in "([":
                return None
        elif token not in ELEMENTS:
            return None
    return tokens if depth == 0 else None


def _count_atoms(tokens):
    """{element: count} for one formula's tokens, brackets multiplied out."""
    stack = [{}]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        count = 1
        if i < len(tokens) and tokens[i].isdigit():
            count = int(tokens[i])
            i += 1
        if token in "([":
            stack.append({})
        elif token in ")]":
            group = stack.pop()
            for element, n in group.items():
                stack[-1][element] = stack[-1].get(element, 0) + n * count
        else:
            stack[-1][token] = stack[-1].get(token, 0) + count
    return stack[0]


def _escape_words(text):
    """Plain words for \\mathrm{}: spaces kept, TeX specials neutralised."""
    text = re.sub(r'[\\{}$^_%&#~]', ' ', text).strip()
    return re.sub(r'\s+', r'\\ ', text)


class Species:
    """One term of a reaction: 2 CuSO4·5H2O (aq), or a word like Water."""

    def __init__(self, text):
        self.raw = text.strip()
        rest = self.raw
        self.mark = ""
        mark = RE_GAS_OR_SOLID.search(rest)
        if mark:
            self.mark = r"\uparrow" if mark.group(1) in ("↑", r"\uparrow") else r"\downarrow"
            rest = rest[:mark.start()]
        state = RE_STATE.search(rest)
        self.state = state.group(1).lower() if state else ""
        if state:
            rest = rest[:state.start()]
        coefficient = RE_COEFFICIENT.match(rest)
        self.coefficient = coefficient.group(1) if coefficient else ""
        if coefficient:
            rest = rest[coefficient.end():]
        rest = rest.strip()
        # An electron: e-, e^-, e^{-}.
        self.electron = bool(re.fullmatch(r'e\^?\{?-\}?', rest))
        self.charge = ""
        if not self.electron:
            charge = RE_CHARGE.search(rest)
            bare = None if charge else RE_BARE_CHARGE.search(rest)
            if charge:
                self.charge = charge.group(1)
                rest = rest[:charge.start()]
            elif bare:
                body = rest[:bare.start()]
                if bare.group(1) and re.fullmatch(r'[A-Z][a-z]?', body):
                    self.charge = bare.group(1) + bare.group(2)
                    rest = body
                else:
                    self.charge = bare.group(2)
                    rest = body + bare.group(1)
            # "+3" and "3+" mean the same; sign last, the way it is printed.
            if self.charge[:1] in ("+", "-") and len(self.charge) > 1:
                self.charge = self.charge[1:] + self.charge[0]
        # Hydrates: CuSO4·5H2O, CuSO4.5H2O, CuSO4*5H2O.
        self.pieces = []
        for piece in re.split(r'\s*[·•∙*.]\s*(?=\d*[A-Z(])', rest):
            count = re.match(r'^(\d+)(?=[A-Z(\[])', piece)
            multiplier = int(count.group(1)) if count else 1
            body = piece[count.end():] if count else piece
            tokens = _formula_tokens(body)
            if tokens is None:
                self.pieces = None
                break
            self.pieces.append((multiplier, tokens))
        self.is_formula = bool(self.pieces) or self.electron
        self.word = "" if self.is_formula else rest

    def atoms(self):
        total = {}
        for multiplier, tokens in self.pieces or []:
            for element, n in _count_atoms(tokens).items():
                total[element] = total.get(element, 0) + n * multiplier
        return total

    def charge_value(self):
        if self.electron:
            return -1
        if not self.charge:
            return 0
        magnitude = int(self.charge[:-1] or 1)
        return magnitude if self.charge[-1] == "+" else -magnitude

    def coefficient_value(self):
        text = self.coefficient or "1"
        if "/" in text:
            top, bottom = text.split("/")
            return int(top) / int(bottom)
        return float(text)

    def latex(self):
        out = ""
        if self.coefficient:
            if "/" in self.coefficient:
                top, bottom = self.coefficient.split("/")
                out += rf"\frac{{{top}}}{{{bottom}}}\,"
            else:
                out += self.coefficient + r"\,"
        if self.electron:
            out += r"\mathrm{e}^{-}"
        elif self.is_formula:
            for index, (multiplier, tokens) in enumerate(self.pieces):
                if index:
                    out += r"\cdot "
                if multiplier != 1:
                    out += str(multiplier)
                for token in tokens:
                    out += f"_{{{token}}}" if token.isdigit() else rf"\mathrm{{{token}}}"
            if self.charge:
                out += f"^{{{self.charge}}}"
        else:
            out += rf"\mathrm{{{_escape_words(self.word)}}}"
        if self.state:
            out += rf"\,\mathrm{{{self.state}}}"
        if self.mark:
            out += self.mark
        return out


def _split_terms(side):
    """Terms of one side. "+" is a separator only between terms, never the
    charge on Na+ -- so it has to have space around it, or sit between two
    things that each look like a formula (H2+O2)."""
    side = side.strip()
    terms = re.split(r'\s+\+\s+', side)
    if len(terms) == 1 and "+" in side:
        terms = re.split(r'(?<=[A-Za-z0-9)\]])\+(?=\s*\d*\s*[A-Z(\[])', side)
    return [Species(t) for t in terms if t.strip()]


def _unwrap_ce(text):
    """\\ce{...} (mhchem) and $...$ are how a model writes chemistry in LaTeX;
    both are just packaging here."""
    text = text.strip().strip("$").strip()
    match = re.fullmatch(r'\\ce\s*\{(.*)\}', text, re.DOTALL)
    return match.group(1).strip() if match else text


def _split_arrow(text):
    """(left, arrow, above, below, right), or None when there is no arrow."""
    match = RE_ARROW.search(text)
    if not match:
        return None
    arrow = match.group(1)
    left, right = text[:match.start()], text[match.end():]
    above = below = ""
    if arrow == r"\xrightarrow":
        # \xrightarrow[below]{above}
        cond = re.match(r'\s*(?:\[([^\]]*)\])?\s*\{([^}]*)\}', right)
        if cond:
            below, above = cond.group(1) or "", cond.group(2)
            right = right[cond.end():]
    else:
        # mhchem: ->[above][below]
        cond = re.match(r'\s*\[([^\]]*)\](?:\s*\[([^\]]*)\])?', right)
        if cond:
            above, below = cond.group(1), cond.group(2) or ""
            right = right[cond.end():]
    return left, arrow, above.strip(), below.strip(), right


def _condition_latex(text):
    text = text.strip()
    if not text:
        return ""
    if text in ("Δ", "delta", r"\Delta", "heat"):
        return r"\Delta" if text != "heat" else r"\mathrm{heat}"
    # A formula as a catalyst (MnO2, H2SO4) keeps its subscripts.
    species = Species(text)
    if species.is_formula:
        return species.latex()
    return rf"\mathrm{{{_escape_words(text)}}}"


# Longer than this, a condition squeezes the arrow into a stub under a banner
# of text; it goes under the whole equation as "Conditions: ..." instead.
CONDITION_OVER_ARROW_MAX = 16


class Reaction:
    def __init__(self, text, above="", below=""):
        text = _unwrap_ce(text)
        pieces = _split_arrow(text)
        if pieces is None:
            raise ValueError("no arrow")
        left, arrow, arrow_above, arrow_below, right = pieces
        if RE_ARROW.search(right):
            raise ValueError("more than one arrow")
        self.reversible = arrow in REVERSIBLE
        self.above = arrow_above or above
        self.below = arrow_below or below
        self.left = _split_terms(left)
        self.right = _split_terms(right)
        if not self.left or not self.right:
            raise ValueError("a side is empty")

    @property
    def all_formulas(self):
        return all(s.is_formula for s in self.left + self.right)

    def balanced(self):
        """True/False when every term is a formula, None when it cannot tell."""
        if not self.all_formulas:
            return None
        def tally(side):
            atoms, charge = {}, 0.0
            for s in side:
                k = s.coefficient_value()
                for element, n in s.atoms().items():
                    atoms[element] = atoms.get(element, 0) + n * k
                charge += s.charge_value() * k
            return {e: round(n, 6) for e, n in atoms.items()}, round(charge, 6)
        return tally(self.left) == tally(self.right)

    def long_conditions(self):
        """Conditions too long to sit over an arrow ("high temperature, high
        pressure, iron catalyst"): written under the equation instead."""
        return [c for c in (self.above, self.below) if len(c) > CONDITION_OVER_ARROW_MAX]

    def arrow_latex(self):
        above, below = ((_condition_latex(c) if len(c) <= CONDITION_OVER_ARROW_MAX else "")
                        for c in (self.above, self.below))
        if self.reversible:
            arrow = r"\rightleftharpoons"
        else:
            # Long enough to sit under its condition: "sunlight" over a bare
            # \longrightarrow overhangs it on both sides. mathtext has no
            # \xrightarrow, so the shaft is extended with overlapping minuses.
            longest = max((len(c) for c in (self.above, self.below)
                           if len(c) <= CONDITION_OVER_ARROW_MAX), default=0)
            arrow =r"\minus\!\!\!" * min(5, max(0, (longest - 3) // 3)) + r"\longrightarrow"
        if above:
            arrow = rf"\overset{{{above}}}{{{arrow}}}"
        if below:
            arrow = rf"\underset{{{below}}}{{{arrow}}}"
        return arrow

    def side_latex(self, side):
        return r"\ +\ ".join(s.latex() for s in side)

    def latex_lines(self, split=False):
        left, right = self.side_latex(self.left), self.side_latex(self.right)
        if split:
            return [left, rf"{self.arrow_latex()}\ {right}"]
        return [rf"{left}\ {self.arrow_latex()}\ {right}"]


def looks_like_reaction(text):
    """Is this payload chemistry rather than maths? For an `equation` tag that
    was really a reaction -- the model uses the word the student used."""
    text = _unwrap_ce(text or "")
    if r"\ce" in (text or ""):
        return True
    for part in _parts(text):
        try:
            reaction = Reaction(part)
        except ValueError:
            continue
        # "P = VI" parses: P, V and I are all element symbols. What makes it
        # chemistry is every term being a formula AND something only chemistry
        # writes -- a subscript, a charge, a coefficient -- or a real arrow.
        if not reaction.all_formulas:
            continue
        terms = reaction.left + reaction.right
        chemical = any(t.charge or t.coefficient or t.electron
                       or any(tok.isdigit() for _, toks in (t.pieces or []) for tok in toks)
                       for t in terms)
        arrow = _split_arrow(_unwrap_ce(part))[1]
        if chemical or arrow != "=":
            return True
    return False


def reaction(payload):
    """[ACTION: show_visual:reaction | Title; 2H2 + O2 -> 2H2O]

    Up to four equations, one per part. Parts that are not an equation: the
    first is the title, and "above = ..." / "below = ..." (or "catalyst =",
    "condition =") go over and under the arrows that do not carry their own.
    """
    import visuals as v
    parts = _parts(payload)
    title, above, below, reactions, notes = None, "", "", [], []
    for part in parts:
        key, sep, value = part.partition("=")
        key_l = key.strip().lower()
        if sep and key_l in ("above", "over", "condition", "conditions"):
            above = value.strip()
            continue
        if sep and key_l in ("below", "under", "catalyst"):
            below = value.strip()
            continue
        try:
            reactions.append(Reaction(part, above, below))
        except ValueError:
            if title is None and not reactions:
                title = part
            else:
                notes.append(part)
    if not reactions:
        return None, "I could not read that reaction"
    reactions = reactions[:4]
    # Conditions given as separate parts apply to reactions written before them too.
    for r in reactions:
        r.above, r.below = r.above or above, r.below or below

    verdicts = [r.balanced() for r in reactions]
    for r, verdict in zip(reactions, verdicts):
        if verdict is False:
            # Drawn anyway -- a skeletal equation is a real thing to show -- but
            # logged, because the model writing a wrong one is worth knowing.
            print(f"[VISUAL] Reaction is not balanced: {r.side_latex(r.left)} -> "
                  f"{r.side_latex(r.right)}", flush=True)
    show_chip = all(verdict is True for verdict in verdicts)

    conditions = []
    for r in reactions:
        conditions += [c for c in r.long_conditions() if c not in conditions]
    note = ("Conditions: " + "; ".join(conditions)) if conditions else ""

    image, draw, top = v._canvas(title)
    bottom_room = (64 if show_chip else 24) + (40 if note else 0)
    one_line = [line for r in reactions for line in r.latex_lines()]
    split = [line for r in reactions for line in r.latex_lines(split=True)]
    # One line while it stays readable; then reactants over "-> products",
    # which lets a long reaction stay large; then one line, smaller.
    placed = _stack(image, top, one_line, bottom_room, sizes=(46, 40, 34, 30))
    if placed is False:
        placed = _stack(image, top, split, bottom_room)
    if placed is False:
        placed = _stack(image, top, one_line, bottom_room, sizes=(27, 24, 21, 19))
    if not placed:
        return None, "that reaction is too long to fit on the board"
    if note:
        font, lines, line_h = v._fitted_lines(draw, note, v.RENDER_W - 80, 40, (21, 19, 17, 15))
        y = v.RENDER_H - bottom_room + 4
        for line in lines[:2]:
            w = v._text_size(draw, line, font)[0]
            draw.text(((v.RENDER_W - w) / 2, y), line, font=font, fill=v.INK_DIM)
            y += line_h
    if show_chip:
        _chip(draw, "✓ Balanced" if len(reactions) == 1 else "✓ All balanced",
              v.RENDER_H - 52, "#15803D")
    return image, ""


# ---------------------------------------------------------------------------
# equations (maths and physics), one line or several
# ---------------------------------------------------------------------------
def _clean_latex(latex):
    """The LaTeX a model writes, turned into what mathtext reads."""
    latex = latex.strip().strip("$").strip()
    latex = re.sub(r'\\begin\{(aligned|align\*?|gather\*?|split|array)\}(\{[^}]*\})?', '', latex)
    latex = re.sub(r'\\end\{(aligned|align\*?|gather\*?|split|array)\}', '', latex)
    latex = latex.replace("&", "")
    latex = re.sub(r'\\[dt]frac', r'\\frac', latex)
    latex = re.sub(r'\\xrightarrow\s*\{([^}]*)\}', r'\\overset{\1}{\\longrightarrow}', latex)
    latex = re.sub(r'\\(displaystyle|textstyle|limits|nolimits)\b', '', latex)
    latex = re.sub(r'(?<!\\)->', r'\\rightarrow ', latex)
    latex = re.sub(r'(?<!\\)<=', r'\\leq ', latex)
    latex = re.sub(r'(?<!\\)>=', r'\\geq ', latex)
    latex = latex.replace("×", r"\times ").replace("·", r"\cdot ").replace("°", r"^\circ")
    latex = latex.replace("→", r"\rightarrow ").replace("√", r"\sqrt")
    return latex.strip()


def _formula_line(part):
    """A line that is just a chemical formula ("H2SO4", "Ca(OH)2", "SO4^2-"),
    set upright with its subscripts; None for anything else."""
    text = _unwrap_ce(part)
    if re.search(r'[=\\\s]', text):
        return None
    species = Species(text)
    if species.is_formula and (species.charge or any(
            tok.isdigit() for _, toks in species.pieces or [] for tok in toks)):
        return species.latex()
    return None


def _is_title(part):
    """Words, not maths: no operator, no backslash, no sub/superscript."""
    return (not re.search(r'[=\\^_<>+]', part)
            and (re.search(r'[A-Za-z]{4,}', part) is not None
                 or re.search(r"[A-Za-z']{2,}\s+[A-Za-z']{2,}", part) is not None))


def equation(payload):
    """One formula, or a short derivation / set of formulas, one per line.

    [ACTION: show_visual:equation | Equations of motion; v = u + at; s = ut + \\frac{1}{2}at^2]
    A payload that is really a reaction is drawn as one.
    """
    import visuals as v
    payload = v._repair_latex(payload or "")
    if looks_like_reaction(payload):
        return reaction(payload)
    parts = []
    for part in _parts(payload):
        parts.extend(p for p in re.split(r'\\\\', part) if p.strip())
    if not parts:
        return None, "there was no equation to write"
    title = None
    if len(parts) > 1 and _is_title(parts[0]):
        title, parts = parts[0], parts[1:]
    lines = [_formula_line(p) or _clean_latex(p) for p in parts[:6]]
    image, draw, top = v._canvas(title)
    placed = _stack(image, top, lines,
                    sizes=(46, 40, 34, 29, 25, 22, 19) if len(lines) > 1
                    else (52, 46, 40, 34, 29, 25, 22))
    if placed is None:
        return None, "I could not write that equation out"
    if not placed:
        return None, "that equation is too long to fit on the board"
    return image, ""


# ---------------------------------------------------------------------------
# molecules
# ---------------------------------------------------------------------------
# The ones a school asks for, so the answer does not depend on the network or
# on the model getting a SMILES right. Keys are lower case, spelled both ways.
MOLECULES = {
    "water": "O", "hydrogen peroxide": "OO", "carbon dioxide": "O=C=O",
    "carbon monoxide": "[C-]#[O+]", "oxygen": "O=O", "hydrogen": "[H][H]",
    "nitrogen": "N#N", "chlorine": "ClCl", "ozone": "[O-][O+]=O",
    "ammonia": "N", "methane": "C", "ethane": "CC", "propane": "CCC",
    "butane": "CCCC", "isobutane": "CC(C)C", "pentane": "CCCCC", "hexane": "CCCCCC",
    "ethene": "C=C", "ethylene": "C=C", "propene": "CC=C", "ethyne": "C#C",
    "acetylene": "C#C", "methanol": "CO", "ethanol": "CCO", "propanol": "CCCO",
    "methanal": "C=O", "formaldehyde": "C=O", "ethanal": "CC=O",
    "acetaldehyde": "CC=O", "propanone": "CC(C)=O", "acetone": "CC(C)=O",
    "methanoic acid": "OC=O", "formic acid": "OC=O",
    "ethanoic acid": "CC(=O)O", "acetic acid": "CC(=O)O",
    "ethyl ethanoate": "CCOC(C)=O", "ethyl acetate": "CCOC(C)=O",
    "benzene": "c1ccccc1", "toluene": "Cc1ccccc1", "phenol": "Oc1ccccc1",
    "cyclohexane": "C1CCCCC1", "naphthalene": "c1ccc2ccccc2c1",
    "glucose": "OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O",
    "fructose": "OC[C@@]1(O)OC[C@@H](O)[C@@H](O)[C@@H]1O",
    "sucrose": "OC[C@H]1O[C@@](CO)(O[C@H]2O[C@H](CO)[C@@H](O)[C@H](O)[C@H]2O)[C@@H](O)[C@@H]1O",
    "glycerol": "OCC(O)CO", "urea": "NC(N)=O", "chloromethane": "CCl",
    "chloroform": "ClC(Cl)Cl", "carbon tetrachloride": "ClC(Cl)(Cl)Cl",
    "hydrochloric acid": "Cl", "hydrogen chloride": "Cl",
    "sulphuric acid": "OS(=O)(=O)O", "sulfuric acid": "OS(=O)(=O)O",
    "nitric acid": "O[N+](=O)[O-]", "phosphoric acid": "OP(=O)(O)O",
    "carbonic acid": "OC(=O)O", "hydrogen sulphide": "S", "hydrogen sulfide": "S",
    "sulphur dioxide": "O=S=O", "sulfur dioxide": "O=S=O",
    "sulphur trioxide": "O=S(=O)=O", "sulfur trioxide": "O=S(=O)=O",
    "nitrogen dioxide": "O=[N]=O", "nitric oxide": "[N]=O",
    "sodium chloride": "[Na+].[Cl-]", "common salt": "[Na+].[Cl-]",
    "sodium hydroxide": "[Na+].[OH-]", "potassium hydroxide": "[K+].[OH-]",
    "calcium hydroxide": "[Ca+2].[OH-].[OH-]", "slaked lime": "[Ca+2].[OH-].[OH-]",
    "calcium oxide": "[Ca+2].[O-2]", "quicklime": "[Ca+2].[O-2]",
    "calcium carbonate": "[Ca+2].[O-]C([O-])=O",
    "sodium carbonate": "[Na+].[Na+].[O-]C([O-])=O", "washing soda": "[Na+].[Na+].[O-]C([O-])=O",
    "sodium bicarbonate": "[Na+].OC([O-])=O", "sodium hydrogen carbonate": "[Na+].OC([O-])=O",
    "baking soda": "[Na+].OC([O-])=O", "ammonium chloride": "[NH4+].[Cl-]",
    "aspirin": "CC(=O)Oc1ccccc1C(=O)O", "paracetamol": "CC(=O)Nc1ccc(O)cc1",
    "caffeine": "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
    "vitamin c": "OC[C@H](O)[C@H]1OC(=O)C(O)=C1O", "ascorbic acid": "OC[C@H](O)[C@H]1OC(=O)C(O)=C1O",
    "glycine": "NCC(=O)O", "alanine": "C[C@H](N)C(=O)O",
    "soap": "CCCCCCCCCCCCCCCCCC(=O)[O-].[Na+]", "sodium stearate": "CCCCCCCCCCCCCCCCCC(=O)[O-].[Na+]",
    "adenine": "Nc1ncnc2[nH]cnc12",
}

_SUBSCRIPT = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")


def _name_key(text):
    text = text.lower().strip(" .")
    text = re.sub(r'\b(the|a|an|molecule|molecular|structure|structural|formula|of|compound)\b', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _pubchem_smiles(name):
    import visuals as v
    for prop in ("SMILES", "IsomericSMILES"):
        url = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
               f"{urllib.parse.quote(name)}/property/{prop}/TXT")
        try:
            with urllib.request.urlopen(url, timeout=v.FETCH_TIMEOUT_S) as response:
                text = response.read(4096).decode("utf-8", "replace").strip().splitlines()
            if text and text[0].strip():
                return text[0].strip()
        except Exception as exc:
            print(f"[VISUAL] PubChem has no {name!r} ({exc}).", flush=True)
            return None
    return None


def _resolve_molecule(parts):
    """(rdkit mol, display name) from "Ethanol; CCO", "Ethanol", or "CCO"."""
    from rdkit import Chem
    title = parts[0]
    given = [p for p in parts[1:] if not re.match(r'^(formula|name)\s*=', p, re.I)]
    key = _name_key(title)
    if key in MOLECULES:
        return Chem.MolFromSmiles(MOLECULES[key]), title
    for candidate in given + ([title] if not given else []):
        text = re.sub(r'^smiles\s*[=:]\s*', '', candidate, flags=re.I).strip()
        if text and " " not in text:
            mol = Chem.MolFromSmiles(text)
            if mol is not None and mol.GetNumAtoms():
                return mol, title if candidate is not title else ""
    smiles = _pubchem_smiles(key or title)
    if smiles:
        mol = Chem.MolFromSmiles(smiles)
        if mol is not None:
            return mol, title
    return None, title


def molecule(payload):
    """[ACTION: show_visual:molecule | Ethanol; CCO] -- name, then SMILES."""
    import visuals as v
    parts = _parts(payload)
    if not parts:
        return None, "there was no molecule to draw"
    try:
        from rdkit import Chem, RDLogger
        from rdkit.Chem import AllChem, rdMolDescriptors
        from rdkit.Chem.Draw import rdMolDraw2D
    except Exception as exc:
        return None, f"the molecule drawer is not installed ({exc})"
    RDLogger.DisableLog("rdApp.*")
    mol, name = _resolve_molecule(parts)
    if mol is None:
        return None, f"I could not work out the structure of {parts[0]}"

    formula = rdMolDescriptors.CalcMolFormula(mol)
    # Small molecules the way a textbook draws them, every H on show; beyond
    # that the hydrogens bury the skeleton, so they stay implied.
    if mol.GetNumHeavyAtoms() <= 8:
        mol = Chem.AddHs(mol)
    AllChem.Compute2DCoords(mol)

    heading = f"{name} ({formula.translate(_SUBSCRIPT)})" if name else formula.translate(_SUBSCRIPT)
    image, draw, top = v._canvas(heading)
    width, height = v.RENDER_W - 40, v.RENDER_H - top - 10
    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    options = drawer.drawOptions()
    options.bondLineWidth = 3
    options.minFontSize = 20
    options.maxFontSize = 40
    options.padding = 0.08
    # A cap, so water is drawn at the size benzene is rather than stretched
    # across the whole board.
    options.fixedBondLength = 75
    options.setBackgroundColour((1, 1, 1, 1))
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    drawn = Image.open(io.BytesIO(drawer.GetDrawingText())).convert("RGB")
    image.paste(drawn, (20, top))
    return image, ""


# ---------------------------------------------------------------------------
# free-body (force) diagrams
# ---------------------------------------------------------------------------
DIRECTIONS = {
    "up": 90, "upward": 90, "upwards": 90, "down": 270, "downward": 270,
    "downwards": 270, "left": 180, "right": 0, "up-right": 45, "up right": 45,
    "up-left": 135, "up left": 135, "down-right": 315, "down right": 315,
    "down-left": 225, "down left": 225, "north": 90, "south": 270, "east": 0,
    "west": 180,
}
RE_MAGNITUDE = re.compile(r'(-?\d+(?:\.\d+)?)\s*(k?N|newtons?)\b', re.I)


def _direction(key):
    key = key.strip().lower()
    if key in DIRECTIONS:
        return DIRECTIONS[key]
    angle = re.fullmatch(r'(-?\d+(?:\.\d+)?)\s*(?:°|deg|degrees?)?', key)
    return float(angle.group(1)) % 360 if angle else None


def forces(payload):
    """[ACTION: show_visual:forces | Block on a table; object = Block;
       up = Normal force 20 N; down = Weight 20 N; right = Push 10 N; left = Friction 4 N]

    Directions are up/down/left/right, the four diagonals, or an angle in
    degrees (0 = right, 90 = up). `surface` draws the floor under the object.
    """
    import visuals as v
    parts = _parts(payload)
    title, label, surface, arrows = None, "", False, []
    for part in parts:
        key, sep, value = part.partition("=")
        if not sep:
            if part.lower().strip() in ("surface", "floor", "ground", "table"):
                surface = True
            elif title is None and not arrows:
                title = part
            continue
        key_l = key.strip().lower()
        if key_l in ("object", "body", "on"):
            label = value.strip()
            continue
        if key_l in ("surface", "floor", "ground"):
            surface = True
            continue
        angle = _direction(key_l)
        if angle is None:
            continue
        magnitude = RE_MAGNITUDE.search(value)
        arrows.append((angle, value.strip(),
                       float(magnitude.group(1)) * (1000 if magnitude.group(2).lower() == "kn" else 1)
                       if magnitude else None))
    if not arrows:
        return None, "there were no forces to draw"
    arrows = arrows[:8]

    image, draw, top = v._canvas(title)
    footer = 40
    cx, cy = v.RENDER_W / 2, top + (v.RENDER_H - top - footer) / 2
    box_w, box_h = 150, 84
    room = min((v.RENDER_H - top - footer) / 2 - box_h / 2 - 34, 200)
    numbers = [m for _, _, m in arrows if m]
    biggest = max(numbers) if numbers and len(numbers) == len(arrows) else None

    if surface:
        floor = cy + box_h / 2
        draw.line([(cx - 260, floor), (cx + 260, floor)], fill=v.INK_DIM, width=4)
        for x in range(int(cx - 250), int(cx + 260), 22):
            draw.line([(x, floor + 2), (x - 14, floor + 16)], fill=v.RULE, width=3)
    v._rounded(draw, (cx - box_w / 2, cy - box_h / 2, cx + box_w / 2, cy + box_h / 2),
               12, v.ACCENT_SOFT, v.ACCENT)
    v._label_in_box(draw, (cx - box_w / 2, cy - box_h / 2, cx + box_w / 2, cy + box_h / 2),
                    label or "Object", v.ACCENT)

    font = v._font(20)
    for index, (angle, text, magnitude) in enumerate(arrows):
        colour = v.STAGE_COLOURS[index % len(v.STAGE_COLOURS)]
        rad = math.radians(angle)
        dx, dy = math.cos(rad), -math.sin(rad)
        # Out from the edge of the box, not its centre, so the arrows read.
        edge = min(abs((box_w / 2) / dx) if dx else 1e9, abs((box_h / 2) / dy) if dy else 1e9)
        horizontal = abs(dx) > abs(dy)
        reach = (v.RENDER_W / 2 - box_w / 2 - 150) if horizontal else room
        length = reach * (0.45 + 0.55 * magnitude / biggest) if biggest else reach * 0.85
        start = (cx + dx * edge, cy + dy * edge)
        end = (start[0] + dx * length, start[1] + dy * length)
        v._arrow(draw, start, end, colour, width=6, head=18)
        # The label beyond the tip, pushed clear of the arrow head.
        lines = v._wrap(draw, text, font, 250 if abs(dy) > 0.5 else 190)
        line_h = v._text_size(draw, "Ag", font)[1] + 4
        block_w = max(v._text_size(draw, line, font)[0] for line in lines)
        block_h = line_h * len(lines)
        lx = end[0] + dx * 14 + (0 if dx > 0.3 else -block_w if dx < -0.3 else -block_w / 2)
        ly = end[1] + dy * 14 + (-block_h if dy < -0.3 else 0 if dy > 0.3 else -block_h / 2)
        lx = max(8, min(v.RENDER_W - block_w - 8, lx))
        ly = max(top, min(v.RENDER_H - block_h - 8, ly))
        for n, line in enumerate(lines):
            draw.text((lx, ly + n * line_h), line, font=font, fill=colour)

    # The net force, when every arrow carries a number to add up.
    if biggest:
        fx = sum(m * math.cos(math.radians(a)) for a, _, m in arrows)
        fy = sum(m * math.sin(math.radians(a)) for a, _, m in arrows)
        net = math.hypot(fx, fy)
        if net < 1e-6:
            summary = "Net force = 0 (balanced forces)"
        else:
            heading = math.degrees(math.atan2(fy, fx)) % 360
            named = min(DIRECTIONS.items(), key=lambda kv: min(abs(kv[1] - heading), 360 - abs(kv[1] - heading)))
            word = named[0] if min(abs(named[1] - heading), 360 - abs(named[1] - heading)) < 1 else f"{heading:.0f}°"
            summary = f"Net force = {net:g} N {'to the ' + word if word in ('left', 'right') else word}"
        width = v._text_size(draw, summary, v._font(22))[0]
        draw.text(((v.RENDER_W - width) / 2, v.RENDER_H - 34), summary,
                  font=v._font(22), fill=v.INK)
    return image, ""


# ---------------------------------------------------------------------------
# circuits
# ---------------------------------------------------------------------------
RE_COMPONENT = re.compile(
    r'^(cell|battery|resistor|resistance|bulb|lamp|switch|key|open switch|closed switch|'
    r'ammeter|voltmeter|galvanometer|led|diode|fuse|capacitor|rheostat|wire)\b\s*(.*)$', re.I)
COMPONENT_ALIASES = {"resistance": "resistor", "lamp": "bulb", "key": "switch",
                     "open switch": "switch", "diode": "led"}
SOURCES = ("cell", "battery")
PART_LEN = 78


def _component(text):
    match = RE_COMPONENT.match(text.strip())
    if not match:
        return None
    kind = match.group(1).lower()
    value = match.group(2).strip()
    value = re.sub(r'\bohms?\b', "Ω", value, flags=re.I)
    closed = kind == "closed switch" or "closed" in value.lower()
    if kind == "closed switch":
        kind = "switch"
    if kind == "switch":
        value = re.sub(r'\b(open|closed)\b', '', value, flags=re.I).strip()
    # "voltmeter across the resistor": where it goes is decided by its place in
    # the list, so the words are not a label.
    value = re.sub(r'\b(across|over|in parallel|in series)\b.*$', '', value, flags=re.I).strip()
    return {"kind": COMPONENT_ALIASES.get(kind, kind), "value": value, "closed": closed}


def _draw_part(draw, part, x0, x1, y, label_below=False):
    """One component on a horizontal wire from x0 to x1 at height y. Its value
    is written above it, or below when something else is drawn above."""
    import visuals as v
    kind, value = part["kind"], part["value"]
    mid = (x0 + x1) / 2
    half = PART_LEN / 2
    a, b = mid - half, mid + half
    wire = dict(fill=v.INK, width=4)
    draw.line([(x0, y), (a, y)], **wire)
    draw.line([(b, y), (x1, y)], **wire)
    label_y = y + 24 if label_below else y - 46
    if kind in ("resistor", "rheostat"):
        points = [(a, y)]
        for i in range(1, 8):
            points.append((a + i * PART_LEN / 8, y + (-12 if i % 2 else 12)))
        points.append((b, y))
        draw.line(points, fill=v.ACCENT, width=4, joint="curve")
        if kind == "rheostat":
            v._arrow(draw, (a + 6, y + 22), (b - 6, y - 22), v.ACCENT, width=3, head=10)
    elif kind == "bulb":
        draw.line([(a, y), (mid - 20, y)], **wire)
        draw.line([(mid + 20, y), (b, y)], **wire)
        draw.ellipse([mid - 20, y - 20, mid + 20, y + 20], outline=v.ACCENT, width=4, fill="#FFF7D6")
        draw.line([(mid - 13, y - 13), (mid + 13, y + 13)], fill=v.ACCENT, width=3)
        draw.line([(mid - 13, y + 13), (mid + 13, y - 13)], fill=v.ACCENT, width=3)
    elif kind in ("ammeter", "voltmeter", "galvanometer"):
        draw.line([(a, y), (mid - 21, y)], **wire)
        draw.line([(mid + 21, y), (b, y)], **wire)
        draw.ellipse([mid - 21, y - 21, mid + 21, y + 21], outline=v.ACCENT, width=4, fill=v.PAPER)
        letter = {"ammeter": "A", "voltmeter": "V", "galvanometer": "G"}[kind]
        w, h = v._text_size(draw, letter, v._font(24))
        draw.text((mid - w / 2, y - h / 2 - 5), letter, font=v._font(24), fill=v.ACCENT)
    elif kind == "switch":
        draw.line([(a, y), (a + 16, y)], **wire)
        draw.line([(b - 16, y), (b, y)], **wire)
        draw.ellipse([a + 12, y - 5, a + 22, y + 5], fill=v.INK)
        draw.ellipse([b - 22, y - 5, b - 12, y + 5], fill=v.INK)
        end = (b - 17, y - 4) if part["closed"] else (b - 20, y - 30)
        draw.line([(a + 17, y), end], fill=v.INK, width=4)
    elif kind == "led":
        draw.line([(a, y), (mid - 14, y)], **wire)
        draw.line([(mid + 14, y), (b, y)], **wire)
        draw.polygon([(mid - 14, y - 16), (mid - 14, y + 16), (mid + 14, y)], outline=v.ACCENT, fill=v.ACCENT_SOFT)
        draw.line([(mid + 14, y - 16), (mid + 14, y + 16)], fill=v.ACCENT, width=4)
        v._arrow(draw, (mid + 2, y - 20), (mid + 14, y - 34), "#D97706", width=2, head=7)
    elif kind == "fuse":
        draw.rectangle([a + 10, y - 10, b - 10, y + 10], outline=v.ACCENT, width=3)
        draw.line([(a, y), (b, y)], **wire)
    elif kind == "capacitor":
        draw.line([(a, y), (mid - 7, y)], **wire)
        draw.line([(mid + 7, y), (b, y)], **wire)
        draw.line([(mid - 7, y - 20), (mid - 7, y + 20)], fill=v.ACCENT, width=5)
        draw.line([(mid + 7, y - 20), (mid + 7, y + 20)], fill=v.ACCENT, width=5)
    else:  # plain wire
        draw.line([(a, y), (b, y)], **wire)
    if value:
        font = v._font(19)
        w = v._text_size(draw, value, font)[0]
        draw.text((mid - w / 2, label_y), value, font=font, fill=v.INK_DIM)


def _draw_source(draw, part, x, y0, y1):
    """A cell (or battery of two) on the vertical wire at x, from y0 down to y1."""
    import visuals as v
    mid = (y0 + y1) / 2
    cells = 2 if part["kind"] == "battery" else 1
    span = 16 * cells + 8 * (cells - 1)
    top = mid - span / 2
    draw.line([(x, y0), (x, top)], fill=v.INK, width=4)
    draw.line([(x, top + span), (x, y1)], fill=v.INK, width=4)
    for c in range(cells):
        yy = top + c * 24
        draw.line([(x - 26, yy), (x + 26, yy)], fill=v.INK, width=4)       # long: +
        draw.line([(x - 13, yy + 14), (x + 13, yy + 14)], fill=v.INK, width=8)  # short: -
    draw.text((x + 30, top - 22), "+", font=v._font(20), fill=v.INK_DIM)
    if part["value"]:
        font = v._font(19)
        w = v._text_size(draw, part["value"], font)[0]
        draw.text((x - 34 - w, mid - 12), part["value"], font=font, fill=v.INK_DIM)


def circuit(payload):
    """[ACTION: show_visual:circuit | Series circuit; cell 6 V; switch; resistor 2 Ω; bulb; ammeter]

    Everything in one loop, in order, starting from the cell. A part written as
    "parallel = resistor 2 Ω, resistor 3 Ω" is a set of branches side by side.
    A voltmeter is connected ACROSS the part written just before it.
    """
    import visuals as v
    title, sources, slots = None, [], []
    for part in _parts(payload):
        key, sep, value = part.partition("=")
        if sep and key.strip().lower() in ("parallel", "branches", "in parallel"):
            branch = [c for c in (_component(p) for p in re.split(r'\s*[,|/]\s*', value)) if c]
            if branch:
                slots.append({"kind": "parallel", "branches": branch[:4]})
            continue
        component = _component(part)
        if component is None:
            if title is None and not slots and not sources:
                title = part
            continue
        if component["kind"] in SOURCES:
            sources.append(component)
        elif component["kind"] == "voltmeter" and slots and slots[-1]["kind"] == "parallel":
            # Across a parallel block IS one more branch of it.
            slots[-1]["branches"] = (slots[-1]["branches"] + [component])[:4]
        elif component["kind"] == "voltmeter" and slots:
            slots[-1].setdefault("across", component)
        else:
            slots.append(component)
    if not sources and not slots:
        return None, "there was no circuit to draw"
    if not sources:
        sources = [{"kind": "cell", "value": "", "closed": False}]
    slots = slots[:6]

    image, draw, top = v._canvas(title)
    left, right = 120, v.RENDER_W - 70
    # Room above the top wire for a voltmeter bridged over one of its parts.
    y_top = top + (84 if any(s.get("across") for s in slots) else 64)
    rungs = max([len(s["branches"]) for s in slots if s["kind"] == "parallel"] or [1])
    y_bottom = min(v.RENDER_H - 30, max(y_top + 150, y_top + 70 * rungs + 40))

    # The source on the left side; the rest shared between the top and bottom wires.
    source = sources[0] if len(sources) == 1 else {"kind": "battery", "value": " + ".join(
        s["value"] for s in sources if s["value"]), "closed": False}
    _draw_source(draw, source, left, y_top, y_bottom)
    draw.line([(right, y_top), (right, y_bottom)], fill=v.INK, width=4)

    top_slots = slots[:max(1, (len(slots) + 1) // 2)] if len(slots) > 2 else slots
    bottom_slots = slots[len(top_slots):]
    if any(s["kind"] == "parallel" for s in bottom_slots):
        top_slots, bottom_slots = slots, []

    def lay(row, y):
        if not row:
            draw.line([(left, y), (right, y)], fill=v.INK, width=4)
            return
        # A lead-in from each corner, so a parallel block's rail never lands on
        # top of the wire coming up from the cell.
        start, end = left + 36, right - 24
        draw.line([(left, y), (start, y)], fill=v.INK, width=4)
        draw.line([(end, y), (right, y)], fill=v.INK, width=4)
        width = (end - start) / len(row)
        for i, slot in enumerate(row):
            x0, x1 = start + i * width, start + (i + 1) * width
            if slot["kind"] == "parallel":
                n = len(slot["branches"])
                bx0, bx1 = x0 + 18, x1 - 18
                draw.line([(x0, y), (bx0, y)], fill=v.INK, width=4)
                draw.line([(bx1, y), (x1, y)], fill=v.INK, width=4)
                gap = (y_bottom - y - 50) / max(1, n - 1) if n > 1 else 0
                gap = min(gap, 80)
                draw.line([(bx0, y), (bx0, y + gap * (n - 1))], fill=v.INK, width=4)
                draw.line([(bx1, y), (bx1, y + gap * (n - 1))], fill=v.INK, width=4)
                for j, branch in enumerate(slot["branches"]):
                    _draw_part(draw, branch, bx0, bx1, y + gap * j)
            else:
                _draw_part(draw, slot, x0, x1, y,
                           label_below=bool(slot.get("across")) and y == y_top)
            across = slot.get("across")
            if across:
                # Bridged over the part it measures, above a top wire and
                # below a bottom one, so it never crosses the loop.
                up = -1 if y == y_top else 1
                mid, half = (x0 + x1) / 2, PART_LEN / 2 + 8
                hy = y + up * 52
                draw.line([(mid - half, y), (mid - half, hy)], fill=v.INK_DIM, width=3)
                draw.line([(mid + half, y), (mid + half, hy)], fill=v.INK_DIM, width=3)
                _draw_part(draw, {"kind": "voltmeter", "value": across["value"], "closed": False},
                           mid - half, mid + half, hy)

    lay(top_slots, y_top)
    lay(bottom_slots, y_bottom)
    for corner in ((left, y_top), (right, y_top), (left, y_bottom), (right, y_bottom)):
        draw.ellipse([corner[0] - 4, corner[1] - 4, corner[0] + 4, corner[1] + 4], fill=v.INK)
    return image, ""
