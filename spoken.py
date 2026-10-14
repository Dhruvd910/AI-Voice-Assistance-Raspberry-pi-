"""Maths, chemistry and units, said the way a teacher says them.

The voice reads what it is given, and what the model writes for the screen is
not what anybody SAYS. Measured by speaking her own sentences through Cartesia
and transcribing the audio back with Whisper -- what a child in the room hears:

    H₂SO₄                ->  "H-G-S-O-D"
    2H₂ + O₂ → 2H₂O      ->  "2H Su plus O tan 2H Su"
    2x = 4               ->  "2x equals sign 4"
    x² − 5x + 6 = 0      ->  "x squared 5x plus 6 equals 0"   (the minus gone)
    πr²                  ->  "par squared"
    √16                  ->  "zaars"
    20 m/s, 6 N          ->  "20 m slash s", "6n"
    NaCl                 ->  "Nushiel"

So every sentence goes through speakable() on its way to the voice: formulas
spelled the way a chemistry teacher spells them ("H 2 S O 4"), symbols said as
words ("equals", "minus", "squared", "square root of"), units said in full
("metres per second", "newtons"). Only what is SENT to the voice changes -- the
caption keeps the symbols, which read better than the words.

Hindi sentences get the Hindi words a maths teacher uses (बराबर, गुणा, बटा, का
वर्ग), since a Hindi reply is spoken by the Hindi voice.

A leaf: nothing here imports anything of ours.
"""

import re

# ---------------------------------------------------------------------------
# chemistry
# ---------------------------------------------------------------------------
ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr
Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb
Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt
Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr""".split())

SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-")
SUPERSCRIPTS = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6",
                "⁷": "7", "⁸": "8", "⁹": "9", "⁺": "+", "⁻": "-", "ⁿ": "n"}
RE_SUPER_RUN = re.compile("[" + "".join(SUPERSCRIPTS) + "]+")

# A formula: element symbols with counts, brackets, and a charge at the end.
# Tried on every word that has a capital letter in it, and only kept when
# the whole word parses as symbols of real elements AND looks like chemistry
# -- a count, a two-letter symbol beside another, a charge -- so "OK", "IT",
# "CBSE" and "He" stay words.
RE_FORMULA_WORD = re.compile(
    r"(?<![A-Za-z0-9])(\d*)((?:[A-Z][a-z]?\d*|\((?:[A-Z][a-z]?\d*)+\)\d*)+)"
    r"(\^?\{?\d*[+-]\}?)?(?![A-Za-z0-9])")
RE_SYMBOL = re.compile(r"[A-Z][a-z]?|\d+|[()]")
COMMON_WORDS = {"OK", "IT", "IS", "IN", "AS", "AT", "US", "NO", "SO", "HE", "HI",
                "BE", "BY", "OF", "ON", "OR", "TO", "UP", "WE", "CO", "PS", "TV",
                "AI", "UK", "AM", "PM", "ICSE", "CBSE", "NCERT", "I", "A", "S", "B",
                "C", "N", "O", "K", "V", "W", "U", "P", "F", "Y"}


def _formula(match):
    """One chemical formula, spelled out, or the word untouched."""
    whole = match.group(0)
    coefficient, body, charge = match.group(1), match.group(2), match.group(3) or ""
    if body.upper() == body and body in COMMON_WORDS:
        return whole
    symbols = [s for s in RE_SYMBOL.findall(body) if s not in "()"]
    if not all(s.isdigit() or s in ELEMENTS for s in symbols):
        return whole
    letters = [s for s in symbols if not s.isdigit()]
    has_count = any(s.isdigit() for s in symbols)
    two_letter = any(len(s) == 2 for s in letters)
    # Chemistry only: a count inside it (H2O, CO2), a charge, or two or more
    # symbols with a two-letter one among them (NaCl, HCl, NaOH). A capitalised
    # ordinary word fails the element test above long before this.
    if not (has_count or charge or (len(letters) >= 2 and two_letter)):
        return whole
    if not has_count and not charge and len(letters) == 2 and not two_letter:
        return whole
    said = []
    for symbol in RE_SYMBOL.findall(body):
        if symbol in "()":
            continue
        # Element symbols letter by letter -- "N A C L", the way a class reads
        # NaCl off the board -- and counts as numbers.
        said.append(symbol if symbol.isdigit() else " ".join(symbol.upper()))
    text = " ".join(said)
    if coefficient:
        text = f"{coefficient} {text}"
    sign = charge.strip("^{}")
    if sign:
        number = sign.rstrip("+-")
        text += f" {number}" if number and number != "1" else ""
        text += " plus" if sign.endswith("+") else " minus"
    return text


# ---------------------------------------------------------------------------
# units, after a number
# ---------------------------------------------------------------------------
# name, plural -- longest spelling first so "m/s²" is not read as "m" then "/s²".
UNITS = [
    ("m/s²", "metre per second squared", "metres per second squared"),
    ("m/s^2", "metre per second squared", "metres per second squared"),
    ("km/h", "kilometre per hour", "kilometres per hour"),
    ("km/hr", "kilometre per hour", "kilometres per hour"),
    ("m/s", "metre per second", "metres per second"),
    ("cm²", "square centimetre", "square centimetres"),
    ("cm^2", "square centimetre", "square centimetres"),
    ("m²", "square metre", "square metres"),
    ("m^2", "square metre", "square metres"),
    ("km²", "square kilometre", "square kilometres"),
    ("mm²", "square millimetre", "square millimetres"),
    ("cm³", "cubic centimetre", "cubic centimetres"),
    ("cm^3", "cubic centimetre", "cubic centimetres"),
    ("m³", "cubic metre", "cubic metres"),
    ("m^3", "cubic metre", "cubic metres"),
    ("kg", "kilogram", "kilograms"), ("mg", "milligram", "milligrams"),
    ("km", "kilometre", "kilometres"), ("cm", "centimetre", "centimetres"),
    ("mm", "millimetre", "millimetres"), ("ml", "millilitre", "millilitres"),
    ("mL", "millilitre", "millilitres"), ("kJ", "kilojoule", "kilojoules"),
    ("kW", "kilowatt", "kilowatts"), ("kN", "kilonewton", "kilonewtons"),
    ("Hz", "hertz", "hertz"), ("Pa", "pascal", "pascals"), ("mol", "mole", "moles"),
    ("°C", "degree Celsius", "degrees Celsius"), ("°F", "degree Fahrenheit",
                                                   "degrees Fahrenheit"),
    ("Ω", "ohm", "ohms"), ("min", "minute", "minutes"), ("hr", "hour", "hours"),
    ("N", "newton", "newtons"), ("J", "joule", "joules"), ("W", "watt", "watts"),
    ("V", "volt", "volts"), ("A", "ampere", "amperes"), ("K", "kelvin", "kelvin"),
    ("L", "litre", "litres"), ("g", "gram", "grams"), ("m", "metre", "metres"),
    ("s", "second", "seconds"), ("h", "hour", "hours"),
]
UNITS_HI = {
    "m/s²": "मीटर प्रति सेकंड का वर्ग", "m/s^2": "मीटर प्रति सेकंड का वर्ग",
    "km/h": "किलोमीटर प्रति घंटा", "km/hr": "किलोमीटर प्रति घंटा",
    "m/s": "मीटर प्रति सेकंड", "cm²": "वर्ग सेंटीमीटर", "cm^2": "वर्ग सेंटीमीटर",
    "m²": "वर्ग मीटर", "m^2": "वर्ग मीटर", "km²": "वर्ग किलोमीटर", "mm²": "वर्ग मिलीमीटर",
    "cm³": "घन सेंटीमीटर", "cm^3": "घन सेंटीमीटर", "m³": "घन मीटर", "m^3": "घन मीटर",
    "kg": "किलोग्राम", "mg": "मिलीग्राम", "km": "किलोमीटर", "cm": "सेंटीमीटर",
    "mm": "मिलीमीटर", "ml": "मिलीलीटर", "mL": "मिलीलीटर", "kJ": "किलोजूल",
    "kW": "किलोवाट", "kN": "किलोन्यूटन", "Hz": "हर्ट्ज़", "Pa": "पास्कल", "mol": "मोल",
    "°C": "डिग्री सेल्सियस", "°F": "डिग्री फ़ारेनहाइट", "Ω": "ओम", "min": "मिनट",
    "hr": "घंटे", "N": "न्यूटन", "J": "जूल", "W": "वाट", "V": "वोल्ट", "A": "एम्पियर",
    "K": "केल्विन", "L": "लीटर", "g": "ग्राम", "m": "मीटर", "s": "सेकंड", "h": "घंटे",
}
_UNIT_ALTERNATION = "|".join(re.escape(u[0]) for u in UNITS)
# A unit stands after a number (with or without a space) and is not the start
# of a longer word: "5 m" and "5m" are metres, "5 mangoes" is not.
RE_UNIT = re.compile(rf"(?<![\w.])(\d+(?:\.\d+)?)\s?({_UNIT_ALTERNATION})(?![A-Za-z0-9²³^])")
# Single letters that are units only with a space before them: "2x" and "3a"
# are algebra, but "6 N" and "2 s" are newtons and seconds.
TIGHT_ONLY = {"J", "W", "V", "A", "K", "L", "g", "m", "s", "h"}
UNIT_WORDS = {u[0]: (u[1], u[2]) for u in UNITS}
# Left at the end of a unit said in full, so "10 newtons - 4 newtons" is still
# a subtraction to the operators below; taken out again before the voice.
OPERAND = "\x07"


# Letters that are units only in a sentence about the thing they measure:
# "Class 6 A" is a section, not six amperes.
UNIT_CONTEXT = {
    "A": re.compile(r"current|ampere|circuit|volt|resist|ohm|charge|करंट|धारा", re.I),
    "K": re.compile(r"temperature|kelvin|heat|degree|तापमान", re.I),
    "L": re.compile(r"volume|litre|liter|water|liquid|आयतन|पानी", re.I),
    "h": re.compile(r"speed|time|hour|travel|journey|km|गति|समय|घंट", re.I),
}


def _unit(match, hindi):
    number, unit = match.group(1), match.group(2)
    gap = match.group(0)[len(number):len(match.group(0)) - len(unit)]
    if unit in TIGHT_ONLY and not gap:
        return match.group(0)
    context = UNIT_CONTEXT.get(unit)
    if context is not None and not context.search(match.string):
        return match.group(0)
    if hindi:
        return f"{number} {UNITS_HI.get(unit, unit)}{OPERAND}"
    one, many = UNIT_WORDS[unit]
    return f"{number} {one if number in ('1', '1.0') else many}{OPERAND}"


# ---------------------------------------------------------------------------
# symbols
# ---------------------------------------------------------------------------
GREEK = {"π": "pi", "θ": "theta", "α": "alpha", "β": "beta", "γ": "gamma",
         "δ": "delta", "Δ": "delta", "λ": "lambda", "μ": "mu", "σ": "sigma",
         "ω": "omega", "φ": "phi", "ρ": "rho", "τ": "tau", "ε": "epsilon",
         "Ω": "omega"}
LATEX_WORDS = {"pi": "pi", "theta": "theta", "alpha": "alpha", "beta": "beta",
               "gamma": "gamma", "delta": "delta", "Delta": "delta", "lambda": "lambda",
               "mu": "mu", "sigma": "sigma", "omega": "omega", "phi": "phi",
               "rho": "rho", "times": "times", "cdot": "times", "div": "divided by",
               "pm": "plus or minus", "approx": "is about", "neq": "is not equal to",
               "leq": "is less than or equal to", "geq": "is greater than or equal to",
               "rightarrow": "gives", "to": "gives", "infty": "infinity",
               "sin": "sine", "cos": "cos", "tan": "tan", "log": "log", "ln": "l n"}

EN = {"=": "equals", "+": "plus", "-": "minus", "×": "times", "÷": "divided by",
      "/": "over", "±": "plus or minus", "≈": "is about", "≠": "is not equal to",
      "≤": "is less than or equal to", "≥": "is greater than or equal to",
      "<": "is less than", ">": "is greater than", "→": "gives", "⇌": "is in equilibrium with",
      "²": "squared", "³": "cubed", "power": "to the power", "root": "the square root of",
      "cube root": "the cube root of", "°": "degrees", "angle": "angle",
      "triangle": "triangle", "perp": "is perpendicular to", "parallel": "is parallel to",
      "percent": "percent"}
HI = {"=": "बराबर", "+": "प्लस", "-": "माइनस", "×": "गुणा", "÷": "भाग", "/": "बटा",
      "±": "प्लस माइनस", "≈": "लगभग", "≠": "बराबर नहीं", "≤": "से कम या बराबर",
      "≥": "से ज़्यादा या बराबर", "<": "से कम", ">": "से ज़्यादा", "→": "से बनता है",
      "⇌": "के साथ साम्य में", "²": "का वर्ग", "³": "का घन", "power": "की घात",
      "root": "का वर्गमूल", "cube root": "का घनमूल", "°": "डिग्री", "angle": "कोण",
      "triangle": "त्रिभुज", "perp": "लंब है", "parallel": "समानांतर है", "percent": "प्रतिशत"}

RE_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# What a sentence about geometry calls its points: AB, ABC, PQR. Only spelled
# out in a sentence that is about geometry, so "OK" and "TV" are left alone.
RE_GEOMETRY = re.compile(r"\b(?:triangle|angle|side|line|segment|ray|parallel|perpendicular|"
                         r"quadrilateral|rectangle|square|circle|chord|radius|diameter|"
                         r"vertex|vertices|point|arc|tangent|polygon)s?\b|[∠△⊥∥]|त्रिभुज|कोण|भुजा",
                         re.IGNORECASE)
RE_POINT_NAMES = re.compile(r"(?<![A-Za-z])([A-Z]{2,4})(?![A-Za-z])")


def _power(base, exponent, words):
    exponent = exponent.strip("{}() ")
    if words is HI:
        if exponent == "2":
            return f"{base} {HI['²']}"
        if exponent == "3":
            return f"{base} {HI['³']}"
        return f"{base} {HI['power']} {exponent.replace('-', 'माइनस ')}"
    if exponent == "2":
        return f"{base} squared"
    if exponent == "3":
        return f"{base} cubed"
    return f"{base} to the power {exponent.replace('-', 'minus ')}"


# What an operator can stand between. Left: a digit, a lone letter (x, not
# "this"), a closing bracket. Right: the same, an opening bracket, or a root.
LEFT = r"(?:(?<=\d)|(?<=(?<![A-Za-z])[A-Za-z])|(?<=[)\]\x07]))"
RIGHT = r"(?=\d|[A-Za-z](?![A-Za-z])|[(√])"


def _operators(out, words):
    out = re.sub(r"(?<=[\w)\]\x07])\s*=\s*(?=[\w(\-−√])", f" {words['=']} ", out)
    # The minus sign itself is never anything else.
    out = re.sub(r"(?<=[\w)\]\x07])\s*−\s*(?=[\w(√])", f" {words['-']} ", out)
    # A hyphen: spaced between operands ("7 - 3", "x - 2"), or tight in
    # algebra ("x-2", "2x-3") -- not "well-known", not "2023-24".
    out = re.sub(rf"{LEFT}\s+-\s+{RIGHT}", f" {words['-']} ", out)
    out = re.sub(r"(?:(?<=\d[a-z])|(?<=(?<![A-Za-z])[a-z]))-(?=[\d(])", f" {words['-']} ", out)
    # A negative number: at the start, after a space, a bracket or an operator.
    out = re.sub(r"(?:^|(?<=[\s(]))[-−](?=\d)", f"{words['-']} ", out)
    out = re.sub(r"(?<=[\w)\]\x07])\s*\+\s*(?=[\w(√])", f" {words['+']} ", out)
    out = re.sub(r"(?<=[\w)\]])\s*×\s*(?=[\w(])|(?<=\d)\s*[xX]\s*(?=\d)",
                 f" {words['×']} ", out)
    out = re.sub(r"(?<=[\w)\]])\s*÷\s*(?=[\w(])", f" {words['÷']} ", out)
    out = re.sub(rf"{LEFT}\s*/\s*{RIGHT}", f" {words['/']} ", out)
    for symbol in ("≈", "≠", "≤", "≥", "±"):
        out = out.replace(symbol, f" {words[symbol]} ")
    out = re.sub(rf"{LEFT}\s+<\s+{RIGHT}", f" {words['<']} ", out)
    out = re.sub(rf"{LEFT}\s+>\s+{RIGHT}", f" {words['>']} ", out)
    out = re.sub(r"(?<=\d)\s*%", f" {words['percent']}", out)
    return out


# A letter standing for a number. Said lower case it comes out wrong: "y
# equals x squared" was heard back as "E equals X squared", "a equals 4" as "i
# equals 4", "y is 7" as "Yai is 7" -- the voice reads a lone small letter as a
# syllable. A capital is read as the letter. So a lone letter beside an
# operator word is capitalised, which leaves the article in "a cube" alone.
OP_AFTER = (r"(?:equals|plus|minus|times|over|squared|cubed|is about|is not equal to|"
            r"is less than|is greater than|to the power|divided by|is\s+(?:minus\s+)?\d)")


def _variables(out):
    out = re.sub(rf"(?<![\w'’-])([a-z])(?=\s+{OP_AFTER}\b)", lambda m: m.group(1).upper(), out)
    return re.sub(r"\b(equals|plus|minus|times|over)\s([a-z])(?![\w'’])",
                  lambda m: f"{m.group(1)} {m.group(2).upper()}", out)


def speakable(text, language=None):
    """`text` with its maths, chemistry and units in words, for the voice.

    `language` is "hi" or "en"; left out, a sentence with Devanagari in it is
    Hindi. Ordinary sentences come back exactly as they went in."""
    if not text:
        return text
    hindi = (language == "hi") if language else bool(RE_DEVANAGARI.search(text))
    words = HI if hindi else EN
    out = text

    # LaTeX that slipped into the spoken line: \frac{a}{b}, \sqrt{x}, \pi.
    out = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", out)
    out = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", out)
    out = re.sub(r"\\(?:mathrm|text|mathbf)\s*\{([^{}]*)\}", r"\1", out)
    out = re.sub(r"\\([A-Za-z]+)", lambda m: f" {LATEX_WORDS.get(m.group(1), m.group(1))} ", out)
    out = out.replace("\\ ", " ").replace("$", "")

    # Chemistry before anything reads its digits: H₂SO₄ and H2SO4 alike.
    out = out.translate(SUBSCRIPTS)
    out = RE_FORMULA_WORD.sub(_formula, out)
    for arrow in ("<=>", "⇌", "⇄", "<->"):
        out = out.replace(arrow, f" {words['⇌']} ")
    out = re.sub(r"\s*(?:-+>|→|⟶|=>)\s*", f" {words['→']} ", out)

    # Units after a number, before "/" and "²" mean anything else.
    out = RE_UNIT.sub(lambda m: _unit(m, hindi), out)

    # Powers: x², x^2, x^{10}, 10⁻³.
    out = RE_SUPER_RUN.sub(lambda m: "^" + "".join(SUPERSCRIPTS[c] for c in m.group(0)), out)
    out = re.sub(r"([\w)\]]+)\s*\^\s*(\{[^{}]*\}|\(-?\w+\)|-?\w+)",
                 lambda m: _power(m.group(1), m.group(2), words), out)

    # Roots, before "√" is lost among the other symbols.
    out = re.sub(r"∛\s*\(?([\w.]+)\)?", lambda m: (f"{m.group(1)} {words['cube root']}" if hindi
                                                  else f"{words['cube root']} {m.group(1)}"), out)
    out = re.sub(r"√\s*\(([^()]*)\)|√\s*([\w.]+)",
                 lambda m: (f"{m.group(1) or m.group(2)} {words['root']}" if hindi
                            else f"{words['root']} {m.group(1) or m.group(2)}"), out)

    # Greek letters, glued or spaced: πr² is "pi r squared".
    for letter, name in GREEK.items():
        out = out.replace(letter, f" {name} ")
    out = re.sub(r"°", f" {words['°']}", out)
    out = re.sub(r"∠\s*", f"{words['angle']} ", out)
    out = re.sub(r"[△Δ]\s*(?=[A-Z])", f"{words['triangle']} ", out)
    out = out.replace("⊥", f" {words['perp']} ").replace("∥", f" {words['parallel']} ")

    # Point names in a sentence about geometry: "AB" is "A B", "ABC" "A B C".
    if RE_GEOMETRY.search(out):
        out = RE_POINT_NAMES.sub(lambda m: (" ".join(m.group(1))
                                            if m.group(1) not in COMMON_WORDS else m.group(1)),
                                 out)

    # The operators. Each only where it IS an operator -- between two maths
    # operands -- and never in a hyphenated word, a year range or a dash
    # between clauses.
    out = _operators(out, words)

    # Brackets the voice would only stumble over: "(x - 2)(x - 3)" is said
    # "x minus 2, times x minus 3".
    out = re.sub(r"\)\s*\(", f", {words['×']} ", out)
    out = re.sub(r"(?<=\w)\(", " ", out)
    out = re.sub(r"[()\[\]{}]", " ", out)
    out = out.replace(OPERAND, "")
    if not hindi:
        out = _variables(out)
    out = re.sub(r"\s+([,.!?।])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip()
