"""Unit tests for the pure parts of Iris. Run with:  python -m unittest discover -s tests"""

import os
import tempfile
import unittest
from pathlib import Path

from core import memory, net
from core.config import CFG, ROOT
from core.skill import REGISTRY, run_tool, skill, tool_schemas
from core.text import (answer, clean, is_junk, just_thanks, prosody, sentences,
                       wants_sleep)


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
            self.assertGreaterEqual(int(rate.rstrip("%")), 4)
            self.assertLessEqual(int(rate.rstrip("%")), 12)
            self.assertGreaterEqual(int(pitch.rstrip("Hz")), -3)
            self.assertLessEqual(int(pitch.rstrip("Hz")), 3)
        self.assertEqual(prosody("x", "+8%", "+0Hz", 4, 3, enabled=False),
                         ("+8%", "+0Hz"))

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
                    mock.patch.object(brain, "_openrouter_models",
                                      lambda limit=4: ["google/gemma-4-31b-it:free"]):
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
