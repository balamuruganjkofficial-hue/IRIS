"""Optional high-quality offline voice: Kokoro through ``kokoro-onnx``.

This is opt-in only. Set ``voice.offline_engine: kokoro`` after running
``scripts/fetch_kokoro.py`` once. Importing this module is cheap: the model is
loaded in the TTS worker thread the first time an offline sentence is actually
spoken, and the real-time factor is measured then. A laptop that cannot run it
faster than ``voice.kokoro_max_rtf`` keeps the Windows SAPI voice, and any
failure here is reported as "not spoken" so the caller falls back silently.
"""

import logging
import os
import threading
import time

from core.config import CFG, ROOT

log = logging.getLogger("iris")

_V = CFG.get("voice") or {}
MODEL = str(_V.get("kokoro_model", "models/kokoro-v1.0.int8.onnx"))
VOICES = str(_V.get("kokoro_voices", "models/voices-v1.0.bin"))
VOICE = str(_V.get("kokoro_voice", "af_heart"))
SPEED = float(_V.get("kokoro_speed", 1.0) or 1.0)
MAX_RTF = float(_V.get("kokoro_max_rtf", 1.5) or 1.5)

_PROBE_TEXT = "This is a quick check of the offline voice."
_MIXER_RATE = 24000                     # core.voice initialises the mixer at this

_LOCK = threading.Lock()
_ENGINE = None
_FAILED = False
_STATUS = {"rtf": None, "reason": "not tried"}


def _path(value):
    value = str(value or "")
    if not value:
        return value
    if not os.path.isabs(value):
        value = os.path.join(str(ROOT), value)
    return os.path.normpath(value)


def model_path():
    return _path(MODEL)


def voices_path():
    return _path(VOICES)


def available():
    """True when the package and both model files exist. Does not load them."""
    if not (os.path.isfile(model_path()) and os.path.isfile(voices_path())):
        return False
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 - not installed is a normal state
        return False
    return True


def status():
    """Small dict for diagnostics; never raises and never loads the model."""
    data = dict(_STATUS)
    data.update({"available": available(), "loaded": _ENGINE is not None,
                 "failed": _FAILED, "voice": VOICE,
                 "model": model_path(), "voices": voices_path()})
    return data


def measure_rtf(engine=None, text=_PROBE_TEXT):
    """Seconds of compute per second of audio. Above 1.0 is slower than realtime."""
    engine = engine if engine is not None else _get_engine(measure=False)
    if engine is None:
        return None
    start = time.perf_counter()
    try:
        samples, rate = engine.create(text, voice=VOICE, speed=SPEED,
                                      lang="en-us")
    except Exception as exc:  # noqa: BLE001 - measurement is best effort
        log.warning("kokoro measurement failed: %s", exc)
        return None
    elapsed = time.perf_counter() - start
    try:
        audio_seconds = len(samples) / float(rate)
    except Exception:  # noqa: BLE001
        return None
    if audio_seconds <= 0:
        return None
    return elapsed / audio_seconds


def _get_engine(measure=True):
    """Load once, in the calling thread, and refuse a model that is too slow."""
    global _ENGINE, _FAILED
    if _ENGINE is not None:
        return _ENGINE
    if _FAILED:
        return None
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        if _FAILED:
            return None
        if not available():
            _FAILED = True
            _STATUS["reason"] = "kokoro-onnx or the model files are missing"
            log.info("kokoro not available: %s", _STATUS["reason"])
            return None
        try:
            from kokoro_onnx import Kokoro
            engine = Kokoro(model_path(), voices_path())
        except Exception as exc:  # noqa: BLE001 - never break the fallback
            _FAILED = True
            _STATUS["reason"] = "load failed: {0}: {1}".format(
                type(exc).__name__, exc)
            log.warning("kokoro could not load, using the Windows voice: %s", exc)
            return None
        if measure:
            rtf = measure_rtf(engine)
            _STATUS["rtf"] = rtf
            if rtf is None:
                _FAILED = True
                _STATUS["reason"] = "the speed check failed"
                log.warning("kokoro speed check failed, using the Windows voice")
                return None
            if rtf > MAX_RTF:
                _FAILED = True
                _STATUS["reason"] = "too slow ({0:.2f}x real time)".format(rtf)
                log.warning("kokoro is %.2fx real time (limit %.1fx), using the "
                            "Windows voice", rtf, MAX_RTF)
                return None
            log.info("kokoro ready: %s at %.2fx real time", VOICE, rtf)
        _ENGINE = engine
        _STATUS["reason"] = "ready"
        return _ENGINE


def speak(text, gen=None, current_gen=None):
    """Speak ``text`` with Kokoro. True when spoken, False to use SAPI.

    ``gen``/``current_gen`` mirror the Edge player so barge-in stops this voice
    as promptly as it stops an mp3.
    """
    text = str(text or "").strip()
    if not text:
        return False
    if gen is not None and current_gen is not None and current_gen() != gen:
        return True                         # cancelled before it started
    try:
        import numpy as np
        import pygame
    except Exception as exc:  # noqa: BLE001 - missing numpy/pygame means SAPI
        log.debug("kokoro playback unavailable: %s", exc)
        return False
    engine = _get_engine()
    if engine is None:
        return False
    try:
        samples, rate = engine.create(text, voice=VOICE, speed=SPEED,
                                      lang="en-us")
    except Exception as exc:  # noqa: BLE001 - synthesis must never crash
        log.warning("kokoro synthesis failed: %s", exc)
        return False
    if samples is None or not rate:
        return False
    if int(rate) != _MIXER_RATE:
        log.warning("kokoro sample rate %s does not match the mixer", rate)
        return False
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1).reshape(-1)
    if gen is not None and current_gen is not None and current_gen() != gen:
        return True                         # cancelled while synthesising
    try:
        channel = pygame.mixer.Sound(buffer=stereo.tobytes()).play()
    except Exception as exc:  # noqa: BLE001 - no device means SAPI
        log.warning("kokoro playback failed: %s", exc)
        return False
    try:
        while channel.get_busy():
            if gen is not None and current_gen is not None and current_gen() != gen:
                break
            pygame.time.wait(20)
    finally:
        try:
            channel.stop()
        except Exception:  # noqa: BLE001 - stop must never raise
            pass
    return True
