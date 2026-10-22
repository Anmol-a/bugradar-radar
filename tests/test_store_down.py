"""22 Oct: a store that answers nothing at all (robots.txt included) must alert on a daily check. No browser here: the
homepage check is driven through a fake page; the real-browser cases are in test_e2e.py (all_down, robots_500 ...)."""
from pathlib import Path

from radar.core.config import Settings
from radar.discovery import discover as d
from radar import worker as w
from tools import upload_dashboard as up

CHALLENGE = '<html><head><title>Just a moment...</title></head><body><div id="cf-chl-widget"></div></body></html>'


class _Resp:
    def __init__(self, status, body=""):
        self.status, self.body = status, body

    def text(self):
        return self.body


def _resp(a):
    if isinstance(a, Exception):
        raise a
    return a if isinstance(a, _Resp) else _Resp(a)


class _Request:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, timeout=None, headers=None, max_redirects=None):
        self.calls.append({"url": url, "timeout": timeout, "max_redirects": max_redirects})
        return _resp(self.answers.pop(0))


class _Page:
    def __init__(self, answers, browser):
        self.context = type("Ctx", (), {})()
        self.context.request = _Request(answers)
        self.browser, self.waits, self.gotos, self.routes, self.order = browser, [], [], [], []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def route(self, pattern, handler):
        self.routes.append(("route", pattern))
        self.handler = handler

    def unroute(self, pattern, handler):
        self.routes.append(("unroute", pattern))
        self.order.append("unroute")

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append((url, wait_until, timeout))
        self.order.append(f"goto {url}")
        if url == "about:blank":
            return None
        return _resp(self.browser)


class _Sess:
    backoff_scale = 1.0

    def __init__(self, answers, browser=503):
        self.page = _Page(answers, browser)

    def sign(self, url):
        return None


S = Settings(nav_timeout_ms=30000, home_timeout_ms=60000)


def test_three_silent_tries_and_a_silent_browser_mean_the_store_is_down():
    sess = _Sess([503, Exception("apiRequestContext.get: Timeout 30000ms exceeded."), 502],
                 browser=Exception("page.goto: net::ERR_CONNECTION_TIMED_OUT at https://shop.in/"))
    a = d.store_answers(sess, "https://shop.in", S)
    assert a.up is False and a.offline is False
    assert a.tries == ["HTTP 503", "Timeout 30000ms exceeded.", "HTTP 502",
                       "browser: net::ERR_CONNECTION_TIMED_OUT at https://shop.in/"]
    calls = sess.page.context.request.calls
    assert [c["url"] for c in calls] == ["https://shop.in/"] * 3                # only the homepage, nothing else
    assert all(c["timeout"] == 30000 and c["max_redirects"] == 0 for c in calls)
    assert sess.page.waits == [20000, 60000]                                    # 5xx rule: 20 s, then 60 s
    # Chromium asked once, given the homepage's patience, only up to the answer's first bytes; then the page is left
    assert sess.page.gotos == [("https://shop.in/", "commit", 60000), ("about:blank", None, 5000)]
    assert sess.page.routes == [("route", "**/*"), ("unroute", "**/*")]        # scripts / images aborted, then let go
    assert sess.page.order.index("goto about:blank") < sess.page.order.index("unroute")   # left before letting go


def test_any_answer_below_500_means_the_store_is_up():
    for code in (200, 301, 302, 403, 404, 429):
        sess = _Sess([code])
        a = d.store_answers(sess, "https://shop.in", S)
        assert a.up is True and a.status == code and a.tries == [f"HTTP {code}"], code
        assert sess.page.waits == [] and sess.page.gotos == []                 # one request, no browser


def test_a_bot_protection_page_sent_with_503_means_the_store_is_up():
    a = d.store_answers(_Sess([_Resp(503, CHALLENGE)]), "https://shop.in", S)
    assert a.up is True and a.challenge is True and a.status == 503


def test_a_store_that_comes_back_on_the_second_try_is_up():
    sess = _Sess([Exception("connect ECONNREFUSED 1.2.3.4:443"), 200])
    a = d.store_answers(sess, "https://shop.in", S)
    assert a.up is True and a.tries == ["connect ECONNREFUSED 1.2.3.4:443", "HTTP 200"]
    assert sess.page.waits == [20000]


def test_the_browser_has_the_last_word_when_the_http_client_is_dropped():
    """Review, 22 Oct: a firewall can drop Playwright's own HTTP client and let browsers through."""
    sess = _Sess([503, 503, 503], browser=200)
    a = d.store_answers(sess, "https://shop.in", S)
    assert a.up is True and a.status == 200 and a.tries[-1] == "browser: HTTP 200"
    a = d.store_answers(_Sess([503, 503, 503], browser=_Resp(503, CHALLENGE)), "https://shop.in", S)
    assert a.up is True and a.challenge is True


def test_radars_own_connection_dropping_is_flagged():
    a = d.store_answers(_Sess([Exception("connect ENETUNREACH 1.2.3.4:443")] * 3,
                              browser=Exception("net::ERR_INTERNET_DISCONNECTED")), "https://shop.in", S)
    assert a.up is False and a.offline is True


def test_the_note_parts_read_plainly():
    assert d._tries(["HTTP 503"] * 3 + ["browser: HTTP 503"]) == "HTTP 503 x3, browser: HTTP 503"
    assert d._tries(["HTTP 503"]) == "HTTP 503"
    assert d._tries(["HTTP 503", "Timeout", "HTTP 503"]) == "HTTP 503, Timeout, HTTP 503"
    assert d._span(42.4) == "42 s" and d._span(250) == "about 4 minutes"
    # brackets and API prefixes never reach the note; IP:port stays whole
    assert d._short_error(Exception("page.goto: net::ERR_TIMED_OUT (at https://x.in/)")) == "net::ERR_TIMED_OUT at https://x.in/"
    assert d._short_error("no answer: apiRequestContext.get: Timeout 20000ms exceeded.") == "Timeout 20000ms exceeded."
    assert d._short_error("connect ECONNREFUSED 127.0.0.1:9") == "connect ECONNREFUSED 127.0.0.1:9"
    assert len(d._short_error(Exception("x" * 400))) == 60


def _ans(tries, secs=250.0, up=False, status=None, challenge=False):
    return d.StoreAnswer(up=up, status=status, tries=tries, secs=secs, challenge=challenge)


def test_notes_stay_short_and_split_cleanly_on_the_dashboard(tmp_path):
    long_err = "Client network socket disconnected before secure TLS connection was established"
    for tries in (["HTTP 503"] * 3 + ["browser: HTTP 503"],
                  [long_err] * 3 + ["browser: " + long_err],
                  [long_err, "Timeout 30000ms exceeded.", long_err + " x", "browser: net::ERR_CONNECTION_RESET"]):
        note = d.down_note(_ans(tries), "Timeout 20000ms exceeded.")
        assert len(note) < 300 and note.count("(") == 1 and note.endswith(")"), note
        run = {"verdict": "unreachable", "device": "desktop", "base_url": "https://shop.in", "cases": [], "notes": [note],
               "started_at": "2026-10-22T10:00:00+00:00", "run_id": "r"}
        site = up.to_payload(run, Path(tmp_path), "", None)["results"][0]
        assert (site["key"], site["status"]) == (up.SITE_KEY, "failed")     # -> problem + 🔴 (after the re-check)
        assert site["reason"] == "The store didn't answer at all: its homepage didn't load, tried 4 times over about 4 minutes"
        assert site["error"] == note                                         # the detail under 'Technical detail'
    second = up.to_payload(run, Path(tmp_path), "", None, {"rechecked": True, "recheck_minutes": 10})["results"][0]
    assert second["reason"].endswith("over about 4 minutes. It also didn't load 10 minutes earlier.")
    for a in (_ans(["HTTP 200"], up=True, status=200), _ans(["HTTP 302"], up=True, status=302),
              _ans(["HTTP 403"], up=True, status=403),
              _ans(["HTTP 503"], up=True, status=503, challenge=True)):
        for why in ("Timeout 20000ms exceeded.", "x" * 60):
            n = d.robots_down_home_up_note(why, a)
            assert len(n) < 300 and "RFC 9309" in n, n
            assert ("open to shoppers" in n) == (a.status == 200), n         # only a real page says 'open to shoppers'


def test_a_long_note_is_split_before_it_is_shortened(tmp_path):
    """Review, 22 Oct: the uploader cut the note at 300 characters first, so a long one lost its closing bracket and
    the owner read the whole technical tail as the reason."""
    note = "The store did not load (" + "detail: " + "x" * 400 + ")"
    run = {"verdict": "unreachable", "device": "desktop", "base_url": "https://shop.in", "cases": [], "notes": [note],
           "started_at": "2026-10-22T10:00:00+00:00", "run_id": "r"}
    site = up.to_payload(run, Path(tmp_path), "", None)["results"][0]
    assert site["reason"] == "The store did not load" and site["error"] == note


def test_only_daily_checks_ask_whether_the_store_is_down():
    base = Settings()
    assert base.check_store_down is False                                   # free checks, benches, the CLI
    assert w.settings_for({"quick": True}, base).check_store_down is True
    assert w.settings_for({"quick": False, "allow_cart": True}, base).check_store_down is True


def test_free_check_never_asks(monkeypatch, tmp_path):
    got = {}

    def fake_scan_devices(url, settings, devices=(), profile=None, progress=None, only_suites=None):
        got["s"] = settings
        return []
    monkeypatch.setattr("radar.runner.executor.scan_devices", fake_scan_devices)
    w.run_scan({"id": "r1", "url": "https://x.in"}, Settings(data_dir=tmp_path), upload=False, log=lambda *_: None)
    assert got["s"].check_store_down is False
    w.run_monitor({"id": "m1", "url": "https://x.in", "devices": ["desktop"]}, Settings(data_dir=tmp_path),
                  upload=False, log=lambda *_: None)
    assert got["s"].check_store_down is True


def test_the_browser_asks_for_the_page_alone():
    """Pictures, scripts and styles are aborted; the page itself goes on to the context's routes (Web Bot Auth)."""
    sess = _Sess([503, 503, 503], browser=503)
    d.store_answers(sess, "https://shop.in", S)

    class _Route:
        def __init__(self, kind):
            self.request = type("Rq", (), {"resource_type": kind})()
            self.done = None

        def abort(self):
            self.done = "abort"

        def fallback(self):
            self.done = "fallback"
    seen = {}
    for kind in ("document", "image", "script", "stylesheet", "font", "fetch", "xhr", "media"):
        r = _Route(kind)
        sess.page.handler(r)
        seen[kind] = r.done
    assert seen == {"document": "fallback", "image": "abort", "script": "abort", "stylesheet": "abort",
                    "font": "abort", "fetch": "abort", "xhr": "abort", "media": "abort"}
