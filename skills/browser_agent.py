"""Iris drives the real Google Chrome over the DevTools protocol.

Chrome is launched (or attached to) with a loopback debugging port, then every
action runs on one Playwright worker thread. The user sees and can use the same
Chrome window; Iris can open tabs, navigate, click, type, read pages, run
JavaScript and manage downloads without a bundled browser.

The old ``browser_*`` tool names are kept so the persona and the agent loop do
not need to change. A JPEG snapshot can still be mirrored into Iris' interface
when ``browser.show_in_ui`` is enabled, but it is off by default now.
"""

import atexit
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from queue import Empty, Queue

import psutil

from core import bus, net
from core.config import CFG, DATA, ROOT
from core.skill import skill

log = logging.getLogger("iris")

CONFIRM = None            # main.py sets this to its confirm() function
_pw = _browser = _ctx = _page = None
_last_shot = None
_SHOTS = DATA / "browser_shots"
_RISKY = re.compile(r"\b(buy|pay|purchase|order|checkout|delete|remove|send|transfer|"
                    r"confirm|place order|submit)\b", re.I)
_fallback_noted = False

_JS_STATE = r"""() => {
  const sel = 'a[href], button, input, textarea, select, summary, [role=button], [role=link], [role=tab], [role=textbox], [role=searchbox], [contenteditable=true]';
  document.querySelectorAll('[data-iris-id]').forEach(e => e.removeAttribute('data-iris-id'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > window.innerHeight) continue;
    n += 1;
    el.setAttribute('data-iris-id', String(n));
    const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.value || el.title || el.alt || '').trim().replace(/\s+/g, ' ').slice(0, 45);
    const type = el.type ? '(' + el.type + ')' : '';
    out.push('[' + n + '] ' + el.tagName.toLowerCase() + type + ' ' + label);
    if (n >= 25) break;
  }
  return out;
}"""

_JS_FIND = r"""(text) => {
  const q = String(text || '').toLowerCase();
  document.querySelectorAll('[data-iris-id]').forEach(e => e.removeAttribute('data-iris-id'));
  const sel = 'a[href], button, input, textarea, select, summary, [role=button], [role=link], [role=tab], [role=textbox], [role=searchbox], [contenteditable=true]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.value || el.title || el.alt || '').trim().replace(/\s+/g, ' ');
    if (!label.toLowerCase().includes(q)) continue;
    n += 1;
    el.setAttribute('data-iris-id', String(n));
    const type = el.type ? '(' + el.type + ')' : '';
    out.push('[' + n + '] ' + el.tagName.toLowerCase() + type + ' ' + label.slice(0, 60));
    if (n >= 25) break;
  }
  return out;
}"""

_JS_LINKS = r"""() => {
  document.querySelectorAll('[data-iris-id]').forEach(e => e.removeAttribute('data-iris-id'));
  const out = [];
  let n = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    const r = a.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    n += 1;
    a.setAttribute('data-iris-id', String(n));
    const label = (a.innerText || a.getAttribute('aria-label') || a.href || '').trim().replace(/\s+/g, ' ').slice(0, 50);
    out.push('[' + n + '] ' + label + ' -> ' + String(a.href).slice(0, 140));
    if (n >= 30) break;
  }
  return out;
}"""

# --------------------------------------------------------------- worker thread
_req = Queue()
_worker = None
_worker_lock = threading.Lock()


def _loop():
    while True:
        try:
            fn, box, done = _req.get(timeout=120)
        except Empty:
            continue
        if fn is None:
            done.set()
            return
        try:
            box["result"] = fn()
        except BaseException as e:      # noqa: BLE001 - reported to the model
            box["error"] = e
        finally:
            done.set()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True, name="iris-browser")
            _worker.start()
        return _worker


def _call(fn, timeout=80):
    """Run a browser action on the worker thread and return its text result."""
    _ensure_worker()
    box, done = {}, threading.Event()
    _req.put((fn, box, done))
    if not done.wait(timeout):
        return ("ERROR: Chrome is taking too long. Tell the user and try again, "
                "or use web_search instead.")
    if "error" in box:
        log.warning("chrome action failed: %s", box["error"])
        return "ERROR: {0}".format(box["error"])
    return box["result"]


# ------------------------------------------------------------------ lifecycle
def _shutdown():
    """Disconnect Playwright. The user's Chrome window is left open."""
    global _pw, _browser, _ctx, _page, _last_shot
    try:
        if _pw is not None:
            _pw.stop()
    except Exception:  # noqa: BLE001
        pass
    _pw = _browser = _ctx = _page = _last_shot = None
    bus.publish("view", {"state": "closed"})


def _shutdown_sync():
    try:
        _call(_shutdown, timeout=12)
    except Exception:  # noqa: BLE001
        pass


atexit.register(_shutdown_sync)


# ------------------------------------------------------------------- chrome
def _chrome_cfg():
    return CFG.get("chrome") or {}


def _port():
    try:
        return int(_chrome_cfg().get("debug_port", 9222) or 9222)
    except (TypeError, ValueError):
        return 9222


def _endpoint():
    return "http://127.0.0.1:{0}".format(_port())


def _endpoint_ok():
    """True only when this loopback port speaks the Chrome DevTools protocol."""
    try:
        with urllib.request.urlopen(_endpoint() + "/json/version", timeout=0.8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return "chrome" in str(data.get("Browser") or "").lower()
    except Exception:  # noqa: BLE001 - not running, or not Chrome
        return False


def _chrome_path():
    configured = str(_chrome_cfg().get("exe") or "").strip()
    candidates = [
        configured,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(os.path.expandvars(path)):
            return os.path.expandvars(path)
    return shutil.which("chrome") or shutil.which("chrome.exe")


def _chrome_running():
    try:
        for proc in psutil.process_iter(["name"]):
            if str(proc.info.get("name") or "").lower() in ("chrome.exe", "chrome"):
                return True
    except Exception:  # noqa: BLE001 - psutil is best effort
        pass
    return False


def _expand(path):
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def _system_user_data():
    return _expand(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                "Google", "Chrome", "User Data"))


def _last_profile(user_data):
    """Chrome remembers the last profile in Local State."""
    try:
        with open(Path(user_data) / "Local State", encoding="utf-8") as f:
            return str((json.load(f).get("profile") or {}).get("last_used") or "")
    except Exception:  # noqa: BLE001 - a fresh profile has no Local State yet
        return ""


def _resolve_profile(want):
    """Accept a nickname from config, or an actual 'Profile N' folder."""
    text = str(want or "").strip()
    if not text:
        return ""
    profiles = _chrome_cfg().get("profiles") or {}
    lowered = text.lower()
    for nick, folder in profiles.items():
        if lowered == str(nick).lower():
            return str(folder)
    for nick, folder in profiles.items():
        nick_l = str(nick).lower()
        if lowered in nick_l or nick_l in lowered:
            return str(folder)
    return text


def _target(kind):
    """Return (user_data_dir, profile_directory, label)."""
    cfg = _chrome_cfg()
    choice = str(cfg.get("user_data_dir") or "iris").strip()
    profile = _resolve_profile(cfg.get("profile_directory")
                               or cfg.get("default_profile"))
    if kind == "system" or choice.lower() == "system":
        data = _system_user_data()
        return data, profile or _last_profile(data), "system"
    if choice.lower() in ("", "iris", "default"):
        data = DATA / "chrome-profile"
    else:
        data = _expand(choice)
    return data, profile, "iris"


def _note_fallback():
    global _fallback_noted
    if _fallback_noted:
        return
    _fallback_noted = True
    bus.publish("toast", "Using Iris's Chrome profile; close Chrome once to let "
                         "me use yours.")


def _spawn_chrome(user_data, profile):
    exe = _chrome_path()
    if not exe:
        raise RuntimeError("Google Chrome is not installed, or chrome.exe was not found.")
    Path(user_data).mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        "--remote-debugging-port={0}".format(_port()),
        "--user-data-dir={0}".format(user_data),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-notifications",
        "--disable-blink-features=AutomationControlled",
    ]
    if profile:
        args.append("--profile-directory={0}".format(profile))
    args.append("about:blank")
    log.info("starting Chrome: %s", " ".join(args[:3]))
    subprocess.Popen(args, close_fds=True)
    try:
        timeout = max(4.0, float(_chrome_cfg().get("start_timeout", 12.0)))
    except (TypeError, ValueError):
        timeout = 12.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _endpoint_ok():
            return True
        time.sleep(0.25)
    return False


def _close_chrome():
    """Only called when the user explicitly allowed it in the config."""
    try:
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/T"],
                       capture_output=True, timeout=12)
        time.sleep(1.5)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not close Chrome: %s", exc)
        return False


def _launch():
    """Attach to Chrome, or start it with a loopback DevTools port."""
    global _pw, _browser, _ctx
    from playwright.sync_api import sync_playwright
    if _pw is None:
        _pw = sync_playwright().start()
    cfg = _chrome_cfg()
    if not _endpoint_ok():
        choice = str(cfg.get("user_data_dir") or "iris").strip().lower()
        kind = "system" if choice == "system" else "iris"
        if kind == "system" and _chrome_running():
            if bool(cfg.get("close_existing", False)) and CONFIRM and \
                    CONFIRM("Chrome is open. Close it so I can control your profile?"):
                _close_chrome()
            else:
                _note_fallback()
                kind = "iris"
        user_data, profile, label = _target(kind)
        started = _spawn_chrome(user_data, profile)
        if not started and label == "system":
            _note_fallback()
            user_data, profile, label = _target("iris")
            started = _spawn_chrome(user_data, profile)
        if not started:
            raise RuntimeError(
                "I could not start Chrome with the DevTools port. Close Chrome "
                "and try again, or set chrome.user_data_dir to 'iris'.")
        log.info("Chrome ready on %s (%s profile %s)",
                 _endpoint(), label, profile or "default")
    _browser = _pw.chromium.connect_over_cdp(_endpoint())
    contexts = _browser.contexts
    _ctx = contexts[0] if contexts else _browser.new_context()
    _ctx.set_default_timeout(9000)
    return _ctx


def _pages():
    try:
        return [p for p in (_ctx.pages if _ctx else []) if not p.is_closed()]
    except Exception:  # noqa: BLE001
        return []


def _page_ok():
    """Iris' own tab. Never hijack one of the user's tabs on first use."""
    global _page
    try:
        if _page is not None and not _page.is_closed():
            return _page
    except Exception:  # noqa: BLE001
        pass
    if _ctx is None:
        _launch()
    for page in _pages():
        try:
            if (page.url or "") in ("", "about:blank", "chrome://newtab/"):
                _page = page
                return page
        except Exception:  # noqa: BLE001
            continue
    _page = _ctx.new_page()
    return _page


def _new_tab(url=""):
    global _page
    if _ctx is None:
        _launch()
    page = _ctx.new_page()
    _page = page
    if url:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
    try:
        page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass
    return _finish(page)


# ------------------------------------------------------------------- capture
def _prune_shots(keep):
    try:
        shots = sorted(_SHOTS.glob("shot_*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in shots[:-keep]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _capture(page):
    """Save a fresh JPEG and stream it to the interface, if enabled."""
    global _last_shot
    cfg = CFG.get("browser") or {}
    if not bool(cfg.get("show_in_ui", False)):
        return
    try:
        _SHOTS.mkdir(parents=True, exist_ok=True)
        quality = max(40, min(90, int(cfg.get("capture_quality", 70))))
        data = page.screenshot(type="jpeg", quality=quality, full_page=False,
                               animations="disabled", timeout=12000)
        path = _SHOTS / "shot_{0}.jpg".format(time.time_ns())
        path.write_bytes(data)
        _last_shot = path
        _prune_shots(max(4, int(cfg.get("keep_shots", 40))))
        try:
            title = (page.title() or "")[:120]
        except Exception:  # noqa: BLE001
            title = ""
        try:
            size = page.viewport_size or {}
            vw = int(size.get("width") or 0)
            vh = int(size.get("height") or 0)
        except Exception:  # noqa: BLE001
            vw = vh = 0
        bus.publish("view", {"state": "view", "path": str(path),
                             "url": (page.url or "")[:300], "title": title,
                             "vw": vw, "vh": vh})
        bus.publish("surface", {"id": "browser", "kind": "browser",
                                "state": "open", "path": str(path),
                                "url": (page.url or "")[:300], "title": title,
                                "vw": vw, "vh": vh})
    except Exception as e:  # noqa: BLE001
        log.debug("screenshot failed: %s", e)


# --------------------------------------------------------------------- state
def _state(page=None):
    page = page or _page_ok()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        page.wait_for_timeout(350)
        els = page.evaluate(_JS_STATE)
    except Exception as e:  # noqa: BLE001
        return "Page: {0}\n(could not read the page: {1})".format(
            (page.url or "")[:100], e)
    try:
        title = page.title()[:80]
    except Exception:  # noqa: BLE001
        title = ""
    return ("Page: {0} ({1})\nElements you can use:\n{2}").format(
        title, (page.url or "")[:100], "\n".join(els))


def _finish(page):
    """Settle, snapshot into the UI (optional) and describe the new page."""
    try:
        page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass
    _capture(page)
    return _state(page)


def _settle(n_before):
    global _page
    try:
        if _ctx is not None and len(_ctx.pages) > n_before:
            _page = _ctx.pages[-1]
            _page.wait_for_load_state("domcontentloaded", timeout=8000)
            _page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass


def _num(x):
    try:
        return int(float(str(x).strip().strip("[]")))
    except (TypeError, ValueError):
        return None


def _ask(question):
    return bool(CONFIRM and CONFIRM(question))


def _goto(url):
    page = _page_ok()
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    return _finish(page)


def _goto_local(path):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (ROOT / p)
    p = p.resolve()
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.is_dir():
        raise IsADirectoryError(str(p))
    page = _page_ok()
    page.goto(p.as_uri(), wait_until="domcontentloaded", timeout=25000)
    return _finish(page)


def _element_label(x, y):
    try:
        return str(_page_ok().evaluate(
            """([x, y]) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return '';
                return String(el.innerText || el.getAttribute('aria-label') ||
                              el.value || el.title || '').trim().slice(0, 120);
            }""", [x, y]) or "")
    except Exception:  # noqa: BLE001
        return ""


def _click_at(x, y):
    page = _page_ok()
    label = _element_label(x, y)
    if _RISKY.search(label) and not _ask("Click '{0}'?".format(label[:80] or "there")):
        return "The user said no."
    before = len(_pages())
    page.mouse.click(float(x), float(y))
    _settle(before)
    return _finish(_page_ok())


def _scroll_at(x, y, dy):
    page = _page_ok()
    try:
        page.mouse.move(float(x), float(y))
    except Exception:  # noqa: BLE001
        pass
    page.mouse.wheel(0, float(dy))
    return _finish(page)


def _type_text(text, submit=False):
    page = _page_ok()
    page.keyboard.type(str(text))
    if submit:
        page.keyboard.press("Enter")
    return _finish(page)


def _press(key):
    page = _page_ok()
    page.keyboard.press(str(key))
    return _finish(page)


def _tabs_text():
    pages = _pages()
    if not pages:
        return "No Chrome tabs are open."
    current = _page
    lines = []
    for index, page in enumerate(pages, 1):
        try:
            title = (page.title() or "")[:70]
            url = (page.url or "")[:110]
        except Exception:  # noqa: BLE001
            title, url = "", ""
        mark = " (active)" if page is current else ""
        lines.append("{0}. {1} - {2}{3}".format(index, title or "(untitled)",
                                                url, mark))
    return "Chrome tabs:\n" + "\n".join(lines)


def _switch_tab(number):
    pages = _pages()
    index = _num(number)
    if index is None or not 1 <= index <= len(pages):
        return "ERROR: give a tab number between 1 and {0}.".format(len(pages))
    global _page
    _page = pages[index - 1]
    try:
        _page.bring_to_front()
    except Exception:  # noqa: BLE001
        pass
    return _finish(_page)


def _close_tab(number=""):
    global _page
    pages = _pages()
    if len(pages) <= 1:
        return "ERROR: that is the last tab; leave it open."
    index = _num(number) if str(number).strip() else len(pages)
    if index is None or not 1 <= index <= len(pages):
        return "ERROR: give a tab number between 1 and {0}.".format(len(pages))
    page = pages[index - 1]
    try:
        page.close()
    except Exception as exc:  # noqa: BLE001
        return "ERROR: could not close that tab: {0}".format(exc)
    if page is _page:
        _page = None
    return _tabs_text()


def _find(text):
    page = _page_ok()
    label = str(text or "").strip()
    if not label:
        return "ERROR: give me something to look for."
    try:
        matches = page.evaluate(_JS_FIND, label)
    except Exception as exc:  # noqa: BLE001
        return "ERROR: could not search the page: {0}".format(exc)
    if not matches:
        return "No matches for '{0}'. Look at the page again.".format(label)
    return "Matches for '{0}':\n{1}".format(label, "\n".join(matches))


def _links():
    page = _page_ok()
    try:
        items = page.evaluate(_JS_LINKS)
    except Exception as exc:  # noqa: BLE001
        return "ERROR: could not list links: {0}".format(exc)
    if not items:
        return "No links found on this page."
    return "Links on this page:\n" + "\n".join(items)


# -------------------------------------------------------------------- skills
@skill("Open a page in Google Chrome. Iris controls the same visible Chrome "
       "window the user can see, so they can watch or take over. Use this for "
       "ANY web task. Args: url", observe=True)
def browser_open(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not net.is_public_url(url):
        return "ERROR: that address is not allowed."
    return _call(lambda: _goto(url))


@skill("Open a fresh tab in Google Chrome, optionally at a URL. Use this when "
       "the user says 'open Chrome' or wants a new tab. Args: url (optional)",
       observe=True)
def browser_home(url=""):
    target = str(url or "").strip()
    if target and not target.startswith(("http://", "https://")):
        target = "https://" + target
    if target and not net.is_public_url(target):
        return "ERROR: that address is not allowed."
    return _call(lambda: _new_tab(target or "https://www.google.com"))


@skill("Show a local file in Chrome (images, PDFs, video, text). Args: path",
       observe=True)
def browser_open_file(path):
    if not bool((CFG.get("browser") or {}).get("local_files", True)):
        return "ERROR: local files are disabled in the config."
    return _call(lambda: _goto_local(path))


def browser_control(action, **kwargs):
    """Small dispatcher used by the interface's own browser controls."""
    action = str(action or "").lower().strip()
    if action == "open":
        return browser_open(kwargs.get("url", ""))
    if action == "home":
        return browser_home()
    if action == "search":
        return browser_search(kwargs.get("query", ""))
    if action == "look":
        return browser_look()
    if action == "back":
        return browser_back()
    if action == "forward":
        return browser_forward()
    if action == "reload":
        return browser_reload()
    if action == "open_local":
        return browser_open_file(kwargs.get("path", ""))
    if action == "tabs":
        return browser_tabs()
    if action == "new_tab":
        return browser_home(kwargs.get("url", ""))
    if action == "switch_tab":
        return browser_switch_tab(kwargs.get("number", ""))
    if action == "close_tab":
        return browser_close_tab(kwargs.get("number", ""))
    if action == "click_at":
        try:
            x, y = float(kwargs.get("x", 0)), float(kwargs.get("y", 0))
        except (TypeError, ValueError):
            return "ERROR: bad click coordinates."
        return _call(lambda: _click_at(x, y))
    if action == "scroll_at":
        try:
            x, y = float(kwargs.get("x", 0)), float(kwargs.get("y", 0))
            dy = float(kwargs.get("dy", 600))
        except (TypeError, ValueError):
            return "ERROR: bad scroll values."
        return _call(lambda: _scroll_at(x, y, dy))
    if action == "type":
        return _call(lambda: _type_text(kwargs.get("text", ""),
                                        bool(kwargs.get("submit", False))))
    if action == "key":
        return _call(lambda: _press(kwargs.get("key", "Enter")))
    if action == "close":
        _shutdown_sync()
        return "Disconnected from Chrome."
    return "ERROR: unknown browser action."


@skill("Search the web in Google Chrome and show the real results page there. "
       "Use web_search for a quick spoken answer, or this when the user wants "
       "to see and click results. Args: query", observe=True)
def browser_search(query):
    q = (query or "").strip()
    if not q:
        return "ERROR: give me something to search for."
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(q)
    return _call(lambda: _goto(url), timeout=80)


@skill("Look at the current Chrome tab again and read its controls. No args.",
       observe=True)
def browser_look():
    return _call(lambda: _finish(_page_ok()))


@skill("Click an element in Chrome by its number from the latest page state. "
       "Args: number", observe=True)
def browser_click(number):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."

    def _do():
        page = _page_ok()
        loc = page.locator('[data-iris-id="{0}"]'.format(n)).first
        try:
            label = (loc.get_attribute("aria-label", timeout=2500)
                     or loc.inner_text(timeout=2500) or "")[:80]
        except Exception:  # noqa: BLE001
            return "ERROR: that element is gone. Look at the page again."
        if _RISKY.search(label) and not _ask("Click '{0}'?".format(label)):
            return "The user said no."
        before = len(_pages())
        try:
            loc.click(timeout=7000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not click it: {0}".format(e)
        _settle(before)
        return _finish(_page_ok())
    return _call(_do)


@skill("Click a link or button in Chrome by its visible text, for example "
       "'Sign in' or 'Next'. Args: text", observe=True)
def browser_click_text(text):
    label = (text or "").strip()
    if not label:
        return "ERROR: give me the text to click."

    def _do():
        page = _page_ok()
        loc = page.get_by_text(label, exact=False).first
        try:
            shown = (loc.inner_text(timeout=2500) or label)[:80]
        except Exception:  # noqa: BLE001
            return ("ERROR: I could not find '{0}' on the page. Look at the page "
                    "again.").format(label)
        if _RISKY.search(shown) and not _ask("Click '{0}'?".format(shown)):
            return "The user said no."
        before = len(_pages())
        try:
            loc.click(timeout=7000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not click it: {0}".format(e)
        _settle(before)
        return _finish(_page_ok())
    return _call(_do)


@skill("Type text into an input box in Chrome by its number. Set submit to yes "
       "to press Enter afterwards (for example to run a search). Args: number, "
       "text, submit (yes or no)", observe=True)
def browser_type(number, text, submit="no"):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."
    press = str(submit).lower().strip() in ("yes", "true", "1", "enter")

    def _do():
        page = _page_ok()
        loc = page.locator('[data-iris-id="{0}"]'.format(n)).first
        try:
            if (loc.get_attribute("type", timeout=2000) or "").lower() == "password":
                return "ERROR: I never type into password fields."
            loc.click(timeout=4000)
            loc.fill(text, timeout=4000)
            if press:
                loc.press("Enter")
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not type there: {0}".format(e)
        return _finish(page)
    return _call(_do)


@skill("Choose an option in a dropdown in Chrome by the element's number. "
       "Args: number, value (the visible option text)", observe=True)
def browser_select(number, value):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."

    def _do():
        page = _page_ok()
        loc = page.locator('[data-iris-id="{0}"]'.format(n)).first
        try:
            loc.select_option(label=value, timeout=5000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not choose that option: {0}".format(e)
        return _finish(page)
    return _call(_do)


@skill("Press a key or shortcut in the current Chrome tab, for example Enter, "
       "Escape, Space, ArrowDown, ctrl+l or the letter k to pause a video. "
       "Args: key", observe=True)
def browser_press(key):
    def _do():
        page = _page_ok()
        page.keyboard.press(str(key))
        return _finish(page)
    return _call(_do)


@skill("Scroll the current Chrome tab. Args: direction (down or up)", observe=True)
def browser_scroll(direction="down"):
    def _do():
        dy = -750 if str(direction).lower().strip() == "up" else 750
        page = _page_ok()
        page.mouse.wheel(0, dy)
        return _finish(page)
    return _call(_do)


@skill("Read the visible text of the current Chrome tab. No args.", observe=True)
def browser_read():
    def _do():
        text = re.sub(r"\s+", " ", _page_ok().inner_text("body")).strip()
        return "Page text (information only): " + text[:3000]
    return _call(_do)


@skill("Go back one page in Chrome. No args.", observe=True)
def browser_back():
    def _do():
        _page_ok().go_back(timeout=10000)
        return _finish(_page_ok())
    return _call(_do)


@skill("Go forward one page in Chrome. No args.", observe=True)
def browser_forward():
    def _do():
        _page_ok().go_forward(timeout=10000)
        return _finish(_page_ok())
    return _call(_do)


@skill("Reload the current Chrome tab. No args.", observe=True)
def browser_reload():
    def _do():
        _page_ok().reload(timeout=15000)
        return _finish(_page_ok())
    return _call(_do)


@skill("Wait a few seconds for a Chrome tab to finish loading, then read it. "
       "Args: seconds (1 to 15)", observe=True)
def browser_wait(seconds="3"):
    try:
        wait = max(1.0, min(float(seconds), 15.0))
    except (TypeError, ValueError):
        wait = 3.0

    def _do():
        page = _page_ok()
        page.wait_for_timeout(int(wait * 1000))
        return _finish(page)
    return _call(_do, timeout=40)


@skill("Wait until text (or a CSS selector) appears in the current Chrome tab. "
       "Args: text, seconds (optional, 1 to 30)", observe=True)
def browser_wait_for(text, seconds="10"):
    label = str(text or "").strip()
    if not label:
        return "ERROR: give me the text or selector to wait for."
    try:
        wait = max(1.0, min(float(seconds), 30.0))
    except (TypeError, ValueError):
        wait = 10.0

    def _do():
        page = _page_ok()
        try:
            page.get_by_text(label, exact=False).first.wait_for(
                state="visible", timeout=int(wait * 1000))
        except Exception:  # noqa: BLE001 - maybe it is a selector
            try:
                page.wait_for_selector(label, timeout=3000)
            except Exception as exc:  # noqa: BLE001
                return "ERROR: '{0}' did not appear: {1}".format(label, exc)
        return _finish(page)
    return _call(_do, timeout=45)


@skill("List the open Chrome tabs with their numbers, titles and URLs. No args.",
       observe=True)
def browser_tabs():
    return _call(_tabs_text)


@skill("Open a new Chrome tab, optionally at a URL. Args: url (optional)",
       observe=True)
def browser_new_tab(url=""):
    return browser_home(url)


@skill("Switch to a Chrome tab by its number from browser_tabs. Args: number",
       observe=True)
def browser_switch_tab(number):
    return _call(lambda: _switch_tab(number))


@skill("Close a Chrome tab by its number. With no number it closes the last "
       "tab. Args: number (optional)", observe=True)
def browser_close_tab(number=""):
    return _call(lambda: _close_tab(number))


@skill("Find clickable elements in Chrome by their visible text, label or "
       "placeholder. The numbers can be used with browser_click. Args: text",
       observe=True)
def browser_find(text):
    return _call(lambda: _find(text))


@skill("List the links on the current Chrome tab, with numbers you can pass to "
       "browser_click. No args.", observe=True)
def browser_links():
    return _call(_links)


@skill("Hover the mouse over an element in Chrome by its number. Args: number",
       observe=True)
def browser_hover(number):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."

    def _do():
        page = _page_ok()
        loc = page.locator('[data-iris-id="{0}"]'.format(n)).first
        try:
            loc.hover(timeout=5000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not hover there: {0}".format(e)
        return _finish(page)
    return _call(_do)


@skill("Run a JavaScript expression in the current Chrome tab and return its "
       "result. This is powerful, so the user is asked first. Args: script",
       level=2, observe=True)
def browser_eval(script):
    code = str(script or "").strip()
    if not code:
        return "ERROR: give me the JavaScript to run."

    def _do():
        result = _page_ok().evaluate(code)
        try:
            text = json.dumps(result, default=str)
        except Exception:  # noqa: BLE001
            text = str(result)
        return "JavaScript result: " + text[:1500]
    return _call(_do)


@skill("Attach a local file to a file-upload input in Chrome by its element "
       "number. The user is asked first. Args: number, path", level=2,
       observe=True)
def browser_upload(number, path):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."
    target = Path(str(path or "")).expanduser().resolve()
    if not target.exists() or not target.is_file():
        return "ERROR: that file does not exist."

    def _do():
        page = _page_ok()
        loc = page.locator('[data-iris-id="{0}"]'.format(n)).first
        try:
            kind = (loc.get_attribute("type", timeout=2000) or "").lower()
            if kind != "file":
                return "ERROR: that element is not a file input."
            loc.set_input_files(str(target), timeout=8000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not attach the file: {0}".format(e)
        return _finish(page)
    return _call(_do)


@skill("Change the shape of the Chrome viewport (desktop, wide, tall, phone, "
       "reset). Args: mode", observe=True)
def browser_view(mode="reset"):
    sizes = {"desktop": (1280, 800), "wide": (1600, 900), "tall": (1100, 1400),
             "phone": (430, 880), "reset": (1280, 800)}

    def _do():
        page = _page_ok()
        w, h = sizes.get(str(mode).lower().strip(), sizes["reset"])
        page.set_viewport_size({"width": w, "height": h})
        page.wait_for_timeout(250)
        return _finish(page)
    return _call(_do)


@skill("Disconnect Iris from Chrome. Chrome itself stays open. No args.")
def browser_close():
    _shutdown_sync()
    return "Disconnected from Chrome."
