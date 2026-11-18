"""Voice pipeline: merging, prefetch gaps, barge-in and offline fallback."""

import threading
import time
import unittest
from unittest import mock

from core import bus, connectivity, voice


def _pipeline(synth, play, **kwargs):
    return voice.SpeechPipeline(synth, play, **kwargs).start()


class TestSentencePipeline(unittest.TestCase):
    def test_queued_sentences_merge_into_one_request(self):
        calls = []
        started = threading.Event()
        release = threading.Event()

        def synth(text):
            calls.append(text)
            if len(calls) == 1:
                started.set()
                release.wait(2)
            return "clip.mp3", True

        pipe = _pipeline(synth, lambda path, gen: None)
        try:
            pipe.speak("First.")
            self.assertTrue(started.wait(1))
            pipe.speak("Second.")
            pipe.speak("Third.")
            release.set()
            pipe.wait_done()
            self.assertEqual(calls, ["First.", "Second. Third."])
        finally:
            release.set()
            pipe.stop()

    def test_first_short_sentence_stays_on_the_fast_path(self):
        calls = []

        def synth(text):
            calls.append(text)
            return "clip.mp3", True

        pipe = _pipeline(synth, lambda path, gen: None)
        try:
            pipe.speak("Hi. This is the rest of the answer.")
            pipe.wait_done()
            self.assertEqual(calls, ["Hi.", "This is the rest of the answer."])
        finally:
            pipe.stop()

    def test_gap_between_consecutive_chunks_is_under_300ms(self):
        started = threading.Event()
        played, ended, synthesised = [], [], []

        def synth(text):
            result = "clip.mp3", True
            synthesised.append(time.perf_counter())
            return result

        def play(path, gen):
            played.append(time.perf_counter())
            if len(played) == 1:
                started.set()
            time.sleep(0.08)                 # simulated audio duration
            ended.append(time.perf_counter())

        pipe = _pipeline(synth, play)
        try:
            pipe.speak("First sentence.")
            self.assertTrue(started.wait(1))
            pipe.speak("Second sentence.")
            pipe.wait_done()
            stats = pipe.gap_stats()
            self.assertGreaterEqual(stats["count"], 1)
            self.assertLess(stats["last"], 0.3)
            self.assertGreaterEqual(len(played), 2)
            self.assertLess(synthesised[1], ended[0])  # prefetched during play
        finally:
            pipe.stop()

    def test_gap_meter_uses_the_injected_clock(self):
        now = [0.0]
        meter = voice.GapMeter(clock=lambda: now[0])
        self.assertIsNone(meter.start(1))
        meter.end(1)
        now[0] = 0.18
        self.assertAlmostEqual(meter.start(1), 0.18)
        self.assertEqual(meter.stats()["count"], 1)

    def test_spoken_text_is_normalised_before_synthesis(self):
        calls = []

        def synth(text):
            calls.append(text)
            return "clip.mp3", True

        pipe = _pipeline(synth, lambda path, gen: None)
        try:
            pipe.speak("The meeting is at 9:30 and 20% are online.")
            pipe.wait_done()
        finally:
            pipe.stop()
        self.assertEqual(
            calls, ["The meeting is at nine thirty and twenty percent are "
                    "online."])

    def test_a_line_with_nothing_speakable_is_dropped(self):
        calls = []
        pipe = _pipeline(lambda text: (calls.append(text),
                                       ("clip.mp3", True))[1],
                         lambda path, gen: None)
        try:
            pipe.speak(":)")
            pipe.wait_done()
        finally:
            pipe.stop()
        self.assertEqual(calls, [])


class TestStop(unittest.TestCase):
    def test_stop_is_fast_and_clears_task_accounting(self):
        playing = threading.Event()
        stopped = threading.Event()
        play_done = threading.Event()

        def synth(text):
            return "clip.mp3", True

        def play(path, gen):
            playing.set()
            stopped.wait(2)
            play_done.set()

        def stop_audio():
            stopped.set()

        pipe = _pipeline(synth, play, stop_audio=stop_audio)
        try:
            pipe.speak("One. Two. Three.")
            self.assertTrue(playing.wait(1))
            started = time.perf_counter()
            pipe.stop()
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.05)
            self.assertTrue(stopped.is_set())
            self.assertFalse(pipe.is_speaking())
            self.assertTrue(play_done.wait(1))
            deadline = time.time() + 1.0
            while pipe.pending_total() and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(pipe.pending_total(), 0)
        finally:
            stopped.set()
            pipe.stop()


    def test_stop_announces_idle(self):
        with mock.patch.object(bus, "publish") as publish:
            pipe = _pipeline(lambda text: ("clip.mp3", True),
                             lambda path, gen: None)
            try:
                pipe.stop()
            finally:
                pipe.stop()
        publish.assert_any_call("state", "idle")


class TestEdgeHealth(unittest.TestCase):
    """The pure state machine, driven by a fake clock."""

    def setUp(self):
        self.now = [1000.0]
        self.health = voice.EdgeHealth(clock=lambda: self.now[0],
                                       down_after=3, window=60.0,
                                       probe_interval=20.0)

    def test_one_failure_does_not_mark_edge_down(self):
        self.assertFalse(self.health.note_failure())
        self.assertFalse(self.health.is_down())

    def test_three_failures_inside_the_window_mark_edge_down(self):
        self.assertFalse(self.health.note_failure())
        self.now[0] += 5
        self.assertFalse(self.health.note_failure())
        self.now[0] += 5
        self.assertTrue(self.health.note_failure())
        self.assertTrue(self.health.is_down())

    def test_old_failures_expire_out_of_the_window(self):
        self.health.note_failure()
        self.now[0] += 61
        self.health.note_failure()
        self.now[0] += 1
        self.assertFalse(self.health.note_failure())
        self.assertFalse(self.health.is_down())

    def test_recovery_clears_the_outage_and_toasts_only_once(self):
        for _ in range(3):
            self.health.note_failure()
            self.now[0] += 1
        self.assertTrue(self.health.is_down())
        self.assertTrue(self.health.note_success())
        self.assertFalse(self.health.is_down())
        self.assertFalse(self.health.note_success())

    def test_probe_is_due_every_interval_while_down(self):
        for _ in range(3):
            self.health.note_failure()
        self.assertFalse(self.health.probe_due())
        self.now[0] += 20
        self.assertTrue(self.health.probe_due())
        self.health.probe_started()
        self.assertFalse(self.health.probe_due())
        self.now[0] += 20
        self.assertTrue(self.health.probe_due())

    def test_network_return_hurries_the_next_probe(self):
        for _ in range(3):
            self.health.note_failure()
        self.now[0] += 5
        self.assertFalse(self.health.probe_due())
        self.health.hurry_probe()
        self.assertTrue(self.health.probe_due())

    def test_a_recent_success_is_warm_and_a_stale_one_is_cold(self):
        self.assertTrue(self.health.is_cold())
        self.health.note_success()
        self.assertFalse(self.health.is_cold())
        self.now[0] += 301
        self.assertTrue(self.health.is_cold())


class TestOfflineFallback(unittest.TestCase):
    def tearDown(self):
        voice._reset_fallback_for_tests()
        connectivity.reset_for_tests(True)

    def _pipe(self, synth, offline, fallback=None):
        return _pipeline(
            synth, lambda path, gen: None, offline=offline,
            use_offline=voice.should_use_offline_voice,
            fallback=fallback if fallback is not None else voice._edge_failed)

    def test_edge_failure_speaks_through_the_offline_voice(self):
        offline = []
        fallback = []

        def synth(text):
            raise TimeoutError("edge too slow")

        def speak_offline(text, gen):
            offline.append(text)
            return True

        def failed(text, exc):
            fallback.append((text, exc))

        pipe = self._pipe(synth, speak_offline, failed)
        try:
            pipe.speak("Hello there.")
            pipe.wait_done()
            self.assertEqual(offline, ["Hello there."])
            self.assertEqual(len(fallback), 1)
            self.assertIsInstance(fallback[0][1], TimeoutError)
        finally:
            pipe.stop()

    def test_one_failed_sentence_still_tries_edge_for_the_next(self):
        voice._reset_fallback_for_tests()
        calls, offline = [], []

        def synth(text):
            calls.append(text)
            if len(calls) == 1:
                raise TimeoutError()
            return "clip.mp3", True

        pipe = self._pipe(synth, lambda text, gen: offline.append(text))
        try:
            pipe.speak("First sentence.")
            pipe.wait_done()
            pipe.speak("Second sentence.")
            pipe.wait_done()
        finally:
            pipe.stop()
        self.assertEqual(calls, ["First sentence.", "Second sentence."])
        self.assertEqual(offline, ["First sentence."])
        self.assertFalse(voice.should_use_offline_voice())

    def test_three_failed_sentences_switch_the_next_one_to_offline(self):
        voice._reset_fallback_for_tests()
        calls, offline = [], []

        def synth(text):
            calls.append(text)
            raise TimeoutError()

        pipe = self._pipe(synth, lambda text, gen: offline.append(text))
        try:
            for index in range(3):
                pipe.speak("Sentence {0}.".format(index))
                pipe.wait_done()
            pipe.speak("Fourth sentence.")
            pipe.wait_done()
        finally:
            pipe.stop()
        self.assertEqual(len(calls), 3)          # the fourth never touched Edge
        self.assertEqual(len(offline), 4)
        self.assertTrue(voice.should_use_offline_voice())

    def test_toast_fires_once_on_the_transition_not_per_failure(self):
        voice._reset_fallback_for_tests()
        with mock.patch.object(bus, "publish") as publish:
            for _ in range(5):
                voice._edge_failed("x", TimeoutError())
        toasts = [call for call in publish.call_args_list
                  if call.args and call.args[0] == "toast"]
        self.assertEqual(len(toasts), 1)
        self.assertTrue(voice.should_use_offline_voice())

    def test_recovery_clears_the_state_and_toasts_once(self):
        voice._reset_fallback_for_tests()
        for _ in range(3):
            voice._edge_failed("x", TimeoutError())
        self.assertTrue(voice.should_use_offline_voice())
        with mock.patch.object(bus, "publish") as publish:
            voice._edge_success()
            voice._edge_success()
        toasts = [call for call in publish.call_args_list
                  if call.args and call.args[0] == "toast"]
        self.assertEqual(len(toasts), 1)
        self.assertFalse(voice.should_use_offline_voice())

    def test_a_bad_request_never_counts_towards_an_outage(self):
        voice._reset_fallback_for_tests()
        for _ in range(5):
            voice._edge_failed("x", ValueError("bad rate"))
        self.assertFalse(voice.should_use_offline_voice())

    def test_edge_engine_never_uses_the_offline_voice(self):
        for _ in range(3):
            voice._edge_failed("x", TimeoutError())
        with mock.patch.object(voice, "ENGINE", "edge"):
            self.assertFalse(voice.should_use_offline_voice())

    def test_offline_engine_always_uses_the_offline_voice(self):
        voice._reset_fallback_for_tests()
        with mock.patch.object(voice, "ENGINE", "offline"):
            self.assertTrue(voice.should_use_offline_voice())

    def test_no_network_uses_the_offline_voice(self):
        voice._reset_fallback_for_tests()
        connectivity.reset_for_tests(False)
        self.assertTrue(voice.should_use_offline_voice())
        connectivity.reset_for_tests(True)
        self.assertFalse(voice.should_use_offline_voice())

    def test_edge_budgets_are_sane(self):
        self.assertGreaterEqual(voice.EDGE_CONNECT_TIMEOUT, 3.0)
        self.assertLessEqual(voice.EDGE_CONNECT_TIMEOUT, 10.0)
        self.assertGreaterEqual(voice.EDGE_FIRST_AUDIO_COLD,
                                voice.EDGE_FIRST_AUDIO_WARM)
        self.assertGreater(voice.EDGE_TOTAL_BUDGET,
                           voice.EDGE_FIRST_AUDIO_COLD)
        self.assertGreaterEqual(voice.EDGE_RETRIES, 1)
        self.assertGreaterEqual(voice.EDGE_DOWN_AFTER, 2)


class TestKokoroOptional(unittest.TestCase):
    def test_missing_files_or_package_fall_back_quietly(self):
        from core import kokoro
        with mock.patch.object(kokoro, "_ENGINE", None), \
                mock.patch.object(kokoro, "_FAILED", False), \
                mock.patch.object(kokoro, "available", return_value=False):
            self.assertFalse(kokoro.available())
            self.assertFalse(kokoro.speak("Hello there."))
            self.assertFalse(kokoro.speak(""))


class TestSapiVoice(unittest.TestCase):
    class Token:
        def __init__(self, description, gender="", language=""):
            self.description = description
            self.gender = gender
            self.language = language

        def GetDescription(self):
            return self.description

        def GetAttribute(self, name):
            if name == "Gender":
                return self.gender
            if name == "Language":
                return self.language
            return ""

    def test_prefers_the_american_female_voice(self):
        voices = [self.Token("Microsoft David Desktop - English (United States)",
                             "Male", "409"),
                  self.Token("Microsoft Zira Desktop - English (United States)",
                             "Female", "409"),
                  self.Token("Microsoft Hazel Desktop - English (Great Britain)",
                             "Female", "809")]
        chosen = voice._pick_sapi_voice(voices)
        self.assertEqual(chosen.GetDescription(),
                         "Microsoft Zira Desktop - English (United States)")

    def test_falls_back_to_any_american_female(self):
        voices = [self.Token("Microsoft David Desktop - English (United States)",
                             "Male", "409"),
                  self.Token("Some American Voice - English (United States)",
                             "Female", "409")]
        chosen = voice._pick_sapi_voice(voices)
        self.assertEqual(chosen.GetDescription(),
                         "Some American Voice - English (United States)")

    def test_never_chooses_a_male_voice_when_a_female_exists(self):
        voices = [self.Token("Microsoft David Desktop - English (United States)",
                             "Male", "409"),
                  self.Token("Microsoft Zira Desktop - English (United States)",
                             "Female", "409")]
        chosen = voice._pick_sapi_voice(voices, "Hazel")
        self.assertEqual(chosen.GetDescription(),
                         "Microsoft Zira Desktop - English (United States)")

    def test_ssml_matches_edge_rate_and_pitch_and_escapes_text(self):
        ssml = voice._sapi_ssml("Tea & biscuits", rate="-5%", pitch="+8Hz")
        self.assertIn('rate="-5%"', ssml)
        self.assertIn('pitch="+8Hz"', ssml)
        self.assertIn("Tea &amp; biscuits", ssml)
        self.assertIn('xml:lang="en-US"', ssml)

    def test_offline_voice_default_is_zira(self):
        self.assertEqual(str(voice.OFFLINE_VOICE_NAME).lower(), "zira")


if __name__ == "__main__":
    unittest.main()
