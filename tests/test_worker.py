"""The live runner (radar/worker.py): queue client, status rule, one-store cycle. No network: the Supabase calls are
faked at urllib level."""
import io
import json
from dataclasses import replace

import pytest

from radar.core.config import Settings
from radar import worker as w


class FakeQueue:
    def __init__(self, rows):
        self.rows, self.finished = list(rows), []

    def claim(self, worker):
        return self.rows.pop(0) if self.rows else None

    def finish(self, monitor_id, status, note):
        self.finished.append((monitor_id, status, note))


def test_overall_status_takes_the_worst_device():
    assert w.overall(["healthy", "healthy"]) == "healthy"
    assert w.overall(["healthy", "degraded"]) == "degraded"
    assert w.overall(["down", "healthy"]) == "down"
    assert w.overall(["blocked"]) == "blocked"
    assert w.overall([]) == "error"
    assert w.overall(["healthy", "something_new"]) == "something_new"     # unknown verdicts are never hidden


def test_monitor_row_maps_to_settings():
    base = Settings()
    s = w.settings_for({"quick": True, "allow_cart": False}, base)
    assert (s.max_products, s.max_collections, s.max_nav_links, s.allow_cart_flow) == (1, 1, 4, False)
    s = w.settings_for({"quick": False, "allow_cart": True}, base)
    assert (s.max_products, s.allow_cart_flow) == (base.max_products, True)


def test_supabase_queue_calls_the_two_functions_with_the_service_key(monkeypatch):
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url, req.get_method(), dict(req.header_items()), json.loads(req.data)))
        if req.full_url.endswith("/claim_monitor"):
            return Resp(json.dumps([{"id": "m1", "url": "https://x.in", "devices": ["desktop"]}]).encode())
        return Resp(b"")
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    q = w.SupabaseQueue("https://ref.supabase.co/", "service-key")
    assert q.claim("box-1") == {"id": "m1", "url": "https://x.in", "devices": ["desktop"]}
    q.finish("m1", "healthy", "n" * 500)
    assert [c[0] for c in calls] == ["https://ref.supabase.co/rest/v1/rpc/claim_monitor",
                                     "https://ref.supabase.co/rest/v1/rpc/finish_monitor"]
    assert calls[0][3] == {"worker": "box-1"} and calls[1][3]["status"] == "healthy" and len(calls[1][3]["note"]) == 300
    for c in calls:
        assert c[2]["Authorization"] == "Bearer service-key" and c[2]["Apikey"] == "service-key"
    # an empty claim answer = nothing due
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda req, timeout=0: Resp(b"[]"))
    assert q.claim("box-1") is None


def test_run_once_finishes_the_claimed_store_even_when_radar_crashes(monkeypatch):
    def boom(url, settings, devices):
        raise RuntimeError("browser exploded")
    monkeypatch.setattr("radar.runner.executor.scan_devices", boom)
    q = FakeQueue([{"id": "m1", "url": "https://x.in", "devices": ["desktop"]}])
    ran = w.run_once(q, Settings(), "box-1", upload=False, log=lambda *_: None)
    assert ran["id"] == "m1"
    assert q.finished == [("m1", "error", "Radar crashed: browser exploded")]
    assert w.run_once(q, Settings(), "box-1", upload=False, log=lambda *_: None) is None   # nothing due


def test_run_once_records_the_worst_device_and_the_note(monkeypatch, tmp_path):
    class R:
        def __init__(self, device, verdict): self.device, self.verdict = device, verdict
    monkeypatch.setattr("radar.runner.executor.scan_devices",
                        lambda url, settings, devices: [(R("desktop", "healthy"), tmp_path), (R("mobile", "degraded"), tmp_path)])
    q = FakeQueue([{"id": "m2", "url": "https://y.in", "devices": ["desktop", "mobile"]}])
    w.run_once(q, Settings(), "box-1", upload=False, log=lambda *_: None)
    (mid, status, note), = q.finished
    assert (mid, status) == ("m2", "degraded") and note.startswith("desktop: healthy; mobile: degraded (")


def test_worker_needs_its_environment(monkeypatch):
    monkeypatch.delenv("BUGRADAR_SUPABASE_URL", raising=False)
    monkeypatch.delenv("BUGRADAR_SERVICE_KEY", raising=False)
    with pytest.raises(SystemExit):
        w.queue_from_env()


# ---- the free scan page (door A, 15 Oct) ----------------------------------------------------------------------------

class ScanQueue(FakeQueue):
    def __init__(self, scans, monitors=()):
        super().__init__(monitors)
        self.scans, self.scan_finished = list(scans), []

    def claim_scan(self, worker):
        return self.scans.pop(0) if self.scans else None

    def finish_scan(self, scan_id, verdict, score, summary, run_id, project_id, note, failed=False):
        self.scan_finished.append(dict(id=scan_id, verdict=verdict, score=score, summary=summary, run_id=run_id,
                                       project_id=project_id, note=note, failed=failed))


def _fake_run(verdict="healthy", cases=()):
    class R:
        def __init__(self):
            self.device, self.verdict, self.run_id, self.platform, self.notes = "desktop", verdict, "r-1", "shopify", []
            self.cases = list(cases)

        def to_dict(self):
            return {"run_id": self.run_id, "verdict": self.verdict, "platform": self.platform, "notes": self.notes,
                    "cases": self.cases, "device": self.device}
    return R()


def _case(cid, verdict="pass", severity="major", steps=(), triage=None, title=None):
    return {"case_id": cid, "suite": cid.split(".")[0], "title": title or cid, "severity": severity, "verdict": verdict,
            "attempts": [{"steps": list(steps), "failed_step": steps[-1]["name"] if steps else None}], "triage": triage}


def test_run_once_takes_a_free_scan_before_any_monitor(monkeypatch, tmp_path):
    seen = {}

    def fake_scan_devices(url, settings, devices, profile=None, **kw):
        seen.update(url=url, devices=devices, profile=profile, cart=settings.allow_cart_flow)
        return [(_fake_run("healthy", [_case("smoke.home_health"), _case("health.not_found")]), tmp_path)]
    monkeypatch.setattr("radar.runner.executor.scan_devices", fake_scan_devices)
    q = ScanQueue([{"id": "s1", "url": "https://x.in", "host": "x.in"}],
                  [{"id": "m1", "url": "https://y.in", "devices": ["desktop"]}])
    ran = w.run_once(q, Settings(), "box-1", upload=False, log=lambda *_: None)
    assert ran["id"] == "s1"
    assert seen == {"url": "https://x.in", "devices": ("desktop",), "profile": "basic", "cart": False}
    assert q.finished == []                                      # the monitor waited
    (f,) = q.scan_finished
    assert (f["id"], f["verdict"], f["score"], f["failed"]) == ("s1", "healthy", 100, False)
    assert f["summary"]["checked"] == 2 and f["summary"]["passed"] == 2 and f["summary"]["problems"] == []
    assert f["note"].startswith("desktop: healthy (")
    # the queue is empty of scans now: the monitor runs next
    monkeypatch.setattr("radar.runner.executor.scan_devices", lambda url, settings, devices, **kw: [(_fake_run(), tmp_path)])
    assert w.run_once(q, Settings(), "box-1", upload=False, log=lambda *_: None)["id"] == "m1"


def test_a_crashed_free_scan_is_marked_failed_not_left_running(monkeypatch):
    def boom(url, settings, devices, **kw):
        raise RuntimeError("browser exploded")
    monkeypatch.setattr("radar.runner.executor.scan_devices", boom)
    q = ScanQueue([{"id": "s2", "url": "https://x.in"}])
    w.run_once(q, Settings(), "box-1", upload=False, log=lambda *_: None)
    (f,) = q.scan_finished
    assert f["failed"] is True and f["verdict"] == "error" and f["score"] is None
    assert f["note"] == "Radar crashed: browser exploded"


def test_score_and_summary_from_a_run():
    failed_pdp = _case("product.pdp.tee", "confirmed_fail", "critical", title="PDP 'Tee'",
                       steps=[{"name": "price_visible", "status": "fail", "detail": "No price on the page"}],
                       triage={"verdict": "store_problem", "reason": "The product page shows no price"})
    slow_home = _case("smoke.home_health", "pass", "critical",
                      steps=[{"name": "slow page /", "status": "warn", "detail": "Homepage took 6.2 s to load"}])
    seo = _case("health.meta.home", "confirmed_fail", "seo", steps=[{"name": "meta", "status": "fail", "detail": "No meta description"}])
    skipped = _case("search.tee", "blocked", "major", steps=[{"name": "open", "status": "blocked", "detail": "not judged: no search box"}])
    run = {"verdict": "down", "platform": "shopify", "notes": ["n1"],
           "cases": [slow_home, failed_pdp, seo, skipped, _case("smoke.nav_links")]}
    assert w.score_of(run) == 100 - 30 - 4 - 3
    s = w.summarize(run)
    # passed counts slow_home (passed with a warning) and nav_links; the warning is counted on top, not instead
    assert (s["checked"], s["passed"], s["failed"], s["warnings"], s["not_tested"]) == (5, 2, 2, 1, 1)
    assert [p["key"] for p in s["problems"]] == ["product.pdp.tee", "health.meta.home", "smoke.home_health"]
    assert s["problems"][0]["name"] == "Product page 'Tee'" and s["problems"][0]["reason"] == "The product page shows no price."
    assert s["problems"][2] == {"key": "smoke.home_health", "name": "smoke.home_health", "severity": "critical",
                                "status": "warning", "reason": "Homepage took 6.2 s to load"}
    # a store Radar could not test has no score: nothing was measured
    assert w.score_of({"verdict": "blocked", "cases": [slow_home]}) is None
    assert w.score_of({"verdict": "unsupported", "cases": []}) is None
    assert w.score_of({"verdict": "down", "cases": [_case("a.b", "confirmed_fail", "critical")] * 4}) == 0


def test_supabase_queue_scan_functions(monkeypatch):
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url.rsplit("/", 1)[1], json.loads(req.data)))
        return Resp(json.dumps([{"id": "s1", "url": "https://x.in"}]).encode() if req.full_url.endswith("claim_scan") else b"")
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    q = w.SupabaseQueue("https://ref.supabase.co", "k")
    assert q.claim_scan("box-1") == {"id": "s1", "url": "https://x.in"}
    q.finish_scan("s1", "healthy", 97, {"checked": 8}, "run-uuid", "proj-uuid", "n" * 400)
    assert calls[0] == ("claim_scan", {"worker": "box-1"})
    fn, body = calls[1]
    assert fn == "finish_scan" and body["scan_id"] == "s1" and body["score"] == 97 and body["run_id"] == "run-uuid"
    assert body["failed"] is False and len(body["note"]) == 300


def test_scan_progress_drives_the_loading_screen_list():
    from types import SimpleNamespace as NS
    sent = []
    prog = w.ScanProgress(lambda st: sent.append(json.loads(json.dumps(st))), log=lambda *_: None)
    prog("start", {})
    assert sent[-1]["stage"] == "opening"
    prog("discovered", {"sitemap": NS(platform="shopify", theme="Dawn", products=[1, 2, 3], collections=[1],
                                       home_title="Tee Shop")})
    cases = [NS(id="smoke.home_health", title="Homepage loads healthy"), NS(id="product.pdp.tee", title="PDP 'Tee'"),
             NS(id="search.tee", title="Search for 'tee' returns products")]
    prog("suites", {"suites": [NS(cases=cases[:2]), NS(cases=cases[2:])]})
    st = sent[-1]
    assert st["stage"] == "testing" and st["store"]["products"] == 3 and st["store"]["theme"] == "Dawn"
    assert [(c["name"], c["state"]) for c in st["checks"]] == [
        ("Homepage loads healthy", "running"), ("Product page 'Tee'", "waiting"), ("Search for 'tee' returns products", "waiting")]
    prog("attempt", {"case": cases[0], "attempt": NS(ok=True, steps=[NS(status="warn")])})
    assert [c["state"] for c in sent[-1]["checks"]] == ["warning", "running", "waiting"]
    prog("attempt", {"case": cases[1], "attempt": NS(ok=False, steps=[])})          # first try failed: tried again
    assert [c["state"] for c in sent[-1]["checks"]] == ["warning", "retrying", "waiting"]
    run = NS(cases=[NS(case_id="smoke.home_health", verdict="pass"), NS(case_id="product.pdp.tee", verdict="confirmed_fail"),
                    NS(case_id="search.tee", verdict="blocked")])
    prog("done", {"run": run})
    assert sent[-1]["stage"] == "finishing"
    assert [c["state"] for c in sent[-1]["checks"]] == ["warning", "failed", "not_tested"]
    # a progress write that fails never stops the scan
    broken = w.ScanProgress(lambda st: (_ for _ in ()).throw(OSError("down")), log=lambda *_: None)
    broken("start", {})


def test_upload_key_mode_serves_free_scans_only_through_the_by_key_functions(monkeypatch):
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url.rsplit("/", 1)[1], json.loads(req.data), req.headers.get("Apikey")))
        return Resp(json.dumps([{"id": "s1", "url": "https://x.in"}]).encode() if "claim_scan" in req.full_url else b"")
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    q = w.SupabaseQueue("https://ref.supabase.co", None, upload_key="brk_abc")
    assert q.claim("box") is None and calls == []                     # monitors need the service key: never asked
    assert q.claim_scan("box") == {"id": "s1", "url": "https://x.in"}
    q.scan_progress("s1", {"stage": "testing"})
    q.finish_scan("s1", "healthy", 100, {}, None, None, "ok")
    assert [c[0] for c in calls] == ["claim_scan_by_key", "scan_progress_by_key", "finish_scan_by_key"]
    assert all(c[1]["brk"] == "brk_abc" and c[2] == w.ANON_KEY for c in calls)
    with pytest.raises(ValueError):
        w.SupabaseQueue("https://ref.supabase.co", None)


def test_serve_for_drains_scans_then_stops_at_the_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr("radar.runner.executor.scan_devices", lambda url, settings, devices, **kw: [(_fake_run(), tmp_path)])
    q = ScanQueue([{"id": "s1", "url": "https://a.in"}, {"id": "s2", "url": "https://b.in"}],
                  [{"id": "m1", "url": "https://y.in", "devices": ["desktop"]}])
    n = w.serve_for(q, Settings(), "gh", seconds=5, upload=False, log=lambda *_: None, scans_only=True, poll_seconds=1)
    assert n == 2 and [f["id"] for f in q.scan_finished] == ["s1", "s2"]
    assert q.finished == []                                            # scans only: the monitor was never claimed


def test_queue_from_env_accepts_either_key(monkeypatch):
    monkeypatch.delenv("BUGRADAR_SERVICE_KEY", raising=False); monkeypatch.delenv("BUGRADAR_KEY", raising=False)
    with pytest.raises(SystemExit):
        w.queue_from_env()
    monkeypatch.setenv("BUGRADAR_KEY", "brk_x")
    q = w.queue_from_env()
    assert q.upload_key == "brk_x" and q.key == w.ANON_KEY and q.url.endswith(".supabase.co")
    monkeypatch.setenv("BUGRADAR_SERVICE_KEY", "service")
    q = w.queue_from_env()
    assert q.upload_key is None and q.key == "service"
