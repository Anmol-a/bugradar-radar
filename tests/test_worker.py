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
