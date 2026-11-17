"""Text to speech: Edge TTS (free) with a disk cache and a small audio pipeline.

Two worker threads:
  * the synthesiser turns text into mp3 files (one asyncio loop, reused),
  * the player streams them through pygame with a low-latency buffer.

Short, frequent sentences are cached on disk and pruned so the cache cannot
grow forever. Every state change is announced on the bus for the interface.
"""

import asyncio
import hashlib
import logging
import os
import queue
import tempfile
import threading
import time
import uuid

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import edge_tts
import pygame

from core import bus
from core.config import CFG, DATA
from core.text import clean, prosody, sentences

log = logging.getLogger("iris")

_V = CFG["voice"]
VOICE = _V["tts_voice"]
RATE = _V.get("rate", "+8%")
PITCH = _V.get("pitch", "+0Hz")
PROSODY = bool(_V.get("prosody", True))
RATE_JITTER = int(_V.get("rate_jitter", 4) or 0)
PITCH_JITTER = int(_V.get("pitch_jitter", 3) or 0)
CACHE = DATA / "tts_cache"
CACHE.mkdir(parents=True, exist_ok=True)
MAX_CACHED_CHARS = int(_V.get("cache_chars", 90))
CACHE_FILES = int(_V.get("cache_files", 400))

_text_q = queue.Queue()         # (gen, text) waiting to be synthesised
_audio_q = queue.Queue()        # (gen, path, keep) ready to play
_gen = 0
_gen_lock = threading.Lock()

_AUDIO_OK = True
try:
    pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=1024)
except Exception as exc:                    # no sound card / driver problem
    _AUDIO_OK = False
    log.warning("audio output unavailable: %s", exc)


async def _synth(text, path, rate, pitch):
    await edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch).save(path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _cache_path(text, rate, pitch):
    key = hashlib.sha1("{0}|{1}|{2}|{3}".format(VOICE, rate, pitch, text)
                       .encode("utf-8")).hexdigest()[:20]
    return str(CACHE / (key + ".mp3"))


def _prepare(text, loop):
    """Returns (audio_path, keep). Short sentences are cached on disk."""
    rate, pitch = prosody(text, RATE, PITCH, RATE_JITTER, PITCH_JITTER, PROSODY)
    if len(text) <= MAX_CACHED_CHARS:
        cp = _cache_path(text, rate, pitch)
        if os.path.exists(cp):
            return cp, True
        tmp = cp + "." + uuid.uuid4().hex[:6] + ".tmp"
        try:
            loop.run_until_complete(_synth(text, tmp, rate, pitch))
            os.replace(tmp, cp)
        except Exception:
            _remove(tmp)
            raise
        return cp, True
    path = os.path.join(tempfile.gettempdir(), "iris_" + uuid.uuid4().hex + ".mp3")
    try:
        loop.run_until_complete(_synth(text, path, rate, pitch))
    except Exception:
        _remove(path)
        raise
    return path, False


def _prune_cache():
    """Keep the newest few hundred clips and drop abandoned temp files."""
    while True:
        try:
            now = time.time()
            for tmp in CACHE.glob("*.tmp"):
                try:
                    if now - tmp.stat().st_mtime > 1800:
                        tmp.unlink()
                except OSError:
                    pass
            clips = sorted(CACHE.glob("*.mp3"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in clips[CACHE_FILES:]:
                _remove(str(old))
        except Exception:
            pass
        time.sleep(900)


def _synth_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        gen, text = _text_q.get()
        if gen != _gen:
            _text_q.task_done()
            continue
        t0 = time.time()
        try:
            path, keep = _prepare(text, loop)
            log.info("tts ready in %.1fs (%d chars)", time.time() - t0, len(text))
        except Exception as exc:
            log.warning("tts failed: %s", exc)
            bus.publish("toast", "Voice synthesis failed.")
            _text_q.task_done()
            continue
        if gen != _gen:
            if not keep:
                _remove(path)
            _text_q.task_done()
        else:
            _audio_q.put((gen, path, keep))


def _play_worker():
    while True:
        gen, path, keep = _audio_q.get()
        try:
            if gen == _gen and _AUDIO_OK:
                bus.publish("state", "speaking")
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy() and gen == _gen:
                    pygame.time.wait(25)
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
        except Exception as exc:
            log.warning("playback failed: %s", exc)
            bus.publish("toast", "Audio playback problem.")
        finally:
            if not keep:
                _remove(path)
            _text_q.task_done()
        if gen == _gen and _audio_q.empty() and _text_q.unfinished_tasks == 0:
            bus.publish("state", "idle")


threading.Thread(target=_synth_worker, daemon=True, name="tts-synth").start()
threading.Thread(target=_play_worker, daemon=True, name="tts-play").start()
threading.Thread(target=_prune_cache, daemon=True, name="tts-prune").start()


def speak(text):
    text = clean(text)
    if not text:
        return
    parts = sentences(text)
    if not parts:
        return
    chunks = [parts[0]]
    if len(parts) > 1:
        chunks.append(" ".join(parts[1:]))
    with _gen_lock:
        gen = _gen
    for chunk in chunks:
        _text_q.put((gen, chunk))


def wait_done():
    """Block until everything queued has finished playing."""
    _text_q.join()


def is_speaking():
    return not _audio_q.empty() or _text_q.unfinished_tasks > 0


def stop():
    """Barge-in: drop everything queued and cut the current clip."""
    global _gen
    with _gen_lock:
        _gen += 1
    try:
        while True:
            _text_q.get_nowait()
            _text_q.task_done()
    except queue.Empty:
        pass
    try:
        while True:
            _, path, keep = _audio_q.get_nowait()
            if not keep:
                _remove(path)
            _text_q.task_done()
    except queue.Empty:
        pass
    if _AUDIO_OK:
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass


def prewarm(phrases):
    """Synthesise common phrases in the background so they play instantly."""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for phrase in phrases:
            try:
                _prepare(phrase, loop)
            except Exception:
                return
    threading.Thread(target=_run, daemon=True, name="tts-prewarm").start()


_call = CFG["persona"]["call_me"]
prewarm(["Iris online.", "Done.", "Opened Chrome.", "Searching.", "Opened notepad.",
         "Opened explorer.", "Opened calculator.", "Screenshot saved.",
         "Hello, {0}.".format(_call), "Yes, {0}?".format(_call)])
