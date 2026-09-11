"""Word and story banks for the Kindergarten screens.

Static content on purpose. A pre-reader gets the same twenty-odd words and the
same handful of stories every session, and that repetition is the point at this
age -- it is also the only version of this that works with no network. Generating
either from the LLM is the obvious fast-follow, not the first pass.

Everything here is read aloud through the existing Piper pipeline, so the text is
written to be SPOKEN: no punctuation a voice cannot read, no parentheses, no
markdown.
"""

import random
import re

# 3-5 letters, one clear syllable each, and every one of them a thing a small
# child can picture. Deliberately no homophones and no silent letters -- the
# child hears the word and taps the letters, so "knee" or "two" would be unfair.
SPELLING_WORDS = [
    {"word": "cat", "hint": "a pet that says meow"},
    {"word": "dog", "hint": "a pet that says woof"},
    {"word": "sun", "hint": "it shines in the sky in the daytime"},
    {"word": "hat", "hint": "you wear it on your head"},
    {"word": "cup", "hint": "you drink water from it"},
    {"word": "bus", "hint": "a big vehicle that takes you to school"},
    {"word": "pen", "hint": "you write with it"},
    {"word": "bed", "hint": "you sleep on it"},
    {"word": "egg", "hint": "a hen lays it"},
    {"word": "fan", "hint": "it spins and makes you cool"},
    {"word": "map", "hint": "it shows you the way"},
    {"word": "box", "hint": "you keep things inside it"},
    {"word": "key", "hint": "it opens a lock"},
    {"word": "leg", "hint": "you walk on two of them"},
    {"word": "fish", "hint": "it swims in the water"},
    {"word": "bird", "hint": "it has wings and it flies"},
    {"word": "milk", "hint": "a white drink that comes from a cow"},
    {"word": "tree", "hint": "it is tall and green and has leaves"},
    {"word": "star", "hint": "it twinkles in the sky at night"},
    {"word": "moon", "hint": "it glows in the sky at night"},
    {"word": "book", "hint": "you read stories in it"},
    {"word": "ball", "hint": "it is round and you play with it"},
    {"word": "door", "hint": "you open it to go inside a room"},
    {"word": "hand", "hint": "it has five fingers"},
    {"word": "rain", "hint": "water that falls from the clouds"},
    {"word": "shoe", "hint": "you wear it on your foot"},
    {"word": "cake", "hint": "a sweet treat on your birthday"},
    {"word": "frog", "hint": "a green animal that hops"},
    {"word": "nose", "hint": "you smell with it"},
    {"word": "lion", "hint": "a big cat that roars"},
]

# Said when the child gets a word right. Rotated so the same praise does not come
# back twice in a row, which is what makes it stop sounding like praise.
PRAISE = [
    "Yes! That's right. Well done.",
    "Perfect. You spelled it.",
    "That's it. Lovely work.",
    "Correct. You're very good at this.",
    "Yes! You got every letter.",
]

# Separate from PRAISE for the same reason STORY_PRAISE is: that list is about
# spelling. "Yes! You got every letter. It is sixteen." is what sharing it with
# the counting test produced.
COUNT_PRAISE = [
    "Yes! That's right.",
    "Perfect. You counted them all.",
    "That's it. Well counted.",
    "Correct! You got every one.",
]

# Separate from PRAISE because that list is about spelling -- "You spelled it",
# "You got every letter" -- and answering a question about a story is not
# spelling anything. Sharing one list produced "Perfect. You spelled it. You
# were listening carefully."
STORY_PRAISE = [
    "Yes! That's right.",
    "Well done. That's exactly right.",
    "Correct! You remembered it.",
    "That's right. Good listening.",
]

# Never says "wrong". The word is simply read back, spelled out, and offered
# again -- a five-year-old who hears "wrong" stops trying.
ENCOURAGEMENT = [
    "Nearly. Let's look at it together.",
    "Good try. Let me help you with this one.",
    "Almost there. Here it comes again.",
    "That was close. Let's try it once more.",
]

# Stories are told in BEATS, not as one block of text. Each beat carries the tone
# it should be delivered in, and those tone names map to Cartesia generation_config
# in assist.KG_EMOTIONS -- an emotion there is a generation parameter, so Liza
# SOUNDS excited rather than saying the word "excited". That distinction is the
# whole point: never write the feeling into the sentence.
#
# Beats are also where the pauses come from. Each one is a separate Cartesia
# request streamed into the same aplay, so the seam between two beats is a real
# breath, and a scary beat can slow down while the happy ending speeds up.
#
# Tones available: excited, curious, gentle, warm, proud, mysterious, amazed,
# sad, encouraging, storyteller.
STORIES = [
    {
        "title": "The Thirsty Crow",
        "segments": [
            ("One hot day, a crow was very thirsty.", "storyteller"),
            ("He looked everywhere for water.", "curious"),
            ("At last, he found a pot!", "excited"),
            ("But the water was right at the bottom. His beak could not reach it.",
             "sad"),
            ("The crow thought... and thought.", "mysterious"),
            ("Then he picked up a small stone, and dropped it into the pot.",
             "curious"),
            ("Then another. And another.", "curious"),
            ("Slowly, the water began to rise. Higher... and higher...", "amazed"),
            ("Until the crow could drink!", "excited"),
            ("He drank the cool water, and flew away happy.", "warm"),
        ],
        "question": "Did the crow drop stones into the pot?",
        "answer": True,
    },
    {
        "title": "The Lion and the Mouse",
        "segments": [
            ("A big lion was fast asleep under a tree.", "gentle"),
            ("A tiny mouse ran right over his paw, and woke him up!", "excited"),
            ("The lion caught the little mouse in his huge hand.", "mysterious"),
            ("Please let me go, squeaked the mouse. One day I will help you.",
             "gentle"),
            ("The lion laughed and laughed.", "excited"),
            ("But he opened his hand, and let the mouse go.", "warm"),
            ("Some days later, the lion was caught in a hunter's net.", "sad"),
            ("He roared and roared, but he could not get out.", "sad"),
            ("The little mouse heard him.", "mysterious"),
            ("She ran to the net, and chewed the ropes, until it fell apart!",
             "excited"),
            ("Even a small friend can be a big help.", "warm"),
        ],
        "question": "Did the mouse help the lion?",
        "answer": True,
    },
    {
        "title": "The Ant and the Grasshopper",
        "segments": [
            ("All summer long, the ant carried food home, a little at a time.",
             "storyteller"),
            ("The grasshopper sat in the sun and sang. Come and play!", "excited"),
            ("Winter is coming, said the ant. And she kept on working.", "gentle"),
            ("Then the cold came. The fields were bare.", "sad"),
            ("The grasshopper had nothing at all to eat.", "sad"),
            ("But the ant had a warm home, full of food.", "warm"),
            ("And she shared it with him.", "warm"),
            ("The grasshopper never forgot it.", "gentle"),
        ],
        "question": "Did the ant work hard all summer?",
        "answer": True,
    },
    {
        "title": "The Tortoise and the Hare",
        "segments": [
            ("The hare could run faster than anyone. And he loved to say so!",
             "excited"),
            ("Let us have a race, said the slow tortoise.", "gentle"),
            ("Everyone laughed!", "excited"),
            ("Off went the hare, so far ahead that he stopped for a nap.",
             "storyteller"),
            ("The tortoise walked on. Step... step... step.", "mysterious"),
            ("He never stopped once.", "gentle"),
            ("The hare woke up, and ran, and ran, all the way to the finish.",
             "excited"),
            ("But the tortoise was already there!", "amazed"),
            ("Slow and steady wins the race.", "warm"),
        ],
        "question": "Did the hare win the race?",
        "answer": False,
    },
    {
        "title": "The Boy Who Cried Wolf",
        "segments": [
            ("A boy looked after the sheep, high on a hill.", "storyteller"),
            ("He was bored. So he shouted, wolf! wolf!", "excited"),
            ("All the villagers came running up the hill to help him.", "excited"),
            ("But there was no wolf at all.", "curious"),
            ("The boy laughed and laughed.", "excited"),
            ("He did it again the next day. And again, they came running.",
             "storyteller"),
            ("And again... there was nothing.", "curious"),
            ("Then one evening, a real wolf came.", "mysterious"),
            ("The boy shouted. And shouted. And shouted.", "sad"),
            ("But nobody came. Because nobody believed him any more.", "sad"),
            ("Always tell the truth.", "gentle"),
        ],
        "question": "Did the villagers come when the real wolf arrived?",
        "answer": False,
    },
    {
        "title": "The Little Red Hen",
        "segments": [
            ("The little red hen found some grains of wheat.", "curious"),
            ("Who will help me plant these, she asked.", "gentle"),
            ("Not I, said the cat. Not I, said the dog. Not I, said the duck.",
             "storyteller"),
            ("So she planted them herself.", "gentle"),
            ("She watered them herself. She cut the wheat herself. She baked the "
             "bread herself.", "proud"),
            ("And when the warm bread came out, everyone wanted some!", "excited"),
            ("But the little red hen ate it with her chicks.", "warm"),
            ("Because they were the ones who helped.", "gentle"),
        ],
        "question": "Did the cat help the hen plant the wheat?",
        "answer": False,
    },
    {
        "title": "The Elephant and the Friends",
        "segments": [
            ("A lonely elephant walked through the forest, looking for a friend.",
             "sad"),
            ("Will you be my friend, he asked the monkey.", "gentle"),
            ("You are too big to swing with me, said the monkey.", "storyteller"),
            ("He asked the rabbit. He asked the frog. They all said the same.",
             "sad"),
            ("Then one day... a tiger came.", "mysterious"),
            ("All the animals were frightened!", "sad"),
            ("But the elephant walked up, tall and calm.", "storyteller"),
            ("And the tiger ran away!", "excited"),
            ("After that, every animal in the forest wanted the elephant as a "
             "friend.", "warm"),
        ],
        "question": "Did the elephant make friends in the end?",
        "answer": True,
    },
    {
        "title": "The Two Goats",
        "segments": [
            ("A narrow log lay across a fast, rushing river.", "storyteller"),
            ("One goat began to cross from this side.", "curious"),
            ("And another goat began to cross from that side.", "curious"),
            ("They met right in the middle!", "amazed"),
            ("Move, said one. You move, said the other!", "excited"),
            ("Neither one would go back.", "gentle"),
            ("So they pushed... and pushed...", "mysterious"),
            ("And both goats fell in the water with a splash!", "excited"),
            ("The next day, they crossed one at a time. And both stayed dry.",
             "warm"),
        ],
        "question": "Did both goats fall into the water?",
        "answer": True,
    },
]

# The on-screen text is the beats joined back together, so the story is written
# in exactly one place and the panel can never drift out of step with what she
# actually reads aloud.
for _story in STORIES:
    _story["text"] = " ".join(text for text, _tone in _story["segments"])


# What a child's spoken spelling comes back as, and how to read it.
#
# Whisper is being asked to transcribe letters said one at a time by a small
# child, which is close to its worst case. Observed shapes: "C A T", "C, A, T.",
# "see ay tee", and -- often -- just the word "cat", because the child answered
# the question they thought they were asked. So this reads generously and the
# caller treats a miss as a nudge rather than a mark: the WRITTEN stage that
# follows is the real assessment, and a four-year-old must never be told they
# are wrong because a microphone misheard them.
LETTER_NAMES = {
    "ay": "a", "aye": "a", "bee": "b", "be": "b", "see": "c", "sea": "c",
    # "and" for N is not a spelling of the letter's name -- it is what Whisper
    # returns for a child saying "N" in the middle of a word. logs/liza.log:
    # HAND came back as 'H A and D', which without this reads as H-A-A-N-D.
    "and": "n", "in": "n", "hen": "n", "ess": "s", "yes": "s",
    "cee": "c", "dee": "d", "de": "d", "ee": "e", "eff": "f", "ef": "f",
    "gee": "g", "aitch": "h", "haitch": "h", "eye": "i", "jay": "j",
    "kay": "k", "el": "l", "ell": "l", "em": "m", "en": "n", "oh": "o",
    "owe": "o", "pee": "p", "pea": "p", "cue": "q", "queue": "q", "ar": "r",
    "are": "r", "ess": "s", "es": "s", "tee": "t", "tea": "t", "you": "u",
    "yoo": "u", "vee": "v", "double you": "w", "doubleyou": "w", "dub": "w",
    "ex": "x", "eks": "x", "why": "y", "wye": "y", "zed": "z", "zee": "z",
}


def heard_spelling(text, target):
    """(verdict, letters) for a spoken spelling attempt.

    verdict is "correct", "said_the_word", "jumbled", "wrong" or "unclear".

    "jumbled" is its own verdict because it is the commonest mistake a child
    this age makes and it needs a different correction: "TAH" for "HAT" is not a
    child who does not know the letters, it is a child who has them in the wrong
    order, and telling them the letters again teaches nothing.
    """
    if not text:
        return "unclear", ""
    target = target.lower()
    tokens = re.findall(r"[A-Za-z]+", text.lower())
    if not tokens:
        return "unclear", ""

    # The child said the word instead of spelling it. Worth its own verdict so
    # she can ask for the letters rather than marking a right answer wrong.
    if len(tokens) == 1 and tokens[0] == target:
        return "said_the_word", target

    letters = []
    for token in tokens:
        if len(token) == 1:
            letters.append(token)
        elif token in LETTER_NAMES:
            letters.append(LETTER_NAMES[token])
        elif token == target:
            # "C A T cat" -- they spelled it and then said it. Ignore the word.
            continue
        else:
            # An unrecognised chunk: keep its letters, since Whisper often runs
            # a spelled word together as "cat" or "ceeayetee".
            letters.extend(token)
    attempt = "".join(letters)
    if not attempt:
        return "unclear", ""
    if attempt == target:
        return "correct", attempt
    if sorted(attempt) == sorted(target):
        return "jumbled", attempt
    if _one_edit_apart(attempt, target):
        return "near", attempt
    return "wrong", attempt


def _one_edit_apart(attempt, target):
    """True when one letter separates the two: swapped, missing, or extra.

    This is the shape a MICROPHONE error takes on this device, and the log has
    all three of it. A child spelling HAND came back as 'H A N B' three times
    running -- B for D, the same way every time -- and SHOE as 'H O E', with the
    S simply gone. A child who does not know a word does not misspell it
    identically three times; a microphone does.

    The caller decides what to do about it, and only starts believing it on the
    second attempt. See TutorUI._kg_judge_spoken.
    """
    if attempt == target or abs(len(attempt) - len(target)) > 1:
        return False
    if len(attempt) == len(target):
        return sum(a != b for a, b in zip(attempt, target)) == 1
    short, long = sorted((attempt, target), key=len)
    for cut in range(len(long)):
        if long[:cut] + long[cut + 1:] == short:
            return True
    return False


def random_word(exclude=None):
    """A word the child has not just had. Falls back to the whole bank once used up."""
    exclude = exclude or set()
    pool = [w for w in SPELLING_WORDS if w["word"] not in exclude] or SPELLING_WORDS
    return random.choice(pool)


def random_story(exclude=None):
    exclude = exclude or set()
    pool = [s for s in STORIES if s["title"] not in exclude] or STORIES
    return random.choice(pool)


def spell_out(word):
    """"cat" -> "C. A. T." -- full stops so the voice says letters, not a word.

    This is the WRITTEN form, for anything that goes on the screen. Use
    spell_out_spoken for anything that goes to the voice.
    """
    return " ".join(f"{letter.upper()}." for letter in word)


# How each English letter is SAID when it is read on its own.
#
# A single Latin letter is the worst input a multilingual voice can be given:
# there is nothing around it to say which language it is in, and on this device
# "A." came back as the Hindi आ, "E." as ई, and so on down the alphabet. The
# alphabet screen exists to teach a child what these letters SOUND like, so
# getting it wrong there is not a blemish, it is the lesson being taught
# backwards.
#
# Spelling out the English NAME of the letter takes the ambiguity away: "ay" is
# an English word and is read as one. It costs a phonetic spelling in the logs
# and in anything that echoes a spoken line, which is why this is separate from
# spell_out rather than replacing it.
LETTER_SOUNDS = {
    "A": "ay",   "B": "bee",  "C": "see",  "D": "dee",  "E": "ee",
    "F": "eff",  "G": "jee",  "H": "aitch", "I": "eye", "J": "jay",
    "K": "kay",  "L": "el",   "M": "em",   "N": "en",   "O": "oh",
    "P": "pee",  "Q": "cue",  "R": "aar",  "S": "ess",  "T": "tee",
    "U": "you",  "V": "vee",  "W": "double-you", "X": "eks",
    "Y": "why",  "Z": "zed",
}


def letter_sound(letter):
    """How to SAY one English letter. The letter itself if it is not one."""
    return LETTER_SOUNDS.get((letter or "").strip().upper(), letter)


def spell_out_spoken(word):
    """"cat" -> "see. ay. tee." -- the spelling as it should be HEARD."""
    return " ".join(f"{letter_sound(letter)}." for letter in word if letter.strip())


# ===========================================================================
# Alphabets
# ===========================================================================
# "A is for Apple" -- the letter, the word, and for Hindi the Latin reading of
# the letter so the label under it is useful to a parent who does not read
# Devanagari. Words are the ones Indian KG classes actually use, because a child
# who has seen the chart at school should recognise them here.
# (letter, word, picture). The picture NAMES A FILE in the A_Z artwork folder,
# which holds one drawing per letter; picture_image looks for it there as well
# as in pictures/, so a name here needs no path.
#
# The WORD is whichever one Indian classrooms actually teach, and the picture
# has to be that word -- not something adjacent. An earlier pass chose words for
# having a convenient emoji and got both wrong: J was "Juice" rather than Joker,
# N was "Nose" rather than Nest, and Q said "Queen" while showing a crown.
#
# With drawn artwork instead of emoji that constraint runs the other way: the
# folder is complete, so every letter has a picture, and the six words that had
# been picked to suit an emoji now follow the drawing that arrived for them --
# G is Grapes rather than Goat, J Juice rather than Joker, M Monkey rather than
# Moon, R Rabbit rather than Rainbow, T Tiger rather than Tree, and X the Xmas
# tree rather than an X-ray, which is what an Indian KG chart prints anyway.
#
# Two filenames are misspelled in the artwork ("rabit", "X-max"). They are
# written here exactly as they are on disk: a name that has to match a file is
# not the place to correct someone's spelling.
ENGLISH_ALPHABET = [
    ("A", "Apple", "apple.png"),        ("B", "Ball", "ball.png"),
    ("C", "Cat", "cat.png"),            ("D", "Dog", "dog.png"),
    ("E", "Elephant", "elephant.png"),  ("F", "Fish", "fish.png"),
    ("G", "Grapes", "grapes.png"),      ("H", "Hat", "hat.png"),
    ("I", "Ice cream", "Ice-Cream.png"), ("J", "Juice", "juice.png"),
    ("K", "Kite", "kite.png"),          ("L", "Lion", "lion.png"),
    ("M", "Monkey", "monkey.png"),      ("N", "Nest", "nest.png"),
    ("O", "Orange", "orange.png"),      ("P", "Parrot", "parrot.png"),
    ("Q", "Queen", "queen.png"),        ("R", "Rabbit", "rabit.png"),
    ("S", "Sun", "sun.png"),            ("T", "Tiger", "tiger.png"),
    ("U", "Umbrella", "umbrella.png"),  ("V", "Van", "van.png"),
    ("W", "Watch", "watch.png"),        ("X", "Xmas", "X-max.png"),
    ("Y", "Yak", "yak.png"),            ("Z", "Zebra", "zebra.png"),
]

# What the voice should say where it differs from what the card shows. "Xmas"
# is what the chart prints under the tree, and it is what a child will see
# written -- but read aloud it comes out "eks-mas", which teaches them a word
# that does not exist. The card keeps the spelling; the voice says the word.
SPOKEN_WORDS = {"Xmas": "Christmas tree"}


def spoken_word(word):
    """The word as it should be SAID. Same as the word, nearly always."""
    return SPOKEN_WORDS.get(word, word)

# स्वर -- the vowels, taught first.
HINDI_VOWELS = [
    ("अ", "अनार", "a", ""),            ("आ", "आम", "aa", "\U0001F96D"),
    ("इ", "इमली", "i", ""),            ("ई", "ईख", "ee", ""),
    ("उ", "उल्लू", "u", "\U0001F989"),  ("ऊ", "ऊन", "oo", "\U0001F9F6"),
    ("ए", "एड़ी", "e", "\U0001F9B6"),   ("ऐ", "ऐनक", "ai", "\U0001F453"),
    ("ओ", "ओखली", "o", ""),            ("औ", "औरत", "au", "\U0001F469"),
    ("अं", "अंगूर", "an", "\U0001F347"), ("अः", "अःहा", "ah", ""),
]

# व्यंजन -- the consonants. ङ, ञ and ण are left out on purpose: they have no
# everyday word a five-year-old would know, and no Indian KG chart teaches them
# with a picture. Including them would mean inventing an example.
HINDI_CONSONANTS = [
    ("क", "कबूतर", "ka", "\U0001F54A\uFE0F"), ("ख", "खरगोश", "kha", "\U0001F430"),
    ("ग", "गाय", "ga", "\U0001F404"),        ("घ", "घर", "gha", "\U0001F3E0"),
    ("च", "चम्मच", "cha", "\U0001F944"),      ("छ", "छाता", "chha", "\u2602\uFE0F"),
    ("ज", "जहाज़", "ja", "\U0001F6A2"),       ("झ", "झंडा", "jha", "\U0001F6A9"),
    ("ट", "टमाटर", "ta", "\U0001F345"),      ("ठ", "ठेला", "tha", "\U0001F6D2"),
    ("ड", "डमरू", "da", "\U0001F941"),       ("ढ", "ढोल", "dha", "\U0001FA98"),
    ("त", "तितली", "ta", "\U0001F98B"),      ("थ", "थैला", "tha", "\U0001F45C"),
    ("द", "दरवाज़ा", "da", "\U0001F6AA"),     ("ध", "धनुष", "dha", "\U0001F3F9"),
    ("न", "नल", "na", "\U0001F6B0"),         ("प", "पतंग", "pa", "\U0001FA81"),
    ("फ", "फल", "pha", "\U0001F34E"),        ("ब", "बकरी", "ba", "\U0001F410"),
    ("भ", "भालू", "bha", "\U0001F43B"),      ("म", "मछली", "ma", "\U0001F41F"),
    ("य", "यज्ञ", "ya", "\U0001F525"),        ("र", "रस्सी", "ra", "\U0001FAA2"),
    ("ल", "लट्टू", "la", ""),                 ("व", "वन", "va", "\U0001F333"),
    ("श", "शेर", "sha", "\U0001F981"),       ("ष", "षट्कोण", "sha", ""),
    ("स", "साँप", "sa", "\U0001F40D"),        ("ह", "हाथी", "ha", "\U0001F418"),
]

HINDI_ALPHABET = HINDI_VOWELS + HINDI_CONSONANTS

# ===========================================================================
# Counting
# ===========================================================================
# Hindi number names are irregular all the way up -- every one of these is its
# own word, none is derivable -- so they are listed rather than generated, and
# the list stops at 50. Beyond that the names stay irregular and getting one
# wrong in a teaching tool is worse than not offering it.
NUMBER_NAMES = [
    ("One", "एक"), ("Two", "दो"), ("Three", "तीन"), ("Four", "चार"),
    ("Five", "पाँच"), ("Six", "छह"), ("Seven", "सात"), ("Eight", "आठ"),
    ("Nine", "नौ"), ("Ten", "दस"), ("Eleven", "ग्यारह"), ("Twelve", "बारह"),
    ("Thirteen", "तेरह"), ("Fourteen", "चौदह"), ("Fifteen", "पंद्रह"),
    ("Sixteen", "सोलह"), ("Seventeen", "सत्रह"), ("Eighteen", "अठारह"),
    ("Nineteen", "उन्नीस"), ("Twenty", "बीस"), ("Twenty one", "इक्कीस"),
    ("Twenty two", "बाईस"), ("Twenty three", "तेईस"), ("Twenty four", "चौबीस"),
    ("Twenty five", "पच्चीस"), ("Twenty six", "छब्बीस"), ("Twenty seven", "सत्ताईस"),
    ("Twenty eight", "अट्ठाईस"), ("Twenty nine", "उनतीस"), ("Thirty", "तीस"),
    ("Thirty one", "इकतीस"), ("Thirty two", "बत्तीस"), ("Thirty three", "तैंतीस"),
    ("Thirty four", "चौंतीस"), ("Thirty five", "पैंतीस"), ("Thirty six", "छत्तीस"),
    ("Thirty seven", "सैंतीस"), ("Thirty eight", "अड़तीस"), ("Thirty nine", "उनतालीस"),
    ("Forty", "चालीस"), ("Forty one", "इकतालीस"), ("Forty two", "बयालीस"),
    ("Forty three", "तैंतालीस"), ("Forty four", "चौवालीस"), ("Forty five", "पैंतालीस"),
    ("Forty six", "छियालीस"), ("Forty seven", "सैंतालीस"), ("Forty eight", "अड़तालीस"),
    ("Forty nine", "उनचास"), ("Fifty", "पचास"),
]
COUNT_BLOCK = 20            # how many are learned before she asks to go further
# What the counting screen puts on the table to be counted.
COUNT_EMOJI = "\U0001F34E"
COUNT_MAX = len(NUMBER_NAMES)


def number_name(value, language="en"):
    """('Seven', 'सात') for 7. None outside the range we have names for."""
    if not 1 <= value <= COUNT_MAX:
        return None
    english, hindi = NUMBER_NAMES[value - 1]
    return hindi if language == "hi" else english


# ===========================================================================
# Hindi stories
# ===========================================================================
# The same stories a child hears in Hindi at home, told in Hindi rather than
# translated word-for-word from the English set -- a translated story reads like
# a translation when it is spoken aloud. Beats and tones work exactly as they do
# in English; detect_tts_language picks the Hindi voice from the script itself,
# so nothing here has to declare a language.
HINDI_STORIES = [
    {
        "title": "प्यासा कौआ",
        "segments": [
            ("एक गरम दिन था। एक कौआ बहुत प्यासा था।", "storyteller"),
            ("उसने हर जगह पानी ढूँढा।", "curious"),
            ("आख़िर उसे एक घड़ा मिल गया!", "excited"),
            ("पर पानी बिलकुल नीचे था। उसकी चोंच वहाँ तक नहीं पहुँची।", "sad"),
            ("कौए ने सोचा... और सोचा।", "mysterious"),
            ("फिर उसने एक छोटा पत्थर उठाया, और घड़े में डाल दिया।", "curious"),
            ("फिर एक और। फिर एक और।", "curious"),
            ("पानी धीरे धीरे ऊपर आने लगा। ऊपर... और ऊपर...", "amazed"),
            ("जब तक कौआ पानी पी नहीं सका!", "excited"),
            ("उसने ठंडा पानी पिया, और ख़ुशी ख़ुशी उड़ गया।", "warm"),
        ],
        "question": "क्या कौए ने घड़े में पत्थर डाले थे?",
        "answer": True,
    },
    {
        "title": "शेर और चूहा",
        "segments": [
            ("एक बड़ा शेर पेड़ के नीचे गहरी नींद में सो रहा था।", "gentle"),
            ("एक नन्हा चूहा उसके पंजे पर दौड़ गया, और शेर जाग गया!", "excited"),
            ("शेर ने चूहे को अपने बड़े पंजे में पकड़ लिया।", "mysterious"),
            ("मुझे छोड़ दो, चूहे ने कहा। एक दिन मैं आपकी मदद करूँगा।", "gentle"),
            ("शेर ज़ोर ज़ोर से हँसा।", "excited"),
            ("पर उसने पंजा खोल दिया, और चूहे को जाने दिया।", "warm"),
            ("कुछ दिन बाद, शेर एक शिकारी के जाल में फँस गया।", "sad"),
            ("उसने बहुत दहाड़ा, पर निकल नहीं पाया।", "sad"),
            ("नन्हे चूहे ने उसकी आवाज़ सुनी।", "mysterious"),
            ("वह दौड़कर आया, और रस्सियाँ कुतर दीं, जब तक जाल टूट नहीं गया!",
             "excited"),
            ("छोटा दोस्त भी बड़ी मदद कर सकता है।", "warm"),
        ],
        "question": "क्या चूहे ने शेर की मदद की?",
        "answer": True,
    },
    {
        "title": "कछुआ और खरगोश",
        "segments": [
            ("खरगोश सबसे तेज़ दौड़ता था। और उसे यह बताना बहुत पसंद था!", "excited"),
            ("चलो दौड़ लगाते हैं, धीमे कछुए ने कहा।", "gentle"),
            ("सब हँस पड़े!", "excited"),
            ("खरगोश इतना आगे निकल गया कि पेड़ के नीचे सो गया।", "storyteller"),
            ("कछुआ चलता रहा। धीरे... धीरे... धीरे।", "mysterious"),
            ("वह एक बार भी नहीं रुका।", "gentle"),
            ("खरगोश उठा, और दौड़ता हुआ आख़िरी लकीर तक पहुँचा।", "excited"),
            ("पर कछुआ तो पहले से वहाँ था!", "amazed"),
            ("धीरे चलो, पर रुको मत।", "warm"),
        ],
        "question": "क्या खरगोश दौड़ जीता?",
        "answer": False,
    },
    {
        "title": "चींटी और टिड्डा",
        "segments": [
            ("पूरी गर्मी चींटी थोड़ा थोड़ा खाना अपने घर ले जाती रही।", "storyteller"),
            ("टिड्डा धूप में बैठकर गाता रहा। आओ खेलें!", "excited"),
            ("सर्दी आने वाली है, चींटी ने कहा। और वह काम करती रही।", "gentle"),
            ("फिर सर्दी आ गई। खेत ख़ाली हो गए।", "sad"),
            ("टिड्डे के पास खाने को कुछ नहीं था।", "sad"),
            ("पर चींटी के घर में खाना भरा था।", "warm"),
            ("और उसने टिड्डे के साथ बाँट लिया।", "warm"),
            ("टिड्डा यह कभी नहीं भूला।", "gentle"),
        ],
        "question": "क्या चींटी ने पूरी गर्मी मेहनत की?",
        "answer": True,
    },
    {
        "title": "एकता में बल",
        "segments": [
            ("एक किसान के चार बेटे थे। वे हमेशा आपस में लड़ते रहते थे।", "sad"),
            ("एक दिन किसान ने लकड़ियों का एक गट्ठर मँगवाया।", "mysterious"),
            ("इसे तोड़ो, उसने कहा।", "gentle"),
            ("हर बेटे ने पूरी ताक़त लगाई। पर गट्ठर नहीं टूटा!", "sad"),
            ("फिर किसान ने गट्ठर खोल दिया, और एक एक लकड़ी दी।", "curious"),
            ("अब हर लकड़ी झट से टूट गई!", "excited"),
            ("देखो, किसान ने कहा। अलग अलग रहोगे तो कमज़ोर हो।", "gentle"),
            ("साथ रहोगे तो कोई तुम्हें नहीं तोड़ सकता।", "warm"),
        ],
        "question": "क्या पूरा गट्ठर टूट गया था?",
        "answer": False,
    },
]

for _story in HINDI_STORIES:
    _story["text"] = " ".join(text for text, _tone in _story["segments"])


def stories_for(language="en"):
    return HINDI_STORIES if language == "hi" else STORIES


def random_story_in(language="en", exclude=None):
    """A story the child has not just had, in the language they picked."""
    exclude = exclude or set()
    bank = stories_for(language)
    fresh = [s for s in bank if s["title"] not in exclude] or bank
    return random.choice(fresh)


# Spoken in the language of the story that was just told -- a Hindi story that
# ends with "Well done!" in English breaks the spell for the child listening.
HINDI_STORY_PRAISE = [
    "शाबाश! बिलकुल सही!",
    "बहुत बढ़िया! तुमने ध्यान से सुना।",
    "एकदम सही जवाब!",
    "वाह! तुम बहुत अच्छे श्रोता हो।",
]


def story_praise(language="en"):
    return random.choice(HINDI_STORY_PRAISE if language == "hi" else STORY_PRAISE)


def story_verdict(story, language="en"):
    """What she says when the comprehension answer was wrong."""
    if language == "hi":
        answer = "हाँ" if story["answer"] else "नहीं"
        return f"बिलकुल नहीं। {story['question']} इसका जवाब है {answer}।"
    answer = "yes" if story["answer"] else "no"
    return f"Not quite. {story['question']} The answer is {answer}."


# ===========================================================================
# Tests
# ===========================================================================
# The test screens show a picture and ask the child to NAME it, then to spell it
# (English) or say its first letter (Hindi -- spelling a word out letter by
# letter is not how Hindi is taught at this age, so the equivalent skill is
# knowing which अक्षर it begins with).
#
# Answer checking is deliberately generous. A five-year-old says "it's an apple",
# not "apple", and Whisper adds its own noise on top; marking that wrong would be
# testing the microphone rather than the child.
# "एक" is NOT in here, though it is the Hindi article: it is also the number
# one, so filtering it made a child answering "एक" to a picture of one apple
# score nothing. Leaving it in costs nothing -- matching asks whether the target
# is among the words said, not that every word said is the target.
_FILLER = {"its", "it's", "it", "is", "a", "an", "the", "this", "that",
           "yeh", "ye", "hai", "यह", "ये", "है"}


def _words(text):
    return [w for w in re.findall(r"[\wऀ-ॿ]+", (text or "").replace("-", " ").lower())
            if w not in _FILLER]


def spelling_target(word):
    """The letters of a word, for marking a spoken spelling: no spaces, no
    hyphens. "Ice cream" is spelled I-C-E-C-R-E-A-M by a child, and "Yo-yo"
    has no dash in it when it is said out loud."""
    return re.sub(r"[^a-z]", "", (word or "").lower())


def matches_answer(said, expected):
    """True when a spoken answer names `expected`, allowing for how children talk."""
    if not said or not expected:
        return False
    # A hyphen is spelling, not speech: nobody says the dash in "yo-yo", and
    # leaving it in meant the word could never match itself.
    target = expected.lower().strip().replace("-", " ")
    spoken = _words(said)
    if not spoken:
        return False
    # The whole phrase, or any single word of it, matching the target.
    if target in " ".join(spoken) or target in spoken:
        return True
    # "ice cream" against ["ice", "cream"], and the other way round.
    parts = [p for p in target.split() if p not in _FILLER]
    return bool(parts) and all(p in " ".join(spoken) for p in parts)


# Every way a number can arrive in a transcript: the digits, the English name
# and the Hindi name. Built once, longest form first, so "twenty one" is matched
# in preference to the "twenty" inside it.
_NUMBER_LOOKUP = None
# Neither \b nor \w behaves the way this needs across Devanagari and digits
# together, so the boundary is spelled out.
_NUMBER_EDGE = "0-9A-Za-z\u0900-\u097F"


def _number_lookup():
    global _NUMBER_LOOKUP
    if _NUMBER_LOOKUP is None:
        table = {}
        for index, (english, hindi) in enumerate(NUMBER_NAMES):
            value = index + 1
            table[english.lower()] = value
            table[hindi] = value
            table[str(value)] = value
        forms = sorted(table, key=len, reverse=True)
        pattern = re.compile(
            f"(?<![{_NUMBER_EDGE}])(?:" + "|".join(re.escape(f) for f in forms)
            + f")(?![{_NUMBER_EDGE}])")
        _NUMBER_LOOKUP = (table, pattern)
    return _NUMBER_LOOKUP


def spoken_numbers(said):
    """Every number in an utterance, in the order it was said.

    Digits and names, both languages, because children answer counting questions
    in whichever language is in their head whatever the question was asked in.
    """
    if not said:
        return []
    table, pattern = _number_lookup()
    return [table[match.group(0).lower()]
            for match in pattern.finditer(said.lower())]


def number_matches(said, value):
    """True when a spoken answer means `value`.

    The LAST number said is the answer, not any number in the sentence. A child
    asked how many apples there are does not reply "fifteen": they count, out
    loud, "one, two, three..." and the number they land on is what they mean.
    Matching anywhere in the utterance -- what this used to do -- marked that
    child correct on a question whose answer was three, because "three" went
    past on the way. Reading the last one marks what they actually decided.
    """
    numbers = spoken_numbers(said)
    return bool(numbers) and numbers[-1] == value


def test_questions(kind, count=5):
    """`count` questions for one test. Only entries that HAVE a picture are used
    -- the whole question is "what is this picture", so one without an image
    would be unanswerable."""
    if kind == "count":
        values = random.sample(range(1, min(21, COUNT_MAX + 1)), min(count, 20))
        # The second half of a counting question is one step off the number they
        # just counted -- and the step goes BOTH WAYS, chosen per question. Always
        # adding taught the child the shape of the question rather than the idea
        # behind it: after two of them they answer "one more than that" without
        # looking. Taking one away is the same idea run backwards and it is the
        # other half of what a KG child is learning.
        #
        # One apple never has one taken away: that lands on zero, which is a
        # harder idea than either of these and has no picture a child can count.
        return [{"kind": "count", "value": v,
                 "step": 1 if v < 2 else random.choice((1, -1))}
                for v in values]
    if kind == "hi":
        bank = [e for e in HINDI_ALPHABET if e[3]]
        picked = random.sample(bank, min(count, len(bank)))
        return [{"kind": "hi", "letter": e[0], "word": e[1], "picture": e[3]}
                for e in picked]
    # Anything the voice cannot read as it is written is left out. A test asks
    # for the word BACK -- named, then spelled -- and "Xmas" would be asked for
    # as "Christmas tree" and marked against the letters X-M-A-S. It is a fine
    # thing to show on a chart and an unfair thing to be tested on.
    bank = [e for e in ENGLISH_ALPHABET if e[2] and e[1] not in SPOKEN_WORDS]
    picked = random.sample(bank, min(count, len(bank)))
    return [{"kind": "en", "letter": e[0], "word": e[1], "picture": e[2]}
            for e in picked]
