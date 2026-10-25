import asyncio
import hashlib
import os
import queue
import re
import tempfile
import threading
import time
import uuid

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import edge_tts
import pygame
from core.config import CFG, ROOT

VOICE = CFG["voice"]["tts_voice"]
RATE = CFG["voice"].get("rate", "+10%")
CACHE = ROOT / "data" / "tts_cache"
CACHE.mkdir(parents=True, exist_ok=True)
MAX_CACHED_CHARS = 70          # short sentences are cached and replay instantly
pygame.mixer.init()

_text_q = queue.Queue()        # (gen, text) waiting to become audio
_audio_q = queue.Queue()       # (gen, path, keep) audio ready to play
_gen = 0                       # stop() bumps this so old items are dropped


async def _synth(text, path):
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _cache_path(text):
    key = hashlib.sha1(f"{VOICE}|{RATE}|{text}".encode("utf-8")).hexdigest()[:20]
    return str(CACHE / (key + ".mp3"))


def _prepare(text):
    """Returns (audio_path, keep). Short sentences are cached on disk."""
    if len(text) <= MAX_CACHED_CHARS:
        cp = _cache_path(text)
        if os.path.exists(cp):
            return cp, True
        tmp = cp + "." + uuid.uuid4().hex[:6] + ".tmp"
        try:
            asyncio.run(_synth(text, tmp))
            os.replace(tmp, cp)
        except Exception:
            _remove(tmp)
            raise
        return cp, True
    path = os.path.join(tempfile.gettempdir(), f"iris_{uuid.uuid4().hex}.mp3")
    try:
        asyncio.run(_synth(text, path))
    except Exception:
        _remove(path)
        raise
    return path, False


def _synth_worker():
    while True:
        gen, text = _text_q.get()
        if gen != _gen:
            _text_q.task_done()
            continue
        t0 = time.time()
        try:
            path, keep = _prepare(text)
            print(f"[voice] ready in {time.time() - t0:.1f}s")
        except Exception as e:
            print("[voice error]", e)
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
            if gen == _gen:
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy() and gen == _gen:
                    pygame.time.wait(30)
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
        except Exception as e:
            print("[voice error]", e)
        finally:
            if not keep:
                _remove(path)
            _text_q.task_done()


threading.Thread(target=_synth_worker, daemon=True).start()
threading.Thread(target=_play_worker, daemon=True).start()


def speak(text):
    text = re.sub(r"[*_`#]", "", text).strip()
    if not text:
        return
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    chunks = [sentences[0]]
    if len(sentences) > 1:
        chunks.append(" ".join(sentences[1:]))
    for c in chunks:
        _text_q.put((_gen, c))


def wait_done():
    _text_q.join()


def stop():
    global _gen
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


def prewarm(phrases):
    """Generate common phrases in the background so they play instantly."""
    def _run():
        for p in phrases:
            try:
                _prepare(p)
            except Exception:
                return
    threading.Thread(target=_run, daemon=True).start()


_call = CFG["persona"]["call_me"]
prewarm(["Iris online.", "Done.", "Opened Chrome.", "Searching.", "Opened notepad.",
         "Opened explorer.", "Opened calculator.", "Screenshot saved.",
         f"Hello, {_call}.", f"Yes, {_call}?"])