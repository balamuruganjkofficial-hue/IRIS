import collections
import queue
import threading
import time

import numpy as np
import pyaudio

from core import wake
from core.config import CFG

RATE = 16000
CHUNK = 1280                  # 80 ms per frame
FRAME_S = CHUNK / RATE


class Ears:
    """Owns the microphone. One stream stays open all the time.
    Modes:  idle   = wait for the wake word
            listen = capture one sentence
            busy   = Iris is thinking or speaking (wake word = interrupt)"""

    def __init__(self):
        v = CFG["voice"]
        self.wake_thr = float(v.get("wake_threshold", 0.5))
        self.barge_thr = float(v.get("barge_in_threshold", 0.7))
        self.barge_in = bool(v.get("barge_in", True))
        self.min_rms = float(v.get("vad_min_rms", 250))
        self.end_silence = float(v.get("end_silence", 0.9))
        self.debug = bool(v.get("wake_debug", False))
        self.events = queue.Queue()
        self.on_interrupt = None
        self.noise = 100.0
        self._mode, self._deadline, self._gen = "busy", 0.0, 0
        self._lock = threading.Lock()
        threading.Thread(target=self._supervisor, daemon=True).start()

    def set_mode(self, mode, timeout=7.0):
        with self._lock:
            self._mode = mode
            self._deadline = time.time() + timeout
            self._gen += 1

    def flush(self):
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass

    def _finish(self, kind, payload):
        with self._lock:
            self._mode = "idle" if kind == "timeout" else "busy"
            self._gen += 1
        self.events.put((kind, payload))

    def _supervisor(self):
        fails = 0
        while True:
            try:
                self._run()
            except Exception as e:            # e.g. after sleep/resume or mic unplugged
                fails += 1
                print("[ears] restarting after error:", e)
                time.sleep(min(10, 1 + fails))

    def _run(self):
        model = None
        try:
            model = wake._load()
        except Exception as e:
            print("[ears] wake word unavailable:", e)
        pa = pyaudio.PyAudio()
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE,
                         input=True, frames_per_buffer=CHUNK)
        preroll = collections.deque(maxlen=8)          # ~0.64 s kept before speech starts
        frames, started, voiced, last_voice, seen_gen = [], False, 0, 0.0, -1
        cooldown, dbg_t, dbg_rms, dbg_peak = 0.0, time.time(), 0.0, 0.0
        try:
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                now = time.time()
                frame = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
                with self._lock:
                    mode, deadline, gen = self._mode, self._deadline, self._gen
                if gen != seen_gen:                     # mode changed: reset capture state
                    seen_gen = gen
                    frames, started, voiced = [], False, 0
                    preroll.clear()
                    cooldown = max(cooldown, now + 0.3)
                    if model is not None:
                        try:
                            model.reset()
                        except Exception:
                            pass

                dbg_rms = max(dbg_rms, rms)
                if self.debug and now - dbg_t > 2:
                    print(f"[ears] mode={mode} rms={dbg_rms:.0f} noise={self.noise:.0f} "
                          f"wake={dbg_peak:.2f}")
                    dbg_t, dbg_rms, dbg_peak = now, 0.0, 0.0

                if mode == "listen":
                    loud = rms > max(self.noise * 3.0, self.min_rms)
                    if not started:
                        preroll.append(data)
                        if loud:
                            started, frames, voiced, last_voice = True, list(preroll), 1, now
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
                elif mode == "busy" and self.barge_in and s >= self.barge_thr:
                    cooldown = now + 1.0
                    if self.on_interrupt:
                        self.on_interrupt()
                    self.set_mode("listen", 7.0)
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()


ears = Ears()
