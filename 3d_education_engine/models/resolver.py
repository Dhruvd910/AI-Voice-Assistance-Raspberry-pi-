"""User language -> canonical model id.

"heart", "human heart", "my heart", "cardiac organ" -> biology.anatomy.heart
"H2O", "water molecule"                               -> chemistry.molecule.water

The aliases themselves live in the manifests (name, aliases, topics), so a
new model brings its own vocabulary and this file never grows a table.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from database.repository import normalise
from models.registry import ModelRegistry

# Words that carry the request, not the thing requested.
RE_REQUEST = re.compile(
    r"^(?:(?:can|could|would|will)\s+you\s+|please\s+|now\s+|ok(?:ay)?\s+|and\s+)*"
    r"(?:show(?:\s+me)?|display|load|open|bring\s+up|give\s+me|let\s+me\s+see|"
    r"i\s+want\s+to\s+see|i'd\s+like\s+to\s+see|create|make|build|draw|render|run|start|"
    r"visuali[sz]e|go\s+to|switch\s+to|take\s+me\s+to|what\s+does)\s+",
    re.IGNORECASE)
RE_FILLER = re.compile(r"^(?:(?:a|an|the|my|our|some|one|model\s+of|3d\s+model\s+of|"
                       r"picture\s+of|simulation\s+of)\s+)+", re.IGNORECASE)
RE_TAIL = re.compile(r"\s+(?:please|now|for\s+me|in\s+3d|look\s+like|model|simulation)$",
                     re.IGNORECASE)
# A chemical formula as typed or transcribed: H2O, CO2, C6H12O6, NaCl.
RE_FORMULA = re.compile(r"\b(?:[A-Z][a-z]?\d*){1,8}\b")


@dataclass
class Resolution:
    model_id: str | None
    phrase: str
    matched: str = ""
    score: float = 0.0
    candidates: list[tuple[str, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.model_id is not None


def strip_request(text: str) -> str:
    phrase = text.strip().rstrip("?.!")
    previous = None
    while previous != phrase:
        previous = phrase
        phrase = RE_REQUEST.sub("", phrase)
        phrase = RE_FILLER.sub("", phrase)
        phrase = RE_TAIL.sub("", phrase)
    return phrase.strip()


class ModelResolver:
    def __init__(self, registry: ModelRegistry, fuzzy_cutoff: float = 0.84):
        self.registry = registry
        self.fuzzy_cutoff = fuzzy_cutoff

    def resolve(self, text: str) -> Resolution:
        phrase = strip_request(text)
        aliases = self.registry.aliases()
        # 1. A formula keeps its case until here: "CO" and "Co" are not the same.
        for formula in RE_FORMULA.findall(phrase):
            if any(ch.isdigit() for ch in formula) or formula in {"NaCl", "HCl", "O2", "N2"}:
                hit = aliases.get(normalise(formula))
                if hit and len(hit) == 1:
                    return Resolution(next(iter(hit)), phrase, formula, 1.0)
        key = normalise(phrase)
        # 2. Exact alias, then without "human"/"molecule"/"atom" style padding.
        for candidate in self._variants(key):
            hit = aliases.get(candidate)
            if hit:
                best = sorted(hit)[0]
                return Resolution(best, phrase, candidate, 1.0, [(m, 1.0) for m in sorted(hit)])
        # 3. The longest alias that appears as whole words inside the phrase.
        contained = [(a, ids) for a, ids in aliases.items()
                     if len(a) > 2 and re.search(rf"\b{re.escape(a)}\b", key)]
        if contained:
            alias, ids = max(contained, key=lambda x: len(x[0]))
            return Resolution(sorted(ids)[0], phrase, alias, 0.9, [(m, 0.9) for m in sorted(ids)])
        # 4. The phrase inside a longer name: "kidney" -> "left kidney". Only
        # for a real word, and the shortest such name wins.
        if len(key) >= 4:
            inside = [(a, ids) for a, ids in aliases.items() if re.search(rf"\b{re.escape(key)}\b", a)]
            if inside:
                alias, ids = min(inside, key=lambda x: (len(x[0]), x[0]))
                return Resolution(sorted(ids)[0], phrase, alias, 0.8, [(m, 0.8) for a, i in inside for m in i])
        # 5. Fuzzy: speech recognition misspells ("mitocondria").
        close = difflib.get_close_matches(key, list(aliases), n=3, cutoff=self.fuzzy_cutoff)
        if close:
            scored = [(sorted(aliases[a])[0], difflib.SequenceMatcher(None, key, a).ratio()) for a in close]
            return Resolution(scored[0][0], phrase, close[0], scored[0][1], scored)
        return Resolution(None, phrase)

    @staticmethod
    def _variants(key: str) -> list[str]:
        out = [key]
        for pad in ("human ", "the human ", "an ", "a "):
            if key.startswith(pad):
                out.append(key[len(pad):])
        for suffix in (" molecule", " atom", " cell", " organ", " structure", " system"):
            if key.endswith(suffix):
                out.append(key[: -len(suffix)])
        if key.endswith("s") and len(key) > 4:
            out.append(key[:-1])
        return out
