"""Network safety helpers: only public http(s) addresses may be fetched."""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse


def ip_is_public(value):
    """True for a routable public IP literal, False for anything local/reserved."""
    try:
        ip = ipaddress.ip_address(str(value).split("%")[0])
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def host_is_public(host):
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {info[4][0] for info in infos}
    if not addrs:
        return False
    return all(ip_is_public(a) for a in addrs)


def is_public_url(url):
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    return host_is_public(u.hostname)


def get_public(url, timeout=10, headers=None, max_redirects=4):
    """GET a public URL, validating every redirect hop before following it."""
    import requests

    for _ in range(max_redirects + 1):
        if not is_public_url(url):
            raise ValueError("that address is not allowed")
        r = requests.get(url, timeout=timeout, headers=headers, allow_redirects=False)
        if r.is_redirect:
            url = urljoin(r.url, r.headers.get("location", ""))
            continue
        r.raise_for_status()
        return r
    raise ValueError("too many redirects")
