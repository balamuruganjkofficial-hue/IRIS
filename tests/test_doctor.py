"""Unit tests for the doctor report: formatting, timeouts and secret safety."""

import tempfile
import time
import unittest
from pathlib import Path

from scripts import doctor


class TestDoctorReport(unittest.TestCase):
    def test_every_line_has_status_and_one_line_fix_hint(self):
        checks = [
            doctor.ok("Python", "version 3.11.9"),
            doctor.warn("Venv", "system interpreter", "use venv\\Scripts\\python.exe"),
            doctor.fail("Net Groq", "api.groq.com: timeout", "check the cable"),
        ]
        text = doctor.format_report(checks, 1.25)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 4)          # three checks plus the summary
        for line in lines:
            self.assertRegex(line, r"^(OK|WARN|FAIL)\s")
            self.assertIn(" | fix: ", line)
            self.assertNotIn("\n", line)
        self.assertIn("3 checks: 1 OK, 1 WARN, 1 FAIL in 1.2s", text)

    def test_summary_status_reflects_worst_check(self):
        warns = doctor.format_report([doctor.warn("A", "b", "c")], 0.1)
        self.assertTrue(warns.startswith("WARN "))
        fails = doctor.format_report([doctor.fail("A", "b", "c")], 0.1)
        self.assertTrue(fails.startswith("FAIL "))
        clean = doctor.format_report([doctor.ok("A", "b")], 0.1)
        self.assertTrue(clean.startswith("OK "))
        self.assertTrue(clean.rstrip().endswith("fix: none"))

    def test_scrub_removes_credentials_and_non_ascii(self):
        text = doctor.scrub(
            "ERROR login api_key=sk-abcdefgh1234 with Bearer gsk_wxyz9876 \u2019")
        self.assertNotIn("sk-abcdefgh1234", text)
        self.assertNotIn("gsk_wxyz9876", text)
        self.assertIn("[redacted]", text)
        self.assertTrue(text.isascii())
        self.assertEqual(doctor.ascii_text("caf\u00e9"), "caf?")
        self.assertEqual(doctor.scrub("a\n\tb"), "a b")

    def test_missing_keys_are_dotted(self):
        data = {"persona": {"name": "Iris"}, "voice": {}}
        defaults = {"persona": {"name": "Iris", "call_me": "boss"},
                    "voice": {"wake_word": "iris", "cache": {"files": 4}}}
        self.assertEqual(doctor.missing_keys(data, defaults),
                         ["persona.call_me", "voice.wake_word", "voice.cache"])

    def test_config_report_lists_missing_keys_and_bad_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text("persona:\n  name: Iris\n", encoding="utf-8")
            lines = doctor.check_config(path, defaults={
                "persona": {"name": "Iris", "call_me": "boss"}})
            self.assertEqual(lines[0].status, "WARN")
            self.assertIn("persona.call_me", lines[0].detail)

            path.write_text("persona: [broken\n", encoding="utf-8")
            lines = doctor.check_config(path, defaults={})
            self.assertEqual(lines[0].status, "FAIL")
            self.assertIn("invalid YAML", lines[0].detail)

    def test_requirements_compare_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "requirements.txt"
            path.write_text("# comment\nfoo==1.0\nbar==2.0\nbaz==3.0\n",
                            encoding="utf-8")
            versions = {"foo": "1.0", "bar": "9.9"}
            lines = doctor.check_requirements(
                path, version_fn=lambda name: versions.get(name))
            self.assertEqual([item.status for item in lines],
                             ["OK", "WARN", "FAIL"])
            self.assertIn("bar 9.9, want 2.0", lines[1].detail)

    def test_log_lines_are_capped_and_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "iris.log"
            rows = ["2026-11-17 10:00:00,000 WARNING routine noise\n"] * 25
            rows.append("2026-11-17 10:00:01,000 INFO tool result: ERROR: gone\n")
            rows.append("2026-11-17 10:00:02,000 ERROR api_key=sk-abcdefghijklmnop\n")
            path.write_text("".join(rows), encoding="utf-8")
            lines = doctor.check_logs(path=path, limit=20)
            self.assertEqual(len(lines), 20)
            self.assertFalse(any("tool result" in item.detail for item in lines))
            self.assertTrue(any(item.status == "FAIL" for item in lines))
            report = doctor.format_report(lines, 0.1)
            self.assertNotIn("sk-abcdefghijklmnop", report)


class TestDoctorTimeouts(unittest.TestCase):
    def test_probe_returns_value_or_error_without_message(self):
        self.assertEqual(doctor.probe(lambda: 7, 0.5).value, 7)
        result = doctor.probe(
            lambda: (_ for _ in ()).throw(RuntimeError("sk-secret-in-message")),
            0.5)
        self.assertEqual(result.error, "error")
        self.assertNotIn("secret", str(result))

    def test_probe_timeout_returns_fast(self):
        started = time.monotonic()
        result = doctor.probe(lambda: time.sleep(0.6), 0.05)
        elapsed = time.monotonic() - started
        self.assertEqual(result.error, "timeout")
        self.assertLess(elapsed, 0.4)

    def test_probe_many_shares_one_deadline(self):
        tasks = [("fast", lambda: 1),
                 ("slow", lambda: time.sleep(0.7)),
                 ("boom", lambda: 1 / 0)]
        started = time.monotonic()
        results = doctor.probe_many(tasks, 0.15)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(results["fast"].value, 1)
        self.assertEqual(results["slow"].error, "timeout")
        self.assertEqual(results["boom"].error, "error")

    def test_network_down_produces_fail_lines_not_tracebacks(self):
        lines = doctor.check_network(hosts=(("fake", "invalid.invalid"),),
                                     timeout=0.05, download_timeout=0.05)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(item.status == "FAIL" for item in lines))
        self.assertTrue(any(" | fix: " in doctor.format_line(item)
                            for item in lines))

    def test_tiny_deadline_skips_heavy_checks(self):
        checks, _ = doctor.run_checks(config={}, deadline=time.monotonic())
        self.assertTrue(any("time budget" in item.detail for item in checks))
        self.assertTrue(all(item.status in ("OK", "WARN", "FAIL")
                            for item in checks))


if __name__ == "__main__":
    unittest.main()
