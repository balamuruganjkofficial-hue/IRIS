"""Configuration loading with sane defaults so a trimmed config.yaml still boots."""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "persona": {
        "name": "Iris",
        "call_me": "boss",
        "vibe": "warm, playful, quietly cheeky",
        "humour": 0.7,          # 0..1 how often a reply carries a joke or jab
        "teasing": 0.6,         # 0..1 how much she mocks him, never cruelly
        "curiosity": 0.7,       # 0..1 how often she asks a real follow-up question
        "opinions": True,       # she may disagree and say what she thinks
        "proactive": True,      # one gentle unprompted line after long silence
        "idle_minutes": 25.0,   # silence before that line
        "idle_gap_minutes": 45.0,
        "idle_lines": [],
    },
    "models": {
        "llm": "openai/gpt-oss-20b",
        "fallback": "openai/gpt-oss-120b",
        # extra free models, e.g. {provider: gemini, model: gemini-2.5-flash}
        "free_fallbacks": [],
        "openrouter_free": True,       # discover :free tool models automatically
        "openrouter_models": [],       # optional explicit free IDs
        "openrouter_max": 4,
        "gemini_model": "gemini-2.5-flash",
        "cerebras_model": "llama-3.3-70b",
        "mistral_model": "mistral-small-latest",
        "github_model": "openai/gpt-4o-mini",
        "ollama_models": [],           # opt-in local models, e.g. [llama3.2]
    },
    "brain": {"max_tokens": 400, "temperature": 0.7,
              "max_steps": 10, "max_tool_calls": 3,
              "request_timeout": 20, "fallback_budget": 50},
    "voice": {
        "tts_voice": "en-GB-SoniaNeural",
        "rate": "+8%",
        "pitch": "+0Hz",
        "prosody": True,                # subtle per-phrase pitch/rate variation
        "rate_jitter": 4,               # ± percent
        "pitch_jitter": 3,              # ± Hz
        "stt_language": "en-US",
        "stt_models": ["whisper-large-v3-turbo", "whisper-large-v3"],
        "wake_word": "iris",
        "wake_engine": "auto",          # auto | vosk | openwakeword
        "wake_phrases": ["iris", "hey iris", "hi iris", "hello iris",
                         "okay iris", "ok iris", "jarvis", "hey jarvis"],
        "wake_fallback": "hey_jarvis",  # openWakeWord model if Vosk is missing
        "wake_threshold": 0.35,
        "barge_in": True,
        "barge_in_threshold": 0.85,
        "vosk_model": "data/vosk/vosk-model-small-en-us-0.15",
        "vosk_download": True,
        "vosk_url": ("https://alphacephei.com/vosk/models/"
                     "vosk-model-small-en-us-0.15.zip"),
        "vad_min_rms": 250.0,
        "end_silence": 0.7,
        "wake_debug": False,
        "cache_chars": 90,
        "cache_files": 400,
    },
    "memory": {"recent_turns": 4, "recalled_facts": 3, "max_turns": 400},
    "permissions": {"voice_confirm": True, "timeout": 9.0, "attempts": 2,
                    "click_fallback": True},
    "browser": {"headless": True, "show_in_ui": True, "capture_quality": 70,
                "capture_width": 1280, "keep_shots": 40,
                "local_files": True, "external_chrome": False},
    "web": {"max_results": 6, "show_in_ui": True},
    "surface": {"enabled": True, "ratio": 0.70, "max_tabs": 5,
                "live_fps": 6, "embed_apps": True, "park_windows": True},
    "chrome": {"default_profile": "", "profiles": {}},
    "paths": {"workspace": ""},
    "ui": {
        "enabled": True,
        "width": 1020,
        "height": 700,
        "fps": 30,
        "always_on_top": False,
        "cyber": True,
        "surface_ratio": 0.70,
        "bg": "#060407",
        "bg2": "#12060A",
        "surface": "#1A070C",
        "surface_hi": "#2A0A12",
        "accent": "#FF1F3D",
        "accent_soft": "#FF5C73",
        "text": "#F7ECEF",
        "muted": "#A07A82",
        "user_bubble": "#4A0818",
        "iris_bubble": "#1A070C",
    },
}


def _merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path=None):
    p = Path(path or os.getenv("IRIS_CONFIG") or (ROOT / "config.yaml"))
    try:
        with open(p, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except FileNotFoundError:
        user = {}
    return _merge(DEFAULTS, user)


CFG = load()

DATA = Path(os.getenv("IRIS_DATA_DIR") or (ROOT / "data")).resolve()
LOGS = ROOT / "logs"

_ws = (CFG.get("paths") or {}).get("workspace") or os.getenv("IRIS_WORKSPACE")
WORKSPACE = Path(os.path.expanduser(str(_ws))).resolve() if _ws else (ROOT / "workspace")

for _folder in (DATA, LOGS, WORKSPACE):
    _folder.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def get(*keys, default=None):
    node = CFG
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node
