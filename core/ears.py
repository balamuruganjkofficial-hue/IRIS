"""The microphone: wake word, voice activity detection and bar signals.

One stream stays open all the time.
Modes:  idle   = wait for the wake word
        listen = capture one sentence
        busy   = Iris is thinking or speaking (wake word = interrupt)

Barge-in has three independent paths, so any one of them can cut her off:
Esc (the interface), the wake phrase, and a loudness detector that learns how
loud her own voice is in the microphone and reacts when the user is clearly
louder for a moment. The loudness path also keeps the last fraction of a second
of audio so the first word the user said is not lost when listening resumes.
"""

import collections
import math
import queue
import statistics
import threading
import time

import numpy as np
import pyaudio

from core import bus
from core.config import CFG
from core import wake

RATE = 16000
CHUNK = 1280                  # 80 ms per frame
FRAME_S = CHUNK / RATE


# --------------------------------------------------------------- barge-in
BARGE_MODES = ("voice", "wake", "off")
_MIN_SAMPLES = 4              # ~0.32 s of audio before the echo baseline is trusted
_QUIET_START_S = 0.3          # start learning anyway if no echo reaches the mic


def barge_mode(voice_cfg=None):
    """Read ``voice.barge_in_mode``, falling back to the legacy barge_in flag."""
    cfg = voice_cfg or {}
    raw = str(cfg.get("barge_in_mode", "") or "").strip().lower()
    if raw in BARGE_MODES:
        return raw
    return "voice" if bool(cfg.get("barge_in", True)) else "off"


class KeepBuffer:
    """The last few tenths of a second of microphone audio, kept for barge-in."""

    def __init__(self, seconds=0.6, frame_s=FRAME_S):
        self.frame_s = max(0.001, float(frame_s))
        seconds = max(0.0, float(seconds))
        self.maxlen = max(1, int(math.ceil(seconds / self.frame_s)))
        self._frames = collections.deque(maxlen=self.maxlen)

    def push(self, frame):
        if frame:
            self._frames.append(frame)

    def take(self):
        """Return the kept frames and start a fresh buffer."""
        frames = list(self._frames)
        self._frames.clear()
        return frames

    def __len__(self):
        return len(self._frames)


class BargeIn:
    """Loudness barge-in: learn her echo, then react to a clearly louder voice.

    Feed one frame's RMS per microphone chunk while audio is playing. The first
    ``learn_s`` of each playback burst becomes the echo baseline; the trigger
    needs ``hold_s`` of audio above ``max(min_rms, baseline * ratio)``. The
    state machine is pure and takes ``now`` from the caller, so tests can replay
    synthetic frames without a microphone or a speaker.
    """

    def __init__(self, ratio=2.2, min_rms=500.0, learn_s=1.0, hold_s=0.3,
                 enabled=True, debug=False):
        self.ratio = max(1.0, float(ratio))
        self.min_rms = max(1.0, float(min_rms))
        self.learn_s = max(0.0, float(learn_s))
        self.hold_s = max(0.0, float(hold_s))
        self.enabled = bool(enabled)
        self.debug = bool(debug)
        self.playing = False
        self.baseline = 0.0
        self.threshold = 0.0
        self.rms = 0.0
        self.triggered = False
        self._burst_at = 0.0
        self._learn_at = None
        self._samples = []
        self._loud_since = None
        self._announced = False

    def set_playing(self, playing, now=0.0):
        """Start a fresh echo baseline when a playback burst begins."""
        playing = bool(playing)
        if playing == self.playing:
            return
        self.playing = playing
        if playing:
            self._burst_at = float(now or 0.0)
            self._learn_at = None
            self._samples = []
            self._loud_since = None
            self.baseline = 0.0
            self.threshold = 0.0
            self.triggered = False
            self._announced = False
        else:
            self._loud_since = None

    def _floor(self):
        """Ignore near-silence while waiting for her own voice to appear."""
        return max(60.0, self.min_rms * 0.2)

    def _learn(self, rms):
        self._samples.append(rms)
        if len(self._samples) >= _MIN_SAMPLES:
            self.baseline = float(statistics.median(self._samples))
            self.threshold = max(self.min_rms, self.baseline * self.ratio)

    def push(self, rms, now):
        """Return True exactly once, when the user has clearly talked over her."""
        if not self.enabled or not self.playing or self.triggered:
            return False
        try:
            rms = max(0.0, float(rms))
            now = float(now)
        except (TypeError, ValueError):
            return False
        self.rms = rms
        if self._learn_at is None:
            if rms >= self._floor():
                self._learn_at = now
            elif now - self._burst_at >= _QUIET_START_S:
                self._learn_at = now      # headphones, or a very quiet echo
            else:
                return False
        if now - self._learn_at <= self.learn_s:
            self._learn(rms)
        elif not self.threshold:
            self.baseline = (float(statistics.median(self._samples))
                             if self._samples else 0.0)
            self.threshold = max(self.min_rms, self.baseline * self.ratio)
        if not self.threshold:
            return False
        if self.debug and not self._announced:
            self._announced = True
            print("[ears] barge-in baseline={0:.0f} threshold={1:.0f}".format(
                self.baseline, self.threshold))
        if rms >= self.threshold:
            if self._loud_since is None:
                self._loud_since = now
            if now - self._loud_since >= self.hold_s:
                self.triggered = True
                self._loud_since = None
                return True
        else:
            self._loud_since = None
        return False

    def debug_line(self):
        return "baseline={0:.0f} threshold={1:.0f} rms={2:.0f}".format(
            self.baseline, self.threshold, self.rms)


class Ears:
    def __init__(self):
        v = CFG["voice"]
        self.wake_thr = float(v.get("wake_threshold", 0.5))
        self.barge_thr = float(v.get("barge_in_threshold", 0.7))
        self.barge_mode = barge_mode(v)
        self.barge = BargeIn(
            ratio=float(v.get("barge_in_ratio", 2.2)),
            min_rms=float(v.get("barge_in_min_rms", 500.0)),
            learn_s=float(v.get("barge_in_learn", 1.0)),
            hold_s=float(v.get("barge_in_hold", 0.3)),
            enabled=self.barge_mode == "voice",
            debug=bool(v.get("barge_in_debug", False)),
        )
        self._keep = KeepBuffer(float(v.get("barge_in_keep", 0.6)))
        self._seed = None
        self._seed_voiced = 0
        self._playing = False
        bus.subscribe("state", self._on_state)
        self.min_rms = float(v.get("vad_min_rms", 250))
        self.end_silence = float(v.get("end_silence", 0.9))
        self.debug = bool(v.get("wake_debug", False))
        self.events = queue.Queue()
        self.on_interrupt = None
        self.noise = 100.0
        self._mode, self._deadline, self._gen = "busy", 0.0, 0
        self._lock = threading.Lock()
        self._level = 0.0
        self._stream = None
        self._pa = None
        self.mic_ok = False
        threading.Thread(target=self._supervisor, daemon=True, name="ears").start()

    def set_mode(self, mode, timeout=7.0):
        with self._lock:
            self._mode = mode
            self._deadline = time.time() + timeout
            self._gen += 1
        if mode == "listen":
            bus.publish("state", "listening")
        elif mode == "idle":
            bus.publish("state", "idle")

    def mode(self):
        with self._lock:
            return self._mode

    def _on_state(self, payload):
        """Remember whether audio is actually playing, for loudness barge-in."""
        self._playing = str(payload or "") == "speaking"

    def _take_seed(self):
        """Audio kept from before a barge-in, for the next listen capture."""
        seed, self._seed = self._seed, None
        return seed or []

    def _seed_preroll(self, preroll):
        """Refill the pre-roll with audio kept from before a barge-in.

        The captured frames then start with the words the user had already
        said, so the first syllable is not lost when listening resumes.
        """
        preroll.clear()
        seed = self._take_seed()
        if not seed:                            # nothing kept: no stale credit
            self._seed_voiced = 0
        preroll.extend(seed)

    def flush(self):
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        self.mic_ok = False
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:
            pass
        self._stream = self._pa = None

    def _finish(self, kind, payload):
        with self._lock:
            self._mode = "idle" if kind == "timeout" else "busy"
            self._gen += 1
        self.events.put((kind, payload))

    def _publish_level(self, rms):
        if not bus.has_subscribers("level"):
            return
        raw = min(1.0, rms / 2200.0)
        self._level = 0.55 * self._level + 0.45 * raw
        bus.publish("level", self._level)

    def _supervisor(self):
        fails = 0
        while True:
            try:
                self._run()
            except Exception as e:            # e.g. after sleep/resume or mic unplugged
                fails += 1
                print("[ears] restarting after error:", e)
                bus.publish("toast", "Microphone hiccup - reconnecting.")
                time.sleep(min(10, 1 + fails))

    def _run(self):
        model = None
        try:
            model = wake._load()
        except Exception as e:
            print("[ears] wake word unavailable:", e)
            bus.publish("toast", "Wake word model unavailable.")
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(format=pyaudio.paInt16, channels=1, rate=RATE,
                                     input=True, frames_per_buffer=CHUNK)
        self.mic_ok = True
        stream = self._stream
        preroll = collections.deque(maxlen=8)          # ~0.64 s kept before speech starts
        frames, started, voiced, last_voice, seen_gen = [], False, 0, 0.0, -1
        cooldown, dbg_t, dbg_rms, dbg_peak = 0.0, time.time(), 0.0, 0.0
        self._seed, self._seed_voiced = None, 0
        try:
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                now = time.time()
                frame = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                with self._lock:
                    mode, deadline, gen = self._mode, self._deadline, self._gen

                # Keep recent audio and learn her own echo from the current burst.
                self._keep.push(data)
                self.barge.set_playing(self._playing, now)

                if gen != seen_gen:                     # mode changed: reset capture state
                    seen_gen = gen
                    frames, started, voiced = [], False, 0
                    self._seed_preroll(preroll)         # barge-in audio survives the reset
                    cooldown = max(cooldown, now + 0.3)
                    if model is not None:
                        try:
                            model.reset()
                        except Exception:
                            pass

                self._publish_level(rms)
                dbg_rms = max(dbg_rms, rms)
                if self.debug and now - dbg_t > 2:
                    extra = ""
                    if self.barge.debug and self.barge.playing:
                        extra = " barge={0:.0f}/{1:.0f}".format(
                            self.barge.baseline, self.barge.threshold)
                    print("[ears] mode={0} rms={1:.0f} noise={2:.0f} wake={3:.2f}{4}".format(
                        mode, dbg_rms, self.noise, dbg_peak, extra))
                    dbg_t, dbg_rms, dbg_peak = now, 0.0, 0.0

                if mode == "listen":
                    loud = rms > max(self.noise * 3.0, self.min_rms)
                    if not started:
                        preroll.append(data)
                        if loud:
                            started, frames = True, list(preroll)
                            voiced, last_voice = max(1, self._seed_voiced), now
                            self._seed_voiced = 0
                        else:
                            self.noise = 0.97 * self.noise + 0.03 * rms
                            if now > deadline:
                                self._finish("timeout", None)
                    else:
                        frames.append(data)
                        if loud:
                            voiced += 1
                            last_voice = now
                        if now - last_voice > self.end_silence or len(frames) * FRAME_S > 20:
                            if voiced >= 4:             # at least about 0.3 s of real speech
                                self._finish("utterance", b"".join(frames))
                            else:                       # a click or cough: keep waiting
                                frames, started, voiced = [], False, 0
                    continue

                # Loudness barge-in: only while she is actually audible, never
                # while a capture is already running (that would drop audio).
                if mode != "listen" and self.barge.enabled and \
                        self.barge.push(rms, now):
                    self._seed = self._keep.take()
                    self._seed_voiced = max(
                        4, int(round(self.barge.hold_s / FRAME_S)))
                    if self.barge.debug:
                        print("[ears] barge-in " + self.barge.debug_line())
                    bus.publish("toast", "Go ahead.")
                    if self.on_interrupt:
                        self.on_interrupt()
                    self.set_mode("listen", 7.0)
                    continue

                if mode == "idle" and rms < max(self.noise * 2.0, self.min_rms):
                    self.noise = 0.98 * self.noise + 0.02 * rms
                if model is None or now < cooldown:
                    continue
                scores = model.predict(frame)
                s = max(scores.values()) if scores else 0.0
                dbg_peak = max(dbg_peak, s)
                if mode == "idle" and s >= self.wake_thr:
                    cooldown = now + 1.0
                    self._finish("wake", None)
                elif mode == "busy" and self.barge_mode != "off" and s >= self.barge_thr:
                    cooldown = now + 1.0
                    if self.on_interrupt:
                        self.on_interrupt()
                    self.set_mode("listen", 7.0)
        finally:
            self.mic_ok = False
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._stream = self._pa = None


_EARS = None


def __getattr__(name):
    """Create the microphone singleton on first use, so importing this is free.

    ``from core.ears import ears`` still works exactly as before; tests can
    import :class:`BargeIn` and :class:`KeepBuffer` without opening the mic.
    """
    global _EARS
    if name == "ears":
        if _EARS is None:
            _EARS = Ears()
        return _EARS
    raise AttributeError("module 'core.ears' has no attribute {0!r}".format(name))
