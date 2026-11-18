"""Bad-internet behaviour: DNS fallback, breakers, connectivity, offline mode."""

import asyncio
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from core import brain, bus, connectivity, dns, offline

HOST = "iris-test.invalid"


class _RateLimitError(Exception):
    status_code = 429


class _ModelLimitError(Exception):
    status_code = 429


class _DailyLimitError(Exception):
    status_code = 429


class _TooLargeError(Exception):
    status_code = 413


def _chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=text, tool_calls=None))])


class _Completions:
    def __init__(self, create):
        self.create = create


def _client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(create)))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class TestDNSFallback(unittest.TestCase):
    def setUp(self):
        dns.clear_cache()
        self.system = mock.Mock(side_effect=socket.gaierror(-2, "no such host"))
        self.doh = mock.Mock(return_value=["127.0.0.1"])
        dns.set_system_resolver(self.system)
        dns.set_resolver(self.doh)
        dns.install()

    def tearDown(self):
        dns.uninstall()
        dns.set_resolver(None)
        dns.set_system_resolver(socket.getaddrinfo)
        dns.clear_cache()

    def test_system_failure_uses_doh_then_caches(self):
        result = socket.getaddrinfo(HOST, 80)
        self.assertEqual(result[0][4], ("127.0.0.1", 80))
        self.assertEqual(dns.stats()["doh_ok"], 1)
        socket.getaddrinfo(HOST, 80)
        self.assertEqual(dns.stats()["doh_ok"], 1)      # no repeated lookups
        self.assertGreaterEqual(dns.stats()["cache_hit"], 1)

    def test_numeric_host_bypasses_dns_entirely(self):
        result = socket.getaddrinfo("127.0.0.1", 1234)
        self.assertEqual(result[0][4][0], "127.0.0.1")
        self.assertEqual(self.system.call_count, 0)
        self.assertEqual(self.doh.call_count, 0)

    def test_repeated_failure_is_cached_and_never_spams(self):
        dns.set_resolver(mock.Mock(side_effect=OSError("offline")))
        dns.clear_cache()
        for _ in range(4):
            with self.assertRaises(socket.gaierror):
                socket.getaddrinfo("dead.invalid", 443)
        self.assertEqual(dns.stats()["doh_fail"], 1)
        self.assertGreaterEqual(dns.stats()["cache_negative"], 3)

    def test_system_timeout_still_falls_back(self):
        slow = mock.Mock(side_effect=TimeoutError("hung DNS"))
        dns.set_system_resolver(slow)
        dns.clear_cache()
        result = socket.getaddrinfo(HOST, 443)
        self.assertEqual(result[0][4][0], "127.0.0.1")


class TestHTTPClients(unittest.TestCase):
    """The same socket patch must serve requests, httpx and aiohttp."""

    def setUp(self):
        real = dns._ORIGINAL
        self.original = real

        def system(host, port, family=0, type=0, proto=0, flags=0):
            if str(host) == HOST:
                raise socket.gaierror(-2, "no such host")
            return real(host, port, family, type, proto, flags)

        dns.clear_cache()
        dns.set_system_resolver(system)
        dns.set_resolver(lambda host, kind: ["127.0.0.1"])
        dns.install()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://{0}:{1}/".format(HOST, self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        dns.uninstall()
        dns.set_resolver(None)
        dns.set_system_resolver(self.original)
        dns.clear_cache()

    def test_requests_httpx_and_aiohttp(self):
        import requests
        session = requests.Session()
        session.trust_env = False
        self.assertEqual(session.get(self.url, timeout=3).text, "ok")
        session.close()

        import httpx
        with httpx.Client(trust_env=False, timeout=3) as client:
            self.assertEqual(client.get(self.url).text, "ok")

        async def fetch():
            import aiohttp
            async with aiohttp.ClientSession(trust_env=False) as session:
                async with session.get(self.url) as response:
                    return await response.text()

        self.assertEqual(asyncio.run(fetch()), "ok")


class TestBreakers(unittest.TestCase):
    def setUp(self):
        brain.reset_breakers()
        connectivity.reset_for_tests(True)

    def tearDown(self):
        brain.reset_breakers()
        connectivity.reset_for_tests(True)

    def _chain(self):
        return [provider for provider, _ in brain.chain()]

    def test_chain_skips_open_provider_then_recovers(self):
        config = dict(brain._M)
        config.update({"llm": "m1", "fallback": "m2", "openrouter_free": False})
        with mock.patch.object(brain, "_M", config), \
                mock.patch.object(brain, "_key", return_value="key"), \
                mock.patch.object(brain, "_groq_models", return_value=[]):
            self.assertIn("groq", self._chain())
            brain.breaker_fail("groq")
            brain.breaker_fail("groq")
            state = brain.breaker_state("groq")
            self.assertTrue(state["open"])
            self.assertGreater(state["remaining"], 0)
            self.assertNotIn("groq", self._chain())
            brain.breaker_ok("groq")
            self.assertIn("groq", self._chain())

    def test_backoff_doubles_after_a_failed_probe(self):
        brain.breaker_fail("groq")
        brain.breaker_fail("groq")
        first = brain.breaker_state("groq")["remaining"]
        brain._BREAKERS["groq"]["open_until"] = time.time() - 1
        brain.breaker_fail("groq")
        second = brain.breaker_state("groq")["remaining"]
        self.assertGreater(second, first + 30)

    def test_http_429_opens_breaker_and_does_not_spam(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs.get("model"))
            raise _RateLimitError("429")

        def current_chain():
            return [] if brain.breaker_trip("groq") else [("groq", "m1")]

        with mock.patch.object(brain, "chain", side_effect=current_chain), \
                mock.patch.object(brain, "_client", return_value=_client(create)), \
                mock.patch.object(brain.time, "sleep", return_value=None), \
                mock.patch.object(brain, "_retry_seconds", return_value=0.01):
            said = []
            brain.ask_stream([], on_sentence=said.append)
            self.assertTrue(brain.breaker_state("groq")["open"])
            used = len(calls)
            brain.ask_stream([], on_sentence=said.append)
            brain.ask_stream([], on_sentence=said.append)
            self.assertEqual(len(calls), used)              # dead provider skipped fast
            self.assertEqual(said.count("One moment."), 1)  # one calm notice only

    def test_timeout_then_recovery(self):
        calls = []

        def create(**kwargs):
            calls.append(1)
            if len(calls) <= 2:
                raise TimeoutError("network down")
            return [_chunk("Hello there.")]

        def current_chain():
            return [] if brain.breaker_trip("groq") else [("groq", "m1")]

        with mock.patch.object(brain, "chain", side_effect=current_chain), \
                mock.patch.object(brain, "_client", return_value=_client(create)), \
                mock.patch.object(brain.time, "sleep", return_value=None):
            self.assertIsNone(brain.ask_stream([]))
            self.assertTrue(brain.breaker_state("groq")["open"])
            brain._BREAKERS["groq"]["open_until"] = 0.0
            result = brain.ask_stream([])
            self.assertIsNotNone(result)
            self.assertFalse(brain.breaker_state("groq")["open"])


class TestFreeModelPool(unittest.TestCase):
    """A limited free model must not take the other free models down with it."""

    def setUp(self):
        brain.reset_breakers()
        connectivity.reset_for_tests(True)

    def tearDown(self):
        brain.reset_breakers()
        connectivity.reset_for_tests(True)
        with brain._GROQ_LOCK:
            brain._GROQ_CACHE.update({"at": 0.0, "models": []})

    def test_discovered_groq_models_join_the_chain(self):
        config = dict(brain._M)
        config.update({"llm": "m1", "fallback": "m2", "free_fallbacks": [],
                       "openrouter_free": False, "groq_free": True,
                       "groq_max": 3})
        with mock.patch.object(brain, "_M", config), \
                mock.patch.object(brain, "_key", return_value="key"), \
                mock.patch.object(brain, "_groq_models",
                                  return_value=["m3", "m4", "m5"]):
            groq = [model for provider, model in brain.chain()
                    if provider == "groq"]
        self.assertEqual(groq, ["m1", "m2", "m3", "m4", "m5"])

    def test_configured_groq_models_override_discovery(self):
        config = dict(brain._M)
        config.update({"groq_models": ["pinned/one", "pinned/two"],
                       "groq_max": 5})
        with mock.patch.object(brain, "_M", config), \
                mock.patch.object(brain, "_key", return_value="key"):
            self.assertEqual(brain._groq_models(5),
                             ["pinned/one", "pinned/two"])

    def test_model_limit_keeps_the_other_models(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs.get("model"))
            if kwargs.get("model") == "m1":
                raise _ModelLimitError(
                    "Rate limit reached for model `m1` on tokens per minute "
                    "(TPM). Please try again in 20s.")
            return [_chunk("Hello there.")]

        with mock.patch.object(brain, "chain",
                               return_value=[("groq", "m1"), ("groq", "m2")]), \
                mock.patch.object(brain, "_client", return_value=_client(create)), \
                mock.patch.object(brain.time, "sleep", return_value=None):
            result = brain.ask_stream([])
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["m1", "m2"])
        self.assertTrue(brain.model_cooling("groq", "m1"))
        self.assertFalse(brain.model_cooling("groq", "m2"))
        self.assertFalse(brain.breaker_state("groq")["open"])

    def test_cooled_model_is_skipped_by_the_chain(self):
        config = dict(brain._M)
        config.update({"llm": "m1", "fallback": "m2", "free_fallbacks": [],
                       "openrouter_free": False, "groq_free": False})
        brain.cool_model("groq", "m1", 30)
        with mock.patch.object(brain, "_M", config), \
                mock.patch.object(brain, "_key", return_value="key"):
            groq = [model for provider, model in brain.chain()
                    if provider == "groq"]
        self.assertNotIn("m1", groq)
        self.assertIn("m2", groq)

    def test_daily_cap_parks_the_whole_provider(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs.get("model"))
            if kwargs.get("model") == "a:free":
                raise _DailyLimitError(
                    "Rate limit exceeded: free-models-per-day. "
                    "X-RateLimit-Reset: 1794960000000")
            return [_chunk("Still here.")]

        with mock.patch.object(brain, "chain",
                               return_value=[("openrouter", "a:free"),
                                             ("groq", "m1")]), \
                mock.patch.object(brain, "_client", return_value=_client(create)), \
                mock.patch.object(brain.time, "sleep", return_value=None):
            result = brain.ask_stream([])
        self.assertIsNotNone(result)
        self.assertEqual(calls, ["a:free", "m1"])
        state = brain.breaker_state("openrouter")
        self.assertTrue(state["open"])
        self.assertGreater(state["remaining"], 60)
        self.assertFalse(brain.breaker_state("groq")["open"])

    def test_413_retries_with_fewer_tools(self):
        seen = []

        def create(**kwargs):
            seen.append(len(kwargs.get("tools") or []))
            if len(seen) == 1:
                raise _TooLargeError(
                    "413 Request too large for model `m1` in organization "
                    "on input tokens per minute (ITPM).")
            return [_chunk("Done.")]

        tools = [{"type": "function", "function": {
            "name": "tool_%d" % i, "description": "d" * 400,
            "parameters": {"type": "object", "properties": {}}}}
            for i in range(50)]
        with mock.patch.object(brain, "chain", return_value=[("groq", "m1")]), \
                mock.patch.object(brain, "_client", return_value=_client(create)), \
                mock.patch.object(brain.time, "sleep", return_value=None):
            result = brain.ask_stream([{"role": "user", "content": "hi"}],
                                      tools=tools)
        self.assertIsNotNone(result)
        self.assertEqual(len(seen), 2)
        self.assertLess(seen[1], seen[0])
        self.assertFalse(brain.breaker_state("groq")["open"])


class TestConnectivity(unittest.TestCase):
    def test_publishes_once_per_change(self):
        events = []
        off = bus.subscribe("connectivity", events.append)
        try:
            connectivity.reset_for_tests(True)
            self.assertTrue(connectivity.set_online(False))     # real change
            self.assertEqual(len(events), 1)
            self.assertFalse(connectivity.set_online(False))    # no repeat
            self.assertEqual(len(events), 1)
            self.assertTrue(connectivity.set_online(True))
            self.assertEqual(events[-1], {"online": True})
            self.assertEqual(len(events), 2)
        finally:
            off()
            connectivity.reset_for_tests(True)

    def test_probe_is_bounded_and_monitor_is_a_daemon(self):
        seen = []

        def fake_create(address, timeout=None):
            seen.append((address, timeout))
            raise OSError("offline")

        with mock.patch.object(connectivity.socket, "create_connection",
                               side_effect=fake_create):
            self.assertFalse(connectivity.probe([("fake.invalid", 443)], timeout=0.25))
        self.assertEqual(seen[0][1], 0.25)

        thread = connectivity.start()
        try:
            self.assertTrue(thread.daemon)
            self.assertIsNot(thread, threading.main_thread())
        finally:
            connectivity.stop()


class TestOfflineMode(unittest.TestCase):
    def tearDown(self):
        brain.reset_breakers()
        connectivity.reset_for_tests(True)

    def test_offline_chain_uses_only_configured_ollama(self):
        connectivity.reset_for_tests(False)
        config = {"ollama_models": ["llama3.2"], "llm": "remote-model"}
        with mock.patch.object(brain, "_M", config), \
                mock.patch.object(brain, "_key", return_value="key"):
            self.assertEqual(brain.chain(), [("ollama", "llama3.2")])
            self.assertTrue(brain.has_local_model())

    def test_offline_router_handles_local_commands_only(self):
        with mock.patch.object(offline, "run_tool", return_value=(False, "done")) as run:
            self.assertEqual(offline.route("volume up"), "done")
            self.assertEqual(run.call_args[0][0], "volume")
            self.assertIsNone(offline.route("search the web for cats"))
        self.assertIn("web_search", offline.ONLINE_ONLY)

    def test_offline_voice_helper_follows_connectivity(self):
        connectivity.reset_for_tests(False)
        import importlib
        voice = importlib.import_module("core.voice")
        self.assertTrue(voice.should_use_offline_voice())
        connectivity.reset_for_tests(True)
        self.assertFalse(voice.should_use_offline_voice())


if __name__ == "__main__":
    unittest.main()
