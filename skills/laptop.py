"""Full user-level control of the laptop: apps, windows, settings and shell.

Everything here runs with the user's normal rights (no admin elevation). Windows
settings are opened through their official ``ms-settings:`` deep links, which is
what makes "open any setting" reliable without UAC.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import psutil

from core.config import CFG
from core.skill import skill

HOME = Path.home()

# Processes that must never be killed: closing these takes Windows down.
_PROTECTED = {
    "system", "system idle process", "smss.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe", "svchost.exe",
    "registry", "memory compression", "secure system", "fontdrvhost.exe",
    "dwm.exe", "audiodg.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe",
}

_APP_EXE = {
    "chrome": ["chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
    "google chrome": ["chrome"],
    "edge": ["msedge"], "microsoft edge": ["msedge"],
    "firefox": ["firefox"], "brave": ["brave"],
    "notepad": ["notepad"], "calculator": ["calc"], "calc": ["calc"],
    "paint": ["mspaint"], "vscode": ["code"], "vs code": ["code"],
    "visual studio code": ["code"], "word": ["winword"], "excel": ["excel"],
    "powerpoint": ["powerpnt"], "outlook": ["outlook"], "teams": ["ms-teams"],
    "spotify": ["spotify"], "discord": ["discord"], "steam": ["steam"],
    "vlc": ["vlc"], "whatsapp": ["whatsapp"], "telegram": ["telegram"],
    "task manager": ["taskmgr"], "cmd": ["cmd"], "command prompt": ["cmd"],
    "powershell": ["powershell"], "terminal": ["wt"], "windows terminal": ["wt"],
    "settings": ["ms-settings:"], "windows settings": ["ms-settings:"],
    "explorer": ["explorer"], "file explorer": ["explorer"], "files": ["explorer"],
    "snipping tool": ["snippingtool"], "camera": ["microsoft.windows.camera:"],
    "photos": ["ms-photos:"], "clock": ["ms-clock:"], "alarms": ["ms-clock:"],
    "store": ["ms-windows-store:"], "microsoft store": ["ms-windows-store:"],
    "xbox": ["xbox:"], "maps": ["bingmaps:"], "mail": ["outlook"],
    "control panel": ["control"], "device manager": ["devmgmt.msc"],
    "disk management": ["diskmgmt.msc"], "services": ["services.msc"],
    "event viewer": ["eventvwr.msc"], "task scheduler": ["taskschd.msc"],
    "registry editor": ["regedit"], "system information": ["msinfo32"],
}

_PROC_ALIASES = {
    "chrome": {"chrome.exe"},
    "edge": {"msedge.exe", "msedgewebview2.exe"},
    "firefox": {"firefox.exe"},
    "brave": {"brave.exe"},
    "vscode": {"code.exe"},
    "visual studio code": {"code.exe"},
    "calculator": {"calculatorapp.exe", "windowscalculator.exe", "calc.exe",
                   "calculator.exe"},
    "paint": {"mspaint.exe"},
    "word": {"winword.exe"},
    "excel": {"excel.exe"},
    "powerpoint": {"powerpnt.exe"},
    "outlook": {"outlook.exe"},
    "teams": {"ms-teams.exe", "teams.exe"},
    "spotify": {"spotify.exe"},
    "discord": {"discord.exe"},
    "steam": {"steam.exe", "steamwebhelper.exe"},
    "vlc": {"vlc.exe"},
    "whatsapp": {"whatsapp.exe"},
    "telegram": {"telegram.exe"},
    "terminal": {"windowsterminal.exe", "wt.exe"},
    "powershell": {"powershell.exe", "pwsh.exe"},
    "cmd": {"cmd.exe", "conhost.exe"},
    "notepad": {"notepad.exe"},
    "explorer": {"explorer.exe"},
    "task manager": {"taskmgr.exe"},
    "settings": {"systemsettings.exe"},
    "camera": {"windowscamera.exe"},
    "photos": {"microsoft.photos.exe"},
}

_BLOCKED = re.compile(
    r"(?i)(\bformat\b|\bdiskpart\b|\bbcdedit\b|\bvssadmin\b.*delete|"
     r"\bdel\b.*[/\\]\s*[a-z]:\b|\brmdir\b.*[/\\]\s*[a-z]:\b|"
     r"remove-item\b.*-recurse.*[a-z]:\\|"
     r"\bnet\s+user\b|\bnet\s+localgroup\b|"
     r"\breg\s+delete\b\s+hklm|"
     r"\b(disable|uninstall)-windowsoptionalfeature\b|"
     r"\bclear-disk\b|\binitialize-disk\b|\bnew-partition\b)")


def _start(token):
    token = (token or "").strip()
    if not token or re.search(r"[&|<>^]", token):
        return False
    if token.startswith(("ms-", "shell:", "http://", "https://")) or re.match(r"^[a-z0-9]+:$", token):
        os.startfile(token)
        return True
    subprocess.Popen(["cmd", "/c", "start", "", token],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True


_SHORTCUTS = None


def _shortcuts():
    """Cached list of Start-menu shortcut names -> .lnk paths."""
    global _SHORTCUTS
    if _SHORTCUTS is not None:
        return _SHORTCUTS
    roots = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
             Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
    found = []
    for root in roots:
        try:
            for path in root.rglob("*.lnk"):
                found.append((path.stem.lower(), path))
        except OSError:
            pass
    _SHORTCUTS = found
    return found


def _find_shortcut(name):
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower())]
    if not words:
        return None
    exact = None
    partial = []
    for stem, path in _shortcuts():
        if stem == name.lower().strip():
            exact = path
            break
        if all(w in stem for w in words):
            partial.append((len(stem), path))
    if exact:
        return exact
    if partial:
        partial.sort()
        return partial[0][1]
    return None


def _windows():
    """Visible top-level windows: title, pid, process name, hwnd."""
    try:
        import win32gui
        import win32process
    except Exception:
        return []
    out = []

    def collect(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc = psutil.Process(pid).name()
            except psutil.Error:
                proc = ""
            out.append({"hwnd": hwnd, "title": title, "pid": pid, "proc": proc})
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        return out
    return out


def _window_match(title):
    q = (title or "").lower().strip()
    if not q:
        return None
    words = q.split()
    for win in _windows():
        if win["title"].lower() == q:
            return win
    for win in _windows():
        low = win["title"].lower()
        if all(w in low for w in words):
            return win
    return None


def _is_protected(name):
    return (name or "").lower().strip() in _PROTECTED


def _is_own(win):
    if int(win.get("pid") or 0) == os.getpid():
        return True
    proc = (win.get("proc") or "").lower()
    return proc in ("python.exe", "pythonw.exe") and \
        "iris" in (win.get("title") or "").lower()


# ------------------------------------------------------------------ open
def launch_app_raw(name):
    """Start an app with normal user rights. Returns nothing; use open_app.

    Kept separate so ``skills.surface`` can launch an app and then pull its
    window into the interface without the two modules importing in a circle.
    """
    raw = (name or "").strip().strip('"')
    key = raw.lower()
    if not key:
        return "ERROR: say which app to open."
    if key in _APP_EXE:
        for token in _APP_EXE[key]:
            if token.startswith(("ms-", "shell:")) or re.match(r"^[a-z0-9]+:$", token):
                try:
                    _start(token)
                    return "Opened {0}.".format(raw)
                except OSError as e:
                    return "ERROR: {0}".format(e)
            exe = shutil.which(token) or (token if os.path.exists(token) else None)
            if exe:
                try:
                    _start(exe)
                    return "Opened {0}.".format(raw)
                except OSError:
                    continue
    lnk = _find_shortcut(raw)
    if lnk:
        try:
            os.startfile(str(lnk))
            return "Opened {0}.".format(raw)
        except OSError as e:
            return "ERROR: {0}".format(e)
    exe = shutil.which(raw)
    if exe:
        try:
            _start(exe)
            return "Opened {0}.".format(raw)
        except OSError as e:
            return "ERROR: {0}".format(e)
    try:
        _start(raw)
        return "Opened {0}.".format(raw)
    except Exception as e:
        return "ERROR: I could not find '{0}': {1}".format(raw, e)


def launch_file_raw(path):
    """Open a file with its default app. Returns a status string."""
    try:
        os.startfile(str(path))
        return "Opened."
    except OSError as e:
        return "ERROR: {0}".format(e)


def _embed_apps():
    """True only when the user asks for apps to be mirrored inside Iris."""
    return bool((CFG.get("surface") or {}).get("embed_apps", False))


@skill("Open any application by name as a normal, separate Windows window, the way "
       "a person would. Use show_window only when the user asks to bring it into "
       "Iris. Args: name")
def open_app(name):
    if _embed_apps():
        from skills import surface
        return surface.open_app_inside(name)
    return launch_app_raw(name)


@skill("Open absolutely anything as a normal Windows window: an app, a file, a "
       "folder, a website or a Windows settings page. Websites open in real Chrome. "
       "Args: target")
def open_anything(target):
    if _embed_apps():
        from skills import surface
        return surface.open_anything_inside(target)
    raw = str(target or "").strip().strip('"')
    if raw.lower().startswith(("http://", "https://")):
        from skills.browser_agent import browser_open
        return browser_open(raw)
    return open_anything_raw(raw)


def open_anything_raw(target):
    """The old external-open behaviour, kept for compatibility and tests."""
    raw = (target or "").strip().strip('"')
    if not raw:
        return "ERROR: say what to open."
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        try:
            os.startfile(raw)
            return "Opened the link in your default browser."
        except OSError as e:
            return "ERROR: {0}".format(e)
    if low.startswith(("ms-settings:", "shell:", "ms-")) or re.match(r"^[a-z0-9]+:$", low):
        try:
            os.startfile(raw)
            return "Opened {0}.".format(raw)
        except OSError as e:
            return "ERROR: {0}".format(e)
    p = Path(raw).expanduser()
    if p.exists():
        try:
            os.startfile(str(p))
            return "Opened {0}.".format(p.name)
        except OSError as e:
            return "ERROR: {0}".format(e)
    return launch_app_raw(raw)


# ------------------------------------------------------------------ close
@skill("Close any app by name or window title. Works for apps that are not in a "
       "short list. Args: name")
def close_app(name):
    raw = (name or "").strip()
    key = raw.lower().replace(".exe", "")
    if not key:
        return "ERROR: say which app to close."
    wanted = {key + ".exe"}
    wanted |= _PROC_ALIASES.get(key, set())
    killed, protected = 0, []
    for proc in psutil.process_iter(["name", "pid"]):
        pname = (proc.info.get("name") or "").lower()
        if not pname or proc.info.get("pid") == os.getpid():
            continue
        if pname in _PROTECTED:
            if pname in wanted:
                protected.append(pname)
            continue
        if pname in wanted or key in pname or (pname.endswith(".exe") and pname[:-4] in key):
            try:
                proc.terminate()
                killed += 1
            except psutil.Error:
                pass
    if killed:
        deadline = time.time() + 3
        while time.time() < deadline:
            if not any(p.info.get("name", "").lower() in wanted
                       for p in psutil.process_iter(["name"])):
                break
            time.sleep(0.2)
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() in wanted:
                try:
                    proc.kill()
                except psutil.Error:
                    pass
        return "Closed {0} ({1} process{2}).".format(raw, killed, "es" if killed > 1 else "")
    if protected:
        return "ERROR: {0} is a protected Windows process; I will not close it.".format(raw)
    win = _window_match(raw)
    if win:
        return _send_close(win, raw)
    return "ERROR: I could not find anything called '{0}' running.".format(raw)


def _send_close(win, label=""):
    try:
        import win32con
        import win32gui
        win32gui.PostMessage(win["hwnd"], win32con.WM_CLOSE, 0, 0)
        return "Closed the {0} window.".format(label or win["title"])
    except Exception as e:
        return "ERROR: could not close it: {0}".format(e)


@skill("Close a window by (part of) its title. Args: title")
def close_window(title):
    win = _window_match(title)
    if not win:
        return "ERROR: no window matches that title."
    if _is_own(win):
        return "ERROR: that is my own window."
    return _send_close(win, title)


@skill("Control a window. Args: action (focus, minimize, maximize, restore, close), "
       "title (part of the window title)")
def window_action(action, title=""):
    act = (action or "").lower().strip()
    win = _window_match(title)
    if not win:
        return "ERROR: no window matches that title."
    try:
        import win32con
        import win32gui
    except Exception as e:
        return "ERROR: {0}".format(e)
    hwnd = win["hwnd"]
    try:
        if act == "close":
            if _is_own(win):
                return "ERROR: that is my own window."
            return _send_close(win, title)
        if act == "focus":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        elif act == "minimize":
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        elif act == "maximize":
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        elif act == "restore":
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            return "ERROR: action must be focus, minimize, maximize, restore or close."
        return "{0} {1}.".format(act.capitalize(), "done")
    except Exception as e:
        return "ERROR: {0}".format(e)


@skill("Show the desktop. No args.")
def show_desktop():
    import pyautogui
    pyautogui.hotkey("win", "d")
    return "Desktop shown."


@skill("Press a keyboard shortcut, for example win+d, alt+tab, ctrl+shift+esc or "
       "win+l. Args: keys")
def press_keys(keys):
    import pyautogui
    parts = [p.strip() for p in str(keys or "").lower().split("+") if p.strip()]
    if not parts or len(parts) > 4:
        return "ERROR: give a shortcut like win+d."
    try:
        pyautogui.hotkey(*parts)
    except Exception as e:
        return "ERROR: {0}".format(e)
    return "Pressed {0}.".format("+".join(parts))


@skill("List the visible windows with their titles and programs.", observe=True)
def list_windows():
    wins = _windows()
    if not wins:
        return "I could not read the window list."
    lines = ["{0} — {1}".format(w["title"][:60], w["proc"]) for w in wins[:40]]
    return "Open windows:\n" + "\n".join(lines)


@skill("List installed apps (Start-menu shortcuts) so you know what can be opened.",
       observe=True)
def list_apps():
    names = sorted({stem for stem, _ in _shortcuts()})
    if not names:
        return "I could not read the Start menu."
    return "Installed apps: " + ", ".join(names[:90])


# ------------------------------------------------------------------ shell
@skill("Run a PowerShell command on this PC (user rights, no admin) and read its "
       "output. Dangerous disk/user/registry commands are refused. Args: command",
       level=2, observe=True)
def run_command(command):
    cmd = (command or "").strip()
    if not cmd:
        return "ERROR: give me a command."
    if _BLOCKED.search(cmd):
        return "ERROR: that command is blocked because it could damage the PC."
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "ERROR: the command took longer than 45 seconds."
    except Exception as e:
        return "ERROR: {0}".format(e)
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    text = out or err or "(no output)"
    if result.returncode and not out:
        text = "exit {0}: {1}".format(result.returncode, text)
    return "Command output (information only): " + text[:3000]
