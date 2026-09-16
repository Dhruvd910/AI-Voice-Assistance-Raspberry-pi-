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
#
# HOW LONG A STORY IS, AND WHY IT IS THAT LONG. These used to run eight to
# eleven beats -- about ninety words, forty seconds -- and they read like the
# summary on the back of a book rather than like a story: the thing happened,
# then the moral. "The stories are too short" was the report, and it was right.
#
# They are now around thirty beats and three hundred words, which is two and a
# half to three minutes told aloud, and they are shaped the way a story is told
# to a small child rather than written down for one:
#   an OPENING that puts them somewhere ("on the hottest day of the summer"),
#   the character met before anything happens to them,
#   REPETITION, which is the engine of a story at this age -- "Not I, said the
#       cat. Not I, said the dog." -- and which also makes the beats easy to
#       follow on the board,
#   DIALOGUE, because a line spoken by somebody is the part a child repeats,
#   a pause held right before the turn ("And then... just as he was about to
#       give up..."), which is what the mysterious tone is for,
#   and the moral said plainly at the end, once, as its own beat.
# Beats stay to a sentence or two each: a beat is one Cartesia request, so it
# is also one breath, and a long one cannot change its tone in the middle.
STORIES = [
    {
        "title": "The Thirsty Crow",
        "segments": [
            ("Long, long ago, on the hottest day of the whole summer...",
             "storyteller"),
            ("the sun sat right in the middle of the sky, and would not move.",
             "storyteller"),
            ("The grass had turned brown. The river had dried into cracked mud.",
             "sad"),
            ("And high above it all flew a little black crow.", "curious"),
            ("His throat was dry. His wings were tired.", "sad"),
            ("Water, he said to himself. I must find water.", "gentle"),
            ("He flew over the fields. Nothing.", "curious"),
            ("He flew over the forest. Nothing there either.", "curious"),
            ("He flew and he flew, until he could hardly flap any more.",
             "sad"),
            ("And then... just as he was about to give up...", "mysterious"),
            ("he saw something in the corner of a garden.", "mysterious"),
            ("A pot! A big clay pot!", "excited"),
            ("The crow dropped down onto its rim, and looked inside.", "curious"),
            ("There WAS water. He could see it shining.", "amazed"),
            ("But it was right at the bottom, far, far down.", "sad"),
            ("He stretched his neck. He stretched and stretched.", "gentle"),
            ("His beak did not even come close.", "sad"),
            ("Now. A different bird might have flown away crying.", "storyteller"),
            ("But this crow sat very still on the rim of the pot...", "mysterious"),
            ("and he thought.", "mysterious"),
            ("And near his feet, in the dry earth, lay a heap of little stones.",
             "curious"),
            ("The crow picked one up in his beak. Plop! Into the pot.",
             "curious"),
            ("And the water moved. Just a tiny bit. But it moved!", "amazed"),
            ("So he picked up another. Plop!", "excited"),
            ("And another. Plop! And another. Plop, plop, plop!", "excited"),
            ("Higher... and higher... the water came climbing up the pot.",
             "amazed"),
            ("Until at last it touched the very tip of his beak.", "excited"),
            ("And the little crow drank, and drank, and drank.", "warm"),
            ("Then he shook out his feathers and flew off into the evening.",
             "warm"),
            ("He was not the biggest bird in the garden. He was not the "
             "strongest.", "gentle"),
            ("But when everybody else gave up, he stopped, and he thought. "
             "And that is how he won.", "warm"),
        ],
        "question": "Did the crow drop stones into the pot?",
        "answer": True,
    },
    {
        "title": "The Lion and the Mouse",
        "segments": [
            ("Deep in the forest, in the warm shade of a banyan tree, "
             "a lion was sleeping.", "gentle"),
            ("Not a small lion. The biggest lion in the whole forest.",
             "mysterious"),
            ("His paws were the size of dinner plates.", "amazed"),
            ("And every animal for miles around went very quietly past him.",
             "mysterious"),
            ("Every animal... except one.", "curious"),
            ("A tiny brown mouse came scampering through the grass, "
             "not looking where she was going at all.", "excited"),
            ("Over a root. Over a stone. And straight over the lion's paw!",
             "excited"),
            ("The lion's eyes opened.", "mysterious"),
            ("SLAP! Down came the great paw, and the mouse was caught.",
             "excited"),
            ("Well well, said the lion. Breakfast.", "mysterious"),
            ("Please! squeaked the mouse. Please let me go!", "sad"),
            ("I am only small. I would not even fill a corner of you.",
             "gentle"),
            ("And one day, I promise, I will help you.", "gentle"),
            ("The lion stared at her.", "curious"),
            ("Then he began to laugh.", "excited"),
            ("YOU? he roared. Help ME?", "excited"),
            ("He laughed so hard that the leaves shook off the tree.",
             "excited"),
            ("And because he was laughing, he lifted his paw... and the "
             "mouse ran free.", "warm"),
            ("Now. The rains came, and went. And the forest forgot all about it.",
             "storyteller"),
            ("Until one night, the hunters came.", "mysterious"),
            ("They spread a great rope net between the trees.", "mysterious"),
            ("And in the dark, the lion walked right into it.", "sad"),
            ("He pulled. He twisted. He roared and roared and roared.",
             "sad"),
            ("But the more he fought, the tighter the net held him.", "sad"),
            ("Far away, under a stone, a tiny brown mouse lifted her head.",
             "curious"),
            ("I know that voice, she said.", "mysterious"),
            ("She ran. Through the grass, over the roots, all the way to "
             "the net.", "excited"),
            ("Keep still, she told the lion. And she began to chew.",
             "gentle"),
            ("Chew, chew, chew. One rope. Then another. Then another.",
             "curious"),
            ("Until the whole net fell apart, and the great lion stepped out "
             "into the moonlight.", "amazed"),
            ("He looked down at the mouse. And this time, he did not laugh.",
             "warm"),
            ("A friend can be small, and still be exactly the friend you "
             "needed.", "warm"),
        ],
        "question": "Did the mouse help the lion?",
        "answer": True,
    },
    {
        "title": "The Ant and the Grasshopper",
        "segments": [
            ("It was summer, and the whole field was golden.", "warm"),
            ("The corn stood tall. The sun was soft. And the days went on "
             "for ever.", "warm"),
            ("On a wide green leaf sat a grasshopper with a fiddle.",
             "curious"),
            ("And he played. All morning, all afternoon, all evening.",
             "excited"),
            ("Tra la la! Nothing to do and nowhere to be!", "excited"),
            ("Below him, on the path, an ant went by.", "storyteller"),
            ("She was carrying a grain of corn twice the size of her head.",
             "amazed"),
            ("Good morning! called the grasshopper. Come up and play!",
             "excited"),
            ("I cannot, said the ant. I am taking this home.", "gentle"),
            ("Home? Whatever for? It is SUMMER!", "excited"),
            ("Winter is coming, said the ant. And she carried on walking.",
             "gentle"),
            ("The grasshopper laughed and went back to his fiddle.",
             "excited"),
            ("The next day, the ant went past again. And the day after that.",
             "storyteller"),
            ("Every single day, one grain at a time, all the way down into "
             "her little house under the ground.", "storyteller"),
            ("And every single day, the grasshopper played.", "excited"),
            ("Then, one morning, the wind changed.", "mysterious"),
            ("The gold went out of the field. The leaves came down.",
             "mysterious"),
            ("And the cold came in, sharp and white.", "sad"),
            ("The grasshopper looked for a seed. There were no seeds.",
             "sad"),
            ("He looked for a green leaf. There were no leaves at all.",
             "sad"),
            ("He was hungry, and he was cold, and his fiddle had gone quiet.",
             "sad"),
            ("At last he came to a small door at the foot of a tree, "
             "and knocked.", "gentle"),
            ("Inside, it was warm. There was a fire. And there was food, "
             "stacked from the floor to the ceiling.", "amazed"),
            ("The ant looked at him standing in the snow.", "gentle"),
            ("And she said: come in.", "warm"),
            ("She gave him soup, and a blanket, and a place by the fire.",
             "warm"),
            ("Then she said, kindly: next summer, work beside me in the "
             "mornings.", "gentle"),
            ("And play your fiddle for us both in the evenings.", "warm"),
            ("And that is exactly what he did.", "warm"),
            ("There is a time to play, and there is a time to get ready. "
             "The trick is knowing which one today is.", "gentle"),
        ],
        "question": "Did the ant work hard all summer?",
        "answer": True,
    },
    {
        "title": "The Tortoise and the Hare",
        "segments": [
            ("There was once a hare who could run like the wind.",
             "excited"),
            ("And he knew it. Oh, he knew it.", "storyteller"),
            ("Watch this! he would shout, and go tearing past the pond so "
             "fast the ducks spun round.", "excited"),
            ("Has anybody here ever beaten ME? he asked. Anybody at all?",
             "excited"),
            ("Nobody said a word. Because nobody ever had.", "gentle"),
            ("Then, from somewhere near the ground, came a slow, quiet voice.",
             "mysterious"),
            ("I will race you, it said.", "mysterious"),
            ("Everybody turned round. It was the tortoise.", "amazed"),
            ("The hare stared. Then he fell over laughing.", "excited"),
            ("YOU? You cannot even catch up with your own shadow!",
             "excited"),
            ("The whole field laughed with him.", "excited"),
            ("The tortoise waited until they had finished. Tomorrow morning, "
             "he said. To the big oak tree.", "gentle"),
            ("And so, the next morning, the animals lined the path.",
             "curious"),
            ("The fox dropped his handkerchief. And they were off!",
             "excited"),
            ("Well. The hare was gone before the handkerchief hit the ground.",
             "excited"),
            ("Over the stream. Through the hedge. Round the long bend.",
             "excited"),
            ("And the tortoise... lifted one foot... and put it down.",
             "gentle"),
            ("Halfway to the oak, the hare stopped and looked back.",
             "curious"),
            ("He could not even SEE the tortoise.", "amazed"),
            ("There is a patch of clover here, he thought. And it is warm.",
             "gentle"),
            ("I shall have a little rest. I have hours.", "gentle"),
            ("And he lay down in the clover, and shut his eyes.",
             "mysterious"),
            ("Step. Step. Step. Step.", "mysterious"),
            ("The tortoise came round the bend.", "curious"),
            ("He went past the clover, and he did not stop.", "curious"),
            ("Up the hill. He did not stop.", "curious"),
            ("Past the stone. He did not stop.", "curious"),
            ("A cheer woke the hare up.", "amazed"),
            ("He jumped to his feet and ran, faster than he had ever run in "
             "his life!", "excited"),
            ("But when he came flying round the last corner...", "excited"),
            ("the tortoise was already sitting under the oak tree.",
             "amazed"),
            ("Slow and steady wins the race. Because slow and steady never "
             "stops.", "warm"),
        ],
        "question": "Did the hare win the race?",
        "answer": False,
    },
    {
        "title": "The Boy Who Cried Wolf",
        "segments": [
            ("High on a green hill above a little village, a boy looked "
             "after the sheep.", "storyteller"),
            ("Every morning he walked them up. Every evening he walked "
             "them down.", "storyteller"),
            ("And in between... nothing happened at all.", "gentle"),
            ("The sheep ate grass. The clouds went by. The boy was bored "
             "to bits.", "sad"),
            ("Then, one afternoon, he had an idea.", "mysterious"),
            ("He stood up on the very top of the hill...", "mysterious"),
            ("and he shouted: WOLF! WOLF! A wolf is eating the sheep!",
             "excited"),
            ("And in the village below, everybody dropped everything.",
             "excited"),
            ("The baker came running with flour on his hands.", "excited"),
            ("The farmer came running with his stick.", "excited"),
            ("Even the old grandmother came puffing up the hill.",
             "excited"),
            ("And when they got to the top... there was no wolf.",
             "curious"),
            ("There was only a boy, rolling on the grass, laughing at them.",
             "excited"),
            ("That was not kind, said the farmer. And they walked back down.",
             "gentle"),
            ("A few days later, the boy was bored again.", "gentle"),
            ("WOLF! he shouted. WOLF! This time it is REALLY a wolf!",
             "excited"),
            ("And up they came again. The baker. The farmer. The "
             "grandmother.", "excited"),
            ("And again... there was nothing there at all.", "curious"),
            ("This time nobody laughed with him. They just looked at him, "
             "and turned round, and went home.", "sad"),
            ("Now.", "mysterious"),
            ("One evening, when the sun was going down behind the hill...",
             "mysterious"),
            ("the grass moved.", "mysterious"),
            ("And out of the long shadows came a wolf.", "sad"),
            ("A real one. Grey, and thin, and quiet.", "sad"),
            ("The boy's voice came out very small. Then it came out very "
             "big.", "sad"),
            ("WOLF! he screamed. PLEASE! THERE IS A WOLF!", "sad"),
            ("Down in the village, the baker heard him. And carried on "
             "baking.", "sad"),
            ("The farmer heard him. And carried on eating his supper.",
             "sad"),
            ("The grandmother heard him, and shook her head, and shut the "
             "window.", "sad"),
            ("Nobody came. Not one of them.", "sad"),
            ("The boy sat down on the hill in the dark, with the sheep "
             "scattered all over it.", "sad"),
            ("It was not his voice they had stopped believing. It was HIM.",
             "gentle"),
            ("That is what a lie costs. Not the first time. The time you "
             "really need somebody.", "gentle"),
        ],
        "question": "Did the villagers come when the real wolf arrived?",
        "answer": False,
    },
    {
        "title": "The Little Red Hen",
        "segments": [
            ("In a yard behind a farmhouse lived a little red hen, "
             "and three friends.", "storyteller"),
            ("A cat, who liked sleeping in the sun.", "gentle"),
            ("A dog, who liked sleeping in the shade.", "gentle"),
            ("And a duck, who liked sleeping just about anywhere.",
             "gentle"),
            ("One morning, scratching about in the dust, the hen found "
             "something.", "curious"),
            ("Grains of wheat! A whole handful of them!", "excited"),
            ("Look! she said. If we plant these, we shall have bread!",
             "excited"),
            ("Who will help me plant them?", "curious"),
            ("Not I, said the cat, and rolled over.", "gentle"),
            ("Not I, said the dog, and shut one eye.", "gentle"),
            ("Not I, said the duck, and said nothing else.", "gentle"),
            ("Then I shall plant them myself, said the little red hen.",
             "proud"),
            ("And she did.", "proud"),
            ("The rain came. The green shoots came. The wheat grew tall "
             "and gold.", "warm"),
            ("Who will help me cut the wheat? asked the hen.", "curious"),
            ("Not I. Not I. Not I.", "gentle"),
            ("Then I shall cut it myself. And she did.", "proud"),
            ("Who will help me carry it to the mill?", "curious"),
            ("Not I. Not I. Not I.", "gentle"),
            ("Then I shall carry it myself. And she did. All the way, "
             "there and back.", "proud"),
            ("Who will help me bake the bread?", "curious"),
            ("Not I. Not I. Not I.", "gentle"),
            ("Then I shall bake it myself. And she did.", "proud"),
            ("And oh, the smell that came out of that kitchen.", "amazed"),
            ("Warm bread. Fresh bread. The best bread in the world.",
             "amazed"),
            ("The cat woke up. The dog opened both eyes. The duck came "
             "running.", "excited"),
            ("I will help you EAT it! said the cat.", "excited"),
            ("So will I! said the dog.", "excited"),
            ("And me! said the duck.", "excited"),
            ("The little red hen looked at the three of them.", "gentle"),
            ("No, she said. You would not plant it, or cut it, or carry "
             "it, or bake it.", "gentle"),
            ("And she sat down with her chicks, who had helped her all "
             "along, and they ate every crumb.", "warm"),
            ("If you want a share of the bread, be there for the work.",
             "gentle"),
        ],
        "question": "Did the cat help the hen plant the wheat?",
        "answer": False,
    },
    {
        "title": "The Elephant and the Friends",
        "segments": [
            ("A young elephant came walking into a new forest one morning.",
             "curious"),
            ("She had nobody at all to talk to, and she badly wanted a "
             "friend.", "sad"),
            ("So she went and found the monkey, swinging in the branches.",
             "curious"),
            ("Will you be my friend? she asked.", "gentle"),
            ("The monkey looked her up and down.", "curious"),
            ("You? You are far too big to swing with me. You would break "
             "the tree.", "storyteller"),
            ("So the elephant went to the rabbit.", "gentle"),
            ("Will you be my friend?", "gentle"),
            ("You are far too big to fit in my burrow, said the rabbit.",
             "storyteller"),
            ("So she went to the frog by the pond.", "gentle"),
            ("Will you be my friend?", "gentle"),
            ("You are far too big to jump, said the frog. Sorry.",
             "storyteller"),
            ("Everywhere she went, it was the same. Too big. Too big. "
             "Too big.", "sad"),
            ("And the elephant sat down by herself at the edge of the "
             "forest, and felt very small indeed.", "sad"),
            ("Then, one evening, the birds stopped singing.", "mysterious"),
            ("All at once, the whole forest went quiet.", "mysterious"),
            ("A tiger had come down from the hills.", "mysterious"),
            ("The monkey froze in his tree. The rabbit shook in her "
             "burrow. The frog could not move at all.", "sad"),
            ("The tiger walked into the clearing, and he was not in any "
             "hurry.", "mysterious"),
            ("And then the ground began to shake.", "amazed"),
            ("Thud. Thud. Thud.", "amazed"),
            ("The elephant came out of the trees, tall and wide and "
             "perfectly calm.", "proud"),
            ("She did not shout. She did not run.", "gentle"),
            ("She simply stood in front of her forest, and looked at him.",
             "proud"),
            ("And the tiger turned, and went back up the hill, and did not "
             "come down again.", "excited"),
            ("One by one, the animals came out.", "curious"),
            ("The monkey. The rabbit. The frog.", "curious"),
            ("We are sorry, said the monkey. We only ever looked at your "
             "size.", "gentle"),
            ("Will YOU be OUR friend?", "gentle"),
            ("And the elephant smiled and said yes, because she had wanted "
             "that all along.", "warm"),
            ("The thing they thought was wrong with her was the very thing "
             "that saved them.", "warm"),
        ],
        "question": "Did the elephant make friends in the end?",
        "answer": True,
    },
    {
        "title": "The Two Goats",
        "segments": [
            ("Between two hills ran a river, fast and white and very deep.",
             "storyteller"),
            ("And across that river lay one narrow log.", "storyteller"),
            ("It was just wide enough for one goat.", "mysterious"),
            ("One morning, a white goat came down the hill on this side.",
             "curious"),
            ("And at the very same moment, a black goat came down the hill "
             "on that side.", "curious"),
            ("Neither of them looked up.", "gentle"),
            ("The white goat stepped onto the log.", "curious"),
            ("The black goat stepped onto the log.", "curious"),
            ("Clip. Clop. Clip. Clop.", "mysterious"),
            ("And they met. Right in the middle.", "amazed"),
            ("Nose to nose, over the roaring water.", "amazed"),
            ("Move, said the white goat.", "excited"),
            ("YOU move, said the black goat.", "excited"),
            ("I was on first!", "excited"),
            ("No you were NOT!", "excited"),
            ("Now, either one of them could have stepped back. It was only "
             "a few steps.", "gentle"),
            ("But neither one would.", "gentle"),
            ("They put their heads down.", "mysterious"),
            ("They pushed.", "mysterious"),
            ("And they pushed.", "mysterious"),
            ("And the log rolled...", "mysterious"),
            ("SPLASH!", "excited"),
            ("Two very wet goats came out of that river, one on each bank, "
             "shivering.", "sad"),
            ("And neither one of them had got across at all.", "sad"),
            ("The next morning, they came down the hills again.",
             "storyteller"),
            ("And this time, the white goat stopped, and lay down flat on "
             "the log.", "gentle"),
            ("And the black goat stepped carefully over her, and walked on.",
             "gentle"),
            ("Then the white goat got up and crossed too.", "warm"),
            ("Both of them got where they were going. And both of them "
             "stayed dry.", "warm"),
            ("Giving way is not losing. Sometimes it is the only way "
             "anybody gets across.", "warm"),
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
    ("अ", "अनार", "a", "anar.png"),    ("आ", "आम", "aa", "\U0001F96D"),
    ("इ", "इमली", "i", ""),            ("ई", "ईख", "ee", ""),
    ("उ", "उल्लू", "u", "\U0001F989"),  ("ऊ", "ऊन", "oo", "\U0001F9F6"),
    ("ए", "एड़ी", "e", "\U0001F9B6"),   ("ऐ", "ऐनक", "ai", "\U0001F453"),
    ("ओ", "ओखली", "o", "okhli.png"),   ("औ", "औरत", "au", "aurat.png"),
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
    ("न", "नल", "na", "nal.png"),            ("प", "पतंग", "pa", "\U0001FA81"),
    ("फ", "फल", "pha", "\U0001F34E"),        ("ब", "बकरी", "ba", "\U0001F410"),
    ("भ", "भालू", "bha", "\U0001F43B"),      ("म", "मछली", "ma", "\U0001F41F"),
    ("य", "यज्ञ", "ya", "\U0001F525"),        ("र", "रस्सी", "ra", "\U0001FAA2"),
    ("ल", "लट्टू", "la", "lattu.png"),        ("व", "वन", "va", "\U0001F333"),
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
# a translation when it is spoken aloud. Three are not in the English bank at
# all (लालची कुत्ता, सोने के अंडे, दो बिल्लियाँ और बंदर), which is deliberate:
# they are the ones a grandmother in this house actually tells, and the point
# of the Hindi side is that it is not a translation of the other one. Beats and tones work exactly as they do
# in English; detect_tts_language picks the Hindi voice from the script itself,
# so nothing here has to declare a language.
HINDI_STORIES = [
    {
        "title": "प्यासा कौआ",
        "segments": [
            ("बहुत पुरानी बात है। गर्मियों का सबसे गरम दिन था।", "storyteller"),
            ("सूरज बीच आसमान में आकर रुक गया था।", "storyteller"),
            ("घास सूख चुकी थी। नदी सूखकर फटी हुई मिट्टी बन गई थी।", "sad"),
            ("और उसी आसमान में उड़ रहा था एक छोटा सा काला कौआ।", "curious"),
            ("उसका गला सूख रहा था। पंख थक चुके थे।", "sad"),
            ("पानी, उसने अपने आप से कहा। पानी तो ढूँढना ही होगा।", "gentle"),
            ("वह खेतों के ऊपर उड़ा। कुछ नहीं मिला।", "curious"),
            ("वह जंगल के ऊपर उड़ा। वहाँ भी कुछ नहीं।", "curious"),
            ("उड़ता रहा, उड़ता रहा, जब तक पंख हिलाना भी मुश्किल हो गया।", "sad"),
            ("और तभी... जब वह हार मानने ही वाला था...", "mysterious"),
            ("उसे एक बगीचे के कोने में कुछ दिखाई दिया।", "mysterious"),
            ("घड़ा! एक बड़ा मिट्टी का घड़ा!", "excited"),
            ("कौआ फुर्र से उतरा और घड़े के किनारे बैठकर अंदर झाँका।", "curious"),
            ("पानी था! उसे चमकता हुआ साफ़ दिख रहा था।", "amazed"),
            ("पर वह बिलकुल नीचे था। बहुत, बहुत नीचे।", "sad"),
            ("उसने गर्दन लंबी की। और लंबी। और लंबी।", "gentle"),
            ("चोंच पानी के पास तक भी नहीं पहुँची।", "sad"),
            ("कोई और पक्षी होता, तो रोता हुआ उड़ जाता।", "storyteller"),
            ("पर यह कौआ घड़े के किनारे चुपचाप बैठ गया...", "mysterious"),
            ("और सोचने लगा।", "mysterious"),
            ("तभी उसकी नज़र नीचे पड़ी। सूखी ज़मीन पर छोटे छोटे पत्थर बिखरे थे।",
             "curious"),
            ("कौए ने एक पत्थर चोंच में उठाया। और घड़े में डाल दिया। छपाक!",
             "curious"),
            ("और पानी हिला। बहुत थोड़ा सा। पर हिला!", "amazed"),
            ("तो उसने एक और उठाया। छपाक!", "excited"),
            ("फिर एक और। छपाक! फिर एक और। छपाक, छपाक, छपाक!", "excited"),
            ("पानी ऊपर आता गया... और ऊपर... और ऊपर...", "amazed"),
            ("जब तक वह उसकी चोंच से छू नहीं गया।", "excited"),
            ("और छोटे से कौए ने जी भर कर पानी पिया।", "warm"),
            ("फिर उसने पंख झाड़े और शाम के आसमान में उड़ गया।", "warm"),
            ("वह बगीचे का सबसे बड़ा पक्षी नहीं था। सबसे ताक़तवर भी नहीं।", "gentle"),
            ("पर जहाँ सब हार मान लेते हैं, वहाँ वह रुका और उसने सोचा। "
             "और इसी से वह जीत गया।", "warm"),
        ],
        "question": "क्या कौए ने घड़े में पत्थर डाले थे?",
        "answer": True,
    },
    {
        "title": "शेर और चूहा",
        "segments": [
            ("जंगल के बीचों बीच, बरगद के पेड़ की ठंडी छाँव में, एक शेर सो रहा था।",
             "gentle"),
            ("कोई छोटा मोटा शेर नहीं। पूरे जंगल का सबसे बड़ा शेर।", "mysterious"),
            ("उसके पंजे थाली जितने बड़े थे।", "amazed"),
            ("और दूर दूर तक हर जानवर उसके पास से दबे पाँव निकलता था।",
             "mysterious"),
            ("हर जानवर... सिर्फ़ एक को छोड़कर।", "curious"),
            ("एक नन्हा भूरा चूहा घास में भागा चला आ रहा था, बिना यह देखे कि "
             "जा कहाँ रहा है।", "excited"),
            ("जड़ के ऊपर से। पत्थर के ऊपर से। और सीधा शेर के पंजे के ऊपर से!",
             "excited"),
            ("शेर की आँखें खुल गईं।", "mysterious"),
            ("धप्प! बड़ा सा पंजा नीचे आया, और चूहा उसमें फँस गया।", "excited"),
            ("अच्छा, शेर ने कहा। नाश्ता।", "mysterious"),
            ("छोड़ दीजिए! चूहा चीं चीं करके बोला। मुझे छोड़ दीजिए!", "sad"),
            ("मैं तो बहुत छोटा हूँ। आपका एक कोना भी नहीं भरूँगा।", "gentle"),
            ("और एक दिन, वादा है, मैं आपकी मदद करूँगा।", "gentle"),
            ("शेर उसे देखता रह गया।", "curious"),
            ("फिर वह हँसने लगा।", "excited"),
            ("तुम? मेरी मदद?", "excited"),
            ("वह इतनी ज़ोर से हँसा कि पेड़ के पत्ते झड़ गए।", "excited"),
            ("और हँसते हँसते उसने पंजा उठा लिया... और चूहा भाग निकला।", "warm"),
            ("फिर बारिशें आईं, और चली गईं। जंगल यह बात भूल भी गया।",
             "storyteller"),
            ("एक रात शिकारी आ गए।", "mysterious"),
            ("उन्होंने पेड़ों के बीच रस्सियों का बड़ा जाल बिछा दिया।",
             "mysterious"),
            ("और अँधेरे में शेर सीधा उसी जाल में जा फँसा।", "sad"),
            ("उसने खींचा। मरोड़ा। दहाड़ा, दहाड़ा और दहाड़ा।", "sad"),
            ("पर जितना वह छटपटाता, जाल उतना ही कसता जाता।", "sad"),
            ("दूर, एक पत्थर के नीचे, नन्हे भूरे चूहे ने सिर उठाया।", "curious"),
            ("यह आवाज़ तो मैं जानता हूँ, उसने कहा।", "mysterious"),
            ("वह दौड़ा। घास से, जड़ों के ऊपर से, सीधा जाल तक।", "excited"),
            ("आप बिलकुल मत हिलिए, उसने शेर से कहा। और कुतरना शुरू कर दिया।",
             "gentle"),
            ("कुतर, कुतर, कुतर। एक रस्सी। फिर दूसरी। फिर तीसरी।", "curious"),
            ("जब तक पूरा जाल खुल नहीं गया, और शेर चाँदनी में बाहर नहीं निकल आया।",
             "amazed"),
            ("उसने चूहे की तरफ़ देखा। और इस बार वह हँसा नहीं।", "warm"),
            ("दोस्त छोटा हो सकता है, और फिर भी ठीक वही दोस्त हो सकता है "
             "जिसकी ज़रूरत थी।", "warm"),
        ],
        "question": "क्या चूहे ने शेर की मदद की?",
        "answer": True,
    },
    {
        "title": "कछुआ और खरगोश",
        "segments": [
            ("एक खरगोश था जो हवा से बातें करता था।", "excited"),
            ("और उसे यह पता था। ख़ूब अच्छी तरह पता था।", "storyteller"),
            ("देखो मुझे! वह चिल्लाता, और तालाब के पास से ऐसा भागता कि बत्तखें "
             "घूम जातीं।", "excited"),
            ("कभी किसी ने मुझे हराया है? वह पूछता। किसी ने भी?", "excited"),
            ("कोई कुछ नहीं बोलता। क्योंकि किसी ने हराया ही नहीं था।", "gentle"),
            ("तभी, ज़मीन के पास से, एक धीमी सी शांत आवाज़ आई।", "mysterious"),
            ("मैं दौड़ लगाऊँगा, आवाज़ ने कहा।", "mysterious"),
            ("सब मुड़कर देखने लगे। वह कछुआ था।", "amazed"),
            ("खरगोश घूरता रह गया। फिर हँसते हँसते लोटपोट हो गया।", "excited"),
            ("तुम? तुम तो अपनी परछाईं तक नहीं पकड़ सकते!", "excited"),
            ("पूरा मैदान उसके साथ हँस पड़ा।", "excited"),
            ("कछुए ने सबके हँसने का इंतज़ार किया। कल सुबह, उसने कहा। "
             "उस बड़े बरगद तक।", "gentle"),
            ("अगली सुबह सारे जानवर रास्ते के दोनों तरफ़ खड़े हो गए।", "curious"),
            ("लोमड़ी ने रूमाल गिराया। और दौड़ शुरू!", "excited"),
            ("रूमाल ज़मीन तक पहुँचा भी नहीं था कि खरगोश गायब।", "excited"),
            ("नाले के ऊपर से। झाड़ी के बीच से। लंबे मोड़ के पार।", "excited"),
            ("और कछुए ने... एक पाँव उठाया... और नीचे रखा।", "gentle"),
            ("आधे रास्ते में खरगोश रुका और पीछे देखा।", "curious"),
            ("कछुआ तो दिख भी नहीं रहा था।", "amazed"),
            ("यहाँ नरम घास है, उसने सोचा। और धूप भी मीठी है।", "gentle"),
            ("थोड़ा आराम कर लेता हूँ। अभी तो घंटों बाक़ी हैं।", "gentle"),
            ("और वह घास पर लेट गया, और आँखें बंद कर लीं।", "mysterious"),
            ("टप। टप। टप। टप।", "mysterious"),
            ("कछुआ मोड़ से निकला।", "curious"),
            ("वह घास के पास से गुज़रा, और रुका नहीं।", "curious"),
            ("पहाड़ी चढ़ा। रुका नहीं।", "curious"),
            ("पत्थर के पास से निकला। रुका नहीं।", "curious"),
            ("एक ज़ोरदार शोर से खरगोश की नींद खुली।", "amazed"),
            ("वह उछलकर भागा, ज़िंदगी में जितना तेज़ कभी नहीं भागा था!",
             "excited"),
            ("पर जब वह आख़िरी मोड़ से निकला...", "excited"),
            ("कछुआ पहले से बरगद के नीचे बैठा था।", "amazed"),
            ("धीरे चलो, पर रुको मत। जीत रुकने वाले की नहीं होती।", "warm"),
        ],
        "question": "क्या खरगोश दौड़ जीता?",
        "answer": False,
    },
    {
        "title": "चींटी और टिड्डा",
        "segments": [
            ("गर्मियों के दिन थे, और पूरा खेत सुनहरा हो रखा था।", "warm"),
            ("बालियाँ ऊँची थीं। धूप नरम थी। और दिन ख़त्म होने का नाम ही नहीं "
             "लेते थे।", "warm"),
            ("एक चौड़े हरे पत्ते पर एक टिड्डा बैठा था, हाथ में बाजा लिए।",
             "curious"),
            ("और वह बजाता रहता। सुबह भर, दोपहर भर, शाम भर।", "excited"),
            ("ता ना ना! न कोई काम, न कहीं जाना!", "excited"),
            ("नीचे पगडंडी पर एक चींटी जा रही थी।", "storyteller"),
            ("अपने सिर से दुगुना बड़ा अनाज का दाना उठाए हुए।", "amazed"),
            ("नमस्ते! टिड्डे ने पुकारा। ऊपर आओ, खेलते हैं!", "excited"),
            ("नहीं आ सकती, चींटी ने कहा। मुझे यह घर ले जाना है।", "gentle"),
            ("घर? भला किसलिए? अभी तो गर्मी है!", "excited"),
            ("सर्दी आने वाली है, चींटी ने कहा। और चलती रही।", "gentle"),
            ("टिड्डा हँसा और फिर से बाजा बजाने लगा।", "excited"),
            ("अगले दिन चींटी फिर गुज़री। और उसके अगले दिन भी।", "storyteller"),
            ("हर एक दिन, एक एक दाना करके, ज़मीन के नीचे अपने छोटे से घर तक।",
             "storyteller"),
            ("और हर एक दिन टिड्डा बजाता रहा।", "excited"),
            ("फिर, एक सुबह, हवा बदल गई।", "mysterious"),
            ("खेत का सोना उड़ गया। पत्ते गिरने लगे।", "mysterious"),
            ("और सर्दी आ गई, तेज़ और सफ़ेद।", "sad"),
            ("टिड्डे ने दाना ढूँढा। कहीं दाना नहीं था।", "sad"),
            ("उसने हरा पत्ता ढूँढा। कहीं पत्ता ही नहीं था।", "sad"),
            ("वह भूखा था, ठंडा था, और उसका बाजा चुप हो चुका था।", "sad"),
            ("आख़िर वह एक पेड़ की जड़ में बने छोटे दरवाज़े तक पहुँचा, "
             "और खटखटाया।", "gentle"),
            ("अंदर गर्मी थी। चूल्हा जल रहा था। और खाना फ़र्श से छत तक भरा था।",
             "amazed"),
            ("चींटी ने उसे बर्फ़ में खड़ा देखा।", "gentle"),
            ("और बोली: अंदर आ जाओ।", "warm"),
            ("उसने उसे खाना दिया, कंबल दिया, और चूल्हे के पास जगह दी।",
             "warm"),
            ("फिर प्यार से बोली: अगली गर्मी में सुबह मेरे साथ काम करना।",
             "gentle"),
            ("और शाम को हम दोनों के लिए बाजा बजाना।", "warm"),
            ("और उसने ठीक वैसा ही किया।", "warm"),
            ("खेलने का भी समय होता है और तैयारी का भी। समझदारी यह जानने में "
             "है कि आज कौन सा दिन है।", "gentle"),
        ],
        "question": "क्या चींटी ने पूरी गर्मी मेहनत की?",
        "answer": True,
    },
    {
        "title": "एकता में बल",
        "segments": [
            ("एक गाँव में एक बूढ़ा किसान रहता था, और उसके चार बेटे थे।",
             "storyteller"),
            ("चारों मेहनती थे। चारों समझदार थे।", "gentle"),
            ("पर वे दिन भर आपस में लड़ते रहते थे।", "sad"),
            ("खेत किसका है। हल किसका है। बैल किसका है।", "sad"),
            ("सुबह से शाम तक झगड़ा। और खेत का काम पड़ा रह जाता।", "sad"),
            ("किसान बूढ़ा हो रहा था, और यही बात उसे सबसे ज़्यादा सताती थी।",
             "sad"),
            ("एक दिन उसने चारों को बुलाया।", "mysterious"),
            ("उसने कहा: जाओ, बाहर से लकड़ियाँ ले आओ।", "mysterious"),
            ("बेटे लकड़ियाँ ले आए। किसान ने उन्हें रस्सी से कसकर बाँध दिया।",
             "curious"),
            ("फिर उसने गट्ठर सबसे बड़े बेटे को दिया।", "mysterious"),
            ("इसे तोड़ो, उसने कहा।", "gentle"),
            ("बड़े बेटे ने पूरी ताक़त लगाई। उसका चेहरा लाल हो गया।", "excited"),
            ("गट्ठर हिला तक नहीं।", "sad"),
            ("दूसरे ने कोशिश की। फिर तीसरे ने। फिर चौथे ने।", "excited"),
            ("चारों ने मिलकर ज़ोर लगाया। गट्ठर फिर भी नहीं टूटा।", "sad"),
            ("नहीं टूटेगा, बड़े बेटे ने हाँफते हुए कहा। यह हो ही नहीं सकता।",
             "sad"),
            ("किसान मुस्कुराया।", "gentle"),
            ("उसने रस्सी खोल दी, और गट्ठर बिखर गया।", "curious"),
            ("फिर उसने हर बेटे को एक एक लकड़ी थमाई।", "curious"),
            ("अब तोड़ो, उसने कहा।", "gentle"),
            ("चटाक! चटाक! चटाक! चटाक!", "excited"),
            ("चारों लकड़ियाँ पल भर में टूट गईं।", "excited"),
            ("देखा? किसान ने कहा।", "gentle"),
            ("अलग अलग रहोगे, तो इन लकड़ियों जैसे हो।", "gentle"),
            ("कोई भी मुश्किल आकर तुम्हें एक एक करके तोड़ देगी।", "sad"),
            ("पर साथ बँधे रहोगे, तो पूरी दुनिया ज़ोर लगा ले, कुछ नहीं होगा।",
             "proud"),
            ("उस दिन के बाद चारों भाई साथ खेत जाते, साथ लौटते, और साथ खाना "
             "खाते।", "warm"),
            ("और वह खेत गाँव का सबसे हरा भरा खेत बन गया।", "warm"),
            ("एकता में ही बल है।", "warm"),
        ],
        "question": "क्या पूरा गट्ठर टूट गया था?",
        "answer": False,
    },
    {
        "title": "लालची कुत्ता",
        "segments": [
            ("एक छोटा कुत्ता था, जिसे हमेशा लगता था कि उसके पास कम है।",
             "storyteller"),
            ("एक दोपहर उसे बहुत ज़ोर की भूख लगी।", "sad"),
            ("वह गली गली घूमा, कहीं कुछ नहीं मिला।", "sad"),
            ("फिर कसाई की दुकान के पीछे उसे कुछ दिखा।", "curious"),
            ("एक बड़ी सी हड्डी! उस पर मांस भी लगा था!", "excited"),
            ("उसने झट से हड्डी मुँह में दबाई और भाग खड़ा हुआ।", "excited"),
            ("वह चाहता था कि कहीं शांत जगह बैठकर आराम से खाए।", "gentle"),
            ("रास्ते में एक छोटी नदी पड़ी, और उस पर लकड़ी का पुल था।",
             "storyteller"),
            ("कुत्ता पुल के बीच पहुँचा, और नीचे झाँका।", "curious"),
            ("और पानी में उसे एक और कुत्ता दिखाई दिया।", "mysterious"),
            ("उसके मुँह में भी एक हड्डी थी!", "amazed"),
            ("अब उसे मालूम नहीं था कि वह उसकी अपनी परछाईं है।", "storyteller"),
            ("उसने सोचा: इसकी हड्डी तो मेरी से भी बड़ी लग रही है।",
             "mysterious"),
            ("दो हड्डियाँ! सोचो! एक अभी, एक शाम को!", "excited"),
            ("वह और नीचे झुका, और ग़ुर्राया।", "excited"),
            ("पानी वाला कुत्ता भी ग़ुर्राया। बिलकुल उसी तरह।", "curious"),
            ("कुत्ते को ग़ुस्सा आ गया। उसने भौंकने के लिए मुँह खोला।",
             "excited"),
            ("भौं!", "excited"),
            ("और उसी पल, उसके मुँह से हड्डी छूट गई।", "sad"),
            ("छपाक! हड्डी पानी में गिरी।", "sad"),
            ("नीचे का कुत्ता ग़ायब। हड्डी ग़ायब।", "sad"),
            ("पानी हिलता रहा, और फिर शांत हो गया।", "sad"),
            ("कुत्ता पुल पर खड़ा रहा। पेट ख़ाली। मुँह ख़ाली।", "sad"),
            ("उसके पास पूरी एक हड्डी थी। पूरी की पूरी।", "gentle"),
            ("पर वह दूसरे की हड्डी गिनने लगा, और अपनी खो बैठा।", "gentle"),
            ("जो मिला है उसे सँभालो। जो नहीं मिला उसके पीछे भागोगे, "
             "तो दोनों जाएँगे।", "warm"),
        ],
        "question": "क्या कुत्ते को दूसरी हड्डी मिल गई?",
        "answer": False,
    },
    {
        "title": "सोने के अंडे",
        "segments": [
            ("बहुत पुरानी बात है। एक ग़रीब किसान और उसकी पत्नी एक छोटे से घर "
             "में रहते थे।", "storyteller"),
            ("उनके पास सिर्फ़ एक मुर्गी थी।", "gentle"),
            ("एक सुबह किसान दड़बे में गया, और ठिठक कर रुक गया।",
             "mysterious"),
            ("घोंसले में कुछ चमक रहा था।", "mysterious"),
            ("उसने उठाया। वह भारी था। बहुत भारी।", "amazed"),
            ("वह सोने का अंडा था!", "excited"),
            ("अगली सुबह फिर एक अंडा। और उसकी अगली सुबह फिर।", "amazed"),
            ("हर रोज़, बिना नागा, एक सोने का अंडा।", "warm"),
            ("किसान ने छत ठीक करवाई। नए कपड़े आए। खाना भर गया।", "warm"),
            ("गाँव में किसी के पास इतना नहीं था जितना अब उनके पास था।",
             "proud"),
            ("पर किसान रात को जागने लगा।", "mysterious"),
            ("एक दिन में सिर्फ़ एक अंडा, वह सोचता। सिर्फ़ एक।", "sad"),
            ("उसकी पत्नी ने कहा: हमारे पास तो सब कुछ है।", "gentle"),
            ("किसान ने कहा: पर और भी तो हो सकता है।", "sad"),
            ("वह सोचता रहा, सोचता रहा, और उसका मन लालच से भर गया।",
             "mysterious"),
            ("अगर रोज़ एक अंडा बनता है, उसने कहा...", "mysterious"),
            ("तो अंदर ज़रूर सोने का ढेर भरा होगा।", "mysterious"),
            ("मैं आज ही सारा निकाल लेता हूँ।", "sad"),
            ("उसकी पत्नी ने रोका। रुक जाइए। ऐसा मत कीजिए।", "sad"),
            ("पर उसने नहीं सुना।", "sad"),
            ("और अगली सुबह... दड़बे में सन्नाटा था।", "sad"),
            ("मुर्गी नहीं थी। और अंदर सोना भी नहीं था।", "sad"),
            ("कुछ भी नहीं। एक कण भी नहीं।", "sad"),
            ("अगली सुबह घोंसले में कोई अंडा नहीं आया।", "sad"),
            ("न उसकी अगली सुबह। न कभी दोबारा।", "sad"),
            ("किसान के पास जो था, उससे उसका पूरा जीवन चल सकता था।", "gentle"),
            ("पर उसने सब कुछ एक ही दिन में चाहा, और सब कुछ खो दिया।",
             "gentle"),
            ("लालच ऐसी चीज़ है जो हाथ की चीज़ भी छीन लेती है।", "warm"),
        ],
        "question": "क्या मुर्गी के अंदर सोना भरा था?",
        "answer": False,
    },
    {
        "title": "दो बिल्लियाँ और बंदर",
        "segments": [
            ("एक गाँव के किनारे दो बिल्लियाँ रहती थीं।", "storyteller"),
            ("एक काली, एक सफ़ेद। और दोनों पक्की सहेलियाँ थीं।", "gentle"),
            ("एक दिन उन्हें रसोई की खिड़की के पास एक रोटी मिली।",
             "excited"),
            ("बड़ी, गोल, गरम रोटी।", "excited"),
            ("काली बिल्ली ने कहा: यह मैंने पहले देखी थी।", "excited"),
            ("सफ़ेद बिल्ली ने कहा: पर उठाई तो मैंने!", "excited"),
            ("आधी आधी कर लेते हैं, काली बोली।", "gentle"),
            ("ठीक है, सफ़ेद बोली। पर आधी बराबर होनी चाहिए।", "curious"),
            ("दोनों ने रोटी तोड़ी। और एक टुकड़ा ज़रा सा बड़ा निकल गया।",
             "curious"),
            ("यह बड़ा है! नहीं, वह बड़ा है!", "excited"),
            ("वे झगड़ने लगीं, और इतनी ज़ोर से झगड़ीं कि पेड़ पर बैठे बंदर ने "
             "सुन लिया।", "mysterious"),
            ("बंदर नीचे उतरा, और बहुत समझदारी से बोला।", "mysterious"),
            ("झगड़ा क्यों? मैं तराज़ू ले आता हूँ। मैं बाँट दूँगा।",
             "mysterious"),
            ("दोनों बिल्लियाँ मान गईं।", "gentle"),
            ("बंदर ने दोनों टुकड़े तराज़ू के दोनों पलड़ों में रखे।",
             "curious"),
            ("ओहो, उसने कहा। यह वाला भारी है।", "curious"),
            ("और उसने उस टुकड़े में से एक बड़ा कौर खा लिया।", "excited"),
            ("अब दूसरा भारी हो गया!", "amazed"),
            ("तो उसने उसमें से भी एक कौर खा लिया।", "excited"),
            ("अब पहला वाला फिर भारी!", "amazed"),
            ("कुतर। कुतर। कुतर।", "mysterious"),
            ("बिल्लियाँ देखती रहीं, और रोटी छोटी होती गई।", "sad"),
            ("रुको! काली बिल्ली बोली। अब हमें कुछ मत बाँटो!", "sad"),
            ("बंदर ने तराज़ू नीचे रखा।", "gentle"),
            ("उसके हाथ में बस दो छोटे छोटे टुकड़े बचे थे।", "sad"),
            ("इतने छोटे, कि वह दोनों एक ही बार में मुँह में रखकर पेड़ पर "
             "चढ़ गया।", "excited"),
            ("दोनों बिल्लियाँ ख़ाली हाथ बैठी रह गईं।", "sad"),
            ("उनके पास आधी आधी रोटी थी, और वह दोनों के लिए काफ़ी थी।",
             "gentle"),
            ("जब दो लोग आपस में लड़ते हैं, तो फ़ायदा अक्सर तीसरे का होता है।",
             "warm"),
        ],
        "question": "क्या बिल्लियों को उनकी रोटी वापस मिली?",
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


def story_by_title(title, language="en"):
    """The story with that title, or None.

    Titles are what the picker screen hands back, so this is the lookup behind
    "the child chose this one" -- and it has to survive the bank being edited
    while a screen is still holding a title from the old one, which is what the
    None is for.
    """
    for story in stories_for(language):
        if story["title"] == title:
            return story
    return None


def story_titles(language="en"):
    """Every title in this language's bank, in the order they are told in."""
    return [story["title"] for story in stories_for(language)]


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
