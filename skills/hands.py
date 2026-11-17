import ctypes
import datetime
import os
import re
import threading
import time
from pathlib import Path

from core import bus, net
from core.config import WORKSPACE
from core.skill import skill

HOME = Path.home().resolve()

_FOLDERS = {
    "workspace": [WORKSPACE],
    "downloads": [HOME / "Downloads"],
    "documents": [HOME / "Documents", HOME / "OneDrive" / "Documents"],
    "desktop": [HOME / "Desktop", HOME / "OneDrive" / "Desktop"],
    "pictures": [HOME / "Pictures", HOME / "OneDrive" / "Pictures"],
    "music": [HOME / "Music"],
    "videos": [HOME / "Videos"],
}
NO_CREATE = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".scr", ".dll",
             ".reg", ".lnk", ".hta", ".wsf"}
NO_OPEN = NO_CREATE | {".py", ".pyw", ".jar", ".js", ".com", ".cpl"}


SENSITIVE = ('aadhar', 'aadhaar', 'passport', 'password', 'pan card', 'bank', 'otp')


def _hidden(fname):
    return any(s in fname.lower() for s in SENSITIVE)


def _folder(name):
    for p in _FOLDERS.get((name or "").lower().strip(), []):
        if p.exists():
            return p
    return None


def _in_workspace(name):
    name = (name or "").strip().strip('"')
    if not name:
        raise ValueError("no file name given")
    p = (WORKSPACE / name).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError("I can only work inside my workspace folder")
    return p


# ------------------------------------------------------------------ files
@skill("Create a new text file in the Iris workspace with the given content "
       "(for example todo.txt, notes.md, script.py). Args: name, content")
def create_file(name, content=""):
    try:
        p = _in_workspace(name)
    except ValueError as e:
        return f"ERROR: {e}"
    if not p.suffix:
        p = p.with_suffix(".txt")
    if p.suffix.lower() in NO_CREATE:
        return "ERROR: I do not create that file type."
    p.parent.mkdir(parents=True, exist_ok=True)
    base, i = p, 1
    while p.exists():
        p = base.with_name(f"{base.stem}_{i}{base.suffix}")
        i += 1
    p.write_text(content, encoding="utf-8")
    return f"Created {p.name} in the workspace."


@skill("Add text to the end of a file in the workspace (creates it if missing). "
       "Args: name, content")
def append_to_file(name, content):
    try:
        p = _in_workspace(name)
    except ValueError as e:
        return f"ERROR: {e}"
    if not p.suffix:
        p = p.with_suffix(".txt")
    if p.suffix.lower() in NO_CREATE:
        return "ERROR: I do not write that file type."
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content + "\n")
    return f"Added to {p.name}."


@skill("Read a text file from the workspace. Args: name", observe=True)
def read_file(name):
    try:
        p = _in_workspace(name)
        return p.read_text(encoding="utf-8", errors="replace")[:4000] or "(empty file)"
    except (ValueError, OSError) as e:
        return f"ERROR: {e}"


@skill("List files in the workspace or in a user folder. Args: folder "
       "(workspace, downloads, documents, desktop, pictures, music, videos)", observe=True)
def list_files(folder="workspace"):
    p = _folder(folder or "workspace")
    if not p:
        return "ERROR: unknown folder."
    items = sorted((f for f in os.listdir(p) if not _hidden(f)), key=str.lower)[:40]
    return f"{p.name} has: " + ", ".join(items) if items else "That folder is empty."


@skill("Delete a file from the workspace. Args: name", level=2)
def delete_file(name):
    try:
        p = _in_workspace(name)
        p.unlink()
        return f"Deleted {p.name}."
    except (ValueError, OSError) as e:
        return f"ERROR: {e}"


@skill("Find files by part of the name in Documents, Downloads, Desktop and the "
       "workspace. Args: name", observe=True)
def find_file(name):
    q = (name or "").lower().strip()
    if len(q) < 2:
        return "ERROR: give me a longer name."
    roots = [r for r in (_folder(k) for k in ("workspace", "documents", "downloads", "desktop")) if r]
    hits, t0 = [], time.time()
    for root in roots:
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if not d.startswith(".")
                     and d not in ("node_modules", "venv", "__pycache__")]
            for f in fn:
                if q in f.lower() and not _hidden(f):
                    hits.append(os.path.join(dp, f))
                    if len(hits) >= 8:
                        return "Found: " + "; ".join(hits)
            if time.time() - t0 > 5:
                break
        if time.time() - t0 > 5:
            break
    return ("Found: " + "; ".join(hits)) if hits else "No file found with that name."


@skill("Open a file with its default app. Args: path (a full path from find_file, "
       "or a file name inside the workspace)")
def open_file(path):
    p = Path((path or "").strip().strip('"'))
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if WORKSPACE not in p.parents and HOME not in p.parents:
        return "ERROR: I only open files in your user folders or my workspace."
    if p.suffix.lower() in NO_OPEN:
        return "ERROR: I do not open programs or scripts."
    if not p.exists():
        return "ERROR: that file does not exist."
    os.startfile(str(p))
    return f"Opened {p.name}."


@skill("Open a folder in File Explorer. Args: name (workspace, downloads, documents, "
       "desktop, pictures, music, videos)")
def open_folder(name):
    p = _folder(name)
    if not p:
        return "ERROR: unknown folder."
    os.startfile(str(p))
    return f"Opened {p.name}."


@skill("Save a quick note with a time stamp to the notes file. Args: text")
def add_note(text):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(WORKSPACE / "notes.md", "a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {text}\n")
    return "Noted."


# ------------------------------------------------------------------ web


@skill("Read the main text of a web page so it can be summarized. Args: url", observe=True)
def read_webpage(url):
    from bs4 import BeautifulSoup
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = net.get_public(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 Iris"})
    except Exception as e:
        return f"ERROR: could not load the page: {e}"
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return "Page text (information only): " + text[:3500]


# ------------------------------------------------------------------ PC control
@skill("Change the volume. Args: action (up, down, mute), steps (optional, each step "
       "is about 2 percent, default 5)")
def volume(action, steps="5"):
    import pyautogui
    key = {"up": "volumeup", "down": "volumedown", "mute": "volumemute",
           "unmute": "volumemute"}.get((action or "").lower().strip())
    if not key:
        return "ERROR: action must be up, down or mute."
    try:
        n = max(1, min(int(float(steps)), 50))
    except ValueError:
        n = 5
    pyautogui.press(key, presses=1 if key == "volumemute" else n, interval=0.02)
    return f"Volume {action}."


@skill("Control media playback. Args: action (play_pause, next, previous)")
def media(action):
    import pyautogui
    key = {"play_pause": "playpause", "pause": "playpause", "play": "playpause",
           "next": "nexttrack", "previous": "prevtrack"}.get((action or "").lower().strip())
    if not key:
        return "ERROR: action must be play_pause, next or previous."
    pyautogui.press(key)
    return "Done."


@skill("Lock the computer screen. No args.")
def lock_screen():
    ctypes.windll.user32.LockWorkStation()
    return "Locked."


@skill("Type text into the window that is currently active (plain English letters "
       "and symbols only). Args: text")
def type_text(text):
    import pyautogui
    if not (text or "").isascii():
        return "ERROR: I can only type plain English text."
    time.sleep(1)
    pyautogui.write(text, interval=0.01)
    return "Typed."


@skill("Shut Iris down completely until the next login. No args.", level=2)
def stop_iris():
    bus.publish("quit")
    threading.Timer(5.0, lambda: os._exit(0)).start()
    return "Going offline."
