import json
import logging
import os
import re
import time
from types import SimpleNamespace

from groq import Groq

from core.config import CFG

log = logging.getLogger("iris")
_client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=15.0, max_retries=0)
MODELS = [CFG["models"]["llm"], CFG["models"]["fallback"]]
_SENT = re.compile(r"(?<=[.!?])\s+")
_SAY = re.compile(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"')


def warmup():
    """Opens the network connection early so the first real request is faster."""
    try:
        _client.models.list()
    except Exception:
        pass


def ask_stream(messages, tools=None, on_sentence=None, on_say=None,
               cancelled=lambda: False, max_tokens=600):
    """Streams the reply. Finished sentences go to on_sentence immediately, and the
    'say' line of each tool call goes to on_say as soon as the model has written it.
    Returns SimpleNamespace(content, tool_calls) or None."""
    for model in MODELS:
        for attempt in range(2):
            spoke = False
            t0, first = time.time(), None
            try:
                kwargs = dict(model=model, messages=messages, max_tokens=max_tokens,
                              temperature=0.5, stream=True,
                              extra_body={"reasoning_effort": "low"})
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                text, buf, calls = "", "", {}
                for chunk in _client.chat.completions.create(**kwargs):
                    if cancelled():
                        break
                    if not chunk.choices:
                        continue
                    d = chunk.choices[0].delta
                    if first is None and (d.content or d.tool_calls):
                        first = time.time() - t0
                    if d.content:
                        text += d.content
                        buf += d.content
                        parts = _SENT.split(buf)
                        if len(parts) > 1:
                            for s in parts[:-1]:
                                if s.strip() and on_sentence:
                                    on_sentence(s.strip())
                                    spoke = True
                            buf = parts[-1]
                    for tc in (d.tool_calls or []):
                        c = calls.setdefault(tc.index, {"id": "", "name": "", "args": "",
                                                        "said": False})
                        if tc.id:
                            c["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                c["name"] = tc.function.name
                            if tc.function.arguments:
                                c["args"] += tc.function.arguments
                        if on_say and not c["said"]:
                            m = _SAY.search(c["args"])
                            if m:
                                c["said"] = True
                                try:
                                    line = json.loads('"' + m.group(1) + '"')
                                except ValueError:
                                    line = m.group(1)
                                if line.strip():
                                    on_say(line.strip())
                                    spoke = True
                if buf.strip() and on_sentence and not cancelled():
                    on_sentence(buf.strip())
                    spoke = True
                tool_calls = [SimpleNamespace(
                    id=c["id"] or f"call_{i}",
                    function=SimpleNamespace(name=c["name"], arguments=c["args"] or "{}"))
                    for i, c in sorted(calls.items())]
                print(f"[llm] {model}: first output {first if first is not None else 0:.1f}s, "
                      f"total {time.time() - t0:.1f}s")
                if text.strip() or tool_calls or cancelled():
                    return SimpleNamespace(content=text, tool_calls=tool_calls)
                log.warning("model %s returned an empty reply", model)
                break
            except Exception as e:
                name = type(e).__name__
                log.warning("model %s failed (%s): %s", model, name, e)
                print(f"[brain] {model} failed ({name}): {e}")
                if spoke:
                    return None                     # already said something; do not start over
                if "RateLimit" in name:
                    break                           # switch model immediately, no waiting
                if attempt == 0 and ("Connection" in name or "Timeout" in name):
                    time.sleep(0.3)
                    continue
                break
    return None
