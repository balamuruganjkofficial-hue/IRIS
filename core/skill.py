import inspect
import logging

log = logging.getLogger("iris")
REGISTRY = {}

_SAY = {"type": "string",
        "description": ("One short, natural sentence to say aloud right now while the "
                        "action runs. Never state results here.")}


def skill(description, level=1, observe=False):
    """level 1 = runs automatically, 2 = asks the user first, 3 = never runs.
    observe=True means the result is sent back to the model so it can speak it."""
    def wrap(fn):
        sig = inspect.signature(fn)
        params = list(sig.parameters)
        required = [p for p, v in sig.parameters.items()
                    if v.default is inspect.Parameter.empty]
        REGISTRY[fn.__name__] = {"fn": fn, "description": description,
                                 "level": level, "observe": observe,
                                 "params": params, "required": required}
        return fn
    return wrap


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
        detail = ", ".join(f"{k}={v}" for k, v in clean.items())
        if not confirm(f"Do you want me to run {name} {detail}?"):
            return True, "The user cancelled the action."
    try:
        result = str(spec["fn"](**clean))
        log.info("tool %s %s -> %s", name, clean, result[:200])
        return (spec["observe"] or result.startswith("ERROR")), result[:1500]
    except Exception as e:
        log.exception("tool %s failed", name)
        return True, f"ERROR: {name} failed: {e}"
