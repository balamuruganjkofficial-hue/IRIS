"""Text to speech: Edge TTS (free) with a disk cache and a small audio pipeline.

Two worker threads:
  * the synthesiser turns text into mp3 files (one asyncio loop per thread),
  * the player streams them through pygame with a low-latency buffer.

Queued sentences are merged into one request, the next clip is synthesised while
the current one plays, and the gap between clips is logged. A single slow or
failed sentence is spoken by the offline voice while the next sentence still
tries Edge; Edge is only marked down after several real network failures, and a
background probe brings it back automatically.
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
from collections import deque, namedtuple

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import edge_tts
import pygame

from core import bus, connectivity
from core.config import CFG, DATA
from core.text import clean, prosody, sentences, speakable

log = logging.getLogger("iris")

_V = CFG["voice"]
VOICE = _V["tts_voice"]
RATE = _V.get("rate", "+2%")
PITCH = _V.get("pitch", "+0Hz")
PROSODY = bool(_V.get("prosody", True))
STYLE = str(_V.get("style", "warm") or "warm").lower()
RATE_JITTER = int(_V.get("rate_jitter", 4) or 0)
PITCH_JITTER = int(_V.get("pitch_jitter", 3) or 0)
SPEAKABLE = bool(_V.get("speakable", True))
PRONUNCIATIONS = dict(_V.get("pronunciations") or {})
CACHE = DATA / "tts_cache"
CACHE.mkdir(parents=True, exist_ok=True)
MAX_CACHED_CHARS = int(_V.get("cache_chars", 90))
CACHE_FILES = int(_V.get("cache_files", 400))

# Edge budgets. A cold websocket can take several seconds to produce its first
# audio chunk, so one timeout is not evidence that the service is broken.
ENGINE = str(_V.get("engine", "auto") or "auto").lower()
if ENGINE not in ("auto", "edge", "offline"):
    log.warning("unknown voice.engine %r, using auto", ENGINE)
    ENGINE = "auto"
EDGE_RETRIES = max(0, int(_V.get("edge_retries", 2) or 0))
EDGE_CONNECT_TIMEOUT = float(_V.get("edge_connect_timeout", 6.0))
EDGE_FIRST_AUDIO_COLD = float(_V.get("edge_first_audio_cold", 8.0))
EDGE_FIRST_AUDIO_WARM = float(_V.get("edge_first_audio_warm", 5.0))
EDGE_TOTAL_BUDGET = float(_V.get("edge_total_budget", 15.0))
EDGE_COLD_AFTER = float(_V.get("edge_cold_after", 300.0))
EDGE_DOWN_AFTER = max(1, int(_V.get("edge_down_after", 3) or 3))
EDGE_DOWN_WINDOW = float(_V.get("edge_down_window", 60.0))
EDGE_PROBE_INTERVAL = float(_V.get("edge_probe_interval", 20.0))
OFFLINE_ENGINE = str(_V.get("offline_engine", "sapi") or "sapi").lower()
KOKORO_MAX_RTF = float(_V.get("kokoro_max_rtf", 1.5))
OFFLINE_VOICE_NAME = str(_V.get("offline_voice", "Zira") or "Zira")

# SAPI flags: async so stop() can purge it, purge-before-speak for barge-in.
# XML lets the offline voice use the same rate and pitch as the online voice.
_SAPI_ASYNC = 1
_SAPI_PURGE = 2
_SAPI_XML = 8

_OFFLINE_VOICE = None
_OFFLINE_LOCK = threading.Lock()

_EDGE_LOCK = threading.RLock()


class EdgeHealth:
    """Decide when Edge TTS is really down, using an injectable clock.

    One slow sentence is a hiccup, not an outage. Edge is marked down only
    after ``down_after`` failed sentences inside ``window`` seconds. While it is
    down ``probe_due`` says when to try a tiny phrase, and the first success
    clears the state so the next sentence is online again. This class is pure
    so the whole state machine can be tested with a fake clock.
    """

    def __init__(self, clock=time.time, down_after=3, window=60.0,
                 probe_interval=20.0):
        self.clock = clock
        self.down_after = max(1, int(down_after))
        self.window = max(1.0, float(window))
        self.probe_interval = max(1.0, float(probe_interval))
        self.failures = deque()
        self.down = False
        self.down_since = 0.0
        self.probe_at = 0.0
        self.last_success = 0.0

    def reset(self):
        self.failures.clear()
        self.down = False
        self.down_since = 0.0
        self.probe_at = 0.0
        self.last_success = 0.0

    def is_down(self):
        return self.down

    def note_success(self):
        """Clear the state. True only when this changed it (one toast)."""
        changed = self.down
        self.failures.clear()
        self.down = False
        self.down_since = 0.0
        self.probe_at = 0.0
        self.last_success = self.clock()
        return changed

    def note_failure(self):
        """Record one failed sentence. True when Edge has just gone down."""
        now = self.clock()
        self.failures.append(now)
        while self.failures and now - self.failures[0] > self.window:
            self.failures.popleft()
        if not self.down and len(self.failures) >= self.down_after:
            self.down = True
            self.down_since = now
            self.probe_at = now + self.probe_interval
            return True
        return False

    def probe_due(self):
        return self.down and self.clock() >= self.probe_at

    def hurry_probe(self):
        """Ask for a probe at the next opportunity (e.g. the network is back)."""
        if self.down:
            self.probe_at = min(self.probe_at, self.clock())

    def probe_started(self):
        self.probe_at = self.clock() + self.probe_interval

    def is_cold(self, cold_after=None):
        limit = EDGE_COLD_AFTER if cold_after is None else float(cold_after)
        return (self.last_success <= 0.0 or
                self.clock() - self.last_success >= limit)


_HEALTH = EdgeHealth(clock=time.time, down_after=EDGE_DOWN_AFTER,
                     window=EDGE_DOWN_WINDOW,
                     probe_interval=EDGE_PROBE_INTERVAL)

_AUDIO_OK = True
try:
    pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=1024)
except Exception as exc:                    # no sound card / driver problem
    _AUDIO_OK = False
    log.warning("audio output unavailable: %s", exc)


_STREAM_IDLE = 30.0        # seconds of silence allowed once audio has started
_STREAM_TOTAL = 180.0      # hard stop for one pathological request


async def _synth_once(text, path, rate, pitch, cold, cap=None):
    """One Edge attempt, streaming to ``path``.

    The connect and first-audio budgets are separate, because a cold websocket
    can be slow before it says anything but then stream a long reply quickly.
    ``cap`` (what is left of the sentence budget) only limits the wait for the
    first audio; once audio is flowing it is allowed to finish.
    """
    connect = max(1, int(round(EDGE_CONNECT_TIMEOUT)))
    receive = max(1, int(round(
        EDGE_FIRST_AUDIO_COLD if cold else EDGE_FIRST_AUDIO_WARM)))
    # The websocket gets its connect budget first, then the first-audio budget.
    first_budget = float(connect) + float(receive)
    if cap is not None:
        first_budget = max(1.0, min(first_budget, float(cap)))
    communicate = edge_tts.Communicate(
        text, VOICE, rate=rate, pitch=pitch,
        connect_timeout=connect, receive_timeout=receive)
    stream = communicate.stream()
    started = time.monotonic()
    first = None
    written = 0
    try:
        with open(path, "wb") as handle:
            while True:
                elapsed = time.monotonic() - started
                if first is None:
                    limit = first_budget - elapsed
                else:
                    limit = _STREAM_IDLE
                if limit <= 0:
                    raise TimeoutError(
                        "no audio within {0:.1f}s".format(first_budget))
                if elapsed > _STREAM_TOTAL:
                    raise TimeoutError(
                        "stream exceeded {0:.0f}s".format(_STREAM_TOTAL))
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(),
                                                   timeout=limit)
                except StopAsyncIteration:
                    break
                if chunk.get("type") != "audio":
                    continue
                data = chunk.get("data") or b""
                if not data:
                    continue
                if first is None:
                    first = time.monotonic() - started
                handle.write(data)
                written += len(data)
    finally:
        try:
            await stream.aclose()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
    if first is None or written <= 0:
        raise TimeoutError("no audio received")


def _retryable(exc):
    """Bad arguments are permanent; everything else is worth one more try."""
    return not isinstance(exc, (ValueError, TypeError))


def _run_loop(loop, coro):
    return loop.run_until_complete(coro)


def _synth(text, path, rate, pitch, loop=None):
    """Synthesise with up to ``edge_retries`` extra attempts and backoff."""
    loop = loop or _worker_loop()
    start = time.monotonic()
    last = None
    for attempt in range(EDGE_RETRIES + 1):
        remaining = EDGE_TOTAL_BUDGET - (time.monotonic() - start)
        if remaining <= 0.5:
            break
        cold = _HEALTH.is_cold() or attempt > 0
        try:
            _run_loop(loop, _synth_once(text, path, rate, pitch, cold,
                                        cap=remaining))
        except Exception as exc:  # noqa: BLE001 - the caller decides the fallback
            last = exc
            log.info("edge attempt %d/%d failed after %.1fs (%s): %s",
                     attempt + 1, EDGE_RETRIES + 1,
                     time.monotonic() - start, type(exc).__name__,
                     exc or "(no message)")
            if not _retryable(exc) or attempt >= EDGE_RETRIES:
                break
            delay = min(0.4 * (2 ** attempt), max(0.0, remaining - 0.5))
            if delay > 0:
                time.sleep(delay)
            continue
        _edge_success()
        return
    if last is None:
        last = TimeoutError("edge budget exhausted")
    raise last


def _pick_sapi_voice(voices, want=None):
    """Choose the female voice that best matches Edge TTS Ava.

    Windows ships several SAPI voices. The first choice is the named American
    female (Zira by default), then any American female, then any female, so the
    offline voice keeps Iris' gender and accent instead of dropping to David.
    ``voices`` is any iterable of SAPI tokens; a fake list is used in tests.
    """
    want = (want if want is not None else OFFLINE_VOICE_NAME).lower().strip()
    best, best_score = None, -1
    for token in voices or ():
        try:
            description = str(token.GetDescription() or "")
        except Exception:  # noqa: BLE001 - a broken token is just skipped
            continue
        text = description.lower()
        try:
            gender = str(token.GetAttribute("Gender") or "").lower()
        except Exception:  # noqa: BLE001
            gender = ""
        try:
            language = str(token.GetAttribute("Language") or "").lower()
        except Exception:  # noqa: BLE001
            language = ""
        score = 0
        if want and want in text:
            score += 100
        if "female" in gender or "female" in text:
            score += 40
        if "409" in language or "en-us" in text or "united states" in text:
            score += 30
        if "desktop" in text:
            score += 5
        if score > best_score:
            best, best_score = token, score
    return best


def _sapi_ssml(text, rate=RATE, pitch=PITCH):
    """Wrap text in SSML so SAPI follows the same rate and pitch as Edge."""
    from xml.sax.saxutils import escape
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xml:lang="en-US"><prosody rate="{0}" pitch="{1}">{2}</prosody></speak>'
            ).format(rate, pitch, escape(str(text)))


def _offline_voice():
    """Lazy SAPI voice; pywin32 is already a project dependency."""
    global _OFFLINE_VOICE
    if _OFFLINE_VOICE is not None:
        return _OFFLINE_VOICE
    with _OFFLINE_LOCK:
        if _OFFLINE_VOICE is None:
            import win32com.client
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            try:
                chosen = _pick_sapi_voice(voice.GetVoices())
                if chosen is not None:
                    voice.Voice = chosen
                    log.info("offline voice: %s", chosen.GetDescription())
                voice.Volume = 100
            except Exception as exc:  # noqa: BLE001 - keep the default voice
                log.warning("could not choose the offline voice: %s", exc)
            _OFFLINE_VOICE = voice
    return _OFFLINE_VOICE


def _speak_offline(text, gen=None):
    """Speak through Kokoro (opt-in) or Windows SAPI, async where possible."""
    if OFFLINE_ENGINE == "kokoro":
        try:
            from core import kokoro
            if kokoro.speak(text, gen, _PIPE.current_gen):
                return True
            log.debug("kokoro did not speak, using the Windows voice")
        except Exception as exc:  # noqa: BLE001 - never break the fallback
            log.warning("kokoro failed, using the Windows voice: %s", exc)
    try:
        voice = _offline_voice()
        if gen is not None and _PIPE.current_gen() != gen:
            return True
        try:
            voice.Speak(_sapi_ssml(text), _SAPI_ASYNC | _SAPI_XML)
        except Exception:  # noqa: BLE001 - some voices reject SSML
            voice.Speak(text, _SAPI_ASYNC)
        if gen is None:
            return True
        # Wait for SAPI to start and finish, but never longer than this budget.
        started = False
        started_at = time.monotonic()
        deadline = started_at + max(2.0, len(text) * 0.12)
        while _PIPE.current_gen() == gen and time.monotonic() < deadline:
            try:
                state = int(voice.Status.RunningState)
            except Exception:  # noqa: BLE001 - status is best effort
                break
            if state == 2:
                started = True
            elif started and state == 1:
                break
            elif not started and time.monotonic() - started_at > 0.25:
                break
            time.sleep(0.01)
        return True
    except Exception as exc:  # noqa: BLE001 - no sound device must not crash Iris
        log.warning("offline voice failed: %s", exc)
        return False


def _stop_offline():
    """Purge anything SAPI is saying. Safe when no voice was created yet."""
    voice = _OFFLINE_VOICE
    if voice is None:
        return
    try:
        voice.Speak("", _SAPI_ASYNC | _SAPI_PURGE)
    except Exception as exc:  # noqa: BLE001 - stop must never raise
        log.debug("offline voice stop failed: %s", exc)


def _edge_success(reason="edge"):
    """A real Edge synthesis succeeded: clear the outage state, toast once."""
    with _EDGE_LOCK:
        changed = _HEALTH.note_success()
    if changed:
        log.info("%s: Edge TTS enabled again", reason)
        bus.publish("toast", "Voice back online.")


def _edge_failed(_text, exc):
    """Count one failed sentence and use the local voice for this one only.

    Only real network trouble counts towards the outage threshold; a bad
    request (``ValueError``/``TypeError``) is logged and otherwise ignored so a
    config typo can never mark Edge down. The toast fires once, on the
    transition to offline, not for every sentence.
    """
    if not _retryable(exc):
        log.warning("edge tts rejected the request (%s): %s",
                    type(exc).__name__, exc or "(no message)")
        return
    with _EDGE_LOCK:
        went_down = _HEALTH.note_failure()
    log.warning("edge tts failed (%s): %s", type(exc).__name__,
                exc or "(no message)")
    if went_down:
        log.warning("edge tts marked down after %d failures within %.0f s; "
                    "probing every %.0f s", EDGE_DOWN_AFTER, EDGE_DOWN_WINDOW,
                    EDGE_PROBE_INTERVAL)
        bus.publish("toast", "Voice switched to the offline voice.")


def _on_connectivity(payload):
    """The network came back: bring the Edge probe forward, nothing more.

    A working route to the internet is not proof that Edge TTS is healthy, so
    the next probe still has to succeed before the state machine recovers.
    """
    online = (bool(payload.get("online")) if isinstance(payload, dict)
              else bool(payload))
    if online:
        with _EDGE_LOCK:
            _HEALTH.hurry_probe()


def _reset_fallback_for_tests():
    """Return the Edge/SAPI choice to a known state without a toast."""
    with _EDGE_LOCK:
        _HEALTH.reset()


def should_use_offline_voice():
    """True when the next sentence should skip Edge.

    ``voice.engine = offline`` forces the local voice, ``edge`` never does, and
    ``auto`` uses the offline voice only while the health state machine says
    Edge is down. A single failed sentence therefore still tries Edge next.
    """
    if ENGINE == "offline":
        return True
    if ENGINE == "edge":
        return False
    if not connectivity.is_online():
        return True
    with _EDGE_LOCK:
        return _HEALTH.is_down()


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _cache_path(text, rate, pitch):
    key = hashlib.sha1("{0}|{1}|{2}|{3}".format(VOICE, rate, pitch, text)
                       .encode("utf-8")).hexdigest()[:20]
    return str(CACHE / (key + ".mp3"))


def _worker_loop():
    """The current thread's asyncio loop, created once and reused."""
    loop = getattr(_TLS, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _TLS.loop = loop
    return loop


def _prepare(text, loop=None):
    """Returns (audio_path, keep). Short sentences are cached on disk."""
    rate, pitch = prosody(text, RATE, PITCH, RATE_JITTER, PITCH_JITTER,
                          PROSODY, STYLE)
    if len(text) <= MAX_CACHED_CHARS:
        cp = _cache_path(text, rate, pitch)
        if os.path.exists(cp):
            return cp, True
        tmp = cp + "." + uuid.uuid4().hex[:6] + ".tmp"
        try:
            _synth(text, tmp, rate, pitch, loop)
            os.replace(tmp, cp)
        except Exception:
            _remove(tmp)
            raise
        return cp, True
    path = os.path.join(tempfile.gettempdir(), "iris_" + uuid.uuid4().hex + ".mp3")
    try:
        _synth(text, path, rate, pitch, loop)
    except Exception:
        _remove(path)
        raise
    return path, False


_TLS = threading.local()


def _edge_synth(text):
    """Synthesise on the worker thread's own asyncio loop."""
    return _prepare(text, _worker_loop())


_PROBE_TEXT = "Okay."


def _edge_probe_once():
    """One cheap Edge request, used only while the state machine says down."""
    loop = _worker_loop()
    path = os.path.join(tempfile.gettempdir(),
                        "iris_probe_" + uuid.uuid4().hex + ".mp3")
    try:
        _run_loop(loop, _synth_once(_PROBE_TEXT, path, RATE, PITCH, True))
        _edge_success("probe")
        return True
    except Exception as exc:  # noqa: BLE001 - a failed probe keeps Edge down
        log.info("edge probe failed (%s): %s", type(exc).__name__,
                 exc or "(no message)")
        return False
    finally:
        _remove(path)


def _probe_worker():
    """While Edge is down, try a tiny phrase every ``edge_probe_interval``."""
    while True:
        time.sleep(2.0)
        try:
            if ENGINE == "offline" or not connectivity.is_online():
                continue
            with _EDGE_LOCK:
                due = _HEALTH.probe_due()
                if due:
                    _HEALTH.probe_started()
            if not due:
                continue
            # Never compete with a reply: a second Edge stream doubles latency.
            if _PIPE.is_speaking() or _PIPE.pending_total() > 0:
                with _EDGE_LOCK:
                    _HEALTH.hurry_probe()
                continue
            _edge_probe_once()
        except Exception as exc:  # noqa: BLE001 - the prober must never die
            log.debug("edge prober: %s", exc)


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


class GapMeter:
    """Measure the silence between consecutive chunks of one utterance."""

    def __init__(self, clock=time.perf_counter, max_gap=2.0):
        self._clock = clock
        self._max_gap = float(max_gap)
        self._lock = threading.Lock()
        self._end = None
        self._gen = None
        self.last = None
        self.worst = 0.0
        self.count = 0

    def start(self, gen):
        now = self._clock()
        with self._lock:
            gap = None
            if self._end is not None and self._gen == gen:
                candidate = max(0.0, now - self._end)
                if candidate <= self._max_gap:
                    gap = candidate
                    self.last = candidate
                    self.worst = max(self.worst, candidate)
                    self.count += 1
            self._gen = gen
            return gap

    def end(self, gen):
        with self._lock:
            if self._gen == gen:
                self._end = self._clock()

    def reset(self):
        with self._lock:
            self._end = None
            self._gen = None

    def stats(self):
        with self._lock:
            return {"last": self.last, "worst": self.worst, "count": self.count}


class _TextQueue:
    """Text waiting for synthesis, with batch extraction for merging."""

    def __init__(self):
        self._items = deque()
        self._cond = threading.Condition()

    def put(self, gen, text, solo=False):
        with self._cond:
            self._items.append((gen, text, bool(solo)))
            self._cond.notify()

    def get_batch(self):
        with self._cond:
            while not self._items:
                self._cond.wait()
            gen, text, solo = self._items.popleft()
            parts = [text]
            if not solo:
                while self._items:
                    ngen, ntext, nsolo = self._items[0]
                    if ngen != gen or nsolo:
                        break
                    self._items.popleft()
                    parts.append(ntext)
            return gen, " ".join(parts), len(parts)

    def drain(self):
        with self._cond:
            items = list(self._items)
            self._items.clear()
            return items


_AudioItem = namedtuple("_AudioItem", "gen path keep count text")


class SpeechPipeline:
    """Merge, prefetch, play and barge-in with injectable fakes for tests."""

    def __init__(self, synth, play, offline=None, use_offline=None,
                 fallback=None, stop_audio=None, clock=time.perf_counter):
        self._synth = synth
        self._play = play
        self._offline = offline
        self._use_offline = use_offline or (lambda: False)
        self._fallback = fallback
        self._stop_audio = stop_audio or (lambda: None)
        self._clock = clock
        self._text_q = _TextQueue()
        self._audio_q = queue.Queue()
        self._gen = 0
        self._gen_lock = threading.RLock()
        self._pending = {}
        self._pending_cond = threading.Condition()
        self._gap = GapMeter(clock)
        self._started = False
        self._threads = []

    def start(self):
        if self._started:
            return self
        self._started = True
        for name, fn in (("tts-synth", self._synth_worker),
                         ("tts-play", self._play_worker)):
            thread = threading.Thread(target=fn, daemon=True, name=name)
            thread.start()
            self._threads.append(thread)
        return self

    def current_gen(self):
        with self._gen_lock:
            return self._gen

    def _add(self, gen, count=1):
        with self._pending_cond:
            self._pending[gen] = self._pending.get(gen, 0) + count
            self._pending_cond.notify_all()

    def _done(self, gen, count=1):
        with self._pending_cond:
            left = self._pending.get(gen, 0) - count
            if left > 0:
                self._pending[gen] = left
            else:
                self._pending.pop(gen, None)
            self._pending_cond.notify_all()

    def pending_total(self):
        with self._pending_cond:
            return sum(self._pending.values())

    def gap_stats(self):
        return self._gap.stats()

    def speak(self, text):
        text = clean(text)
        if not text:
            return
        if SPEAKABLE:
            # The UI already showed the original reply; only the voice hears
            # the normalised version. If nothing speakable is left (a line of
            # emoji or a bare URL), say nothing instead of reading symbols.
            text = speakable(text, PRONUNCIATIONS)
            if not text:
                return
        parts = sentences(text)
        if not parts:
            return
        chunks = [parts[0]]
        if len(parts) > 1:
            chunks.append(" ".join(parts[1:]))
        with self._gen_lock:
            gen = self._gen
            with self._pending_cond:
                first = self._pending.get(gen, 0) == 0
            if first:
                self._gap.reset()
            for index, chunk in enumerate(chunks):
                self._add(gen, 1)
                self._text_q.put(gen, chunk, solo=first and index == 0)

    def wait_done(self):
        """Block until the current generation has been spoken or dropped."""
        while True:
            with self._gen_lock:
                gen = self._gen
            with self._pending_cond:
                if self._pending.get(gen, 0) <= 0:
                    return
                self._pending_cond.wait(0.05)

    def is_speaking(self):
        with self._gen_lock:
            gen = self._gen
        with self._pending_cond:
            return self._pending.get(gen, 0) > 0

    def stop(self):
        """Barge-in: silence audio and cancel every queued chunk immediately."""
        with self._gen_lock:
            self._gen += 1
            text_items = self._text_q.drain()
            audio_items = []
            try:
                while True:
                    audio_items.append(self._audio_q.get_nowait())
            except queue.Empty:
                pass
            for gen, _text, _solo in text_items:
                self._done(gen, 1)
            for item in audio_items:
                if item.path and not item.keep:
                    _remove(item.path)
                self._done(item.gen, item.count)
            self._gap.reset()
            with self._pending_cond:
                self._pending_cond.notify_all()
        self._stop_audio()
        bus.publish("state", "idle")       # the player may never reach its finally

    def _enqueue_offline(self, gen, text, count):
        if gen != self.current_gen():
            self._done(gen, count)
            return
        self._audio_q.put(_AudioItem(gen, None, True, count, text))

    def _synth_worker(self):
        while True:
            gen, text, count = self._text_q.get_batch()
            if gen != self.current_gen():
                self._done(gen, count)
                continue
            if self._use_offline():
                self._enqueue_offline(gen, text, count)
                continue
            try:
                path, keep = self._synth(text)
            except Exception as exc:  # noqa: BLE001 - fallback must always run
                if gen != self.current_gen():
                    self._done(gen, count)  # stopped while Edge was working
                    continue
                log.warning("edge tts failed: %s", exc)
                if self._fallback:
                    try:
                        self._fallback(text, exc)
                    except Exception:  # noqa: BLE001 - toast is best effort
                        pass
                self._enqueue_offline(gen, text, count)
                continue
            if gen != self.current_gen():
                if not keep:
                    _remove(path)
                self._done(gen, count)
                continue
            self._audio_q.put(_AudioItem(gen, path, keep, count, None))

    def _play_worker(self):
        while True:
            item = self._audio_q.get()
            started = False
            try:
                if item.gen == self.current_gen():
                    bus.publish("state", "speaking")
                    gap = self._gap.start(item.gen)
                    if gap is not None:
                        log.info("tts gap %d ms", int(gap * 1000))
                        if gap > 0.3:
                            log.warning("tts gap over 300 ms: %d ms",
                                        int(gap * 1000))
                    started = True
                    if item.path is None:
                        if self._offline:
                            self._offline(item.text, item.gen)
                    else:
                        self._play(item.path, item.gen)
            except Exception as exc:  # noqa: BLE001 - playback must not die
                log.warning("playback failed: %s", exc)
                bus.publish("toast", "Audio playback problem.")
            finally:
                if item.path and not item.keep:
                    _remove(item.path)
                if started and item.gen == self.current_gen():
                    self._gap.end(item.gen)
                else:
                    self._gap.reset()
                self._done(item.gen, item.count)
                if item.gen == self.current_gen() and not self.is_speaking():
                    bus.publish("state", "idle")


def _play_edge(path, gen):
    """Play one mp3 and stop promptly when the generation changes."""
    if not _AUDIO_OK or _PIPE.current_gen() != gen:
        return
    pygame.mixer.music.load(path)
    if _PIPE.current_gen() != gen:
        return
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy() and _PIPE.current_gen() == gen:
        pygame.time.wait(25)
    pygame.mixer.music.stop()
    pygame.mixer.music.unload()


def _stop_all_audio():
    """Silence pygame, Kokoro and SAPI in one call, used by stop()."""
    if _AUDIO_OK:
        try:
            pygame.mixer.stop()          # Kokoro Sound channels
        except Exception:  # noqa: BLE001 - stop must never raise
            pass
        try:
            pygame.mixer.music.stop()    # Edge mp3 playback
        except Exception:  # noqa: BLE001 - stop must never raise
            pass
    _stop_offline()


_PIPE = SpeechPipeline(
    synth=_edge_synth,
    play=_play_edge,
    offline=_speak_offline,
    use_offline=should_use_offline_voice,
    fallback=_edge_failed,
    stop_audio=_stop_all_audio,
)
_PIPE.start()
bus.subscribe("connectivity", _on_connectivity)

threading.Thread(target=_probe_worker, daemon=True, name="tts-probe").start()
threading.Thread(target=_prune_cache, daemon=True, name="tts-prune").start()


def speak(text):
    _PIPE.speak(text)


def wait_done():
    """Block until everything queued has finished playing."""
    _PIPE.wait_done()


def is_speaking():
    return _PIPE.is_speaking()


def stop():
    """Barge-in: drop everything queued and cut the current clip."""
    _PIPE.stop()


def prewarm(phrases):
    """Synthesise common phrases in the background so they play instantly."""
    def _run():
        if should_use_offline_voice():
            return                          # no point synthesising online clips
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for phrase in phrases:
            # Never compete with a real reply: a second Edge stream roughly
            # doubles the latency, and the greeting matters more than a cache.
            while _PIPE.is_speaking() or _PIPE.pending_total() > 0:
                time.sleep(0.2)
            if should_use_offline_voice():
                return
            if SPEAKABLE:
                phrase = speakable(phrase, PRONUNCIATIONS) or phrase
            try:
                _prepare(phrase, loop)
            except Exception:
                return
    threading.Thread(target=_run, daemon=True, name="tts-prewarm").start()


_call = CFG["persona"]["call_me"]
prewarm(["Iris online.", "Done.", "Opened Chrome.", "Searching.", "Opened notepad.",
         "Opened explorer.", "Opened calculator.", "Screenshot saved.",
         "Hello, {0}.".format(_call), "Yes, {0}?".format(_call)])
