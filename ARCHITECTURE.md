# How the code is laid out

`assist.py` used to be one 10,622-line file. It is now seventeen, and this is the
map. Start with the table: it says which file to open for the change you want to
make.

| file | lines | open it to change |
|---|---|---|
| `assist.py` | 22 | nothing — it only starts the program |
| `uibridge.py` | 40 | how another thread calls the screen |
| `state.py` | 190 | the state the parts share |
| `audio.py` | 290 | how she speaks, and the subtitles |
| `profiles.py` | 365 | who is using the device |
| `kg.py` | 475 | the Kindergarten flow under the screens |
| `store.py` | 529 | the database, the knowledge graph, the progress log |
| `prompts.py` | 573 | **what she is told to be, and every fixed line she says** |
| `media.py` | 666 | music and video |
| `config.py` | 869 | **the .env, and every number worth tuning** |
| `books.py` | 883 | the textbooks: fetching, indexing and searching them |
| `kg_content.py` | 1,380 | the alphabets, the words, the stories |
| `actions.py` | 1,438 | what she can do on the device |
| `speech.py` | 1,731 | the microphone, the voice detector, Whisper |
| `visuals.py` | 1,995 | everything drawn on the board |
| `ui.py` | 5,794 | **everything that draws** |
| `assistant.py` | 3,161 | `ai_loop` — the turn-by-turn state machine |

`ai_loop` is 1,442 of `assistant.py`'s lines. It stays whole on purpose: it is
one state machine, and cutting it into pieces would make it harder to follow,
not easier.

## Two rules that keep it untangled

**1. Leaves import nothing of ours.** `config`, `state`, `uibridge`, `prompts`,
`media`, `profiles`, `store`, `visuals`, `kg_content` and `books` never import
the assistant. You can open a shell, import one, and poke at it. (`books`
imports `store`, which is itself a leaf; that is as deep as it goes.)

**2. A module that the assistant imports FROM, and that also calls back, binds
the back-reference at the FOOT of the file.**

```python
# ... every definition above ...

import assistant     # last line
```

`ui.py`, `actions.py`, `speech.py`, `kg.py` and `audio.py` all do this. With the
import up top, `python -c "import ui"` fails: the module hands control to the
assistant before defining anything, and the assistant's `from ui import ...`
finds an empty module. At the foot, the cycle resolves whichever module is asked
for first. **Do not move those imports up**, and do not change them to
`from assistant import ...` — that form is resolved at import time and needs the
other module finished before this one has started.

## Two traps worth knowing about

**`assist.py` must stay a shim.** A file that is both the script you run and a
module something imports gets loaded twice, under two names, with two copies of
every queue and every `threading.Event`. The screen would then be talking to a
second, unheard copy of the program. It also has to keep that name: the `liza`
launcher finds a running instance with `pgrep -f "python.*assist\.py"`.

**In `state.py`, how you reach a value depends on whether it is reassigned.**
Events, queues, locks and lists are only ever mutated in place, so they are
imported by name. The plain values are reassigned, so they go through the
module — `state.currently_playing`, both to read and to write.
`from state import currently_playing` binds the value as it stood at import,
and the writer's assignment never reaches the reader. Nothing raises; the reader
just sees `None` for ever.

Two local names shadow modules, so watch for them: `state` is a parameter of
`TutorUI.set_state`, and `config` is a local in `audio_player_worker`. Both
files import around it — see the note at the top of each.

## Checking a change

```bash
.venv/bin/python -m pyflakes *.py          # undefined names
.venv/bin/python -c "import ui"            # each module must import on its own
liza restart                               # edits do nothing until this
```

`pyflakes` cannot see cross-module attribute access. `assistant.mpv_command`
stayed broken through a whole commit because it is a lookup on a module object,
invisible until the button is pressed. If you move a name between modules, grep
for its old home.
