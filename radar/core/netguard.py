"""Public addresses only (29 Oct 2026, security review #4).

A store address comes from a dashboard user (a monitor) or a visitor (a free scan). Radar must never be pointed at
the machine it runs on or at a private network: http://localhost:5432, http://169.254.169.254/ (cloud metadata),
http://10.0.0.5/admin, or a public-looking name whose DNS answer is one of those. Three places use this:

  worker.py          refuses a monitor / scan whose address is not public before Radar starts (nothing is visited)
  core/browser.py    every request the browser makes (pages, redirects, scripts, images) goes through a route that
                     aborts it when its host is not public: covers redirects and links found on the store's pages
  Session.request_get  Radar's own HTTP requests (robots.txt, sitemaps, homepage answer, redirect checks): each hop
                     checked, redirects followed by hand

Limit: the browser resolves names itself, so a name that answers a public address to Radar and a private one to the
browser a moment later (DNS rebinding) is not caught here. Short cache + every request checked keeps that window small.

Tests run against a mock store on 127.0.0.1: RADAR_ALLOW_PRIVATE_HOSTS=1 switches the guard off (never on a worker).
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urlparse

_TTL = 60.0
_cache: dict[str, tuple[bool, float]] = {}
_lock = threading.Lock()


class PrivateAddress(Exception):
    """The address is not a public web address (a private, loopback or link-local network, or not http/https)."""


def guard_on() -> bool:
    return os.environ.get("RADAR_ALLOW_PRIVATE_HOSTS", "") not in ("1", "true", "yes")


def public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            return public_ip(str(a.ipv4_mapped))
        if a.sixtofour is not None and not public_ip(str(a.sixtofour)):
            return False
    return a.is_global and not (a.is_multicast or a.is_loopback or a.is_link_local or a.is_private
                                or a.is_reserved or a.is_unspecified)


def _resolve(host: str) -> list[str]:
    try:
        return list({ai[4][0] for ai in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except (socket.gaierror, UnicodeError, OSError):
        return []


def public_host(host: str, resolve=None) -> bool:
    """True when the name is not an internal one and every address it resolves to is public. A name that does not
    resolve counts as public: the visit then fails on its own (a store that is down, not an attack)."""
    h = (host or "").strip().lower().rstrip(".").strip("[]")
    if not h:
        return False
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        ipaddress.ip_address(h.split("%", 1)[0])
        return public_ip(h)                  # an address written out (also the numeric forms getaddrinfo accepts)
    except ValueError:
        pass
    now = time.monotonic()
    with _lock:
        hit = _cache.get(h)
        if hit and now - hit[1] < _TTL:
            return hit[0]
    ips = (resolve or _resolve)(h)
    ok = all(public_ip(ip) for ip in ips)    # [] -> True (does not resolve: not ours to judge)
    with _lock:
        _cache[h] = (ok, now)
    return ok


def public_url(url: str, resolve=None) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if not guard_on():
        return True
    return public_host(u.hostname or "", resolve)


def require_public(url: str, resolve=None) -> None:
    if not public_url(url, resolve):
        raise PrivateAddress(f"not a public web address: {(urlparse(url).hostname or url)[:80]}")


def browser_url_ok(url: str, resolve=None) -> bool:
    """For the browser route: only network schemes are judged (data:, blob: and the like never reach a route)."""
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https", "ws", "wss"):
        return True
    if not guard_on():
        return True
    try:
        return public_host(urlparse(url).hostname or "", resolve)
    except ValueError:
        return False


def clear_cache() -> None:
    with _lock:
        _cache.clear()
