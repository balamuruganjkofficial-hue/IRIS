"""Small, deterministic local command router used when the net is down.

It is deliberately boring: a handful of exact patterns mapped to existing
level-1 skills. Nothing here talks to a model or a website, so local work keeps
working even with no Ollama installed. Level-2 skills still go through the
normal voice confirmation inside ``core.skill.run_tool``.
"""

import datetime
import re

from core.skill import run_tool

# These need the internet; the agent hides them from the offline tool list.
ONLINE_ONLY = {
    "web_search", "web_news", "open_result", "browser_search",
    "read_webpage", "google_search", "open_website", "open_chrome",
    "browser_home", "browser_open", "browser_new_tab", "browser_back",
    "browser_forward", "browser_reload", "browser_wait", "browser_wait_for",
    "browser_switch_tab", "browser_close_tab",
}

_DENY = lambda _question: False


def _run(name, confirm, **args):
    try:
        _, result = run_tool(name, args, confirm or _DENY)
        return result
    except Exception as exc:  # noqa: BLE001 - local failure stays local
        return "ERROR: {0} could not run: {1}".format(name, type(exc).__name__)


def route(text, confirm=None):
    """Return a spoken reply for a known local command, else None."""
    value = str(text or "").strip().lower().rstrip(" .!?")
    if not value:
        return None

    if value in ("what time is it", "what's the time", "the time", "time"):
        return "It is {0}.".format(datetime.datetime.now().strftime("%I:%M %p").lstrip("0"))

    if value in ("battery", "system info", "system status", "system stats"):
        return _run("system_info", confirm)

    if value in ("screenshot", "take a screenshot", "take screenshot"):
        return _run("screenshot", confirm)

    if re.fullmatch(r"(?:volume|sound) up", value):
        return _run("volume", confirm, action="up")
    if re.fullmatch(r"(?:volume|sound) down", value):
        return _run("volume", confirm, action="down")
    if re.fullmatch(r"mute(?: (?:the )?sound)?", value):
        return _run("volume", confirm, action="mute")

    if value in ("lock screen", "lock the screen"):
        return _run("lock_screen", confirm)

    if value in ("show dashboard", "show system dashboard"):
        return _run("show_dashboard", confirm)

    if re.fullmatch(r"list (?:my )?(windows|apps|files)", value):
        target = value.split()[-1]
        names = {"windows": "list_windows", "apps": "list_apps",
                 "files": "list_files"}
        return _run(names[target], confirm)

    match = re.fullmatch(r"open (.+)", value)
    if match:
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "www.")):
            return "No internet right now, so I cannot open that page."
        return _run("open_anything", confirm, target=target)

    match = re.fullmatch(r"close (.+)", value)
    if match:
        return _run("close_app", confirm, name=match.group(1).strip())

    match = re.fullmatch(r"note (.+)", value)
    if match:
        return _run("add_note", confirm, text=match.group(1).strip())

    match = re.fullmatch(r"find (.+)", value)
    if match:
        return _run("find_file", confirm, name=match.group(1).strip())

    match = re.fullmatch(r"read (.+)", value)
    if match:
        return _run("read_file", confirm, name=match.group(1).strip())
    return None
