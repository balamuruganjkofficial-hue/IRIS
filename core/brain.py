"""The language model: a chain of free models across free providers.

Groq's free tier is the primary. If a model is rate limited, out of quota,
down, or returns an empty reply, Iris automatically moves to the next free
model: OpenRouter's ``:free`` models (discovered live), Google Gemini's free
tier, Cerebras, Mistral, GitHub Models, or a local Ollama. Only providers whose
key is present are used, and OpenRouter is restricted to ``:free`` model IDs.
No paid model is ever contacted.
"""

import json
import logging
import os
import re
import threading
import time
from types import SimpleNamespace

from openai import OpenAI

from core import connectivity
from core.config import CFG
from core.skill import select_tools
from core.text import SENT_RE

log = logging.getLogger("iris")
_B = CFG.get("brain") or {}
_M = CFG.get("models") or {}

_RETRY = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.I)
_SAY = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"')

# All of these speak the OpenAI chat-completions API, so one client code path
# works for every provider. A provider is skipped unless its key is present.
_PROVIDERS = {
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "env": ("GROQ_API_KEY",),
    },
    "openrouter": {
        "base": "https://openrouter.ai/api/v1",
        "env": ("OPENROUTER_API_KEY",),
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    },
    "cerebras": {
        "base": "https://api.cerebras.ai/v1",
        "env": ("CEREBRAS_API_KEY",),
    },
    "mistral": {
        "base": "https://api.mistral.ai/v1",
        "env": ("MISTRAL_API_KEY",),
    },
    "github": {
        "base": "https://models.github.ai/inference",
        "env": ("GITHUB_TOKEN",),
    },
    "ollama": {
        "base": "http://127.0.0.1:11434/v1",
        "env": (),
        "timeout": 90.0,
    },
}

_BAD_KEYS = {"", "none", "null", "changeme", "your_key_here",
             "your_api_key_here", "sk-..."}

# Discovery ranking. Bigger, newer general models make better assistants than
# tiny or code-only ones, and some families are consistently strong at tools.
_STRONG_HINTS = ("ultra", "super", "pro", "max", "large", "reason", "thinking",
                 "r1", "instruct", "70b", "120b", "235b", "550b", "27b", "31b",
                 "32b")
_WEAK_HINTS = ("nano", "tiny", "small", "1.7b", "2b", "3b", "4b", "7b", "8b",
               "flash", "preview", "guard", "code", "embed")
_FAMILY_BONUS = {"google/": 6, "nvidia/": 6, "deepseek/": 5,
                 "inclusionai/": 5, "thinkingmachines/": 5, "baidu/": 4,
                 "meta-llama/": 4, "qwen/": 4, "mistralai/": 3, "cohere/": 2}

_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()
_OR_LOCK = threading.Lock()
_OR_CACHE = {"at": 0.0, "models": []}
_GROQ_LOCK = threading.Lock()
_GROQ_CACHE = {"at": 0.0, "models": []}

# Groq exposes speech, guard and embedding models on the same endpoint. Only
# chat models can answer a conversation, so everything else is filtered out.
_GROQ_SKIP = ("whisper", "orpheus", "guard", "safeguard", "tts", "embed",
              "rerank", "moderation", "audio", "vision")

# Per-model cooldown. Groq limits tokens per minute per model, so one model
# being limited must not disable the other free models on the same provider.
_MODEL_UNTIL = {}
_MODEL_LOCK = threading.Lock()

# Per-provider circuit breaker. Two failures inside BREAKER_WINDOW open the
# circuit for 60 s; each later trip doubles that, capped at 15 min. Open
# providers are skipped without creating clients, so a dead endpoint costs
# almost nothing while the other free models still answer.
BREAKER_FAILURES = 2
BREAKER_WINDOW = 90.0
BREAKER_BASE = 60.0
BREAKER_MAX = 900.0
_BREAKERS = {}
_BREAKER_LOCK = threading.RLock()


def breaker_state(provider):
    """Read-only snapshot, including whether the circuit is open right now."""
    with _BREAKER_LOCK:
        item = _BREAKERS.get(provider) or {}
        remaining = max(0.0, float(item.get("open_until", 0.0)) - time.time())
        return {"level": int(item.get("level", 0)), "remaining": remaining,
                "open": remaining > 0.0,
                "failures": len(item.get("fails", []))}


def breaker_trip(provider):
    """True while this provider should be skipped."""
    with _BREAKER_LOCK:
        item = _BREAKERS.get(provider)
        return bool(item and float(item.get("open_until", 0.0)) > time.time())


def breaker_fail(provider):
    """Record a failure and open (or back off) the circuit when warranted."""
    now = time.time()
    with _BREAKER_LOCK:
        item = _BREAKERS.setdefault(
            provider, {"fails": [], "open_until": 0.0, "level": 0})
        expired = 0.0 < float(item["open_until"]) <= now
        item["fails"] = [stamp for stamp in item["fails"]
                         if now - stamp <= BREAKER_WINDOW]
        item["fails"].append(now)
        if expired or len(item["fails"]) >= BREAKER_FAILURES:
            item["level"] += 1
            wait = min(BREAKER_MAX, BREAKER_BASE * (2 ** (item["level"] - 1)))
            item["open_until"] = now + wait
            item["fails"] = []
        return breaker_state(provider)


def breaker_ok(provider):
    """A successful call clears that provider's failure history."""
    with _BREAKER_LOCK:
        _BREAKERS.pop(provider, None)


def breaker_hold(provider, seconds):
    """Skip a provider for a known period, e.g. a daily free-tier reset."""
    now = time.time()
    with _BREAKER_LOCK:
        item = _BREAKERS.setdefault(
            provider, {"fails": [], "open_until": 0.0, "level": 0})
        item["open_until"] = max(float(item.get("open_until", 0.0)),
                                 now + max(1.0, float(seconds or 1.0)))
        item["fails"] = []
        return breaker_state(provider)


def model_cooling(provider, model):
    """True while this one model is rate limited and should be skipped."""
    with _MODEL_LOCK:
        return _MODEL_UNTIL.get((provider, model), 0.0) > time.time()


def cool_model(provider, model, seconds):
    """Park one limited model without touching the rest of its provider."""
    until = time.time() + max(1.0, float(seconds or 1.0))
    with _MODEL_LOCK:
        key = (provider, model)
        _MODEL_UNTIL[key] = max(_MODEL_UNTIL.get(key, 0.0), until)
    return _MODEL_UNTIL[(provider, model)]


def reset_breakers():
    with _BREAKER_LOCK:
        _BREAKERS.clear()
    with _MODEL_LOCK:
        _MODEL_UNTIL.clear()


def has_local_model():
    """True when an opt-in Ollama model is configured for offline use."""
    return any(str(model).strip() for model in (_M.get("ollama_models") or []))


# ------------------------------------------------------------------ providers
def _env(*names):
    """First real key among the given environment variables."""
    for name in names:
        value = (os.getenv(name) or "").strip().strip('"').strip("'")
        if value and value.lower() not in _BAD_KEYS and not value.startswith("your_"):
            return value
    return ""


def _key(provider):
    if provider == "ollama":
        return "ollama"                     # local, no key needed
    meta = _PROVIDERS.get(provider)
    return _env(*(meta["env"] if meta else ()))


def _client(provider):
    with _CLIENT_LOCK:
        if provider in _CLIENTS:
            return _CLIENTS[provider]
        meta = _PROVIDERS[provider]
        kwargs = dict(api_key=_key(provider) or "free", base_url=meta["base"],
                      timeout=float(meta.get("timeout", _B.get("request_timeout", 20))),
                      max_retries=0)
        if provider == "openrouter":
            kwargs["default_headers"] = {"HTTP-Referer": "https://localhost/iris",
                                         "X-Title": "Iris"}
        client = OpenAI(**kwargs)
        _CLIENTS[provider] = client
        return client


def _model_rank(mid, context_length=0):
    """Higher is better for a general-purpose voice assistant."""
    text = str(mid or "").lower()
    score = 0
    for hint in _STRONG_HINTS:
        if hint in text:
            score += 3
    for hint in _WEAK_HINTS:
        if hint in text:
            score -= 2
    for prefix, bonus in _FAMILY_BONUS.items():
        if text.startswith(prefix):
            score += bonus
    try:
        context = int(context_length or 0)
    except (TypeError, ValueError):
        context = 0
    if context >= 200000:
        score += 2
    elif 0 < context < 16000:
        score -= 4
    return score


def _openrouter_models(limit=4):
    """Live list of OpenRouter models that are both free and tool-capable.

    The list changes often, so it is discovered from the public models
    endpoint and cached for an hour. Paid IDs are filtered out by the
    ``:free`` suffix. A static ``models.openrouter_models`` list overrides it.
    """
    limit = max(1, int(limit or 4))
    configured = [str(m).strip() for m in (_M.get("openrouter_models") or [])
                  if str(m).strip()]
    if configured:
        return [m for m in configured if m.endswith(":free")][:limit]

    now = time.time()
    with _OR_LOCK:
        cached = list(_OR_CACHE["models"])
        fresh = now - float(_OR_CACHE["at"]) < 3600
    if cached and fresh:
        return cached[:limit]
    if not connectivity.is_online():
        return cached[:limit]          # no network, no discovery call

    try:
        import requests
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=4)
        response.raise_for_status()
        data = response.json().get("data") or []
    except Exception as e:  # noqa: BLE001 - discovery is best effort
        log.debug("OpenRouter free-model discovery failed: %s", e)
        return cached[:limit]

    ids, contexts = [], {}
    for model in data:
        mid = str(model.get("id") or "")
        if not mid.endswith(":free"):
            continue                             # never a paid model
        if "tools" not in (model.get("supported_parameters") or []):
            continue
        try:
            context = int(model.get("context_length") or 0)
        except (TypeError, ValueError):
            context = 0
        if context and context < 16000:
            continue
        ids.append(mid)
        contexts[mid] = context

    ids.sort(key=lambda mid: (-_model_rank(mid, contexts.get(mid, 0)), mid))
    models = ids[:limit]
    if models:
        with _OR_LOCK:
            _OR_CACHE.update({"at": now, "models": models})
    return models or cached[:limit]


def _groq_models(limit=6):
    """Live list of Groq's free chat models, so a new one is used automatically.

    Groq rate limits are per model, not per account, so trying every chat model
    is the cheapest way to survive a busy minute. The list is discovered from
    the models endpoint and cached for an hour. A static
    ``models.groq_models`` list overrides discovery.
    """
    limit = max(1, int(limit or 6))
    configured = [str(m).strip() for m in (_M.get("groq_models") or [])
                  if str(m).strip()]
    if configured:
        return configured[:limit]

    now = time.time()
    with _GROQ_LOCK:
        cached = list(_GROQ_CACHE["models"])
        fresh = now - float(_GROQ_CACHE["at"]) < 3600
    if cached and fresh:
        return cached[:limit]
    if not connectivity.is_online():
        return cached[:limit]

    key = _key("groq")
    if not key:
        return []
    try:
        import requests
        response = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": "Bearer " + key}, timeout=4)
        response.raise_for_status()
        data = response.json().get("data") or []
    except Exception as e:  # noqa: BLE001 - discovery is best effort
        log.debug("Groq free-model discovery failed: %s", e)
        return cached[:limit]

    ids, contexts = [], {}
    for model in data:
        mid = str(model.get("id") or "")
        low = mid.lower()
        if not mid or any(skip in low for skip in _GROQ_SKIP):
            continue
        try:
            size = int(model.get("context_window") or 0)
        except (TypeError, ValueError):
            size = 0
        ids.append(mid)
        contexts[mid] = size

    ids.sort(key=lambda mid: (-_model_rank(mid, contexts.get(mid, 0)), mid))
    models = ids[:limit]
    if models:
        with _GROQ_LOCK:
            _GROQ_CACHE.update({"at": now, "models": models})
    return models or cached[:limit]


def chain():
    """Ordered (provider, model) pairs. Only free endpoints, only with keys."""
    out, seen = [], set()
    offline = not connectivity.is_online()

    def add(provider, model):
        provider = str(provider or "").strip().lower()
        model = str(model or "").strip()
        if not provider or not model or (provider, model) in seen:
            return
        if offline and provider != "ollama":
            return                          # offline: local model or nothing
        if breaker_trip(provider):
            return                          # circuit open: skip, do not connect
        if model_cooling(provider, model):
            return                          # this model is limited right now
        if provider not in _PROVIDERS:
            log.warning("unknown model provider %r, skipping", provider)
            return
        if provider == "openrouter" and not model.endswith(":free"):
            log.warning("refusing non-free OpenRouter model %r", model)
            return
        if not _key(provider):
            return
        seen.add((provider, model))
        out.append((provider, model))

    # 1. The configured free Groq models.
    add("groq", _M.get("llm"))
    add("groq", _M.get("fallback"))

    # 2. Extra free models from config, e.g.
    #    - {provider: gemini, model: gemini-2.5-flash}
    for entry in _M.get("free_fallbacks") or []:
        if isinstance(entry, dict):
            add(entry.get("provider", "groq"), entry.get("model"))
        elif isinstance(entry, str):
            add("groq", entry)

    # 3. Every other free Groq chat model. Groq limits per model, so this is
    #    the fastest fallback when one model's tokens-per-minute runs out.
    if (not offline and bool(_M.get("groq_free", True)) and _key("groq")):
        for model in _groq_models(_M.get("groq_max", 6)):
            add("groq", model)

    # 4. OpenRouter's current free, tool-capable models.
    if (not offline and bool(_M.get("openrouter_free", True))
            and _key("openrouter")):
        for model in _openrouter_models(_M.get("openrouter_max", 10)):
            add("openrouter", model)

    # 5. Other free tiers, only when their key exists.
    add("gemini", _M.get("gemini_model", "gemini-2.5-flash"))
    add("cerebras", _M.get("cerebras_model", "llama-3.3-70b"))
    add("mistral", _M.get("mistral_model", "mistral-small-latest"))
    add("github", _M.get("github_model", "openai/gpt-4o-mini"))

    # 6. A local Ollama is free and needs no key, but is opt-in.
    for model in _M.get("ollama_models") or []:
        add("ollama", model)
    return out


# --------------------------------------------------------------------- errors
def _status(err):
    """HTTP status from an SDK exception, or None."""
    for obj in (err, getattr(err, "response", None)):
        code = getattr(obj, "status_code", None)
        if code is not None:
            try:
                return int(code)
            except (TypeError, ValueError):
                pass
    match = re.search(r"\b(400|401|402|403|404|408|409|413|422|429|500|502|503|504)\b",
                      str(err))
    return int(match.group(1)) if match else None


def _is_rate(err):
    text = str(err).lower()
    return ("RateLimit" in type(err).__name__ or _status(err) in (402, 429)
            or "rate limit" in text or "quota" in text or "too many requests" in text)


def _retry_seconds(err):
    match = _RETRY.search(str(err))
    if not match:
        return None
    return int(match.group(1) or 0) * 60 + float(match.group(2))


def _transient(err):
    name = type(err).__name__
    return (_status(err) in (408, 500, 502, 503, 504)
            or "Timeout" in name or "Connection" in name or "APIError" in name)


_DAILY_RE = re.compile(r"free-models-per-day|per[- ]day|daily", re.I)
_MODEL_RATE_RE = re.compile(
    r"for model|tokens per minute|\bitpm\b|\btpm\b|requests per minute|\brpm\b",
    re.I)
_RESET_RE = re.compile(r"X-RateLimit-Reset'?\s*:\s*'?(\d{9,})")


def _rate_scope(err):
    """'account' (whole provider is out), 'model' (one model) or 'provider'."""
    text = str(err)
    if _DAILY_RE.search(text):
        return "account"
    if _MODEL_RATE_RE.search(text):
        return "model"
    return "provider"


def _rate_wait(err):
    """How long to park the thing that hit the limit, in seconds."""
    now = time.time()
    retry = _retry_seconds(err)
    match = _RESET_RE.search(str(err))
    reset = int(match.group(1)) / 1000.0 if match else 0.0
    reset_wait = reset - now if reset > now else 0.0
    scope = _rate_scope(err)
    if scope == "account":
        return min(86400.0, max(60.0, reset_wait or 3600.0))
    if scope == "model":
        return max(5.0, min(120.0, retry or 20.0))
    return max(1.0, min(120.0, retry or 10.0))


def _is_model_error(err):
    """A bad model or an oversized request; skip it without killing the provider."""
    return _status(err) in (400, 404, 413, 422)


def _last_user_text(messages):
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _tool_budget():
    try:
        return max(0, int(_B.get("groq_tool_budget", 4000)))
    except (TypeError, ValueError):
        return 4000


def _tools_for(provider, tools, query):
    """Fit the tool list into a small model's token limit, when needed."""
    if not tools or provider != "groq":
        return tools
    budget = _tool_budget()
    return select_tools(tools, budget, query) if budget else tools


# ------------------------------------------------------------------- requests
def _kwargs(provider, model, messages, tools, max_tokens):
    kwargs = dict(model=model, messages=messages, max_tokens=max_tokens,
                  temperature=float(_B.get("temperature", 0.5)), stream=True)
    if tools:
        kwargs["tools"] = tools
    if provider == "groq" and "gpt-oss" in model.lower():
        kwargs["extra_body"] = {"reasoning_effort": "low"}
    return kwargs


def warmup():
    """Open connections early and discover the current free model lists."""
    if not connectivity.is_online():
        return
    try:
        if _key("groq"):
            _client("groq").models.list()
    except Exception:  # noqa: BLE001 - warmup must never fail loudly
        pass
    try:
        if bool(_M.get("groq_free", True)) and _key("groq"):
            _groq_models(_M.get("groq_max", 6))
    except Exception:  # noqa: BLE001
        pass
    try:
        if bool(_M.get("openrouter_free", True)) and _key("openrouter"):
            _openrouter_models(_M.get("openrouter_max", 10))
    except Exception:  # noqa: BLE001
        pass


def ask_stream(messages, tools=None, on_sentence=None, on_say=None,
               cancelled=lambda: False, max_tokens=None):
    """Stream one reply, walking the free-model chain until one answers.

    Finished sentences go to ``on_sentence`` immediately and a tool call's
    ``say`` line goes to ``on_say`` as soon as it is written. If a model has
    already spoken before failing, the chain stops (never restarts a reply
    mid-sentence). If every model is rate limited, wait briefly once and retry.
    Returns SimpleNamespace(content, tool_calls) or None.
    """
    max_tokens = max_tokens or _B.get("max_tokens", 400)
    deadline = time.time() + float(_B.get("fallback_budget", 50))
    models = chain()
    if not models:
        log.error("no free language model is configured or has a key")
        return None
    log.debug("free model chain: %s", " -> ".join(
        "{0}/{1}".format(p, m) for p, m in models))
    waited_once = False
    query = _last_user_text(messages)

    for pass_no in range(2):
        waits = []
        other_failure = False
        for provider, model in models:
            if breaker_trip(provider):
                continue                    # opened mid-chain: skip fast
            if model_cooling(provider, model):
                continue                    # this model is limited right now
            if time.time() > deadline:
                log.warning("free-model time budget exhausted")
                break
            current_tools = _tools_for(provider, tools, query)
            for attempt in range(3):
                spoke = False
                t0, first = time.time(), None
                try:
                    client = _client(provider)
                    text, buf, calls = "", "", {}
                    stream = client.chat.completions.create(
                        **_kwargs(provider, model, messages, current_tools,
                                  max_tokens))
                    for chunk in stream:
                        if cancelled():
                            break
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        if first is None and (delta.content or delta.tool_calls):
                            first = time.time() - t0
                        if delta.content:
                            text += delta.content
                            buf += delta.content
                            parts = SENT_RE.split(buf)
                            if len(parts) > 1:
                                for sentence in parts[:-1]:
                                    if sentence.strip() and on_sentence:
                                        on_sentence(sentence.strip())
                                        spoke = True
                                buf = parts[-1]
                        for call in (delta.tool_calls or []):
                            item = calls.setdefault(
                                call.index, {"id": "", "name": "", "args": "",
                                             "said": False})
                            if call.id:
                                item["id"] = call.id
                            if call.function:
                                if call.function.name:
                                    item["name"] = call.function.name
                                if call.function.arguments:
                                    item["args"] += call.function.arguments
                            if on_say and not item["said"]:
                                match = _SAY.search(item["args"])
                                if match:
                                    item["said"] = True
                                    try:
                                        line = json.loads('"' + match.group(1) + '"')
                                    except ValueError:
                                        line = match.group(1)
                                    if line.strip():
                                        on_say(line.strip())
                                        spoke = True
                    if buf.strip() and on_sentence and not cancelled():
                        on_sentence(buf.strip())
                        spoke = True
                    tool_calls = [SimpleNamespace(
                        id=item["id"] or "call_{0}".format(index),
                        function=SimpleNamespace(name=item["name"],
                                                 arguments=item["args"] or "{}"))
                        for index, item in sorted(calls.items())]
                    log.info("llm %s/%s: first %.1fs total %.1fs",
                             provider, model,
                             first if first is not None else 0, time.time() - t0)
                    if text.strip() or tool_calls or cancelled():
                        breaker_ok(provider)
                        return SimpleNamespace(content=text, tool_calls=tool_calls)
                    log.warning("%s/%s returned an empty reply", provider, model)
                    cool_model(provider, model, 30)
                    other_failure = True
                    break
                except Exception as e:  # noqa: BLE001 - try the next free model
                    name = type(e).__name__
                    log.warning("model %s/%s failed (%s): %s",
                                provider, model, name, e)
                    print("[brain] {0}/{1} failed ({2}): {3}".format(
                        provider, model, name, str(e)[:160]))
                    if spoke:
                        return None          # already said something; do not restart
                    if _is_rate(e):
                        scope = _rate_scope(e)
                        wait = _rate_wait(e)
                        if scope == "account":
                            # The whole provider is out (e.g. OpenRouter's
                            # free-models-per-day cap): skip it until reset.
                            breaker_hold(provider, wait)
                        elif scope == "model":
                            # One model's tokens-per-minute ran out; the other
                            # models on the same provider are still fine.
                            cool_model(provider, model, wait)
                            waits.append(wait)
                        else:
                            breaker_fail(provider)
                            waits.append(wait)
                        break                # next free model
                    if _is_model_error(e):
                        # A model that cannot take this request (bad model or
                        # HTTP 413 "too large"). Shrink the tool list and retry
                        # once, then skip this model without hurting the provider.
                        if _status(e) == 413 and current_tools:
                            smaller = select_tools(
                                current_tools, max(1200, _tool_budget() // 2),
                                query)
                            if len(smaller) < len(current_tools):
                                current_tools = smaller
                                continue
                        break
                    breaker_fail(provider)
                    other_failure = True
                    if attempt == 0 and _transient(e):
                        time.sleep(0.3)
                        continue
                    break

        if (pass_no == 0 and not waited_once and waits and not other_failure
                and min(waits) <= 15 and time.time() < deadline):
            wait = min(waits) + 0.5
            waited_once = True
            print("[brain] every free model is limited, waiting {0:.0f}s".format(wait))
            if on_sentence:
                on_sentence("One moment.")
            time.sleep(wait)
            continue
        break
    return None
