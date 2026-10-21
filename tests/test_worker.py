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


# ---- a store that didn't load is looked at again before anyone is told (21 Oct) ----------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402


def _ago(minutes):
    """A recheck_since the way PostgREST sends it (5-digit fraction, +00:00)."""
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond:06d}"[:5] + "+00:00"


def _recheck_setup(monkeypatch, tmp_path, verdicts):
    """scan_devices answers the given verdicts per device (or raises); upload_results is recorded instead of sent."""
    class R:
        def __init__(self, device, verdict): self.device, self.verdict = device, verdict
    def fake(url, settings, devices):
        if verdicts == "crash":
            raise RuntimeError("browser exploded")
        return [(R(d, v), tmp_path) for d, v in zip(devices, verdicts)]
    monkeypatch.setattr("radar.runner.executor.scan_devices", fake)
    sent = []
    monkeypatch.setattr(w, "upload_results", lambda data_dir, since, log=print, extra_meta=None: sent.append(extra_meta) or ([], ""))
    return sent


def _one(monitor, verdicts, monkeypatch, tmp_path):
    sent = _recheck_setup(monkeypatch, tmp_path, verdicts)
    q = FakeQueue([{"id": "m1", "url": "https://x.in", "devices": ["desktop"], **monitor}])
    w.run_once(q, Settings(), "box-1", upload=True, log=lambda *_: None)
    (mid, status, note), = q.finished
    return status, note, sent


FIRST = {"last_status": "healthy", "rechecks": 0, "recheck_since": None}
SECOND = {"last_status": "recheck", "rechecks": 1, "recheck_since": _ago(11)}


def test_a_store_that_didnt_load_is_not_uploaded_the_first_time(monkeypatch, tmp_path):
    status, note, sent = _one(FIRST, ["unreachable"], monkeypatch, tmp_path)
    assert status == "recheck" and sent == []                      # no run, no problem, no email for a blip
    assert note.startswith("desktop: unreachable (") and note.endswith("didn't load, looking again in 10 minutes")
    # a store already down yesterday gets the same first look (its problem is already open: no new email either way)
    assert _one({**FIRST, "last_status": "unreachable"}, ["unreachable"], monkeypatch, tmp_path)[:3:2] == ("recheck", [])
    # a brand-new monitor (never ran) too
    assert _one({"last_status": None}, ["unreachable"], monkeypatch, tmp_path)[:3:2] == ("recheck", [])


def test_the_second_look_is_uploaded_and_says_so(monkeypatch, tmp_path):
    # still down: uploaded (the dashboard opens the problem and alerts) with how long after the first look
    status, note, sent = _one(SECOND, ["unreachable"], monkeypatch, tmp_path)
    assert status == "unreachable" and sent == [{"monitor_id": "m1", "rechecked": True, "recheck_minutes": 11}]
    assert note.endswith("still didn't load 11 min after the first look")
    # back by then: uploaded as a normal run, nothing was ever opened; the note keeps the trace of the blip
    status, note, sent = _one({**SECOND, "devices": ["desktop", "mobile"]}, ["healthy", "healthy"], monkeypatch, tmp_path)
    assert status == "healthy" and sent == [{"monitor_id": "m1", "rechecked": True, "recheck_minutes": 11}]
    assert note.endswith("second look, 11 min after it didn't load")
    # a real finding on the second look is uploaded straight away too
    assert _one(SECOND, ["down"], monkeypatch, tmp_path)[0] == "down"


def test_any_device_not_loading_holds_the_whole_first_look(monkeypatch, tmp_path):
    status, _, sent = _one({**FIRST, "devices": ["desktop", "mobile"]}, ["healthy", "unreachable"], monkeypatch, tmp_path)
    assert status == "recheck" and sent == []
    # a phone-only check works the same way
    assert _one({**FIRST, "devices": ["mobile"]}, ["unreachable"], monkeypatch, tmp_path)[:3:2] == ("recheck", [])


def test_only_a_store_that_didnt_load_is_looked_at_again(monkeypatch, tmp_path):
    # real findings and Radar-side states on a FIRST look are uploaded straight away, as before
    for verdict in ("healthy", "degraded", "down", "blocked", "unsupported", "no_network"):
        status, note, sent = _one(FIRST, [verdict], monkeypatch, tmp_path)
        assert status == verdict and sent == [{"monitor_id": "m1"}] and "look" not in note, verdict
    # a crash on a first look is still an error, never a re-check
    assert _one(FIRST, "crash", monkeypatch, tmp_path)[:3:2] == ("error", [])


def test_a_second_look_that_couldnt_test_tries_once_more(monkeypatch, tmp_path):
    # reviewer 21 Oct: the store didn't load, then Radar was offline / crashed / robots.txt was silent on the second look:
    # uploading that would drop a real outage until tomorrow, so one more look (at most 2 re-checks in a row)
    for verdicts in (["no_network"], ["blocked"], ["error"], "crash"):
        status, note, sent = _one(SECOND, verdicts, monkeypatch, tmp_path)
        assert status == "recheck" and sent == [] and "trying again in 10 minutes" in note, verdicts
    # after 2 re-checks: whatever the third look found is uploaded (the cap; nothing loops)
    status, _, sent = _one({**SECOND, "rechecks": 2, "recheck_since": _ago(22)}, ["no_network"], monkeypatch, tmp_path)
    assert status == "no_network" and sent == [{"monitor_id": "m1", "rechecked": True, "recheck_minutes": 22}]
    status, _, sent = _one({**SECOND, "rechecks": 2, "recheck_since": _ago(22)}, "crash", monkeypatch, tmp_path)
    assert status == "error" and sent == []
    # a device that DID test the store is enough: uploaded
    status, _, sent = _one({**SECOND, "devices": ["desktop", "mobile"]}, ["healthy", "no_network"], monkeypatch, tmp_path)
    assert status == "no_network" and sent and sent[0]["rechecked"] is True


def test_an_old_recheck_starts_over(monkeypatch, tmp_path):
    # reviewer 21 Oct: paused during the 10 minutes and resumed days later, or a lock taken over after 3 h: not a second
    # look (the blip filter applies again and nothing claims "it also didn't load N minutes earlier")
    for since in (_ago(61), _ago(3 * 24 * 60), None, "not a time"):
        status, _, sent = _one({**SECOND, "recheck_since": since}, ["unreachable"], monkeypatch, tmp_path)
        assert status == "recheck" and sent == [], since
        status, note, sent = _one({**SECOND, "recheck_since": since}, ["healthy"], monkeypatch, tmp_path)
        assert status == "healthy" and sent == [{"monitor_id": "m1"}] and "second look" not in note, since


def test_postgrest_times_are_read_in_every_shape():
    want = datetime(2026, 10, 21, 10, 15, 52, 630580, tzinfo=timezone.utc)
    for s in ("2026-10-21T10:15:52.63058+00:00", "2026-10-21 10:15:52.63058+00", "2026-10-21T10:15:52.630580Z",
              "2026-10-21T15:45:52.63058+05:30"):
        assert w._when(s) == want, s
    assert w._when("2026-10-21T10:15:52+00:00") == want.replace(microsecond=0)
    assert w._when(None) is None and w._when("") is None and w._when("yesterday") is None


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
    # problems are checks that FAILED; a check that works with a note goes under warning_checks (19 Oct)
    assert [p["key"] for p in s["problems"]] == ["product.pdp.tee", "health.meta.home"]
    assert all(p["status"] == "failed" for p in s["problems"])
    assert s["problems"][0]["name"] == "Product page 'Tee'" and s["problems"][0]["reason"] == "The product page shows no price."
    assert s["warning_checks"] == [{"key": "smoke.home_health", "name": "smoke.home_health", "severity": "critical",
                              "status": "warning", "reason": "Homepage took 6.2 s to load"}]
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


def test_upload_key_mode_serves_free_scans_through_the_by_key_functions(monkeypatch):
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url.rsplit("/", 1)[1], json.loads(req.data), req.headers.get("Apikey")))
        return Resp(json.dumps([{"id": "s1", "url": "https://x.in"}]).encode() if "claim_scan" in req.full_url else b"")
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    q = w.SupabaseQueue("https://ref.supabase.co", None, upload_key="brk_abc")
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


def test_queue_age_script_says_yes_only_for_an_old_waiting_request(monkeypatch, capsys):
    import tools.queue_age as qa

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    answers = iter([[{"queued": 2, "oldest_seconds": 400}], [{"queued": 1, "oldest_seconds": 30}], [{"queued": 0, "oldest_seconds": 0}]])
    monkeypatch.setattr(qa.urllib.request, "urlopen", lambda req, timeout=0: Resp(json.dumps(next(answers)).encode()))
    for want in ("yes", "no", "no"):
        qa.main(["--older-than", "360"])
        assert capsys.readouterr().out.strip() == want
    def boom(req, timeout=0):
        raise OSError("down")
    monkeypatch.setattr(qa.urllib.request, "urlopen", boom)
    qa.main([])
    assert capsys.readouterr().out.strip() == "no"            # unknown = let the next run ask again, never crash


def test_free_scan_takes_a_homepage_picture_only_when_the_store_was_tested(monkeypatch, tmp_path):
    """19 Oct: the result page shows the homepage as Radar saw it; a store Radar could not test gets no picture
    (robots.txt or the platform said no), and a picture that fails never fails the scan."""
    from types import SimpleNamespace as NS
    shots = []
    for verdict, expect in (("healthy", 1), ("down", 1), ("blocked", 0), ("unsupported", 0)):
        run = NS(verdict=verdict, run_id="r", to_dict=lambda v=verdict: {"verdict": v, "cases": []})
        monkeypatch.setattr("radar.runner.executor.scan_devices", lambda *a, run=run, **k: [(run, tmp_path)])
        monkeypatch.setattr(w, "homepage_shot", lambda url, s, d, log=print: shots.append((url, d)) or True)
        before = len(shots)
        out = w.run_scan({"id": "s", "url": "https://x.in"}, Settings(), upload=False, log=lambda *_: None)
        assert len(shots) - before == expect, verdict
        assert out["verdict"] == verdict and out["failed"] is False
    assert shots[0] == ("https://x.in", tmp_path)


def test_homepage_picture_never_raises(tmp_path):
    class Boom:
        def __init__(self, *a, **k): raise RuntimeError("no chromium here")
    import radar.core.browser as br
    real = br.Browser
    br.Browser = Boom
    try:
        said = []
        assert w.homepage_shot("https://x.in", Settings(), tmp_path, log=said.append) is False
        assert "homepage picture skipped" in said[0] and not (tmp_path / w.HOME_SHOT).exists()
    finally:
        br.Browser = real


def test_upload_key_mode_also_serves_the_keys_own_monitors(monkeypatch):
    """19 Oct (dashboard migration 14): with only the upload key, monitors go through claim_monitor_by_key /
    finish_monitor_by_key with the key; with the service key, the plain functions as before."""
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=0):
        fn = req.full_url.rsplit("/", 1)[1]
        calls.append((fn, json.loads(req.data), req.headers.get("Apikey")))
        rows = [{"id": "m1", "url": "https://x.in", "devices": ["desktop"]}] if fn.startswith("claim_monitor") else []
        return Resp(json.dumps(rows).encode())
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    keyed = w.SupabaseQueue("https://ref.supabase.co", None, upload_key="brk_abc")
    assert keyed.claim("box-2") == {"id": "m1", "url": "https://x.in", "devices": ["desktop"]}
    keyed.finish("m1", "healthy", "desktop: healthy (90 s)")
    assert calls[0][:2] == ("claim_monitor_by_key", {"brk": "brk_abc", "worker": "box-2"})
    assert calls[1][:2] == ("finish_monitor_by_key", {"brk": "brk_abc", "monitor_id": "m1", "status": "healthy",
                                                      "note": "desktop: healthy (90 s)"})
    assert calls[0][2] == w.ANON_KEY             # the public key reaches PostgREST; the upload key is the proof
    calls.clear()
    full = w.SupabaseQueue("https://ref.supabase.co", "service-key")
    full.claim("box-1"); full.finish("m1", "down", "n" * 400)
    assert [c[0] for c in calls] == ["claim_monitor", "finish_monitor"] and len(calls[1][1]["note"]) == 300


def test_always_on_worker_honours_scans_only(monkeypatch):
    """19 Oct: `radar worker --scans-only --poll 15` (Contabo slot 1) took a daily monitor: serve() ignored the flag."""
    seen = []
    monkeypatch.setattr(w, "run_once", lambda q, b, wk, upload=True, log=print, scans_only=False: seen.append(scans_only) or {"id": "x"})
    for flag in (True, False):
        n = iter(range(3))
        w.serve(None, Settings(), "box-1", poll_seconds=0, upload=False, log=lambda *_: None, scans_only=flag,
                _stop=lambda: next(n) >= 2)
    assert seen == [True, True, False, False]
    # and the command line passes it through to the always-on mode
    got = {}
    monkeypatch.setattr(w, "serve", lambda q, b, wk, poll, upload=True, scans_only=False: got.update(poll=poll, scans_only=scans_only))
    monkeypatch.setattr(w, "queue_from_env", lambda: None)
    monkeypatch.setattr(w, "load_settings", lambda: Settings())
    w.main(["--scans-only", "--poll", "15"])
    assert got == {"poll": 15, "scans_only": True}


def test_a_daily_check_tells_the_dashboard_which_monitor_it_was(monkeypatch, tmp_path):
    # 19 Oct: the dashboard files the run in the monitor's own workspace (a pilot store's), so the upload names the monitor
    class R:
        def __init__(self, device, verdict): self.device, self.verdict = device, verdict
    monkeypatch.setattr("radar.runner.executor.scan_devices", lambda url, settings, devices: [(R("desktop", "healthy"), tmp_path)])
    sent = []
    monkeypatch.setattr(w, "upload_results", lambda data_dir, since, log=print, extra_meta=None: sent.append(extra_meta) or ([], ""))
    status, note = w.run_monitor({"id": "mon-7", "url": "https://p.in", "devices": ["desktop"]}, Settings(), log=lambda *_: None)
    assert status == "healthy" and sent == [{"monitor_id": "mon-7"}]
    sent.clear()
    w.run_monitor({"url": "https://p.in", "devices": ["desktop"]}, Settings(), log=lambda *_: None)   # an old row without an id
    assert sent == [None]
