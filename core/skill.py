import inspect
import json
import logging
import re
import threading

log = logging.getLogger("iris")
REGISTRY = {}
_CONFIRM = threading.local()

_SAY = {"type": "string",
        "description": ("One short, natural sentence to say aloud right now while the "
                        "action runs. Never state results here.")}

# Tools that are cheap and useful in almost every turn. When a model has a low
# input-token limit these are kept first, then the rest are ranked by relevance.
_CORE_TOOLS = frozenset({
    "open_app", "open_anything", "system_info", "web_search", "remember",
    "set_reminder", "notify", "screenshot", "create_skill", "list_skills",
    "forget_skill",
})
_WORD_RE = re.compile(r"[a-z0-9]+")


def register(name, fn, description, level=1, observe=False, params=None,
             required=None, detail=None):
    """Add a tool to the registry, with explicit parameters when needed.

    ``@skill`` uses this for normal functions. Generated skills (``core/recipe``)
    have no real Python signature, so they pass their parameter names directly.
    ``detail`` is an optional callable that turns the cleaned arguments into a
    short confirmation line, so a JSON-heavy tool does not read its payload aloud.
    """
    if params is None:
        sig = inspect.signature(fn)
        params = list(sig.parameters)
        required = [p for p, v in sig.parameters.items()
                    if v.default is inspect.Parameter.empty]
    REGISTRY[name] = {"fn": fn, "description": description,
                      "level": int(level), "observe": bool(observe),
                      "params": list(params or []),
                      "required": list(required or []),
                      "detail": detail}
    return fn


def skill(description, level=1, observe=False):
    """level 1 = runs automatically, 2 = asks the user first, 3 = never runs.
    observe=True means the result is sent back to the model so it can speak it."""
    def wrap(fn):
        return register(fn.__name__, fn, description, level=level,
                        observe=observe)
    return wrap


def current_confirm():
    """The confirm callable of the tool currently running, if any.

    Generated skills run inside ``run_tool`` and use this to send every step
    through the same confirmation path as a hand-written skill.
    """
    return getattr(_CONFIRM, "fn", None)


def tool_schemas(prefix=None):
    out = []
    for name, s in REGISTRY.items():
        if s["level"] >= 3:
            continue
        if prefix and not name.startswith(prefix):
            continue
        out.append({"type": "function", "function": {
            "name": name,
            "description": s["description"],
            "parameters": {
                "type": "object",
                "properties": {"say": _SAY,
                               **{p: {"type": "string"} for p in s["params"]}},
                "required": ["say"] + s["required"],
            }}})
    return out


def _schema_tokens(schema):
    """Rough token estimate for one tool schema.

    Two characters per token is deliberately conservative: JSON punctuation
    and long tool names tokenize much less efficiently than prose, so a
    four-character estimate lets an oversized request slip through.
    """
    try:
        return max(1, len(json.dumps(schema, separators=(",", ":"))) // 2)
    except (TypeError, ValueError):
        return 1


def select_tools(items, budget, query=""):
    """Keep the most useful tool schemas inside a rough token budget.

    Groq's free tier allows only a few thousand input tokens per minute, while
    the full registry is larger than that. Sending everything gets HTTP 413 and
    no reply at all, so when a budget is set this keeps a small core set and
    fills the rest by how well each tool matches the user's words. A budget of
    zero or less means "send everything", which is what the larger free models
    get.
    """
    items = list(items or [])
    try:
        budget = int(budget or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0 or not items:
        return items
    if sum(_schema_tokens(item) for item in items) <= budget:
        return items
    words = set(_WORD_RE.findall(str(query or "").lower()))
    core, rest = [], []
    for item in items:
        name = str((item.get("function") or {}).get("name") or "")
        (core if name in _CORE_TOOLS else rest).append(item)

    def score(item):
        fn = item.get("function") or {}
        text = (str(fn.get("name") or "") + " " +
                str(fn.get("description") or "")).lower()
        return len(words & set(_WORD_RE.findall(text)))

    chosen, used = [], 0
    for item in core:
        cost = _schema_tokens(item)
        if used + cost <= budget:
            chosen.append(item)
            used += cost
    for item in sorted(rest, key=lambda it: (
            -score(it), str((it.get("function") or {}).get("name") or ""))):
        cost = _schema_tokens(item)
        if used + cost <= budget:
            chosen.append(item)
            used += cost
    return chosen


def run_tool(name, args, confirm):
    spec = REGISTRY.get(name)
    if spec is None:
        return True, "ERROR: unknown tool " + str(name)
    if spec["level"] >= 3:
        return True, "That action is not allowed."
    clean = {k: str(v) for k, v in (args or {}).items() if k in spec["params"]}
    missing = [p for p in spec["required"] if p not in clean]
    if missing:
        return True, "ERROR: missing arguments: " + ", ".join(missing)
    if spec["level"] == 2:
        maker = spec.get("detail")
        try:
            detail = maker(clean) if maker else ", ".join(
                f"{k}={v}" for k, v in clean.items())
        except Exception:  # noqa: BLE001 - a bad summary must not block a tool
            detail = ", ".join(f"{k}={v}" for k, v in clean.items())
        if not confirm(f"Do you want me to run {name} {detail}?"):
            return True, "The user cancelled the action."
    previous = getattr(_CONFIRM, "fn", None)
    _CONFIRM.fn = confirm
    try:
        result = str(spec["fn"](**clean))
        log.info("tool %s %s -> %s", name, clean, result[:200])
        return (spec["observe"] or result.startswith("ERROR")), result[:1500]
    except Exception as e:
        log.exception("tool %s failed", name)
        return True, f"ERROR: {name} failed: {e}"
    finally:
        _CONFIRM.fn = previous
