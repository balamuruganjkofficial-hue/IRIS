"""The unprompted decision loop: rate limits, parsing and safety."""

import unittest

from core import initiative


CFG = {"autonomy": {"enabled": True, "idle_minutes": 20,
                    "cooldown_minutes": 40, "max_per_hour": 3,
                    "model_decides": True}}


class TestShouldConsider(unittest.TestCase):
    def test_disabled_never_fires(self):
        cfg = {"autonomy": {"enabled": False}}
        self.assertFalse(initiative.should_consider(
            10_000, 0, 0, config=cfg))

    def test_waits_for_real_silence(self):
        now = 10_000.0
        self.assertFalse(initiative.should_consider(
            now, now - 19 * 60, 0, config=CFG))
        self.assertTrue(initiative.should_consider(
            now, now - 21 * 60, 0, config=CFG))

    def test_cooldown_between_unprompted_lines(self):
        now = 10_000.0
        self.assertFalse(initiative.should_consider(
            now, 0, now - 39 * 60, config=CFG))
        self.assertTrue(initiative.should_consider(
            now, 0, now - 41 * 60, config=CFG))

    def test_hourly_cap(self):
        now = 10_000.0
        self.assertFalse(initiative.should_consider(
            now, 0, 0, [now - 5, now - 15, now - 25], config=CFG))
        self.assertTrue(initiative.should_consider(
            now, 0, 0, [now - 4000, now - 15, now - 25], config=CFG))

    def test_busy_blocks_her(self):
        self.assertFalse(initiative.should_consider(
            10_000, 0, 0, busy=True, config=CFG))

    def test_bad_numbers_do_not_raise(self):
        self.assertFalse(initiative.should_consider(
            "soon", "yesterday", None, config=CFG))


class TestParseDecision(unittest.TestCase):
    def test_json_with_say_and_tool(self):
        got = initiative.parse_decision(
            '{"say": "Pulling up your notes.", "do": '
            '{"tool": "open_app", "args": {"name": "notepad"}}}')
        self.assertEqual(got["say"], "Pulling up your notes.")
        self.assertEqual(got["tool"], "open_app")
        self.assertEqual(got["args"], {"name": "notepad"})

    def test_code_fence_and_say_only(self):
        got = initiative.parse_decision(
            '```json\n{"say": "You have been quiet.", "do": null}\n```')
        self.assertEqual(got["say"], "You have been quiet.")
        self.assertIsNone(got["tool"])

    def test_empty_or_nothing_means_stay_quiet(self):
        self.assertIsNone(initiative.parse_decision(""))
        self.assertIsNone(initiative.parse_decision(
            '{"say": "", "do": null}'))

    def test_plain_sentence_is_a_fallback_line(self):
        got = initiative.parse_decision("Still here if you need me.")
        self.assertEqual(got["say"], "Still here if you need me.")
        self.assertIsNone(got["tool"])

    def test_unparseable_json_is_ignored(self):
        self.assertIsNone(initiative.parse_decision("{not json at all"))


class TestSafeTools(unittest.TestCase):
    def test_ordinary_tools_are_allowed(self):
        for name in ("browser_search", "open_app", "web_search",
                     "show_dashboard", "list_windows"):
            self.assertTrue(initiative.safe_tool(name), name)

    def test_risky_tools_are_not(self):
        for name in ("delete_file", "run_command", "close_app",
                     "browser_eval", "browser_upload", "forget", ""):
            self.assertFalse(initiative.safe_tool(name), name)


class TestInitiative(unittest.TestCase):
    def _clock(self, start=10_000.0):
        now = [start]
        return now, lambda: now[0]

    def test_respects_idle_and_cooldown(self):
        now, clock = self._clock()
        calls = []

        def decide(context="", facts=None, now=None, config=None):
            calls.append(now)
            return {"say": "Still here.", "tool": None, "args": {}}

        auto = initiative.Initiative(CFG, now=clock, decide_fn=decide)
        self.assertIsNone(auto.maybe())
        now[0] += 21 * 60
        got = auto.maybe()
        self.assertEqual(got["say"], "Still here.")
        self.assertIsNone(auto.maybe())          # cooldown
        self.assertEqual(len(calls), 1)

    def test_fallback_keeps_her_human_when_the_model_is_down(self):
        now, clock = self._clock()
        auto = initiative.Initiative(CFG, now=clock,
                                     decide_fn=lambda *a, **k: None)
        now[0] += 21 * 60
        got = auto.maybe(fallback=lambda: "Still here, boss.")
        self.assertEqual(got["say"], "Still here, boss.")

    def test_activity_resets_the_idle_clock(self):
        now, clock = self._clock()
        auto = initiative.Initiative(CFG, now=clock,
                                     decide_fn=lambda *a, **k: None)
        now[0] += 21 * 60
        auto.note_activity()
        self.assertIsNone(auto.maybe())


if __name__ == "__main__":
    unittest.main()
