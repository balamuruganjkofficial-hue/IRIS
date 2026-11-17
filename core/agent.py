"""The agent loop: memory + prompt + tool calls, one streamed reply at a time."""

import json
import logging
import random
import threading
import time

from core import brain, bus, memory, voice
from core.config import CFG
from core.persona import build_prompt
from core.skill import run_tool, tool_schemas
import skills.basic            # noqa: F401
import skills.hands            # noqa: F401
import skills.system           # noqa: F401
import skills.laptop           # noqa: F401
import skills.websearch        # noqa: F401
import skills.browser_agent    # noqa: F401
import skills.surface          # noqa: F401

log = logging.getLogger("iris")
_B = CFG["brain"]
ACKS = ["On it.", "Say less.", "Fine, hang on.", "Already moving.", "Yeah, yeah.",
        "Working on it."]                       # last resort only
cancel = threading.Event()                      # set when the user interrupts
voice.prewarm(ACKS)
threading.Thread(target=brain.warmup, daemon=True, name="llm-warmup").start()


def system_prompt(facts):
    return build_prompt(facts)


def _trim_pages(messages):
    """Keep only the newest browser page snapshot to save time and tokens."""
    seen = False
    for m in reversed(messages):
        if m.get("role") == "tool" and str(m.get("content", "")).startswith("Page:"):
            if seen:
                m["content"] = "(older page state omitted)"
            seen = True


def run(user_text, confirm):
    cancel.clear()
    t0 = time.time()
    bus.publish("state", "thinking")
    facts = memory.search_facts(user_text, CFG["memory"]["recalled_facts"])
    messages = [{"role": "system", "content": system_prompt(facts)}]
    messages += memory.recent(CFG["memory"]["recent_turns"])
    messages.append({"role": "user", "content": user_text})
    memory.add_turn("user", user_text)

    spoken = []
    first = []

    def say(sentence):
        if not first:
            first.append(time.time() - t0)
            bus.publish("latency", first[0])
            print("[t] first words after {0:.1f}s".format(first[0]))
        voice.speak(sentence)
        spoken.append(sentence)
        bus.publish("say", sentence)

    tools_all, tools_browser = tool_schemas(), tool_schemas("browser_")
    browser_mode = False        # once she starts browsing, send only the browser tools
    last_result = ""
    max_calls = int(_B.get("max_tool_calls", 3))
    for _ in range(int(_B.get("max_steps", 10))):
        if cancel.is_set():
            break
        tools = tools_browser if browser_mode else tools_all
        _trim_pages(messages)
        before = len(spoken)
        msg = brain.ask_stream(messages, tools, on_sentence=say, on_say=say,
                               cancelled=cancel.is_set)
        if cancel.is_set():
            break
        if msg is None:
            say("My brain's gone quiet on me. Give me a second and try that again.")
            break
        calls = msg.tool_calls
        if not calls:
            break
        if any(c.function.name.startswith("browser_") for c in calls):
            browser_mode = True

        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name,
                                                      "arguments": c.function.arguments or "{}"}}
                                        for c in calls]})
        need_followup = len(spoken) == before     # she said nothing yet, so she must now
        for c in calls[:max_calls]:
            if cancel.is_set():
                break
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            bus.publish("tool", c.function.name.replace("_", " "))
            t1 = time.time()
            observe, result = run_tool(c.function.name, args, confirm)
            log.info("tool %s took %.1fs", c.function.name, time.time() - t1)
            print("[tool] {0} took {1:.1f}s".format(c.function.name, time.time() - t1))
            last_result = result
            need_followup = need_followup or observe
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
        for c in calls[max_calls:]:
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "content": "Skipped: too many tool calls."})
        if cancel.is_set() or not need_followup:
            break

    if not spoken and not cancel.is_set():
        say("That one bit me. Check the terminal and I'll try again."
            if last_result.startswith("ERROR") else random.choice(ACKS))
    print("[t] finished in {0:.1f}s".format(time.time() - t0))
    reply = " ".join(spoken)
    if reply:
        memory.add_turn("assistant", reply)
        bus.publish("reply", reply)
    return reply
