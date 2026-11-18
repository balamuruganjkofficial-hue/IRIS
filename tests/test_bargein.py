"""Loudness barge-in: echo baseline, trigger timing, kept audio and modes."""

import collections
import random
import unittest

from core import ears

FRAME = 0.08          # seconds per microphone chunk


def _echo(det, start, seconds=1.2, level=1200.0, jitter=0.15, seed=7):
    """Feed her own voice through the mic; return (now, trigger times)."""
    rng = random.Random(seed)
    now, fired = float(start), []
    for _ in range(int(round(seconds / FRAME))):
        now += FRAME
        if det.push(level * rng.uniform(1.0 - jitter, 1.0 + jitter), now):
            fired.append(now)
    return now, fired


class TestEchoBaseline(unittest.TestCase):
    def _detector(self, **kwargs):
        opts = dict(ratio=2.0, min_rms=500.0, learn_s=1.0, hold_s=0.3)
        opts.update(kwargs)
        det = ears.BargeIn(**opts)
        det.set_playing(True, 0.0)
        return det

    def test_her_own_echo_never_triggers(self):
        det = self._detector()
        _now, fired = _echo(det, 0.0, seconds=3.0)
        self.assertEqual(fired, [])
        self.assertGreaterEqual(det.baseline, 900.0)
        self.assertGreater(det.threshold, det.baseline)

    def test_a_single_loud_spike_does_not_trigger(self):
        det = self._detector()
        now, _ = _echo(det, 0.0, seconds=1.4)
        self.assertFalse(det.push(4000.0, now + FRAME))
        self.assertFalse(det.push(1200.0, now + 2 * FRAME))

    def test_echo_that_dips_and_returns_does_not_trigger(self):
        det = self._detector()
        now, _ = _echo(det, 0.0, seconds=1.4)
        for _ in range(3):
            now += FRAME
            self.assertFalse(det.push(200.0, now))
        for _ in range(6):
            now += FRAME
            self.assertFalse(det.push(1300.0, now))

    def test_a_new_burst_learns_a_new_baseline(self):
        det = self._detector()
        now, _ = _echo(det, 0.0, seconds=1.2, level=1200.0)
        first = det.baseline
        det.set_playing(False, now)
        det.set_playing(True, now + 0.5)
        _now, _fired = _echo(det, now + 0.5, seconds=1.2, level=3000.0)
        self.assertGreater(det.baseline, first)


class TestTrigger(unittest.TestCase):
    def test_user_talking_over_her_triggers_within_400_ms(self):
        det = ears.BargeIn(ratio=2.0, min_rms=500.0, learn_s=1.0, hold_s=0.3)
        det.set_playing(True, 0.0)
        now, fired = _echo(det, 0.0, seconds=1.2)
        self.assertEqual(fired, [])

        started = None
        for _ in range(12):
            now += FRAME
            if started is None:
                started = now
            if det.push(3500.0, now):
                break
        else:
            self.fail("talking over her did not trigger")

        self.assertLessEqual(now - started, 0.4)
        self.assertGreaterEqual(now - started, 0.24)    # it waits for the hold time
        self.assertTrue(det.triggered)

    def test_it_does_not_fire_twice_in_one_burst(self):
        det = ears.BargeIn(ratio=2.0, min_rms=500.0, learn_s=1.0, hold_s=0.3)
        det.set_playing(True, 0.0)
        now, _ = _echo(det, 0.0, seconds=1.2)
        for _ in range(8):
            now += FRAME
            if det.push(3500.0, now):
                break
        else:
            self.fail("no first trigger")
        self.assertFalse(det.push(3500.0, now + FRAME))
        self.assertFalse(det.push(3500.0, now + 2 * FRAME))

    def test_quiet_room_uses_the_absolute_floor(self):
        det = ears.BargeIn(ratio=2.0, min_rms=500.0, learn_s=0.5, hold_s=0.3)
        det.set_playing(True, 0.0)
        now = 0.0
        for _ in range(10):                 # headphones, or a muted speaker
            now += FRAME
            det.push(40.0, now)
        self.assertEqual(det.threshold, 500.0)
        fired = False
        for _ in range(6):
            now += FRAME
            fired = det.push(700.0, now) or fired
        self.assertTrue(fired)


class TestModes(unittest.TestCase):
    def test_off_disables_the_detector(self):
        det = ears.BargeIn(enabled=False)
        det.set_playing(True, 0.0)
        _now, fired = _echo(det, 0.0, seconds=1.2)
        self.assertEqual(fired, [])

    def test_nothing_fires_when_she_is_not_playing(self):
        det = ears.BargeIn()
        det.set_playing(False, 0.0)
        _now, fired = _echo(det, 0.0, seconds=1.2)
        self.assertEqual(fired, [])

    def test_mode_defaults_and_the_legacy_flag(self):
        self.assertEqual(ears.barge_mode({}), "voice")
        self.assertEqual(ears.barge_mode({"barge_in": False}), "off")
        self.assertEqual(ears.barge_mode({"barge_in_mode": "wake"}), "wake")
        self.assertEqual(ears.barge_mode({"barge_in_mode": "off"}), "off")
        self.assertEqual(ears.barge_mode({"barge_in_mode": "nonsense"}), "voice")


class TestKeptAudio(unittest.TestCase):
    def test_keeps_the_last_six_tenths_of_a_second(self):
        buf = ears.KeepBuffer(0.6, frame_s=0.08)
        for i in range(12):
            buf.push(bytes([i]))
        kept = buf.take()
        self.assertEqual(len(kept), buf.maxlen)
        self.assertGreaterEqual(len(kept) * 0.08, 0.6)
        self.assertEqual(kept[-1], bytes([11]))
        self.assertEqual(kept[0], bytes([12 - len(kept)]))
        self.assertEqual(buf.take(), [])

    def test_kept_audio_seeds_the_next_capture(self):
        buf = ears.KeepBuffer(0.6, frame_s=0.08)
        for i in range(10):
            buf.push(bytes([i]))
        e = ears.Ears.__new__(ears.Ears)        # no mic, no thread
        e._seed, e._seed_voiced = buf.take(), 4
        preroll = collections.deque(maxlen=8)
        preroll.append(b"stale")                # left over from the old mode
        e._seed_preroll(preroll)                # what _run does on a mode change
        preroll.append(b"next")
        utterance = list(preroll)
        self.assertEqual(utterance[-1], b"next")
        self.assertNotIn(b"stale", utterance)
        self.assertIn(bytes([9]), utterance)    # the start of what was said survives
        self.assertNotIn(bytes([0]), utterance)  # only the kept tail, not the whole burst


class TestSeedWiring(unittest.TestCase):
    def test_take_seed_returns_the_kept_audio_once(self):
        e = ears.Ears.__new__(ears.Ears)        # no mic, no thread
        e._seed = [b"a", b"b"]
        self.assertEqual(e._take_seed(), [b"a", b"b"])
        self.assertEqual(e._take_seed(), [])

    def test_a_later_mode_change_drops_stale_speech_credit(self):
        e = ears.Ears.__new__(ears.Ears)
        e._seed, e._seed_voiced = [b"a"], 4
        e._seed_preroll(collections.deque(maxlen=8))
        self.assertEqual(e._seed_voiced, 4)     # kept audio still counts
        e._seed_preroll(collections.deque(maxlen=8))
        self.assertEqual(e._seed_voiced, 0)     # second reset must not inherit it


if __name__ == "__main__":
    unittest.main()
