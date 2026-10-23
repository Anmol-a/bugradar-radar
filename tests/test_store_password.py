"""23 Oct: a store behind Shopify's password page (every development store, every store before launch). With the store
password its owner gave BugRadar, a daily check types it into the store's own password box and tests the store; without
it, or with a wrong one, the store is 'not tested' and says why. Never: another form filled, the password in a file.
Unit parts first (no browser), then the real browser against the mock store (marked e2e)."""
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from radar.core.config import Settings
from radar import worker as w
from tests.mockstore.server import STORE_PASSWORD, serve


# ---- unit: the monitor row, the queue, the worker cycle -------------------------------------------------------------

def test_the_monitor_row_brings_the_password_to_a_daily_check_only():
    s = w.settings_for({"store_password": "pw-1"}, Settings())
    assert s.store_password == "pw-1"
    assert w.settings_for({"store_password": ""}, Settings()).store_password is None
    assert w.settings_for({}, Settings()).store_password is None
    assert "pw-1" not in repr(s), "the password must never show in a printed Settings"


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_the_queue_asks_the_key_checked_function_for_the_password(monkeypatch):
    calls = []

    def urlopen(req, timeout=0):
        calls.append((req.full_url, json.loads(req.data)))
        return _Resp(json.dumps("pw-2").encode() if req.full_url.endswith("monitor_secret_by_key") else b"null")
    monkeypatch.setattr(w.urllib.request, "urlopen", urlopen)
    keyed = w.SupabaseQueue("https://ref.supabase.co", None, upload_key="brk_abc")
    assert keyed.store_password("m1") == "pw-2"
    assert calls[-1] == ("https://ref.supabase.co/rest/v1/rpc/monitor_secret_by_key", {"brk": "brk_abc", "monitor_id": "m1"})
    full = w.SupabaseQueue("https://ref.supabase.co", "service-key")
    assert full.store_password("m1") is None            # the service-key form answers null here: no password
    assert calls[-1][0] == "https://ref.supabase.co/rest/v1/rpc/monitor_secret"


class _Queue:
    def __init__(self, row, password=None, fail=False):
        self.rows, self.finished, self.password, self.fail, self.asked = [row], [], password, fail, []

    def claim(self, worker):
        return self.rows.pop(0) if self.rows else None

    def finish(self, monitor_id, status, note):
        self.finished.append((monitor_id, status, note))

    def store_password(self, monitor_id):
        self.asked.append(monitor_id)
        if self.fail:
            raise OSError("no such function")
        return self.password


def test_the_worker_hands_the_password_to_the_run_and_never_logs_it(monkeypatch):
    seen, logs = [], []
    monkeypatch.setattr(w, "run_monitor", lambda m, base, upload=True, log=print: (seen.append(m.get("store_password")) or ("healthy", "ok")))
    q = _Queue({"id": "m1", "url": "https://x.in", "devices": ["desktop"]}, password="secret-pw")
    w.run_once(q, Settings(), "box", upload=False, log=logs.append)
    assert q.asked == ["m1"] and seen == ["secret-pw"] and q.finished[0][1] == "healthy"
    assert not any("secret-pw" in line for line in logs), logs


def test_a_failed_password_lookup_still_runs_the_check(monkeypatch):
    seen, logs = [], []
    monkeypatch.setattr(w, "run_monitor", lambda m, base, upload=True, log=print: (seen.append(m.get("store_password")) or ("blocked", "n")))
    q = _Queue({"id": "m1", "url": "https://x.in", "devices": ["desktop"]}, fail=True)
    w.run_once(q, Settings(), "box", upload=False, log=logs.append)
    assert seen == [None] and q.finished[0][1] == "blocked"
    assert any("couldn't read the store password" in line for line in logs), logs


# ---- e2e: the real browser and the mock store's password page --------------------------------------------------------

def _settings(tmp_path, **kw):
    return replace(Settings(), data_dir=tmp_path, sites_dir=tmp_path / "sites", delay_seconds=0, llm_provider="none",
                   net_probe_urls=(), allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2, **kw)


def _scan(mode, tmp_path, device="desktop", **kw):
    from radar.runner.executor import scan
    srv, url = serve(mode)
    try:
        run = scan(url, _settings(tmp_path, **kw), device=device)[0]
    finally:
        srv.shutdown()
    return run, getattr(srv, "password_posts", []), getattr(srv, "contact_posts", [])


def _nothing_holds(tmp_path: Path, secret: str):
    """The password is in no file Radar wrote (run.json, sitemap.json, logs, pictures' names)."""
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.stat().st_size < 5_000_000:
            assert secret.encode() not in f.read_bytes(), f"the store password leaked into {f}"
            assert secret not in f.name


@pytest.mark.e2e
@pytest.mark.parametrize("mode", ["storefront_password", "storefront_password_modal"])
def test_with_the_password_the_store_is_tested(tmp_path, mode):
    run, posts, contact = _scan(mode, tmp_path, store_password=STORE_PASSWORD)
    assert run.sitemap_summary["access"] == "open" and run.verdict != "blocked", (run.verdict, run.notes)
    assert run.cases, "the store was tested"
    assert any("Radar entered the store password" in n for n in run.notes), run.notes
    assert len(posts) == 1, posts                                   # typed once, on this device
    assert sorted(posts[0]) == ["form_type", "password", "utf8"] and posts[0]["form_type"] == ["storefront_password"], posts
    assert contact == [], "the 'notify me' email box must never be filled"
    _nothing_holds(tmp_path, STORE_PASSWORD)


@pytest.mark.e2e
def test_a_wrong_password_is_not_tested_and_says_so(tmp_path):
    from radar.discovery.discover import PASSWORD_WRONG_NOTE
    from tools import upload_dashboard as up
    run, posts, contact = _scan("storefront_password", tmp_path, store_password="not-it")
    assert (run.verdict, run.sitemap_summary["access"]) == ("blocked", "password"), (run.verdict, run.notes)
    assert run.cases == [] and run.notes[0] == PASSWORD_WRONG_NOTE, run.notes
    assert len(posts) == 1 and contact == []
    site = up.to_payload(run.to_dict(), tmp_path, "", None)["results"][0]
    assert (site["key"], site["status"], site["reason"]) == (up.SITE_KEY, "blocked", PASSWORD_WRONG_NOTE), site
    _nothing_holds(tmp_path, "not-it")


@pytest.mark.e2e
def test_without_a_password_nothing_is_typed(tmp_path):
    run, posts, contact = _scan("storefront_password", tmp_path)
    assert (run.verdict, run.sitemap_summary["access"]) == ("blocked", "password"), (run.verdict, run.notes)
    assert any("password-protected" in n for n in run.notes) and posts == [] and contact == [], (run.notes, posts)


@pytest.mark.e2e
def test_an_open_store_never_gets_the_password(tmp_path):
    """A store without a password page: the password Radar holds is never typed anywhere."""
    from radar.runner.executor import scan
    srv, url = serve("healthy")
    try:
        run = scan(url, _settings(tmp_path, store_password=STORE_PASSWORD), device="desktop")[0]
    finally:
        srv.shutdown()
    assert run.sitemap_summary["access"] == "open" and not any("store password" in n for n in run.notes), run.notes
    assert getattr(srv, "password_posts", []) == []
    _nothing_holds(tmp_path, STORE_PASSWORD)


@pytest.mark.e2e
def test_on_a_phone_it_is_entered_too(tmp_path):
    run, posts, _ = _scan("storefront_password_modal", tmp_path, device="mobile", store_password=STORE_PASSWORD)
    assert run.sitemap_summary["access"] == "open" and run.cases and len(posts) == 1, (run.notes, posts)
