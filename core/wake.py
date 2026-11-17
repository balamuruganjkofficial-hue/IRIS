"""Wake-word detection, offline and free.

Two engines share one tiny interface so ``core/ears.py`` does not care which is
in use::

    model = wake.load()
    scores = model.predict(frame)   # frame = numpy int16, 16 kHz mono
    model.reset()

* **Vosk** (default) is a small offline speech recogniser constrained by a
  grammar built from the configured wake phrases, so saying "Iris", "hey Iris",
  "hi Iris" or "okay Iris" wakes her - and "Jarvis" still works as an alias.
  The 41 MB English model is downloaded once, for free, into ``data/vosk``.
* **openWakeWord** remains as a fallback for custom ``.onnx`` models or when
  Vosk cannot be installed. Its bundled ``hey_jarvis`` model is used only when
  the user explicitly asks for that engine.

Nothing here talks to a cloud service: once the model file is on disk the wake
word works with the network unplugged.
"""

import json
import logging
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

from core.config import CFG, DATA, ROOT

log = logging.getLogger("iris")

_MODEL = None
_ENGINE = "none"
_LOCK = threading.Lock()
_TOKEN = re.compile(r"[a-z0-9']+")

DEFAULT_PHRASES = ["iris", "hey iris", "hi iris", "hello iris", "okay iris",
                   "ok iris", "jarvis", "hey jarvis"]
DEFAULT_VOSK_URL = ("https://alphacephei.com/vosk/models/"
                    "vosk-model-small-en-us-0.15.zip")
_OWW_BUILTIN = {"hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy",
                "timer", "weather"}


# ------------------------------------------------------------------ helpers
def _voice_cfg():
    return CFG.get("voice") or {}


def phrases():
    """Configured wake phrases, plus the single wake word, lowercased."""
    cfg = _voice_cfg()
    out = []
    for item in cfg.get("wake_phrases") or DEFAULT_PHRASES:
        text = re.sub(r"\s+", " ", str(item or "").replace("_", " ")).strip().lower()
        if text and text not in out:
            out.append(text)
    word = str(cfg.get("wake_word", "iris") or "").replace("_", " ").strip().lower()
    if word and not word.endswith(".onnx") and word not in out:
        out.insert(0, word)
    return out


def _tokens(text):
    return _TOKEN.findall((text or "").lower())


def _matches(text, wanted):
    """True when the tail of the recognised text ends with a wake phrase."""
    got = _tokens(text)
    if not got:
        return False
    for phrase in wanted:
        words = _tokens(phrase)
        if words and len(got) >= len(words) and got[-len(words):] == words:
            return True
    # A single distinctive word is enough ("Iris"), even mid-sentence.
    for phrase in wanted:
        words = _tokens(phrase)
        if len(words) == 1 and words[0] in got:
            return True
    return False


def _model_dir():
    cfg = _voice_cfg()
    raw = str(cfg.get("vosk_model") or "data/vosk/vosk-model-small-en-us-0.15")
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def _model_ready(path):
    return (path / "am" / "final.mdl").exists() or (path / "conf").exists()


def _download_model(path):
    """Fetch and unpack the small English model once. Returns True on success."""
    cfg = _voice_cfg()
    if not bool(cfg.get("vosk_download", True)):
        return False
    if _model_ready(path):
        return True
    url = str(cfg.get("vosk_url") or DEFAULT_VOSK_URL)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    archive = parent / "vosk-model.zip"
    tmp = parent / "vosk-model-unpack"
    try:
        import requests
        log.info("downloading the free Vosk wake-word model (about 41 MB)")
        try:
            from core import bus
            bus.publish("toast", "Teaching myself to hear 'Iris'…")
        except Exception:  # noqa: BLE001
            pass
        with requests.get(url, stream=True, timeout=(10, 120)) as response:
            response.raise_for_status()
            with open(archive, "wb") as fh:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        source = roots[0] if len(roots) == 1 else tmp
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        shutil.move(str(source), str(path))
        log.info("wake-word model ready at %s", path)
        try:
            from core import bus
            bus.publish("toast", "Wake word ready: say 'Iris'.")
        except Exception:  # noqa: BLE001
            pass
        return _model_ready(path)
    except Exception as e:  # noqa: BLE001 - fall back to openWakeWord
        log.warning("could not download the Vosk model: %s", e)
        return False
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------- engines
class VoskWake:
    """Grammar-constrained offline keyword spotting."""

    name = "vosk"

    def __init__(self, directory, wanted):
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        self.path = str(directory)
        self.wanted = list(wanted)
        self.model = Model(self.path)
        self._grammar = json.dumps(self.wanted + ["[unk]"])
        self._make()

    def _make(self):
        from vosk import KaldiRecognizer
        self.rec = KaldiRecognizer(self.model, 16000, self._grammar)
        self.rec.SetWords(False)

    def reset(self):
        # A fresh recogniser drops any half-heard phrase from before a pause.
        try:
            self._make()
        except Exception:  # noqa: BLE001
            pass

    def predict(self, frame):
        try:
            if hasattr(frame, "tobytes"):
                data = frame.astype("int16", copy=False).tobytes()
            else:
                data = bytes(frame)
            if self.rec.AcceptWaveform(data):
                text = json.loads(self.rec.Result() or "{}").get("text", "")
                final = True
            else:
                text = json.loads(self.rec.PartialResult() or "{}").get("partial", "")
                final = False
        except Exception:  # noqa: BLE001
            return {}
        if not _matches(text, self.wanted):
            return {}
        # A finished phrase is much more trustworthy than a partial one.
        return {"iris": 0.98 if final else 0.80}


class OpenWakeWord:
    """openWakeWord's ONNX models, for custom or bundled wake words."""

    name = "openwakeword"

    def __init__(self, names):
        import openwakeword
        from openwakeword.model import Model
        self.names = list(names)
        try:
            self.model = Model(wakeword_models=self.names,
                               inference_framework="onnx")
        except Exception:
            openwakeword.utils.download_models()
            self.model = Model(wakeword_models=self.names,
                               inference_framework="onnx")

    def reset(self):
        try:
            self.model.reset()
        except Exception:  # noqa: BLE001
            pass

    def predict(self, frame):
        try:
            scores = self.model.predict(frame)
        except Exception:  # noqa: BLE001
            return {}
        return {k: float(v) for k, v in (scores or {}).items()}


# --------------------------------------------------------------------- setup
def _oww_names():
    cfg = _voice_cfg()
    names = []
    word = str(cfg.get("wake_word", "iris") or "").strip()
    if word.lower().endswith(".onnx"):
        p = Path(word)
        names.append(str(p if p.is_absolute() else ROOT / p))
    elif word.replace(" ", "_").lower() in _OWW_BUILTIN:
        names.append(word.replace(" ", "_"))
    else:
        log.info("no bundled openWakeWord model for %r; using the fallback", word)
    fallback = str(cfg.get("wake_fallback", "hey_jarvis") or "").strip()
    if fallback and fallback not in names:
        names.append(fallback)
    return names


def _build():
    cfg = _voice_cfg()
    engine = str(cfg.get("wake_engine", "auto") or "auto").lower()
    wanted = phrases()
    word = str(cfg.get("wake_word", "iris") or "").strip()

    if engine in ("auto", "vosk") and not word.lower().endswith(".onnx"):
        directory = _model_dir()
        if not _model_ready(directory) and not _download_model(directory):
            if engine == "vosk":
                raise RuntimeError("the Vosk wake-word model is not available")
            log.warning("Vosk unavailable; falling back to openWakeWord")
        else:
            try:
                return VoskWake(directory, wanted)
            except Exception as e:  # noqa: BLE001
                if engine == "vosk":
                    raise
                log.warning("Vosk could not start (%s); using openWakeWord", e)

    return OpenWakeWord(_oww_names())


def load():
    """Load (once) and return the active wake-word model."""
    global _MODEL, _ENGINE
    with _LOCK:
        if _MODEL is None:
            _MODEL = _build()
            _ENGINE = getattr(_MODEL, "name", "unknown")
            log.info("wake engine: %s", _ENGINE)
        return _MODEL


# Backwards-compatible private name used by the ears.
_load = load


def engine():
    return _ENGINE


def warmup():
    """Load in a background thread so the first wake word is not delayed."""
    def _run():
        try:
            load()
        except Exception as e:  # noqa: BLE001
            log.warning("wake word unavailable: %s", e)
    threading.Thread(target=_run, daemon=True, name="wake-warmup").start()
