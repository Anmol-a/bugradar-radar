"""Unit tests for pure logic (no browser)."""
import json
from dataclasses import replace

import pytest

from radar.core.config import Settings, normalize_url, site_id_from_url, _apply
from radar.core.models import SiteMap, Product, Collection, CaseResult
from radar.core.storage import Storage
from radar.discovery.shopify_data import products_from_json, product_from_js
from radar.generate.builder import build_suites
from radar.healing.locator import score
from radar.healing.llm import LLMClient, extract_json, make_client
from radar.runner.confirm import Attempt, should_retry, verdict
from radar.runner.executor import run_verdict


# ---------- config ----------
def test_normalize_and_site_id():
    assert normalize_url("Moxiebeauty.in/products/x?y=1") == "https://moxiebeauty.in"
    assert normalize_url("http://localhost:8765/a") == "http://localhost:8765"
    assert site_id_from_url("https://www.Vaaree.com/x") == "vaaree.com"
    assert site_id_from_url("shop.xyz.in") == "shop.xyz.in"
    assert site_id_from_url("http://127.0.0.1:9000") == "127.0.0.1_9000"
    with pytest.raises(ValueError):
        normalize_url("  ")


def test_site_override_merges_selectors(tmp_path):
    s = Settings(selectors={"add_to_cart": ["a"]})
    s2 = _apply(s, {"delay_seconds": 0, "selectors": {"checkout_button": ["b"]}, "unknown_key": 1})
    assert s2.delay_seconds == 0 and s2.selectors == {"add_to_cart": ["a"], "checkout_button": ["b"]}


# ---------- confirm ----------
OK, BAD = Attempt(True), Attempt(False, "x", "e")


def test_confirm_logic():
    assert verdict([OK]) == "pass" and not should_retry([OK])
    assert should_retry([BAD]) and verdict([BAD, OK]) == "flaky"
    assert not should_retry([BAD, BAD]) and verdict([BAD, BAD]) == "confirmed_fail"
    assert verdict([BAD, OK, BAD]) == "confirmed_fail"


def test_run_verdict_severity():
    mk = lambda v, sev: CaseResult("c", "s", "t", "k", sev, verdict=v)
    assert run_verdict([mk("pass", "critical")]) == "healthy"
    assert run_verdict([mk("flaky", "critical")]) == "degraded"
    assert run_verdict([mk("confirmed_fail", "minor")]) == "degraded"
    assert run_verdict([mk("confirmed_fail", "critical")]) == "down"
    assert run_verdict([mk("blocked", "critical")]) == "blocked"
    assert run_verdict([mk("blocked", "minor")]) == "healthy"
    # SEO notes (soft 404, meta tags) never make a store degraded or down (9 Oct)
    assert run_verdict([mk("confirmed_fail", "seo"), mk("flaky", "seo"), mk("pass", "critical")]) == "healthy"
    assert run_verdict([mk("confirmed_fail", "seo"), mk("confirmed_fail", "major")]) == "degraded"


# ---------- catalog parsing ----------
def test_products_from_json_in_stock_first():
    data = {"products": [
        {"handle": "a", "title": "A", "variants": [{"id": 1, "available": False, "price": "9"}]},
        {"handle": "b", "title": "B", "variants": [{"id": 2, "available": False}, {"id": 3, "available": True, "price": "5"}]},
        {"handle": "", "variants": [{"id": 9}]}, {"handle": "c", "variants": []}]}
    out = products_from_json(data, "https://x.in")
    assert [p["handle"] for p in out] == ["b", "a"]
    assert out[0]["variant_id"] == 3 and out[0]["available"] and out[0]["url"] == "https://x.in/products/b"


def test_product_from_js_converts_paise():
    p = product_from_js({"handle": "v", "title": "V", "variants": [{"id": 7, "available": True, "price": 129900}]}, "https://x.in")
    assert p["price"] == "1299.00" and p["variant_id"] == 7
    assert product_from_js({}, "https://x.in") is None


# ---------- generator ----------
def _sm(**kw):
    sm = SiteMap(site_id="x.in", base_url="https://x.in", platform="shopify",
                 nav=[{"text": "Shop", "url": "https://x.in/collections/all"}],
                 collections=[Collection("all", "All", "https://x.in/collections/all")],
                 products=[Product("sold", "Sold Lamp", "https://x.in/products/sold", 1, "9", False),
                           Product("vase", "Ceramic Vase", "https://x.in/products/vase", 2, "5", True)],
                 search_path="/search")
    for k, v in kw.items():
        setattr(sm, k, v)
    return sm


def test_builder_generates_all_suites():
    suites = build_suites(_sm(), Settings())
    ids = [s.id for s in suites]
    assert ids == ["journey", "smoke", "catalog", "product", "cart", "search", "health"]
    j = suites[0].cases[0]
    assert j.check == "shopper_journey" and j.params["product_handles"] == ["vase"] and j.params["allow_cart"]
    cart = next(s for s in suites if s.id == "cart").cases[0]
    assert cart.params["variant_id"] == 2 and cart.severity == "critical"
    search = next(s for s in suites if s.id == "search").cases[0]
    assert "q=vase" in search.params["url"]
    assert all(c.id.startswith(s.id + ".") for s in suites for c in s.cases)   # stable ids


def test_builder_respects_no_cart_and_unsupported():
    no_cart = build_suites(_sm(), replace(Settings(), allow_cart_flow=False))
    assert "cart" not in [s.id for s in no_cart] and not no_cart[0].cases[0].params["allow_cart"]
    assert build_suites(_sm(platform="unknown"), Settings()) == []
    no_stock = _sm(products=[Product("sold", "Sold", "https://x.in/products/sold", 1, "9", False)])
    assert "cart" not in [s.id for s in build_suites(no_stock, Settings())]


# ---------- healing heuristic ----------
@pytest.mark.parametrize("cand,expect_ok", [
    ({"tag": "button", "text": "Add to Bag", "attrs": "class=btn"}, True),
    ({"tag": "button", "text": "ADD TO CART", "attrs": "name=add"}, True),
    ({"tag": "button", "text": "", "attrs": "class=product-form__submit form_action=/cart/add"}, False),
    ({"tag": "button", "text": "Notify me when available", "attrs": "class=add-to-cart"}, False),
    ({"tag": "a", "text": "Add to wishlist", "attrs": ""}, False),
    ({"tag": "button", "text": "Add to cart", "attrs": "", "disabled": True}, False),
])
def test_add_to_cart_score(cand, expect_ok):
    assert (score("add_to_cart", cand) >= 0.6) == expect_ok


def test_checkout_score():
    assert score("checkout_button", {"tag": "button", "text": "Check out", "attrs": "name=checkout"}) >= 0.6
    assert score("checkout_button", {"tag": "a", "text": "Continue shopping", "attrs": "href=/collections"}) == 0


# ---------- LLM client ----------
def test_extract_json_handles_fences():
    assert extract_json('Sure:\n```json\n{"index": 3, "confidence": 0.9}\n```') == {"index": 3, "confidence": 0.9}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_anthropic_request_shape_and_usage():
    seen = {}

    def fake(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return {"content": [{"type": "text", "text": '{"index": 1, "confidence": 0.8}'}],
                "usage": {"input_tokens": 100, "output_tokens": 12}}
    c = LLMClient("anthropic", "claude-haiku-4-5-20251001", 5, api_key="k", transport=fake)
    assert c.complete_json("sys", "user") == {"index": 1, "confidence": 0.8}
    assert seen["url"].endswith("/v1/messages") and seen["headers"]["x-api-key"] == "k"
    assert seen["body"]["model"] == "claude-haiku-4-5-20251001" and seen["body"]["system"] == "sys"
    assert c.usage()["input_tokens"] == 100 and c.calls == 1


def test_openai_compat_and_budget_and_errors():
    def fake(url, headers, body):
        assert url == "https://api.example.com/v1/chat/completions"
        return {"choices": [{"message": {"content": '{"index": null}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
    c = LLMClient("openai_compat", "m", 1, base_url="https://api.example.com/v1", api_key="k", transport=fake)
    assert c.complete_json("s", "u") == {"index": None}
    assert c.complete_json("s", "u") is None          # over the per-run budget
    boom = LLMClient("anthropic", "m", 3, api_key="k", transport=lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert boom.complete_json("s", "u") is None and "OSError" in boom.errors[0]


def test_make_client_auto(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "RADAR_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert make_client(Settings()).provider == "none"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    c = make_client(Settings())
    assert c.provider == "anthropic" and c.model == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("OPENAI_API_KEY", "y")                     # OpenAI first while BugRadar is pre-revenue
    c = make_client(Settings())
    assert (c.provider, c.model, c.base_url, c.api_key) == ("openai", "gpt-5-mini", "https://api.openai.com/v1", "y")
    from dataclasses import replace as _r
    assert make_client(_r(Settings(), llm_model="gpt-4o-mini")).model == "gpt-4o-mini"   # switch = one setting


def test_openai_request_with_screenshot_json_mode_and_cost():
    seen = {}
    def fake(url, headers, body):
        seen.update(url=url, headers=headers, body=body)
        return {"choices": [{"message": {"content": '{"verdict": "real_store_problem"}'}}],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 100}}
    c = LLMClient("openai", "gpt-4o-mini", 5, base_url="https://api.openai.com/v1", api_key="k", transport=fake)
    assert c.complete_json("sys", "look", images=[b"\xff\xd8jpeg"]) == {"verdict": "real_store_problem"}
    assert seen["url"] == "https://api.openai.com/v1/chat/completions" and seen["headers"]["authorization"] == "Bearer k"
    b = seen["body"]
    assert b["response_format"] == {"type": "json_object"} and b["temperature"] == 0
    parts = b["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,") and parts[1]["image_url"]["detail"] == "low"
    assert c.usage()["est_usd"] == round(4000 / 1e6 * 0.15 + 100 / 1e6 * 0.60, 6)
    r = LLMClient("openai", "gpt-5-mini", 5, base_url="https://api.openai.com/v1", api_key="k", transport=fake)
    r.complete_json("s", "u")
    assert "temperature" not in seen["body"] and seen["body"]["reasoning_effort"] == "minimal"


def test_dotenv_loads_keys_without_overriding(tmp_path, monkeypatch):
    from radar.core.config import load_dotenv
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("RADAR_LLM_MODEL", "already-set")
    f = tmp_path / ".env"
    f.write_text('# comment\nOPENAI_API_KEY="sk-test"\nRADAR_LLM_MODEL=gpt-5-mini\nEMPTY=\n')
    load_dotenv(f)
    import os
    assert os.environ["OPENAI_API_KEY"] == "sk-test" and os.environ["RADAR_LLM_MODEL"] == "already-set"


# ---------- storage ----------
def test_storage_incidents_cache_and_isolation(tmp_path):
    st = Storage(tmp_path)
    assert st.site_dir("xyz.in") != st.site_dir("abc.com")
    st.cache_locator("xyz.in", "add_to_cart", "button.a", "heuristic")
    assert st.cached_locator("xyz.in", "add_to_cart") == "button.a"
    assert st.cached_locator("abc.com", "add_to_cart") is None          # per-site isolation
    run = {"run_id": "r1", "site_id": "xyz.in", "started_at": "2026-10-03T00:00:00+00:00",
           "finished_at": "2026-10-03T00:01:00+00:00", "device": "desktop", "verdict": "down",
           "counts": {"pass": 0, "flaky": 0, "confirmed_fail": 1, "blocked": 0, "skipped": 0},
           "cases": [{"case_id": "cart.add", "suite": "cart", "title": "t", "severity": "critical",
                      "verdict": "confirmed_fail", "incident_signature": "xyz.in|cart.add|click",
                      "attempts": [{"failed_step": "click", "error": "boom"}]}]}
    st.save_run(run, tmp_path / "r1")
    st.save_run(dict(run, run_id="r2"), tmp_path / "r2")
    inc = st.incidents("xyz.in")
    assert len(inc) == 1 and inc[0]["occurrences"] == 2 and inc[0]["status"] == "open"   # deduped
    st.resolve_missing_incidents("xyz.in", set())
    assert st.incidents("xyz.in")[0]["status"] == "resolved"
    assert st.incidents("abc.com") == []


# ---------- add-to-cart verification ----------
from radar.discovery.shopify_data import assess_add, parse_sent_variant_ids, prices_in_text, rupees

VASE = {"id": 2, "title": "Vase", "variants": [{"id": 201, "price": 129900}, {"id": 202, "price": 139900}]}


def _cart(*lines):
    return {"item_count": sum(l[2] for l in lines),
            "items": [{"variant_id": v, "product_id": pid, "quantity": q, "price": pr, "product_title": t}
                      for v, pid, q, pr, t in [(l[0], l[1], l[2], l[3], l[4]) for l in lines]]}


def test_assess_add_right_product():
    r = assess_add(_cart(), _cart((201, 2, 1, 129900, "Vase")), VASE, 201, [201])
    assert r["target"]["variant_id"] == 201 and not r["paid_extras"] and r["sent_ok"]


def test_assess_add_wrong_product_is_not_target():
    r = assess_add(_cart(), _cart((301, 3, 1, 49900, "Spoons")), VASE, 201, [301])
    assert r["target"] is None and r["paid_extras"][0]["title"] == "Spoons"
    assert r["sent_ok"] is False and r["sent_foreign"] == [301]


def test_assess_add_free_gift_vs_paid_extra_and_existing_items():
    before = _cart((301, 3, 1, 49900, "Spoons"))
    after = _cart((301, 3, 1, 49900, "Spoons"), (201, 2, 1, 129900, "Vase"), (999, 9, 1, 0, "Free sample"))
    r = assess_add(before, after, VASE, 201, [])
    assert r["target"]["title"] == "Vase" and r["free_extras"][0]["title"] == "Free sample"
    assert not r["paid_extras"] and r["sent_ok"] is None     # existing Spoons line is not "added"


def test_assess_add_quantity_increment():
    r = assess_add(_cart((201, 2, 1, 129900, "Vase")), _cart((201, 2, 3, 129900, "Vase")), VASE, 201, [])
    assert r["target"]["qty_delta"] == 2


def test_parse_sent_variant_ids_all_formats():
    assert parse_sent_variant_ids('{"id": 201, "quantity": 1}') == [201]
    assert parse_sent_variant_ids('{"items": [{"id": 201}, {"id": 999}]}') == [201, 999]
    assert parse_sent_variant_ids("form_type=product&id=201&quantity=1") == [201]
    multipart = '------X\r\nContent-Disposition: form-data; name="id"\r\n\r\n51469498810690\r\n------X--'
    assert parse_sent_variant_ids(multipart) == [51469498810690]
    assert parse_sent_variant_ids(None) == [] and parse_sent_variant_ids("") == []


def test_prices_in_text_and_rupees():
    assert 1059.0 in prices_in_text("MRP ₹1,059.00 incl. taxes") and 295.0 in prices_in_text("Rs. 295")
    assert rupees(105900) == "₹1,059.00"


# ---------- detection + bench parsing ----------
from radar.discovery.detect import detect_checkout_app, detect_access
from radar.bench import parse_store_list, summarise


def test_checkout_app_detection():
    assert detect_checkout_app('<script src="https://pdp.gokwik.co/x.js">') == "GoKwik"
    assert detect_checkout_app("shopflo-checkout gokwik") == "GoKwik + Shopflo"
    assert detect_checkout_app("<html>plain</html>") == "Shopify checkout"


def test_access_detection():
    assert detect_access(200, "Opening soon", '<form action="/password" method="post">', "/password") == "password"
    assert detect_access(403, "Just a moment...", "<div id=cf-chl-widget>", "/") == "bot_blocked"
    assert detect_access(200, "Shop", "<html>normal</html>", "/") == "open"


def test_store_list_parsing():
    e = parse_store_list("# c\nhttps://a.com\n\nhttps://b.com  cart  # note\nc.in CART\n")
    assert e == [("https://a.com", False), ("https://b.com", True), ("c.in", True)]


def test_bench_summary_row():
    run = {"site_id": "x.in", "base_url": "https://x.in", "verdict": "down", "started_at": "2026-10-04T00:00:00+00:00",
           "finished_at": "2026-10-04T00:01:30+00:00", "notes": [], "healing_events": [],
           "sitemap_summary": {"platform": "shopify", "theme": "Dawn", "checkout_app": "GoKwik", "access": "open"},
           "cases": [{"case_id": "cart.add_to_cart", "suite": "cart", "verdict": "confirmed_fail",
                      "attempts": [{"failed_step": "cart_received_this_product", "error": "boom",
                                    "steps": [{"status": "warn"}]}]},
                     {"case_id": "product.pdp.a", "suite": "product", "verdict": "pass", "attempts": [{"steps": []}]},
                     {"case_id": "product.pdp.b", "suite": "product", "verdict": "flaky", "attempts": [{"steps": []}]}]}
    r = summarise(run, "/r.html")
    assert r["suites"] == {"cart": "confirmed_fail", "product": "flaky"} and r["theme"] == "Dawn"
    assert r["secs"] == 90 and r["warnings"] == 1 and len(r["failures"]) == 2
    run["cases"][2]["attempts"] = [{"ok": False, "failed_step": "click_into_product", "error": "opened the wrong product", "steps": []},
                                   {"ok": True, "steps": []}]
    fl = summarise(run, "/r.html")["failures"][1]          # boat-lifestyle.com, bench 5: flaky row was empty
    assert fl["step"] == "click_into_product" and fl["error"] == "failed once, passed on retry: opened the wrong product"


# ---------- bench-RCA rules (4 Oct) ----------
from radar.generate.builder import _search_term
from radar.checks.library import title_match
from radar.discovery.discover import same_site, NOT_FOR_TESTS


def test_search_term_never_the_brand():
    # rarerabbit.in: old rule searched "rare" (the brand) and found no relevant results
    brand = {"rarerabbit", "rare", "rabbit"}
    assert _search_term("kore-s-mens-sweatshirt-maroon", "Rare Rabbit Men's Kore S Sweatshirt", brand) == "kore"
    assert _search_term("travel-pack-combo", "Travel Pack Combo Sunscreen", set()) == "sunscreen"
    assert _search_term("", "", set()) == ""


def test_brand_words_include_domain_parts_title_after_dash_and_vendors():
    """thehouseofrare.com (bench 7): 'rare' was searched; the brand sits inside the domain label and after
    the dash in the title, and Shopify's vendor field names the brands (Rare Rabbit)."""
    from radar.generate.builder import _brand_words
    from radar.core.models import SiteMap, Product
    sm = SiteMap(site_id="thehouseofrare.com", base_url="https://thehouseofrare.com",
                 home_title="Premium Clothing Brand in India - The House of Rare",
                 products=[Product("rare-rabbit-mens-kore-sweatshirt", "Rare Rabbit Men's Kore Sweatshirt", "u",
                                   vendor="Rare Rabbit")])
    b = _brand_words(sm)
    assert {"rare", "rabbit", "house", "thehouseofrare"} <= b
    assert _search_term("rare-rabbit-mens-kore-sweatshirt", "Rare Rabbit Men's Kore Sweatshirt", b) == "kore"


def test_title_match_tolerant_but_not_blind():
    assert title_match("Skincare Duo (face wash 50 ml + sunscreen 30 g)", "Skincare Duo (Face Wash 50ml + Sunscreen 30g)", "")[0]
    assert title_match("Ceramic Flower Vase", "Vase", "")[0]                     # shorter display name
    ok, how = title_match("Ceramic Flower Vase", "Handmade Pot", "Handmade Pot ₹1,299")
    assert not ok and "0/3" in how


def test_same_site_and_offsite():
    assert same_site("https://moxiebeauty.in", "https://www.moxiebeauty.in/")
    assert same_site("https://a.myshopify.com", "https://a.myshopify.com/x")
    assert not same_site("https://antinorm.com", "https://zombo.com/")          # wrong domain in the list
    assert not same_site("https://peepbeauty.com", "https://www.hugedomains.com/domain_profile.cfm")


def test_gift_clone_and_gift_card_products_excluded():
    for h in ("skincare-duo-face-wash-50-ml-sunscreen-30-g-sca_clone_freegift", "e-gift-card", "free-gift-pouch",
              "shampoo-sampler"):
        assert NOT_FOR_TESTS.search(h), h
    for h in ("ceramic-vase", "kore-s-mens-sweatshirt-maroon", "testosterone-booster"):
        assert not NOT_FOR_TESTS.search(h), h


def test_token_priced_freebies_are_found():
    from radar.discovery.discover import token_priced
    from radar.core.models import Product
    ps = [Product("duo", "Skincare Duo", "u", price="1.00"), Product("a", "A", "u", price="499.00"),
          Product("b", "B", "u", price="649.00"), Product("c", "C", "u", price="899.00"), Product("z", "Z", "u", price="0")]
    assert [p.handle for p in token_priced(ps)] == ["duo"]
    # a cheap store is not a freebie store: Rs 40 items next to Rs 60 items are real products
    cheap = [Product(h, h, "u", price=x) for h, x in (("x", "40"), ("y", "60"), ("w", "55"))]
    assert token_priced(cheap) == []
    assert token_priced(ps[:2]) == []          # too few prices to judge


def test_llm_overlay_pick_is_vetoed_unless_it_only_closes():
    from radar.checks.library import safe_to_close
    for ok in ("×", "Close", "No thanks", "Maybe later", "I'll pass", "Decline", "", "Not interested"):
        assert safe_to_close(ok, "popup__close" if ok == "" else ""), ok
    for bad in ("Try my luck", "Subscribe", "Yes, I am 18+", "Accept all", "Get 10% off", "Shop now", "Continue", "Spin", ""):
        assert not safe_to_close(bad), bad


def test_llm_check_scores_answers():
    import json as _j
    from radar import llmcheck
    def perfect(url, headers, body):
        txt = body["messages"][1]["content"]
        case = next(c for c in llmcheck.CASES if c["error"] in txt)
        return {"choices": [{"message": {"content": _j.dumps({"verdict": sorted(case["want"])[0], "category": "other",
                                                                  "reason": "r", "evidence": "e"})}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 40}}
    llm = LLMClient("openai", "gpt-4o-mini", 100, base_url="https://api.openai.com/v1", api_key="k", transport=perfect)
    r = llmcheck.run(llm, progress=lambda *_: None)
    assert r["correct"] == r["total"] == 22 and r["calls"] == 22 and r["est_usd"] > 0 and r["held_out"] == "3/3"
    always_store = LLMClient("openai", "gpt-4o-mini", 100, base_url="https://api.openai.com/v1", api_key="k",
                             transport=lambda u, h, b: {"choices": [{"message": {"content": '{"verdict": "real_store_problem"}'}}]})
    assert llmcheck.run(always_store, progress=lambda *_: None)["correct"] == 12    # blaming the store always is caught


def test_triage_answer_validation():
    from radar.healing.triage import clean
    assert clean({"verdict": "radar_problem", "category": "weird"})["category"] == "other"
    assert clean({"verdict": "maybe"}) is None and clean(None) is None and clean("x") is None


def test_triage_facts_only_state_what_code_can_prove():
    from radar.healing.triage import facts
    miss = [{"what": "product name shown on the page", "expected": "Ceramic Flower Vase", "actual": "none", "ok": False}]
    assert any("DOES appear" in f for f in facts(miss, "", "Ceramic Flower Vase\nRs 1,299"))
    assert not any("DOES appear" in f for f in facts(miss, "", "We have moved"))
    wrong = [{"what": "product added to cart", "expected": "Ceramic Flower Vase", "actual": "Wooden Spoon Set", "ok": False}]
    assert not any("DOES appear" in f for f in facts(wrong, "", "Ceramic Flower Vase ... Wooden Spoon Set"))   # wrong != missing
    banner = "Flat 20% OFF sitewide\nThe product is currently unavailable"
    fs = facts(miss, "", banner)
    assert not any("popup" in f for f in fs) and any("unavailable" in f for f in fs)     # offer bar is not a popup
    cov = facts([], 'could not click: <div class="klaviyo-form x"></div> subtree intercepts pointer events', "Get 10% off! Subscribe")
    assert any("covered" in f and "klaviyo-form" in f for f in cov) and any("popup" in f for f in cov)


def test_triage_facts_for_store_side_findings():
    from radar.healing.triage import facts
    nobuy = [{"what": "add-to-cart control for THIS product on its page", "expected": "present",
              "actual": "none: no add-to-cart form for this product (3 such forms belong to other products' cards); no other buy control found", "ok": False}]
    assert any("NO add-to-cart form for this product" in f and "OTHER products' cards" in f for f in facts(nobuy, "", "Add to cart"))
    soft = [{"what": "HTTP status for a page that does not exist", "expected": 404, "actual": "200", "ok": False}]
    assert any("soft 404" in f for f in facts(soft, "", "Welcome"))
    hid = [{"what": "selected variant price shown", "expected": "₹199.00", "ok": False,
            "actual": "not on page (₹199.00 is only inside a hidden element <product-sticky-form.hidden-lap-and-up>, not shown on this screen size)"}]
    fs = facts(hid, "", "Supply6 360\nADD TO CART")
    assert any("hides at this screen size" in f and "real_store_problem" in f for f in fs)     # supplysix.com, bench 4
    assert not any("DOES appear" in f for f in fs)


def test_triage_names_unrecognised_buy_words_next_to_the_price():
    from radar.healing.triage import facts, _action_lines
    nobuy = [{"what": "add-to-cart control for THIS product on its page", "expected": "present",
              "actual": "none: no add-to-cart form for this product; no other buy control found", "ok": False}]
    assert _action_lines("Ceramic Flower Vase\nRs 1,299.00\nGrab it\nShare\nHandmade in Khurja.") == ["Grab it", "Share"]
    assert any("'Grab it'" in f for f in facts(nobuy, "", "Ceramic Flower Vase\nRs 1,299.00\nGrab it\nShare"))
    assert _action_lines("No price here\nGrab it") == []                       # no price line: nothing claimed
    assert _action_lines("₹499\nAdd to cart\n(Inclusive of all taxes)\n2 left") == []   # known words, notes, numbers


def test_user_agent_always_carries_radars_name():
    """6 Oct: 'browser' style = the normal Chrome name + BugRadar, never a disguise; robots.txt still
    matched on the BugRadar token."""
    from radar.core.browser import browser_user_agent
    from radar.core.robots import Robots
    me = "BugRadar/0.1 (+bugradar.in)"
    mac = browser_user_agent(me, "141.0.7390.37", "browser", system="Darwin")
    assert mac == ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/141.0.0.0 Safari/537.36 BugRadar/0.1 (+bugradar.in)")
    assert "HeadlessChrome" not in mac and mac.endswith(me)
    assert browser_user_agent(me, "141.0.1", "plain") == me
    pixel = "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.0 Mobile Safari/537.36"
    m = browser_user_agent(me, "141.0.1", "browser", device_ua=pixel)
    assert "Pixel 7" in m and "Chrome/141.0.0.0 Mobile" in m and m.endswith(me)
    r = Robots("User-agent: BugRadar\nDisallow: /\n\nUser-agent: *\nAllow: /\n", me)
    assert not r.allowed("https://x.in/")            # the store's rule for BugRadar still applies


def test_emulated_platform_matches_the_user_agent_sent():
    """9 Oct: speed snippets hold the theme for 'Linux x86_64' (PageSpeed). Radar's navigator.platform must match the
    device its UA names: desktop = Windows Chrome on every host, Pixel 7 = Android, never the runner's Linux."""
    from radar.core.browser import PLATFORM, browser_user_agent, emulated_system, DESKTOP_SYSTEM
    me = "BugRadar/0.1 (+bugradar.in)"
    pixel = "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.0 Mobile Safari/537.36"
    assert PLATFORM[emulated_system(None)] == "Win32"
    assert PLATFORM[emulated_system(pixel)] == "Linux armv81"
    assert PLATFORM[emulated_system("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")] == "iPhone"
    desk = browser_user_agent(me, "141.0.1", "browser", system=DESKTOP_SYSTEM)
    assert "Windows NT 10.0" in desk and desk.endswith(me)


def test_search_term_skips_brand_plurals_and_promo_words():
    """plumgoodness.com, bench 9: 'plums' (brand 'Plum' + s) matched all 246 products by brand; 'mystery' came from
    a promo item the store's search app leaves out. Neither is a word a shopper's search can be judged by."""
    brand = {"plum", "goodness", "plumgoodness", "bodylovin"}
    assert _search_term("set-of-5-plums-2", "Set of 5 Plums", brand) == ""
    assert _search_term("mystery-merch", "Mystery Merch", brand) == ""
    assert _search_term("mystery-box-2-full-size", "Mystery Box (2 full-size)", brand) == ""
    assert _search_term("green-tea-face-wash", "Green Tea Face Wash", brand) == "green"
    assert _search_term("plumeria-body-mist", "Plumeria Body Mist", brand) == "plumeria"   # a real word, not the brand
    from radar.generate.builder import _stems
    assert "berry" in _stems("berries") and "plum" in _stems("plums") and _stems("aloe") == {"aloe"}


def test_search_suite_carries_up_to_three_words():
    from radar.core.models import Product
    sm = _sm()
    sm.products = [Product(h, t, f"https://x.in/products/{h}", i + 1, "499.00", True)
                   for i, (h, t) in enumerate([("aloe-gel", "Aloe Gel"), ("neem-soap", "Neem Soap"),
                                               ("rice-toner", "Rice Toner"), ("kale-chips", "Kale Chips")])]
    suites = build_suites(sm, Settings())
    case = next(c for s in suites if s.id == "search" for c in s.cases)
    assert len(case.params["alt_urls"]) == 2, case.params


# ---------- desktop + mobile in every run (7 Oct) ----------
def _attempt_with(loads=(), console=(), failed=()):
    from radar.core.models import AttemptResult
    return AttemptResult(1, True, console=list(console), failed_requests=list(failed), loads=list(loads))


def _case_with(attempt, verdict="pass"):
    cr = CaseResult("c", "journey", "t", "x", "major", verdict)
    cr.attempts.append(attempt)
    return cr


def test_incident_signature_is_per_device_and_desktop_keeps_the_old_key():
    from radar.runner.confirm import signature, signature_device
    assert signature("xyz.in", "cart.add", "click") == "xyz.in|cart.add|click"                    # unchanged for desktop
    assert signature("xyz.in", "cart.add", "click", "mobile") == "xyz.in|cart.add|click|mobile"
    assert signature_device("xyz.in|cart.add|click") == "desktop"
    assert signature_device(signature("xyz.in", "cart.add", None, "mobile")) == "mobile"


def test_a_passing_mobile_run_never_closes_a_desktop_incident_and_the_reverse(tmp_path):
    st = Storage(tmp_path)

    def run(rid, device, sig):
        return {"run_id": rid, "site_id": "xyz.in", "started_at": "2026-10-07T00:00:00+00:00",
                "finished_at": "2026-10-07T00:01:00+00:00", "device": device, "verdict": "down" if sig else "healthy",
                "counts": {"pass": 0, "flaky": 0, "confirmed_fail": 1 if sig else 0, "blocked": 0, "skipped": 0},
                "cases": [{"case_id": "cart.add", "suite": "cart", "title": "t", "severity": "critical",
                           "verdict": "confirmed_fail", "incident_signature": sig,
                           "attempts": [{"failed_step": "click", "error": "boom"}]}] if sig else []}

    st.save_run(run("d1", "desktop", "xyz.in|cart.add|click"), tmp_path / "d1")
    st.save_run(run("m1", "mobile", "xyz.in|cart.add|click|mobile"), tmp_path / "m1")
    inc = {i["signature"]: i for i in st.incidents("xyz.in")}
    assert inc["xyz.in|cart.add|click"]["device"] == "desktop" and inc["xyz.in|cart.add|click|mobile"]["device"] == "mobile"
    st.resolve_missing_incidents("xyz.in", set(), "mobile")          # mobile run passed: only the mobile incident closes
    inc = {i["signature"]: i["status"] for i in st.incidents("xyz.in")}
    assert inc == {"xyz.in|cart.add|click": "open", "xyz.in|cart.add|click|mobile": "resolved"}
    st.resolve_missing_incidents("xyz.in", set(), "desktop")
    assert {i["status"] for i in st.incidents("xyz.in")} == {"resolved"}


def test_remembered_locators_are_kept_per_device(tmp_path):
    from radar.healing.locator import Healer
    llm = make_client(replace(Settings(), llm_provider="none"))
    st = Storage(tmp_path)
    d, m = Healer("xyz.in", "r", st, llm, None, "desktop"), Healer("xyz.in", "r", st, llm, None, "mobile")
    assert d.cache_id == "xyz.in" and m.cache_id == "xyz.in@mobile" and m.site_id == "xyz.in"
    st.cache_locator(d.cache_id, "add_to_cart", "button.desk", "hint")
    st.cache_locator(m.cache_id, "add_to_cart", "button.phone", "hint")
    assert st.cached_locator(d.cache_id, "add_to_cart") == "button.desk"
    assert st.cached_locator(m.cache_id, "add_to_cart") == "button.phone"


def test_perf_summary_numbers_and_empty():
    from radar.runner.executor import perf_summary
    assert perf_summary([]) == {} and perf_summary([_case_with(_attempt_with())]) == {}
    a = _attempt_with(
        loads=[{"url": "/", "ttfb": .2, "dcl": .8, "load": 1.0, "lcp": 1.1},
               {"url": "/products/x", "ttfb": .3, "dcl": 1.5, "load": 4.0, "lcp": None}],
        console=[{"type": "error", "text": "boom", "url": "u"}, {"type": "warning", "text": "w", "url": "u"}],
        failed=["GET https://cdn.x/a.js"])
    b = _attempt_with(loads=[{"url": "/", "ttfb": .2, "dcl": 1.2, "load": 2.0, "lcp": 1.5}],        # same page, slower
                      console=[{"type": "error", "text": "boom", "url": "u"}, {"type": "pageerror", "text": "uncaught", "url": "u"}],
                      failed=["GET https://cdn.x/a.js", "GET https://cdn.x/b.js"])
    p = perf_summary([_case_with(a), _case_with(b)])
    assert p["pages"] == 2 and p["slowest"] == {"url": "/products/x", "load_secs": 4.0}
    assert p["median_load_secs"] == 3.0                 # pages: / = 2.0 (slowest seen), /products/x = 4.0
    assert (p["console_errors"], p["console_warnings"], p["failed_requests"]) == (2, 1, 2)   # de-duplicated across cases


def _row(verdict, suites, failures=(), device="desktop", warnings=0, notes=()):
    return {"site_id": "xyz.in", "url": "https://xyz.in", "verdict": verdict, "platform": "shopify", "theme": "Dawn",
            "checkout": "Shopify checkout", "access": "open", "suites": suites, "failures": list(failures),
            "warnings": warnings, "healed": 0, "radar_suspect": 0, "notes": list(notes), "report": f"/r/{device}.html",
            "secs": 10, "device": device, "perf": {"pages": 3}}


def test_bench_row_combines_devices_worst_wins_and_says_where_it_fails_only_on_one():
    from radar.bench import combine
    fail = {"case": "product.pdp.vase", "verdict": "confirmed_fail", "step": "price", "error": "no price"}
    row = combine({"desktop": _row("down", {"product": "confirmed_fail", "journey": "pass"}, [fail]),
                   "mobile": _row("healthy", {"product": "pass", "journey": "flaky"}, device="mobile", warnings=2)})
    assert row["verdict"] == "down" and row["suites"] == {"product": "confirmed_fail", "journey": "flaky"}
    assert row["suites_by_device"]["mobile"]["product"] == "pass"
    assert row["failures"] == [dict(fail, device="desktop")]
    assert row["device_only"] == {"desktop": ["product.pdp.vase"], "mobile": []}
    assert row["devices"]["mobile"]["verdict"] == "healthy" and row["devices"]["mobile"]["report"] == "/r/mobile.html"
    assert row["warnings"] == 2 and row["secs"] == 20 and "device" not in row and "perf" not in row
    both_fail = combine({"desktop": _row("down", {}, [fail]), "mobile": _row("down", {}, [fail], "mobile")})
    assert both_fail["device_only"] == {"desktop": [], "mobile": []}                   # same failure on both: not 'only'


def test_bench_row_verdict_ranking_and_single_device():
    from radar.bench import combine
    assert combine({"desktop": _row("healthy", {}), "mobile": _row("degraded", {}, device="mobile")})["verdict"] == "degraded"
    assert combine({"desktop": _row("down", {}), "mobile": _row("no_network", {}, device="mobile")})["verdict"] == "no_network"
    one = combine({"desktop": _row("blocked", {}, notes=["robots"])})
    assert one["verdict"] == "blocked" and list(one["devices"]) == ["desktop"] and one["device_only"] == {}


def test_page_url_for_the_timing_table_has_no_host():
    from radar.core.browser import _short_url
    assert _short_url("https://xyz.in/products/x?variant=1#top") == "/products/x?variant=1"
    assert _short_url("https://xyz.in") == "/"


# ---------- cloud runs: what goes to the cloud-results branch (7 Oct) ----------
def test_cloud_publisher_copies_the_small_results_and_marks_latest(tmp_path):
    from tools.publish_cloud_results import publish
    data, out = tmp_path / "data", tmp_path / "out"
    run_d = data / "sites" / "xyz.in" / "runs" / "R1-d"
    run_m = data / "sites" / "xyz.in" / "runs" / "R1-m"
    for d in (run_d, run_m):
        d.mkdir(parents=True)
        (d / "run.json").write_text("{}")
        (d / "report.html").write_text("x")
        (d / "journey.a1.01-home.jpg").write_bytes(b"j")
        (d / "journey.a1_trace.zip").write_bytes(b"z")
    (run_d / "product.a1.png").write_bytes(b"p")
    bench = data / "bench" / "20261007T150000Z"
    bench.mkdir(parents=True)
    (bench / "bench.json").write_text(json.dumps({"totals": {"stores": 1, "healthy": 1}, "rows": [
        {"site_id": "xyz.in", "devices": {"desktop": {"report": str(run_d / "report.html")},
                                          "mobile": {"report": str(run_m / "report.html")}}}]}))
    (bench / "bench.html").write_text("<html>")
    log = tmp_path / "bench.log"
    log.write_text("RADAR BENCH ...")
    dest = publish(out, data, log, {"event": "push"})
    assert dest == out / "runs" / "20261007T150000Z" and (out / "LATEST").read_text().strip() == "20261007T150000Z"
    assert {p.name for p in dest.iterdir()} == {"bench.json", "bench.html", "bench.log", "meta.json", "sites"}
    assert {p.name for p in (dest / "sites" / "xyz.in" / "R1-d").iterdir()} == {"run.json", "product.a1.png"}
    assert {p.name for p in (dest / "sites" / "xyz.in" / "R1-m").iterdir()} == {"run.json"}     # no jpg, zip, html
    meta = json.loads((dest / "meta.json").read_text())
    assert meta["runs_copied"] == 2 and meta["totals"]["healthy"] == 1


def test_cloud_publisher_still_publishes_the_log_when_radar_made_no_bench(tmp_path):
    from tools.publish_cloud_results import publish
    log = tmp_path / "bench.log"
    log.write_text("Traceback: boom")
    dest = publish(tmp_path / "out", tmp_path / "data", log, {"event": "push"})
    assert dest.name.endswith("-no-bench") and (dest / "bench.log").read_text() == "Traceback: boom"
    assert json.loads((dest / "meta.json").read_text())["runs_copied"] == 0


def test_cloud_publisher_publishes_the_finished_runs_when_the_bench_was_cut_short(tmp_path):
    from tools.publish_cloud_results import publish
    run = tmp_path / "data" / "sites" / "xyz.in" / "runs" / "R1-d"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    (run / "cart.a1.png").write_bytes(b"p")
    dest = publish(tmp_path / "out", tmp_path / "data", None, {})
    assert dest.name.endswith("-no-bench") and (dest / "sites" / "xyz.in" / "R1-d" / "cart.a1.png").exists()
    meta = json.loads((dest / "meta.json").read_text())
    assert meta["partial"] is True and meta["runs_copied"] == 1


def test_cloud_publisher_on_our_own_server_publishes_only_this_runs_results(tmp_path):
    """On the self-hosted server data/ keeps every earlier run: only what THIS run wrote is published."""
    import os
    from tools.publish_cloud_results import publish
    data = tmp_path / "data"
    old_bench = data / "bench" / "20261001T000000Z"
    old_bench.mkdir(parents=True)
    (old_bench / "bench.json").write_text(json.dumps({"rows": []}))
    old_run = data / "sites" / "old.in" / "runs" / "R0"
    old_run.mkdir(parents=True)
    (old_run / "run.json").write_text("{}")
    for p in (old_bench, old_bench / "bench.json", old_run / "run.json"):
        os.utime(p, (1000, 1000))
    new_run = data / "sites" / "new.in" / "runs" / "R1"
    new_run.mkdir(parents=True)
    (new_run / "run.json").write_text("{}")
    dest = publish(tmp_path / "out", data, None, {}, since=2000)
    assert dest.name.endswith("-no-bench")                                     # the old bench is not this run's
    assert [p.name for p in (dest / "sites").iterdir()] == ["new.in"]          # the old run is not republished


def test_price_shown_accepts_whole_rupee_rounding_only():
    from radar.checks.library import price_shown
    assert price_shown(3391.50, [3990.0, 3392.0])[0]          # thelabellife.com
    assert price_shown(727.18, [727.0])[0]                     # salty.co.in
    assert price_shown(1299.0, [1299.0])[0]
    assert not price_shown(1299.0, [1300.0])[0]               # a whole-rupee price must match exactly
    assert not price_shown(727.18, [728.5])[0]
    assert not price_shown(3391.50, [3390.0])[0]


# ---------- Web Bot Auth (signed requests, 9 Oct) ----------
def test_webbotauth_thumbprint_matches_rfc8037_vector():
    from radar.core.webbotauth import jwk_thumbprint
    assert jwk_thumbprint("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo") == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


def test_webbotauth_signature_verifies_and_is_per_host():
    from radar.core.webbotauth import Signer, verify, authority, b64u
    import secrets as _s
    t = [1_800_000_000]
    s = Signer(b64u(_s.token_bytes(32)), clock=lambda: t[0])
    h = s.headers("https://www.bummer.in/products/x.js")
    assert h["Signature-Agent"] == '"https://bugradar.in"'
    assert 'tag="web-bot-auth"' in h["Signature-Input"] and 'alg="ed25519"' in h["Signature-Input"]
    assert f'keyid="{s.keyid}"' in h["Signature-Input"] and "created=1800000000;expires=1800003600" in h["Signature-Input"]
    assert verify(h, "www.bummer.in", s.x) and not verify(h, "bummer.in", s.x)      # bound to the host
    assert s.headers("https://www.bummer.in/") == h                                  # cached within its lifetime
    t[0] += 3560
    assert s.headers("https://www.bummer.in/") != h                                  # renewed before it expires
    assert authority("http://127.0.0.1:8765/x") == "127.0.0.1:8765" and authority("https://A.com:443/") == "a.com"
    d = s.directory()["keys"][0]
    assert d["kid"] == s.keyid and d["kty"] == "OKP" and d["crv"] == "Ed25519"


def test_title_count_reads_shopifys_search_count():
    """ptron.in / kushals.com (new30c, 10 Oct): Shopify prints its own result count in the search page title."""
    from radar.checks.library import _title_count
    assert _title_count('Search: 513 results found for "sonor" - pTron India') == 513
    assert _title_count('Search: 1,000 results found for "zircon"') == 1000
    assert _title_count("Search: ceramic | Mock Store") is None


# ---------------- journeys 28-30 (10 Oct 2026) ----------------

def test_footer_links_are_read_as_one_info_page_per_kind_same_store_only():
    from radar.discovery.discover import info_pages
    base = "https://shop.example.in"
    links = [{"text": "Shipping & Returns", "url": base + "/pages/shipping-returns"},     # returns wins: one page, both
             {"text": "Delivery Information", "url": base + "/pages/delivery"},
             {"text": "Return policy", "url": base + "/policies/refund-policy"},           # refund already taken
             {"text": "Privacy", "url": "https://www.shop.example.in/pages/privacy-policy#top"},
             {"text": "T&C", "url": base + "/pages/terms-conditions"},                    # matched by the path
             {"text": "Contact Us", "url": base + "/pages/contact"},
             {"text": "Contact on WhatsApp", "url": "https://wa.me/919999999999"},          # another site
             {"text": "Track order", "url": base + "/apps/track-order"},                    # app proxy, not a page
             {"text": "Returns portal", "url": "https://returns.otherapp.com/shop"},
             {"text": "Shipping bags", "url": base + "/collections/shipping-bags"}]         # a collection, not info
    got = info_pages(links, base)
    assert [(p["kind"], p["url"]) for p in got] == [
        ("refund", base + "/pages/shipping-returns"), ("shipping", base + "/pages/delivery"),
        ("privacy", "https://www.shop.example.in/pages/privacy-policy"), ("terms", base + "/pages/terms-conditions"),
        ("contact", base + "/pages/contact")]
    assert info_pages([], base) == []


def test_layout_verdicts_say_what_sticks_out_and_what_covers_the_screen():
    from radar.checks.library import layout_verdicts, COVERED_MAX
    ok = layout_verdicts({"moved": 2, "vw": 412, "by": []}, {"pct": 20, "by": ["div.sticky-atc 9%"]})
    assert [v[3] for v in ok] == [True, True] and ok[0][2] == "fits (412px)" and ok[1][2] == "20%"
    bad = layout_verdicts({"moved": 60, "vw": 412, "by": ["div.marquee (900px wide, 488px past the edge)"]},
                          {"pct": COVERED_MAX + 7, "by": ["div#chat 30%", "div.bar 12%"]})
    assert [v[3] for v in bad] == [False, False]
    assert bad[0][2] == "yes, by 60px: div.marquee (900px wide, 488px past the edge)"
    assert bad[1][2] == f"{COVERED_MAX + 7}%: div#chat 30%, div.bar 12%"


def test_info_suite_is_built_only_from_pages_discovery_could_open():
    sm = SiteMap(site_id="x.in", base_url="https://x.in", platform="shopify",
                 collections=[Collection("all", "All", "https://x.in/collections/all")],
                 products=[Product("a", "Alpha Lamp", "https://x.in/products/a", 11, "499.00", True)],
                 info_pages=[{"kind": "shipping", "text": "Shipping", "url": "https://x.in/pages/shipping"}],
                 account_url="https://x.in/account/login")
    info = next(s for s in build_suites(sm, Settings()) if s.id == "info")
    assert [(c.id, c.check, c.severity) for c in info.cases] == [
        ("info.policy_pages", "info_pages", "minor"), ("info.account_page", "account_page", "minor")]
    sm.info_pages, sm.account_url = [], ""
    assert not any(s.id == "info" for s in build_suites(sm, Settings()))


def test_no_results_message_is_found_in_the_ways_stores_say_it():
    from radar.checks.library import no_results_message
    for t in ("Search: 0 results found for “qzxvbugradar”", "No results found for qzxvbugradar. Check the spelling",
              "Sorry, we couldn't find anything matching qzxvbugradar", "Your search for qzxvbugradar did not match any products",
              "We found 0 results", "Nothing found. Try a different search term"):
        assert no_results_message(t), t
    assert no_results_message("Bestsellers: Ceramic Vase ₹1,299 · Wooden Spoon Set ₹499") == ""
    assert no_results_message("Showing 20 results for vase") == ""
