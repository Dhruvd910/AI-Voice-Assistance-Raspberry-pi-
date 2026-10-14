# 🤖 Liza: Raspberry Pi AI Voice Assistant & Interactive Tutor

A local-first, multimodal AI assistant designed for the Raspberry Pi. Liza combines a physical touchscreen interface with custom animated emotional states, hardware push-button triggers, speech recognition, and a **Dual-Brain architecture** allowing you to seamlessly toggle between cloud/local LLMs (Groq/Ollama) and a custom AWS EC2 backend.

---

## ✨ Key Features

* **Dual-Brain Architecture:** Toggle between **LIZA** (Groq / Local Ollama) and your custom **AWS Bot** directly from the UI.
* **Interactive Study Modes:**
  * **Tutor Mode:** Structured, expert-level explanations broken down by core principles, mechanisms, and real-world examples.
  * **Co-Tell Mode:** Collaborative partner mode that uses short prompts and asks follow-up questions to test your knowledge.
  * **Re-Tell Mode:** Step-by-step active examiner mode that listens as you explain a topic, validates your statements, and offers feedback. Say "that's it" (or pause for 10 seconds) and she marks it: the topic and a score out of 10, your strong points, your weak points (mistakes, and the key ideas you left out, checked against what she taught you and your textbook), and the one thing to focus on next. A report card with the same points stays on the board, the result goes on the progress screen, and your next re-tell of that topic says whether you have fixed it. Ask "what did I miss?" straight afterwards and she explains it.
* **Physical Hardware Support:** Designed for 5-inch touchscreens (XPT2046 SPI) and supports physical GPIO push-buttons for instant wake-up.
* **Live Web Search Fallback:** Automatically queries DuckDuckGo for real-time technical answers when needed.
* **Visuals on the Transcribe Board:** Ask her to show a diagram, an equation, a graph, a number line, a table or a picture and it appears on screen — see [Showing Things](#-showing-things-diagrams-equations-graphs--pictures) below.
* **Write and Draw on the Board:** Open the Transcribe Board full screen and write a sum, a chemical equation or a physics problem with your finger, or draw a diagram. She reads it as you write, solves it step by step out loud with the working written beside yours, and opens a graph to drag or a simulation to run — see [Writing on the Board](#-writing-on-the-board) below.
* **She Can See:** Turn on the live camera, hold something up and ask "what is this?", or hold up a page of your book and ask her to read it, solve a question on it or check your answer — see [Showing Her Things](#-showing-her-things-the-camera) below.
* **Her Textbooks:** Put the child's own CBSE or ICSE books on the device and she answers out of the chapter they are actually taught from — see [Her Textbooks](#-her-textbooks) below.
* **Progress:** Every story heard, word spelled and test taken is kept per student, shown on a **My progress** screen, and remembered by her.

---

## 🛠️ Hardware Requirements

* Raspberry Pi (Pi 4 or Pi 5 recommended)
* 5-inch HDMI Touchscreen (with SPI touch controller)
* USB Microphone & Speaker (or a combined USB audio device)
* Momentary push-button (optional, for physical wake-up)
* Raspberry Pi camera module (optional, so she can see; on a Pi 5 it needs the narrow 22-pin cable)
* External USB Pendrive/SSD (optional, for storing local Ollama models)

---

## 📦 Dependency Installation Guide

Follow these steps to set up the software environment on a fresh Raspberry Pi OS installation.

### Step 1: System Updates & Dependencies
Open your terminal and ensure your system packages are up to date, then install essential system dependencies for audio and graphics:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-tk python3-pil python3-pil.imagetk portaudio19-dev libasound2-dev -y
```

---

## 🎙️ Voice Commands

Spoken in English, Hindi or a mix of the two. These are handled on the device and
never reach the language model, so they work even mid-sentence.

### Playing things

| Say | What happens |
| --- | --- |
| "play hanuman chalisa" · "हनुमान चालीसा बजाओ" | Audio only, in the music panel |
| "play video of the solar system" · "सोलर सिस्टम का वीडियो चलाओ" | Video, full screen |
| "show me a video of photosynthesis" | Video, full screen |

The word **video** is what decides between the two. Videos are found by searching
the web, played full screen, and closed by tapping the picture or asking her to.

### While something is playing

The microphone is **off** for as long as music or a video is playing — it is not
transcribing the track. The only way back in is the wake word:

1. Say **"Hey Liza"** (or tap the screen).
2. Whatever is playing **pauses**, and a video freezes with `PAUSED · LISTENING`.
3. Say what you want — "stop the music", "close the video", or any question.
4. Say nothing for **3 seconds** and the microphone closes again, and playback
   picks up where it left off.

You can do it in one breath: *"Hey Liza, stop the music."*

### Interrupting

| Say | What happens |
| --- | --- |
| "stop Liza" · "लीज़ा रुको" | She stops talking, but keeps listening |
| "stop listening" · "सुनना बंद करो" | Back to standby until the wake word or a tap |
| "stop music" · "गाना बंद करो" | Music stops |
| "close video" · "वीडियो बंद करो" | Video closes |
| "stop" | Everything making a noise stops |

She listens for these *while she is speaking*, so there is no need to wait for her
to finish a sentence. While media is playing, wake her first as described above.

---

## 🖼️ Showing Things: Diagrams, Equations, Graphs & Pictures

Ask her to show something and a picture appears on the **transcribe board** — the
panel on screen that normally shows what she's saying. Tap it to enlarge, tap
again to close.

| Say | What appears |
| --- | --- |
| "show me the water cycle" | A cycle diagram — stages arranged in a ring |
| "draw the steps of photosynthesis" | A left-to-right flow diagram |
| "what's the equation for gravity" *(just told, not drawn — see below)* | Spoken only |
| "show me the equation for gravity" | The formula, typeset properly |
| "show me y equals x squared" | A **live** graph, with a slider under every number in it |
| "draw a number line from 1 to 10" | A ruler with the numbers under it |
| "show me 2 plus 3 on the number line" | The same, with the hops drawn over it |
| "show me the three times table" | A table |
| "draw a triangle with base 6 and height 4" | The figure, measurements on the right edges |
| "draw a cylinder with radius 3 cm and height 7 cm" | The solid as the textbook draws it — hidden edges dashed, measurements on, volume and surface area worked out underneath. Cube, cuboid, cylinder, cone, sphere, hemisphere, pyramid and prisms |
| "show me three quarters" | Shaded circles |
| "put quarter past three on a clock" | A clock face reading that time |
| "show me the timeline of the freedom struggle" | Dates along a line |
| "compare plant and animal cells" | Two overlapping circles |
| "show me a toucan" | A real photograph |
| "show me the balanced equation for burning methane" · "मीथेन के जलने की अभिक्रिया लिखकर दिखाओ" | The reaction, with subscripts, states, charges and conditions over the arrow — and a **Balanced** tick only when it really is |
| "draw the structure of benzene" | The molecule's structure, with its formula |
| "show me the equations of motion" | Several formulas, one per line |
| "draw a free body diagram of a box pushed on the floor" | The object with its forces, and the net force when the forces have numbers |
| "draw a circuit with a cell, a switch and two bulbs in series" | A circuit diagram: cells, resistors, bulbs, switches, meters, parallel branches |
| "show me how to solve 2x + 3 = 7" · "solve this" with the board open | The working, step by step, with what was done beside each line and the answer boxed — each step lights up as she says it |

**She never refuses to draw something.** Anything without a shape of its own
becomes a picture of whatever was asked for, and past that the words on a card.
The number line, table, shape, angle, clock, fraction, array, timeline,
comparison and tree are all drawn **on the Pi and never by an image model** —
those are the ones where the numbers *are* the content, and an image model
draws a convincing number line with the 7 missing. The same goes for
reactions, molecules, force diagrams and circuits (`science.py`): an image
model drew hydrogen burning *backwards*. Molecule structures need
`.venv/bin/pip install rdkit`; a common one is drawn from a built-in list, and
any other is looked up by name on PubChem.

### 3D models you turn with a finger

"Show me a water molecule in 3D" opens a ball-and-stick model full screen.
It spins slowly until the first touch. After that, drag to turn it, − and +
to zoom, Reset to put it back, and × to close. A still stays on the board;
tap it to open the model again. The shape is worked out on the Pi by RDKit
(water really is bent at 104.5°, methane really is a tetrahedron), for any
molecule the flat drawer knows. It needs `.venv/bin/pip install pyvista`.
Without it, a 3D request falls back to the flat drawing.

Biology and physics have 3D models too, built in code from simple shapes
(`models3d.py`). They work offline, load in under a second, and are labelled
like a textbook figure:

- **Physics:** the solar system (planets orbit), the Sun, Earth and Moon (lit
  by the Sun, so day and night and the Moon's phases show), the layers of the
  Earth (cut open), the atom of any of the first 20 elements (Bohr model,
  electrons moving in their shells), the magnetic field of a bar magnet, a
  wire and a solenoid, white light through a prism, a transverse wave and a
  sound wave (both moving).
- **Biology:** animal cell, plant cell, DNA, neuron, red blood cells, virus,
  bacteriophage, bacterium, flower, the human eye (cut open), mitochondrion
  and chloroplast.
- **Geometry:** cube, cuboid, sphere, hemisphere, cylinder, cone, square
  pyramid, tetrahedron, triangular prism and hexagonal prism. See-through faces,
  the edges picked out, the corners marked, and the faces, edges and vertices
  counted. Give the measurements ("a cube of side 4 cm", "a cylinder of radius
  3 cm and height 7 cm") and it is drawn to those proportions, with the volume
  and surface area worked out in the corner: *Volume = a³ = 64 cm³*.

  Before this, "cube" was looked up as a chemical name, and PubChem has a
  compound called that (rotenone, from the cubé plant). A child asking for a
  cube got a molecule.

Anything not on the list, such as a skeleton or a brain, is shown as a
picture. Put a real model file in `3d-models/`, named for what it is (for
example `brain.glb`), and Liza will use it instead. See `3d-models/README.txt`.

#### The 3D Education Engine (`3d_education_engine/`)

Some models come from the 3D Education Engine instead:
- a real anatomical **human heart** with named chambers, valves and vessels
- heart muscle, a heart muscle cell and a mitochondrion to zoom down through
- molecules built from PubChem's 3D data
- 17 physics **simulations with settings**, such as projectile motion,
  orbits, electric fields and circuits

Liza checks the engine's list first (`viewer3d.engine_model_for`). Anything
it doesn't have still comes from `models3d.py`.

With an engine model up, you can talk to the model:

- "rotate it", "cut it in half", "show the left ventricle", "make it
  transparent", "label the chambers", "go one level deeper", "go back",
  "make the heart beat", "change the angle to 60 degrees".

These are carried out by the engine straight away, without asking the
language model. Questions still go to Liza, who sees the model's parts and
state in ON_BOARD. She can point at things with
`[ACTION: model3d_do: highlight the left ventricle]`.

The engine is a separate app with its own interpreter
(`3d_education_engine/.venv`). Liza never imports it: the engine draws in
its own worker process (`app/liza_worker.py`), speaking the same protocol as
viewer3d's worker. What it can show is read from
`3d_education_engine/data/liza_catalog.json`. It can also run on its own:
see `3d_education_engine/README.md`.

The drawing runs in a separate worker process (`viewer3d.py`), so a graphics
driver crash costs one model, not the whole tutor. The Pi's GPU reports an
older OpenGL than VTK asks for, so `viewer3d.py` sets
`MESA_GL_VERSION_OVERRIDE=3.3` before VTK loads.

### Buttons, not questions

She never asks "Would you like to see a simulation of it?". When a picture,
model, graph or simulation would help and you did not ask for one, she puts a
**button** for it under her answer and says "Tap below to see it". Tap it, or
just say "yes" or "show me", and it opens. Two or three choices get a button
each ("See the reaction", "See the steps"). Her next answer takes them down
unless it offers something new.

On the home screen the buttons stand along the bottom of the Transcribe Board;
on the full-screen board, at the bottom right of the page.

**A button whether or not she remembers.** Ask about something this device
can show in 3D — "tell me about projectile motion", "what is DNA?", "how many
faces does a cylinder have?" — and its button goes up after her answer even
when her answer put none up itself (measured: asked about projectile motion
three times, the model offered the simulation once). It comes from *your*
words, never hers, and everyday words that only sound like a topic
("current", "spring", "wave", "charge") are left out.

> **Why buttons.** Asked a question like that, a child has to answer out loud,
> and a short "Yes." is exactly what the microphone throws away as noise. In
> one session two "Yes."es in a row were dropped before a longer reply got
> through. A tap cannot be misheard. If the model still writes the question
> next to the button, the question is not spoken.

### The live graph is different from everything else

Ask for a graph of a formula — "y = x^2", "y = m\*x + c" — and instead of a
picture, you get a small plot with a **slider for every number in the
expression**. Drag the power on `x^2` and watch it become `x^4` in real time.
This runs entirely on the Pi: no network call, no delay, redraws on every pixel
of finger movement. Say "now make it x cubed" and she updates the formula from
whatever the slider is currently set to.

**Several curves on one graph.** "y = x² − 3; y = A sin x + B" draws both, the
first in red and the second in blue, with each formula written in its colour
above the plot. A **letter** with no value given gets a slider of its own,
starting where the curve is the plain one — A at 1 and B at 0, so you start
from y = sin x and drag the wave taller and higher. Formulas are read the way a
person writes them: `x²`, `2x`, `A sin x`, `sin²x`, `sin⁻¹ x`, `√x`, `|x − 2|`,
`log x`, `ln x`, `y = mx + c`. A sine wave is drawn across −2π to 2π and marked
in π; `log x` and `√x` start at the y axis. An equation such as `2x + 3 = 7`
plots as the line and the level it has to reach, crossing where x is the
answer.

A graph made of actual data points ("plot 0,0; 1,5; 2,20") or bars
("Mon=3; Tue=5") is a normal picture instead — sliders only apply to formulas.

### The board stays up so she can explain it

Once something is on the board, it stays there — a follow-up question like
"explain it" or "what's the second stage" talks you through the *same* picture
instead of clearing it. If the diagram has stages, the one she's currently
explaining lights up as she names it. She takes the picture down herself once
the conversation moves to something unrelated.

### How the picture gets made

If you set a `FAL_KEY` in `.env` (a free key from
[fal.ai](https://fal.ai/dashboard/keys)), every diagram, equation, graph and
photo is drawn by **Seedream 4**, an AI image model — noticeably better
looking than the built-in renderer. Without a key, or if the network call
fails or times out, everything falls back to being drawn locally on the Pi
(cycles and flow diagrams by hand, equations and point-graphs via matplotlib) —
nothing breaks, it just looks plainer. The live-graph slider above is **never**
sent over the network either way; it has to redraw instantly, so it's always
drawn on-device.

---

## ✍️ Writing on the Board

The Transcribe Board opens **full screen** as a page you write and draw on with
your finger — and she understands what is on it.

* **Open it:** tap the button with the pencil in the top-right corner of the
  Transcribe Board, or say "open the board", "make the board full screen",
  "I want to draw", "बोर्ड खोलो", "मुझे कुछ लिखना है".
* **Close it:** the button in the top-right corner of the page (the one with
  the brackets pointing in), or "close the board", "go back", "बोर्ड बंद करो".
  Your writing is kept: open it again and it is still there. It is wiped when a
  different student is picked.

### The page

Along the top: four pens (black, blue, red and green — a ring in red round the
part you are asking about reads to her as exactly that), the rubber, **undo**
and **clear**. Clear can be undone too, so one stray tap cannot lose a whole
sum. Along the bottom: her state on the mic button (tap it to talk, the way
Speak works) and what she is saying, since the page covers her face and the
small board. While she talks, the **Stop talking** pill is at the top.

### She reads it as you write

Stop writing for a moment and she looks at the page: a chip in the corner says
what she read — **I read: 2x + 3 = 7**, **I read: H₂ + O₂ → H₂O**, **I read: a
block on a slope with two arrows**. Nobody has to ask; it is how you know she
has understood your handwriting before you ask her anything about it. If she
read it wrong, write it more clearly and the chip changes.

### Buttons under your writing

With the reading come **buttons for what you wrote**, right under it — about
three or four seconds after you stop writing, without waiting for her to say
anything:

| You wrote or drew | Buttons |
| --- | --- |
| y = x² − 3 and y = A sin x + B | **Plot them** |
| 2x + 3 = 7 | **Solve it** · **Plot it** |
| H₂ + O₂ → H₂O | **Balance it** · **See the reaction** |
| A cube (drawn or named, with or without its measurements) | **See it in 3D** · **See the shape** |
| A triangle, a rectangle, a hexagon… | **Measure it** |
| Projectile motion, a pendulum, a circuit… | **Run the simulation** |
| H₂SO₄, methane | **See it in 3D** |

**Solve it** and **Balance it** ask her, exactly as Ask Liza does. Every other
button is checked before it is shown — a formula that would not plot, or a
model this device does not have, gets no button — so a button never opens onto
"I couldn't do that". Write anything new and they go, until the next reading.

### Graphs on the page

**Plot it** (or a graph she draws while the page is up) puts the graph **on the
page, beside your writing**, as a card: the curves in their colours, the
formulas above them, a slider per letter. It goes on the side with less of your
writing, narrowed if a line runs into that side. The corner buttons enlarge it
to the whole screen and put it away; **Graph** in the toolbar brings it back.
It is the same graph as on the home screen's board, so a slider dragged here
has moved there too. The graph and her working take turns beside your writing:
**Steps** and **Graph** in the toolbar swap them.

### Measuring a figure

Draw a triangle, tap **Measure it**, and it is tidied where you drew it:
straight sides through your corners, the corners lettered A, B, C, each angle
written inside its corner — **64°, 55°, 61°** — and **A + B + C = 180°** in the
middle. A four-sided figure adds up to 360°, up to an octagon at 1080°. The
corners and angles are worked out on the Pi from your lines; she is told them
too, so you can ask her why they add up to that. Tap it again, or write
anything, and the marks go. **Explore it** opens your own figure in the
geometry lab (below), where its corners can be dragged.

### Ask her about it

Tap **Ask Liza**, or just ask out loud while the page is up. Whatever is on the
page goes to her with the question, as a picture, and she reads it properly
again — thinking it through first when there is something to work out, because
a sum worked out in a hurry is where the slips are. A drawing, a word or a name
is answered straight away: thinking about "a cube" once cost an answer
nineteen seconds of silence.

| Write or draw | Say | What happens |
| --- | --- | --- |
| 2x + 3 = 7 | "Solve this" · *(or tap Ask Liza)* | She talks you through every step, and the working appears beside your writing |
| H₂ + O₂ → H₂O | "Can you balance this?" · "इसे बैलेंस करो" | The balancing, step by step, ending on 2H₂ + O₂ → 2H₂O |
| A box with arrows marked 10 N and 4 N | "What's the net force?" | The forces, the sum, and the answer with its direction |
| Your own working | "Is my answer right?" · "मेरा जवाब सही है?" | What is right first, then the first mistake and exactly where, then the correct working |
| A drawing | "What did I draw?" | What it is, and the one thing worth knowing about it (a cube: 6 faces, 12 edges, 8 corners) — never "what would you like to explore?" — with a button to see it properly |
| y = x² − 3 and y = A sin x + B | *(tap Ask Liza)* | What each curve looks like, and both plotted beside your writing with A and B on sliders |
| A right triangle with sides 3 cm and 4 cm | "Find the missing side" | Pythagoras step by step to 5 cm, and a **See the shape** button with the triangle drawn and labelled |
| A box (a cube) marked 4 cm | "What is its volume?" | V = a³ = 64 cm³, and a **See it in 3D** button for the cube with its measurements |
| A circle with radius 7 cm | "Find the area" | πr² = 154 cm², worked through |

**The working beside yours.** Each step is the maths and, under it, what was
done ("take 3 from both sides"), with the answer boxed in green at the end. The
step she is saying lights up as she says it, and the column scrolls to keep it
in view. It goes on whichever side of the page has less of your writing; the ✕
puts it away and **Steps** in the toolbar brings it back. Hindi works too —
ask in Hindi and the steps are written in Hindi.

**See it.** Where it helps, the working ends with a button to try the idea
yourself:

* **See the graph** — the line or curve, with a slider for every number in it.
* **Run the simulation** — the 3D engine's simulation set to the problem's own
  numbers: a ball thrown at 20 m/s at 30° is launched at 20 m/s and 30°.
* **See the reaction** / **See the molecule** — the balanced reaction drawn
  properly, or the molecule's structure.
* **See the shape** / **See it in 3D** — the figure or solid with your
  measurements on it.

A graph opens on the page beside your writing (see above); everything else
opens over the page, and closing it brings you back where you were.

**Without the page.** A solution she works out for a question held up to the
camera, or for "show me how to solve it", goes on the small board instead, as a
picture with a dot per step — tap it to enlarge.

### Saying it instead of tapping it

| Say | What happens |
| --- | --- |
| "open the board" · "I want to write" · "बोर्ड खोलो" | The page opens |
| "clear the board" · "start again" · "बोर्ड साफ करो" | The page is wiped (undo brings it back) |
| "undo" · "erase the last line" · "पिछला मिटाओ" | The last line goes |
| "close the board" · "go back" · "बोर्ड बंद करो" | Back to the home screen |

### Settings

In `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOARD` | `1` | `0` takes the full-screen board away |
| `BOARD_READ_LIVE` | `1` | `0` stops the live "I read:" chip; she still reads the page when asked |
| `BOARD_READ_PAUSE_S` | `1.0` | How long the writing must stop before she reads it (and the buttons under it come up) |
| `BOARD_READ_MODEL` | `LLM_MODEL` | The model that does the live reading. It has to be able to see |
| `BOARD_REASONING` | `low` | How hard she thinks before answering about the page: `off`, `low`, `medium`, `high`. Thinking costs about two seconds before the first word, which she covers with "Let me see what you wrote." |
| `BOARD_MAX_TOKENS` | `2400` | Room for the spoken steps and the written working together |
| `BOARD_SEND_MAX` | `1280` | Longest side, in pixels, of the picture of the page she is sent |
| `BOARD_KEEP_SKETCHES` | `5` | How many of those pictures are kept in `pictures/board/`, so an odd answer can be checked against what she saw |

> **Why the live reading is not sent with the question.** The chip's reading
> is a quick glance, made without thinking so it can keep up with your writing.
> Given to her as a hint, it was trusted over the picture: a hand-drawn "10 N"
> glanced at as "1ON" turned the net force wrong in half the runs, and in none
> of the runs without the hint. So the chip is for you, and she reads the
> picture herself.

---

## 📐 The Geometry Lab

A solid or a flat figure you can **turn with a finger and tap** — and every
tap is measured. It opens whenever a geometry figure is asked for ("show me a
cube", "a cylinder of radius 3 cm and height 7 cm", "a right triangle with
sides 3 and 4"), from **See it in 3D** under a cube drawn on the board, from
**Explore it** under a drawn triangle, and from her own answers. It is lettered
the way a textbook letters it: a cube or cuboid is **ABCD** round the bottom and
**EFGH** above (E over A), a triangle **ABC**, a cone's apex **V** over its
centre **O**.

**Tap one thing** and it says what it is: an edge's length and the angle the
two faces make along it; a face's shape, sides, angles and area; a corner's
angles and why they add up to less than 360°.

**Tap two** and it says how they meet, with the angle drawn where it is:

| Tap | It shows |
| --- | --- |
| Edges AB and AE | ∠BAE = 90°: they meet at A, perpendicular |
| Corners A and G | AG = 4√3 ≈ 6.93 cm, a space diagonal — Pythagoras twice |
| A, G, then edge AE | ∠GAE = 54.7° |
| Edge AE and the top face | AE ⟂ face EFGH: 90° |
| A, G and the bottom face | AG makes 35.3° with face ABCD, with its shadow AC and the drop GC |
| Faces ABCD and ABFE | They meet along AB at 90° (a tetrahedron's faces: 70.5°) |
| Edges AB and FG | Skew lines: never meet; 90° between their directions; 4 cm apart |
| Faces ABCD and EFGH | Parallel, 4 cm apart |
| A pyramid's apex and its base | The height, measured straight down at 90° |
| A cone's slant VP and radius OP | ∠VPO = 53.1° |

**Tap three corners** and the solid is cut flat through them: B, D and E make an
equilateral triangle with 60° in every corner; A, C and G cut the cube into a
rectangle. A flat figure's corners can be **dragged** instead of turned: the
angles change as you drag, and still add up to 180°.

Tap something again to unpick it; **Clear** unpicks everything, **Turn back**
straightens it. **Ask Liza about it** asks her why — "AE is perpendicular to
face EFGH: why?" — and she can point at parts herself while she explains. Ask
out loud about parts by name ("what's the angle between AG and the bottom
face?") and they are picked on the screen and measured *before* she answers,
so she can tell you the number as well as why.

Everything is drawn and measured on the Pi, from the figure's own corners —
no picture, no network. Closing the lab leaves the figure small on the board.

---

## 🗣️ How She Says Maths

What she writes on the screen is not what she should say out loud. Her answers
were played through her voice and transcribed back with Whisper, to hear what a
child in the room hears, and it was often wrong: "H₂SO₄" came out as
"H-G-S-O-D", "2H₂ + O₂ → 2H₂O" as "2H Su plus O tan 2H Su", "2x = 4" as "2x
equals sign 4", "πr²" as "par squared", "√16" as "zaars", "NaCl" as "Nushiel",
and the minus in "x² − 5x" was not said at all.

So every sentence is turned into words just before it is spoken
(`spoken.py`). The caption on the screen keeps the symbols.

| Written | Said |
| --- | --- |
| H₂SO₄, CO₂, NaCl | "H 2 S O 4", "C O 2", "N A C L" — the way a chemistry teacher spells a formula |
| 2H₂ + O₂ → 2H₂O | "2 H 2 plus O 2 gives 2 H 2 O" |
| x² − 5x + 6 = 0 | "X squared minus 5x plus 6 equals 0" |
| √16, πr², a³ | "the square root of 16", "pi r squared", "A cubed" |
| 20 m/s, 9.8 m/s², 6 N, 154 cm² | "20 metres per second", "9.8 metres per second squared", "6 newtons", "154 square centimetres" |
| triangle ABC, AB = 5 cm | "triangle A B C, A B equals 5 centimetres" |

A letter on its own is capitalised when it stands for a number: the voice reads
a lone small "y" as a syllable ("y equals" was heard as "E equals"). Hindi
answers get the words a Hindi maths teacher uses: बराबर, गुणा, बटा, का वर्ग.

---

## 📷 Showing Her Things: the Camera

With a camera module plugged in, she can see. The camera is **live**: once it
is on, the picture stays on the Transcribe Board and she answers about whatever
is in front of it the moment you ask, with no photo to take and no countdown.

* **Turn it on:** "Turn on the camera", "कैमरा ऑन करो", or tap the camera button
  in the top-left corner of the Transcribe Board. Asking her to look turns it on
  too.
* **Ask about an object:** "What is this?", "What am I holding?", "ये क्या है?",
  "मेरे हाथ में क्या है?" Then hold up the next thing and ask again.
* **Ask about a page:** "Read this page", "Solve this question", "Check my
  answer", "Help me with my homework question", "इसमें क्या लिखा है?", "मेरी
  किताब का सवाल नंबर तीन समझाओ"
* **Turn it off:** "Turn off the camera", "कैमरा बंद करो", "wipe out the
  board", the ✕ on the picture, or the camera button again.

It also turns itself off when she goes to sleep, when another screen opens,
when she draws something on the board (ask her to look and it comes back), when
a different student is picked, and after 10 minutes with no questions. While
it is on, every question goes to her with what the camera sees at that moment;
she ignores the picture when the question is about something else.

The pictures go to the same model that answers everything else (it reads
print, handwriting and Hindi); nothing is recognised on the Pi itself. The last
five frames she was sent are kept in `pictures/camera/`, so an odd answer can
be checked against what she saw.

**Setting it up.** Power the Pi off, then plug the camera into the connector
the screen is *not* using. Push the ribbon in fully, contacts the same way
round as the screen's ribbon, and close the latch. Then check:

```bash
rpicam-hello --list-cameras
```

It should list one camera (`imx708`, `imx219`, `ov5647`...). "No cameras
available" means the ribbon is not seated or is the wrong way round, or the
camera is not an official one and needs its `dtoverlay=` line in
`/boot/firmware/config.txt`. Then `liza restart`.

Settings (in `.env`): `CAMERA=0` turns the camera feature off,
`CAMERA_IDLE_OFF_S` is how long it stays on with no questions (600 seconds),
and `CAMERA_ROTATION=180` is for a module mounted upside down. `CAMERA_PREVIEW_MIRROR=1` mirrors only the preview, for a camera that
faces you. The rest are in `config.py`.

---

## 📚 Her Textbooks

Put the child's own books on the device and every question is searched against
them before it is answered. "Explain the water cycle" has one answer in a Class
6 chapter and another in a Class 9 one, and a child revising for Friday's test
needs the first — so where the book answers the question, she answers from the
book: its definition, its wording, its examples. Where it does not, she answers
as she always did.

She never says she looked it up, never names a chapter, and never reads a page
number aloud. She is a teacher who knows the book, not a search engine reading
it out.

### Getting the books

The books live in `books_new/`, one folder per class and one per subject inside
it — `books_new/VI/science/`, `books_new/XII/physics/`. Classes 6 to 12 are kept;
physical education, arts, Hindi-language and Sanskrit books are skipped for now.

```bash
liza books repair       # re-fetch any NCERT PDF whose download was cut off
liza books zips         # unpack zips, or fetch the book a broken zip was meant to hold
liza books ingest       # read everything into the index
liza books status
```

A PDF or zip that will not open is usually one still being copied in — check
its size a minute apart first. One that really is cut off can be fetched again
by `repair` or `zips`, which work from the NCERT file names and replace a broken
copy only once the new one opens.

**ICSE books cannot be fetched.** CISCE does not publish them; they are
commercial books from Selina, Frank and others and there is no legal download.
Supply your own copies instead — drop the PDFs into
`books_new/ICSE/IX/Physics/` and run `liza books ingest`. Until then an ICSE
student is answered from the NCERT passages for their class, and told plainly
that it is not their own book.

The folder layout **is** the manifest:

    books_new/<class>/<subject>/<anything>.pdf

Class and subject are read out of the path — Roman numerals and hand-typed
folder names like `Social_science_i` are understood — so nothing has to be
registered anywhere. Re-running `ingest` replaces what is already indexed rather
than doubling it. See `books_new/README.md` for the rest.

### Questions about the book itself

Ingesting also reads each chapter's **own name off its first page**, so
"what's the first chapter?", "what is chapter 9 called?" and "what comes after
magnets?" are answered from the shelf rather than from memory. That matters
more than it sounds: NCERT replaced the Class 6 Science book, and asked without
this she answered "Food: Where Does It Come From?" — the first chapter of the
edition that was *withdrawn*. The book on the device is *Curiosity*, and its
first chapter is *The Wonderful World of Science*.

A subject that has not been ingested is one she says she does not have, rather
than one she invents the contents of.

### Checking what it found

```bash
liza books search "how do we separate sand from water" --class 6
```

Searching is Postgres full text, not embeddings: the index is already on the
device, it answers in single-digit milliseconds over a whole shelf, and a
school question is full of exactly the rare nouns — "photosynthesis",
"trigonometry", "Mughal" — that lexical search is best at. A passage that only
weakly matches is thrown away rather than shown to her, because the one thing
worse than having no book on the device is having the wrong page of it quoted
at a child.

With no books ingested, or with PostgreSQL down, this whole feature is simply
absent and she answers exactly as she did before.

---

## ⚙️ Optional Settings

Set these in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VIDEO` | `1` | Set to `0` to disable video playback entirely |
| `VIDEO_MAX_FPS` | `30` | Frame cap. Lower it if video stutters on a busy Pi |
| `BARGE_IN` | `1` | Set to `0` so she cannot be interrupted mid-sentence |
| `WAKE_WORD` | `1` | Set to `0` for tap-only waking |
| `MEDIA_SILENCE_S` | `3` | Seconds of silence before the mic closes and playback resumes |
| `BARGE_MEDIA_GAIN` | `1.8` | How much louder than the music your voice must be to wake her. Raise it if playback pauses by itself; lower it if the wake word gets missed |
| `BARGE_MAX_NO_SPEECH` | `0.35` | Reject a barge-in capture when Whisper is this unsure it was speech at all |
| `BARGE_MIN_LOGPROB` | `-0.75` | Reject a barge-in capture below this confidence |
| `FAL_KEY` | *(unset)* | Enables AI-drawn visuals via Seedream 4. Free key at [fal.ai](https://fal.ai/dashboard/keys). Without it, diagrams are drawn locally on the Pi instead |
| `VISUAL_SEEDREAM_KINDS` | all kinds | Comma-separated list to limit which visual types use Seedream, e.g. `picture,photo,image,cycle,steps` to keep equations/graphs drawn locally (more reliably correct, less pretty) |
| `BOOK_CONTEXT_PASSAGES` | `3` | How many textbook passages may go into one answer |
| `BOOK_CLASS_BACK` | `4` | How many classes BELOW the student's own to search. A Class 8 child asking about fractions is asking about a Class 5 chapter |
| `BOOK_RANK_RELATIVE` | `0.25` | How good a passage must be, relative to the best hit, to be used at all |
| `WAKE_COLD_AFTER_S` | `120` | Standby this long with nobody touching her drops to the stricter sleep-time wake bar, so room noise cannot wake her on a device that was switched on and left |

> **Why those last three exist:** the microphone sits next to the speaker, so
> anything it hears past a playing track is mostly the track itself, and Whisper
> answers non-speech audio by inventing sentences. The two score thresholds throw
> those away. Just as important, **never seed that transcription with a prompt
> containing the words you are listening for** — Whisper hands them straight back
> out of music. Seeding it with the stop commands made every track stop itself;
> seeding it with "Hey Liza" would make the music wake her.

> **Note:** there is no `mpv`, `ffmpeg` or browser on a stock Pi image, so video is
> decoded in-process by PyAV and drawn onto the Tk canvas. This needs Pillow, which
> the `apt` line above installs as `python3-pil.imagetk`.
