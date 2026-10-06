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
    assert r["correct"] == r["total"] == 21 and r["calls"] == 21 and r["est_usd"] > 0 and r["held_out"] == "3/3"
    always_store = LLMClient("openai", "gpt-4o-mini", 100, base_url="https://api.openai.com/v1", api_key="k",
                             transport=lambda u, h, b: {"choices": [{"message": {"content": '{"verdict": "real_store_problem"}'}}]})
    assert llmcheck.run(always_store, progress=lambda *_: None)["correct"] == 11    # blaming the store always is caught


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
