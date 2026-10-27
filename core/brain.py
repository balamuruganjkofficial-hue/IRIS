import logging
import os
import re
from types import SimpleNamespace

from groq import Groq

from core.config import CFG

log = logging.getLogger("iris")
_client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=15.0, max_retries=1)
MODELS = [CFG["models"]["llm"], CFG["models"]["fallback"]]
_SENT = re.compile(r"(?<=[.!?])\s+")


def ask_stream(messages, tools=None, on_sentence=None, cancelled=lambda: False,
               max_tokens=600):
    """Streams the reply. Each finished sentence is passed to on_sentence immediately.
    Returns SimpleNamespace(content, tool_calls) or None."""
    for model in MODELS:
        spoke = False
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
                    c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        c["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            c["name"] = tc.function.name
                        if tc.function.arguments:
                            c["args"] += tc.function.arguments
            if buf.strip() and on_sentence and not cancelled():
                on_sentence(buf.strip())
                spoke = True
            tool_calls = [SimpleNamespace(id=c["id"] or f"call_{i}",
                                          function=SimpleNamespace(name=c["name"],
                                                                   arguments=c["args"] or "{}"))
                          for i, c in sorted(calls.items())]
            if text.strip() or tool_calls or cancelled():
                return SimpleNamespace(content=text, tool_calls=tool_calls)
            log.warning("model %s returned an empty reply", model)
        except Exception as e:
            log.warning("model %s failed: %s", model, e)
            print(f"[brain] {model} failed: {e}")
            if spoke:
                break                       # already said something, do not start over
    return None