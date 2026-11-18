"""Generated skills: validation, persistence, confirmation and execution."""

import json
import tempfile
import unittest
from pathlib import Path

from core import recipe
from core.skill import REGISTRY, register, run_tool, tool_schemas


class TestRecipeValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        recipe.reset_for_tests(self.tmp.name)

        def open_thing(name):
            return "opened " + name

        def risky(name):
            return "risky " + name

        def blocked(name):
            return "never"

        register("iris_recipe_open", open_thing, "Open a test thing.",
                 params=["name"], required=["name"])
        register("iris_recipe_risky", risky, "A risky test thing.", level=2,
                 params=["name"], required=["name"])
        register("iris_recipe_blocked", blocked, "A blocked test thing.", level=3,
                 params=["name"], required=["name"])

    def tearDown(self):
        for name in ("iris_recipe_open", "iris_recipe_risky",
                     "iris_recipe_blocked"):
            REGISTRY.pop(name, None)
        recipe.reset_for_tests()
        self.tmp.cleanup()

    def _raw(self, **overrides):
        raw = {"name": "test_skill", "description": "Open a thing for me.",
               "args": [], "steps": [{"tool": "iris_recipe_open",
                                      "args": {"name": "notepad"}}]}
        raw.update(overrides)
        return raw

    def test_accepts_a_valid_recipe(self):
        clean, error = recipe.validate(self._raw())
        self.assertEqual(error, "")
        self.assertEqual(clean["name"], "test_skill")
        self.assertEqual(clean["steps"][0]["args"]["name"], "notepad")

    def test_rejects_unknown_tool(self):
        raw = self._raw(steps=[{"tool": "nope", "args": {}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("unknown tool", error)

    def test_rejects_blocked_tool(self):
        raw = self._raw(steps=[{"tool": "iris_recipe_blocked",
                                "args": {"name": "x"}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("blocked tool", error)

    def test_rejects_missing_required_argument(self):
        raw = self._raw(steps=[{"tool": "iris_recipe_open", "args": {}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("missing name", error)

    def test_rejects_unknown_argument(self):
        raw = self._raw(steps=[{"tool": "iris_recipe_open",
                                "args": {"name": "x", "colour": "red"}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("does not take colour", error)

    def test_rejects_unknown_placeholder(self):
        raw = self._raw(args=["topic"],
                        steps=[{"tool": "iris_recipe_open",
                                "args": {"name": "{thing}"}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("{thing}", error)

    def test_rejects_too_many_steps(self):
        step = {"tool": "iris_recipe_open", "args": {"name": "x"}}
        raw = self._raw(steps=[step] * (recipe.MAX_STEPS + 1))
        _clean, error = recipe.validate(raw)
        self.assertIn("at most", error)

    def test_rejects_management_tool(self):
        raw = self._raw(steps=[{"tool": "forget_skill", "args": {"name": "x"}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("management", error)

    def test_rejects_builtin_name_collision(self):
        raw = self._raw(name="iris_recipe_open")
        _clean, error = recipe.validate(raw)
        self.assertIn("already exists", error)

    def test_rejects_another_recipe(self):
        first = self._raw(name="first_skill")
        clean, error = recipe.validate(first)
        self.assertEqual(error, "")
        recipe.register_recipe(clean)
        raw = self._raw(name="second_skill",
                        steps=[{"tool": "first_skill", "args": {}}])
        _clean, error = recipe.validate(raw)
        self.assertIn("another learned skill", error)

    def test_creation_asks_before_saving(self):
        steps = json.dumps([{"tool": "iris_recipe_open",
                             "args": {"name": "notepad"}}])
        asked = []

        def confirm(question):
            asked.append(question)
            return False

        _observe, result = run_tool(
            "create_skill",
            {"say": "Saving it.", "description": "Open Notepad for me.",
             "steps": steps, "name": "open_notepad"},
            confirm)
        self.assertIn("cancelled", result)
        self.assertIn("save the skill open_notepad with 1 steps", asked[0])
        self.assertNotIn("[{", asked[0])
        self.assertNotIn("open_notepad", REGISTRY)
        self.assertFalse((Path(self.tmp.name) / "open_notepad.json").exists())

    def test_create_persists_and_registers(self):
        steps = json.dumps([{"tool": "iris_recipe_open",
                             "args": {"name": "notepad"}}])
        _observe, result = run_tool(
            "create_skill",
            {"say": "Saving it.", "description": "Open Notepad for me.",
             "steps": steps, "name": "open_notepad"},
            lambda _question: True)
        self.assertIn("Learned the skill open_notepad", result)
        self.assertIn("open_notepad", REGISTRY)
        path = Path(self.tmp.name) / "open_notepad.json"
        self.assertTrue(path.exists())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["steps"][0]["tool"], "iris_recipe_open")
        names = [item["function"]["name"] for item in tool_schemas()]
        self.assertIn("open_notepad", names)

    def test_generated_name_from_description(self):
        steps = json.dumps([{"tool": "iris_recipe_open",
                             "args": {"name": "notepad"}}])
        _observe, result = run_tool(
            "create_skill",
            {"say": "Saving it.", "description": "Open Notepad every morning.",
             "steps": steps},
            lambda _question: True)
        self.assertIn("open_notepad_every_morning", result)
        self.assertIn("open_notepad_every_morning", REGISTRY)

    def test_learned_skill_runs_its_steps(self):
        steps = json.dumps([
            {"tool": "iris_recipe_open", "args": {"name": "notepad"}},
            {"tool": "iris_recipe_open", "args": {"name": "{second}"}},
        ])
        _observe, result = run_tool(
            "create_skill",
            {"say": "Saving it.", "description": "Open two things for me.",
             "steps": steps, "args": "second", "name": "open_two"},
            lambda _question: True)
        self.assertIn("Learned", result)
        _observe, result = run_tool(
            "open_two", {"say": "Opening.", "second": "calculator"},
            lambda _question: True)
        self.assertIn("opened notepad", result)
        self.assertIn("opened calculator", result)

    def test_risky_step_still_asks(self):
        steps = json.dumps([{"tool": "iris_recipe_risky",
                             "args": {"name": "the file"}}])
        run_tool("create_skill",
                 {"say": "Saving it.", "description": "Do the risky thing.",
                  "steps": steps, "name": "risky_skill"},
                 lambda _question: True)
        asked = []

        def confirm(question):
            asked.append(question)
            return True

        _observe, result = run_tool(
            "risky_skill", {"say": "Doing it."}, confirm)
        self.assertIn("risky the file", result)
        self.assertTrue(any("iris_recipe_risky" in q for q in asked))

    def test_error_stops_the_recipe(self):
        def broken(name):
            return "ERROR: " + name

        register("iris_recipe_broken", broken, "A broken test thing.",
                 params=["name"], required=["name"])
        calls = []

        def after(name):
            calls.append(name)
            return "done " + name

        register("iris_recipe_after", after, "A following test thing.",
                 params=["name"], required=["name"])
        try:
            steps = json.dumps([
                {"tool": "iris_recipe_broken", "args": {"name": "first"}},
                {"tool": "iris_recipe_after", "args": {"name": "second"}},
            ])
            run_tool("create_skill",
                     {"say": "Saving it.", "description": "Break on purpose.",
                      "steps": steps, "name": "broken_skill"},
                     lambda _question: True)
            _observe, result = run_tool(
                "broken_skill", {"say": "Trying."}, lambda _question: True)
            self.assertIn("Stopped at step 1", result)
            self.assertEqual(calls, [])
        finally:
            REGISTRY.pop("iris_recipe_broken", None)
            REGISTRY.pop("iris_recipe_after", None)

    def test_list_shows_learned_skills(self):
        steps = json.dumps([{"tool": "iris_recipe_open",
                             "args": {"name": "{thing}"}}])
        run_tool("create_skill",
                 {"say": "Saving it.", "description": "Open a chosen thing.",
                  "steps": steps, "args": "thing", "name": "open_chosen"},
                 lambda _question: True)
        _observe, result = run_tool("list_skills", {"say": "Checking."},
                                    lambda _question: True)
        self.assertIn("open_chosen", result)
        self.assertIn("Open a chosen thing.", result)
        self.assertIn("thing", result)

    def test_forget_removes_file_and_tool(self):
        steps = json.dumps([{"tool": "iris_recipe_open",
                             "args": {"name": "notepad"}}])
        run_tool("create_skill",
                 {"say": "Saving it.", "description": "Open Notepad for me.",
                  "steps": steps, "name": "open_notepad"},
                 lambda _question: True)
        _observe, result = run_tool(
            "forget_skill", {"say": "Dropping it.", "name": "open_notepad"},
            lambda _question: True)
        self.assertIn("Forgot", result)
        self.assertNotIn("open_notepad", REGISTRY)
        self.assertFalse((Path(self.tmp.name) / "open_notepad.json").exists())

    def test_load_all_skips_bad_files(self):
        good = self._raw(name="loaded_skill")
        (Path(self.tmp.name) / "loaded_skill.json").write_text(
            json.dumps(good), encoding="utf-8")
        (Path(self.tmp.name) / "broken.json").write_text("{not json",
                                                         encoding="utf-8")
        loaded = recipe.load_all(self.tmp.name)
        self.assertEqual(loaded, 1)
        self.assertIn("loaded_skill", REGISTRY)


if __name__ == "__main__":
    unittest.main()
