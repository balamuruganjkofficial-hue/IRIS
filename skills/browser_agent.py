"""Iris' own browser: headless by default, mirrored into her interface.

All Playwright calls run on one dedicated worker thread (the sync API is not
thread-safe), so the assistant can browse while the interface opens a link and
Iris keeps talking. Each action publishes a JPEG snapshot on the ``view`` bus
topic; the cybernetic panel draws it, so no external Chrome window is needed.
"""

import atexit
import html
import logging
import re
import threading
import time
from queue import Empty, Queue

from core import bus, net
from core.config import CFG, ROOT
from core.skill import skill

log = logging.getLogger("iris")

CONFIRM = None            # main.py sets this to its confirm() function
_pw = _ctx = _page = None
_last_shot = None
_SHOTS = ROOT / "data" / "browser_shots"
_RISKY = re.compile(r"\b(buy|pay|purchase|order|checkout|delete|remove|send|transfer|"
                    r"confirm|place order|submit)\b", re.I)

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


def _call(fn, timeout=75):
    """Run a browser action on the worker thread and return its text result."""
    _ensure_worker()
    box, done = {}, threading.Event()
    _req.put((fn, box, done))
    if not done.wait(timeout):
        return ("ERROR: the page is taking too long. Tell the user and try again, "
                "or use web_search instead.")
    if "error" in box:
        log.warning("browser action failed: %s", box["error"])
        return "ERROR: {0}".format(box["error"])
    return box["result"]


# ------------------------------------------------------------------ lifecycle
def _shutdown():
    global _pw, _ctx, _page, _last_shot
    try:
        if _ctx is not None:
            _ctx.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _pw is not None:
            _pw.stop()
    except Exception:  # noqa: BLE001
        pass
    _pw = _ctx = _page = _last_shot = None
    bus.publish("view", {"state": "closed"})


def _shutdown_sync():
    try:
        _call(_shutdown, timeout=12)
    except Exception:  # noqa: BLE001
        pass


atexit.register(_shutdown_sync)


def _launch():
    global _pw, _ctx
    from playwright.sync_api import sync_playwright
    if _pw is None:
        _pw = sync_playwright().start()
    cfg = CFG.get("browser") or {}
    headless = bool(cfg.get("headless", True))
    width = max(640, int(cfg.get("capture_width", 1280)))
    profile = ROOT / "data" / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)
    common = dict(
        headless=headless,
        viewport={"width": width, "height": max(600, int(width * 0.625))},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled",
              "--disable-notifications", "--no-first-run"],
        ignore_default_args=["--enable-automation"],
    )
    last = None
    for extra in ({"channel": "chrome"}, {}):
        try:
            _ctx = _pw.chromium.launch_persistent_context(
                str(profile), **dict(common, **extra))
            break
        except Exception as e:  # noqa: BLE001 - try the bundled Chromium next
            last = e
            _ctx = None
    if _ctx is None:
        raise RuntimeError("could not start the browser: {0}".format(last))
    _ctx.set_default_timeout(9000)
    return _ctx


def _page_ok():
    global _page
    try:
        if _page is not None and not _page.is_closed():
            return _page
    except Exception:  # noqa: BLE001
        pass
    try:
        if _ctx is not None:
            pages = [p for p in _ctx.pages if not p.is_closed()]
            _page = pages[-1] if pages else _ctx.new_page()
            return _page
    except Exception:  # noqa: BLE001
        pass
    _launch()
    _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
    return _page


# ------------------------------------------------------------------- capture
def _prune_shots(keep):
    try:
        shots = sorted(_SHOTS.glob("shot_*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in shots[:-keep]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _capture(page):
    """Save a fresh JPEG and stream it to the interface."""
    global _last_shot
    cfg = CFG.get("browser") or {}
    if not bool(cfg.get("show_in_ui", True)):
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
    """Settle, snapshot into the UI and describe the new page."""
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


def _host(url):
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url


def _results_page(query, items, kind="web"):
    """Build the search-results page locally, in Iris' own colours.

    Search engines challenge headless browsers, so the links from the free
    DuckDuckGo lookup are rendered here instead. Everything stays inside
    Iris' interface and every card is a real, clickable result.
    """
    cards = []
    for n, item in enumerate(items, 1):
        url = html.escape(item.get("url", ""))
        title = html.escape(item.get("title", ""))
        body = html.escape(item.get("body", ""))
        source = html.escape(item.get("source") or _host(item.get("url", "")))
        cards.append(
            '<a class="card" href="{url}">'
            '<div class="head"><span class="n">{n:02d}</span>'
            '<span class="src">{src}</span></div>'
            '<div class="title">{title}</div>'
            '<div class="body">{body}</div>'
            '<div class="open">OPEN IN IRIS  ›</div></a>'.format(
                url=url, n=n, src=source, title=title, body=body))
    if not cards:
        cards.append('<div class="empty">NO SIGNAL<br>Nothing found.</div>')
    return """<!doctype html><html><head><meta charset="utf-8">
<title>{q} - Iris search</title><style>
*{{box-sizing:border-box}}
html,body{{margin:0;background:#060407;color:#F7ECEF;
  font-family:"Segoe UI",Arial,sans-serif;min-height:100%}}
body{{background-image:
  linear-gradient(rgba(255,31,61,.06) 1px,transparent 1px),
  linear-gradient(90deg,rgba(255,31,61,.06) 1px,transparent 1px);
  background-size:42px 42px;padding:40px 52px 60px}}
header{{display:flex;align-items:baseline;gap:18px;
  border-bottom:1px solid rgba(255,31,61,.35);padding-bottom:16px;margin-bottom:26px}}
.brand{{font-family:Consolas,monospace;font-weight:bold;letter-spacing:8px;
  color:#FF1F3D;font-size:22px}}
.q{{font-size:15px;color:#FF5C73}}
.count{{margin-left:auto;font-family:Consolas,monospace;font-size:11px;color:#A07A82}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.card{{display:block;text-decoration:none;background:#12060A;
  border:1px solid rgba(255,31,61,.28);border-radius:12px;padding:16px 18px;
  transition:border-color .12s,background .12s}}
.card:hover{{background:#1A070C;border-color:#FF1F3D}}
.head{{display:flex;justify-content:space-between;font-family:Consolas,monospace;
  font-size:11px;color:#FF5C73;margin-bottom:7px}}
.n{{color:#FF1F3D;font-weight:bold}}
.src{{color:#A07A82}}
.title{{font-size:16px;font-weight:600;line-height:1.3;margin-bottom:7px}}
.body{{font-size:12px;line-height:1.5;color:#B08A92}}
.open{{margin-top:10px;font-family:Consolas,monospace;font-size:10px;color:#FF1F3D;
  letter-spacing:1px}}
.empty{{grid-column:1/-1;text-align:center;color:#A07A82;padding:80px 0;
  font-family:Consolas,monospace;letter-spacing:3px}}
</style></head><body>
<header><span class="brand">IRIS</span><span class="q">{q}</span>
<span class="count">{kind} · {n} RESULTS · IN-INTERFACE</span></header>
<div class="grid">{cards}</div></body></html>""".format(
        q=html.escape(query), kind=kind.upper(), n=len(items),
        cards="".join(cards))


def _home_page():
    """Iris' own start page, in the new dark sharp-red palette."""
    links = [
        ("YouTube", "https://www.youtube.com", "VIDEO"),
        ("Wikipedia", "https://www.wikipedia.org", "KNOWLEDGE"),
        ("GitHub", "https://github.com", "CODE"),
        ("Gmail", "https://mail.google.com", "MAIL"),
        ("Google Maps", "https://maps.google.com", "MAPS"),
        ("Reddit", "https://www.reddit.com", "COMMUNITY"),
        ("Stack Overflow", "https://stackoverflow.com", "DEV"),
        ("Spotify", "https://open.spotify.com", "AUDIO"),
    ]
    cards = "".join(
        '<a class="card" href="{url}"><span class="k">{kind}</span>'
        '<span class="t">{name}</span><span class="go">OPEN  ›</span></a>'.format(
            url=html.escape(url), kind=kind, name=html.escape(name))
        for name, url, kind in links)
    return """<!doctype html><html><head><meta charset="utf-8">
<title>Iris // Secure Browser</title><style>
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:#060407;color:#F7ECEF;
  font-family:"Segoe UI",Arial,sans-serif}
body{background-image:
  radial-gradient(circle at 70% 8%,rgba(255,31,61,.14),transparent 42%),
  linear-gradient(rgba(255,31,61,.055) 1px,transparent 1px),
  linear-gradient(90deg,rgba(255,31,61,.055) 1px,transparent 1px);
  background-size:auto,46px 46px,46px 46px;padding:54px 64px 60px}
.scan{position:fixed;left:0;right:0;top:0;height:2px;
  background:linear-gradient(90deg,transparent,#FF1F3D,transparent);
  animation:scan 6s linear infinite;opacity:.55}
@keyframes scan{0%{transform:translateY(0)}100%{transform:translateY(100vh)}}
.hero{display:flex;align-items:flex-end;gap:22px;
  border-bottom:1px solid rgba(255,31,61,.35);padding-bottom:22px;margin-bottom:26px}
.brand{font-family:Consolas,monospace;font-weight:bold;letter-spacing:12px;
  color:#FF1F3D;font-size:38px;text-shadow:0 0 22px rgba(255,31,61,.45)}
.tag{font-family:Consolas,monospace;font-size:11px;color:#A07A82;
  letter-spacing:2px;padding-bottom:8px}
.online{margin-left:auto;font-family:Consolas,monospace;font-size:10px;
  color:#FF5C73;letter-spacing:2px;padding-bottom:8px}
.online b{display:inline-block;width:7px;height:7px;border-radius:50%;
  background:#FF1F3D;box-shadow:0 0 10px #FF1F3D;margin-right:7px}
form{display:flex;gap:10px;margin-bottom:30px}
input{flex:1;background:#12060A;border:1px solid rgba(255,31,61,.35);
  border-radius:10px;padding:15px 18px;color:#F7ECEF;font-size:15px;outline:none}
input:focus{border-color:#FF1F3D;box-shadow:0 0 0 3px rgba(255,31,61,.12)}
button{background:#FF1F3D;color:#1A0308;border:0;border-radius:10px;
  padding:0 26px;font-weight:700;font-size:13px;letter-spacing:1px;cursor:pointer}
button:hover{background:#FF5C73}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.card{display:flex;flex-direction:column;gap:7px;text-decoration:none;
  background:#12060A;border:1px solid rgba(255,31,61,.22);border-radius:12px;
  padding:17px 18px 15px;transition:.14s}
.card:hover{background:#1A070C;border-color:#FF1F3D;
  transform:translateY(-2px);box-shadow:0 8px 26px rgba(255,31,61,.10)}
.k{font-family:Consolas,monospace;font-size:9px;letter-spacing:2px;color:#A07A82}
.t{font-size:16px;font-weight:600;color:#F7ECEF}
.go{font-family:Consolas,monospace;font-size:9px;letter-spacing:1.5px;
  color:#FF1F3D;margin-top:5px}
.foot{margin-top:30px;display:flex;gap:24px;font-family:Consolas,monospace;
  font-size:10px;color:#6F4E56;letter-spacing:1.4px}
.foot b{color:#FF5C73;font-weight:400}
</style></head><body><div class="scan"></div>
<div class="hero"><span class="brand">IRIS</span>
<span class="tag">SECURE BROWSER // IN-INTERFACE</span>
<span class="online"><b></b>LINK ACTIVE</span></div>
<form action="https://html.duckduckgo.com/html/" method="get">
<input name="q" placeholder="Search the web from inside Iris…" autofocus>
<button type="submit">SEARCH</button></form>
<div class="grid">{cards}</div>
<div class="foot"><span><b>MODE</b> HEADLESS · PRIVATE</span>
<span><b>RENDER</b> LIVE MIRROR</span>
<span><b>HOST</b> IRIS-OS</span></div>
</body></html>""".replace("{cards}", cards)


def _goto_local(path):
    from pathlib import Path
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


def _home():
    page = _page_ok()
    page.set_content(_home_page(), wait_until="domcontentloaded")
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
    before = len(_ctx.pages) if _ctx else 0
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


# -------------------------------------------------------------------- skills
@skill("Open a page in Iris's headless browser; the page then appears as a live "
       "image inside her interface. Use this for ANY web task (searching a site, "
       "YouTube, forms) instead of opening Chrome. After each action you get the "
       "updated page; keep going until the user's goal is done. Args: url",
       observe=True)
def browser_open(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not net.is_public_url(url):
        return "ERROR: that address is not allowed."
    return _call(lambda: _goto(url))


@skill("Open Iris's own start page inside her browser, the one with the search box "
       "and quick links. Use this when the user just says 'open the browser' or "
       "'open Chrome'. No args.", observe=True)
def browser_home():
    return _call(_home)


@skill("Show a local file inside Iris's own interface. Images, PDFs, video, audio "
       "and text are rendered in her browser panel. Args: path", observe=True)
def browser_open_file(path):
    if not bool((CFG.get("browser") or {}).get("local_files", True)):
        return "ERROR: local files are disabled in the config."
    return _call(lambda: _goto_local(path))


def browser_control(action, **kwargs):
    """Small dispatcher used by the interface's own browser controls.

    It never runs on the Tk thread: main.py calls it from a worker thread.
    """
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
        return "Closed the browser."
    return "ERROR: unknown browser action."


@skill("Search the web and show a real, clickable results page inside Iris's own "
       "browser panel. Use web_search for a quick spoken answer, or this when the "
       "user wants to see and click the results. Args: query", observe=True)
def browser_search(query):
    q = (query or "").strip()
    if not q:
        return "ERROR: give me something to search for."

    def _do():
        from skills.websearch import collect
        try:
            items = collect(q, "web")
        except Exception as e:  # noqa: BLE001
            return "ERROR: the search failed: {0}".format(e)
        if not items:
            return "No results found for '{0}'.".format(q)
        page = _page_ok()
        page.set_content(_results_page(q, items), wait_until="domcontentloaded")
        return _finish(page)
    return _call(_do, timeout=80)


@skill("Look at the current page in Iris's browser again and refresh the image in "
       "her interface. No args.", observe=True)
def browser_look():
    return _call(lambda: _finish(_page_ok()))


@skill("Click an element in Iris's browser by its number from the latest page "
       "state. Args: number", observe=True)
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
        before = len(_ctx.pages)
        try:
            loc.click(timeout=7000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not click it: {0}".format(e)
        _settle(before)
        return _finish(_page_ok())
    return _call(_do)


@skill("Click a link or button in Iris's browser by its visible text, for example "
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
        before = len(_ctx.pages)
        try:
            loc.click(timeout=7000)
        except Exception as e:  # noqa: BLE001
            return "ERROR: could not click it: {0}".format(e)
        _settle(before)
        return _finish(_page_ok())
    return _call(_do)


@skill("Type text into an input box in Iris's browser by its number. Set submit to "
       "yes to press Enter afterwards (for example to run a search). Args: number, "
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


@skill("Choose an option in a dropdown in Iris's browser by the element's number. "
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


@skill("Press a key on the current page in Iris's browser, for example Enter, "
       "Escape, Space, ArrowDown, or the letter k to pause or play a YouTube "
       "video. Args: key", observe=True)
def browser_press(key):
    def _do():
        page = _page_ok()
        page.keyboard.press(str(key))
        return _finish(page)
    return _call(_do)


@skill("Scroll the page in Iris's browser. Args: direction (down or up)", observe=True)
def browser_scroll(direction="down"):
    def _do():
        dy = -750 if str(direction).lower().strip() == "up" else 750
        page = _page_ok()
        page.mouse.wheel(0, dy)
        return _finish(page)
    return _call(_do)


@skill("Read the visible text of the current page in Iris's browser. No args.",
       observe=True)
def browser_read():
    def _do():
        text = re.sub(r"\s+", " ", _page_ok().inner_text("body")).strip()
        return "Page text (information only): " + text[:3000]
    return _call(_do)


@skill("Go back one page in Iris's browser. No args.", observe=True)
def browser_back():
    def _do():
        _page_ok().go_back(timeout=10000)
        return _finish(_page_ok())
    return _call(_do)


@skill("Go forward one page in Iris's browser. No args.", observe=True)
def browser_forward():
    def _do():
        _page_ok().go_forward(timeout=10000)
        return _finish(_page_ok())
    return _call(_do)


@skill("Reload the current page in Iris's browser. No args.", observe=True)
def browser_reload():
    def _do():
        _page_ok().reload(timeout=15000)
        return _finish(_page_ok())
    return _call(_do)


@skill("Wait a few seconds for a page in Iris's browser to finish loading, then "
       "look at it and refresh the image in her interface. Args: seconds (1 to 15)",
       observe=True)
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


@skill("Change the shape of Iris's virtual browser view. Args: mode (desktop, "
       "wide, tall, phone, reset)", observe=True)
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


@skill("Close Iris's browser and clear the live view from her interface. No args.")
def browser_close():
    _shutdown_sync()
    return "Closed the browser."
