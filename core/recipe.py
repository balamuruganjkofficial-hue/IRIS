"""Skills Iris writes for herself: small data-only recipes over existing tools.

A recipe is a JSON document. It contains no Python and cannot run arbitrary
code: it lists registered tools and the arguments to pass them. Every step goes
through :func:`core.skill.run_tool`, so a risky step still asks through the
normal voice/UI confirmation path.

Recipes are created by the ``create_skill`` tool (level 2, so the user is asked
before anything is saved), stored in ``data/skills`` and registered as normal
tools when Iris starts. They are deliberately limited:

* only registered tools can be called, never another recipe;
* a step can only use parameters that tool really accepts;
* arguments are plain text/number/bool values, with ``{name}`` placeholders;
* level-3 tools and the skill-management tools are refused.

The generated skill itself runs automatically (level 1). That is safe because
each individual step keeps its own level: a recipe that opens Notepad runs
without a prompt, while a recipe that deletes a file still asks at that step.
"""

import json
import logging
import os
import re
import threading
from pathlib import Path

from core import bus
from core.config import CFG, DATA, ROOT
from core.skill import REGISTRY, current_confirm, register, run_tool, skill

log = logging.getLogger("iris")

_S = CFG.get("skills") or {}
MAX_STEPS = max(1, int(_S.get("max_steps", 8) or 8))
MAX_RECIPES = max(1, int(_S.get("max_recipes", 50) or 50))
MAX_DESCRIPTION = 300
MAX_STEP_JSON = 4000

_raw_folder = str(_S.get("folder") or "").strip()
FOLDER = Path(_raw_folder) if _raw_folder else (DATA / "skills")
if not FOLDER.is_absolute():
    FOLDER = ROOT / FOLDER

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_ARG_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

RESERVED = {"create_skill", "list_skills", "forget_skill"}
RECIPES = {}
LOCK = threading.RLock()


def _split_args(value):
    """Turn 'topic, city' (or a JSON list) into a list of parameter names."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(v).strip() for v in data if str(v).strip()]
        except (ValueError, TypeError):
            pass
    return [part for part in re.split(r"[,\s]+", text) if part]


def _slug(description):
    words = re.findall(r"[a-z0-9]+", str(description or "").lower())
    name = "_".join(words[:4]) or "learned_skill"
    if not name[0].isalpha():
        name = "skill_" + name
    name = name[:40].strip("_")
    return name if len(name) >= 3 else (name + "_skill")[:40]


def _unique(name, folder):
    candidate = name
    index = 2
    while (candidate in REGISTRY or candidate in RECIPES
           or (Path(folder) / (candidate + ".json")).exists()):
        suffix = "_" + str(index)
        candidate = name[:40 - len(suffix)] + suffix
        index += 1
    return candidate


def _path(name, folder=None):
    return Path(folder or FOLDER) / (str(name) + ".json")


def _parse_steps(text):
    try:
        data = json.loads(str(text or ""))
    except (ValueError, TypeError) as exc:
        return None, "steps must be a JSON array: " + str(exc)
    if not isinstance(data, list):
        return None, "steps must be a JSON array of {tool, args} objects"
    if len(text) > MAX_STEP_JSON:
        return None, "steps are too long (limit %d characters)" % MAX_STEP_JSON
    return data, ""


def validate(raw, folder=None, loading=False):
    """Return (clean_recipe, error). A clean recipe is safe to save and run."""
    if not isinstance(raw, dict):
        return None, "a recipe must be an object"
    name = str(raw.get("name") or "").strip().lower()
    if not _NAME_RE.match(name):
        return None, ("name must be 3-40 lowercase letters, digits or underscores "
                      "and start with a letter")
    if name in RESERVED:
        return None, "that name is reserved for skill management"
    if name in REGISTRY and name not in RECIPES:
        return None, "a tool named %s already exists" % name
    if not loading and name in RECIPES:
        return None, "a learned skill named %s already exists" % name

    description = " ".join(str(raw.get("description") or "").split())
    if not 5 <= len(description) <= MAX_DESCRIPTION:
        return None, "description must be 5-%d characters" % MAX_DESCRIPTION

    args = _split_args(raw.get("args"))
    if len(args) > 6:
        return None, "a skill can take at most 6 inputs"
    for arg in args:
        if not _ARG_RE.match(arg):
            return None, "bad input name: %s" % arg
        if arg == "say":
            return None, "'say' is reserved for the spoken line"
    if len(set(args)) != len(args):
        return None, "input names must be unique"

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, "a skill needs at least one step"
    if len(steps) > MAX_STEPS:
        return None, "a skill can have at most %d steps" % MAX_STEPS

    clean_steps = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return None, "step %d must be an object" % index
        tool_name = str(step.get("tool") or "").strip()
        spec = REGISTRY.get(tool_name)
        if spec is None:
            return None, "step %d uses unknown tool: %s" % (index, tool_name)
        if tool_name in RESERVED:
            return None, "step %d cannot call a skill-management tool" % index
        if tool_name in RECIPES:
            return None, "step %d cannot call another learned skill" % index
        if tool_name == name:
            return None, "step %d calls the skill itself" % index
        if spec["level"] >= 3:
            return None, "step %d uses a blocked tool: %s" % (index, tool_name)
        step_args = step.get("args") or {}
        if not isinstance(step_args, dict):
            return None, "step %d args must be an object" % index
        unknown = [key for key in step_args if key not in spec["params"]]
        if unknown:
            return None, "step %d: %s does not take %s" % (
                index, tool_name, ", ".join(unknown))
        missing = [key for key in spec["required"] if key not in step_args]
        if missing:
            return None, "step %d: missing %s for %s" % (
                index, ", ".join(missing), tool_name)
        clean_args = {}
        for key, value in step_args.items():
            if not isinstance(value, (str, int, float, bool)):
                return None, "step %d: %s must be text, a number or true/false" % (
                    index, key)
            for found in _PLACEHOLDER_RE.findall(str(value)):
                if found not in args:
                    return None, "step %d uses {%s}, which is not in args" % (
                        index, found)
            clean_args[key] = value
        clean_steps.append({"tool": tool_name, "args": clean_args})

    return ({"name": name, "description": description, "args": args,
             "steps": clean_steps, "version": 1}, "")


def save(recipe, folder=None):
    """Write one recipe atomically, so a crash cannot leave a broken skill."""
    path = _path(recipe["name"], folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(recipe, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _fill(value, values):
    if not isinstance(value, str):
        return str(value)
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), value)


def execute(recipe, values):
    """Run every step in order, stopping at the first error or cancellation."""
    confirm = current_confirm() or (lambda _question: False)
    done = []
    for index, step in enumerate(recipe["steps"], 1):
        try:
            args = {key: _fill(value, values)
                    for key, value in (step.get("args") or {}).items()}
        except KeyError as exc:
            return "ERROR: missing input %s" % exc
        _observe, result = run_tool(step["tool"], args, confirm)
        done.append("%s: %s" % (step["tool"], result[:300]))
        if result.startswith("ERROR") or result.startswith("The user cancelled"):
            return ("Stopped at step %d of %d. %s"
                    % (index, len(recipe["steps"]), " | ".join(done)))[:1500]
    return ("Done. " + " | ".join(done))[:1500]


def _tool_description(recipe):
    extra = ""
    if recipe["args"]:
        extra = " Inputs: %s." % ", ".join(recipe["args"])
    return ("A skill Iris wrote for herself: %s%s It runs its saved steps in "
            "order and stops on the first error." %
            (recipe["description"], extra))


def register_recipe(recipe):
    """Expose one validated recipe as a normal tool."""
    def run(**kwargs):
        return execute(recipe, kwargs)

    run.__name__ = recipe["name"]
    with LOCK:
        register(recipe["name"], run, _tool_description(recipe), level=1,
                 observe=True, params=recipe["args"], required=recipe["args"])
        RECIPES[recipe["name"]] = recipe
    return recipe


def load_all(folder=None, limit=None):
    """Load and register every valid recipe in a folder. Bad files are skipped."""
    folder = Path(folder or FOLDER)
    if not folder.exists():
        return 0
    loaded = 0
    for path in sorted(folder.glob("*.json")):
        if limit is not None and loaded >= limit:
            break
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            log.warning("skipping unreadable skill %s: %s", path.name, exc)
            continue
        clean, error = validate(raw, folder=folder, loading=True)
        if error:
            log.warning("skipping invalid skill %s: %s", path.name, error)
            continue
        if clean["name"] in REGISTRY and clean["name"] not in RECIPES:
            log.warning("skipping skill %s: name is already a built-in tool",
                        clean["name"])
            continue
        try:
            register_recipe(clean)
            loaded += 1
        except OSError as exc:
            log.warning("could not load skill %s: %s", path.name, exc)
    return loaded


def reset_for_tests(folder=None):
    """Forget learned recipes and optionally point the loader at a temp folder."""
    global FOLDER
    with LOCK:
        for name in list(RECIPES):
            REGISTRY.pop(name, None)
        RECIPES.clear()
        if folder is not None:
            FOLDER = Path(folder)


def _create_detail(values):
    name = str(values.get("name") or "(auto name)")
    try:
        steps = json.loads(values.get("steps") or "[]")
        tools = [str(step.get("tool")) for step in steps
                 if isinstance(step, dict) and step.get("tool")]
        count = len(tools)
        used = ", ".join(tools[:4]) + ("..." if len(tools) > 4 else "")
    except (ValueError, TypeError):
        count, used = "?", ""
    extra = " (%s)" % used if used else ""
    return "save the skill %s with %s steps%s" % (name, count, extra)


def _forget_detail(values):
    return "delete the learned skill " + str(values.get("name") or "(none)")


def create_skill(description, steps, args="", name=""):
    folder = FOLDER
    parsed, error = _parse_steps(steps)
    if error:
        return "ERROR: " + error
    requested = str(name or "").strip().lower()
    wanted = requested or _slug(description)
    if not _NAME_RE.match(wanted):
        return "ERROR: name must be 3-40 lowercase letters, digits or underscores"
    if requested and (wanted in REGISTRY or wanted in RECIPES):
        return "ERROR: a tool named %s already exists" % wanted
    final_name = _unique(wanted, folder)
    clean, error = validate({"name": final_name, "description": description,
                             "args": _split_args(args), "steps": parsed},
                            folder=folder)
    if error:
        return "ERROR: " + error
    existing = len(RECIPES)
    if existing >= MAX_RECIPES:
        return ("ERROR: you already have %d learned skills; forget one first"
                % existing)
    try:
        save(clean, folder)
    except OSError as exc:
        return "ERROR: could not save the skill: %s" % exc
    register_recipe(clean)
    log.info("learned skill %s (%d steps)", clean["name"], len(clean["steps"]))
    bus.publish("toast", "Learned a new skill: " + clean["name"])
    return ("Learned the skill %s. It is available from the next turn."
            % clean["name"])


@skill("List the skills you have written for yourself, with their inputs. "
       "No args.", observe=True)
def list_skills():
    with LOCK:
        recipes = list(RECIPES.values())
    if not recipes:
        return "You have not written any skills yet."
    lines = []
    for recipe in sorted(recipes, key=lambda item: item["name"]):
        inputs = ", ".join(recipe["args"]) or "no inputs"
        lines.append("%s - %s (%s)" % (recipe["name"], recipe["description"],
                                       inputs))
    return "\n".join(lines)


def forget_skill(name):
    key = str(name or "").strip().lower()
    recipe = RECIPES.get(key)
    if recipe is None:
        return "ERROR: no learned skill named %s" % (key or "(empty)")
    try:
        _path(key).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return "ERROR: could not delete the skill: %s" % exc
    with LOCK:
        REGISTRY.pop(key, None)
        RECIPES.pop(key, None)
    log.info("forgot skill %s", key)
    return "Forgot the skill %s." % key


register("create_skill", create_skill,
         "Create a reusable skill for yourself from tools you already have. "
         "steps is a JSON array like [{\"tool\": \"open_app\", \"args\": "
         "{\"name\": \"notepad\"}}]. Use {name} placeholders and list each name "
         "in args. It may only call registered tools, never code or another "
         "learned skill. The user is asked before it is saved, and risky steps "
         "still ask every time they run. Args: description, steps, args "
         "(optional comma-separated inputs), name (optional).",
         level=2, observe=True, detail=_create_detail)
register("forget_skill", forget_skill,
         "Delete one of the skills you wrote for yourself. Built-in tools are "
         "never touched. Args: name", level=2, observe=True,
         detail=_forget_detail)


if bool(_S.get("enabled", True)):
    load_all(limit=MAX_RECIPES)
else:
    for _name in RESERVED:
        REGISTRY.pop(_name, None)
