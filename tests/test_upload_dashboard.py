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
    assert by["cart.add"]["reason"] == "A chat widget covers the button." and by["cart.add"]["attempts"] == 2
    assert by["cart.add"]["artifacts"][0]["content_type"] == "image/png"      # not a real picture: sent as it is
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


def test_finds_runs_in_the_engines_own_data_folder(tmp_path, capsys):
    """A GitHub machine has data/sites/<store>/runs/<run>/run.json; the uploader must find those (13 Oct: it found none)."""
    d = tmp_path / "data" / "sites" / "shop.example" / "runs" / "r1"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(RUN))
    assert up.main([str(tmp_path / "data"), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"project": "shop.example"' in out and "1 found" in out


def test_phone_failure_gets_the_small_step_photo_when_the_full_page_one_is_too_big(tmp_path):
    """13 Oct: phone full-page PNGs are over the size limit, so phone failures reached the dashboard with no picture."""
    d = tmp_path / "data" / "sites" / "shop.example" / "runs" / "r1"
    d.mkdir(parents=True)
    (d / "shot.png").write_bytes(b"x" * (up.MAX_SHOT_BYTES + 1))
    (d / "a2.02-add.jpg").write_bytes(b"\xff\xd8 small")
    run = json.loads(json.dumps(RUN))
    run["cases"][1]["attempts"][-1].update(failed_step="add", steps=[{"name": "open", "status": "pass", "shot": "a2.01-open.jpg"},
                                                                      {"name": "add", "status": "fail", "shot": "a2.02-add.jpg"}])
    art = {r["key"]: r for r in up.to_payload(run, d, "", None)["results"]}["cart.add"]["artifacts"][0]
    assert art["name"] == "a2.02-add.jpg" and art["content_type"] == "image/jpeg"


def test_build_day_is_the_shoppers_day_and_names_are_plain(tmp_path):
    run = json.loads(json.dumps(RUN))
    run["started_at"] = "2026-10-12T23:40:00+00:00"                      # 05:10 in India on the 13th
    run["cases"][0]["title"] = "PDP 'Vitamin C Serum'"
    run["cases"][1]["triage"] = {"verdict": "radar_problem", "reason": "A popup covers the button."}
    p = up.to_payload(run, tmp_path, "", None)
    by = {r["key"]: r for r in p["results"]}
    assert p["build"]["version"] == "2026-10-13"
    assert by["smoke.home"]["name"] == "Product page 'Vitamin C Serum'"
    assert by["cart.add"]["reason"] == "A popup covers the button (this may be the test's fault, not the store's)."


def test_every_run_has_a_line_for_the_store_itself(tmp_path):
    """A store that cannot be opened has no checks at all: it must still arrive, as a failure (13 Oct: it was dropped)."""
    ok = up.to_payload(RUN, tmp_path, "", None)["results"][0]
    assert (ok["key"], ok["status"]) == (up.SITE_KEY, "passed")
    down = {"run_id": "r2", "site_id": "gone.example", "base_url": "https://gone.example", "device": "desktop", "verdict": "unreachable",
            "started_at": "2026-10-12T10:00:00+00:00", "cases": [], "notes": ["gone.example: the domain does not resolve"]}
    rs = up.to_payload(down, tmp_path, "", None)["results"]
    assert len(rs) == 1 and rs[0]["status"] == "failed" and "does not resolve" in rs[0]["reason"]
    long = "gone.example: the domain does not resolve. Check the address (robots.txt request: APIRequestContext.get: getaddrinfo ENOTFOUND gone.example)"
    r = up.to_payload({**down, "notes": [long]}, tmp_path, "", None)["results"][0]
    assert r["reason"] == "gone.example: the domain does not resolve. Check the address" and r["error"] == long
    for verdict, want in (("blocked", "blocked"), ("unsupported", "skipped")):
        assert up.to_payload({**down, "verdict": verdict}, tmp_path, "", None)["results"][0]["status"] == want
    d = tmp_path / "data" / "sites" / "gone.example" / "runs" / "r2"
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(down))
    assert up.main([str(tmp_path / "data"), "--dry-run"]) == 0


def test_a_big_full_page_png_goes_up_as_a_small_jpeg(tmp_path, monkeypatch):
    """Checks without step photos (product pages) only have the full-page PNG; on phones it is over the limit (13 Oct)."""
    Image = __import__("pytest").importorskip("PIL.Image")
    d = tmp_path / "data" / "sites" / "shop.example" / "runs" / "r1"
    d.mkdir(parents=True)
    im = Image.effect_noise((1082, 2202), 8).convert("RGB")     # photo-like: fine grain everywhere, so the PNG is large
    im.save(d / "shot.png")
    small = up.smaller(d / "shot.png")
    assert small and small[0] == "shot.jpg" and len(small[2]) < (d / "shot.png").stat().st_size
    monkeypatch.setattr(up, "MAX_SHOT_BYTES", (len(small[2]) + (d / "shot.png").stat().st_size) // 2)   # PNG over, JPEG under
    art = {r["key"]: r for r in up.to_payload(RUN, d, "", None)["results"]}["cart.add"]["artifacts"][0]
    assert art["name"] == "shot.jpg" and art["content_type"] == "image/jpeg"


def test_store_name_drops_www(tmp_path):
    assert up.to_payload({**RUN, "base_url": "https://www.shop.example"}, tmp_path, "", None)["project"] == "shop.example"


def test_a_sold_out_product_is_tagged(tmp_path):
    run = json.loads(json.dumps(RUN))
    run["cases"][0]["attempts"][-1]["steps"] = [{"name": "sold_out_product", "status": "info", "detail": "sold out: /products/x"}]
    by = {r["key"]: r for r in up.to_payload(run, tmp_path, "", None)["results"]}
    assert by["smoke.home"]["status"] == "passed" and by["smoke.home"]["tags"] == ["sold out"] and "sold out" in by["smoke.home"]["reason"]
    assert "tags" not in by["cart.add"]


def test_a_failed_check_carries_the_page_it_stopped_on(tmp_path):
    """The dashboard's 'Open this page' link: always the store's own address, never a private or made-up page."""
    base = "https://shop.example"
    case = lambda **a: {"check": "product_page", "attempts": [{"steps": [], "loads": [], **a}]}          # noqa: E731
    # the page the last try ended on
    assert up.failing_page(case(loads=[{"url": "/"}, {"url": "/collections/all"}]), base) == ("https://shop.example/collections/all", None)
    # a link check names the broken link in the step that warned
    steps = [{"name": "footer: opens /pages/about", "status": "pass"}, {"name": "footer: opens /pages/gone", "status": "warn"}]
    assert up.failing_page(case(steps=steps, loads=[{"url": "/pages/about"}]), base)[0] == "https://shop.example/pages/gone"
    # a cart is private to each visitor: the product that was added is linked instead
    assert up.failing_page(case(loads=[{"url": "/products/serum?variant=1"}, {"url": "/cart"}]), base) == ("https://shop.example/products/serum?variant=1", "cart")
    assert up.failing_page(case(loads=[{"url": "/cart"}]), base) == (None, "cart")
    # the named product's page was hidden and the next catalog product was tested: the link is that one, and says so
    moved = [{"name": "loads", "status": "warn"}, {"name": "loads_next_catalog_product_1", "status": "pass"}, {"name": "shows_title_price_image", "status": "fail"}]
    assert up.failing_page(case(steps=moved, loads=[{"url": "/products/a"}, {"url": "/products/b"}]), base) == ("https://shop.example/products/b", "next_product")
    # another site is never linked; the made-up 404 address has no page at all
    assert up.failing_page(case(loads=[{"url": "https://evil.example/x"}]), base) == (None, None)
    assert up.failing_page({"check": "not_found", "attempts": [{"loads": [{"url": "/"}]}]}, base) == (None, "test_address")
    # and it is in the payload, with the steps of the last try, for failed checks only
    run = json.loads(json.dumps(RUN))
    run["cases"][1]["attempts"][-1].update(loads=[{"url": "/products/serum"}], steps=[{"name": "open", "status": "pass"}, {"name": "add", "status": "fail"}])
    by = {r["key"]: r for r in up.to_payload(run, tmp_path, "", None)["results"]}
    assert by["cart.add"]["page_url"] == "https://shop.example/products/serum" and [s["name"] for s in by["cart.add"]["steps"]] == ["open", "add"]
    assert "page_url" not in by["smoke.home"]
