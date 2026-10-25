import logging
import os
from groq import Groq
from core.config import CFG

log = logging.getLogger("iris")
_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
MODELS = [CFG["models"]["llm"], CFG["models"]["fallback"]]


def ask(messages, tools=None, max_tokens=700):
    """Returns the model's message (has .content and .tool_calls), or None."""
    for model in MODELS:
        try:
            kwargs = dict(model=model, messages=messages, max_tokens=max_tokens,
                          temperature=0.4,
                          extra_body={"reasoning_effort": "low"})
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            msg = _client.chat.completions.create(**kwargs).choices[0].message
            if (msg.content or "").strip() or msg.tool_calls:
                return msg
            log.warning("model %s returned an empty reply", model)
        except Exception as e:
            log.warning("model %s failed: %s", model, e)
            print(f"[brain] {model} failed: {e}")
    return None