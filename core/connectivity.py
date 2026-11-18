"""Internet state with one bus event per change.

A daemon thread makes short TCP attempts against a few public endpoints. It
never touches Tk widgets: it only publishes ``connectivity`` events, and the UI
subscriber enqueues them like every other bus topic. Default state is "online"
until a probe proves otherwise, so startup behaviour is unchanged when the
monitor has not run yet.
"""

import socket
import threading
import time

from core import bus

TOPIC = "connectivity"
_STATE = {"online": True, "at": 0.0}
_LOCK = threading.RLock()
_STOP = threading.Event()
_THREAD = None

# Defaults; start() refreshes them from config.network.connectivity.
INTERVAL = 20.0
OFFLINE_INTERVAL = 10.0
TIMEOUT = 1.2
HOSTS = (("1.1.1.1", 443), ("api.groq.com", 443), ("openrouter.ai", 443))


def _settings():
    try:
        from core.config import CFG
        return ((CFG.get("network") or {}).get("connectivity") or {})
    except Exception:  # noqa: BLE001
        return {}


def _load_settings():
    global INTERVAL, OFFLINE_INTERVAL, TIMEOUT, HOSTS
    cfg = _settings()
    INTERVAL = float(cfg.get("interval", INTERVAL))
    OFFLINE_INTERVAL = float(cfg.get("offline_interval", OFFLINE_INTERVAL))
    TIMEOUT = float(cfg.get("timeout", TIMEOUT))
    raw = cfg.get("hosts") or []
    parsed = []
    for item in raw:
        try:
            parsed.append((str(item[0]), int(item[1])))
        except (IndexError, TypeError, ValueError):
            continue
    if parsed:
        HOSTS = tuple(parsed)


def is_online():
    with _LOCK:
        return bool(_STATE["online"])


def state():
    with _LOCK:
        return dict(_STATE)


def set_online(online, publish=True):
    """Set the state. Publish only when it actually changes."""
    value = bool(online)
    with _LOCK:
        changed = _STATE["online"] != value
        _STATE["online"] = value
        _STATE["at"] = time.time()
    if changed and publish:
        bus.publish(TOPIC, {"online": value})
    return changed


def probe(hosts=None, timeout=None):
    """True when at least one cheap TCP connect succeeds."""
    targets = hosts or HOSTS
    limit = float(timeout or TIMEOUT)
    for host, port in targets:
        try:
            with socket.create_connection((host, int(port)), timeout=limit):
                return True
        except OSError:
            continue
    return False


def _loop():
    while not _STOP.wait(OFFLINE_INTERVAL if not is_online() else INTERVAL):
        try:
            set_online(probe())
        except Exception:  # noqa: BLE001 - a failed probe means offline
            set_online(False)


def start():
    """Start the monitor once. Returns its daemon thread."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD
        _load_settings()
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, daemon=True, name="connectivity")
        _THREAD.start()
        return _THREAD


def stop():
    _STOP.set()
    thread = _THREAD
    if thread is not None and thread.is_alive():
        thread.join(0.2)
    return thread


def reset_for_tests(online=True):
    """Return to a known state without emitting a bus event."""
    with _LOCK:
        _STATE["online"] = bool(online)
        _STATE["at"] = 0.0
