"""Browser session: one Chromium, a fresh context per attempt, evidence capture.

Honest identification, no stealth: the User-Agent always ends with Radar's own name (BugRadar/0.1
(+bugradar.in)), real device emulation only, navigator.webdriver left as it is.

User-Agent styles (Settings.ua_style, 6 Oct):
  browser (default)  the real Chrome name of the browser in use + Radar's name, e.g.
                     "Mozilla/5.0 (Macintosh; ...) Chrome/141.0.0.0 Safari/537.36 BugRadar/0.1 (+bugradar.in)"
                     (simple "is this a normal browser?" filters let it in; logs still say BugRadar)
  plain              "BugRadar/0.1 (+bugradar.in)" only
robots.txt is always matched on the BugRadar token, whatever the style.
"""
from __future__ import annotations

import json

import hashlib
import platform
import re
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

from radar.core.config import Settings
from radar.core.robots import Robots

DEVICES = {"desktop": None, "mobile": "Pixel 7"}
DEFAULT_DEVICES = ("desktop", "mobile")      # every run tests both (product decision, 7 Oct: Revenue Shield parity)
MAX_CONSOLE, MAX_LOADS = 40, 25
RADAR_PROBE_URLS = ("bugradar-check-does-not-exist",)    # Radar's own deliberate soft-404 request: not store noise

# Navigation timing of the current document (cheap, run on every step) and its largest contentful paint (run once
# per document, 250 ms cap, best effort).
_TIMING_JS = """() => {
  const n = performance.getEntriesByType('navigation')[0];
  if (!n) return null;
  return {origin: performance.timeOrigin, start: n.startTime, ttfb: n.responseStart,
          dcl: n.domContentLoadedEventEnd, load: n.loadEventEnd};
}"""
_LCP_JS = """() => new Promise(res => {
  try {
    const po = new PerformanceObserver(l => { const e = l.getEntries(); res(e.length ? e[e.length - 1].startTime : null); po.disconnect(); });
    po.observe({type: 'largest-contentful-paint', buffered: true});
    setTimeout(() => { po.disconnect(); res(null); }, 250);
  } catch (e) { res(null); }
})"""


def _short_url(url: str) -> str:
    """Path + query without the host, for the timing table ('/products/x?variant=1')."""
    from urllib.parse import urlparse
    p = urlparse(url or "")
    return ((p.path or "/") + (f"?{p.query}" if p.query else ""))[:100]


_OS = {"Darwin": "Macintosh; Intel Mac OS X 10_15_7", "Windows": "Windows NT 10.0; Win64; x64"}


def browser_user_agent(identity: str, chrome_version: str, style: str = "browser",
                       device_ua: str | None = None, system: str | None = None) -> str:
    """The User-Agent Radar sends. Pure, unit-tested. Radar's own name is ALWAYS in it; 'browser' only
    adds the normal Chrome (or the emulated device's) name in front, instead of Playwright's
    'HeadlessChrome'. The major version is kept, minor parts zeroed like Chrome's reduced UA."""
    if style == "plain":
        return identity
    major = (chrome_version or "0").split(".")[0]
    if device_ua:                                   # mobile emulation: the device's own browser name
        base = re.sub(r"(Headless)?Chrome/[\d.]+", f"Chrome/{major}.0.0.0", device_ua)
    else:
        os_part = _OS.get(system or platform.system(), "X11; Linux x86_64")
        base = f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    return f"{base} {identity}"


class RobotsBlocked(Exception):
    pass


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc or url


class RateLimited(Exception):
    """The store's platform kept answering HTTP 429 to Radar after backing off (Shopify throttling Radar's IP:
    own Contabo server, 9 Oct, all 3 demo stores). Says nothing about the store: the test is BLOCKED, never a
    store failure, never an incident."""


class Browser:
    def __init__(self, settings: Settings, device: str = "desktop"):
        self.s, self.device = settings, device
        self._pw = self._browser = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.s.headless, slow_mo=self.s.slow_mo_ms)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def new_context(self) -> BrowserContext:
        name = DEVICES.get(self.device)
        d = self._pw.devices[name] if name else None
        ua = browser_user_agent(self.s.user_agent, self._browser.version, self.s.ua_style,
                                d["user_agent"] if d else None)
        kw = dict(user_agent=ua, locale=self.s.locale, timezone_id=self.s.timezone)
        if d:
            kw.update(viewport=d["viewport"], is_mobile=True, has_touch=True,
                      device_scale_factor=d["device_scale_factor"])
        else:
            kw.update(viewport={"width": 1366, "height": 850})
        return self._browser.new_context(**kw)

    @contextmanager
    def attempt(self, evidence_dir: Path, tag: str):
        """Fresh context + page with tracing; yields a Session. Saves trace/screenshot on failure
        (caller sets session.failed)."""
        ctx = self.new_context()
        ctx.tracing.start(screenshots=True, snapshots=True)
        page = ctx.new_page()
        sess = Session(page, self.s)
        sess.evidence_dir, sess.tag = Path(evidence_dir), tag
        try:
            yield sess
        finally:
            if sess.failed:
                shot = evidence_dir / f"{tag}.png"
                trace = evidence_dir / f"{tag}_trace.zip"
                try:
                    page.screenshot(path=str(shot), full_page=False)
                    sess.screenshot = shot.name
                    # what the LLM triage sees: small JPEG + the URL and visible text at failure
                    sess.fail_jpeg = page.screenshot(type="jpeg", quality=60, full_page=False)
                    sess.fail_url = page.url
                    sess.fail_text = (page.evaluate("() => document.body ? document.body.innerText : ''") or "")[:4000]
                except Exception:  # noqa: BLE001
                    pass
                ctx.tracing.stop(path=str(trace))
                sess.trace = trace.name
            else:
                ctx.tracing.stop()
            ctx.close()


class Session:
    """What a check gets: the page plus polite, robots-aware helpers."""

    def __init__(self, page: Page, settings: Settings, robots: Robots | None = None):
        self.page, self.s = page, settings
        self.robots = robots
        self.failed = False
        self.screenshot = self.trace = None
        self.evidence_dir: Path | None = None
        self.tag = ""
        self.fail_jpeg: bytes | None = None
        self.fail_url = self.fail_text = None
        self._shots = 0
        self._last_shot: tuple[str, str] | None = None     # (md5 of bytes, file) to skip identical pictures
        self.console_errors: list[str] = []      # uncaught JS errors only: what the 'no_js_errors' check judges
        self.failed_requests: list[str] = []
        # Evidence for the report (never judged, 7 Oct): everything the page logged as error/warning, and how long
        # each page took. Every store has third-party console noise, so none of this can fail a test.
        self.console: list[dict] = []            # {type: error|warning|pageerror, text, url}, de-duplicated, capped
        self.loads: list[dict] = []              # {url, ttfb, dcl, load, lcp} in seconds, one per page document
        self._console_seen: set[str] = set()
        self._load_seen: set[tuple] = set()
        self._last_nav = 0.0
        self.last_load_secs = 0.0
        page.on("pageerror", lambda e: (self.console_errors.append(str(e)[:200]),
                                        self._log("pageerror", str(e), page.url)))
        page.on("console", lambda m: self._log(m.type, m.text, (m.location or {}).get("url") or page.url)
                if m.type in ("error", "warning") else None)
        page.on("requestfailed", lambda r: self.failed_requests.append(f"{r.method} {r.url[:150]}"))
        # pages of the store (main frame) that answered 5xx during this attempt, also when reached by a click: lets the
        # runner wait out a short server hiccup before re-checking (9 Oct cloud run)
        self.server_errors: list[str] = []
        page.on("response", self._on_response)

    def _on_response(self, r):
        try:
            if 500 <= r.status <= 599 and r.request.resource_type == "document" and r.frame.parent_frame is None:
                self.server_errors.append(f"HTTP {r.status} {r.url[:150]}")
        except Exception:  # noqa: BLE001  never break a page event
            pass

    def _log(self, kind: str, text: str, url: str):
        """One console entry for the report. Never raises (called from a browser event)."""
        try:
            text = " ".join((text or "").split())[:220]
            if any(p in (url or "") for p in RADAR_PROBE_URLS):
                return
            key = f"{kind}|{text[:120]}"
            if text and key not in self._console_seen and len(self.console) < MAX_CONSOLE:
                self._console_seen.add(key)
                self.console.append({"type": kind, "text": text, "url": (url or "")[:140]})
        except Exception:  # noqa: BLE001
            pass

    def record_load(self):
        """Timing of the page document the shopper is on now (navigation timing + largest contentful paint),
        once per document. Evidence only: a failure here is swallowed, never a test failure."""
        if len(self.loads) >= MAX_LOADS:
            return
        try:
            t = self.page.evaluate(_TIMING_JS)
        except Exception:  # noqa: BLE001  page closed / mid-navigation: no timing, no problem
            return
        if not t or not t.get("load"):
            return
        key = (t["origin"], round(t["start"]))
        if key in self._load_seen:
            return
        self._load_seen.add(key)
        try:
            lcp = self.page.evaluate(_LCP_JS)
        except Exception:  # noqa: BLE001
            lcp = None
        sec = lambda ms: None if ms is None else round(ms / 1000, 2)
        self.loads.append({"url": _short_url(self.page.url), "ttfb": sec(t["ttfb"]), "dcl": sec(t["dcl"]),
                           "load": sec(t["load"]), "lcp": sec(lcp)})

    def allowed(self, url: str) -> bool:
        if not self.s.respect_robots or self.robots is None:
            return True
        from urllib.parse import urlparse
        path = urlparse(url).path
        if path.startswith("/cart") and self.s.allow_cart_flow:
            return True   # shopper-flow exemption, see ARCHITECTURE.md
        if path.rstrip("/") == "/search" and self.s.search_is_shopper_flow:
            return True   # a shopper typing in the store's own search box; see ARCHITECTURE.md
        return self.robots.allowed(url)

    def _polite(self):
        wait = self.s.delay_seconds - (time.time() - self._last_nav)
        if wait > 0:
            time.sleep(wait)
        self._last_nav = time.time()

    def goto(self, url: str):
        if not self.allowed(url):
            raise RobotsBlocked(f"robots.txt disallows {url}")
        self._polite()
        t0 = time.time()
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=self.s.nav_timeout_ms)
        self.last_load_secs = round(time.time() - t0, 2)
        self.settle()
        self.record_load()
        return resp, self.last_load_secs

    def settle(self, rounds: int = 3):
        """Wait for 'load' and for client-side redirects to finish. Real stores redirect in JS
        (e.g. a free-sample product forwarding to the full-size one); reading the page during that
        hop fails with 'Execution context was destroyed'."""
        for _ in range(rounds):
            before = self.page.url
            try:
                self.page.wait_for_load_state("load", timeout=10000)
            except Exception:  # noqa: BLE001  slow third-party assets must not fail the step
                pass
            self.page.wait_for_timeout(600)
            if self.page.url == before:
                return

    def step_shot(self, step: str) -> str | None:
        """Compressed viewport JPEG after a step (~60-120 KB). Returns the file name or None."""
        self.record_load()                       # journeys move by clicking: time every page the shopper lands on
        if not self.evidence_dir:
            return None
        try:
            data = self.page.screenshot(type="jpeg", quality=50, full_page=False)
        except Exception:  # noqa: BLE001  page closed / navigating: no picture, never a failure
            return None
        digest = hashlib.md5(data).hexdigest()
        if self._last_shot and self._last_shot[0] == digest:     # nothing changed on screen: reuse, no new file
            return self._last_shot[1]
        self._shots += 1
        name = f"{self.tag}.{self._shots:02d}-{re.sub(r'[^a-z0-9_]+', '-', step.lower())[:40]}.jpg"
        (self.evidence_dir / name).write_bytes(data)
        self._last_shot = (digest, name)
        return name

    def evaluate(self, js: str, arg=None):
        """page.evaluate that survives a navigation happening underneath it."""
        for i in range(4):
            try:
                return self.page.evaluate(js, arg) if arg is not None else self.page.evaluate(js)
            except Exception as e:  # noqa: BLE001
                if i == 3 or not re.search(r"context was destroyed|navigat|Target closed", str(e)):
                    raise
                self.settle()

    backoff_scale = 1.0          # tests shrink the waits; real runs use the full ones
    _DATA_CACHE: dict = {}       # /products/<h>.js answers, per process (= per store in a bench), 2 min

    def get_json(self, url: str):
        """Product data (/products/<h>.js) is asked 3-4 times per page by different checks; one answer is reused
        for 2 minutes so Radar sends a store's platform far fewer requests (Shopify rate-limits datacenter IPs:
        own server, 9 Oct). Cart data (/cart.js) and listings are never cached."""
        key = url.split("#")[0]
        cacheable = bool(re.search(r"/products/[^/?#]+\.js(\?|$)", key))
        if cacheable:
            hit = Session._DATA_CACHE.get(key)
            if hit and time.time() - hit[0] < 120:
                return json.loads(hit[1])
        out = self._get_json(url)
        if cacheable:
            Session._DATA_CACHE[key] = (time.time(), json.dumps(out))
        return out

    def _get_json(self, url: str):
        if not self.allowed(url):
            raise RobotsBlocked(f"robots.txt disallows {url}")
        # Store data (/products/x.js, /cart.js) is retried on a transient miss: no answer, a 5xx/429, or a body
        # that is not JSON (bench 11: palmonas.com returned an empty body once, wellbeingnutrition.com timed out
        # once; both passed on the next attempt). A real 4xx is final.
        last, limited, after = None, 0, 0
        for i, wait in enumerate((0, 1000, 3000, 8000, 20000)):
            if i >= 3 and not limited:
                break                       # the 2 extra, longer waits are only for HTTP 429 (rate limit)
            if wait:
                self.page.wait_for_timeout(int(min(30000, max(wait, after * 1000)) * self.backoff_scale))
            try:
                r = self.page.context.request.get(url, timeout=20000)
            except Exception as e:  # noqa: BLE001  timeout, reset, DNS
                last = f"no answer ({str(e).splitlines()[0][:120]})"
                continue
            if r.status == 429:
                limited += 1
                try:
                    after = float((r.headers or {}).get("retry-after") or 0)
                except ValueError:
                    after = 0
                last = "HTTP 429"
                continue
            if r.status >= 500:
                last = f"HTTP {r.status}"
                continue
            if r.status >= 400:
                raise AssertionError(f"{url} returned HTTP {r.status}")
            try:
                return r.json()
            except ValueError:
                last = f"HTTP {r.status} but the body is not JSON ({(r.text() or '')[:60]!r})"
        if last == "HTTP 429":
            raise RateLimited(f"{_host(url)} kept answering HTTP 429 (Too Many Requests) to Radar's data request "
                              f"after waiting ~32 s: the platform is rate-limiting Radar, this is not a store failure")
        raise AssertionError(f"{url}: no valid data after {i + 1} tries (last: {last})")
