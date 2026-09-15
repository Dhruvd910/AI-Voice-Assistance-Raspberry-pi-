# 🤖 Liza: Raspberry Pi AI Voice Assistant & Interactive Tutor

A local-first, multimodal AI assistant designed for the Raspberry Pi. Liza combines a physical touchscreen interface with custom animated emotional states, hardware push-button triggers, speech recognition, and a **Dual-Brain architecture** allowing you to seamlessly toggle between cloud/local LLMs (Groq/Ollama) and a custom AWS EC2 backend.

---

## ✨ Key Features

* **Dual-Brain Architecture:** Toggle between **LIZA** (Groq / Local Ollama) and your custom **AWS Bot** directly from the UI.
* **Interactive Study Modes:**
  * **Tutor Mode:** Structured, expert-level explanations broken down by core principles, mechanisms, and real-world examples.
  * **Co-Tell Mode:** Collaborative partner mode that uses short prompts and asks follow-up questions to test your knowledge.
  * **Re-Tell Mode:** Step-by-step active examiner mode that listens as you explain a topic, validates your statements, and offers feedback.
* **Physical Hardware Support:** Designed for 5-inch touchscreens (XPT2046 SPI) and supports physical GPIO push-buttons for instant wake-up.
* **Live Web Search Fallback:** Automatically queries DuckDuckGo for real-time technical answers when needed.
* **Visuals on the Transcribe Board:** Ask her to show a diagram, an equation, a graph or a picture and it appears on screen — see [Showing Things](#-showing-things-diagrams-equations-graphs--pictures) below.

---

## 🛠️ Hardware Requirements

* Raspberry Pi (Pi 4 or Pi 5 recommended)
* 5-inch HDMI Touchscreen (with SPI touch controller)
* USB Microphone & Speaker (or a combined USB audio device)
* Momentary push-button (optional, for physical wake-up)
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
| "show me a toucan" | A real photograph |

### The live graph is different from everything else

Ask for a graph of a formula — "y = x^2", "y = m\*x + c" — and instead of a
picture, you get a small plot with a **slider for every number in the
expression**. Drag the power on `x^2` and watch it become `x^4` in real time.
This runs entirely on the Pi: no network call, no delay, redraws on every pixel
of finger movement. Say "now make it x cubed" and she updates the formula from
whatever the slider is currently set to.

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
