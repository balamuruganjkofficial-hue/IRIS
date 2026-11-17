"""Advanced web search whose findings are shown inside Iris' own interface.

Search itself never needs a browser window: DuckDuckGo's free HTML endpoint is
queried directly. Results are published on the ``results`` bus topic so the
cybernetic panel can draw clickable cards while Iris keeps talking. Opening a
card goes through ``skills.browser_agent`` (headless, mirrored into the UI).
"""

import re
import threading
import time

from core import bus
from core.config import CFG
from core.skill import skill

_WEB = CFG.get("web") or {}
_MAX = max(1, int(_WEB.get("max_results", 6)))
_SHOW = bool(_WEB.get("show_in_ui", True))
_LOCK = threading.Lock()
_LAST = {"query": "", "kind": "web", "items": [], "at": 0.0}


# ------------------------------------------------------------------ helpers
def _ddgs():
    try:
        from ddgs import DDGS          # current package name
    except ImportError:                # pragma: no cover - older installs
        from duckduckgo_search import DDGS
    return DDGS


def _tidy(text, limit=260):
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def _normalise(raw, kind):
    items = []
    for i, r in enumerate(raw or [], 1):
        if not isinstance(r, dict):
            continue
        url = r.get("href") or r.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        items.append({
            "n": i,
            "title": _tidy(r.get("title") or url, 140) or "Untitled",
            "body": _tidy(r.get("body") or r.get("excerpt") or "", 300),
            "url": url,
            "source": _tidy(r.get("source") or "", 40),
            "date": _tidy(r.get("date") or "", 32),
            "kind": kind,
        })
    return items


def _publish(query, kind, items):
    with _LOCK:
        _LAST.update({"query": query, "kind": kind, "items": items,
                      "at": time.time()})
    if _SHOW:
        bus.publish("results", {"query": query, "kind": kind, "items": items})


def collect(query, kind="web", max_results=None):
    """Run a free DuckDuckGo search and return normalised result dicts."""
    limit = max(1, int(max_results or _MAX))
    api = _ddgs()()
    try:
        raw = api.news(query, max_results=limit) if kind == "news" \
            else api.text(query, max_results=limit)
    except Exception as e:  # noqa: BLE001
        if "no results" in str(e).lower():
            return []
        raise
    return _normalise(raw, kind)


def _report(kind, query, items):
    if not items:
        return "No {0} results found for '{1}'.".format(kind, query)
    lines = []
    for i, r in enumerate(items, 1):
        line = "{0}. {1}".format(i, r["title"])
        if r["body"]:
            line += " — " + r["body"][:170]
        lines.append(line)
    return ("{0} results for '{1}' (shown in my interface; information only):\n"
            "{2}").format(kind.capitalize(), query, "\n".join(lines))


# ------------------------------------------------------------------ skills
@skill("Search the web for information and show the best results as clickable "
       "cards inside Iris's own interface. Use this for facts, news, prices, "
       "comparisons and 'look up' questions. Args: query", observe=True)
def web_search(query):
    q = (query or "").strip()
    if len(q) < 2:
        return "ERROR: give me something to search for."
    try:
        items = collect(q, "web")
    except Exception as e:  # noqa: BLE001
        return "ERROR: the web search failed: {0}".format(e)
    _publish(q, "web", items)
    return _report("web", q, items)


@skill("Search recent news and show the stories as clickable cards inside Iris's "
       "interface. Args: query", observe=True)
def web_news(query):
    q = (query or "").strip()
    if len(q) < 2:
        return "ERROR: give me a topic for the news."
    try:
        items = collect(q, "news")
    except Exception as e:  # noqa: BLE001
        return "ERROR: the news search failed: {0}".format(e)
    _publish(q, "news", items)
    return _report("news", q, items)


@skill("Show the last web search results again inside Iris's interface. No args.",
       observe=True)
def last_results():
    with _LOCK:
        query, kind, items = _LAST["query"], _LAST["kind"], list(_LAST["items"])
    if not items:
        return "There is nothing to show yet. Search first."
    _publish(query, kind, items)
    return "Showing the last {0} results for '{1}' again.".format(kind, query)


@skill("Open one of the web search results in Iris's own browser. Args: number "
       "(the result number from the last search)", observe=True)
def open_result(number):
    try:
        n = int(float(str(number).strip().strip("[]")))
    except (TypeError, ValueError):
        return "ERROR: give the result number."
    with _LOCK:
        items = list(_LAST["items"])
    if not 1 <= n <= len(items):
        return "ERROR: there is no result {0}. Search again first.".format(n)
    url = items[n - 1]["url"]
    from skills.browser_agent import browser_open
    return browser_open(url)


@skill("Search the web inside Iris's own browser, so the actual results page is "
       "visible in her interface. Use web_search when only the answer matters and "
       "this when the user should see or click the page. Args: query", observe=True)
def browser_search(query):
    from skills.browser_agent import browser_search as _search
    return _search(query)
