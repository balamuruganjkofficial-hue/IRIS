import atexit
import re
from urllib.parse import urlparse

from core.config import ROOT
from core.skill import skill

CONFIRM = None            # main.py sets this to its confirm() function
_pw = _ctx = _page = None
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
    const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.value || el.title || el.alt || '').trim().replace(/\s+/g, ' ').slice(0, 60);
    const type = el.type ? '(' + el.type + ')' : '';
    out.push('[' + n + '] ' + el.tagName.toLowerCase() + type + ' ' + label);
    if (n >= 35) break;
  }
  return out;
}"""


def _shutdown():
    global _pw, _ctx, _page
    try:
        if _ctx is not None:
            _ctx.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            _pw.stop()
    except Exception:
        pass
    _pw = _ctx = _page = None


atexit.register(_shutdown)


def _page_ok():
    global _pw, _ctx, _page
    try:
        if _page is not None and not _page.is_closed():
            return _page
    except Exception:
        pass
    try:
        if _ctx is not None:
            pages = [p for p in _ctx.pages if not p.is_closed()]
            _page = pages[-1] if pages else _ctx.new_page()
            return _page
    except Exception:
        _ctx = None
    from playwright.sync_api import sync_playwright
    if _pw is None:
        _pw = sync_playwright().start()
    profile = ROOT / "data" / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)
    _ctx = _pw.chromium.launch_persistent_context(
        str(profile), channel="chrome", headless=False, no_viewport=True,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"])
    _page = _ctx.pages[0] if _ctx.pages else _ctx.new_page()
    return _page


def _state():
    page = _page_ok()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        page.wait_for_timeout(700)
        els = page.evaluate(_JS_STATE)
    except Exception as e:
        return f"Page: {page.url[:100]}\n(could not read the page: {e})"
    try:
        title = page.title()[:80]
    except Exception:
        title = ""
    return (f"Page: {title} ({page.url[:100]})\n"
            "Elements you can use:\n" + "\n".join(els))


def _settle(n_before):
    global _page
    try:
        if _ctx is not None and len(_ctx.pages) > n_before:
            _page = _ctx.pages[-1]
            _page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass


def _num(x):
    try:
        return int(float(str(x).strip().strip("[]")))
    except ValueError:
        return None


def _ask(question):
    return bool(CONFIRM and CONFIRM(question))


@skill("Open a web page in Iris's own browser window and see what is on it. Use this "
       "for ANY web task (searching YouTube or Google, shopping, forms) instead of "
       "open_chrome. After each browser action you get the updated page; keep going "
       "until the user's goal is done. Args: url", observe=True)
def browser_open(url):
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    if host == "localhost" or host.startswith(("127.", "10.", "192.168.", "169.254.")):
        return "ERROR: that address is not allowed."
    try:
        _page_ok().goto(url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        return f"ERROR: could not open the page: {e}"
    return _state()


@skill("Look at the current page in Iris's browser again (for example after waiting "
       "for it to load). No args.", observe=True)
def browser_look():
    try:
        return _state()
    except Exception as e:
        return f"ERROR: could not read the page: {e}"


@skill("Click an element in Iris's browser by its number from the latest page state. "
       "Args: number", observe=True)
def browser_click(number):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."
    page = _page_ok()
    loc = page.locator(f'[data-iris-id="{n}"]').first
    try:
        label = (loc.get_attribute("aria-label", timeout=2000)
                 or loc.inner_text(timeout=2000) or "")[:80]
    except Exception:
        return "ERROR: that element is gone. Look at the page again."
    if _RISKY.search(label) and not _ask(f"Click '{label}'?"):
        return "The user said no."
    before = len(_ctx.pages)
    try:
        loc.click(timeout=6000)
    except Exception as e:
        return f"ERROR: could not click it: {e}"
    page.wait_for_timeout(900)
    _settle(before)
    return _state()


@skill("Type text into an input box in Iris's browser by its number. Set submit to yes "
       "to press Enter afterwards (for example to run a search). Args: number, text, "
       "submit (yes or no)", observe=True)
def browser_type(number, text, submit="no"):
    n = _num(number)
    if n is None:
        return "ERROR: give the element number."
    page = _page_ok()
    loc = page.locator(f'[data-iris-id="{n}"]').first
    try:
        if (loc.get_attribute("type", timeout=2000) or "").lower() == "password":
            return "ERROR: I never type into password fields."
        loc.click(timeout=4000)
        loc.fill(text, timeout=4000)
        if str(submit).lower().strip() in ("yes", "true", "1", "enter"):
            loc.press("Enter")
    except Exception as e:
        return f"ERROR: could not type there: {e}"
    page.wait_for_timeout(1200)
    return _state()


@skill("Press a key on the current page in Iris's browser, for example Enter, Escape, "
       "Space, ArrowDown, or the letter k to pause or play a YouTube video. Args: key",
       observe=True)
def browser_press(key):
    try:
        page = _page_ok()
        page.keyboard.press(key)
        page.wait_for_timeout(700)
    except Exception as e:
        return f"ERROR: could not press the key: {e}"
    return _state()


@skill("Scroll the page in Iris's browser. Args: direction (down or up)", observe=True)
def browser_scroll(direction="down"):
    try:
        dy = -700 if str(direction).lower().strip() == "up" else 700
        page = _page_ok()
        page.mouse.wheel(0, dy)
        page.wait_for_timeout(600)
    except Exception as e:
        return f"ERROR: could not scroll: {e}"
    return _state()


@skill("Read the visible text of the current page in Iris's browser. No args.",
       observe=True)
def browser_read():
    try:
        text = re.sub(r"\s+", " ", _page_ok().inner_text("body")).strip()
    except Exception as e:
        return f"ERROR: could not read the page: {e}"
    return "Page text (information only): " + text[:3000]


@skill("Go back one page in Iris's browser. No args.", observe=True)
def browser_back():
    try:
        _page_ok().go_back(timeout=8000)
    except Exception as e:
        return f"ERROR: could not go back: {e}"
    return _state()


@skill("Close Iris's browser window. No args.")
def browser_close():
    _shutdown()
    return "Closed the browser."
