"""The KG Workbook: worksheet pages a child does with their finger.

Everything else in the KG section is Liza showing and saying something. The
workbook is the other half of a KG class -- the child DOES the page: draws a
line from a letter to its picture, adds two groups of apples, colours a house,
puts the days of the week back in order. She reads the instructions aloud and
reacts to each tap, because the child this is for cannot read them.

The page content (what to ask, what counts as right) is the top half of this
file and has no Tk in it. The screens are the WorkbookScreens mixin at the
bottom, which TutorUI inherits; it uses only TutorUI's own drawing helpers, so
this module never imports ui and the two cannot import each other in a loop.
"""

import os
import random
import sys

import kg_content

# ===========================================================================
# Page content
# ===========================================================================

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
        "Sunday"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

# Things to count in the sums: (picture, one, many). Fruit and toys a KG child
# names without thinking, so the sum is the only new thing on the page.
COUNTERS = [
    ("\U0001F34E", "apple", "apples"),
    ("\U0001F388", "balloon", "balloons"),
    ("⭐", "star", "stars"),
    ("\U0001F41F", "fish", "fish"),
    ("\U0001F338", "flower", "flowers"),
    ("\U0001F34C", "banana", "bananas"),
    ("\U0001F986", "duck", "ducks"),
]

# The crayon box: (name, colour). Eight, because eight is what a KG crayon box
# has, and every name here is one they are learning anyway.
CRAYONS = [
    ("Red", "#EF4444"), ("Orange", "#F97316"), ("Yellow", "#FACC15"),
    ("Green", "#22C55E"), ("Blue", "#3B82F6"), ("Purple", "#A855F7"),
    ("Pink", "#F472B6"), ("Brown", "#92400E"),
]

# Colouring pages: real line drawings (black outlines on white), in
# UI/KG Activity/Colouring. Every one is public domain (Openclipart, CC0); see
# the README in that folder for where each came from. A tap fills the closed
# area under the finger, so any outline drawing works -- the earlier pictures
# were shapes drawn in code, and they looked it.
COLOURING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "UI", "KG Activity", "Colouring")
# (name, file) -- in the order "Next picture" goes through them: the boldest,
# simplest outlines first, for the youngest fingers.
PICTURES = [
    ("Kitty", "kitty.png"), ("Octopus", "octopus.png"),
    ("Duck", "duck.png"), ("Cloud", "cloud.png"),
    ("Butterfly", "butterfly.png"), ("Turtle", "turtle.png"),
    ("House", "house.png"), ("Bird", "bird.png"),
    ("Whale", "whale.png"), ("Dinosaur", "dinosaur.png"),
    ("Rocket", "rocket.png"), ("Ant", "ant.png"),
]

_page_cache = {}


def colouring_page(file, width, height):
    """(labels, regions, shade, outside) for one colouring page fitted into
    width x height.

    labels is an int32 array the size of the page: 1 on the black lines, and
    one number per closed area elsewhere. Working this out once per page is
    what makes a tap instant -- filling is then just "every pixel with this
    number", where a flood fill per tap took a third of a second on the Pi.

    shade is the page's own soft-edged lines (0..1), multiplied over the
    colours so the outline stays smooth instead of the stair-stepped edge of
    the labels. outside is the label of the paper round the drawing, which is
    left white: filled, it showed as a hard-edged coloured rectangle.
    None if the file cannot be read.
    """
    key = (file, width, height)
    if key in _page_cache:
        return _page_cache[key]
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    try:
        source = Image.open(os.path.join(COLOURING_DIR, file))
        source.load()
    except Exception as exc:
        print(f"[WORKBOOK] Could not open colouring page {file} ({exc}).",
              flush=True)
        _page_cache[key] = None
        return None
    # A transparent PNG is drawn onto white first, or its see-through
    # background comes out black and the whole page is one big line.
    if source.mode in ("RGBA", "LA", "P"):
        source = source.convert("RGBA")
        ground = Image.new("RGBA", source.size, (255, 255, 255, 255))
        source = Image.alpha_composite(ground, source)
    grey = source.convert("L")
    grey.thumbnail((width - 8, height - 8), Image.LANCZOS)
    page = Image.new("L", (width, height), 255)
    page.paste(grey, ((width - grey.width) // 2, (height - grey.height) // 2))
    # Lines thickened by a pixel each way: it closes the hairline gaps an
    # outline drawing always has, which otherwise let one tap flood the sky
    # into the cat, and a bold line is what a KG colouring book has anyway.
    soft = page.filter(ImageFilter.MinFilter(3))
    lines = soft.point(lambda v: 0 if v < 170 else 255)
    on_line = np.asarray(lines) == 0
    work = Image.fromarray(np.where(on_line, 1, 0).astype(np.int32), "I")
    label = 2
    while True:
        flat = np.asarray(work)
        free = np.flatnonzero(flat == 0)
        if free.size == 0:
            break
        y, x = divmod(int(free[0]), width)
        ImageDraw.floodfill(work, (x, y), label, thresh=0)
        label += 1
    labels = np.asarray(work).copy()
    shade = np.asarray(soft, dtype=np.float32)[..., None] / 255.0
    corners = [labels[0, 0], labels[0, -1], labels[-1, 0], labels[-1, -1]]
    outside = max(set(corners), key=corners.count)
    result = (labels, label, shade, int(outside))
    _page_cache[key] = result
    return result


QUESTIONS_PER_PAGE = 5

# Body parts: (name, picture, what we do with it). The use is None where the
# honest answer is "several of these" -- a mouth eats, but so do teeth and the
# tongue, and a question with two right answers marks a right child wrong.
# Legs are left out for the same reason: we walk with our legs AND our feet.
BODY_PARTS = [
    ("eyes", "\U0001F440", "see"),
    ("ears", "\U0001F442", "hear"),
    ("nose", "\U0001F443", "smell"),
    ("tongue", "\U0001F445", "taste"),
    ("teeth", "\U0001F9B7", "chew"),
    ("hand", "\u270B", "clap"),
    ("foot", "\U0001F9B6", "walk"),
    ("brain", "\U0001F9E0", "think"),
    ("mouth", "\U0001F444", None),
    ("arm", "\U0001F4AA", None),
]

# Words to complete, by topic: (word, picture). Days and months have no
# picture; their card says where they come in the week or year instead.
SPELLING_TOPICS = {
    "animals": ("Animals", "\U0001F436", "#059669", [
        ("dog", "\U0001F436"), ("cat", "\U0001F431"), ("cow", "\U0001F404"),
        ("pig", "\U0001F416"), ("hen", "\U0001F414"), ("fish", "\U0001F41F"),
        ("lion", "\U0001F981"), ("duck", "\U0001F986"), ("goat", "\U0001F410"),
        ("frog", "\U0001F438"), ("bear", "\U0001F43B"), ("tiger", "\U0001F42F"),
        ("horse", "\U0001F434"), ("zebra", "\U0001F993"), ("sheep", "\U0001F411"),
        ("snake", "\U0001F40D"), ("monkey", "\U0001F412"), ("rabbit", "\U0001F430"),
        ("parrot", "\U0001F99C"), ("giraffe", "\U0001F992"),
        ("elephant", "\U0001F418"),
    ]),
    "fruits": ("Fruits", "\U0001F34E", "#E11D48", [
        ("apple", "\U0001F34E"), ("mango", "\U0001F96D"), ("grapes", "\U0001F347"),
        ("banana", "\U0001F34C"), ("orange", "\U0001F34A"), ("lemon", "\U0001F34B"),
        ("pear", "\U0001F350"), ("cherry", "\U0001F352"), ("kiwi", "\U0001F95D"),
        ("pineapple", "\U0001F34D"), ("coconut", "\U0001F965"),
        ("peach", "\U0001F351"), ("melon", "\U0001F348"),
    ]),
    "days": ("Days", "\U0001F4C5", "#7C3AED", [(day, "") for day in DAYS]),
    "months": ("Months", "\U0001F5D3\uFE0F", "#0EA5E9",
               [(month, "") for month in MONTHS]),
}

ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _options(answer, low, high, count=3):
    """The right answer and its nearest neighbours, shuffled.

    Neighbours rather than random numbers: "2 + 3" with 9 among the answers is
    a question about which number is silly, not about adding.
    """
    near = [n for n in range(answer - 3, answer + 4)
            if n != answer and low <= n <= high]
    random.shuffle(near)
    picks = [answer] + sorted(near, key=lambda n: abs(n - answer))[:count - 1]
    random.shuffle(picks)
    return picks


def make_sums():
    """Five addition questions, a + b with both groups small enough to count."""
    out, seen = [], set()
    while len(out) < QUESTIONS_PER_PAGE:
        a, b = random.randint(1, 5), random.randint(1, 5)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        picture, one, many = random.choice(COUNTERS)
        out.append({"a": a, "b": b, "answer": a + b, "picture": picture,
                    "one": one, "many": many,
                    "options": _options(a + b, 1, 10)})
    return out


def make_take_aways():
    """Five subtraction questions. Never an answer of nought -- a KG child
    taking every apple away has not learned what is left."""
    out, seen = [], set()
    while len(out) < QUESTIONS_PER_PAGE:
        a = random.randint(2, 9)
        b = random.randint(1, a - 1)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        picture, one, many = random.choice(COUNTERS)
        out.append({"a": a, "b": b, "answer": a - b, "picture": picture,
                    "one": one, "many": many,
                    "options": _options(a - b, 1, 9)})
    return out


def make_missing_numbers():
    """Five rows of six numbers in order with one gone."""
    out = []
    starts = random.sample(range(1, 16), QUESTIONS_PER_PAGE)
    for start in starts:
        row = list(range(start, start + 6))
        gap = random.randint(1, 4)          # never the first or last
        answer = row[gap]
        out.append({"row": row, "gap": gap, "answer": answer,
                    "options": _options(answer, 1, 20)})
    return out


def match_set(language, can_show=lambda picture: True, count=4):
    """`count` (letter, word, picture) to match, each with a picture to show.

    `can_show` drops a letter whose picture will not draw. Without a picture
    the card falls back to writing the word -- and a written ओखली next to ओ
    is the answer, not a question."""
    if language == "hi":
        bank = [(letter, word, picture)
                for letter, word, _reading, picture in kg_content.HINDI_ALPHABET
                if picture]
    else:
        bank = list(kg_content.ENGLISH_ALPHABET)
    bank = [entry for entry in bank if can_show(entry[2])]
    return random.sample(bank, min(count, len(bank)))


def sequence_page(names, blanks):
    """(blank indices in order, tiles shuffled) for a days or months page."""
    holes = sorted(random.sample(range(len(names)), blanks))
    tiles = [names[i] for i in holes]
    random.shuffle(tiles)
    return holes, tiles


def make_body_questions():
    """Five body-part questions, mixing "tap the nose" with "what do we smell
    with?" -- the first is the word, the second is what it is FOR."""
    out = []
    used = random.sample([p for p in BODY_PARTS if p[2]], 3)
    found = random.sample([p for p in BODY_PARTS if p not in used], 2)
    for part, mode in [(p, "use") for p in used] + [(p, "find") for p in found]:
        others = random.sample([p for p in BODY_PARTS if p is not part
                                and (mode == "find" or p[2] != part[2])], 3)
        options = others + [part]
        random.shuffle(options)
        out.append({"part": part, "mode": mode, "options": options})
    random.shuffle(out)
    return out


def make_spelling(topic):
    """Five words from `topic`, each with letters to fill in.

    One gap in a word of up to four letters, two up to six, three past that.
    Never the first letter: it is the one the child hears most clearly when
    the word is said, and it is what makes the word recognisable on the page.
    """
    words = random.sample(SPELLING_TOPICS[topic][3], QUESTIONS_PER_PAGE)
    out = []
    for word, picture in words:
        letters = word.lower()
        gaps = 1 if len(letters) <= 4 else 2 if len(letters) <= 6 else 3
        holes = sorted(random.sample(range(1, len(letters)), gaps))
        missing = [letters[i] for i in holes]
        spare = random.sample([c for c in ALPHABET if c not in letters], 2)
        tiles = missing + spare
        random.shuffle(tiles)
        out.append({"word": word, "picture": picture, "holes": holes,
                    "tiles": tiles})
    return out


def plural(n, one, many):
    return one if n == 1 else many


def number_word(n):
    return (kg_content.number_name(n, "en") or str(n)).lower()


# ===========================================================================
# The screens
# ===========================================================================

PAPER = "#FFFDF5"
PAPER_EDGE = "#E7D9B0"
PAPER_RULE = "#F1E6C8"
INK = "#17408B"          # CLASS_BLUE, the classroom's navy
INK_SOFT = "#5B6B8C"
GOOD = "#16A34A"
MISS = "#FCA5A5"
CARD = "#FFFFFF"
CARD_EDGE = "#C7D2FE"
PICKED = "#FDE68A"
LINE_COLOURS = ["#EF4444", "#22C55E", "#3B82F6", "#A855F7", "#F97316"]
OUTLINE = "#334155"

CLASS_BG = ("KG Activity", "A_Z", "Class_bg.png")

# The eight pages on the workbook's cover: label, line under it, picture,
# colour, and the method that opens the page.
PAGES = [
    ("Match A B C", "Letters & pictures", "\U0001F34E", "#2563EB", "en_match"),
    ("अक्षर मिलाओ", "अक्षर और तस्वीर", "\U0001F989", "#DB2777", "hi_match"),
    ("Add", "2 + 3 = ?", "\U0001F388", "#059669", "add"),
    ("Take away", "5 - 2 = ?", "\U0001F41F", "#D97706", "take"),
    ("Colouring", "Fill in the colours", "\U0001F58D️", "#E11D48", "colour"),
    ("Days of the week", "Monday to Sunday", "\U0001F4C5", "#7C3AED", "days"),
    ("Months", "January to December", "\U0001F5D3️", "#0EA5E9", "months"),
    ("Missing numbers", "1, 2, ?, 4", "\U0001F522", "#4F46E5", "missing"),
    ("Body parts", "Eyes, ears, nose...", "\U0001F443", "#0D9488", "body"),
    ("Spell it", "Fill in the letters", "\u270F\uFE0F", "#9333EA", "spell"),
]


def _say(lines):
    """Speak (text, tone) lines as one response. kg is imported here, not at
    the top, because kg binds the assistant last and ui imports this module
    while it is still being built."""
    import kg
    kg.kg_say_many(lines)


class WorkbookScreens:
    """The workbook pages, mixed into TutorUI."""

    WB_ROW_Y0, WB_ROW_Y1 = 394, 442

    # ----- the page every screen is written on -----
    def _wbk_page(self, name, title, subtitle=None):
        """The classroom, with a workbook page held up over it."""
        self._overlay_screen(name, "", backdrop=CLASS_BG)
        self._wbk_token = getattr(self, "_wbk_token", 0) + 1
        tag = self.OVERLAY_TAG
        # A shadow, then the sheet, then its ruled lines and red margin, so it
        # reads as a page from an exercise book rather than as another card.
        self._round_rect(24, 16, 784, 388, 22, fill="#B8A57A", outline="",
                         tags=tag)
        self._round_rect(18, 10, 778, 382, 22, fill=PAPER, outline=PAPER_EDGE,
                         width=3, tags=tag)
        for y in range(104, 380, 34):
            self.canvas.create_line(30, y, 766, y, fill=PAPER_RULE, tags=tag)
        self.canvas.create_line(30, 76, 766, 76, fill=PAPER_EDGE, width=2,
                                tags=tag)
        for x in range(60, 760, 70):
            # The spiral binding along the top edge.
            self.canvas.create_oval(x - 5, 4, x + 5, 16, fill="#94A3B8",
                                    outline="#64748B", tags=tag)
        self._overlay_text(398, 36 if subtitle else 44, title, 19, bold=True,
                           fill=INK)
        if subtitle:
            self._overlay_text(398, 62, subtitle, 10, fill=INK_SOFT)

    def _wbk_after_speaking(self, callback, settle_ms=900):
        """Run `callback` once she has finished the line she is saying, unless
        the page has moved on by then (a tap on Next, Back, another answer)."""
        token = self._wbk_token

        def fire():
            if getattr(self, "_wbk_token", None) == token:
                callback()
        self._kg_after_speaking(fire, settle_ms)

    def _wbk_tagged(self, item, tag):
        if item is not None:
            self.canvas.addtag_withtag(tag, item)

    def _wbk_box(self, x0, y0, x1, y1, fill=CARD, outline=CARD_EDGE, width=3,
                radius=14, command=None):
        """A card on the page. With a command, the whole card is the target."""
        self._overlay_seq += 1
        tag = f"wbk{self._overlay_seq}"
        self._round_rect(x0, y0, x1, y1, radius, fill=fill, outline=outline,
                         width=width, tags=(self.OVERLAY_TAG, tag))
        if command is not None:
            self.canvas.tag_bind(tag, "<Button-1>",
                                 lambda e: self._overlay_press(e, command))
        return tag

    def _wbk_text(self, x, y, text, size, tag=None, bold=True, fill=INK):
        item = self._overlay_text(x, y, text, size, bold=bold, fill=fill)
        if tag:
            self._wbk_tagged(item, tag)
        return item

    # ----- the cover: choose a page -----
    def show_kg_workbook(self):
        self._wbk_page("kg_workbook", "My Workbook", "Pick a page to do")
        for index, (label, sub, picture, colour, key) in enumerate(PAGES):
            col, row = index % 5, index // 5
            x0, y0 = 34 + col * 148, 88 + row * 146
            x1, y1 = x0 + 138, y0 + 136
            tag = self._wbk_box(x0, y0, x1, y1, fill=colour, outline=colour,
                               radius=18,
                               command=lambda k=key: self._wbk_open(k))
            # A white disc behind the picture, so a red apple on a red tile
            # is still an apple.
            mid = (x0 + x1) / 2
            disc = self.canvas.create_oval(mid - 30, y0 + 10, mid + 30,
                                           y0 + 70, fill="#FFFFFF",
                                           outline="", tags=self.OVERLAY_TAG)
            self._wbk_tagged(disc, tag)
            item = self._place_emoji(picture, (x0 + x1) / 2, y0 + 40, 44)
            if item is None:
                item = self.canvas.create_text((x0 + x1) / 2, y0 + 40,
                                               text=sub[:4],
                                               font=self._font(16, True),
                                               fill=colour,
                                               tags=self.OVERLAY_TAG)
            self._wbk_tagged(item, tag)
            self._wbk_text((x0 + x1) / 2, y0 + 92, label, 11, tag,
                          fill="#FFFFFF")
            self._wbk_text((x0 + x1) / 2, y0 + 116, sub, 8, tag, bold=False,
                          fill="#FFFFFF")
        self._kg_class_button(self.KG_ROW_3[0], "Back", self.show_kg_home,
                              arrow="left")
        _say([("Here is your workbook!", "excited"),
              ("Which page would you like to do?", "curious")])

    def _wbk_open(self, key):
        {"en_match": lambda: self.show_wbk_match("en"),
         "hi_match": lambda: self.show_wbk_match("hi"),
         "add": lambda: self.show_wbk_numbers("add"),
         "take": lambda: self.show_wbk_numbers("take"),
         "missing": lambda: self.show_wbk_numbers("missing"),
         "colour": lambda: self.show_wbk_colouring(0),
         "days": lambda: self.show_wbk_sequence("days"),
         "months": lambda: self.show_wbk_sequence("months"),
         "body": self.show_wbk_body,
         "spell": self.show_wbk_spell_topics,
         }[key]()

    def _wbk_buttons(self, again_label, again, say_again, back=None):
        self._kg_class_button(self.KG_ROW_3[0], "Back",
                              back or self.show_kg_workbook, arrow="left")
        self._kg_class_button(self.KG_ROW_3[1], "Say it again", say_again,
                              primary=True)
        self._kg_class_button(self.KG_ROW_3[2], again_label, again,
                              arrow="right")

    def _wbk_page_done(self, topic, detail, score=None, out_of=None):
        self._kg_note("workbook", topic, detail=detail, score=score,
                      out_of=out_of, once=False)

    # ----- match the letter with its picture -----
    def show_wbk_match(self, language="en"):
        self._wbk_match = {"language": language,
                          "items": match_set(language, self._wbk_can_show),
                          "done": set(), "picked": None, "missed": 0}
        order = list(range(len(self._wbk_match["items"])))
        random.shuffle(order)
        self._wbk_match["order"] = order
        self._draw_wb_match()
        self._wbk_match_intro()

    def _wbk_can_show(self, picture):
        # picture_image lives in ui, which imports this module; reached through
        # the class's own module so nothing here imports ui.
        module = sys.modules[type(self).__module__]
        return module.picture_image(picture, 52) is not None

    def _wbk_match_intro(self):
        if self._wbk_match["language"] == "hi":
            _say([("हर अक्षर को उसकी तस्वीर से मिलाओ।", "encouraging"),
                  ("पहले अक्षर छुओ, फिर उसकी तस्वीर।", "curious")])
        else:
            _say([("Match each letter with its picture.", "encouraging"),
                  ("Tap a letter, then tap its picture.", "curious")])

    MATCH_ROW_Y = (112, 180, 248, 316)
    MATCH_LEFT = (96, 216)
    MATCH_RIGHT = (580, 700)

    def _draw_wb_match(self):
        state = self._wbk_match
        hindi = state["language"] == "hi"
        self._wbk_page("wb_match", "अक्षर मिलाओ" if hindi else "Match the Letters",
                      "अक्षर को उसकी तस्वीर से जोड़ो" if hindi
                      else "Join each letter to its picture")
        items = state["items"]
        lx0, lx1 = self.MATCH_LEFT
        rx0, rx1 = self.MATCH_RIGHT
        right_y = {}
        for slot, index in enumerate(state["order"]):
            right_y[index] = self.MATCH_ROW_Y[slot]
        # Lines first, so the cards sit on top of their ends.
        for n, index in enumerate(sorted(state["done"])):
            y0 = self.MATCH_ROW_Y[index]
            y1 = right_y[index]
            colour = LINE_COLOURS[index % len(LINE_COLOURS)]
            self.canvas.create_line(lx1, y0, rx0, y1, fill=colour, width=6,
                                    capstyle="round", tags=self.OVERLAY_TAG)
        for index, (letter, _word, _picture) in enumerate(items):
            y = self.MATCH_ROW_Y[index]
            done = index in state["done"]
            picked = state["picked"] == ("letter", index)
            tag = self._wbk_box(lx0, y - 30, lx1, y + 30,
                               fill=PICKED if picked else CARD,
                               outline=(LINE_COLOURS[index % 5] if done
                                        else CARD_EDGE),
                               width=4 if done or picked else 3,
                               command=lambda i=index: self._wbk_match_tap(
                                   "letter", i))
            self._wbk_text((lx0 + lx1) / 2, y,
                          letter if hindi else f"{letter} {letter.lower()}",
                          26, tag)
            self.canvas.create_oval(lx1 - 7, y - 7, lx1 + 7, y + 7,
                                    fill=INK, outline="", tags=self.OVERLAY_TAG)
        for index, (_letter, _word, picture) in enumerate(items):
            y = right_y[index]
            done = index in state["done"]
            picked = state["picked"] == ("picture", index)
            tag = self._wbk_box(rx0, y - 30, rx1, y + 30,
                               fill=PICKED if picked else CARD,
                               outline=(LINE_COLOURS[index % 5] if done
                                        else CARD_EDGE),
                               width=4 if done or picked else 3,
                               command=lambda i=index: self._wbk_match_tap(
                                   "picture", i))
            item = self._place_emoji(picture, (rx0 + rx1) / 2, y, 52)
            if item is None:
                item = self._overlay_text((rx0 + rx1) / 2, y, _word, 12,
                                          bold=True, fill=INK)
            self._wbk_tagged(item, tag)
            self.canvas.create_oval(rx0 - 7, y - 7, rx0 + 7, y + 7,
                                    fill=INK, outline="", tags=self.OVERLAY_TAG)
        if len(state["done"]) == len(items):
            self._wbk_text(398, 214, "शाबाश!" if hindi else "All matched!",
                          20, fill=GOOD)
        self._wbk_buttons("New page",
                         lambda: self.show_wbk_match(state["language"]),
                         self._wbk_match_intro)

    def _wbk_match_tap(self, side, index):
        state = self._wbk_match
        if index in state["done"]:
            return
        picked = state["picked"]
        if picked is None or picked[0] == side:
            # The first half of a pair -- or a change of mind on the same side.
            state["picked"] = (side, index)
            self._draw_wb_match()
            if side == "letter":
                letter, word, _ = state["items"][index]
                if state["language"] == "hi":
                    _say([(f"{letter}।", "curious")])
                else:
                    _say([(f"{kg_content.letter_sound(letter)}.", "curious")])
            return
        state["picked"] = None
        letter_i = index if side == "letter" else picked[1]
        picture_i = index if side == "picture" else picked[1]
        letter, _, _ = state["items"][letter_i]
        _, word, _ = state["items"][picture_i]
        hindi = state["language"] == "hi"
        if letter_i == picture_i:
            state["done"].add(letter_i)
            self._draw_wb_match()
            said = (f"{letter} से {word}।" if hindi else
                    f"{kg_content.letter_sound(letter)} for "
                    f"{kg_content.spoken_word(word)}!")
            if len(state["done"]) == len(state["items"]):
                praise = ("शाबाश! तुमने सब मिला दिए।" if hindi
                          else "Well done! You matched them all.")
                _say([(said, "excited"), (praise, "proud")])
                self._wbk_page_done(
                    "Match letters (Hindi)" if hindi else "Match letters",
                    "Matched " + ", ".join(i[0] for i in state["items"])
                    + " to their pictures",
                    score=len(state["items"]) - min(state["missed"],
                                                    len(state["items"])),
                    out_of=len(state["items"]))
            else:
                _say([(said, "warm")])
            return
        state["missed"] += 1
        self._draw_wb_match()
        if hindi:
            _say([(f"यह {word} है। यह {letter} से नहीं होता।", "gentle"),
                  ("फिर से कोशिश करो।", "encouraging")])
        else:
            _say([(f"That is {kg_content.spoken_word(word)}.", "gentle"),
                  (f"Find the picture for {kg_content.letter_sound(letter)}.",
                   "encouraging")])

    # ----- add, take away, missing numbers -----
    NUMBER_TITLES = {"add": ("Add Them Up", "How many altogether?"),
                     "take": ("Take Away", "How many are left?"),
                     "missing": ("Missing Numbers", "Which number is hiding?")}

    def show_wbk_numbers(self, kind):
        maker = {"add": make_sums, "take": make_take_aways,
                 "missing": make_missing_numbers}[kind]
        self._wbk_num = {"kind": kind, "qs": maker(), "at": 0, "score": 0,
                        "missed": set(), "solved": False}
        self._draw_wb_numbers()
        self._wbk_num_ask()

    def _wbk_num_q(self):
        return self._wbk_num["qs"][self._wbk_num["at"]]

    def _wbk_num_ask(self):
        state, q = self._wbk_num, self._wbk_num_q()
        kind = state["kind"]
        if kind == "add":
            _say([(f"{number_word(q['a']).capitalize()} {plural(q['a'], q['one'], q['many'])}"
                   f", and {number_word(q['b'])} more.", "curious"),
                  (f"How many {q['many']} altogether?", "encouraging")])
        elif kind == "take":
            gone = "goes" if q["b"] == 1 else "go"
            _say([(f"There are {number_word(q['a'])} "
                   f"{plural(q['a'], q['one'], q['many'])}.", "curious"),
                  (f"{number_word(q['b']).capitalize()} {gone} away.",
                   "mysterious"),
                  ("How many are left?", "encouraging")])
        else:
            _say([("Which number is missing?", "curious")])

    def _draw_wb_numbers(self):
        state, q = self._wbk_num, self._wbk_num_q()
        kind = state["kind"]
        title, sub = self.NUMBER_TITLES[kind]
        self._wbk_page("wb_numbers", title,
                      f"{sub}   Question {state['at'] + 1} of {len(state['qs'])}")
        tag = self.OVERLAY_TAG
        shown = str(q["answer"]) if state["solved"] else "?"
        shown_fill = GOOD if state["solved"] else INK
        if kind == "add":
            for (x0, x1), n in (((40, 280), q["a"]), ((340, 580), q["b"])):
                self._wbk_box(x0, 88, x1, 236)
                self._wbk_counters(q["picture"], n, (x0 + x1) / 2, 162, 3, 72)
                self._wbk_text((x0 + x1) / 2, 260, str(n), 22)
            self._wbk_text(310, 162, "+", 34)
            self._wbk_text(610, 162, "=", 34)
            self._wbk_box(640, 108, 760, 216,
                         outline=GOOD if state["solved"] else CARD_EDGE)
            self._wbk_text(700, 162, shown, 38, fill=shown_fill)
        elif kind == "take":
            self._wbk_box(40, 88, 530, 236)
            crossed = self._wbk_counters(q["picture"], q["a"], 285, 162, 5, 92)
            for cx, cy in crossed[q["a"] - q["b"]:]:
                self.canvas.create_line(cx - 24, cy - 24, cx + 24, cy + 24,
                                        fill="#DC2626", width=6, tags=tag)
                self.canvas.create_line(cx - 24, cy + 24, cx + 24, cy - 24,
                                        fill="#DC2626", width=6, tags=tag)
            self._wbk_text(285, 260, f"{q['a']} take away {q['b']}", 14,
                          fill=INK_SOFT)
            self._wbk_text(650, 128, f"{q['a']} − {q['b']}", 32)
            self._wbk_text(610, 196, "=", 30)
            self._wbk_box(640, 166, 740, 228,
                         outline=GOOD if state["solved"] else CARD_EDGE)
            self._wbk_text(690, 197, shown, 30, fill=shown_fill)
        else:
            width, gap = 96, 16
            x = 398 - (6 * width + 5 * gap) / 2
            for i, n in enumerate(q["row"]):
                hole = i == q["gap"]
                self._wbk_box(x, 118, x + width, 206,
                             fill=PICKED if hole and not state["solved"] else CARD,
                             outline=GOOD if hole and state["solved"] else CARD_EDGE)
                self._wbk_text(x + width / 2, 162, shown if hole else str(n), 30,
                              fill=shown_fill if hole else INK)
                x += width + gap
            self._wbk_text(398, 250, "Count along the row", 12, bold=False,
                          fill=INK_SOFT)
        # The answers to choose from.
        if not state["solved"]:
            width, gap = 120, 34
            x = 398 - (3 * width + 2 * gap) / 2
            for n in q["options"]:
                missed = n in state["missed"]
                self._overlay_button(x, 288, x + width, 362, str(n),
                                     None if missed else
                                     (lambda v=n: self._wbk_num_pick(v)),
                                     fill=MISS if missed else "#2563EB",
                                     text_colour="#FFFFFF", radius=18, size=26)
                x += width + gap
        else:
            self._wbk_text(398, 324, "Well done!", 20, fill=GOOD)
        self._wbk_buttons("Next" if not self._wbk_num_last() else "New page",
                         self._wbk_num_next, self._wbk_num_ask)

    def _wbk_counters(self, picture, n, cx, cy, per_row, spacing):
        """`n` pictures in rows of `per_row`, centred on (cx, cy). Returns
        their centres, so take-away can cross the last few out."""
        rows = [per_row] * (n // per_row) + ([n % per_row] if n % per_row else [])
        size = 54 if spacing < 80 else 60
        centres = []
        top = cy - (len(rows) - 1) * 36
        for r, count in enumerate(rows):
            y = top + r * 72
            x = cx - (count - 1) * spacing / 2
            for _ in range(count):
                if self._place_emoji(picture, x, y, size) is None:
                    self.canvas.create_oval(x - 20, y - 20, x + 20, y + 20,
                                            fill="#EF4444", outline="",
                                            tags=self.OVERLAY_TAG)
                centres.append((x, y))
                x += spacing
        return centres

    def _wbk_num_last(self):
        return self._wbk_num["at"] >= len(self._wbk_num["qs"]) - 1

    def _wbk_num_pick(self, value):
        state, q = self._wbk_num, self._wbk_num_q()
        if state["solved"]:
            return
        kind = state["kind"]
        if value == q["answer"]:
            state["solved"] = True
            if not state["missed"]:
                state["score"] += 1
            if kind == "add":
                line = (f"Yes! {number_word(q['a']).capitalize()} and {number_word(q['b'])}"
                        f" make {number_word(q['answer'])}.")
            elif kind == "take":
                line = (f"Yes! {number_word(q['a']).capitalize()} take away "
                        f"{number_word(q['b'])} leaves "
                        f"{number_word(q['answer'])}.")
            else:
                line = (f"Yes! {number_word(q['answer']).capitalize()} comes after "
                        f"{number_word(q['answer'] - 1)}.")
            self._draw_wb_numbers()
            _say([(line, "excited")])
            self._wbk_after_speaking(self._wbk_num_next)
            return
        state["missed"].add(value)
        self._draw_wb_numbers()
        if kind == "add":
            hint = f"Count all the {q['many']} together."
        elif kind == "take":
            hint = f"Count the {q['many']} that are not crossed out."
        else:
            hint = "Count along the row."
        _say([("Not quite.", "gentle"), (hint, "encouraging")])

    def _wbk_num_next(self):
        state = self._wbk_num
        if self._wbk_num_last():
            if state["solved"]:
                self._wbk_num_finish()
            else:
                self.show_wbk_numbers(state["kind"])
            return
        state["at"] += 1
        state["missed"] = set()
        state["solved"] = False
        self._draw_wb_numbers()
        self._wbk_num_ask()

    def _wbk_num_finish(self):
        state = self._wbk_num
        title, _ = self.NUMBER_TITLES[state["kind"]]
        self._wbk_finish(title, state["score"], len(state["qs"]),
                         lambda: self.show_wbk_numbers(state["kind"]))

    def _wbk_finish(self, title, stars, total, again, back=None):
        """The stars at the end of a five-question page."""
        self._wbk_page("wb_done", title, "Page finished!")
        y_star = 170
        x = 398 - (total - 1) * 42
        for i in range(total):
            if i >= stars or self._place_emoji("⭐", x, y_star, 64) is None:
                # An empty place: a ring, so the row still counts to five.
                self.canvas.create_oval(x - 26, y_star - 26, x + 26, y_star + 26,
                                        fill="#FEF3C7" if i < stars else "",
                                        outline="#F59E0B" if i < stars
                                        else "#CBD5E1", width=4,
                                        tags=self.OVERLAY_TAG)
            x += 84
        self._wbk_text(398, 256, f"You got {stars} out of {total} first time!",
                      18)
        self._kg_class_button(self.KG_ROW_3[0], "Back",
                              back or self.show_kg_workbook, arrow="left")
        self._kg_class_button(self.KG_ROW_3[2], "Do it again", again,
                              primary=True, arrow="right")
        self._wbk_page_done(title, f"{title}: {stars} of {total} first time",
                           score=stars, out_of=total)
        _say([("You finished the page!", "excited"),
              (f"You got {number_word(stars) if stars else 'none'} out of "
               f"{number_word(total)} right first time.", "proud")])

    # ----- colouring -----
    PAINT_BOX = (182, 82, 770, 378)
    PALETTE_X = (62, 124)

    def show_wbk_colouring(self, index=0):
        if not PICTURES:
            _say([("The colouring pictures are missing.", "gentle")])
            return
        index %= len(PICTURES)
        x0, y0, x1, y1 = self.PAINT_BOX
        page = colouring_page(PICTURES[index][1], x1 - x0, y1 - y0)
        if page is None:
            return
        self._wbk_paint = {"index": index, "fills": {}, "page": page,
                           "crayon": getattr(self, "_wbk_crayon", 0)}
        self._draw_wbk_colouring()
        name = PICTURES[index][0]
        _say([(f"Let's colour the {name.lower()}!", "excited"),
              ("Pick a crayon, then tap the picture.", "curious")])

    def _wbk_paint_image(self):
        """The page as it looks now: lines, and every area in its colour."""
        import numpy as np
        from PIL import Image, ImageTk
        state = self._wbk_paint
        labels, count, shade, _outside = state["page"]
        table = np.full((count + 1, 3), 255, dtype=np.uint8)
        for label, colour in state["fills"].items():
            table[label] = tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))
        rgb = (table[labels] * shade).astype(np.uint8)
        photo = ImageTk.PhotoImage(Image.fromarray(rgb, "RGB"))
        self._overlay_photos.append(photo)
        # Only the newest picture is ever on screen; keep the list from
        # growing by one bitmap per tap over a long colouring session.
        if len(self._overlay_photos) > 40:
            del self._overlay_photos[:-20]
        return photo

    def _draw_wbk_colouring(self):
        state = self._wbk_paint
        name = PICTURES[state["index"]][0]
        self._wbk_page("wb_colour", f"Colour the {name}",
                       "Pick a crayon, then tap the picture")
        # The crayon box down the left-hand side.
        for i, (cname, colour) in enumerate(CRAYONS):
            cx = self.PALETTE_X[i % 2]
            cy = 112 + (i // 2) * 70
            chosen = i == state["crayon"]
            self._overlay_seq += 1
            tag = f"wbkcrayon{self._overlay_seq}"
            if chosen:
                self.canvas.create_oval(cx - 32, cy - 32, cx + 32, cy + 32,
                                        fill="", outline=INK, width=4,
                                        tags=(self.OVERLAY_TAG, tag))
            self.canvas.create_oval(cx - 25, cy - 25, cx + 25, cy + 25,
                                    fill=colour, outline="#FFFFFF", width=3,
                                    tags=(self.OVERLAY_TAG, tag))
            self.canvas.tag_bind(tag, "<Button-1>",
                                 lambda e, n=i: self._overlay_press(
                                     e, lambda: self._wbk_pick_crayon(n)))
        self.canvas.create_line(170, 90, 170, 372, fill=PAPER_EDGE, width=2,
                                tags=self.OVERLAY_TAG)
        x0, y0, _x1, _y1 = self.PAINT_BOX
        self._overlay_seq += 1
        tag = f"wbkpaint{self._overlay_seq}"
        self._wbk_paint_item = self.canvas.create_image(
            x0, y0, anchor="nw", image=self._wbk_paint_image(),
            tags=(self.OVERLAY_TAG, tag))
        self.canvas.tag_bind(tag, "<Button-1>",
                             lambda e: self._overlay_press(
                                 e, lambda: self._wbk_paint_at(e.x, e.y)))
        self._kg_class_button(self.KG_ROW_4[0], "Back", self.show_kg_workbook,
                              arrow="left")
        self._kg_class_button(self.KG_ROW_4[1], "Clear",
                              lambda: self.show_wbk_colouring(state["index"]))
        self._kg_class_button(self.KG_ROW_4[2], "I'm done",
                              self._wbk_colouring_done, primary=True)
        self._kg_class_button(self.KG_ROW_4[3], "Next picture",
                              lambda: self.show_wbk_colouring(state["index"] + 1),
                              arrow="right", size=11)

    def _wbk_pick_crayon(self, n):
        self._wbk_paint["crayon"] = self._wbk_crayon = n
        self._draw_wbk_colouring()
        _say([(f"{CRAYONS[n][0]}!", "excited")])

    def _wbk_paint_at(self, x, y):
        state = self._wbk_paint
        labels, _count, _shade, outside = state["page"]
        x0, y0, _x1, _y1 = self.PAINT_BOX
        px, py = int(x - x0), int(y - y0)
        height, width = labels.shape
        if not (0 <= px < width and 0 <= py < height):
            return
        label = int(labels[py, px])
        if label == 1:
            # A fingertip is wider than the line it landed on: take the
            # nearest area instead of doing nothing.
            near = labels[max(0, py - 8):py + 9, max(0, px - 8):px + 9]
            choices = near[near != 1]
            if choices.size == 0:
                return
            values, counts = __import__("numpy").unique(choices,
                                                         return_counts=True)
            label = int(values[counts.argmax()])
        if label == outside:
            return
        state["fills"][label] = CRAYONS[state["crayon"]][1]
        try:
            self.canvas.itemconfigure(self._wbk_paint_item,
                                      image=self._wbk_paint_image())
        except Exception:
            self._draw_wbk_colouring()

    def _wbk_colouring_done(self):
        state = self._wbk_paint
        name = PICTURES[state["index"]][0]
        if not state["fills"]:
            _say([("Your picture is still empty!", "curious"),
                  ("Pick a crayon and tap the picture.", "encouraging")])
            return
        names = {colour: cname.lower() for cname, colour in CRAYONS}
        used = sorted({names[c] for c in state["fills"].values()})
        self._wbk_page_done("Colouring", f"Coloured the {name.lower()} with "
                            + ", ".join(used))
        _say([("What a beautiful picture!", "excited"),
              (f"You used {', '.join(used)}.", "proud"),
              ("Tap next picture for another one.", "warm")])

    # ----- days of the week, months of the year -----
    def show_wbk_sequence(self, kind):
        names = DAYS if kind == "days" else MONTHS
        holes, tiles = sequence_page(names, 3 if kind == "days" else 4)
        self._wbk_seq = {"kind": kind, "names": names, "holes": holes,
                        "tiles": tiles, "filled": set(), "missed": 0}
        self._draw_wb_sequence()
        self._wbk_seq_intro()

    def _wbk_seq_intro(self):
        if self._wbk_seq["kind"] == "days":
            _say([("There are seven days in a week.", "warm"),
                  ("Some days are missing. Put them back in order!",
                   "encouraging")])
        else:
            _say([("There are twelve months in a year.", "warm"),
                  ("Some months are missing. Put them back in order!",
                   "encouraging")])

    def _wbk_seq_next_hole(self):
        state = self._wbk_seq
        for hole in state["holes"]:
            if hole not in state["filled"]:
                return hole
        return None

    def _draw_wb_sequence(self):
        state = self._wbk_seq
        days = state["kind"] == "days"
        self._wbk_page("wb_seq",
                      "Days of the Week" if days else "Months of the Year",
                      "Tap the missing ones in order")
        names = state["names"]
        rows = [names[:4], names[4:]] if days else [names[0:4], names[4:8],
                                                     names[8:12]]
        width, gap = 168, 14
        height = 50 if days else 44
        top = 90 if days else 86
        step = 62 if days else 54
        nxt = self._wbk_seq_next_hole()
        i = 0
        for r, row in enumerate(rows):
            x = 398 - (len(row) * width + (len(row) - 1) * gap) / 2
            y = top + r * step
            for name in row:
                hole = i in state["holes"]
                shown = not hole or i in state["filled"]
                fill = (PICKED if i == nxt else
                        "#DCFCE7" if hole and shown else CARD)
                self._wbk_box(x, y, x + width, y + height, fill=fill,
                             outline=(INK if i == nxt else
                                      GOOD if hole and shown else CARD_EDGE),
                             width=4 if i == nxt else 3, radius=12)
                self._wbk_text(x + width / 2, y + height / 2,
                              name if shown else "?", 14 if shown else 18,
                              fill=GOOD if hole and shown else INK)
                x += width + gap
                i += 1
        remaining = [t for t in state["tiles"]
                     if names.index(t) not in state["filled"]]
        tile_y = 238 if days else 270
        if remaining:
            x = 398 - (len(remaining) * width + (len(remaining) - 1) * gap) / 2
            for name in remaining:
                self._overlay_button(x, tile_y, x + width, tile_y + 56, name,
                                     lambda n=name: self._wbk_seq_tap(n),
                                     fill="#7C3AED" if days else "#0EA5E9",
                                     text_colour="#FFFFFF", radius=16, size=14)
                x += width + gap
        else:
            self._wbk_text(398, tile_y + 28, "All in order!", 20, fill=GOOD)
        self._wbk_buttons("New page", lambda: self.show_wbk_sequence(state["kind"]),
                         self._wbk_seq_intro)

    def _wbk_seq_tap(self, name):
        state = self._wbk_seq
        hole = self._wbk_seq_next_hole()
        if hole is None:
            return
        expected = state["names"][hole]
        if name != expected:
            state["missed"] += 1
            before = (f"After {state['names'][hole - 1]} comes {expected}."
                      if hole else f"{expected} comes first.")
            _say([("Not that one.", "gentle"), (before, "curious")])
            return
        state["filled"].add(hole)
        self._draw_wb_sequence()
        if self._wbk_seq_next_hole() is None:
            what = ("days of the week" if state["kind"] == "days"
                    else "months of the year")
            self._wbk_page_done("Days of the week" if state["kind"] == "days"
                               else "Months of the year",
                               f"Put the missing {what} back in order",
                               score=max(0, len(state["holes"]) - state["missed"]),
                               out_of=len(state["holes"]))
            _say([(f"{name}!", "excited"), ("Well done! Let's say them all.",
                                            "proud"),
                  (", ".join(state["names"]) + ".", "warm")])
            return
        _say([(f"{name}!", "excited")])

    # ----- body parts -----
    def show_wbk_body(self):
        self._wbk_bodyq = {"qs": make_body_questions(), "at": 0, "score": 0,
                           "missed": set(), "solved": False}
        self._draw_wbk_body()
        self._wbk_body_ask()

    def _wbk_body_q(self):
        return self._wbk_bodyq["qs"][self._wbk_bodyq["at"]]

    def _wbk_body_prompt(self, q):
        name, _picture, use = q["part"]
        if q["mode"] == "use":
            return f"What do we {use} with?"
        return f"Tap the {name}."

    def _wbk_body_ask(self):
        _say([(self._wbk_body_prompt(self._wbk_body_q()), "curious")])

    def _draw_wbk_body(self):
        state, q = self._wbk_bodyq, self._wbk_body_q()
        self._wbk_page("wb_body", "My Body",
                       f"Question {state['at'] + 1} of {len(state['qs'])}")
        self._wbk_text(398, 112, self._wbk_body_prompt(q), 20)
        width, gap = 158, 22
        x = 398 - (4 * width + 3 * gap) / 2
        for part in q["options"]:
            name, picture, _use = part
            right = part is q["part"]
            missed = name in state["missed"]
            if state["solved"] and right:
                fill, edge = "#DCFCE7", GOOD
            elif missed:
                fill, edge = "#FEE2E2", MISS
            else:
                fill, edge = CARD, CARD_EDGE
            command = (None if state["solved"] or missed else
                       (lambda n=name: self._wbk_body_pick(n)))
            tag = self._wbk_box(x, 146, x + width, 352, fill=fill,
                                outline=edge, width=4, radius=18,
                                command=command)
            item = self._place_emoji(picture, x + width / 2, 226, 96)
            self._wbk_tagged(item, tag)
            self._wbk_text(x + width / 2, 318, name.capitalize(), 16, tag,
                           fill=GOOD if state["solved"] and right else INK)
            x += width + gap
        last = state["at"] >= len(state["qs"]) - 1
        self._wbk_buttons("Next" if not last else "Finish", self._wbk_body_next,
                          self._wbk_body_ask)

    def _wbk_body_pick(self, name):
        state, q = self._wbk_bodyq, self._wbk_body_q()
        if state["solved"]:
            return
        part, _picture, use = q["part"]
        if name == part:
            state["solved"] = True
            if not state["missed"]:
                state["score"] += 1
            self._draw_wbk_body()
            line = (f"Yes! We {use} with our {part}." if q["mode"] == "use"
                    else f"Yes! That is the {part}.")
            _say([(line, "excited")])
            self._wbk_after_speaking(self._wbk_body_next)
            return
        state["missed"].add(name)
        self._draw_wbk_body()
        _say([(f"That is the {name}.", "gentle"),
              (self._wbk_body_prompt(q), "encouraging")])

    def _wbk_body_next(self):
        state = self._wbk_bodyq
        if state["at"] >= len(state["qs"]) - 1:
            if state["solved"]:
                self._wbk_finish("My Body", state["score"], len(state["qs"]),
                                 self.show_wbk_body)
            else:
                self.show_wbk_body()
            return
        state["at"] += 1
        state["missed"] = set()
        state["solved"] = False
        self._draw_wbk_body()
        self._wbk_body_ask()

    # ----- complete the spelling -----
    def show_wbk_spell_topics(self):
        self._wbk_page("wb_spell_topics", "Spell It", "Pick what to spell")
        width, gap = 168, 14
        x = 398 - (4 * width + 3 * gap) / 2
        for key, (label, picture, colour, words) in SPELLING_TOPICS.items():
            tag = self._wbk_box(x, 108, x + width, 330, fill=colour,
                                outline=colour, radius=20,
                                command=lambda k=key: self.show_wbk_spell(k))
            disc = self.canvas.create_oval(x + width / 2 - 50, 130,
                                           x + width / 2 + 50, 230,
                                           fill="#FFFFFF", outline="",
                                           tags=self.OVERLAY_TAG)
            self._wbk_tagged(disc, tag)
            self._wbk_tagged(self._place_emoji(picture, x + width / 2, 180, 72),
                             tag)
            self._wbk_text(x + width / 2, 268, label, 18, tag, fill="#FFFFFF")
            sample = words[0][0] if key in ("days", "months") else words[1][0]
            self._wbk_text(x + width / 2, 300, sample.capitalize() + "...", 10,
                           tag, bold=False, fill="#FFFFFF")
            x += width + gap
        self._kg_class_button(self.KG_ROW_3[0], "Back", self.show_kg_workbook,
                              arrow="left")
        _say([("Let's spell some words!", "excited"),
              ("Animals, fruits, days, or months?", "curious")])

    def show_wbk_spell(self, topic):
        self._wbk_spl = {"topic": topic, "qs": make_spelling(topic), "at": 0,
                         "score": 0, "filled": set(), "missed": 0,
                         "wrong_here": 0}
        self._draw_wbk_spell()
        self._wbk_spell_ask()

    def _wbk_spell_q(self):
        return self._wbk_spl["qs"][self._wbk_spl["at"]]

    def _wbk_spell_next_hole(self):
        state, q = self._wbk_spl, self._wbk_spell_q()
        for hole in q["holes"]:
            if hole not in state["filled"]:
                return hole
        return None

    def _wbk_spell_ask(self):
        q = self._wbk_spell_q()
        _say([(f"{q['word'].capitalize()}.", "curious"),
              ("Fill in the missing letters.", "encouraging")])

    def _wbk_spell_clue(self, q):
        """What the card shows above a word with no picture."""
        word = q["word"]
        if word in DAYS:
            n = DAYS.index(word) + 1
            return f"Day {n} of the week"
        if word in MONTHS:
            return f"Month {MONTHS.index(word) + 1} of the year"
        return None

    def _draw_wbk_spell(self):
        state, q = self._wbk_spl, self._wbk_spell_q()
        label = SPELLING_TOPICS[state["topic"]][0]
        self._wbk_page("wb_spell", f"Spell It: {label}",
                       f"Word {state['at'] + 1} of {len(state['qs'])}")
        word = q["word"].upper()
        done = self._wbk_spell_next_hole() is None
        # The picture, or for a day or month the clue to which one it is.
        clue = self._wbk_spell_clue(q)
        if q["picture"]:
            self._place_emoji(q["picture"], 398, 128, 82)
        else:
            self._wbk_box(250, 98, 546, 158, fill="#EEF2FF", outline=CARD_EDGE)
            self._wbk_text(398, 128, clue, 16)
        # The word, one box per letter.
        width = min(64, (700 - (len(word) - 1) * 8) // len(word))
        gap = 8
        x = 398 - (len(word) * width + (len(word) - 1) * gap) / 2
        nxt = self._wbk_spell_next_hole()
        for i, letter in enumerate(word):
            hole = i in q["holes"]
            shown = not hole or i in state["filled"]
            self._wbk_box(x, 184, x + width, 254,
                          fill=(PICKED if i == nxt else
                                "#DCFCE7" if hole and shown else CARD),
                          outline=(INK if i == nxt else
                                   GOOD if hole and shown else CARD_EDGE),
                          width=4 if i == nxt else 3, radius=10)
            self._wbk_text(x + width / 2, 219, letter if shown else "_", 26,
                           fill=GOOD if hole and shown else INK)
            x += width + gap
        if done:
            self._wbk_text(398, 314, f"{q['word'].capitalize()}!  Well done!",
                           20, fill=GOOD)
        else:
            tiles = q["tiles"]
            width, gap = 76, 18
            x = 398 - (len(tiles) * width + (len(tiles) - 1) * gap) / 2
            for n, letter in enumerate(tiles):
                used = n in state.get("used_tiles", set())
                self._overlay_button(x, 282, x + width, 352, letter.upper(),
                                     None if used else
                                     (lambda n=n: self._wbk_spell_tap(n)),
                                     fill="#E5E7EB" if used else "#9333EA",
                                     text_colour="#FFFFFF", radius=16, size=24)
                x += width + gap
        last = state["at"] >= len(state["qs"]) - 1
        self._wbk_buttons("Next word" if not last else "Finish",
                          self._wbk_spell_next, self._wbk_spell_ask,
                          back=self.show_wbk_spell_topics)

    def _wbk_spell_tap(self, n):
        state, q = self._wbk_spl, self._wbk_spell_q()
        hole = self._wbk_spell_next_hole()
        if hole is None:
            return
        letter = q["tiles"][n]
        expected = q["word"].lower()[hole]
        if letter != expected:
            # The right letter for a LATER gap is still right: a child filling
            # "m_ng_" from the end has spelled mango, not made a mistake.
            later = [h for h in q["holes"] if h not in state["filled"]
                     and q["word"].lower()[h] == letter]
            if later:
                hole, expected = later[0], letter
        if letter != expected:
            state["missed"] += 1
            state["wrong_here"] += 1
            if state["wrong_here"] >= 2:
                # Twice wrong on the same gap: tell them, rather than leave a
                # four-year-old guessing through the alphabet.
                _say([("Not that one.", "gentle"),
                      (f"The missing letter is "
                       f"{kg_content.letter_sound(expected)}.", "warm")])
            else:
                _say([("Not that one.", "gentle"),
                      (f"Listen: {q['word']}. Try another letter.",
                       "encouraging")])
            return
        state["filled"].add(hole)
        state["wrong_here"] = 0
        state.setdefault("used_tiles", set()).add(n)
        self._draw_wbk_spell()
        if self._wbk_spell_next_hole() is None:
            if state["missed"] == 0:
                state["score"] += 1
            _say([(f"{q['word'].capitalize()}.", "excited"),
                  (kg_content.spell_out_spoken(q["word"]), "warm"),
                  (f"{q['word'].capitalize()}!", "proud")])
            self._wbk_after_speaking(self._wbk_spell_next, 1200)
            return
        _say([(f"{kg_content.letter_sound(letter)}.", "curious")])

    def _wbk_spell_next(self):
        state = self._wbk_spl
        if state["at"] >= len(state["qs"]) - 1:
            if self._wbk_spell_next_hole() is None:
                label = SPELLING_TOPICS[state["topic"]][0]
                self._wbk_finish(f"Spell It: {label}", state["score"],
                                 len(state["qs"]),
                                 lambda: self.show_wbk_spell(state["topic"]),
                                 back=self.show_wbk_spell_topics)
            else:
                self.show_wbk_spell(state["topic"])
            return
        state["at"] += 1
        state["filled"] = set()
        state["used_tiles"] = set()
        state["missed"] = 0
        state["wrong_here"] = 0
        self._draw_wbk_spell()
        self._wbk_spell_ask()
