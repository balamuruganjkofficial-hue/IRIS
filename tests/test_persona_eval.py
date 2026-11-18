"""Persona eval scorers, checked on canned replies. No network, no models."""

import json
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from core import persona
from scripts import persona_eval as pe

# Fixed persona settings so the tests do not depend on the live config.yaml.
TEST_PERSONA = {
    "name": "Iris",
    "call_me": "boss",
    "vibe": "warm, playful, quietly cheeky",
    "humour": 0.7,
    "teasing": 0.6,
    "curiosity": 0.7,
}
TEST_PROMPT = persona.build_prompt(config={"persona": TEST_PERSONA})


def check(score, key):
    """One check from a ReplyScore, by key."""
    for item in score.checks:
        if item.key == key:
            return item
    raise AssertionError("no check named " + key)


def demo_result(categories=None, limit=0):
    cases = pe.select_cases(categories, limit)
    replies = [pe.DEMO_REPLIES[c.id] for c in cases]
    return pe.score_all(cases, replies, persona_cfg=TEST_PERSONA)


class TestTextScorers(unittest.TestCase):
    def test_sentence_count_and_abbreviations(self):
        self.assertEqual(pe.count_sentences("One. Two! Three?"), 3)
        self.assertEqual(pe.count_sentences("It's 3 a.m. and I'm still up."), 1)
        self.assertEqual(pe.count_sentences("Ask Dr. Smith. He knows."), 2)
        self.assertEqual(pe.count_sentences(""), 0)

    def test_markdown_features(self):
        self.assertEqual(pe.find_markdown("Just a plain spoken reply."), [])
        cases = {
            "- a bullet": "bullet",
            "1. a numbered line": "numbered",
            "**bold**": "bold",
            "`code`": "code",
            "# heading": "heading",
            "[label](https://example.com)": "link",
            "| a | b |": "table",
        }
        for text, feature in cases.items():
            self.assertIn(feature, pe.find_markdown(text), text)

    def test_helpdesk_phrases_are_case_and_quote_insensitive(self):
        self.assertEqual(pe.find_helpdesk("I'd be happy to help."),
                         ["i'd be happy to"])
        self.assertEqual(pe.find_helpdesk("I\u2019d be happy to help."),
                         ["i'd be happy to"])
        self.assertEqual(pe.find_helpdesk("How can I assist?"),
                         ["how can i assist"])
        self.assertEqual(pe.find_helpdesk("As an AI, I cannot."),
                         ["as an ai"])
        self.assertEqual(pe.find_helpdesk("Right, that's done."), [])

    def test_helpdesk_list_is_configurable(self):
        self.assertEqual(pe.find_helpdesk("Let me check.", ["let me"]),
                         ["let me"])
        self.assertEqual(pe.find_helpdesk("Let me check.", ["as an ai"]), [])

    def test_load_helpdesk_skips_blanks_and_comments(self):
        handle, path = tempfile.mkstemp(suffix=".txt")
        os.close(handle)
        try:
            Path(path).write_text(
                "# my phrases\nAs an AI\n\n  I'd be happy to  \n",
                encoding="utf-8")
            self.assertEqual(pe.load_helpdesk(path),
                             ("as an ai", "i'd be happy to"))
        finally:
            os.unlink(path)

    def test_load_helpdesk_can_be_empty(self):
        handle, path = tempfile.mkstemp(suffix=".txt")
        os.close(handle)
        try:
            Path(path).write_text("# only a comment\n\n", encoding="utf-8")
            self.assertEqual(pe.load_helpdesk(path), ())
        finally:
            os.unlink(path)

    def test_banned_opener_only_looks_at_the_first_sentence(self):
        self.assertEqual(pe.banned_opener("Certainly, boss."), "certainly")
        self.assertEqual(pe.banned_opener("Of course."), "of course")
        self.assertEqual(pe.banned_opener("I'd be happy to help."),
                         "i'd be happy to")
        self.assertIsNone(pe.banned_opener("Ha. Of course not."))
        self.assertIsNone(pe.banned_opener("Right, done."))

    def test_name_count_and_overuse(self):
        self.assertEqual(pe.name_count("Morning, boss."), (1, 0))
        self.assertEqual(pe.name_count("Boss, boss, boss."), (3, 0))
        self.assertEqual(pe.name_count("Iris will do it."), (0, 1))
        case = pe.Case("x", "small_talk", "hi", False)
        self.assertFalse(check(pe.score_reply(case, "Boss, boss."), "name").ok)
        self.assertTrue(check(pe.score_reply(case, "Morning, boss."), "name").ok)

    def test_questions_per_reply_and_overall_rate(self):
        case = pe.Case("x", "small_talk", "hi", False)
        self.assertFalse(check(pe.score_reply(case, "Why? What happened?"),
                               "questions").ok)
        self.assertTrue(check(pe.score_reply(case, "What happened?"),
                              "questions").ok)
        cases = [pe.Case(str(i), "small_talk", "hi", False) for i in range(3)]
        result = pe.score_all(cases, ["Yes?", "No question.", "Maybe?"],
                              persona_cfg={"curiosity": 0.5})
        self.assertAlmostEqual(result.question_rate, 200.0 / 3.0, places=4)

    def test_opener_key_and_repeated_openers(self):
        self.assertEqual(pe.opener_key("Ha. Morning, boss."), "ha")
        self.assertEqual(pe.opener_key("Right, that error is odd."),
                         "right that error")
        self.assertEqual(pe.opener_key(""), "")
        replies = ["Ha. One.", "Ha. Two.", "Ha. Three.", "Right."]
        self.assertEqual(pe.repeated_openers(replies), {"ha": 3})
        self.assertEqual(pe.repeated_openers(replies[:2]), {})

    def test_repeated_opener_check_on_the_third_use(self):
        case = pe.Case("x", "small_talk", "hi", False)
        reply = "Ha. Morning, boss."
        key = pe.opener_key(reply)
        self.assertTrue(check(pe.score_reply(
            case, reply, opener_counts=Counter({key: 2})), "openers").ok)
        self.assertFalse(check(pe.score_reply(
            case, reply, opener_counts=Counter({key: 3})), "openers").ok)

    def test_tone_on_upset_turns(self):
        self.assertTrue(pe.tone_ok("Oof, that's rough. Want to talk?")[0])
        ok, detail = pe.tone_ok("At least you tried. Ha.")
        self.assertFalse(ok)
        self.assertIn("teasing", detail)
        ok, detail = pe.tone_ok("Open the folder.")
        self.assertFalse(ok)
        self.assertIn("warm", detail)

    def test_tone_is_only_scored_when_the_user_is_upset(self):
        calm = pe.Case("calm", "jokes", "Tell me a joke.", False)
        upset = pe.Case("upset", "bad_day", "I'm fed up.", True)
        self.assertFalse(any(c.key == "tone" for c in
                             pe.score_reply(calm, "Ha. No.").checks))
        self.assertTrue(any(c.key == "tone" for c in
                            pe.score_reply(upset, "Ha. No.").checks))


class TestReplyScoring(unittest.TestCase):
    def test_a_good_upset_reply_passes_every_check(self):
        case = pe.Case("bd", "bad_day", "I'm fed up.", True)
        reply = "Oof, that's rough. Want to talk?"
        counts = Counter({pe.opener_key(reply): 1})
        score = pe.score_reply(case, reply, opener_counts=counts)
        self.assertEqual(score.score, 1.0)
        self.assertTrue(all(c.ok for c in score.checks), score.checks)

    def test_markdown_helpdesk_and_name_failures_are_reported(self):
        case = pe.Case("cm", "commands", "open chrome", False)
        reply = "**Sure.**\n1. One\n2. Two\nI'd be happy to help, boss boss."
        score = pe.score_reply(case, reply)
        self.assertFalse(check(score, "markdown").ok)
        self.assertFalse(check(score, "helpdesk").ok)
        self.assertFalse(check(score, "name").ok)
        self.assertIn("bold", check(score, "markdown").detail)

    def test_too_many_sentences_fail(self):
        case = pe.Case("x", "small_talk", "hi", False)
        score = pe.score_reply(case, "One. Two. Three. Four.")
        self.assertFalse(check(score, "sentences").ok)
        self.assertIn("4", check(score, "sentences").detail)

    def test_an_empty_reply_scores_zero(self):
        case = pe.Case("x", "small_talk", "hi", False)
        score = pe.score_reply(case, "   ")
        self.assertEqual(score.score, 0.0)
        self.assertEqual([c.key for c in score.checks], ["reply"])
        self.assertFalse(score.checks[0].ok)

    def test_worst_replies_lowest_score_first(self):
        cases = [pe.Case("a", "small_talk", "hi", False),
                 pe.Case("b", "small_talk", "hi", False),
                 pe.Case("c", "small_talk", "hi", False)]
        replies = ["Morning, boss.",
                   "Boss, boss.",
                   "**Bold.** I'd be happy to help, boss boss."]
        result = pe.score_all(cases, replies, persona_cfg=TEST_PERSONA)
        self.assertEqual([s.case.id for s in pe.worst_replies(result)],
                         ["c", "b"])


class TestReport(unittest.TestCase):
    def test_blame_maps_failed_checks_to_prompt_sentences(self):
        blame = pe.blame_prompt(["sentences", "name", "tone"], TEST_PROMPT)
        self.assertTrue(any("One to three short sentences" in b
                            for b in blame), blame)
        self.assertTrue(any("Call him boss now and then" in b
                            for b in blame), blame)
        self.assertTrue(any("drop the act and be warm" in b
                            for b in blame), blame)
        self.assertEqual(pe.blame_prompt(["reply"], TEST_PROMPT), [])

    def test_scorecard_has_the_rules_rate_and_worst_replies(self):
        result = demo_result()
        text = pe.format_scorecard(result, prompt=TEST_PROMPT,
                                   persona_cfg=TEST_PERSONA, label="demo")
        for needle in ("Iris persona eval", "Rule", "Overall:", "Question rate:",
                       "Worst 3", "prompt lines likely responsible",
                       "band 45-95%", "Repeated openers:"):
            self.assertIn(needle, text)
        self.assertTrue(all(ord(c) < 128 for c in text))

    def test_json_scorecard_is_serialisable(self):
        result = demo_result()
        data = pe.to_json(result, "demo")
        json.dumps(data)
        self.assertEqual(data["summary"]["replies"], len(pe.CASES))
        self.assertEqual(len(data["cases"]), len(pe.CASES))
        self.assertIn("worst", data)

    def test_demo_replies_cover_every_case_and_category(self):
        self.assertEqual(len(pe.CASES), 31)
        self.assertEqual(set(pe.DEMO_REPLIES), {c.id for c in pe.CASES})
        self.assertEqual({c.category for c in pe.CASES}, set(pe.CATEGORIES))
        self.assertGreaterEqual(sum(1 for c in pe.CASES if c.upset), 8)

    def test_demo_scorecard_shows_a_repeated_opener_and_a_failure(self):
        result = demo_result()
        self.assertEqual(result.repeated, {"ha": 3})
        self.assertTrue(pe.worst_replies(result))
        text = pe.format_scorecard(result, prompt=TEST_PROMPT,
                                   persona_cfg=TEST_PERSONA)
        self.assertIn("ha x3", text)
        self.assertIn("WARN", text)          # question rate is below curiosity

    def test_select_cases_by_category_and_limit(self):
        bad_day = pe.select_cases(["bad_day"])
        self.assertTrue(bad_day)
        self.assertTrue(all(c.category == "bad_day" for c in bad_day))
        self.assertEqual(len(pe.select_cases(None, 3)), 3)
        self.assertEqual(pe.select_cases(["nope"]), [])


if __name__ == "__main__":
    unittest.main()
