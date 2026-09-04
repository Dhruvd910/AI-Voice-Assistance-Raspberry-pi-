"""The .env, and the numbers a person might want to change without
reading the program.

This module OWNS loading the .env, and does it on import. That is not tidiness:
ui.py reads the KG patience numbers at import time, and every one of them is an
os.getenv call, so whichever module got imported first would otherwise decide
whether the .env was in effect yet. Owning it here means the answer cannot
depend on import order.
"""

import os

from cartesia import Cartesia

def load_dotenv(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Only strip a MATCHED surrounding pair. Stripping quote characters
            # unconditionally corrupts any value that legitimately ends in one,
            # such as the ALSA device name plug:'dmix:CARD=Device_1,DEV=0'.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and not os.getenv(key):
                os.environ[key] = value

# Read the .env before anything below, or anything in another module, calls
# os.getenv. See the note at the top of this file.
load_dotenv()

# The KG screens ask questions that are answered slowly and in pieces, so they
# get their own patience rather than the conversational one. See
# kg_request_listen for what each of the three numbers actually bounds.
#
# Spelling: a child says the letters one at a time with a think between each.
KG_SPELL_START_TIMEOUT_S = float(os.getenv("KG_SPELL_START_TIMEOUT", "10.0"))
KG_SPELL_PHRASE_LIMIT_S = float(os.getenv("KG_SPELL_PHRASE_LIMIT", "22.0"))
KG_SPELL_END_SILENCE_S = float(os.getenv("KG_SPELL_END_SILENCE", "2.2"))

# Counting: a child counting fifteen apples touches each one on the screen and
# says the number, and the gap between "seven" and "eight" is a real pause. At
# the conversational threshold the answer captured was "one, two" -- which is
# the reported bug, not a child who cannot count.
KG_COUNT_START_TIMEOUT_S = float(os.getenv("KG_COUNT_START_TIMEOUT", "12.0"))
KG_COUNT_PHRASE_LIMIT_S = float(os.getenv("KG_COUNT_PHRASE_LIMIT", "45.0"))
KG_COUNT_END_SILENCE_S = float(os.getenv("KG_COUNT_END_SILENCE", "3.0"))


# Where sound goes. The same ALSA device twice: mpv prefixes ALSA names with
# "alsa/", aplay does not.
AUDIO_OUTPUT_DEVICE = os.getenv("AUDIO_OUTPUT_DEVICE", "plug:'dmix:CARD=Device_1,DEV=0'")
# Same device, but mpv prefixes ALSA names with "alsa/".
MPV_AUDIO_DEVICE = os.getenv("MPV_AUDIO_DEVICE", "alsa/plug:'dmix:CARD=Device_1,DEV=0'")


# 2. The Voice (Cartesia API)
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
cartesia_client = Cartesia(api_key=CARTESIA_API_KEY or None)

# Text-to-speech: one Cartesia voice speaks both languages, switched per sentence.
CARTESIA_MODEL = os.getenv("CARTESIA_MODEL", "sonic-3.5")
CARTESIA_SAMPLE_RATE = int(os.getenv("CARTESIA_SAMPLE_RATE", "22050"))
CARTESIA_SPEED = os.getenv("CARTESIA_SPEED", "fast")  # slow | normal | fast
BYTES_PER_SEC = CARTESIA_SAMPLE_RATE * 2  # 16-bit mono

# Set CARTESIA_VOICE_ID to a multilingual voice, or give Hindi and English their
# own voices. Run `python assist.py --list-voices` to see what your key can use.
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "")
VOICE_IDS = {
    "en": os.getenv("CARTESIA_VOICE_ID_EN", "") or CARTESIA_VOICE_ID,
    "hi": os.getenv("CARTESIA_VOICE_ID_HI", "") or CARTESIA_VOICE_ID,
}
