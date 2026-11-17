"""Iris' own display surface: everything she opens is shown inside her window.

A *surface* is one tab inside Iris' interface. Skills in this module publish a
surface description on the bus and ``interfaces/ui.py`` renders it. Supported
kinds:

    browser   live headless-browser mirror (also used for PDFs and media)
    image     a picture, scaled to fit
    text      documents, code, logs, notes
    folder    a browsable directory listing
    app       an application window, embedded natively or mirrored live
    screen    a live view of the desktop
    results   search-result cards
    dashboard battery, CPU, RAM, disk, network and top processes
    info      a small status card

No external window is created for web pages, files, folders or apps. Native
apps are mirrored live into the surface (PrintWindow/BitBlt frames), with
clicks, scrolling, keys and typing forwarded to the real window on a
best-effort basis. Nothing needs admin rights and no window is re-parented.
"""

import datetime
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import psutil

from core import bus
from core.config import CFG, DATA, ROOT, WORKSPACE
from core.skill import skill

_SURFACE = CFG.get("surface") or {}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
             ".ico", ".ppm", ".pgm"}
TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml",
            ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".sql", ".sh",
            ".ps1", ".bat", ".cmd", ".c", ".h", ".cpp", ".hpp", ".java", ".go",
            ".rs", ".rb", ".php", ".swift", ".kt", ".lua", ".r", ".tex", ".diff",
            ".patch", ".gitignore", ".dockerfile", ".makefile"}
BROWSER_EXT = {".pdf", ".html", ".htm", ".svg", ".mp3", ".wav", ".ogg", ".m4a",
               ".flac", ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
OFFICE_EXT = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
              ".odp", ".rtf"}

_BROWSERS = {"chrome", "google chrome", "edge", "microsoft edge", "firefox",
             "brave", "opera", "vivaldi", "browser", "web browser", "internet"}
_SETTINGS = {"settings", "windows settings", "system settings", "setting"}
_FILES = {"explorer", "file explorer", "files", "file manager", "this pc",
          "my computer", "downloads", "documents"}
_SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google maps": "https://maps.google.com",
    "maps": "https://maps.google.com",
    "wikipedia": "https://www.wikipedia.org",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "stack overflow": "https://stackoverflow.com",
    "spotify": "https://open.spotify.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "instagram": "https://www.instagram.com",
    "whatsapp": "https://web.whatsapp.com",
    "linkedin": "https://www.linkedin.com",
    "chatgpt": "https://chatgpt.com",
}


# ------------------------------------------------------------------ plumbing
def publish(payload):
    bus.publish("surface", payload)


def _resolve(path):
    raw = str(path or "").strip().strip('"').strip("'")
    if not raw:
        return None
    raw = os.path.expandvars(os.path.expanduser(raw))
    p = Path(raw)
    candidates = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates += [Path.cwd() / p, WORKSPACE / p, ROOT / p, Path.home() / p]
    for item in candidates:
        try:
            if item.exists():
                return item.resolve()
        except OSError:
            continue
    # A bare name from a recent screenshot or download?
    for folder in (DATA / "screenshots", Path.home() / "Downloads",
                   Path.home() / "Desktop", Path.home() / "Documents"):
        try:
            for hit in folder.rglob(p.name):
                if hit.exists():
                    return hit.resolve()
        except OSError:
            continue
    return None


def _live_allowed():
    return bool(_SURFACE.get("enabled", True))


def _start_live(payload):
    """Kick off the frame worker for app windows and the desktop."""
    if not _live_allowed():
        return
    kind = payload.get("kind")
    if payload.get("state") == "closed":
        _stop_live()
        return
    try:
        from skills import capture
        if kind == "screen" and payload.get("live", True):
            capture.start_screen()
        elif kind == "app" and payload.get("hwnd"):
            capture.start_window(payload["hwnd"], payload.get("title", ""))
    except Exception:  # noqa: BLE001 - a mirror is never critical
        pass


def _stop_live():
    try:
        from skills import capture
        capture.stop()
    except Exception:  # noqa: BLE001
        pass


def _present(payload):
    payload = dict(payload or {})
    payload.setdefault("state", "open")
    publish(payload)
    _start_live(payload)


def _short(text, limit=120):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _host(url):
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url


def _is_url(text):
    t = str(text or "").strip()
    if t.startswith(("http://", "https://")):
        return True
    return (bool(re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[/:?#].*)?$", t, re.I))
            and " " not in t and not Path(t).exists())


# ------------------------------------------------------------------ browsers
def _browser():
    from skills import browser_agent
    return browser_agent


def open_browser(url=None):
    if url:
        return _browser().browser_open(url)
    return _browser().browser_home()


# --------------------------------------------------------------------- text
@skill("Show text, notes, an answer, a list or a short document inside Iris's own "
       "interface. Args: text, title (optional)")
def show_text(text, title="Note"):
    body = str(text or "")
    if not body.strip():
        return "ERROR: there is no text to show."
    _present({"id": "text", "kind": "text", "title": title or "Note",
              "subtitle": "{0} characters".format(len(body)), "text": body})
    return "Showing that inside my interface."


@skill("Show an image file inside Iris's own interface. Args: path, title (optional)")
def show_image(path, title=""):
    p = _resolve(path)
    if p is None or not p.is_file():
        return "ERROR: I could not find that image."
    if p.suffix.lower() not in IMAGE_EXT:
        return show_file(str(p), title=title)
    _present({"id": "image", "kind": "image", "title": title or p.name,
              "subtitle": str(p), "path": str(p)})
    return "Showing {0} inside my interface.".format(p.name)


@skill("Show a file inside Iris's own interface. Images, text, code, PDFs, video "
       "and audio are rendered there directly. Args: path, title (optional)")
def show_file(path, title=""):
    p = _resolve(path)
    if p is None:
        return "ERROR: I could not find '{0}'.".format(path)
    if p.is_dir():
        return show_folder(str(p))
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return show_image(str(p), title=title)
    if ext in BROWSER_EXT:
        bus.publish("toast", "Opening {0} inside my interface.".format(p.name))
        return _browser().browser_open_file(str(p))
    if ext in TEXT_EXT or p.name.lower() in (".env", "dockerfile", "makefile",
                                             "readme", "license"):
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return "ERROR: could not read it: {0}".format(e)
        if len(raw) > 400_000:
            raw = raw[:400_000] + "\n\n… (truncated)"
        _present({"id": "text", "kind": "text", "title": title or p.name,
                  "subtitle": str(p), "text": raw, "path": str(p)})
        return "Showing {0} inside my interface.".format(p.name)
    # Office documents or unknown types: open with the default app, then bring
    # that app's window inside the interface.
    return _present_file_with_app(p, title)


def _present_file_with_app(p, title=""):
    from skills import laptop
    before = {w["hwnd"] for w in laptop._windows()}
    try:
        if hasattr(laptop, "launch_file_raw"):
            message = laptop.launch_file_raw(str(p))
        else:  # pragma: no cover - older laptop module
            os.startfile(str(p))
            message = "Opened."
    except Exception as e:  # noqa: BLE001
        return "ERROR: could not open it: {0}".format(e)
    if str(message).startswith("ERROR"):
        return message
    label = title or p.name
    _present({"id": "app", "kind": "app", "state": "loading", "title": label,
              "subtitle": str(p), "text": "Bringing the app inside…"})
    threading.Thread(target=_find_and_show, args=(label, p.stem, before),
                     daemon=True, name="surface-file").start()
    return "Opening {0} inside my interface.".format(p.name)


# ------------------------------------------------------------------- folder
def _entry(path):
    try:
        st = path.stat()
    except OSError:
        return None
    is_dir = path.is_dir()
    return {
        "name": path.name or str(path),
        "path": str(path),
        "kind": "dir" if is_dir else "file",
        "ext": "" if is_dir else path.suffix.lower().lstrip("."),
        "size": 0 if is_dir else int(st.st_size),
        "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%d %b %Y  %H:%M"),
    }


@skill("Browse a folder inside Iris's own interface, with clickable files and "
       "subfolders. Args: path (optional, defaults to your home folder)")
def show_folder(path=""):
    p = _resolve(path) if str(path or "").strip() else Path.home()
    if p is None:
        return "ERROR: I could not find that folder."
    if not p.is_dir():
        return show_file(str(p))
    try:
        entries = list(p.iterdir())
    except OSError as e:
        return "ERROR: could not read that folder: {0}".format(e)
    items = []
    for child in entries:
        item = _entry(child)
        if item:
            items.append(item)
    items.sort(key=lambda x: (x["kind"] != "dir", x["name"].lower()))
    _present({"id": "folder", "kind": "folder", "title": p.name or str(p),
              "subtitle": str(p), "path": str(p), "items": items[:300],
              "total": len(items)})
    return "Showing {0} inside my interface ({1} items).".format(p, len(items))


# ---------------------------------------------------------------- dashboard
def _dashboard_data():
    data = {"at": datetime.datetime.now().strftime("%H:%M:%S"),
            "date": datetime.datetime.now().strftime("%A, %d %B %Y")}
    try:
        battery = psutil.sensors_battery()
        data["battery"] = None if battery is None else round(battery.percent)
        data["plugged"] = bool(battery and battery.power_plugged)
    except Exception:  # noqa: BLE001
        data["battery"] = None
    try:
        data["cpu"] = round(psutil.cpu_percent(interval=0.35))
        data["cores"] = psutil.cpu_count(logical=True)
        data["freq"] = round((psutil.cpu_freq().current if psutil.cpu_freq() else 0))
    except Exception:  # noqa: BLE001
        data["cpu"], data["cores"], data["freq"] = 0, 0, 0
    try:
        vm = psutil.virtual_memory()
        data["ram"] = round(vm.percent)
        data["ram_used"] = round(vm.used / 1e9, 1)
        data["ram_total"] = round(vm.total / 1e9, 1)
    except Exception:  # noqa: BLE001
        data["ram"] = data["ram_used"] = data["ram_total"] = 0
    try:
        du = psutil.disk_usage(str(Path.home().anchor or "/"))
        data["disk"] = round(du.percent)
        data["disk_free"] = round(du.free / 1e9, 1)
        data["disk_total"] = round(du.total / 1e9, 1)
    except Exception:  # noqa: BLE001
        data["disk"] = data["disk_free"] = data["disk_total"] = 0
    try:
        io = psutil.net_io_counters()
        data["net_up"] = round(io.bytes_sent / 1e6, 1)
        data["net_down"] = round(io.bytes_recv / 1e6, 1)
    except Exception:  # noqa: BLE001
        data["net_up"] = data["net_down"] = 0
    try:
        data["uptime"] = int(time.time() - psutil.boot_time())
    except Exception:  # noqa: BLE001
        data["uptime"] = 0
    procs = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            info = proc.info
            if not info.get("memory_info"):
                continue
            procs.append((info["memory_info"].rss, info.get("name") or "?"))
        except (psutil.Error, OSError):
            continue
    procs.sort(reverse=True)
    data["processes"] = [{"name": n, "mb": round(m / 1e6)} for m, n in procs[:6]]
    return data


@skill("Show a live system dashboard inside Iris's interface: battery, CPU, RAM, "
       "disk, network, uptime and the heaviest apps. No args.", observe=True)
def show_dashboard():
    data = _dashboard_data()
    _present({"id": "dashboard", "kind": "dashboard", "title": "System",
              "subtitle": "LIVE TELEMETRY // THIS LAPTOP", "data": data,
              "live": True})
    return ("System dashboard is up: CPU {0}%, RAM {1}%, disk {2}%.".format(
        data.get("cpu", 0), data.get("ram", 0), data.get("disk", 0)))


# ------------------------------------------------------------------- screen
@skill("Show a live view of the whole screen inside Iris's interface. Args: mode "
       "(live or snapshot, default live)", observe=True)
def show_screen(mode="live"):
    live = str(mode).lower().strip() != "snapshot"
    payload = {"id": "screen", "kind": "screen", "title": "Desktop",
               "subtitle": "LIVE SCREEN // {0}".format(
                   "STREAM" if live else "SNAPSHOT"),
               "live": live}
    if not live:
        try:
            from PIL import ImageGrab
            folder = DATA / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            shot = folder / datetime.datetime.now().strftime(
                "screen_%Y%m%d_%H%M%S.png")
            try:
                ImageGrab.grab(all_screens=True).save(shot)
            except TypeError:                  # older Pillow
                ImageGrab.grab().save(shot)
            payload["path"] = str(shot)
        except Exception:  # noqa: BLE001 - fall back to a live stream
            payload["live"] = True
    _present(payload)
    return "Showing the screen inside my interface."


# ------------------------------------------------------------------ windows
def _match_window(title):
    from skills import laptop
    return laptop._window_match(title)


def _publish_window(win, label=""):
    _present({"id": "app", "kind": "app", "title": label or win["title"],
              "subtitle": "{0}  ·  PID {1}".format(win.get("proc") or "app",
                                                   win.get("pid") or "?"),
              "hwnd": int(win["hwnd"]), "proc": win.get("proc", ""),
              "window_title": win.get("title", "")})
    return "Showing {0} inside my interface.".format(label or win["title"])


@skill("Bring an open window into Iris's own interface by (part of) its title. "
       "Args: title", observe=True)
def show_window(title):
    win = _match_window(title)
    if not win:
        return "ERROR: no open window matches that title."
    return _publish_window(win, title)


def _find_new_window(before, key, timeout=14.0):
    """Wait for a newly launched app's first visible window."""
    from skills import laptop
    words = [w for w in re.findall(r"[a-z0-9]+", (key or "").lower())
             if w not in ("open", "the", "app", "application")]
    aliases = set()
    for word in words:
        aliases |= laptop._PROC_ALIASES.get(word, set())
    deadline = time.time() + timeout
    while time.time() < deadline:
        wins = laptop._windows()
        for win in wins:
            if int(win["hwnd"]) in before:
                continue
            pat = (win.get("title", "") + " " + win.get("proc", "")).lower()
            if not words or all(w in pat for w in words) or \
                    any(a in pat for a in aliases):
                return win
        # Some apps focus an existing window instead of creating a new one.
        for win in wins:
            pat = (win.get("title", "") + " " + win.get("proc", "")).lower()
            if words and all(w in pat for w in words):
                return win
        time.sleep(0.4)
    return None


def _find_and_show(label, key, before):
    win = _find_new_window(before, key)
    if win:
        _publish_window(win, label)
        return
    _present({"id": "app", "kind": "info", "title": label,
              "subtitle": "APP STATUS",
              "text": ("{0} is running, but it has not opened a window I can "
                       "display yet.\n\nIt may still be starting, or it may live "
                       "only in the notification area. Ask me to show its window "
                       "by name once it appears.").format(label)})


def open_app_inside(name):
    """Launch or reveal an application inside Iris' interface."""
    raw = str(name or "").strip().strip('"')
    key = raw.lower().strip()
    if not key:
        return "ERROR: say which app to open."
    if key in _BROWSERS:
        return open_browser()
    if key in _SITES:
        return open_browser(_SITES[key])
    if key in _SETTINGS or key.startswith("ms-settings:"):
        return show_settings(raw)
    if key in _FILES:
        target = {"downloads": Path.home() / "Downloads",
                  "documents": Path.home() / "Documents"}.get(key, Path.home())
        return show_folder(str(target))

    existing = _match_window(raw)
    if existing:
        return _publish_window(existing, raw)

    from skills import laptop
    before = {w["hwnd"] for w in laptop._windows()}
    message = laptop.launch_app_raw(raw)
    if str(message).startswith("ERROR"):
        return message
    _present({"id": "app", "kind": "app", "state": "loading", "title": raw,
              "subtitle": "LAUNCHING", "text": "Bringing {0} inside…".format(raw)})
    threading.Thread(target=_find_and_show, args=(raw, raw, before),
                     daemon=True, name="surface-app").start()
    return "Opening {0} inside my interface.".format(raw)


def open_anything_inside(target):
    """Open a URL, file, folder, settings page or app - always inside Iris."""
    raw = str(target or "").strip().strip('"')
    if not raw:
        return "ERROR: say what to open."
    low = raw.lower()
    if low.startswith(("http://", "https://")):
        return open_browser(raw)
    if low in _SITES:
        return open_browser(_SITES[low])
    if low.startswith("ms-settings:") or low.startswith("shell:") or \
            re.match(r"^[a-z0-9]+:$", low):
        if low.startswith("ms-settings:"):
            return show_settings(raw)
        try:
            os.startfile(raw)
        except OSError as e:
            return "ERROR: {0}".format(e)
        return "Opened {0}.".format(raw)
    if _is_url(raw):
        return open_browser(raw)
    p = _resolve(raw)
    if p is not None:
        return show_folder(str(p)) if p.is_dir() else show_file(str(p))
    return open_app_inside(raw)


def open_path_inside(target):
    """Callback for clicks inside a folder/info surface."""
    raw = str(target or "").strip()
    low = raw.lower()
    if low.startswith("ms-settings:"):
        return show_settings(raw)
    if low.startswith(("http://", "https://")):
        return open_browser(raw)
    p = _resolve(raw)
    if p is not None:
        return show_folder(str(p)) if p.is_dir() else show_file(str(p))
    return open_anything_inside(raw)


# ----------------------------------------------------------------- settings
def show_settings(page):
    from skills import system
    uri = system._page_uri(page) or (page if str(page).startswith("ms-settings:")
                                     else None)
    if not uri:
        return "ERROR: I could not find that settings page."
    from skills import laptop
    before = {w["hwnd"] for w in laptop._windows()}
    try:
        os.startfile(uri)
    except OSError as e:
        return "ERROR: {0}".format(e)
    _present({"id": "app", "kind": "app", "state": "loading",
              "title": "Settings", "subtitle": uri,
              "text": "Opening Windows Settings inside…"})
    threading.Thread(target=_find_and_show, args=("Settings", "settings", before),
                     daemon=True, name="surface-settings").start()
    return "Opening {0} settings inside my interface.".format(page)


# -------------------------------------------------------------------- close
@skill("Close the surface (browser, image, document, app or dashboard) that is "
       "currently showing in Iris's interface. No args.")
def close_surface():
    publish({"state": "closed"})
    _stop_live()
    return "Closed the surface."


# ------------------------------------------------------------------- status
def surface_open(payload):
    """Convenience wrapper used by other skills (basic.py, websearch.py)."""
    publish(payload)
