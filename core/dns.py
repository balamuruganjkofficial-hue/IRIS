"""Application-level DNS fallback, free and local to the process.

Iris wraps ``socket.getaddrinfo`` once at startup. A normal system lookup is
tried first (bounded by a short daemon-thread wait); if it fails, the name is
resolved through Cloudflare's free DNS-over-HTTPS JSON endpoint using an IP
literal, so the fallback cannot recursively need DNS itself. Results are cached
for five minutes and failures for a few seconds, which keeps a dead network
from turning every tool call into another slow lookup.

Because the patch is on ``socket`` itself it covers httpx (Groq/OpenAI SDKs),
requests/urllib3 and aiohttp (edge-tts) without touching those libraries.
Nothing here imports the assistant modules, so it can be installed very early.
"""

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from urllib.parse import quote

_ORIGINAL = socket.getaddrinfo     # pristine resolver, never replaced by tests
_SYSTEM = _ORIGINAL                # current system resolver (test seam)
_LOCK = threading.RLock()
_CACHE = {}
_RESOLVER = None                    # test seam: (host, type) -> [ip, ...]
_INSTALLED = False

# Defaults; install() refreshes them from config.network.dns when available.
CACHE_SECONDS = 300.0
NEGATIVE_SECONDS = 30.0
SYSTEM_TIMEOUT = 1.0
DOH_TIMEOUT = 1.5
DOH_IP = "1.1.1.1"
DOH_TLS_HOST = "cloudflare-dns.com"

_STATS = {"system_ok": 0, "system_fail": 0, "doh_ok": 0, "doh_fail": 0,
          "cache_hit": 0, "cache_negative": 0}


def _settings():
    try:
        from core.config import CFG
        return ((CFG.get("network") or {}).get("dns") or {})
    except Exception:  # noqa: BLE001 - config is optional here
        return {}


def _load_settings():
    global CACHE_SECONDS, NEGATIVE_SECONDS, SYSTEM_TIMEOUT, DOH_TIMEOUT
    global DOH_IP, DOH_TLS_HOST
    cfg = _settings()
    CACHE_SECONDS = float(cfg.get("cache_seconds", CACHE_SECONDS))
    NEGATIVE_SECONDS = float(cfg.get("negative_seconds", NEGATIVE_SECONDS))
    SYSTEM_TIMEOUT = float(cfg.get("system_timeout", SYSTEM_TIMEOUT))
    DOH_TIMEOUT = float(cfg.get("doh_timeout", DOH_TIMEOUT))
    DOH_IP = str(cfg.get("doh_ip") or DOH_IP)
    DOH_TLS_HOST = str(cfg.get("doh_tls_host") or DOH_TLS_HOST)


def install():
    """Patch socket.getaddrinfo once. Safe to call again."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return False
        _load_settings()
        socket.getaddrinfo = getaddrinfo
        _INSTALLED = True
        return True


def uninstall():
    """Restore the resolver captured before install (mainly for tests)."""
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            return False
        socket.getaddrinfo = _ORIGINAL
        _INSTALLED = False
        return True


def installed():
    return _INSTALLED


def set_system_resolver(fn):
    """Test seam for the failing system resolver; None restores the real one."""
    global _SYSTEM
    with _LOCK:
        _SYSTEM = fn or _ORIGINAL


def set_resolver(fn):
    """Test seam for DoH. fn(host, 'A'|'AAAA') -> list of IP strings."""
    global _RESOLVER
    with _LOCK:
        _RESOLVER = fn


def clear_cache():
    with _LOCK:
        _CACHE.clear()
        for key in _STATS:
            _STATS[key] = 0


def stats():
    with _LOCK:
        return dict(_STATS)


def _is_ip(value):
    try:
        ipaddress.ip_address(str(value).split("%")[0])
        return True
    except ValueError:
        return False


def _records(ip, port, family, socktype, proto):
    """Build getaddrinfo-shaped tuples from one IP literal."""
    try:
        address = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return []
    fam = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    if family and family not in (0, fam):
        return []
    pairs = []
    if socktype in (0, socket.SOCK_STREAM):
        pairs.append((socket.SOCK_STREAM, socket.IPPROTO_TCP))
    if socktype in (0, socket.SOCK_DGRAM):
        pairs.append((socket.SOCK_DGRAM, socket.IPPROTO_UDP))
    out = []
    for kind, default_proto in pairs:
        if proto and proto != default_proto:
            continue
        out.append((fam, kind, default_proto, "", (str(address), int(port or 0))))
    return out


def _system(host, port, family, socktype, proto, flags):
    """Bound the OS resolver so a hung DNS server cannot freeze a turn."""
    box = {}

    def run():
        try:
            box["value"] = _SYSTEM(host, port, family, socktype, proto, flags)
        except BaseException as exc:  # noqa: BLE001 - reported by the caller
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="dns-system")
    worker.start()
    worker.join(max(0.05, SYSTEM_TIMEOUT))
    if "value" in box:
        return box["value"]
    if "error" in box:
        raise box["error"]
    raise socket.gaierror(-3, "system DNS timeout")


def _doh_query(host, rtype="A"):
    """One Cloudflare JSON query over TLS to an IP literal (no recursion)."""
    want = 1 if rtype == "A" else 28
    path = "/dns-query?name={0}&type={1}".format(quote(str(host)), rtype)
    request = (
        "GET {0} HTTP/1.1\r\n"
        "Host: {1}\r\n"
        "Accept: application/dns-json\r\n"
        "User-Agent: Iris-DNS/1.0\r\n"
        "Connection: close\r\n\r\n"
    ).format(path, DOH_TLS_HOST)
    raw, tls = None, None
    try:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(DOH_TIMEOUT)
        raw.connect((DOH_IP, 443))
        tls = ssl.create_default_context().wrap_socket(
            raw, server_hostname=DOH_TLS_HOST)
        tls.settimeout(DOH_TIMEOUT)
        tls.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(tls)
        response.begin()
        if response.status != 200:
            raise OSError("DoH HTTP {0}".format(response.status))
        payload = json.loads(response.read(65536).decode("utf-8", "replace"))
        found = []
        for answer in payload.get("Answer") or []:
            if int(answer.get("type") or 0) != want:
                continue
            ip = str(answer.get("data") or "").strip('"')
            try:
                if ipaddress.ip_address(ip).version == (4 if want == 1 else 6):
                    found.append(ip)
            except ValueError:
                continue
        return found
    finally:
        for handle in (tls, raw):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass


def _fallback(host, port, family, socktype, proto, flags):
    query = _RESOLVER or _doh_query
    types = ("A",) if family == socket.AF_INET else ("A", "AAAA")
    out = []
    for rtype in types:
        try:
            for ip in query(host, rtype) or []:
                out.extend(_records(ip, port, family, socktype, proto))
        except Exception:  # noqa: BLE001 - try the other record type
            continue
    return out or None


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Drop-in socket.getaddrinfo with a cached DoH safety net."""
    if host is None or host == "":
        return _system(host, port, family, type, proto, flags)

    # Numeric hosts never need DNS; answer locally to avoid any recursion and
    # to keep even a fully broken resolver usable for DoH and the UI lock.
    if _is_ip(host):
        numeric = isinstance(port, int) or (isinstance(port, str) and port.isdigit())
        if numeric:
            return _records(host, int(port or 0), family, type, proto)

    key = (str(host), port, family, type, proto, flags)
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            _STATS["cache_hit"] += 1
            if hit[1] is not None:
                return list(hit[1])
            _STATS["cache_negative"] += 1
            raise socket.gaierror(-2, str(hit[2]))

    try:
        value = _system(host, port, family, type, proto, flags)
    except Exception as exc:  # noqa: BLE001 - any resolver failure is retryable
        with _LOCK:
            _STATS["system_fail"] += 1
        value = _fallback(host, port, family, type, proto, flags)
        if value is None:
            message = "DNS failed for {0}".format(host)
            with _LOCK:
                _STATS["doh_fail"] += 1
                _CACHE[key] = (now + NEGATIVE_SECONDS, None, message)
            raise socket.gaierror(-2, message) from None
        with _LOCK:
            _STATS["doh_ok"] += 1
            _CACHE[key] = (now + CACHE_SECONDS, list(value), None)
        return value

    with _LOCK:
        _STATS["system_ok"] += 1
        _CACHE[key] = (now + CACHE_SECONDS, list(value), None)
    return value
