import datetime
import json
import os
import subprocess
import threading
import urllib.parse
import webbrowser

import psutil
from plyer import notification

from core import memory, voice
from core.config import CFG, ROOT
from core.skill import skill

_USER_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "Google", "Chrome", "User Data")
_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"Google\Chrome\Application\chrome.exe"),
]


def _load_profiles():
    """Read real profile names from Chrome. Returns (lookup, display_names, last_used)."""
    lookup, names, last_used = {}, [], None
    try:
        with open(os.path.join(_USER_DATA, "Local State"), encoding="utf-8") as f:
            state = json.load(f)["profile"]
        last_used = state.get("last_used")
        for folder, info in state.get("info_cache", {}).items():
            main = (info.get("name") or folder).strip()
            names.append(main)
            keys = [main, info.get("gaia_name"), info.get("shortcut_name"),
                    info.get("user_name"), (info.get("user_name") or "").split("@")[0]]
            for k in keys:
                if k:
                    lookup[k.lower().strip()] = folder
            lookup[folder.lower()] = folder
    except Exception as e:
        print("[chrome] could not read profiles:", e)
    for nick, folder in CFG.get("chrome", {}).get("profiles", {}).items():
        lookup[nick.lower().strip()] = folder
        names.append(nick)
    return lookup, names, last_used


_LOOKUP, _NAMES, _LAST_USED = _load_profiles()
_DEFAULT = CFG.get("chrome", {}).get("default_profile") or _LAST_USED or "Default"
_NAMES_TXT = ", ".join(_NAMES) or "none found"


def _resolve_profile(want):
    w = (want or "").lower().strip()
    if not w:
        return _DEFAULT
    if w in _LOOKUP:
        return _LOOKUP[w]
    for key, folder in _LOOKUP.items():
        if key and (w in key or key in w):
            return folder
    return None


def _chrome(url=None, profile=""):
    """Every browser task is shown *inside* Iris; no external Chrome window.

    The ``profile`` argument is kept so older spoken commands still work.
    """
    from skills import surface
    return surface.open_browser(url or None)


@skill("Open Iris's own browser inside her interface, with her start page and "
       "search box. Use this whenever the user says 'open Chrome' or 'open the "
       "browser'. Args: profile (optional; accepted for compatibility)")
def open_chrome(profile=""):
    from skills.browser_agent import browser_home
    result = browser_home()
    if result.startswith("ERROR"):
        return result
    return "Opened my browser inside the interface."


@skill("Search the web and show the results page inside Iris's own browser panel. "
       "Args: query, profile (optional; accepted for compatibility)")
def google_search(query, profile=""):
    from skills.browser_agent import browser_search
    result = browser_search(query)
    return result if result.startswith("ERROR") else "Searching inside my interface."


@skill("Show a website inside Iris's own browser panel. Args: url, profile "
       "(optional; accepted for compatibility)")
def open_website(url, profile=""):
    from skills.browser_agent import browser_open
    url = str(url or "").strip()
    if " " in url or "." not in url:
        return "ERROR: that does not look like a website."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    result = browser_open(url)
    return result if result.startswith("ERROR") else \
        "Opened {0} inside my interface.".format(url)


@skill("Get battery, RAM and CPU status. No args.", observe=True)
def system_info():
    b = psutil.sensors_battery()
    batt = "no battery" if b is None else (
        f"{b.percent:.0f}% ({'charging' if b.power_plugged else 'on battery'})")
    ram = psutil.virtual_memory()
    return (f"Battery {batt}. RAM {ram.percent:.0f}% used, "
            f"{ram.available / 1e9:.1f} GB free. CPU {psutil.cpu_percent(interval=0.5):.0f}%.")


def _fire(text):
    voice.speak("Reminder: " + text)
    try:
        notification.notify(title="Iris reminder", message=text, timeout=8)
    except Exception:
        pass


@skill("Set a reminder. Args: minutes (number), text")
def set_reminder(minutes, text):
    try:
        m = float(minutes)
    except ValueError:
        return "ERROR: minutes must be a number."
    if not 0 < m <= 1440:
        return "ERROR: choose between 0 and 1440 minutes."
    t = threading.Timer(m * 60, _fire, args=[text])
    t.daemon = True
    t.start()
    return f"Reminder set for {m:g} minutes."


@skill("Show a desktop notification. Args: title, message")
def notify(title, message):
    try:
        notification.notify(title=title, message=message, timeout=6)
    except Exception as e:
        return f"ERROR: could not show the notification: {e}"
    return "Shown."


@skill("Take a screenshot of the screen and show it inside Iris's own interface. "
       "No args.")
def screenshot():
    from PIL import ImageGrab
    folder = ROOT / "data" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.datetime.now().strftime("shot_%Y%m%d_%H%M%S.png")
    try:
        try:
            ImageGrab.grab(all_screens=True).save(path)
        except TypeError:                     # older Pillow
            ImageGrab.grab().save(path)
    except Exception as e:
        return f"ERROR: could not take the screenshot: {e}"
    print("[screenshot]", path)
    try:
        from skills import surface
        surface.show_image(str(path), title="Screenshot")
    except Exception:  # noqa: BLE001
        pass
    return "Screenshot taken and shown inside my interface."


@skill("Remember a fact about the user for later. Args: fact")
def remember(fact):
    memory.add_fact(fact)
    return "Saved."


@skill("Delete remembered facts that contain this text. Args: text", level=2)
def forget(text):
    return f"Deleted {memory.forget_fact(text)} fact(s)."
