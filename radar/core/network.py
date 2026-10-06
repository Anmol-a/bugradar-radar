"""Is Radar itself online? Asked before Radar blames a store for anything that looks like a network failure.

Why (bench 8, 6 Oct 2026): the Mac running the bench lost its internet connection mid-run. Every page load
then failed with net::ERR_INTERNET_DISCONNECTED and Radar called three stores DOWN and 27 'unreachable'.
On a schedule that would have told merchants "your store is down" while the outage was ours.

Rule: before a failure is confirmed, before a homepage is called unreachable and before a robots.txt is
called unreachable, Radar checks its own connection. Offline = verdict NO_NETWORK (Radar side): nothing is
reported against the store, no incident is opened or closed, nothing alerts.

The probe asks a few independent, always-on hosts. ANY HTTP answer (even 403/500) or a TLS error means the
network works (we reached a server); only "no route / no DNS / timeout" on every host means Radar is offline.
An empty probe list switches the check off (tests that do not exercise it).
"""
from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request

DEFAULT_PROBES = (
    "https://www.google.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://www.shopify.com/",
)

_OK_TTL = 30.0                      # a positive answer is reused for 30 s (a run can have many failures)
_cache: dict = {"key": None, "at": 0.0}


def _reachable(url: str, timeout: float, ua: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True                  # the server answered: the network works
    except urllib.error.URLError as e:
        return isinstance(e.reason, ssl.SSLError)   # reached a server, local certificate store problem
    except ssl.SSLError:
        return True
    except Exception:  # noqa: BLE001  timeout, reset, DNS
        return False


def online(probes: tuple | list = DEFAULT_PROBES, timeout: float = 5.0,
           ua: str = "BugRadar/0.1 (+bugradar.in)") -> bool:
    """True if Radar can reach the internet. Never cached when False (the next check may succeed)."""
    if not probes:
        return True
    key = tuple(probes)
    if _cache["key"] == key and time.time() - _cache["at"] < _OK_TTL:
        return True
    for url in probes:
        if _reachable(url, timeout, ua):
            _cache.update(key=key, at=time.time())
            return True
    _cache.update(key=None, at=0.0)
    return False


def reset_cache() -> None:
    _cache.update(key=None, at=0.0)


NO_NETWORK_NOTE = ("Radar lost its OWN internet connection{where}; nothing is reported against the store "
                   "(no failure, no incident, no alert). Run again when the connection is back.")
