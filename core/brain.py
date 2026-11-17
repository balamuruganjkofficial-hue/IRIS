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

from core.config import CFG
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

_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()
_OR_LOCK = threading.Lock()
_OR_CACHE = {"at": 0.0, "models": []}


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

    try:
        import requests
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=4)
        response.raise_for_status()
        data = response.json().get("data") or []
    except Exception as e:  # noqa: BLE001 - discovery is best effort
        log.debug("OpenRouter free-model discovery failed: %s", e)
        return cached[:limit]

    ids = []
    for model in data:
        mid = str(model.get("id") or "")
        if not mid.endswith(":free"):
            continue                             # never a paid model
        if "tools" not in (model.get("supported_parameters") or []):
            continue
        try:
            if int(model.get("context_length") or 0) < 16000:
                continue
        except (TypeError, ValueError):
            pass
        ids.append(mid)

    preferred = ("google/", "meta-llama/", "qwen/", "mistralai/", "deepseek/",
                 "nvidia/", "inclusionai/")
    ids.sort(key=lambda mid: 0 if mid.startswith(preferred) else 1)
    models = ids[:limit]
    if models:
        with _OR_LOCK:
            _OR_CACHE.update({"at": now, "models": models})
    return models or cached[:limit]


def chain():
    """Ordered (provider, model) pairs. Only free endpoints, only with keys."""
    out, seen = [], set()

    def add(provider, model):
        provider = str(provider or "").strip().lower()
        model = str(model or "").strip()
        if not provider or not model or (provider, model) in seen:
            return
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

    # 3. OpenRouter's current free, tool-capable models.
    if bool(_M.get("openrouter_free", True)) and _key("openrouter"):
        for model in _openrouter_models(_M.get("openrouter_max", 4)):
            add("openrouter", model)

    # 4. Other free tiers, only when their key exists.
    add("gemini", _M.get("gemini_model", "gemini-2.5-flash"))
    add("cerebras", _M.get("cerebras_model", "llama-3.3-70b"))
    add("mistral", _M.get("mistral_model", "mistral-small-latest"))
    add("github", _M.get("github_model", "openai/gpt-4o-mini"))

    # 5. A local Ollama is free and needs no key, but is opt-in.
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
    """Open connections early and discover the current free OpenRouter models."""
    try:
        if _key("groq"):
            _client("groq").models.list()
    except Exception:  # noqa: BLE001 - warmup must never fail loudly
        pass
    try:
        if bool(_M.get("openrouter_free", True)) and _key("openrouter"):
            _openrouter_models(_M.get("openrouter_max", 4))
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

    for pass_no in range(2):
        waits = []
        other_failure = False
        for provider, model in models:
            if time.time() > deadline:
                log.warning("free-model time budget exhausted")
                break
            for attempt in range(2):
                spoke = False
                t0, first = time.time(), None
                try:
                    client = _client(provider)
                    text, buf, calls = "", "", {}
                    stream = client.chat.completions.create(
                        **_kwargs(provider, model, messages, tools, max_tokens))
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
                        return SimpleNamespace(content=text, tool_calls=tool_calls)
                    log.warning("%s/%s returned an empty reply", provider, model)
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
                        waits.append(_retry_seconds(e) or 10.0)
                        break                # this provider is limited; next model
                    other_failure = True
                    if attempt == 0 and _transient(e):
                        time.sleep(0.3)
                        continue
                    break

        if (pass_no == 0 and waits and not other_failure
                and min(waits) <= 15 and time.time() < deadline):
            wait = min(waits) + 0.5
            print("[brain] every free model is limited, waiting {0:.0f}s".format(wait))
            if on_sentence:
                on_sentence("One moment.")
            time.sleep(wait)
            continue
        break
    return None
