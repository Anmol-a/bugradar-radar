"""22 Oct: a store that answers nothing at all (robots.txt included) must alert on a daily check. No browser here: the
homepage check is driven through a fake page; the real-browser cases are in test_e2e.py (all_down, robots_500 ...)."""
from pathlib import Path

from radar.core.config import Settings
from radar.discovery import discover as d
from radar import worker as w
from tools import upload_dashboard as up


class _Resp:
    def __init__(self, status):
        self.status = status


class _Request:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, timeout=None, headers=None):
        self.calls.append((url, timeout))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return _Resp(a)


class _Page:
    def __init__(self, answers):
        self.context = type("Ctx", (), {})()
        self.context.request = _Request(answers)
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class _Sess:
    backoff_scale = 1.0

    def __init__(self, answers):
        self.page = _Page(answers)

    def sign(self, url):
        return None


S = Settings(home_timeout_ms=60000)


def test_three_silent_tries_mean_the_store_is_down():
    sess = _Sess([503, Exception("apiRequestContext.get: Timeout 60000ms exceeded."), 502])
    answered, seen, _ = d.store_answers(sess, "https://shop.in", S)
    assert answered is False
    assert seen == ["HTTP 503", "Timeout 60000ms exceeded.", "HTTP 502"]
    req = sess.page.context.request
    assert [c[0] for c in req.calls] == ["https://shop.in/"] * 3            # only the homepage, nothing else
    assert all(c[1] == 60000 for c in req.calls)                            # the homepage's own patience
    assert sess.page.waits == [20000, 60000]                                # 5xx rule: 20 s, then 60 s


def test_any_answer_below_500_means_the_store_is_up():
    for code in (200, 301, 403, 404, 429):
        sess = _Sess([code])
        answered, seen, _ = d.store_answers(sess, "https://shop.in", S)
        assert answered is True and seen == [f"HTTP {code}"], code
        assert sess.page.waits == []                                        # one request, no waiting


def test_a_store_that_comes_back_on_the_second_try_is_up():
    sess = _Sess([Exception("connect ECONNREFUSED 1.2.3.4:443"), 200])
    answered, seen, _ = d.store_answers(sess, "https://shop.in", S)
    assert answered is True and seen == ["connect ECONNREFUSED 1.2.3.4:443", "HTTP 200"]
    assert sess.page.waits == [20000]


def test_the_note_parts_read_plainly():
    assert d._tries(["HTTP 503"] * 3) == "HTTP 503 x3"
    assert d._tries(["HTTP 503"]) == "HTTP 503"
    assert d._tries(["HTTP 503", "Timeout 60000ms exceeded."]) == "HTTP 503, Timeout 60000ms exceeded."
    assert d._span(42.4) == "42 s" and d._span(250) == "about 4 minutes"
    # brackets never reach the note: the uploader would cut the sentence there
    assert d._short_error(Exception("page.goto: net::ERR_TIMED_OUT (at https://x.in/)")) == "net::ERR_TIMED_OUT at https://x.in/"
    assert len(d._short_error(Exception("x" * 400))) == 90


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


DOWN_NOTE = ("The store didn't answer at all: its homepage didn't load, tried 3 times over about 4 minutes "
             "(homepage: HTTP 503 x3; robots.txt: HTTP 503, tried 3 times)")


def test_the_dashboard_files_a_store_that_answers_nothing_as_failed(tmp_path):
    run = {"verdict": "unreachable", "device": "desktop", "base_url": "https://shop.in", "cases": [], "notes": [DOWN_NOTE],
           "started_at": "2026-10-22T10:00:00+00:00", "finished_at": "2026-10-22T10:05:00+00:00", "run_id": "r"}
    site = up.to_payload(run, Path(tmp_path), "", None)["results"][0]
    assert (site["key"], site["status"]) == (up.SITE_KEY, "failed")         # -> problem + 🔴 (after the re-check)
    assert site["reason"] == "The store didn't answer at all: its homepage didn't load, tried 3 times over about 4 minutes"
    assert site["error"] == DOWN_NOTE                                       # the detail stays under 'Technical detail'
    second = up.to_payload(run, Path(tmp_path), "", None, {"rechecked": True, "recheck_minutes": 10})["results"][0]
    assert second["reason"] == ("The store didn't answer at all: its homepage didn't load, tried 3 times over about 4 "
                                "minutes. It also didn't load 10 minutes earlier.")


def test_the_worker_looks_again_before_filing_it(monkeypatch, tmp_path):
    """The verdict the new rule produces takes the 21 Oct re-check path: nothing uploaded on the first look."""
    class R:
        verdict, device, sitemap_summary = "unreachable", "desktop", {"access": "unreachable"}
    monkeypatch.setattr("radar.runner.executor.scan_devices", lambda *a, **k: [(R(), tmp_path)])
    sent = []
    monkeypatch.setattr(w, "upload_results", lambda *a, **k: (sent.append(1), ([], ""))[1])
    status, note = w.run_monitor({"id": "m1", "url": "https://x.in", "devices": ["desktop"]},
                                 Settings(data_dir=tmp_path), log=lambda *_: None)
    assert status == w.RECHECK and sent == [] and "looking again in 10 minutes" in note
