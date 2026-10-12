"""The dashboard uploader turns a Radar run.json into the dashboard's results format and posts it with the key."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tools import upload_dashboard as up

RUN = {
    "run_id": "r1", "site_id": "shop.example", "base_url": "https://shop.example", "device": "mobile",
    "started_at": "2026-10-12T10:00:00+00:00", "finished_at": "2026-10-12T10:02:00+00:00", "verdict": "degraded",
    "cases": [
        {"case_id": "smoke.home", "suite": "smoke", "title": "Home opens", "check": "x", "severity": "critical",
         "verdict": "pass", "attempts": [{"n": 1, "ok": True, "secs": 1.5, "steps": []}]},
        {"case_id": "cart.add", "suite": "cart", "title": "Add to cart", "check": "x", "severity": "critical",
         "verdict": "confirmed_fail", "triage": {"reason": "A chat widget covers the button"},
         "attempts": [{"n": 1, "ok": False, "secs": 2, "error": "covered", "screenshot": "shot.png", "steps": []},
                      {"n": 2, "ok": False, "secs": 2, "error": "covered", "screenshot": "shot.png", "steps": []}]},
        {"case_id": "search.q", "suite": "search", "title": "Search", "check": "x", "severity": "major", "verdict": "blocked",
         "attempts": [{"n": 1, "ok": False, "steps": [{"name": "s", "status": "blocked", "detail": "robots.txt disallows /search"}]}]},
        {"case_id": "cat.more", "suite": "catalog", "title": "More", "check": "x", "severity": "major", "verdict": "flaky",
         "attempts": [{"n": 1, "ok": False}, {"n": 2, "ok": True}]},
    ],
}


def write_run(tmp_path):
    d = tmp_path / "runs" / "x" / "sites" / "shop.example" / "r1"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(RUN))
    (d / "shot.png").write_bytes(b"\x89PNG fake")
    return d


def test_payload_maps_verdicts_reasons_and_evidence(tmp_path):
    d = write_run(tmp_path)
    p = up.to_payload(RUN, d, "", None)
    assert p["project"] == "shop.example" and p["build"]["version"] == "2026-10-12"
    assert p["run"]["tool"] == "radar" and p["run"]["device"] == "mobile"
    by = {r["key"]: r for r in p["results"]}
    assert [by[k]["status"] for k in ("smoke.home", "cart.add", "search.q", "cat.more")] == ["passed", "failed", "blocked", "flaky"]
    assert by["cart.add"]["reason"] == "A chat widget covers the button" and by["cart.add"]["attempts"] == 2
    assert by["cart.add"]["artifacts"][0]["content_type"] == "image/png"
    assert "robots.txt" in by["search.q"]["reason"] and "artifacts" not in by["search.q"]
    assert by["cat.more"]["reason"].startswith("Passed on try 2")


def test_sends_with_key_and_never_fails_without_one(tmp_path, monkeypatch, capsys):
    write_run(tmp_path)
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"results": 4, "incidents_opened": 1}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(up, "INGEST_URL", f"http://127.0.0.1:{srv.server_port}/ingest")
    monkeypatch.setenv("no_proxy", "127.0.0.1"); monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.delenv("BUGRADAR_KEY", raising=False)
    assert up.main([str(tmp_path)]) == 0 and not got          # no key: nothing sent, run not failed
    monkeypatch.setenv("BUGRADAR_KEY", "brk_test")
    assert up.main([str(tmp_path), "--ci-url", "https://ci/1"]) == 0
    srv.shutdown()
    assert got[0][0] == "Bearer brk_test" and got[0][1]["run"]["ci_url"] == "https://ci/1"
    assert "1 sent, 0 failed" in capsys.readouterr().out
