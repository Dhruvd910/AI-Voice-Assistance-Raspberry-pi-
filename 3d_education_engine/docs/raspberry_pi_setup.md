# Raspberry Pi 5 setup

Tested on a Raspberry Pi 5 (8 GB), Raspberry Pi OS (Debian 13, X11/LXDE), Python 3.13, an
800×480 touch display.

## 1. System packages

```bash
sudo apt install python3-venv libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1 alsa-utils
```

The `libxcb-*` libraries are what Qt 6 needs for its X11 platform plugin. `alsa-utils` provides
`aplay` for speech output.

## 2. Python environment

```bash
cd ~/liza/3d_education_engine
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir -r requirements.txt      # ~1.2 GB installed (VTK is 570 MB)
.venv/bin/pip install --no-cache-dir -r requirements-dev.txt  # tests
```

**Low on SD-card space?** VTK, RDKit, NumPy and PySide6 are large. If another virtual environment
on the same card already has them at the same Python version, hard-link them instead of installing
a second copy (no extra space; each environment stays independent, because pip replaces files
rather than editing them):

```bash
SRC=/path/to/other/.venv/lib/python3.13/site-packages
DST=.venv/lib/python3.13/site-packages
for p in vtkmodules vtk.py vtk-*.dist-info rdkit rdkit.libs rdkit-*.dist-info numpy numpy.libs numpy-*.dist-info; do
  cp -al "$SRC/$p" "$DST/"
done
```

This is how the development Pi was set up (VTK/RDKit from `~/liza/.venv`, PySide6 from MAYA's).

Optional extras: `requirements-voice.txt` (on-device Whisper + wake word, ~300 MB) and
`requirements-optional.txt` (Chroma + sentence-transformers).

## 3. Graphics

Mesa's v3d driver reports an older desktop OpenGL than VTK asks for.
`app.config.prepare_gl_environment()` sets `MESA_GL_VERSION_OVERRIDE=3.3` and
`MESA_GLSL_VERSION_OVERRIDE=330` before VTK loads, on ARM only. Measured on the Pi 5:

| | |
|---|---|
| first frame | ~0.8 s (context creation) |
| frame, heart (30k triangles, low LOD), 560×400 | ~17 ms |
| load heart / cell / molecule | 0.5 / 0.2 / 0.1 s |
| heart → tissue transition (24 frames) | ~1.2 s |

The low level of detail is chosen automatically on a Pi (`MODEL_LOD=auto`).

## 4. Run

```bash
./run.sh                      # full screen on a Pi; Esc does not exit — use Exit (tap twice)
./run.sh --windowed           # in a window
.venv/bin/python -m app.main --check
```

From an SSH session the launcher uses `DISPLAY=:0`, so the app appears on the Pi's screen.

## 5. Voice (optional)

In `.env`:

```bash
STT_PROVIDER=openai_compatible     # Groq Whisper: fast, needs GROQ_API_KEY / STT_API_KEY
# STT_PROVIDER=faster_whisper      # fully offline; pip install -r requirements-voice.txt
TTS_PROVIDER=cartesia              # or piper (offline) with PIPER_MODEL=/path/voice.onnx
CARTESIA_API_KEY=...
CARTESIA_VOICE_ID=...
WAKEWORD=true                      # needs requirements-voice.txt; model "hey_jarvis"
```

Tap 🎤 (or say the wake word), speak, and the command runs when you stop talking.

## 6. Start at boot (optional)

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/3d-education.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=3D Education Engine
Exec=/home/pi1/liza/3d_education_engine/run.sh
EOF
```

## Troubleshooting

- **Blank viewport / "bad X server connection"** — run with `DISPLAY=:0` (the launcher does).
- **Qt: "could not load the Qt platform plugin xcb"** — install the `libxcb-*` packages above.
- **Slow on the Pi** — check `MODEL_LOD=auto` (or `low`) and `DEPTH_PEELING=false`.
- **Microphone button disabled** — `STT_PROVIDER` is `none` or its key is missing.
