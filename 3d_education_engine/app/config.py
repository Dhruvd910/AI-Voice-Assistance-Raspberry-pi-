"""Settings, read once from the environment and an optional `.env` file.

Every value has a default that works offline on a Raspberry Pi 5, so the engine
starts with no configuration at all: visualization, chemistry, physics and the
knowledge base need nothing from the network. Only the LLM and the cloud voice
providers need keys, and the agent falls back to its rule-based parser when
the LLM is not configured.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. No interpolation, no export keyword magic."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def is_raspberry_pi() -> bool:
    try:
        return "raspberry pi" in Path("/proc/device-tree/model").read_text(errors="ignore").lower()
    except OSError:
        return False


@dataclass
class LLMSettings:
    # Any OpenAI-compatible chat-completions endpoint: OpenRouter, Groq,
    # OpenAI, a local llama.cpp / Ollama server...
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    model: str = "google/gemini-2.5-flash"
    temperature: float = 0.2
    timeout_s: float = 30.0
    max_tool_rounds: int = 5

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)


@dataclass
class VoiceSettings:
    stt_provider: str = "none"            # none | openai_compatible | faster_whisper
    stt_base_url: str = "https://api.groq.com/openai/v1"
    stt_api_key: str = ""
    stt_model: str = "whisper-large-v3-turbo"
    faster_whisper_model: str = "base"
    tts_provider: str = "none"            # none | cartesia | piper
    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""
    cartesia_model: str = "sonic-2"
    piper_model: str = ""                 # path to a .onnx voice for the piper CLI
    wakeword_enabled: bool = False
    wakeword_model: str = "hey_jarvis"
    audio_output_device: str = "default"
    record_seconds_max: float = 8.0


@dataclass
class RenderSettings:
    width: int = 560
    height: int = 400
    depth_peeling: bool = True
    background: str = "#101418"
    lod: str = "auto"                     # auto | high | low
    memory_budget_mb: int = 600
    transition_frames: int = 12


@dataclass
class Settings:
    root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    assets_dir: Path = PROJECT_ROOT / "assets"
    manifests_dir: Path = PROJECT_ROOT / "models" / "manifests"
    documents_dir: Path = PROJECT_ROOT / "knowledge" / "documents"
    attributions_path: Path = PROJECT_ROOT / "ATTRIBUTIONS.md"
    database_path: Path = PROJECT_ROOT / "data" / "engine.db"
    offline: bool = False
    on_pi: bool = field(default_factory=is_raspberry_pi)
    rag_backend: str = "sqlite"           # sqlite | chroma
    embedder: str = "hashing"             # hashing | sentence_transformers
    fullscreen: bool = False
    llm: LLMSettings = field(default_factory=LLMSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    render: RenderSettings = field(default_factory=RenderSettings)

    @property
    def lod(self) -> str:
        if self.render.lod != "auto":
            return self.render.lod
        return "low" if self.on_pi else "high"


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(root: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    """Settings from `root/.env` overlaid by the process environment."""
    root = root or PROJECT_ROOT
    merged = load_dotenv(root / ".env")
    merged.update(os.environ if env is None else env)
    get = merged.get

    settings = Settings(
        root=root,
        data_dir=root / "data",
        assets_dir=root / "assets",
        manifests_dir=root / "models" / "manifests",
        documents_dir=root / "knowledge" / "documents",
        attributions_path=root / "ATTRIBUTIONS.md",
        database_path=Path(get("ENGINE_DB", str(root / "data" / "engine.db"))),
    )
    settings.offline = _bool(get("ENGINE_OFFLINE"), False)
    settings.rag_backend = get("RAG_BACKEND", settings.rag_backend)
    settings.embedder = get("EMBEDDER", settings.embedder)
    settings.fullscreen = _bool(get("FULLSCREEN"), settings.on_pi)

    llm = settings.llm
    llm.base_url = get("LLM_BASE_URL", llm.base_url).rstrip("/")
    llm.api_key = get("LLM_API_KEY", "") or get("OPENROUTER_API_KEY", "")
    llm.model = get("LLM_MODEL", llm.model)
    llm.temperature = float(get("LLM_TEMPERATURE", llm.temperature))
    llm.timeout_s = float(get("LLM_TIMEOUT", llm.timeout_s))

    voice = settings.voice
    voice.stt_provider = get("STT_PROVIDER", voice.stt_provider)
    voice.stt_base_url = get("STT_BASE_URL", voice.stt_base_url).rstrip("/")
    voice.stt_api_key = get("STT_API_KEY", "") or get("GROQ_API_KEY", "")
    voice.stt_model = get("STT_MODEL", voice.stt_model)
    voice.faster_whisper_model = get("FASTER_WHISPER_MODEL", voice.faster_whisper_model)
    voice.tts_provider = get("TTS_PROVIDER", voice.tts_provider)
    voice.cartesia_api_key = get("CARTESIA_API_KEY", "")
    voice.cartesia_voice_id = get("CARTESIA_VOICE_ID", "")
    voice.cartesia_model = get("CARTESIA_MODEL", voice.cartesia_model)
    voice.piper_model = get("PIPER_MODEL", "")
    voice.wakeword_enabled = _bool(get("WAKEWORD"), False)
    voice.wakeword_model = get("WAKEWORD_MODEL", voice.wakeword_model)
    voice.audio_output_device = get("AUDIO_OUTPUT_DEVICE", voice.audio_output_device)

    render = settings.render
    render.width = int(get("RENDER_WIDTH", render.width))
    render.height = int(get("RENDER_HEIGHT", render.height))
    render.depth_peeling = _bool(get("DEPTH_PEELING"), render.depth_peeling)
    render.lod = get("MODEL_LOD", render.lod)
    render.memory_budget_mb = int(get("MEMORY_BUDGET_MB", render.memory_budget_mb))
    return settings


def prepare_gl_environment() -> None:
    """Must run before VTK is imported.

    Mesa's v3d driver on the Pi 5 reports an older desktop OpenGL than VTK asks
    for; raising the REPORTED version lets VTK create its context. It changes
    nothing on a desktop GPU that already offers 3.3+.
    """
    if platform.machine() in {"aarch64", "armv7l"}:
        os.environ.setdefault("MESA_GL_VERSION_OVERRIDE", "3.3")
        os.environ.setdefault("MESA_GLSL_VERSION_OVERRIDE", "330")
