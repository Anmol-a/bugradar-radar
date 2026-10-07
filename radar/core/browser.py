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
        self.console_errors: list[str] = []
        self.failed_requests: list[str] = []
        self._last_nav = 0.0
        self.last_load_secs = 0.0
        page.on("pageerror", lambda e: self.console_errors.append(str(e)[:200]))
        page.on("requestfailed", lambda r: self.failed_requests.append(f"{r.method} {r.url[:150]}"))

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

    def get_json(self, url: str):
        if not self.allowed(url):
            raise RobotsBlocked(f"robots.txt disallows {url}")
        # Store data (/products/x.js, /cart.js) is retried on a transient miss: no answer, a 5xx/429, or a body
        # that is not JSON (bench 11: palmonas.com returned an empty body once, wellbeingnutrition.com timed out
        # once; both passed on the next attempt). A real 4xx is final.
        last = None
        for i, wait in enumerate((0, 1000, 3000)):
            if wait:
                self.page.wait_for_timeout(wait)
            try:
                r = self.page.context.request.get(url, timeout=20000)
            except Exception as e:  # noqa: BLE001  timeout, reset, DNS
                last = f"no answer ({str(e).splitlines()[0][:120]})"
                continue
            if r.status >= 500 or r.status == 429:
                last = f"HTTP {r.status}"
                continue
            if r.status >= 400:
                raise AssertionError(f"{url} returned HTTP {r.status}")
            try:
                return r.json()
            except ValueError:
                last = f"HTTP {r.status} but the body is not JSON ({(r.text() or '')[:60]!r})"
        raise AssertionError(f"{url}: no valid data after 3 tries (last: {last})")
