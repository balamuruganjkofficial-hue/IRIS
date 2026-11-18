"""Per-turn latency tracing with a rotated CSV log.

One turn starts when the user stops speaking (or submits a typed command) and
ends when the last spoken word has finished playing. Each stage boundary is a
single timestamp; the tracer derives short *durations* from them so the report
can compare stages fairly. All work here is a handful of assignments plus one
small file append per turn, so the overhead is negligible even on the laptop.

The module keeps a process-wide ``TRACE`` used by the assistant. Tests can build
their own :class:`Tracer` with an injected clock and a temporary CSV path.
Nothing in this module imports the brain, the voice engine or the UI.
"""

import csv
import datetime
import io
import os
import threading
import time
from pathlib import Path

from core import bus

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "logs" / "latency.csv"
MAX_BYTES = 1024 * 1024                     # rotate at 1 MB

# One CSV row per turn. Blank numeric fields mean "stage did not happen".
FIELDS = ("turn", "at", "total_ms", "first_word_ms", "stt_ms", "token_ms",
          "say_ms", "tts_ms", "play_ms", "finish_ms", "tools", "tool_ms",
          "tool_max_ms")

# Stage keys shared with scripts/latency_report.py (in pipeline order).
STAGES = ("stt", "token", "say", "tts", "play", "finish")

_SINGLE = ("first_say", "tts_ready", "first_play", "reply_finished",
           "stt_done", "first_token")


def _seconds(value):
    """CSV milliseconds string -> seconds float; blank -> 0.0."""
    try:
        return max(0.0, float(value)) / 1000.0 if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


class Tracer:
    """Collect timestamps for one turn at a time.

    ``clock`` is injectable so tests can drive the whole pipeline with a fake
    clock. ``write`` controls the CSV append and ``publish`` controls the
    compatibility event on the ``latency`` bus topic.
    """

    def __init__(self, path=None, clock=None, max_bytes=MAX_BYTES,
                 write=True, publish=True):
        raw = Path(path or DEFAULT_PATH)
        self.path = raw if raw.is_absolute() else ROOT / raw
        self.clock = clock or time.time
        self.max_bytes = max(1, int(max_bytes))
        self.write = bool(write)
        self.publish = bool(publish)
        self._lock = threading.RLock()
        self._file_lock = threading.Lock()
        self._turn = 0
        self._active = False
        self._marks = {}
        self._pending = []                  # (tool name, started_at)
        self._spans = []                    # (tool name, seconds)

    # ------------------------------------------------------------- lifecycle
    def now(self):
        return self.clock()

    @property
    def active(self):
        with self._lock:
            return self._active

    def begin_turn(self, speech_end=None):
        """Start a turn. ``speech_end`` defaults to the current clock value."""
        with self._lock:
            self._turn += 1
            self._active = True
            self._marks = {"speech_end": float(
                self.clock() if speech_end is None else speech_end)}
            self._pending = []
            self._spans = []
            return self._turn

    def reset(self):
        """Forget the current turn without writing a row."""
        with self._lock:
            self._active = False
            self._marks = {}
            self._pending = []
            self._spans = []

    def mark(self, name, value=None):
        """Record a boundary once per turn. Returns True when it was stored."""
        with self._lock:
            if not self._active or name in self._marks:
                return False
            if name in ("tts_ready", "first_play") and "first_say" not in self._marks:
                return False                    # stale audio from an older turn
            self._marks[name] = float(self.clock() if value is None else value)
            return True

    def tool_start(self, name, at=None):
        with self._lock:
            if not self._active:
                return False
            self._pending.append((str(name), float(self.clock() if at is None else at)))
            return True

    def tool_end(self, name, at=None):
        with self._lock:
            if not self._active or not self._pending:
                return False
            stamp = float(self.clock() if at is None else at)
            for index in range(len(self._pending) - 1, -1, -1):
                if self._pending[index][0] == str(name):
                    start = self._pending.pop(index)[1]
                    self._spans.append((str(name), max(0.0, stamp - start)))
                    return True
            return False

    # --------------------------------------------------------------- results
    def first_word(self):
        """Seconds from the end of user speech to the first spoken say-line."""
        with self._lock:
            start, said = self._marks.get("speech_end"), self._marks.get("first_say")
            return max(0.0, said - start) if start is not None and said is not None else 0.0

    def _row_locked(self):
        marks = self._marks

        def delta(start, end):
            a, b = marks.get(start), marks.get(end)
            if a is None or b is None:
                return ""
            return "{0:.1f}".format(max(0.0, b - a) * 1000.0)

        spans = [seconds for _, seconds in self._spans]
        speech = marks.get("speech_end")
        at = ""
        if speech is not None:
            try:
                at = datetime.datetime.fromtimestamp(speech).strftime(
                    "%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, OverflowError):
                at = ""
        return {
            "turn": self._turn,
            "at": at,
            "total_ms": delta("speech_end", "reply_finished"),
            "first_word_ms": delta("speech_end", "first_say"),
            "stt_ms": delta("speech_end", "stt_done"),
            "token_ms": delta("stt_done", "first_token"),
            "say_ms": delta("first_token", "first_say"),
            "tts_ms": delta("first_say", "tts_ready"),
            "play_ms": delta("tts_ready", "first_play"),
            "finish_ms": delta("first_play", "reply_finished"),
            "tools": len(spans),
            "tool_ms": "{0:.1f}".format(sum(spans)) if spans else "",
            "tool_max_ms": "{0:.1f}".format(max(spans)) if spans else "",
        }

    def row(self):
        with self._lock:
            return self._row_locked()

    def publish_live(self):
        """The live 'first word' event, now with a dict the UI also understands."""
        if not self.publish:
            return None
        payload = {"turn": self._turn, "stage": "first_say",
                   "first_word": round(self.first_word(), 3)}
        bus.publish("latency", payload)
        return payload

    def finish(self):
        """Close the turn, append its CSV row and publish the full trace."""
        with self._lock:
            if not self._active:
                return None
            row = self._row_locked()
            self._active = False
        if self.write:
            self._append(row)
        if self.publish:
            payload = dict(row)
            payload["first_word"] = round(_seconds(row["first_word_ms"]), 3)
            payload["total"] = round(_seconds(row["total_ms"]), 3)
            payload["stages_ms"] = {name: row[name + "_ms"] for name in STAGES}
            bus.publish("latency", payload)
        return row

    def discard(self):
        with self._lock:
            self._active = False

    # ---------------------------------------------------------------- storage
    def _append(self, row):
        """Append one row, rotating to latency.1.csv when the log is full."""
        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow([row.get(field, "") for field in FIELDS])
        line = out.getvalue()

        try:
            with self._file_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                need_header = not self.path.exists() or self.path.stat().st_size == 0
                if (self.path.exists() and not need_header
                        and self.path.stat().st_size + len(line.encode("utf-8"))
                        > self.max_bytes):
                    try:
                        os.replace(str(self.path), str(self.path.with_suffix(".1.csv")))
                        need_header = True
                    except OSError:
                        pass                 # keep appending if rotation is blocked
                with self.path.open("a", encoding="utf-8", newline="") as handle:
                    if need_header:
                        writer = csv.writer(handle, lineterminator="\n")
                        writer.writerow(FIELDS)
                    handle.write(line)
            return True
        except OSError:
            return False                     # logging must never break a reply


def _initial():
    """Build the shared tracer from config.yaml, tolerating a broken config."""
    settings = {}
    try:
        from core.config import CFG
        settings = CFG.get("latency") or {}
    except Exception:  # noqa: BLE001 - config problems must not stop Iris
        pass
    return Tracer(path=settings.get("log") or DEFAULT_PATH,
                  max_bytes=settings.get("max_bytes", MAX_BYTES),
                  write=bool(settings.get("enabled", True)))


TRACE = _initial()


def configure(**kwargs):
    """Replace the shared tracer (used by tests and alternate data dirs)."""
    global TRACE
    old = TRACE
    TRACE = Tracer(path=kwargs.get("path", old.path),
                   clock=kwargs.get("clock", old.clock),
                   max_bytes=kwargs.get("max_bytes", old.max_bytes),
                   write=kwargs.get("write", old.write),
                   publish=kwargs.get("publish", old.publish))
    return TRACE


def now():                       return TRACE.now()
def begin_turn(speech_end=None): return TRACE.begin_turn(speech_end)
def mark(name, value=None):      return TRACE.mark(name, value)
def tool_start(name):            return TRACE.tool_start(name)
def tool_end(name):              return TRACE.tool_end(name)
def first_word():                return TRACE.first_word()
def publish_live():              return TRACE.publish_live()
def finish_turn():               return TRACE.finish()
def discard_turn():              return TRACE.discard()
