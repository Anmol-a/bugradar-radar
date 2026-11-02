"""Keep real store names out of public logs.

This repository is public, so everything a GitHub Actions run prints (and its step summary) can be read by anyone.
On GitHub (GITHUB_ACTIONS=true) store addresses are replaced by a short stable alias, e.g. ``store-3fa9c1``: the same
store always gets the same alias, so a run can still be followed and matched with the dashboard (which shows the
real name to signed-in admins). RADAR_SHOW_STORES=1 turns it off (local debugging). Off GitHub nothing changes.

Two kinds of masking:
- hosts we know (a store list, the job a worker just claimed): every mention, bare or in a URL;
- any other web address: its host, unless it is one of ours or a public service (KEEP below).
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sys
import threading
from urllib.parse import urlsplit

# Hosts that are not stores (or are Shopify's own demo stores) and stay readable.
KEEP = (
    "github.com", "githubusercontent.com", "bugradar.in", "supabase.co", "supabase.com", "shopify.com",
    "shopifycdn.com", "shopifysvc.com", "cdn.shopify.com", "pypi.org", "pythonhosted.org", "playwright.dev",
    "playwright.azureedge.net", "microsoft.com", "azureedge.net", "ubuntu.com", "googleapis.com", "google.com",
    "gstatic.com", "cloudflare.com", "npmjs.org", "npmjs.com", "openai.com", "anthropic.com", "example.com",
    "localhost",
)
DEMO = re.compile(r"^theme-[a-z0-9-]+-demo\.myshopify\.com$")
URL_HOST = re.compile(r"(?i)\bhttps?://([a-z0-9.-]+\.[a-z]{2,})")


def enabled(env=os.environ) -> bool:
    return env.get("GITHUB_ACTIONS") == "true" and env.get("RADAR_SHOW_STORES") != "1"


def _norm(host: str) -> str:
    h = host.strip().lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def alias(host: str) -> str:
    return "store-" + hashlib.sha256(_norm(host).encode()).hexdigest()[:6]


def kept(host: str) -> bool:
    h = _norm(host)
    return bool(DEMO.match(h)) or any(h == k or h.endswith("." + k) for k in KEEP)


def host_of(entry: str) -> str | None:
    """Host from a store-list line or a URL ('https://x.in  cart', 'x.in'); None for comments/blank."""
    s = entry.strip()
    if not s or s.startswith("#"):
        return None
    s = s.split()[0]
    if "://" not in s:
        s = "https://" + s
    try:
        h = urlsplit(s).hostname
    except ValueError:
        return None
    return _norm(h) if h and "." in h else None


class Redactor:
    def __init__(self, hosts=()):
        self._lock = threading.Lock()
        self._hosts: set[str] = set()
        self._rx: re.Pattern | None = None
        self.add(*hosts)

    def add(self, *entries: str) -> None:
        new = {h for e in entries if e for h in [host_of(e)] if h and not kept(h)}
        with self._lock:
            if new - self._hosts:
                self._hosts |= new
                alts = sorted(self._hosts, key=len, reverse=True)
                # www. optional, not part of a longer name on either side
                self._rx = re.compile(r"(?i)(?<![a-z0-9.-])(?:www\.)?(" + "|".join(map(re.escape, alts)) + r")(?![a-z0-9-]|\.[a-z0-9])")

    def scrub(self, text: str) -> str:
        rx = self._rx
        if rx is not None:
            text = rx.sub(lambda m: alias(m.group(1)), text)
        return URL_HOST.sub(lambda m: m.group(0) if kept(m.group(1)) or m.group(1).startswith("store-")
                            else m.group(0)[: m.start(1) - m.start(0)] + alias(m.group(1)), text)


class _Stream(io.TextIOBase):
    """A text stream that scrubs whole lines before passing them on (a name split across writes is still caught)."""

    def __init__(self, inner, red: Redactor):
        self._inner, self._red, self._buf = inner, red, ""

    def write(self, s: str) -> int:
        self._buf += s
        if "\n" in self._buf:
            head, self._buf = self._buf.rsplit("\n", 1)
            self._inner.write(self._red.scrub(head + "\n"))
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._inner.write(self._red.scrub(self._buf))
            self._buf = ""
        self._inner.flush()

    def fileno(self):
        return self._inner.fileno()

    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")


_installed: Redactor | None = None


def install(hosts=()) -> Redactor | None:
    """On GitHub: route this process's stdout/stderr through a Redactor (once). Returns it, or None when off."""
    global _installed
    if not enabled():
        return None
    if _installed is None:
        _installed = Redactor(hosts)
        sys.stdout = _Stream(sys.stdout, _installed)
        sys.stderr = _Stream(sys.stderr, _installed)
    else:
        _installed.add(*hosts)
    return _installed


def remember(*entries: str) -> None:
    """Add hosts to the installed Redactor (no-op off GitHub)."""
    if _installed is not None:
        _installed.add(*entries)
