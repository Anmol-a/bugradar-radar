import base64
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
         "verdict": "confirmed_fail", "triage": {"verdict": "real_store_problem", "reason": "A chat widget covers the button"},
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
    # the owner's reason is the robot's own evidence; the LLM triage rides along in the technical error only
    assert by["cart.add"]["reason"] == "Covered." and by["cart.add"]["attempts"] == 2
    assert by["cart.add"]["error"] == "covered\nTriage (likely a real store problem): A chat widget covers the button"
    assert by["cart.add"]["artifacts"][0]["content_type"] == "image/png"      # not a real picture: sent as it is
    assert "robots.txt" in by["search.q"]["reason"] and "artifacts" not in by["search.q"]
    assert by["cat.more"]["reason"].startswith("Passed on try 2")


def test_sends_with_key_and_never_fails_without_one(tmp_path, monkeypatch, capsys):
    write_run(tmp_path)
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"results": 4, "incidents_opened": 1, "run_id": "run-uuid", "project_id": "proj-uuid"}')

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
    # the worker (15 Oct) calls upload() directly and reads the dashboard's run id off the record
    recs = up.upload(tmp_path, "brk_test", log=lambda *_: None)
    srv.shutdown()
    assert got[0][0] == "Bearer brk_test" and got[0][1]["run"]["ci_url"] == "https://ci/1"
    assert "1 sent, 0 failed" in capsys.readouterr().out
    assert len(recs) == 1 and recs[0]["ok"] and (recs[0]["run_id"], recs[0]["project_id"]) == ("run-uuid", "proj-uuid")
    assert recs[0]["radar_run_id"] == got[1][1]["run"]["meta"]["radar_run_id"]


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
    assert by["smoke.home"]["name"] == "Product page 'Vitamin C Serum'"        # an unknown key: only the shop-talk goes
    assert by["cart.add"]["reason"] == "Covered." and "test's fault" not in by["cart.add"]["reason"]
    assert by["cart.add"]["error"].endswith("Triage (may be the test's fault, not the store's): A popup covers the button.")


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
    # a daily check's second look (worker RECHECK, 21 Oct): the store's line says it wasn't a one-off; nothing else changes
    p = up.to_payload(down, tmp_path, "", None, {"monitor_id": "m1", "rechecked": True, "recheck_minutes": 11})
    assert p["results"][0]["reason"] == "gone.example: the domain does not resolve. It also didn't load 11 minutes earlier."
    assert p["run"]["meta"]["rechecked"] is True and p["run"]["meta"]["monitor_id"] == "m1" and p["run"]["meta"]["recheck_minutes"] == 11
    assert up.to_payload(down, tmp_path, "", None, {"rechecked": True, "recheck_minutes": 1})["results"][0]["reason"].endswith(
        "It also didn't load 1 minute earlier.")
    assert up.to_payload({**down, "notes": []}, tmp_path, "", None, {"rechecked": True})["results"][0]["reason"] == \
        "The store did not load. It also didn't load on an earlier look."
    assert up.to_payload(RUN, tmp_path, "", None, {"rechecked": True, "recheck_minutes": 11})["results"][0]["reason"] is None  # loaded
    assert up.to_payload({**down, "verdict": "blocked"}, tmp_path, "", None, {"rechecked": True})["results"][0]["reason"] == \
        "gone.example: the domain does not resolve"                                                    # not a failure
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


def test_the_homepage_picture_rides_on_the_stores_own_line(tmp_path):
    """19 Oct: a free check's homepage.jpg (worker.homepage_shot) goes up on the SITE_KEY line; none when it is missing
    or over the size limit."""
    run = {"base_url": "https://x.in", "verdict": "healthy", "cases": []}
    assert "artifacts" not in up.to_payload(run, tmp_path, "b", None)["results"][0]
    (tmp_path / "homepage.jpg").write_bytes(b"\xff\xd8jpeg")
    site = up.to_payload(run, tmp_path, "b", None)["results"][0]
    assert site["key"] == up.SITE_KEY
    (a,) = site["artifacts"]
    assert (a["kind"], a["name"], a["content_type"]) == ("screenshot", "homepage.jpg", "image/jpeg")
    assert base64.b64decode(a["data_base64"]) == b"\xff\xd8jpeg"
    (tmp_path / "homepage.jpg").write_bytes(b"x" * (up.MAX_SHOT_BYTES + 1))
    assert "artifacts" not in up.to_payload(run, tmp_path, "b", None)["results"][0]


def test_a_bench_label_files_runs_under_their_own_store(tmp_path, capsys):
    """22 Oct: the GitHub nightly bench filed the Dawn demo into the store BugRadar's daily check watches; a blip there
    (no 10-minute re-check on the bench) opened a problem. With a label it is its own store on the dashboard."""
    assert up.project_name(RUN) == "shop.example"
    assert up.project_name(RUN, "nightly bench") == "shop.example (nightly bench)"
    assert up.project_name({**RUN, "base_url": "https://www.shop.example"}, "  nightly   bench ") == "shop.example (nightly bench)"
    assert up.to_payload(RUN, tmp_path, "", None)["project"] == "shop.example"                    # the worker: unchanged
    assert up.to_payload(RUN, tmp_path, "", None, None, "nightly bench")["project"] == "shop.example (nightly bench)"
    write_run(tmp_path)
    recs = up.upload(tmp_path, "brk_x", dry_run=True, log=lambda *_: None, project_label="nightly bench")
    assert [r["project"] for r in recs] == ["shop.example (nightly bench)"]
    up.main([str(tmp_path), "--dry-run", "--project-label", "nightly bench"])
    assert "shop.example (nightly bench)" in capsys.readouterr().out
    up.main([str(tmp_path), "--dry-run", "--project-label", ""])                                 # manual runs: as before
    assert '"project": "shop.example"' in capsys.readouterr().out


def test_robots_txt_down_while_the_store_is_open_is_a_warning(tmp_path):
    """22 Oct: a daily check found robots.txt failing while the store answers: the store line is a WARNING the owner
    sees (search engines pause crawling), not a quiet 'not tested', and never a problem / alert (status != failed)."""
    note = "robots.txt could not be fetched (HTTP 503, 3 tries), so nothing was tested: ... The homepage answers (HTTP 200) ..."
    run = {**RUN, "verdict": "blocked", "cases": [], "notes": [note], "sitemap_summary": {"store_up_robots_down": 200}}
    site = up.to_payload(run, tmp_path, "", None)["results"][0]
    assert (site["key"], site["status"]) == (up.SITE_KEY, "warning")
    assert site["reason"].startswith("Your store opens, but its robots.txt file isn't loading") and site["error"] == note
    site = up.to_payload({**run, "sitemap_summary": {"store_up_robots_down": 403}}, tmp_path, "", None)["results"][0]
    assert site["status"] == "warning" and site["reason"].startswith("Your store's server answers (HTTP 403)")
    # free checks / benches (no such fact) and other blocked runs: unchanged
    assert up.to_payload({**run, "sitemap_summary": {}}, tmp_path, "", None)["results"][0]["status"] == "blocked"
    assert up.to_payload({**run, "sitemap_summary": {"store_up_robots_down": None}}, tmp_path, "", None)["results"][0]["status"] == "blocked"


# ---------------- owner-facing reasons and names (28 Oct, findings #6 and #7) ----------------

def _failed(cid, title, step, error, triage=None, verdict="confirmed_fail"):
    return {"case_id": cid, "suite": cid.split(".")[0], "title": title, "severity": "major", "verdict": verdict, "triage": triage,
            "attempts": [{"n": 1, "ok": False, "failed_step": step, "error": error,
                          "steps": [{"name": "open /pages/about", "status": "pass"}, {"name": step, "status": "fail", "error": error}]}]}


NAV_404 = "broken navigation pages: expected 0, got 1: /pages/this-page-does-not-exist"
NAV_TRIAGE = {"verdict": "radar_problem", "category": "locator",
              "reason": "The robot reported a broken navigation link but the failed URL (/pages/this-page-does-not-exist) does not "
                        "appear in the visible page text, so the monitor likely looked in the wrong place (locator) rather than "
                        "the store actually being broken"}


def test_a_broken_menu_link_is_told_in_plain_words_and_the_triage_stays_technical():
    """28 Oct (finding #6): a tester saw 'the monitor likely looked in the wrong place ... this may be the test's fault'
    under a menu link that really answered HTTP 404. The owner's reason is built from the robot's evidence only."""
    case = _failed("smoke.nav_links", "Navigation links resolve", "all_links_ok", NAV_404, NAV_TRIAGE)
    assert up.reason_of(case) == "A link in your main menu opens a page that doesn't exist: /pages/this-page-does-not-exist."
    err = up.error_of(case)
    assert err.startswith(NAV_404 + "\nTriage (may be the test's fault, not the store's): The robot reported")
    for bad in ("test's fault", "locator", "monitor", "triage", "robot"):
        assert bad not in up.reason_of(case).lower()
    # several broken links; a link that failed for another reason (no list) still reads plainly
    two = _failed("smoke.nav_links", "Navigation links resolve", "all_links_ok", "broken navigation pages: expected 0, got 2: /a, /b")
    assert up.reason_of(two) == "2 links in your main menu open pages that don't exist: /a, /b."
    assert up.reason_of(_failed("smoke.nav_links", "Navigation links resolve", "all_links_ok", "")) == \
        "A link in your main menu opens a page that doesn't load."
    # and in the payload: reason plain, error technical, name plain
    by = {r["key"]: r for r in up.to_payload({**RUN, "cases": [case]}, __import__("pathlib").Path("."), "", None)["results"]}
    assert by["smoke.nav_links"]["name"] == "Menu links"
    assert by["smoke.nav_links"]["reason"] == "A link in your main menu opens a page that doesn't exist: /pages/this-page-does-not-exist."
    assert "Triage (may be the test's fault" in by["smoke.nav_links"]["error"]


def test_triage_wording_never_reaches_the_owner_whatever_its_verdict():
    for verdict in ("radar_problem", "unsure", "real_store_problem", None):
        tri = {"verdict": verdict, "reason": "the monitor likely looked in the wrong place (this may be the test's fault, not the store's)"}
        case = _failed("cart.add_to_cart", "Add 'Tee' to cart", "click_add_to_cart",
                       "buy button could not be clicked: a chat widget covers it", tri)
        reason = up.reason_of(case)
        assert reason == "Adding a product to the cart didn't work: buy button could not be clicked: a chat widget covers it."
        assert "fault" not in reason and "monitor" not in reason and "triage" not in reason.lower()
        assert up.TRIAGE_NOTE["radar_problem"] not in reason and up.TRIAGE_NOTE["unsure"] not in reason
        assert up.error_of(case).startswith("buy button could not be clicked") and "Triage (" in up.error_of(case)
    # no triage at all: the error is just the robot's
    assert up.error_of(_failed("cart.add_to_cart", "Add 'Tee' to cart", "s", "covered")) == "covered"
    assert up.error_of({"attempts": [{}]}) is None


def test_pass_blocked_flaky_and_unknown_keys():
    assert up.reason_of({"verdict": "pass", "attempts": [{}]}) is None
    blocked = {"case_id": "search.tee", "verdict": "blocked", "attempts": [{"steps": [{"name": "s", "status": "blocked", "detail": "robots.txt disallows /search"}]}]}
    assert up.reason_of(blocked) == "robots.txt disallows /search"
    assert up.reason_of({"case_id": "x.y", "verdict": "blocked", "attempts": [{"note": "not judged: no box"}]}) == "not judged: no box"
    assert up.reason_of({"verdict": "flaky", "attempts": [{}, {}]}) == "Passed on try 2 after failing first"
    # an unknown check key: the robot's evidence, lightly cleaned, capitalised, one sentence, no longer than 300 chars
    odd = _failed("cart.price_consistency", "Same price everywhere", "card_price", "card price: expected ₹499, got ₹599")
    assert up.reason_of(odd) == "Card price was ₹599 (expected ₹499)."
    long = _failed("cart.drawer_and_page", "Cart drawer", "drawer", "x" * 500)
    assert len(up.reason_of(long)) <= 300 and up.reason_of(long).endswith("….")
    assert up.reason_of(_failed("some.new_check", "New", "s", "")) is None


def test_plain_reasons_per_check_kind():
    r = lambda cid, title, step, err: up.reason_of(_failed(cid, title, step, err))        # noqa: E731
    assert r("smoke.home_health", "Homepage loads healthy", "loads", "HTTP status: expected < 400, got 503") == \
        "Your homepage didn't load properly: the server answered HTTP 503."
    assert r("smoke.home_health", "Homepage loads healthy", "loads", "page not blank: expected ≥ 40 chars of text or an image, got 0 chars, 0 images") == \
        "Your homepage didn't load properly: the page was blank."
    assert r("smoke.home_health", "Homepage loads healthy", "loads", "page did not load: no page within 30 s, nor within 60 s on a second try (Timeout)") == \
        "Your homepage didn't load properly: it didn't load within 60 seconds."
    assert r("smoke.mobile_menu", "Menu opens on phones", "menu_opens", "menu opens when its button is tapped: expected ≥ 3 menu links appear, got none") == \
        "The menu button didn't open the menu in the mobile browser."
    assert r("smoke.whatsapp", "WhatsApp chat button opens a chat with a real phone number", "whatsapp_link", "x") == \
        "The WhatsApp button doesn't open a chat with a valid number."
    assert r("smoke.more_links", "Links beyond the menu open", "all_links_ok",
             "links beyond the menu that fail to open: expected 0, got 1: footer /pages/gone ('About')") == \
        "A link on your homepage, announcement bar or footer opens a page that doesn't work: footer /pages/gone ('About')."
    assert r("cart.edit_and_checkout", "Cart page: change quantity", "checkout_opens", "checkout page: expected renders, got HTTP 500") == \
        "The cart page or the checkout button didn't work: checkout page was HTTP 500 (expected renders)."
    assert r("search.serum", "Search for 'serum' returns products", "returns_relevant_products", "product results: expected ≥ 1, got 0") == \
        "Search didn't return products for 'serum'."
    assert r("search.no_results", "Search for a word no store sells shows a 'no results' page", "loads", "HTTP status: expected < 400, got 500") == \
        "Searching for a word no store sells didn't show a proper 'no results' page."
    assert r("search.suggestions", "Typing 'serum' in the search box suggests products", "suggestions_while_typing", "x") == \
        "Typing 'serum' in the search box didn't suggest any products."
    pdp = "PDP 'Vitamin C Serum'"
    assert r("product.pdp.vitamin-c-serum", pdp, "shows_title_price_image", "selected variant price shown: expected ₹499, got not on page") == \
        "The product page 'Vitamin C Serum' is missing its price."
    assert r("product.pdp.vitamin-c-serum", pdp, "shows_title_price_image",
             "product name shown on the page: expected 'Vitamin C Serum', got none: no visible text matching the product name") == \
        "The product page 'Vitamin C Serum' is missing its name."
    assert r("product.pdp.vitamin-c-serum", pdp, "shows_title_price_image", "main product image loaded: expected image ≥150px rendered, got a large image failed to load: /x.jpg") == \
        "The product page 'Vitamin C Serum' is missing its picture."
    assert r("product.pdp.vitamin-c-serum", pdp, "buy_button_ready", "buy button enabled: expected True, got False (button.disabled)") == \
        "The product page 'Vitamin C Serum' doesn't let shoppers add it to the cart: the buy button is missing or disabled."
    assert r("product.pdp.vitamin-c-serum", pdp, "loads", "HTTP status: expected < 400, got 404") == \
        "The product page 'Vitamin C Serum' didn't load properly: the server answered HTTP 404."
    assert r("product.pincode", "Pincode checker on 'Vitamin C Serum' answers for a delivery pincode", "pincode_answers",
             "pincode checker answers for 400060: expected a delivery answer (deliverable / not deliverable / days), got nothing changed") == \
        "The pincode checker on 'Vitamin C Serum' didn't answer for 400060."
    assert r("catalog.collection.serums", "Collection 'Serums' lists products", "lists_products", "products listed: expected ≥ 1, got 0") == \
        "The collection 'Serums' shows no products."
    assert r("catalog.collection.serums", "Collection 'Serums' lists products", "loads", "HTTP status: expected < 400, got 500") == \
        "The collection 'Serums' didn't load properly: the server answered HTTP 500."
    assert r("catalog.more.serums", "Collection 'Serums' shows products past the first page", "page_2_opens", "HTTP status: expected < 400, got 404") == \
        "The collection 'Serums' doesn't show more products past its first page."
    assert r("catalog.filter.serums", "Filtering collection 'Serums' narrows its products", "filter_narrows_results", "x") == \
        "Filtering the collection 'Serums' doesn't narrow its products."
    assert r("journey.shopper", "Home → collection → product", "click_into_product", "product card a shopper can click: expected found, got none") == \
        "A shopper's path (menu → product → cart) broke at: opening a product from the collection."
    assert r("journey.shopper", "Home → collection → product", "checkout_button_ready", "x") == \
        "A shopper's path (menu → product → cart) broke at: the checkout button."
    assert r("health.not_found", "Missing page returns 404", "returns_404", "HTTP status for a page that does not exist: expected 404, got 200") == \
        "A missing page doesn't show a proper 'page not found' page."
    assert r("health.meta.home", "Homepage meta tags", "title", "<title> length: expected 10–70 chars, got missing") == \
        "The homepage is missing its title or description."
    assert r("health.meta.pdp", "Product page meta tags", "meta_description", "x") == "The product page is missing its title or description."
    assert r("info.policy_pages", "Footer info pages load with real content (shipping, contact)", "all_info_pages_open",
             "footer info pages that fail to open: expected 0, got 1: shipping (/policies/shipping-policy)") == \
        "An info page linked in your footer doesn't open: shipping (/policies/shipping-policy)."
    assert r("info.account_page", "Account login page loads", "loads", "HTTP status: expected < 400, got 500") == \
        "The account login page didn't open properly: HTTP status was 500 (expected < 400)."


def test_plain_names_by_check_key():
    n = up.plain_name
    assert n("Navigation links resolve", "smoke.nav_links") == "Menu links"
    assert n("Homepage loads healthy", "smoke.home_health") == "Homepage"
    assert n("Menu opens on phones", "smoke.mobile_menu") == "Mobile menu"
    assert n("Links beyond the menu open (announcement bar, homepage, footer)", "smoke.more_links") == "Footer and banner links"
    assert n("WhatsApp chat button opens a chat with a real phone number", "smoke.whatsapp") == "WhatsApp button"
    assert n("Home → collection → product → add to cart", "journey.shopper") == "Shopper's path"
    assert n("Add 'Tee' to cart", "cart.add_to_cart") == "Add to cart"
    assert n("Cart page: change quantity, checkout opens, remove item", "cart.edit_and_checkout") == "Cart page and checkout button"
    assert n("Same price on the collection card ...", "cart.price_consistency") == "Prices match across pages"
    assert n("Cart drawer and cart page ...", "cart.drawer_and_page") == "Cart drawer"
    assert n("Discount box on the cart page ...", "cart.discount_code") == "Discount box"
    assert n("'Buy it now' on 'Tee' opens a checkout", "cart.buy_now") == "Buy it now"
    assert n("Search for a word no store sells shows a 'no results' page", "search.no_results") == "Search: no-results page"
    assert n("Typing 'serum' in the search box suggests products", "search.suggestions") == "Search suggestions"
    assert n("Search for 'serum' returns products", "search.serum") == "Search: serum"
    assert n("PDP 'Vitamin C Serum'", "product.pdp.vitamin-c-serum") == "Product page: Vitamin C Serum"
    assert n("Picking another variant of 'Tee' updates the page", "product.variant_switch") == "Variant picker"
    assert n("Pincode checker on 'Tee' answers for a delivery pincode", "product.pincode") == "Pincode checker"
    assert n("Collection 'Serums' lists products", "catalog.collection.serums") == "Collection: Serums"
    assert n("Collection 'Serums' shows products past the first page", "catalog.more.serums") == "Collection paging: Serums"
    assert n("Filtering collection 'Serums' narrows its products", "catalog.filter.serums") == "Collection filters: Serums"
    assert n("Homepage meta tags", "health.meta.home") == "Homepage title and description"
    assert n("Product page meta tags", "health.meta.pdp") == "Product page title and description"
    assert n("Missing page returns 404", "health.not_found") == "Missing-page (404) page"
    assert n("Footer info pages load with real content (shipping)", "info.policy_pages") == "Info pages (shipping, contact)"
    assert n("Account login page loads", "info.account_page") == "Account login page"
    assert n("x", up.SITE_KEY) == up.SITE_NAME
    # no key / an unknown key: as before, only 'PDP' is replaced; a product check without a quoted title keeps its kind
    assert n("PDP 'Tee'") == "Product page 'Tee'" and n("Some new check", "new.check") == "Some new check"
    assert n("product.pdp.tee", "product.pdp.tee") == "Product page"
