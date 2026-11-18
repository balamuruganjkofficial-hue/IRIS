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
        "llm": "qwen/qwen3.8-27b",
        "fallback": "openai/gpt-oss-120b",
        # extra free models, e.g. {provider: gemini, model: gemini-2.5-flash}
        "free_fallbacks": [{"provider": "groq", "model": "openai/gpt-oss-20b"}],
        "groq_free": True,             # discover every free Groq chat model
        "groq_models": [],             # optional explicit Groq IDs
        "groq_max": 6,                 # how many Groq models to try
        "openrouter_free": True,       # discover :free tool models automatically
        "openrouter_models": [],       # optional explicit free IDs
        "openrouter_max": 10,
        "gemini_model": "gemini-2.5-flash",
        "cerebras_model": "llama-3.3-70b",
        "mistral_model": "mistral-small-latest",
        "github_model": "openai/gpt-4o-mini",
        "ollama_models": [],           # opt-in local models, e.g. [llama3.2]
    },
    "brain": {"max_tokens": 400, "temperature": 0.7,
              "max_steps": 10, "max_tool_calls": 3,
              "request_timeout": 20, "fallback_budget": 50,
              "groq_tool_budget": 4000},
    "voice": {
        # Ava is Edge's most expressive free Copilot voice: warm and conversational.
        "tts_voice": "en-US-AvaMultilingualNeural",
        "rate": "+2%",                  # base rate; the style range clamps it
        "pitch": "+0Hz",                # base pitch; the style range clamps it
        "style": "warm",                # calm | warm | energetic
        "prosody": True,                # subtle per-phrase pitch/rate variation
        "rate_jitter": 4,               # +/- percent
        "pitch_jitter": 3,              # +/- Hz
        "speakable": True,              # turn numbers, URLs and markdown into speech
        "pronunciations": {             # words the TTS would otherwise mangle
            "SQL": "sequel",
            "JSON": "jason",
            "GUI": "gooey",
            "API": "A P I",
            "URL": "U R L",
            "HTTP": "H T T P",
            "AI": "A I",
            "CPU": "C P U",
            "GPU": "G P U",
            "SSD": "S S D",
            "USB": "U S B",
            "OS": "O S",
        },
        "engine": "auto",               # auto | edge | offline
        "edge_retries": 2,              # extra attempts per sentence before SAPI
        "edge_connect_timeout": 6.0,    # websocket connect budget, seconds
        "edge_first_audio_cold": 8.0,   # first audio after a cold connection
        "edge_first_audio_warm": 5.0,   # first audio when Edge was just used
        "edge_total_budget": 20.0,      # stop retrying one sentence after this
        "edge_cold_after": 300.0,       # seconds without success before "cold"
        "edge_down_after": 3,           # real network failures before offline
        "edge_down_window": 60.0,       # those failures must happen this fast
        "edge_probe_interval": 20.0,    # while down, try Edge this often
        "offline_engine": "sapi",       # sapi | kokoro (kokoro is opt-in)
        "kokoro_model": "models/kokoro-v1.0.int8.onnx",
        "kokoro_voices": "models/voices-v1.0.bin",
        "kokoro_voice": "af_heart",
        "kokoro_speed": 1.0,
        "kokoro_max_rtf": 1.5,          # refuse Kokoro if slower than this
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
        "barge_in_mode": "voice",       # voice | wake | off (Esc always works)
        "barge_in_ratio": 2.2,          # how much louder than her echo counts as you
        "barge_in_min_rms": 500.0,      # never barge in below this absolute level
        "barge_in_learn": 1.0,          # seconds of her own voice used as the baseline
        "barge_in_hold": 0.3,           # seconds you must stay louder before she stops
        "barge_in_keep": 0.6,           # seconds kept so your first word is not lost
        "barge_in_debug": False,        # print the echo baseline and threshold
        "vosk_model": "data/vosk/vosk-model-small-en-us-0.15",
        "vosk_download": True,
        "vosk_url": ("https://alphacephei.com/vosk/models/"
                     "vosk-model-small-en-us-0.15.zip"),
        "vad_min_rms": 250.0,
        "end_silence": 0.7,
        "wake_debug": False,
        "cache_chars": 90,
        "cache_files": 400,
        "offline_voice": "Zira",        # female SAPI voice; Zira matches en-US
    },
    "skills": {
        "enabled": True,                # load recipes Iris wrote for herself
        "folder": "",                   # empty = data/skills
        "max_steps": 8,                 # steps in one generated skill
        "max_recipes": 50,              # hard cap, so the folder cannot grow forever
    },
    "autonomy": {
        "enabled": True,                # she may speak or act on her own
        "idle_minutes": 20.0,           # silence before she considers it
        "cooldown_minutes": 40.0,       # minimum gap between unprompted lines
        "max_per_hour": 3,              # hard cap, so she never nags
        "check_minutes": 8.0,           # minimum gap between model decisions
        "model_decides": True,          # let the free model choose the line
    },
    "network": {
        "dns": {
            "cache_seconds": 300,       # positive DNS answers live 5 minutes
            "negative_seconds": 30,     # failures are cached briefly to avoid spam
            "system_timeout": 1.0,      # max seconds to wait for the OS resolver
            "doh_timeout": 1.5,         # max seconds for Cloudflare DoH
            "doh_ip": "1.1.1.1",        # IP literal, so DoH never needs DNS
            "doh_tls_host": "cloudflare-dns.com",  # certificate name for DoH
        },
        "connectivity": {
            "interval": 20.0,           # seconds between checks while online
            "offline_interval": 10.0,   # faster retry while offline
            "timeout": 1.2,             # per-host TCP connect timeout
            "hosts": [["1.1.1.1", 443], ["api.groq.com", 443],
                      ["openrouter.ai", 443]],
        },
    },
    "latency": {
        "enabled": True,                # write one timing row per turn to logs/latency.csv
        "log": "logs/latency.csv",      # rotated to latency.1.csv at 1 MB
        "max_bytes": 1048576,           # rotation threshold in bytes (1 MB)
        "report_turns": 50,             # default --last for scripts/latency_report.py
    },
    "memory": {"recent_turns": 4, "recalled_facts": 3, "max_turns": 400},
    "permissions": {"voice_confirm": True, "timeout": 9.0, "attempts": 2,
                    "click_fallback": True},
    "browser": {"headless": False, "show_in_ui": False, "capture_quality": 70,
                "capture_width": 1280, "keep_shots": 40,
                "local_files": True, "external_chrome": True},
    "web": {"max_results": 6, "show_in_ui": True},
    "surface": {"enabled": True, "ratio": 0.70, "max_tabs": 5,
                "live_fps": 6, "embed_apps": False, "park_windows": True},
    "chrome": {"enabled": True,
               "debug_port": 9222,          # local DevTools port, loopback only
               "user_data_dir": "iris",     # iris | system | explicit folder
               "profile_directory": "",     # e.g. Profile 2 in system mode
               "default_profile": "",       # alias from chrome.profiles
               "profiles": {},
               "close_existing": False,     # never kill a running Chrome unasked
               "start_timeout": 12.0},
    "paths": {"workspace": ""},
    "ui": {
        "enabled": True,
        "width": 1020,
        "height": 700,
        "fps": 30,
        "always_on_top": False,
        "avatar": True,                 # small floating Iris outside the window
        "avatar_size": 118,
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
