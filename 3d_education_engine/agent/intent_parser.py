"""Fast, offline understanding of the commands students say most.

"Rotate it", "zoom in", "cut it in half", "highlight the left ventricle",
"change the launch angle to 60 degrees" do not need a language model: they
are recognised here, in microseconds, with no network. What this parser does
not recognise goes to the LLM (when one is configured), which can only call
the same validated tools.

Each rule returns tool calls; the parser never touches the scene itself.
A small set of Hindi/Hinglish verbs is understood too ("dikhao", "ghumao"),
since the target users mix languages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent.tool_registry import ToolCall
from database.repository import normalise
from models.resolver import ModelResolver, strip_request
from visualization.clipping import PLANE_WORDS
from visualization.transparency import WORDS as OPACITY_WORDS

NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
                "forty": 40, "forty five": 45, "forty-five": 45, "fifty": 50, "sixty": 60, "seventy": 70,
                "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "one hundred": 100,
                "one eighty": 180, "hundred and eighty": 180}

HINGLISH = [
    (r"\b(?:dikhao|dikha\s+do|dikhaiye|dikhaao|दिखाओ|दिखा\s*दो|दिखाइए)\b", "show"),
    (r"\b(?:ghumao|ghuma\s+do|घुमाओ|घुमा\s*दो)\b", "rotate"),
    (r"\b(?:wapas|waapas|वापस|peeche\s+jao|पीछे\s*जाओ)\b", "go back"),
    (r"\b(?:andar|अंदर)\b", "inside"),
    (r"\b(?:kaato|kaat\s+do|काटो|काट\s*दो)\b", "cut"),
    (r"\b(?:bada\s+karo|बड़ा\s*करो|paas\s+lao)\b", "zoom in"),
    (r"\b(?:chhota\s+karo|छोटा\s*करो|door\s+karo)\b", "zoom out"),
    (r"\b(?:ruko|roko|रुको|रोको)\b", "stop"),
]

QUESTION_START = re.compile(r"^(?:what|why|how|which|where|when|who|whose|explain|describe|tell me|is|are|does|do|can|could|"
                            r"what's|whats|kya|kyon|kyun|kaise|क्या|क्यों|कैसे)\b")


@dataclass
class ParseContext:
    model_id: str | None = None
    model_name: str = ""
    part_phrases: dict[str, str] = field(default_factory=dict)      # "left ventricle" -> "left_ventricle"
    group_phrases: dict[str, str] = field(default_factory=dict)     # "four chambers" -> "chambers"
    sim_params: dict[str, str] = field(default_factory=dict)        # "launch angle" -> "angle"
    animations: list[str] = field(default_factory=list)
    is_simulation: bool = False
    is_molecule: bool = False
    is_atom: bool = False
    selected_part: str | None = None
    # Parts that open into a model of their own ("zoom into the nucleus").
    part_models: dict[str, str] = field(default_factory=dict)


@dataclass
class ParseResult:
    kind: str                            # command | question | model_request | unknown
    calls: list[ToolCall] = field(default_factory=list)
    question: str = ""
    topic: str | None = None             # model or part id the question is about
    reply_hint: str = ""                 # what to say if the calls succeed

    @property
    def understood(self) -> bool:
        return self.kind != "unknown"


def number_in(text: str) -> float | None:
    m = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    for word, value in sorted(NUMBER_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b", text):
            return float(value)
    return None


class IntentParser:
    def __init__(self, resolver: ModelResolver):
        self.resolver = resolver

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _clean(text: str) -> str:
        t = text.strip()
        for pattern, repl in HINGLISH:
            t = re.sub(pattern, repl, t, flags=re.IGNORECASE)
        t = re.sub(r"[?!.,;]+", " ", t.lower())
        t = re.sub(r"\b(?:please|pls|liza|jarvis|hey|ok|okay|now|can you|could you|would you|i want you to)\b", " ", t)
        return " ".join(t.split())

    @staticmethod
    def _find(text: str, phrases: dict[str, str]) -> tuple[str, str] | None:
        best = None
        for phrase, ref in phrases.items():
            if phrase and re.search(rf"\b{re.escape(phrase)}\b", text):
                if best is None or len(phrase) > len(best[0]):
                    best = (phrase, ref)
        return best

    def _part_ref(self, text: str, ctx: ParseContext) -> str | None:
        group = self._find(text, ctx.group_phrases)
        part = self._find(text, ctx.part_phrases)
        if group and (not part or len(group[0]) >= len(part[0])):
            return group[1]
        if part:
            return part[1]
        if re.search(r"\b(?:this part|that part|this one|that one|the part)\b", text) and ctx.selected_part:
            return ctx.selected_part
        return None

    @staticmethod
    def _target(text: str) -> bool:
        """Does the sentence point at the model/selection ('it', 'this', 'the model')?"""
        return bool(re.search(r"\b(?:it|this|that|the model|everything|whole thing|all of it|them)\b", text))

    # ------------------------------------------------------------ main
    def parse(self, raw: str, ctx: ParseContext) -> ParseResult:
        text = self._clean(raw)
        if not text:
            return ParseResult("unknown")
        # "rotate it and cut it open" / "ghumao aur andar se dikhao": several
        # commands in one breath. Used only when EVERY clause is a command,
        # so "compare water and carbon dioxide" is never split.
        clauses = [c.strip() for c in re.split(r"\b(?:and then|then|and|aur|phir|also)\b", text) if c.strip()]
        if len(clauses) > 1 and not QUESTION_START.match(text):
            results, last_part = [], None
            for clause in clauses:
                r = self._parse_clean(clause, clause, ctx)
                # "show the chambers and label them": "them" is the chambers.
                if last_part and re.search(r"\b(?:them|it|those|these|they)\b", clause):
                    for call in r.calls:
                        if call.arguments.get("part_id") == "all":
                            call.arguments["part_id"] = last_part
                parts = [c.arguments["part_id"] for c in r.calls
                         if c.arguments.get("part_id") not in (None, "all", "none")]
                last_part = parts[0] if parts else last_part
                results.append(r)
            if all(r.kind == "command" for r in results):
                return ParseResult("command", [call for r in results for call in r.calls])
        return self._parse_clean(text, raw, ctx)

    def _parse_clean(self, text: str, raw: str, ctx: ParseContext) -> ParseResult:
        for rule in (self._dive, self._navigation, self._reset, self._rotate, self._zoom, self._transparency, self._cut,
                     self._explode, self._labels, self._measure, self._animation, self._physics, self._chemistry,
                     self._hide_show_highlight, self._question, self._show_model):
            result = rule(text, raw, ctx)
            if result is not None:
                return result
        return ParseResult("unknown")

    # ------------------------------------------------------------ rules
    # "Zoom into the nucleus", "focus on the mitochondria", "go inside the
    # nucleus", and the Hinglish "nucleus ke andar jao" / "nucleus mein zoom
    # karo" (andar is already "inside" by now). Only a part that opens into a
    # model of its own is taken here; every other part falls through to the
    # rules below, which move the camera to it or cut it open as before.
    DIVE_BEFORE = re.compile(
        r"\b(?:zoom(?: in)? (?:into|in on|onto|on|to)|focus (?:on|in on)|go (?:inside|into|in)(?: of)?|"
        r"get inside|enter|look inside|see inside|show (?:me )?(?:what is )?inside(?: of)?|"
        r"take (?:me|us) (?:inside|into)(?: of)?|dive into|explore)\s+(?:the |a |an |this |that )?(.+)$")
    DIVE_AFTER = re.compile(
        r"^(?:show |take me )?(?:the |a |an |this |that )?(.+?)\s+(?:ke |ki |ka |mein |me |par |pe )?"
        r"(?:inside|zoom|focus)\b")

    def _dive(self, text, raw, ctx):
        if not ctx.part_models:
            return None
        for pattern in (self.DIVE_BEFORE, self.DIVE_AFTER):
            m = pattern.search(text)
            if not m:
                continue
            part = self._part_ref(m.group(1), ctx)
            if part in ctx.part_models:
                return ParseResult("command", [ToolCall("zoom_into", {"part_id": part})])
        return None

    def _navigation(self, text, raw, ctx):
        if re.fullmatch(r"(?:go\s+)?back|go back(?: please)?|previous(?: model)?|back to the (?:last|previous) (?:one|model)|undo that", text) \
                or re.search(r"\bgo (?:back|to the previous)\b", text):
            return ParseResult("command", [ToolCall("go_back")])
        if re.search(r"\b(?:go|zoom|move)\s+(?:one\s+)?(?:level\s+)?(?:deeper|down a level|smaller)\b|\bone level deeper\b|\bnext level\b|\bgo inside\b|\bgo smaller\b", text):
            return ParseResult("command", [ToolCall("go_deeper")])
        if re.search(r"\b(?:go|zoom)\s+(?:one\s+)?level\s+up\b|\bzoom out a level\b|\bbigger picture\b|\bgo up a level\b", text):
            return ParseResult("command", [ToolCall("go_up")])
        m = re.search(r"\buntil (?:i|we) (?:can )?see (?:a |an |the )?(.+)$", text)
        if m:
            return ParseResult("command", [ToolCall("zoom_to_level", {"target": m.group(1).strip()})])
        return None

    def _reset(self, text, raw, ctx):
        if re.fullmatch(r"(?:reset|reset (?:it|the view|the camera|view|camera|everything|all)|start again|original view|default view|front view|home view)", text):
            calls = [ToolCall("reset_camera")]
            if "everything" in text or "all" in text:
                calls = [ToolCall("clear_clip"), ToolCall("explode", {"amount": 0.0}), ToolCall("highlight", {"part_id": "none"}),
                         ToolCall("set_transparency", {"part_id": "all", "value": 1.0}), ToolCall("show_part", {"part_id": "all"}),
                         ToolCall("remove_label", {"label_id": "all"}), ToolCall("reset_camera")]
            return ParseResult("command", calls)
        return None

    def _rotate(self, text, raw, ctx):
        if not re.search(r"\b(?:rotate|turn|spin|twist|flip|revolve|rotate)\b", text) or re.search(r"\bturn (?:on|off)\b", text):
            return None
        n = number_in(text)
        deg = n if n is not None else (180.0 if re.search(r"\b(?:around|round|over|flip|back side|other side)\b", text) else 45.0)
        if re.search(r"\b(?:up|upwards|forward|towards me|top)\b", text):
            return ParseResult("command", [ToolCall("rotate", {"x": -deg})])
        if re.search(r"\b(?:down|downwards|backward|away)\b", text):
            return ParseResult("command", [ToolCall("rotate", {"x": deg})])
        if re.search(r"\b(?:left|anticlockwise|counterclockwise|anti clockwise)\b", text):
            return ParseResult("command", [ToolCall("rotate", {"y": -deg})])
        if re.search(r"\broll\b", text):
            return ParseResult("command", [ToolCall("rotate", {"z": deg})])
        return ParseResult("command", [ToolCall("rotate", {"y": deg})])

    def _zoom(self, text, raw, ctx):
        into = re.search(r"\bzoom (?:in )?(?:into|onto|on|to) (?:the )?(.+)$", text)
        if into:
            target = into.group(1)
            part = self._part_ref(target, ctx)
            if part:
                return ParseResult("command", [ToolCall("focus", {"part_id": part})])
            res = self.resolver.resolve(target)
            if res.ok and res.model_id != ctx.model_id:
                return ParseResult("command", [ToolCall("zoom_to_level", {"target": res.model_id})])
        if re.search(r"\b(?:zoom in|closer|bigger|enlarge|magnify|get closer|come closer)\b", text) \
                or re.fullmatch(r"zoom(?: it| this| that)?(?: more| again| a bit| a little| further)?", text):
            amount = 2.5 if re.search(r"\b(?:lot|much|way)\b", text) else 1.5
            return ParseResult("command", [ToolCall("zoom", {"amount": amount})])
        if re.search(r"\b(?:zoom out|further|farther|smaller|shrink|move back|back off)\b", text):
            amount = 0.4 if re.search(r"\b(?:lot|much|way)\b", text) else 0.67
            return ParseResult("command", [ToolCall("zoom", {"amount": amount})])
        return None

    def _transparency(self, text, raw, ctx):
        word = None
        for w in sorted(OPACITY_WORDS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(w)}\b", text):
                word = w
                break
        if word is None and "transparen" not in text and "opacity" not in text:
            return None
        if word is None:
            word = "transparent"
        value = OPACITY_WORDS[word]
        pct = re.search(r"(\d+)\s*(?:%|percent)", text)
        if pct:
            value = float(pct.group(1)) / 100.0
            if "transparen" in text:
                value = 1.0 - value
            value = min(1.0, max(0.0, value))
        part = self._part_ref(text, ctx) or "all"
        if part != "all" and re.search(r"\b(?:except|but|other than|everything else|the rest)\b", text):
            calls = [ToolCall("set_transparency", {"part_id": "all", "value": value}),
                     ToolCall("set_transparency", {"part_id": part, "value": 1.0})]
            return ParseResult("command", calls)
        return ParseResult("command", [ToolCall("set_transparency", {"part_id": part, "value": value})])

    def _cut(self, text, raw, ctx):
        if re.search(r"\b(?:uncut|remove the cut|undo the cut|close the cut|put (?:it )?back together|make it whole|whole again)\b", text):
            return ParseResult("command", [ToolCall("clear_clip"), ToolCall("explode", {"amount": 0.0})])
        if re.search(r"\b(?:inside|interior)\b", text) and not QUESTION_START.match(text):
            part = self._part_ref(text, ctx)
            if part:
                return ParseResult("command", [ToolCall("cut_through", {"part_id": part})])
            if ctx.model_id and re.search(r"\b(?:show|look|see)\b", text) and ctx.part_phrases:
                return ParseResult("command", [ToolCall("clip", {"plane": "y", "position": 0.5, "keep": "positive"})])
        if not re.search(r"\b(?:cut|slice|section|cross section|cross-section|dissect|open (?:it|the)|cut open|halve)\b", text):
            return None
        plane = "y"
        for word, axis in sorted(PLANE_WORDS.items(), key=lambda kv: -len(kv[0])):
            if word in text and word != "in half":
                plane = axis
                break
        part = self._part_ref(text, ctx)
        if part:
            return ParseResult("command", [ToolCall("cut_through", {"part_id": part, "plane": plane})])
        return ParseResult("command", [ToolCall("clip", {"plane": plane, "position": 0.5, "keep": "positive"})])

    def _explode(self, text, raw, ctx):
        if re.search(r"\b(?:assemble|put together|reassemble|collapse|unexplode)\b", text):
            return ParseResult("command", [ToolCall("explode", {"amount": 0.0})])
        if re.search(r"\b(?:explode|exploded view|pull (?:it |them )?apart|separate the parts|take (?:it )?apart|spread (?:it |them )?out)\b", text):
            return ParseResult("command", [ToolCall("explode", {"amount": 0.8})])
        return None

    def _labels(self, text, raw, ctx):
        if re.search(r"\b(?:remove|hide|clear|delete) (?:all )?(?:the )?labels?\b|\bno labels\b", text):
            return ParseResult("command", [ToolCall("remove_label", {"label_id": "all"})])
        if re.search(r"\b(?:label|labels|name the parts|show (?:the )?names)\b", text):
            part = self._part_ref(text, ctx) or "all"
            return ParseResult("command", [ToolCall("label_parts", {"part_id": part})])
        return None

    def _measure(self, text, raw, ctx):
        m = re.search(r"\b(?:measure|distance|how far)\b.*?\b(?:between|from)\b (?:the )?(.+?) (?:and|to) (?:the )?(.+)$", text)
        if not m:
            return None
        a, b = self._part_ref(m.group(1), ctx), self._part_ref(m.group(2), ctx)
        if a and b:
            return ParseResult("command", [ToolCall("measure_distance", {"point_a": a, "point_b": b})])
        return None

    def _animation(self, text, raw, ctx):
        if re.fullmatch(r"(?:stop|pause|freeze|hold on|wait|stop (?:it|the animation|the simulation|beating))", text):
            return ParseResult("command", [ToolCall("pause")])
        if re.search(r"\b(?:beat|beating|heartbeat|heart beat|pump|pumping)\b", text) and "heartbeat" in ctx.animations:
            return ParseResult("command", [ToolCall("animate", {"name": "heartbeat"})])
        if re.fullmatch(r"(?:play|start|run|go|animate|resume|continue|start (?:it|the simulation|the animation)|run (?:it|the simulation)|play (?:it|the animation|the reaction))", text):
            return ParseResult("command", [ToolCall("play")])
        if ctx.is_simulation and re.fullmatch(r"(?:reset|restart)(?: (?:it|the simulation))?", text):
            return ParseResult("command", [ToolCall("reset_simulation")])
        return None

    def _physics(self, text, raw, ctx):
        if not ctx.is_simulation:
            return None
        if re.search(r"\b(?:measurements?|results?|range|how far did|how high|time of flight|period)\b", text) and QUESTION_START.match(text):
            return ParseResult("command", [ToolCall("get_measurements")])
        m = re.search(r"\b(?:change|set|make|put|increase|decrease|raise|lower|reduce)\b (?:the )?(.+?) (?:to|=|at|by|as) (-?[\d.]+|\w+(?: \w+)?)", text)
        if m:
            name_phrase, value = m.group(1), number_in(m.group(2))
            key = self._find(name_phrase, ctx.sim_params)
            if key and value is not None:
                return ParseResult("command", [ToolCall("set_parameter", {"name": key[1], "value": value}), ToolCall("play")])
        m = re.search(r"\b(?:launch|throw|fire|shoot)\b.*?\bat (-?[\d.]+) ?(?:degrees|°)", text)
        if m and "angle" in ctx.sim_params.values():
            return ParseResult("command", [ToolCall("set_parameter", {"name": "angle", "value": float(m.group(1))}), ToolCall("play")])
        return None

    def _chemistry(self, text, raw, ctx):
        if re.search(r"\bbond angles?\b|\bangle between\b|\bshape of (?:the |this )?molecule\b|\bmolecular geometry\b", text):
            calls = []
            target = re.sub(r".*\bangle(?:s)?\b\s*(?:of|in)?\s*", "", text).strip()
            res = self.resolver.resolve(target) if target and target not in {"it", "this"} else None
            if res and res.ok and res.model_id != ctx.model_id and res.model_id.startswith("chemistry.molecule"):
                calls.append(ToolCall("load_model", {"model_id": res.model_id}))
            elif not ctx.is_molecule:
                return None
            return ParseResult("command", calls + [ToolCall("show_bond_angle")])
        if re.search(r"\belectron (?:configuration|arrangement|shells?)\b|\bshells\b|\bvalence electrons\b", text):
            calls = []
            m = re.search(r"\b(?:of|for|in) (?:a |an |the )?(\w+)(?: atom)?\b", text)
            if m:
                res = self.resolver.resolve(f"{m.group(1)} atom")
                if res.ok and res.model_id.startswith("chemistry.atom") and res.model_id != ctx.model_id:
                    calls.append(ToolCall("load_model", {"model_id": res.model_id}))
            if not calls and not ctx.is_atom:
                return None
            return ParseResult("command", calls + [ToolCall("electron_configuration")])
        styles = {"space filling": "space_filling", "space-filling": "space_filling", "spacefill": "space_filling",
                  "ball and stick": "ball_and_stick", "ball-and-stick": "ball_and_stick", "stick model": "sticks",
                  "sticks": "sticks", "backbone": "backbone", "ribbon": "backbone", "surface": "surface"}
        for word, style in styles.items():
            # The skeleton's "backbone" is a part to show, not a way to draw it.
            if word in ctx.part_phrases or word in ctx.group_phrases:
                continue
            if word in text and (ctx.is_molecule or style in {"backbone", "surface"}):
                return ParseResult("command", [ToolCall("set_representation", {"style": style})])
        m = re.search(r"\bcompare (?:the )?(.+?) (?:and|with|to|vs|versus) (?:the )?(.+)$", text)
        if m:
            ids = []
            for phrase in m.groups():
                res = self.resolver.resolve(phrase)
                if res.ok and res.model_id.startswith("chemistry.molecule"):
                    ids.append(res.model_id)
            if len(ids) == 2:
                return ParseResult("command", [ToolCall("compare_molecules", {"model_ids": ids})])
        return None

    def _hide_show_highlight(self, text, raw, ctx):
        if re.search(r"\b(?:unhighlight|clear (?:the )?highlight|no highlight|stop highlighting)\b", text):
            return ParseResult("command", [ToolCall("highlight", {"part_id": "none"})])
        if re.search(r"\bshow (?:me )?(?:all|everything|all (?:the )?parts|the whole (?:thing|model))\b", text) and not self._part_ref(text, ctx):
            return ParseResult("command", [ToolCall("show_part", {"part_id": "all"}), ToolCall("highlight", {"part_id": "none"})])
        part = self._part_ref(text, ctx)
        if part is None:
            return None
        if re.search(r"\b(?:hide|remove|take away|get rid of|turn off)\b", text):
            if re.search(r"\b(?:everything else|the rest|all (?:the )?others?|other parts|except)\b", text):
                return ParseResult("command", [ToolCall("isolate", {"part_id": part})], topic=part)
            return ParseResult("command", [ToolCall("hide_part", {"part_id": part})], topic=part)
        if re.search(r"\b(?:stand out|pop out|glow|bright)\b", text):
            return ParseResult("command", [ToolCall("highlight", {"part_id": part})], topic=part)
        if re.search(r"\b(?:only|just|isolate)\b", text) and re.search(r"\b(?:show|display|keep|leave|see|isolate)\b", text):
            return ParseResult("command", [ToolCall("isolate", {"part_id": part})], topic=part)
        if re.search(r"\b(?:highlight|point (?:out|to)|mark|emphasi[sz]e|where is|locate|find)\b", text):
            return ParseResult("command", [ToolCall("highlight", {"part_id": part}), ToolCall("label_parts", {"part_id": part})], topic=part)
        if re.search(r"\b(?:focus|centre|center)\b", text):
            return ParseResult("command", [ToolCall("focus", {"part_id": part})], topic=part)
        if re.search(r"\b(?:show|display|see|view)\b", text) and not QUESTION_START.match(text):
            return ParseResult("command", [ToolCall("show_part", {"part_id": part}), ToolCall("label_parts", {"part_id": part})], topic=part)
        return None

    def _question(self, text, raw, ctx):
        if not (QUESTION_START.match(text) or raw.strip().endswith("?")):
            return None
        part = self._part_ref(text, ctx)
        calls = [ToolCall("highlight", {"part_id": part})] if part and part in ctx.part_phrases.values() else []
        topic = f"{ctx.model_id}.{part}" if part and ctx.model_id else None
        if topic is None:
            res = self.resolver.resolve(strip_request(raw))
            topic = res.model_id if res.ok and res.score >= 0.9 else ctx.model_id
        return ParseResult("question", calls, question=raw.strip(), topic=topic)

    def _show_model(self, text, raw, ctx):
        if not re.search(r"\b(?:show|display|load|open|bring up|create|make|build|draw|visuali[sz]e|run|start|"
                         r"i want to see|let me see|give me|zoom into|go to)\b", text):
            return None
        phrase = strip_request(raw)
        if not phrase:
            return None
        res = self.resolver.resolve(phrase)
        if res.ok:
            if res.model_id == ctx.model_id:
                return ParseResult("command", [ToolCall("reset_camera")], reply_hint=f"{ctx.model_name} is already on screen.")
            return ParseResult("model_request", [ToolCall("model_change", {"model_id": res.model_id})], topic=res.model_id)
        return ParseResult("model_request", [ToolCall("find_or_acquire", {"query": phrase})])


def build_context(engine, registry, selected_part: str | None) -> ParseContext:
    """Assemble what the parser needs from the scene. Call on the UI thread."""
    ctx = ParseContext(selected_part=selected_part)
    scene = engine.scene
    if scene is None:
        return ctx
    ctx.model_id, ctx.model_name = scene.model_id, scene.title
    entry = registry.get(scene.model_id)
    for pid, part in scene.parts.items():
        ctx.part_phrases[normalise(part.name)] = pid
        ctx.part_phrases[normalise(pid)] = pid
        short = re.sub(r"\s*\(.*?\)", "", normalise(part.name)).strip()
        if short:
            ctx.part_phrases.setdefault(short, pid)
    if entry:
        for pid, spec in entry.parts.items():
            for alias in [spec["name"], *spec.get("aliases", [])]:
                ctx.part_phrases.setdefault(normalise(alias), pid)
        for gid, names in entry.manifest.get("group_aliases", {}).items():
            for n in names:
                ctx.group_phrases[normalise(n)] = gid
    for gid in engine.groups():
        ctx.group_phrases.setdefault(normalise(gid), gid)
        if gid.endswith("s"):
            ctx.group_phrases.setdefault(normalise(gid)[:-1], gid)
    for gid, names in scene.extras.get("group_aliases", {}).items():
        for n in names:
            ctx.group_phrases.setdefault(normalise(n), gid)
    ctx.animations = list(scene.animations)
    sim = scene.extras.get("simulation")
    if sim is not None:
        ctx.is_simulation = True
        for name, p in sim.params.items():
            ctx.sim_params[normalise(name)] = name
            ctx.sim_params[normalise(p.description)] = name
        for alias, name in sim.ALIASES.items():
            ctx.sim_params[normalise(alias)] = name
    ctx.is_molecule = scene.extras.get("molecule") is not None
    ctx.is_atom = scene.extras.get("atom") is not None
    if entry:
        ctx.part_models = {pid: target for pid, target in
                           entry.relationships.get("part_models", {}).items()
                           if pid in scene.parts and target in registry}
    return ctx
