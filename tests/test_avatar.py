"""Pure motion helpers for the human avatar, tested without a display."""

import math
import unittest

from interfaces.avatar import (blink_amount, brow_lift, eye_open, gaze_offset,
                               head_offset, mix, mouth_amount)


class TestAvatarMotion(unittest.TestCase):
    def test_blink_closes_and_opens(self):
        self.assertAlmostEqual(blink_amount(0.0), 0.0)
        self.assertAlmostEqual(blink_amount(0.13), 1.0, places=2)
        self.assertAlmostEqual(blink_amount(0.40), 0.0, places=2)
        for step in range(200):
            value = blink_amount(step * 0.05)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_mouth_only_moves_when_speaking(self):
        for state in ("idle", "listening", "thinking"):
            self.assertEqual(mouth_amount(state, 1.0), 0.0)
        values = [mouth_amount("speaking", i * 0.07) for i in range(200)]
        self.assertGreater(max(values), 0.5)
        self.assertGreaterEqual(min(values), 0.0)
        self.assertLessEqual(max(values), 1.0)

    def test_head_drift_is_small(self):
        for state in ("idle", "listening", "thinking", "speaking"):
            for step in range(100):
                dx, dy = head_offset(step * 0.1, state)
                self.assertLessEqual(abs(dx), 3.0)
                self.assertLessEqual(abs(dy), 2.5)

    def test_gaze_stays_inside_the_eye(self):
        for state in ("idle", "listening", "thinking", "speaking"):
            for step in range(100):
                gx, gy = gaze_offset(state, step * 0.1)
                self.assertGreaterEqual(gx, -1.0)
                self.assertLessEqual(gx, 1.0)
                self.assertGreaterEqual(gy, -1.0)
                self.assertLessEqual(gy, 1.0)

    def test_state_expressions(self):
        self.assertGreater(brow_lift("listening"), brow_lift("idle"))
        self.assertLess(brow_lift("thinking"), brow_lift("idle"))
        self.assertGreater(eye_open("listening"), eye_open("speaking"))
        self.assertEqual(brow_lift("unknown"), 0.0)
        self.assertEqual(eye_open("unknown"), 1.0)

    def test_mix_clamps(self):
        self.assertEqual(mix("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(mix("#000000", "#ffffff", 1.0), "#ffffff")
        self.assertEqual(mix("#000000", "#ffffff", 2.0), "#ffffff")
        self.assertEqual(mix("#000000", "#ffffff", -1.0), "#000000")
        self.assertTrue(math.isfinite(sum(
            int(mix("#102030", "#405060", 0.5)[i:i + 2], 16)
            for i in (1, 3, 5))))


if __name__ == "__main__":
    unittest.main()
