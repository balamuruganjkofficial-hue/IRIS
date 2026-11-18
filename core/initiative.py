"""Iris' own initiative: the small, rate-limited freedom to speak or act first.

The agent loop answers when spoken to. This module gives Iris a second, quieter
loop: after a long silence she may decide that one short, useful line is worth
saying, and sometimes a safe tool call is worth making on her own. It is not a
licence to run the machine unattended - risky tools still go through the normal
confirmation path, and the rate limits in ``config.yaml`` are hard caps.

Everything except :func:`decide` is pure, so the decision rules can be tested
without a microphone, a model or a speaker.
"""

import json
import logging
import re
import threading
import time

from core import brain
from core.config import CFG

log = logging.getLogger("iris")

_PROMPT = (
    "You are {name}, thinking to yourself while {call} is busy and quiet. "
    "Decide whether one small, genuinely useful thing is worth doing now. "
    "Reply with JSON only, no markdown and no commentary:\n"
    '{{"say": "one short spoken line", "do": null}}\n'
    'or, when a safe tool call clearly helps:\n'
    '{{"say": "one short spoken line", "do": {{"tool": "tool_name", '
    '"args": {{"arg": "value"}}}}}}\n'
    "Use only ordinary, reversible actions. Never choose a risky, destructive, "
    "money-spending or private action; the user will handle those. If there is "
    "nothing worth saying, reply "
    '{{"say": "", "do": null}}. One sentence, in your own voice, no results.'
)

# Tools Iris may run without a human in the loop. Everything else is either
# read-only narration or needs the normal confirmation path.
SAFE_TOOLS = {
    "open_app", "open_anything", "web_search", "web_news", "system_info",
    "show_dashboard", "show_screen", "show_image", "show_text", "show_file",
    "show_folder", "list_windows", "list_apps", "read_file", "list_files",
    "find_file", "open_folder", "browser_home", "browser_open",
    "browser_search", "browser_look", "browser_read", "browser_tabs",
    "browser_new_tab", "browser_switch_tab", "browser_find", "browser_links",
    "browser_scroll", "browser_wait", "browser_wait_for", "browser_back",
    "browser_forward", "browser_reload", "browser_view",
}
SAFE_PREFIXES = ("show_", "list_")
UNSAFE_TOOLS = {"browser_eval", "browser_upload", "browser_press",
                "browser_click", "browser_click_text", "browser_type",
                "browser_select", "delete_file", "run_command", "close_app",
                "close_window", "forget"}


def safe_tool(name):
    """True only for ordinary, reversible tools Iris may choose herself."""
    text = str(name or "").strip()
    if not text or text in UNSAFE_TOOLS:
        return False
    return text in SAFE_TOOLS or text.startswith(SAFE_PREFIXES)


def _settings(config=None):
    cfg = config or CFG
    return cfg.get("autonomy") or {}


def should_consider(now, last_activity, last_spoken, spoken_times=(),
                    busy=False, config=None):
    """True when an unprompted line is allowed right now.

    ``spoken_times`` are previous unprompted timestamps; only the last hour
    counts towards ``max_per_hour``. Every argument is explicit so this is easy
    to test and impossible to accidentally fire in a tight loop.
    """
    auto = _settings(config)
    if not bool(auto.get("enabled", True)):
        return False
    if busy:
        return False
    try:
        idle = max(1.0, float(auto.get("idle_minutes", 20.0))) * 60.0
        cooldown = max(1.0, float(auto.get("cooldown_minutes", 40.0))) * 60.0
        cap = max(0, int(auto.get("max_per_hour", 3) or 0))
    except (TypeError, ValueError):
        idle, cooldown, cap = 20 * 60.0, 40 * 60.0, 3
    try:
        now = float(now)
        last_activity = float(last_activity)
        last_spoken = float(last_spoken or 0.0)
    except (TypeError, ValueError):
        return False
    if now - last_activity < idle:
        return False
    if last_spoken and now - last_spoken < cooldown:
        return False
    if cap:
        recent = 0
        for stamp in spoken_times or ():
            try:
                if now - float(stamp) < 3600.0:
                    recent += 1
            except (TypeError, ValueError):
                continue
        if recent >= cap:
            return False
    return True


def _action(value):
    """Normalise the model's ``do`` field into (tool, args) or (None, {})."""
    if isinstance(value, dict):
        name = value.get("tool") or value.get("name") or value.get("action")
        args = value.get("args") or value.get("arguments") or {}
        if not name:
            return None, {}
        return str(name).strip(), args if isinstance(args, dict) else {}
    if isinstance(value, str) and value.strip():
        return value.strip(), {}
    return None, {}


def parse_decision(raw):
    """Turn a model's reply into ``{"say", "tool", "args"}``.

    Accepts a clean JSON object, JSON wrapped in a code fence, or a short plain
    sentence as a last resort. Anything empty or unparseable returns ``None`` so
    the caller simply does nothing.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    data = None
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
        except ValueError:
            data = None
    if isinstance(data, dict):
        line = str(data.get("say") or data.get("speak") or "").strip()
        tool, args = _action(data.get("do") or data.get("tool")
                             or data.get("action"))
        if not line and not tool:
            return None
        return {"say": line, "tool": tool, "args": args}
    if text.startswith(("{", "[", "<")):
        return None
    line = text.strip().strip('"').strip()
    if not line:
        return None
    return {"say": line[:240], "tool": None, "args": {}}


def _messages(context="", facts=None, now=None, config=None):
    cfg = config or CFG
    persona = cfg.get("persona") or {}
    name = persona.get("name", "Iris")
    call = persona.get("call_me", "boss")
    stamp = (now or time.time())
    try:
        clock = time.strftime("%A, %d %B %Y, %I:%M %p", time.localtime(stamp))
    except (TypeError, ValueError, OSError):
        clock = ""
    lines = [_PROMPT.format(name=name, call=call),
             "It is now {0}.".format(clock)]
    if facts:
        lines.append("Things you remember about {0}:\n{1}".format(
            call, "\n".join("- " + str(f) for f in facts)))
    if context:
        lines.append("What has just happened:\n" + str(context)[:1500])
    return [{"role": "system", "content": "\n\n".join(lines)},
            {"role": "user", "content": "Decide now. JSON only."}]


def decide(context="", facts=None, now=None, config=None, ask=None):
    """Ask the free model for one unprompted decision.

    Returns a parsed decision or ``None``. It never speaks and never runs a
    tool itself, so the caller keeps control of timing and permissions.
    """
    auto = _settings(config)
    if not bool(auto.get("model_decides", True)):
        return None
    ask = ask or brain.ask_stream
    parts = []
    try:
        reply = ask(_messages(context, facts, now, config), tools=None,
                    on_sentence=parts.append, max_tokens=180,
                    cancelled=lambda: False)
    except Exception as exc:  # noqa: BLE001 - initiative must never crash Iris
        log.warning("initiative model failed: %s", exc)
        return None
    raw = " ".join(part for part in parts if part).strip()
    if not raw and reply is not None:
        raw = str(getattr(reply, "content", "") or "").strip()
    decision = parse_decision(raw)
    if decision:
        decision["raw"] = raw[:600]
    return decision


class Initiative:
    """Stateful wrapper around :func:`should_consider` and :func:`decide`."""

    def __init__(self, config=None, now=time.time, decide_fn=None):
        self.config = config or CFG
        self._now = now
        self._decide = decide_fn or decide
        self._lock = threading.Lock()
        stamp = self._now()
        self.last_activity = stamp
        self.last_spoken = 0.0
        self.last_checked = 0.0
        self.spoken_times = []

    def note_activity(self, when=None):
        """A real turn happened; reset the idle clock."""
        with self._lock:
            self.last_activity = self._now() if when is None else float(when)

    def maybe(self, context="", facts=None, busy=False, fallback=None):
        """Return one decision, or ``None`` when she should stay quiet.

        ``fallback`` is an optional callable returning a plain line, used when
        the model is unavailable or chooses to say nothing. It keeps the old
        gentle idle line working offline without a second timer.
        """
        now = self._now()
        with self._lock:
            allowed = should_consider(
                now, self.last_activity, self.last_spoken,
                tuple(self.spoken_times), busy, self.config)
            if allowed:
                auto = _settings(self.config)
                try:
                    check_gap = max(1.0, float(auto.get("check_minutes", 8.0))) * 60.0
                except (TypeError, ValueError):
                    check_gap = 8 * 60.0
                if self.last_checked and now - self.last_checked < check_gap:
                    allowed = False
                if allowed:
                    self.last_checked = now
        if not allowed:
            return None
        decision = self._decide(context, facts=facts, now=now,
                                config=self.config)
        if not decision and fallback is not None:
            try:
                line = str(fallback() or "").strip()
            except Exception:  # noqa: BLE001 - a fallback must never crash
                line = ""
            if line:
                decision = {"say": line, "tool": None, "args": {}, "raw": ""}
        if not decision:
            return None
        if not decision.get("say") and not decision.get("tool"):
            return None
        with self._lock:
            self.last_spoken = now
            self.last_activity = now
            self.spoken_times = [t for t in self.spoken_times
                                 if now - float(t) < 3600.0] + [now]
        return decision
