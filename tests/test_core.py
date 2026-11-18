"""Unit tests for the pure parts of Iris. Run with:  python -m unittest discover -s tests"""

import os
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import memory, net
from core.config import CFG, ROOT
from core.skill import (REGISTRY, run_tool, select_tools, skill,
                        tool_schemas)
from core.text import (answer, clean, is_junk, just_thanks, prosody, sentences,
                       speakable, style_bounds, wants_sleep)


class TestText(unittest.TestCase):
    def test_sentences(self):
        self.assertEqual(sentences("One. Two! Three?"), ["One.", "Two!", "Three?"])
        self.assertEqual(sentences(""), [])

    def test_clean(self):
        self.assertEqual(clean("**hello** `world`"), "hello world")

    def test_junk(self):
        self.assertTrue(is_junk(""))
        self.assertTrue(is_junk("um"))
        self.assertTrue(is_junk("Thanks for watching!"))
        self.assertFalse(is_junk("open chrome"))

    def test_sleep(self):
        words = {"goodbye", "bye", "thanks", "thank", "nevermind"}
        self.assertTrue(wants_sleep("thanks", words))
        self.assertTrue(wants_sleep("goodbye Iris", words))
        self.assertTrue(wants_sleep("that's all", words))
        self.assertFalse(wants_sleep("thanks for the reminder", words))
        self.assertFalse(wants_sleep("thanks, open chrome", words))

    def test_thanks(self):
        for phrase in ("thanks", "thank you", "cheers boss", "thank you so much",
                       "okay thanks", "ta"):
            self.assertTrue(just_thanks(phrase), phrase)
        for phrase in ("thanks, open chrome", "no thanks", "", "than", "thank you for",
                       "thank you for opening the file and closing it later"):
            self.assertFalse(just_thanks(phrase), phrase)

    def test_prosody(self):
        first = prosody("Hello there.", "+8%", "+0Hz", 4, 3)
        self.assertEqual(first, prosody("Hello there.", "+8%", "+0Hz", 4, 3))
        self.assertNotEqual(first, prosody("Different line.", "+8%", "+0Hz", 4, 3))
        for rate, pitch in (first, prosody("x", "+8%", "+0Hz", 4, 3)):
            self.assertGreaterEqual(int(rate.rstrip("%")), 5)
            self.assertLessEqual(int(rate.rstrip("%")), 13)
            self.assertGreaterEqual(int(pitch.rstrip("Hz")), -3)
            self.assertLessEqual(int(pitch.rstrip("Hz")), 3)
        self.assertEqual(prosody("x", "+8%", "+0Hz", 4, 3, enabled=False),
                         ("+8%", "+0Hz"))

    def test_prosody_follows_punctuation(self):
        question = prosody("Ready?", "+8%", "+0Hz", 0, 0)
        statement = prosody("This is a line.", "+8%", "+0Hz", 0, 0)
        long_line = prosody("word " * 30, "+8%", "+0Hz", 0, 0)
        short_line = prosody("Go.", "+8%", "+0Hz", 0, 0)
        self.assertEqual(question[1], "+3Hz")
        self.assertEqual(statement[1], "+0Hz")
        self.assertLess(int(long_line[0].rstrip("%")),
                        int(statement[0].rstrip("%")))
        self.assertGreater(int(short_line[0].rstrip("%")),
                           int(statement[0].rstrip("%")))

    def test_spoken_yes(self):
        for phrase in ("yes", "yeah", "yep", "sure", "go ahead", "do it",
                       "please proceed", "confirm", "granted"):
            self.assertTrue(answer(phrase), phrase)

    def test_spoken_no(self):
        for phrase in ("no", "nope", "nah", "cancel", "don't", "do not",
                       "no thanks", "never mind", "stop"):
            self.assertFalse(answer(phrase), phrase)

    def test_spoken_unclear(self):
        for phrase in ("maybe later", "what do you mean", "", "hmm"):
            self.assertIsNone(answer(phrase), phrase)


class TestSpeakable(unittest.TestCase):
    def test_numbers_percent_and_currency(self):
        self.assertEqual(speakable("I have 3 cats and 12 dogs."),
                         "I have three cats and twelve dogs.")
        self.assertEqual(speakable("It is 20% done and costs $1.50."),
                         "It is twenty percent done and costs one dollar and "
                         "fifty cents.")

    def test_times_are_said_not_spelled(self):
        self.assertEqual(speakable("Call at 9:30pm, or 17:45."),
                         "Call at nine thirty p m, or five forty-five p m.")
        self.assertEqual(speakable("The meeting is at 9:00."),
                         "The meeting is at nine o'clock.")

    def test_dates_and_units(self):
        self.assertEqual(speakable("The date is 2026-11-18."),
                         "The date is November eighteenth, twenty twenty-six.")
        self.assertEqual(speakable("It is 5km and 20 degrees."),
                         "It is five kilometres and twenty degrees.")

    def test_urls_paths_markdown_and_emoji_are_removed(self):
        self.assertEqual(speakable("Read https://example.com/foo now."),
                         "Read now.")
        self.assertEqual(speakable("Open C:\\Users\\me\\secret.txt please."),
                         "Open please.")
        self.assertEqual(speakable("**Hello** `world` :)"), "Hello world")

    def test_abbreviations_and_pronunciations(self):
        self.assertEqual(speakable("Use SQL and JSON, e.g. this."),
                         "Use sequel and jason, for example this.")
        self.assertEqual(speakable("Say SQL.", {"SQL": "S Q L"}), "Say S Q L.")

    def test_ellipsis_becomes_a_pause(self):
        self.assertEqual(speakable("First... then second."),
                         "First, then second.")

    def test_long_sentence_is_split_at_commas(self):
        long_line = ("This is a very long sentence, with several commas, that "
                     "keeps going on and on, because it was written by a model "
                     "that never learned to stop talking, and it really should "
                     "be split into pieces so the voice can breathe.")
        spoken = speakable(long_line)
        self.assertGreaterEqual(len(re.findall(r"[.!?]", spoken)), 2)
        for piece in re.split(r"(?<=[.!?])\s+", spoken):
            self.assertLessEqual(len(piece.split()), 26, piece)

    def test_empty_and_garbage_input_is_safe(self):
        self.assertEqual(speakable(""), "")
        self.assertEqual(speakable(None), "")
        self.assertIsInstance(speakable("..."), str)

    def test_prosody_strings_are_always_valid(self):
        rng = random.Random(7)
        words = ("hi", "there", "why", "now", "okay", "please", "explain",
                 "battery", "twenty", "Chrome", "logs", "again")
        endings = ("", ".", "?", "!", "...")
        rates = ("+0%", "+2%", "+5%", "-3%", "+8%", "abc", "")
        pitches = ("+0Hz", "-2Hz", "+3Hz", "+-1Hz", "garbage")
        for style in (None, "calm", "warm", "energetic", "nonsense"):
            for _ in range(400):
                text = (" ".join(rng.choice(words)
                                 for _ in range(rng.randint(0, 40))) +
                        rng.choice(endings))
                rate, pitch = prosody(
                    text, rng.choice(rates), rng.choice(pitches),
                    rng.randint(0, 10), rng.randint(0, 10),
                    rng.choice((True, False)), style)
                self.assertRegex(rate, r"^[+-]\d+%$")
                self.assertRegex(pitch, r"^[+-]\d+Hz$")

    def test_styles_clamp_rate_and_pitch(self):
        for style in ("calm", "warm", "energetic"):
            low_r, high_r, low_p, high_p = style_bounds(style)
            for enabled in (True, False):
                for _ in range(50):
                    rate, pitch = prosody("Ready?", "+20%", "+15Hz", 10, 10,
                                          enabled, style)
                    self.assertRegex(rate, r"^[+-]\d+%$")
                    self.assertRegex(pitch, r"^[+-]\d+Hz$")
                    self.assertGreaterEqual(int(rate.rstrip("%")), low_r)
                    self.assertLessEqual(int(rate.rstrip("%")), high_r)
                    self.assertGreaterEqual(int(pitch.rstrip("Hz")), low_p)
                    self.assertLessEqual(int(pitch.rstrip("Hz")), high_p)


class TestNet(unittest.TestCase):
    def test_private_literals(self):
        for value in ("127.0.0.1", "10.0.0.1", "192.168.1.5", "172.16.0.1",
                      "169.254.1.1", "0.0.0.0", "::1", "fe80::1"):
            self.assertFalse(net.ip_is_public(value), value)

    def test_public_literals(self):
        for value in ("8.8.8.8", "1.1.1.1", "172.32.0.1", "2606:4700:4700::1111"):
            self.assertTrue(net.ip_is_public(value), value)

    def test_local_urls_rejected(self):
        self.assertFalse(net.is_public_url("http://127.0.0.1:8000/x"))
        self.assertFalse(net.is_public_url("http://localhost/x"))
        self.assertFalse(net.is_public_url("file:///C:/Windows"))
        self.assertFalse(net.is_public_url(""))


class TestSkills(unittest.TestCase):
    def setUp(self):
        @skill("test skill")
        def iris_test_tool(name, extra="x"):
            return "ok " + name

        @skill("blocked skill", level=3)
        def iris_blocked_tool():
            return "nope"

        self.tool = "iris_test_tool"

    def tearDown(self):
        REGISTRY.pop("iris_test_tool", None)
        REGISTRY.pop("iris_blocked_tool", None)

    def test_schema(self):
        names = [s["function"]["name"] for s in tool_schemas()]
        self.assertIn(self.tool, names)
        self.assertNotIn("iris_blocked_tool", names)
        schema = next(s for s in tool_schemas() if s["function"]["name"] == self.tool)
        self.assertEqual(schema["function"]["parameters"]["required"], ["say", "name"])
        self.assertIn("say", schema["function"]["parameters"]["properties"])

    def test_run(self):
        observe, result = run_tool(self.tool, {"say": "hi", "name": "bob"}, lambda q: True)
        self.assertEqual(result, "ok bob")
        self.assertFalse(observe)

    def test_missing_args(self):
        _, result = run_tool(self.tool, {"say": "hi"}, lambda q: True)
        self.assertIn("missing arguments", result)

    def test_unknown(self):
        _, result = run_tool("nope_not_here", {}, lambda q: True)
        self.assertIn("unknown tool", result)


class TestToolBudget(unittest.TestCase):
    """Small free models get a relevant subset instead of an oversized request."""

    @staticmethod
    def _tool(name, description):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": {}}}}

    def test_zero_budget_keeps_everything(self):
        items = [self._tool("a", "x" * 400), self._tool("b", "y" * 400)]
        self.assertEqual(select_tools(items, 0), items)
        self.assertEqual(select_tools(items, None), items)

    def test_small_budget_keeps_the_matching_tool(self):
        items = [self._tool("open_chrome", "Open Chrome."),
                 self._tool("take_screenshot", "Grab the screen."),
                 self._tool("volume", "Change the volume.")]
        chosen = select_tools(items, 70, "open chrome please")
        names = [item["function"]["name"] for item in chosen]
        self.assertIn("open_chrome", names)
        self.assertLess(len(chosen), len(items))

    def test_core_tools_survive_a_tight_budget(self):
        items = [self._tool("open_app", "Open an app."),
                 self._tool("system_info", "Battery and RAM."),
                 self._tool("rare_tool", "Something obscure.")]
        names = [item["function"]["name"]
                 for item in select_tools(items, 130, "hello")]
        self.assertIn("open_app", names)
        self.assertIn("system_info", names)

    def test_budget_is_actually_respected(self):
        items = [self._tool("tool_%d" % i, "d" * 300) for i in range(40)]
        chosen = select_tools(items, 500, "anything")
        self.assertLess(len(chosen), len(items))
        self.assertGreaterEqual(len(chosen), 1)


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        memory.init(self.db)

    def tearDown(self):
        memory.close()
        self.tmp.cleanup()

    def test_facts(self):
        memory.add_fact("my favourite browser is Firefox")
        memory.add_fact("I live in Chennai")
        hits = memory.search_facts("what is my favourite browser", 3)
        self.assertTrue(any("Firefox" in h for h in hits))
        self.assertEqual(memory.forget_fact("Firefox"), 1)
        self.assertEqual(memory.search_facts("favourite browser", 3), [])

    def test_turns(self):
        memory.add_turn("user", "hi")
        memory.add_turn("assistant", "hello")
        memory.add_turn("user", "again")
        recent = memory.recent(4)
        self.assertEqual([m["role"] for m in recent], ["user", "assistant", "user"])
        self.assertEqual(recent[-1]["content"], "again")

    def test_prune(self):
        for i in range(12):
            memory.add_turn("user", "turn {0}".format(i))
        old = CFG["memory"]["max_turns"]
        CFG["memory"]["max_turns"] = 5
        try:
            memory.prune_turns()
            self.assertLessEqual(len(memory.recent(50)), 5)
        finally:
            CFG["memory"]["max_turns"] = old


class TestBrainFallback(unittest.TestCase):
    def setUp(self):
        from core import brain, connectivity
        brain.reset_breakers()
        connectivity.reset_for_tests(True)

    def tearDown(self):
        from core import brain
        brain.reset_breakers()

    def test_status_and_rate(self):
        from core import brain

        class RateLimitError(Exception):
            status_code = 429

        self.assertEqual(brain._status(RateLimitError("slow down")), 429)
        self.assertTrue(brain._is_rate(RateLimitError("slow down")))
        self.assertFalse(brain._is_rate(ValueError("bad key")))
        self.assertEqual(brain._status(Exception("upstream error 503")), 503)

    def test_provider_kwargs(self):
        from core import brain
        messages = [{"role": "user", "content": "hi"}]
        kw = brain._kwargs("groq", "openai/gpt-oss-20b", messages, None, 100)
        self.assertEqual(kw["extra_body"], {"reasoning_effort": "low"})
        kw = brain._kwargs("openrouter", "google/gemma-4-31b-it:free",
                           messages, None, 100)
        self.assertNotIn("extra_body", kw)

    def test_chain_is_free_and_ordered(self):
        from unittest import mock
        from core import brain
        old = CFG["models"].get("free_fallbacks")
        CFG["models"]["free_fallbacks"] = [
            {"provider": "openrouter", "model": "paid/model"},
        ]
        try:
            with mock.patch.object(brain, "_key", lambda provider: "test"), \
                    mock.patch.object(brain, "_groq_models",
                                      lambda limit=6: []), \
                    mock.patch.object(brain, "_openrouter_models",
                                      lambda limit=10: ["google/gemma-4-31b-it:free"]):
                models = brain.chain()
        finally:
            CFG["models"]["free_fallbacks"] = old
        self.assertEqual(models[0], ("groq", CFG["models"]["llm"]))
        self.assertIn(("openrouter", "google/gemma-4-31b-it:free"), models)
        self.assertIn(("gemini", CFG["models"]["gemini_model"]), models)
        self.assertNotIn(("openrouter", "paid/model"), models)
        for provider, model in models:
            if provider == "openrouter":
                self.assertTrue(model.endswith(":free"), model)

    def test_openrouter_override_stays_free(self):
        from core import brain
        old = CFG["models"].get("openrouter_models")
        CFG["models"]["openrouter_models"] = ["paid/model", "free/model:free"]
        try:
            self.assertEqual(brain._openrouter_models(4), ["free/model:free"])
        finally:
            CFG["models"]["openrouter_models"] = old

    def test_all_rate_limited_waits_once(self):
        from unittest import mock
        from core import brain

        class RateLimitError(Exception):
            pass

        calls, said = [], []

        def fake_client(provider):
            calls.append(provider)
            raise RateLimitError("Rate limit reached. Please try again in 2s.")

        with mock.patch.object(brain, "chain", lambda: [("groq", "m1")]), \
                mock.patch.object(brain, "_client", fake_client), \
                mock.patch.object(brain.time, "sleep", lambda seconds: None):
            msg = brain.ask_stream([{"role": "user", "content": "hi"}],
                                   on_sentence=said.append)
        self.assertIsNone(msg)
        self.assertIn("One moment.", said)
        self.assertGreaterEqual(len(calls), 2)


class TestPaths(unittest.TestCase):
    def test_workspace_sandbox(self):
        from skills.hands import _in_workspace
        self.assertTrue(str(_in_workspace("note.txt")).startswith(str(ROOT)))
        with self.assertRaises(ValueError):
            _in_workspace("../../escape.txt")
        with self.assertRaises(ValueError):
            _in_workspace("")

    def test_browser_number(self):
        from skills.browser_agent import _num
        self.assertEqual(_num("[7]"), 7)
        self.assertEqual(_num("3"), 3)
        self.assertIsNone(_num("hello"))


class TestWake(unittest.TestCase):
    def test_phrases(self):
        from core import wake
        got = wake.phrases()
        self.assertEqual(got[0], "iris")
        self.assertIn("hey iris", got)
        self.assertIn("jarvis", got)
        self.assertEqual(len(got), len(set(got)))

    def test_matching(self):
        from core import wake
        wanted = ["iris", "hey iris", "jarvis"]
        self.assertTrue(wake._matches("hey iris", wanted))
        self.assertTrue(wake._matches("okay so iris", wanted))
        self.assertTrue(wake._matches("jarvis", wanted))
        self.assertFalse(wake._matches("irish stew", wanted))
        self.assertFalse(wake._matches("", wanted))


class TestSurface(unittest.TestCase):
    def test_url_detection(self):
        from skills import surface
        self.assertTrue(surface._is_url("example.com"))
        self.assertTrue(surface._is_url("https://example.com/a?b=1"))
        self.assertFalse(surface._is_url("open chrome"))
        self.assertFalse(surface._is_url(str(ROOT)))

    def test_entries(self):
        from skills import surface
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / "a.txt"
            note.write_text("hello", encoding="utf-8")
            sub = Path(d) / "sub"
            sub.mkdir()
            file_entry = surface._entry(note)
            dir_entry = surface._entry(sub)
        self.assertEqual(file_entry["kind"], "file")
        self.assertEqual(file_entry["ext"], "txt")
        self.assertGreater(file_entry["size"], 0)
        self.assertEqual(dir_entry["kind"], "dir")


class TestPersona(unittest.TestCase):
    def test_percent(self):
        from core.persona import percent
        self.assertEqual(percent(0.7), 70)
        self.assertEqual(percent(0), 0)
        self.assertEqual(percent(1), 100)
        self.assertEqual(percent("nonsense"), 50)

    def test_prompt_is_human(self):
        from core.persona import build_prompt
        prompt = build_prompt(["likes strong coffee"])
        self.assertIn("Iris", prompt)
        self.assertIn("boss", prompt)
        self.assertIn("voice conversation", prompt)
        self.assertIn("tease", prompt)
        self.assertIn("follow-up question", prompt)
        self.assertIn("about 70%", prompt)
        self.assertIn("likes strong coffee", prompt)
        self.assertIn("opinions", prompt)

    def test_prompt_follows_config(self):
        from core.persona import build_prompt
        cfg = {"persona": {"name": "Iris", "call_me": "chief", "humour": 0.2,
                           "teasing": 0.1, "curiosity": 0.9,
                           "opinions": False, "vibe": "dry and blunt"}}
        prompt = build_prompt([], config=cfg)
        self.assertIn("chief", prompt)
        self.assertIn("dry and blunt", prompt)
        self.assertIn("About 10%", prompt)          # teasing
        self.assertIn("about 20%", prompt)          # humour
        self.assertIn("about 90%", prompt)          # curiosity
        self.assertIn("Keep your opinions to yourself", prompt)
        self.assertNotIn("Things you remember", prompt)

    def test_prompt_has_no_template_leftovers(self):
        from core.persona import build_prompt
        prompt = build_prompt([])
        self.assertNotIn("None", prompt)
        self.assertNotIn("{", prompt)
        self.assertNotIn("}", prompt)


class TestModels(unittest.TestCase):
    def test_bigger_general_models_rank_higher(self):
        from core.brain import _model_rank
        strong = _model_rank("nvidia/nemotron-3.5-super-vl:free", 1000000)
        tiny = _model_rank("liquid/lfm-3-1.7b:free", 262144)
        self.assertGreater(strong, tiny)

    def test_large_context_and_known_families_get_a_bonus(self):
        from core.brain import _model_rank
        good = _model_rank("google/gemma-4-31b-it:free", 200000)
        weak = _model_rank("unknown/nano:free", 8000)
        self.assertGreater(good, weak)

    def test_tiny_models_lose_points(self):
        from core.brain import _model_rank
        self.assertLess(_model_rank("someone/tiny-2b:free", 4000),
                        _model_rank("someone/tiny-2b:free", 400000))


class TestChrome(unittest.TestCase):
    def test_profile_nicknames_resolve(self):
        from skills import browser_agent
        cfg = {"profiles": {"work": "Profile 8", "balamurugan": "Profile 2"}}
        with mock.patch.object(browser_agent, "_chrome_cfg", lambda: cfg):
            self.assertEqual(browser_agent._resolve_profile("work"), "Profile 8")
            self.assertEqual(browser_agent._resolve_profile("Work"), "Profile 8")
            self.assertEqual(browser_agent._resolve_profile("Profile 3"), "Profile 3")
            self.assertEqual(browser_agent._resolve_profile(""), "")

    def test_iris_profile_lives_under_data(self):
        from skills import browser_agent
        cfg = {"user_data_dir": "iris", "profile_directory": ""}
        with mock.patch.object(browser_agent, "_chrome_cfg", lambda: cfg):
            data, profile, label = browser_agent._target("iris")
        self.assertEqual(label, "iris")
        self.assertEqual(profile, "")
        self.assertIn("chrome-profile", str(data))

    def test_devtools_endpoint_is_loopback(self):
        from skills import browser_agent
        with mock.patch.object(browser_agent, "_chrome_cfg",
                               lambda: {"debug_port": 9333}):
            self.assertEqual(browser_agent._endpoint(),
                             "http://127.0.0.1:9333")


class TestPlatform(unittest.TestCase):
    def test_settings_links(self):
        from skills.system import _page_uri
        self.assertEqual(_page_uri("wifi"), "ms-settings:network-wifi")
        self.assertEqual(_page_uri("Wi-Fi settings"), "ms-settings:network-wifi")
        self.assertEqual(_page_uri("bluetooth"), "ms-settings:bluetooth")
        self.assertEqual(_page_uri("microphone"), "ms-settings:privacy-microphone")
        self.assertEqual(_page_uri("night light"), "ms-settings:nightlight")
        self.assertEqual(_page_uri("windows update"), "ms-settings:windowsupdate")
        self.assertEqual(_page_uri("ms-settings:display"), "ms-settings:display")
        self.assertIsNone(_page_uri("quantum flux capacitor"))

    def test_protected_processes(self):
        from skills import laptop
        for name in ("lsass.exe", "csrss.exe", "winlogon.exe", "SYSTEM"):
            self.assertTrue(laptop._is_protected(name), name)
        self.assertFalse(laptop._is_protected("chrome.exe"))

    def test_result_normalising(self):
        from skills.websearch import _normalise
        items = _normalise([
            {"title": "One", "href": "https://example.com/a", "body": "hello"},
            {"title": "Bad", "href": "javascript:alert(1)"},
        ], "web")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "One")
        self.assertEqual(items[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
