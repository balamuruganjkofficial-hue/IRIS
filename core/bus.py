"""A tiny event bus so the assistant, the voice engine and the interface can talk
without importing each other. Subscribers run on the publisher's thread, so a UI
subscriber must only enqueue; it must never touch widgets from here."""

import threading

_lock = threading.Lock()
_subs = {}


def subscribe(topic, fn):
    """Register fn(payload) for topic. Returns a callable that unsubscribes."""
    with _lock:
        _subs.setdefault(topic, []).append(fn)

    def _off():
        unsubscribe(topic, fn)

    return _off


def unsubscribe(topic, fn):
    with _lock:
        fns = _subs.get(topic)
        if fns and fn in fns:
            fns.remove(fn)
            if not fns:
                _subs.pop(topic, None)


def has_subscribers(topic):
    with _lock:
        return bool(_subs.get(topic))


def publish(topic, payload=None):
    with _lock:
        fns = list(_subs.get(topic, ()))
    for fn in fns:
        try:
            fn(payload)
        except Exception:                       # a broken listener never hurts Iris
            pass
