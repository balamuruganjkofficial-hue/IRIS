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

APPS = {"edge": "msedge", "notepad": "notepad", "calculator": "calc",
        "explorer": "explorer", "vscode": "code", "paint": "mspaint",
        "word": "winword", "excel": "excel"}
PROCS = {"chrome": "chrome.exe", "edge": "msedge.exe", "notepad": "notepad.exe",
         "calculator": "CalculatorApp.exe", "vscode": "Code.exe",
         "paint": "mspaint.exe", "word": "WINWORD.EXE", "excel": "EXCEL.EXE"}

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
    folder = _resolve_profile(profile)
    if folder is None:
        return f"ERROR: no Chrome profile called '{profile}'. Profiles: {_NAMES_TXT}."
    exe = next((p for p in _CHROME_PATHS if os.path.exists(p)), None)
    if not exe:
        webbrowser.open(url or "https://www.google.com")
        return "ERROR: Chrome was not found, so I used the default browser."
    cmd = [exe, "--profile-directory=" + folder]
    if url:
        cmd.append(url)
    subprocess.Popen(cmd)
    print(f"[chrome] profile folder: {folder}")
    return "Opened Chrome."


@skill(f"Open Google Chrome. Args: profile (optional, one of: {_NAMES_TXT}; "
       "leave empty if the user did not name a profile)")
def open_chrome(profile=""):
    return _chrome(profile=profile)


@skill("Open an app (not Chrome). Args: name (edge, notepad, calculator, explorer, "
       "vscode, paint, word, excel)")
def open_app(name):
    key = name.lower().strip()
    if key in ("chrome", "google chrome"):
        return _chrome()
    token = APPS.get(key)
    if not token:
        return "ERROR: unknown app. Known apps: " + ", ".join(APPS)
    subprocess.Popen(["cmd", "/c", "start", "", token],
                     creationflags=subprocess.CREATE_NO_WINDOW)
    return "Opened " + name + "."


@skill("Close an app. Args: name (chrome, edge, notepad, calculator, vscode, paint, "
       "word, excel)", level=2)
def close_app(name):
    exe = PROCS.get(name.lower().strip())
    if not exe:
        return "ERROR: unknown app."
    n = 0
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == exe.lower():
            try:
                p.terminate()
                n += 1
            except psutil.Error:
                pass
    return f"Closed {n} {name} process(es)."


@skill("Search Google in Chrome. Args: query, profile (optional Chrome profile name)")
def google_search(query, profile=""):
    r = _chrome(url="https://www.google.com/search?q=" + urllib.parse.quote_plus(query),
                profile=profile)
    return "Searching." if not r.startswith("ERROR") else r


@skill("Open a website in Chrome. Args: url, profile (optional Chrome profile name)")
def open_website(url, profile=""):
    url = url.strip()
    if " " in url or "." not in url:
        return "ERROR: that does not look like a website."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = _chrome(url=url, profile=profile)
    return "Opened " + url + "." if not r.startswith("ERROR") else r


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
    notification.notify(title=title, message=message, timeout=6)
    return "Shown."


@skill("Take a screenshot of the screen and save it. No args.")
def screenshot():
    from PIL import ImageGrab
    folder = ROOT / "data" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / datetime.datetime.now().strftime("shot_%Y%m%d_%H%M%S.png")
    ImageGrab.grab().save(path)
    print("[screenshot]", path)
    return "Screenshot saved."


@skill("Remember a fact about the user for later. Args: fact")
def remember(fact):
    memory.add_fact(fact)
    return "Saved."


@skill("Delete remembered facts that contain this text. Args: text", level=2)
def forget(text):
    return f"Deleted {memory.forget_fact(text)} fact(s)."