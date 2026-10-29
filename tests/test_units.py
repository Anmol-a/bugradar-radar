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
    assert normalize_url("Shop063.example/products/x?y=1") == "https://shop063.example"
    assert normalize_url("http://localhost:8765/a") == "http://localhost:8765"
    assert site_id_from_url("https://www.Shop125.example/x") == "shop125.example"
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
    edit = next(s for s in suites if s.id == "cart").cases[1]       # journeys #19-#21: warnings until a bench measures them
    assert edit.check == "cart_edit" and edit.params["variant_id"] == 2 and edit.params["strict"] is False
    assert edit.severity == "major"
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
    fl = summarise(run, "/r.html")["failures"][1]          # shop017.example, bench 5: flaky row was empty
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
    assert same_site("https://shop063.example", "https://www.shop063.example/")
    assert same_site("https://a.myshopify.com", "https://a.myshopify.com/x")
    assert not same_site("https://shop087.example", "https://zombo.com/")          # wrong domain in the list
    assert not same_site("https://shop064.example", "https://www.hugedomains.com/domain_profile.cfm")


def test_gift_clone_and_gift_card_products_excluded():
    for h in ("skincare-duo-face-wash-50-ml-sunscreen-30-g-sca_clone_freegift", "e-gift-card", "free-gift-pouch",
              "shampoo-sampler", "free-returns-package-protection", "shipping-protection", "route-package-protection",
              "1 Year Extended Warranty for IZI Spectra Combo", "2-year-warranty-upgrade",
              "2 Year Accidental Protection for IZI MINI X Drone"):
        assert NOT_FOR_TESTS.search(h), h
    for h in ("ceramic-vase", "kore-s-mens-sweatshirt-maroon", "testosterone-booster", "sun-protection-spf-50",
              "free-shipping-tote", "drone-with-1-year-manufacturer-support"):
        assert not NOT_FOR_TESTS.search(h), h


def test_gift_sku_by_product_type_or_whole_tag():
    """shop145.example 'Free Fabric' (type 'Freebies'): a gift SKU by the store's own label. A tag that only marks a real
    product as eligible for an offer ('free-gift-eligible') does not count."""
    from radar.discovery.discover import GIFT_KIND
    from radar.discovery.shopify_data import products_from_json, product_from_js
    for k in ("Freebies", "Saree | freebie", "Free Gift", "Sarees | GWP | new"):
        assert GIFT_KIND.search(k), k
    for k in ("free-gift-eligible", "gwp-eligible", "Sarees | Silk", "Free Size", ""):
        assert not GIFT_KIND.search(k), k
    p = products_from_json({"products": [{"handle": "free-fabric", "title": "Free Fabric", "product_type": "Freebies",
                                          "tags": ["gift"], "variants": [{"id": 1, "available": True, "price": "1000"}]}]},
                           "https://x.in")[0]
    assert p["kind"] == "Freebies | gift"
    j = product_from_js({"handle": "a", "type": "Saree", "tags": "silk, new", "variants": [{"id": 2, "available": True,
                                                                                         "price": 100}]}, "https://x.in")
    assert j["kind"] == "Saree | silk | new"


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
    fs = facts(hid, "", "Shop111 360\nADD TO CART")
    assert any("hides at this screen size" in f and "real_store_problem" in f for f in fs)     # shop084.example, bench 4
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
    """shop040.example, bench 9: 'plums' (the brand word + s) matched all 246 products by brand; 'mystery' came from
    a promo item the store's search app leaves out. Neither is a word a shopper's search can be judged by."""
    brand = {"plum", "shop040"}
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
    assert price_shown(3391.50, [3990.0, 3392.0])[0]          # shop043.example
    assert price_shown(727.18, [727.0])[0]                     # shop109.example
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
    h = s.headers("https://www.shop129.example/products/x.js")
    assert h["Signature-Agent"] == '"https://bugradar.in"'
    assert 'tag="web-bot-auth"' in h["Signature-Input"] and 'alg="ed25519"' in h["Signature-Input"]
    assert f'keyid="{s.keyid}"' in h["Signature-Input"] and "created=1800000000;expires=1800003600" in h["Signature-Input"]
    assert verify(h, "www.shop129.example", s.x) and not verify(h, "shop129.example", s.x)      # bound to the host
    assert s.headers("https://www.shop129.example/") == h                                  # cached within its lifetime
    t[0] += 3560
    assert s.headers("https://www.shop129.example/") != h                                  # renewed before it expires
    assert authority("http://127.0.0.1:8765/x") == "127.0.0.1:8765" and authority("https://A.com:443/") == "a.com"
    d = s.directory()["keys"][0]
    assert d["kid"] == s.keyid and d["kty"] == "OKP" and d["crv"] == "Ed25519"


def test_title_count_reads_shopifys_search_count():
    """shop141.example / shop105.example (new30c, 10 Oct): Shopify prints its own result count in the search page title."""
    from radar.checks.library import _title_count
    assert _title_count('Search: 513 results found for "sonor" - pTron India') == 513
    assert _title_count('Search: 1,000 results found for "zircon"') == 1000
    assert _title_count("Search: ceramic | Mock Store") is None


# ---------------- journeys 28-30 (10 Oct 2026) ----------------

def test_footer_links_are_read_as_one_info_page_per_kind_same_store_only():
    from radar.discovery.discover import info_pages
    base = "https://shop.example.in"
    links = [{"text": "Annual Return FY 2024-25", "url": base + "/cdn/shop/t/234/assets/annual-return-fy-2024-25.pdf"},
             {"text": "Shipping & Returns", "url": base + "/pages/shipping-returns"},     # returns wins: one page, both
             {"text": "Delivery Information", "url": base + "/pages/delivery"},
             {"text": "Return policy", "url": base + "/policies/refund-policy"},           # refund already taken
             {"text": "Privacy", "url": "https://www.shop.example.in/pages/privacy-policy#top"},
             {"text": "T&C", "url": base + "/pages/terms-conditions"},                    # matched by the path
             {"text": "Contact Us", "url": base + "/pages/contact"},
             {"text": "Contact on WhatsApp", "url": "https://wa.me/919999999999"},          # another site
             {"text": "Track order", "url": base + "/apps/track-order"},                    # app proxy, not a page
             {"text": "Returns portal", "url": "https://returns.otherapp.com/shop"},
             {"text": "Shipping bags", "url": base + "/collections/shipping-bags"},         # a collection, not info
             {"text": "Annual Return FY 2024-25", "url": base + "/pages/annual-return"},       # a company filing
             {"text": "Return form", "url": base + "/cdn/shop/t/234/assets/returns.pdf"}]   # a file, not a page
    got = info_pages(links, base)
    assert [(p["kind"], p["url"]) for p in got] == [
        ("refund", base + "/pages/shipping-returns"), ("shipping", base + "/pages/delivery"),
        ("privacy", "https://www.shop.example.in/pages/privacy-policy"), ("terms", base + "/pages/terms-conditions"),
        ("contact", base + "/pages/contact")]
    assert info_pages([], base) == []
    # a company-law filing is not the refund page (shop143.example, run 38077075618), nor any file under /cdn/
    filing = [{"text": "Annual Return FY 2024-25", "url": base + "/cdn/shop/t/234/assets/annual-return-fy-2024-25.pdf"},
              {"text": "Annual Return", "url": base + "/pages/annual-return"},
              {"text": "Returns", "url": base + "/pages/returns.pdf"},
              {"text": "Return & Exchange", "url": base + "/pages/return-exchange"}]
    assert [(p["kind"], p["url"]) for p in info_pages(filing, base)] == [("refund", base + "/pages/return-exchange")]


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


def test_core_web_vitals_warn_only_when_poor():
    from radar.checks.library import vitals_verdict
    assert vitals_verdict({"lcp": 2300, "cls": 0.04})[1:] == ("LCP 2.3s, CLS 0.04", True)
    assert vitals_verdict({"lcp": None, "cls": 0})[1:] == ("LCP n/a, CLS 0.0", True)
    exp, act, ok = vitals_verdict({"lcp": 5200, "cls": 0.31, "by": "div.offer-banner"})
    assert not ok and act == "LCP 5.2s (poor > 4.0s); CLS 0.31 (poor > 0.25), biggest shift: div.offer-banner"
    assert vitals_verdict({"lcp": 4000, "cls": 0.25})[2]          # exactly at the threshold is not poor


def test_a_popup_radar_could_not_close_is_not_counted_as_a_covering_bar():
    from radar.checks.library import layout_verdicts
    v = layout_verdicts({"moved": 0, "vw": 412}, {"pct": 0, "popup_pct": 92, "by": []})[1]
    assert v[3] and v[2] == "0% (a popup Radar could not close covered 92%, not counted)"
    assert layout_verdicts({"moved": 8, "vw": 412}, {"pct": 0})[0][3]          # 8 px of sideways play: noise, not judged
def test_checkout_page_is_opened_only_inside_the_cart_flow():
    """robots.txt disallows /checkout on every Shopify store; Radar opens it (to see it renders, never filled) only when
    the cart flow is on, like /cart. Everything else under robots.txt is unchanged."""
    from radar.core.browser import Session
    from radar.core.robots import Robots
    rob = Robots("User-agent: *\nDisallow: /checkout\nDisallow: /checkouts/\nDisallow: /admin\n", "BugRadar")
    for cart, want in ((True, True), (False, False)):
        sess = Session.__new__(Session)
        sess.s, sess.robots = replace(Settings(), allow_cart_flow=cart), rob
        assert sess.allowed("https://x.in/checkout") is want
        assert sess.allowed("https://x.in/checkouts/cn/abc123") is want
        assert sess.allowed("https://x.in/admin") is False


def test_links_beyond_the_menu_skip_menu_info_product_and_file_links():
    from radar.discovery.discover import more_links
    b = "https://shop.example.in"
    links = [{"where": "announcement bar", "text": "Sale", "url": b + "/collections/sale"},
             {"where": "announcement bar", "text": "Shop", "url": b + "/collections/sale/"},          # same page again
             {"where": "homepage section", "text": "Bestseller", "url": b + "/products/lamp"},       # product: own tests
             {"where": "homepage section", "text": "New in", "url": b + "/collections/new"},
             {"where": "homepage section", "text": "Story", "url": b + "/pages/story"},
             {"where": "homepage section", "text": "Gifts", "url": b + "/collections/gifts"},          # 3rd: over the cap
             {"where": "homepage section", "text": "Men", "url": b + "/collections/men"},              # in the menu
             {"where": "footer", "text": "Shipping", "url": b + "/pages/shipping"},                     # info page, tested
             {"where": "footer", "text": "Annual Return", "url": b + "/pages/annual-return"},
             {"where": "footer", "text": "Brochure", "url": b + "/cdn/shop/files/brochure.pdf"},
             {"where": "footer", "text": "Blog", "url": b + "/blogs/news"},
             {"where": "footer", "text": "Insta", "url": "https://instagram.com/shop"},
             {"where": "footer", "text": "Email", "url": b + "/cdn-cgi/l/email-protection#5a3f"}]   # Cloudflare's email link
    got = more_links(links, b, [b + "/collections/men", b + "/pages/shipping"])
    assert [(x["where"], x["url"]) for x in got] == [
        ("announcement bar", b + "/collections/sale"), ("homepage section", b + "/collections/new"),
        ("homepage section", b + "/pages/story"), ("footer", b + "/blogs/news")]


def test_script_weight_warns_only_at_the_extreme():
    from radar.checks.library import scripts_verdict
    ok = scripts_verdict({"scripts": 40, "kb": 1800, "third": 12, "top": ["cdn.judge.me (3)"]})
    assert ok[2] and ok[1] == "40 scripts, at least 1.8 MB of JavaScript, 12 third-party script hosts: cdn.judge.me (3)"
    assert not scripts_verdict({"scripts": 90, "kb": 5200, "third": 8})[2]
    assert not scripts_verdict({"scripts": 60, "kb": 900, "third": 25})[2]
    assert scripts_verdict({})[1] == "0 scripts, at least 0 KB of JavaScript, 0 third-party script hosts"


def test_dead_store_addresses_are_told_apart_from_temporary_problems():
    from radar.discovery.discover import dead_domain_reason as why
    assert why("APIRequestContext.get: getaddrinfo ENOTFOUND shop142.example").startswith("the domain does not resolve")
    assert why("APIRequestContext.get: getaddrinfo EAI_AGAIN shop113.example").startswith("the domain did not resolve")
    assert why("APIRequestContext.get: connect ECONNREFUSED 127.0.0.1:443", "shop128.example") == \
        "the domain points to 127.0.0.1 (no server there), not to a store"
    assert why("connect ECONNREFUSED 127.0.0.1:9", "127.0.0.1") == ""      # a server address that is simply silent
    assert "(websitewelcome.com)" in why("APIRequestContext.get: Hostname/IP does not match certificate's altnames: "
                                        "Host: shop089.example. is not in the cert's altnames: DNS:*.websitewelcome.com, DNS:websi")
    assert why("APIRequestContext.get: self-signed certificate; if the root CA is installed locally").startswith("its HTTPS")
    for temporary in ("APIRequestContext.get: Timeout 20000ms exceeded.", "Client network socket disconnected before secure "
                      "TLS connection was established", "connect ECONNREFUSED 104.18.2.3:443", ""):
        assert why(temporary) == "", temporary


# ---------- journey 22: variant switch ----------
def test_variant_switch_targets_prefer_in_stock_other_price_then_fewest_changes():
    from radar.checks.library import variant_switch_targets, changed_options, variant_options
    vs = [{"id": 1, "available": True, "price": 1000, "option1": "S", "option2": "Red"},
          {"id": 2, "available": True, "price": 1000, "option1": "M", "option2": "Red"},     # same price
          {"id": 3, "available": True, "price": 1200, "option1": "L", "option2": "Blue"},    # other price, 2 changes
          {"id": 4, "available": False, "price": 1500, "option1": "XL", "option2": "Red"},
          {"id": 5, "available": False, "price": 1500, "option1": "XL", "option2": "Blue"}]
    t, sold = variant_switch_targets(vs, 1)
    assert t["id"] == 3 and sold["id"] == 4                 # a price change beats fewer clicks; sold-out: fewest changes
    assert changed_options(vs[2], vs[0]) == ["L", "Blue"] and changed_options(vs[1], vs[0]) == ["M"]
    assert variant_switch_targets(vs, "2")[0]["id"] == 3     # ids compared as text (page values are strings)
    # nothing different to pick: a single variant, or variants with the same option values
    assert variant_switch_targets([vs[0]], 1) == (None, None)
    assert variant_switch_targets([vs[0], dict(vs[0], id=9)], 1) == (None, None)
    # no other in-stock variant: only the sold-out one is used
    assert variant_switch_targets([vs[0], vs[3]], 1) == (None, vs[3])
    assert variant_options({"title": "Default Title"}) == [] and variant_options({"title": "50 ml / Rose"}) == ["50 ml", "Rose"]


def test_switch_verdict_ignores_the_address_radar_opened_with():
    """Journey 22 round 2: ?variant= that was already in the address before the pick is no evidence (shop099.example,
    shop137.example); any id field of the product form counts; form stale while the page shows the pick = not judged."""
    from radar.checks.library import switch_verdict, marks_values
    form = lambda v, *more: {"variant": v, "all": [v, *more]}
    # no form, address unchanged, page marks the picked pill -> ok; marks nothing -> not judged, never 'kept old'
    assert switch_verdict(2, "1", None, "1", True, False)[0] == "ok"
    assert switch_verdict(2, "1", None, "1", False, False)[0] == "unjudged"
    assert switch_verdict(2, "1", None, "1", False, False, page_vid="1")[:2] == ("bad", "1")
    # no form, the theme pushed an address: it decides
    assert switch_verdict(2, "1", None, "2", False, False)[0] == "ok"
    assert switch_verdict(2, "1", None, "3", False, False)[:2] == ("bad", "3")
    # a product form: authoritative; a second id field (no-js select / sticky form) holding the pick counts
    assert switch_verdict(2, "1", form("2"), "1", False, False)[0] == "ok"
    assert switch_verdict(2, "1", form("1", "2"), "1", False, False)[0] == "ok"
    assert switch_verdict(2, "1", form("1"), "1", True, False)[:2] == ("bad", "1")     # pill checked, nothing else: broken
    assert switch_verdict(2, "1", form("1"), "2", False, False)[0] == "unjudged"      # address pushed, id not
    assert switch_verdict(2, "1", form("1"), "1", True, True)[0] == "unjudged"        # pill + new price, id not
    assert marks_values(["2 Pack", "2 Pack\nRs. 2,099\nSave 12%"], ["2 Pack"]) and marks_values(["M"], ["M"])
    assert not marks_values(["1 Pack Rs. 1,199"], ["2 Pack"]) and not marks_values(["Small"], ["S"]) and not marks_values([], ["M"])


def test_sold_out_variant_blocks_buy_only_when_no_enabled_buy_button_is_left():
    from radar.checks.library import sold_out_blocks
    assert sold_out_blocks([{"text": "Sold out", "off": True}])[0]
    assert sold_out_blocks([{"text": "Notify me when available", "off": False}])[0]
    assert sold_out_blocks([])[0]
    ok, shown = sold_out_blocks([{"text": "Add to cart", "off": False}])
    assert not ok and shown == "'Add to cart'"
    assert not sold_out_blocks([{"text": "Sold out", "off": True}, {"text": "Buy it now", "off": False}])[0]


def test_variant_switch_case_is_generated_for_a_multi_variant_product_only():
    sm = _sm(products=[Product("sold", "Sold Lamp", "https://x.in/products/sold", 1, "9", False),
                       Product("vase", "Ceramic Vase", "https://x.in/products/vase", 2, "5", True, variants=3)])
    prod = next(s for s in build_suites(sm, Settings()) if s.id == "product")
    vs = [c for c in prod.cases if c.check == "variant_switch"]
    assert [c.id for c in vs] == ["product.variant_switch"] and vs[0].severity == "minor"
    assert vs[0].params == {"url": "https://x.in/products/vase?variant=2", "strict": False}
    one = next(s for s in build_suites(_sm(), Settings()) if s.id == "product")
    assert not any(c.check == "variant_switch" for c in one.cases)

def test_card_price_reads_the_sale_price_not_the_mrp_or_a_save_badge():
    from radar.checks.library import card_price
    sale = [{"text": "₹1,599.00", "struck": True}, {"text": "Save ₹300", "struck": False}, {"text": "₹1,299.00", "struck": False}]
    assert card_price(sale) == (1299.0, False)
    # MRP not struck (plain label) but higher: the lowest price is what the shopper pays
    assert card_price([{"text": "MRP Rs. 799", "struck": False}, {"text": "Rs. 649", "struck": False}]) == (649.0, False)
    assert card_price([{"text": "From ₹499.00", "struck": False}]) == (499.0, True)
    assert card_price([{"text": "20% off", "struck": False}, {"text": "1,099 INR", "struck": False}]) == (1099.0, False)
    assert card_price([{"text": "₹15 / 100 ml", "struck": False}, {"text": "₹300", "struck": False}]) == (300.0, False)
    assert card_price([{"text": "₹1,599.00", "struck": True}]) == (None, False)        # only the struck MRP: no price
    assert card_price([]) == (None, False)


def test_card_price_agrees_with_the_variant_or_the_lowest_price():
    from radar.checks.library import card_price_agrees
    assert card_price_agrees(1299.0, 129900, 129900)[0]
    assert card_price_agrees(1300.0, 129950, 129950)[0]                # the card rounds to whole rupees
    ok, how = card_price_agrees(1299.0, 139900, 129900)                 # 'From' / lowest variant
    assert ok and "lowest" in how
    ok, how = card_price_agrees(999.0, 129900, 129900)
    assert not ok and how == "₹999.00"


def test_price_consistency_case_only_with_the_cart_flow():
    sm = SiteMap(site_id="x.com", base_url="https://x.com", platform="shopify",
                 collections=[Collection("c", "C", "https://x.com/collections/c", 3)],
                 products=[Product("a", "Alpha Vase", "https://x.com/products/a", 11, "499.00", True)])
    cart = next(x for x in build_suites(sm, Settings()) if x.id == "cart")
    c = next(c for c in cart.cases if c.check == "price_consistency")
    assert c.id == "cart.price_consistency" and c.severity == "minor"
    assert c.params["collection_url"] == "https://x.com/collections/c" and c.params["product_handles"] == ["a"]
    assert c.params["strict"] is False
    assert not any(c.check == "price_consistency" for x in build_suites(sm, replace(Settings(), allow_cart_flow=False))
                   for c in x.cases)


# ---------- journey 18: cart drawer = cart page = /cart.js ----------

def test_cart_drawer_case_only_with_the_cart_flow():
    sm = SiteMap(site_id="x.com", base_url="https://x.com", platform="shopify",
                 collections=[Collection("c", "C", "https://x.com/collections/c", 3)],
                 products=[Product("a", "Alpha Vase", "https://x.com/products/a", 11, "499.00", True)])
    cart = next(x for x in build_suites(sm, Settings()) if x.id == "cart")
    c = next(c for c in cart.cases if c.check == "cart_drawer")
    assert c.id == "cart.drawer_and_page" and c.severity == "minor" and c.params["strict"] is False
    assert c.params["url"] == "https://x.com/products/a?variant=11" and c.params["cart_path"] == sm.cart_path
    assert not any(c.check == "cart_drawer" for x in build_suites(sm, replace(Settings(), allow_cart_flow=False))
                   for c in x.cases)


@pytest.mark.parametrize("text,want", [
    ("Your cart\nVase ₹1,299.00\nSubtotal\n₹1,299.00\nCheck out", 1299.0),         # label and amount in two elements
    ("Subtotal (2 items) ₹2,598.00", 2598.0),                                       # '(2 items)' is not ₹2
    ("You save ₹300.00\nEstimated total Rs. 1,299", 1299.0),                         # savings line skipped
    ("Taxes and shipping calculated at checkout\nTotal ₹499.00", 499.0),
    ("Your cart\nVase ₹1,299.00\nCheck out", None),                                 # no subtotal shown
])
def test_subtotal_in_text(text, want):
    from radar.checks.library import subtotal_in_text
    assert subtotal_in_text(text) == want


@pytest.mark.parametrize("text,want", [
    ("Ceramic Flower Vase\n₹1,299.00\nQty: 2", 2),
    ("Ceramic Flower Vase x1 ₹1,299.00", 1),
    ("Ceramic Flower Vase × 3", 3),
    ("Ceramic Flower Vase\n₹1,299.00", None),
    ("Wooden Spoon Set Qty: 4", None),                                              # another product's line
])
def test_qty_in_text(text, want):
    from radar.checks.library import qty_in_text
    assert qty_in_text(text, "Ceramic Flower Vase") == want


def test_cart_view_mismatches_in_plain_words():
    from radar.checks.library import cart_view_mismatches
    truth = {"qty": 1, "totals": [129900]}
    ok = {"listed": True, "empty": False, "qty": 1, "subtotal": 1299.0}
    assert cart_view_mismatches("the cart drawer", ok, truth) == []
    assert cart_view_mismatches("the cart drawer", dict(ok, qty=None, subtotal=None), truth) == []   # not shown: not judged
    stale = dict(ok, qty=2, subtotal=2598.0)
    assert cart_view_mismatches("the cart drawer", stale, truth) == [
        "quantity differs: the cart drawer shows 2, /cart.js says 1",
        "subtotal differs: the cart drawer shows ₹2,598.00, /cart.js says ₹1,299.00"]
    empty_page = {"listed": False, "empty": True, "qty": None, "subtotal": None}
    assert cart_view_mismatches("the cart page", empty_page, truth, ok) == [
        "the cart page does not list the product and says the cart is empty (the cart drawer showed it)"]
    assert cart_view_mismatches("the cart page", dict(ok, qty=3), truth, stale)[0] == \
        "quantity differs: the cart page shows 3, /cart.js says 1, the drawer showed 2"
    # a cart-level discount: the subtotal may be either total_price or items_subtotal_price
    assert cart_view_mismatches("the cart page", ok, {"qty": 1, "totals": [109900, 129900]}) == []
# ---------- journey 24: sold-out product page ----------
from radar.checks.library import sold_out_wording, sold_out_verdict


@pytest.mark.parametrize("text,want", [
    ("Brass Table Lamp ₹2,499.00 Sold out", "Sold out"),
    ("SOLD OUT", "SOLD OUT"),
    ("Sold-out", "Sold-out"),
    ("This item is Out of stock", "Out of stock"),
    ("Out-of-stock", "Out-of-stock"),
    ("Notify me when available", "Notify me"),
    ("Email me when it's back", "Email me when it's back"),
    ("Coming soon", "Coming soon"),
    ("Unavailable", "Unavailable"),
    ("Currently not available", "Currently not available"),
    ("Back in stock soon!", "Back in stock soon"),
])
def test_sold_out_wording_finds_what_stores_show(text, want):
    assert sold_out_wording(text) == want


@pytest.mark.parametrize("text", ["Add to cart", "In stock, ready to ship", "Only 3 left", "Buy it now",
                                  "Cash on delivery available", "Soldier figurine", ""])
def test_sold_out_wording_ignores_normal_buy_text(text):
    assert sold_out_wording(text) == ""


def test_sold_out_verdict_fails_only_an_enabled_button_with_no_sold_out_words():
    assert sold_out_verdict(None, "Sold out")[2] and "page says 'Sold out'" in sold_out_verdict(None, "Sold out")[1]
    assert sold_out_verdict(None, "")[2]                                     # no button at all: nothing to buy
    assert sold_out_verdict({"text": "Sold out", "enabled": False}, "Sold out")[1] == "buy button 'Sold out' disabled; page says 'Sold out'"
    assert sold_out_verdict({"text": "Add to cart", "enabled": True}, "Out of stock")[2]   # told, Shopify refuses the add
    exp, actual, ok = sold_out_verdict({"text": "Add to cart", "enabled": True}, "")
    assert not ok and "ENABLED and no sold-out wording" in actual and "'Add to cart'" in actual


def test_builder_adds_a_sold_out_case_only_when_the_store_has_a_sold_out_product():
    prod = next(s for s in build_suites(_sm(), Settings()) if s.id == "product")
    so = next(c for c in prod.cases if c.check == "sold_out_page")
    assert so.id == "product.sold_out" and so.severity == "minor" and so.params["url"] == "https://x.in/products/sold?variant=1"
    assert so.params["strict"] is False and so.params["cart_probe"] is True
    no_cart = next(s for s in build_suites(_sm(), replace(Settings(), allow_cart_flow=False)) if s.id == "product")
    assert next(c for c in no_cart.cases if c.check == "sold_out_page").params["cart_probe"] is False
    in_stock = _sm(products=[Product("vase", "Ceramic Vase", "https://x.in/products/vase", 2, "5", True)])
    assert not any(c.check == "sold_out_page" for s in build_suites(in_stock, Settings()) for c in s.cases)
# ---------- journey 25: collection filter (11 Oct 2026) ----------
SHOPIFY_ROBOTS = ("User-agent: *\nDisallow: /checkout\nDisallow: /collections/*sort_by*\nDisallow: /*/collections/*sort_by*\n"
                  "Disallow: /collections/*filter*&*filter*\nDisallow: /collections/*+*\nDisallow: /collections/*%2B*\n"
                  "Disallow: /search\n\nUser-agent: adsbot-google\nDisallow: /\n")


def test_robots_wildcards_follow_shopify_default_on_every_python():
    """Python's own parser reads '*' literally up to 3.12 and encodes the '&' of '*filter*&*filter*' in 3.13: Radar's
    own RFC 9309 matcher must still refuse sort_by and two filters, and allow one filter."""
    from radar.core.robots import Robots, robots_rules, rules_allow
    r = Robots(SHOPIFY_ROBOTS, "BugRadar/0.1")
    c = "https://x.in/collections/all"
    assert r.allowed(c) and r.allowed(c + "?filter.v.availability=1") and r.allowed(c + "?page=2")
    assert not r.allowed(c + "?sort_by=price-ascending")
    assert not r.allowed(c + "?filter.v.availability=1&sort_by=manual")
    assert not r.allowed(c + "?filter.v.availability=1&filter.v.price.gte=")       # Dawn's empty price boxes count
    assert not r.allowed(c + "?filter.v.availability=1&filter.p.vendor=Acme")
    assert not r.allowed("https://x.in/collections/a+b") and not r.allowed("https://x.in/collections/a%2Bb")
    assert not r.allowed("https://x.in/en/collections/all?sort_by=x")
    rules = robots_rules(SHOPIFY_ROBOTS, "BugRadar")
    assert (False, "/") not in rules                       # another bot's group never applies to Radar
    assert rules_allow([(False, "/a"), (True, "/a/b")], "/a/b/c") and not rules_allow([(False, "/a"), (True, "/a/b")], "/a/c")
    assert rules_allow([(False, "/x$")], "/xy") and not rules_allow([(False, "/x$")], "/x")
    assert robots_rules("User-agent: BugRadar\nDisallow: /p\nUser-agent: *\nDisallow: /", "bugradar") == [(False, "/p")]


def test_filter_address_has_exactly_one_filter_and_the_one_filter_address_is_built_clean():
    from radar.checks.library import applied_filters, one_filter_url
    u = "https://x.in/collections/all?filter.v.availability=1&filter.v.price.gte=&filter.v.price.lte=&sort_by=manual"
    assert applied_filters(u) == [("filter.v.availability", "1")]                 # empty boxes and sort_by are not filters
    assert applied_filters("https://x.in/collections/all?filter.v.availability=1&filter.p.vendor=A%20B") == [
        ("filter.v.availability", "1"), ("filter.p.vendor", "A B")]
    assert applied_filters("https://x.in/collections/all?page=2") == []
    assert one_filter_url(u, "filter.v.availability", "1") == "https://x.in/collections/all?filter.v.availability=1"
    assert one_filter_url("https://x.in/collections/all#top", "filter.v.option.color", "Dark Red") == \
        "https://x.in/collections/all?filter.v.option.color=Dark+Red"


def test_filter_pick_prefers_in_stock_and_never_a_price_box_or_an_empty_option():
    from radar.checks.library import pick_filter
    o = lambda n, v, label="": {"name": n, "value": v, "label": label}
    assert pick_filter([o("filter.v.price.gte", ""), o("filter.p.vendor", "Acme"), o("filter.v.availability", "1")])["name"] \
        == "filter.v.availability"
    assert pick_filter([o("filter.v.availability", "0"), o("filter.v.option.size", "M")])["value"] == "M"
    assert pick_filter([o("filter.p.m.custom.fabric", "Silk"), o("filter.p.tag", "new", "New (0)")])["value"] == "Silk"
    assert pick_filter([o("filter.v.price.lte", "500")]) is None and pick_filter([]) is None
    assert pick_filter([o("filter.v.option.size", "S", "Small (0)‎"), o("filter.v.option.size", "M", "Medium (2)")])["value"] == "M"


def test_facet_count_reads_the_themes_own_count_next_to_a_filter_option():
    from radar.checks.library import facet_count
    assert facet_count("Small (43)‎") == 43                     # shop121.example: an invisible direction mark after it
    assert facet_count("In stock (1,204)") == 1204 and facet_count("Out of stock ( 0 )") == 0
    assert facet_count("In stock only") is None and facet_count("") is None and facet_count("Size (S) Blue") is None


def test_filter_truth_comes_from_the_stores_own_product_data():
    from radar.checks.library import filter_matches, price_order_ok
    p = {"vendor": "Acme Co", "product_type": "Mugs", "tags": ["New", "Gift"],
         "variants": [{"available": False, "option1": "Red", "option2": "L"}, {"available": True, "option1": "Blue"}]}
    sold = {"variants": [{"available": False}]}
    assert filter_matches(p, "filter.v.availability", "1") is True and filter_matches(sold, "filter.v.availability", "1") is False
    assert filter_matches(p, "filter.v.availability", "0") is None
    assert filter_matches(p, "filter.v.option.color", "red") is True and filter_matches(p, "filter.v.option.color", "Green") is False
    assert filter_matches(p, "filter.p.vendor", "Acme Co") is True and filter_matches(p, "filter.p.product_type", "Plates") is False
    assert filter_matches(p, "filter.p.tag", "gift") is True and filter_matches(sold, "filter.p.vendor", "x") is None
    assert filter_matches(p, "filter.p.m.custom.fabric", "Silk") is None
    assert price_order_ok(["a", "zz", "b"], {"a": 1.0, "b": 2.0})[0]
    ok, why = price_order_ok(["b", "a"], {"a": 1.0, "b": 2.0})
    assert not ok and why == "a (₹1.00) listed after b (₹2.00)"


def test_builder_adds_one_filter_case_for_the_biggest_collection():
    sm = _sm(collections=[Collection("small", "Small", "https://x.in/collections/small", 3),
                          Collection("big", "Big", "https://x.in/collections/big", 40)])
    cat = next(s for s in build_suites(sm, Settings()) if s.id == "catalog")
    f = [c for c in cat.cases if c.check == "collection_filter"]
    assert [c.id for c in f] == ["catalog.filter.big"] and f[0].severity == "minor"
    assert f[0].params == {"url": "https://x.in/collections/big", "strict": False,
                           "products_json": "https://x.in/collections/big/products.json?limit=250"}
def test_crawl_delay_is_read_for_radars_token():
    from radar.core.robots import Robots
    assert Robots("User-agent: *\nCrawl-delay: 5\nDisallow: /cart\n", "BugRadar/0.1 (+bugradar.in)").crawl_delay() == 5.0
    assert Robots("User-agent: *\nDisallow: /cart\n", "BugRadar/0.1").crawl_delay() is None
    assert Robots(None, "BugRadar/0.1").crawl_delay() is None


# ---------- new30i/j: empty / landing collections warn, menu hop targets ----------
def test_store_warning_makes_a_hard_step_a_warning_and_other_failures_stay_failures():
    from radar.checks.library import Steps, StepFailed, StoreWarning

    def landing():
        raise StoreWarning("products listed: expected ≥ 1, got 0: a landing page with no product grid")

    def broken():
        raise AssertionError("products listed: expected ≥ 1, got 0")
    st = Steps()
    assert st.run("lists_products", landing) is None
    assert st.items[-1].status == "warn" and "landing page" in st.items[-1].error
    with pytest.raises(StepFailed):
        st.run("lists_products", broken)
    assert st.items[-1].status == "fail"


def test_menu_hop_targets_are_other_store_pages_only():
    from radar.checks.library import _hop_target
    home = "https://store.example/"
    assert _hop_target(home, "/pages/coffee")
    assert _hop_target(home, "https://store.example/pages/bakehouse")
    for h in ("/", "#", "javascript:void(0)", "/products/x", "/cart", "/account/login", "/search?q=a",
              "https://other.example/pages/coffee", ""):
        assert not _hop_target(home, h), h

# ---------- journeys 33 + 35: buy-now and third-party checkout apps ----------

def test_checkout_app_named_from_its_host_or_address():
    from radar.discovery.detect import checkout_app_of, CHECKOUT_APP_HOSTS
    assert checkout_app_of("https://pdp.gokwik.co/v4/checkout?x=1") == "GoKwik"
    assert checkout_app_of("https://checkout.shopflo.co/s/abc") == "Shopflo"
    assert checkout_app_of("https://fastrr-boost-ui.pickrr.com/") == "Shiprocket Fastrr"
    assert checkout_app_of("https://api.razorpay.com/v1/checkout/public") == "Razorpay Magic Checkout"
    assert checkout_app_of("https://checkout.getsimpl.com/1") == "Simpl"
    assert checkout_app_of("http://localhost:9/gokwik/checkout?items=a") == "GoKwik"      # app named in the address
    assert checkout_app_of("https://notgokwik.co.evil.com/") == ""                       # host suffix, not substring
    assert checkout_app_of("https://store.in/checkouts/cn/abc") == "" and checkout_app_of("") == ""
    assert set(CHECKOUT_APP_HOSTS) >= {"GoKwik", "Shopflo", "Shiprocket Fastrr", "Razorpay Magic Checkout", "Simpl"}


def test_checkout_apps_found_in_scripts_iframes_and_html():
    from radar.discovery.detect import checkout_apps_in
    assert checkout_apps_in(["https://cdn.shopify.com/a.js", "https://pdp.gokwik.co/merchant.js"]) == ["GoKwik"]
    assert checkout_apps_in([], "<script>window.shopfloConfig = {}</script>") == ["Shopflo"]
    assert checkout_apps_in(["https://checkout.getsimpl.com/x.js"], "magic-checkout") == ["Razorpay Magic Checkout", "Simpl"]
    assert checkout_apps_in(["https://cdn.shopify.com/a.js"], "<html>plain</html>") == []


def test_checkout_first_screen_verdict():
    from radar.checks.library import checkout_view_verdict
    t = "Ceramic Flower Vase"
    tp = lambda **k: checkout_view_verdict({"text": "Mock Store secured by GoKwik", "fields": 0, "phone": "", "otp": False,
                                            "error": "", **k}, t, True)
    assert tp(phone="Enter mobile number", fields=1)[0] and "NOT filled" in tp(phone="Enter mobile number")[1]
    assert tp(otp=True)[0] and tp(text="Order summary: Ceramic Flower Vase × 1")[0]
    assert tp(text="", fields=0) == (False, "blank (no text, no field)")
    assert not tp(text="Something went wrong, please try again later", error="Something went wrong")[0]
    assert not tp()[0]                                      # text, but no phone screen and no order summary
    shop = lambda **k: checkout_view_verdict({"text": "Contact Delivery Payment Ceramic Flower Vase × 1", "fields": 2,
                                              "phone": "", "otp": False, "error": "", **k}, t, False)
    assert shop()[0] and not shop(fields=0)[0] and not shop(text="Contact Delivery Payment, your cart")[0]
    assert not shop(text="There was a problem with our checkout", error="There was a problem")[0]


def test_buy_now_case_only_with_the_cart_flow():
    cart = next(s for s in build_suites(_sm(), Settings()) if s.id == "cart")
    c = next(c for c in cart.cases if c.check == "buy_now")
    assert c.id == "cart.buy_now" and c.severity == "minor" and c.params["strict"] is False
    assert c.params["variant_id"] == 2 and c.params["url"].endswith("?variant=2")
    assert cart.cases[0].check == "add_to_cart"             # the add-to-cart test stays first; it never clicks buy-now
    assert not any(c.check == "buy_now" for x in build_suites(_sm(), replace(Settings(), allow_cart_flow=False))
                   for c in x.cases)


# ---------- open incidents pin their product / collection into every later run (14 Oct, Anmol: option (a)) ----------
def test_builder_pins_products_and_collections_with_open_incidents():
    from radar.core.models import Collection as C, Product as P
    prods = [P(f"p{i}", f"Product {i}", f"https://x.in/products/p{i}", i, "5", True) for i in range(1, 6)]
    prods.append(P("sold", "Sold Lamp", "https://x.in/products/sold", 9, "9", False))
    cols = [C(f"c{i}", f"Col {i}", f"https://x.in/collections/c{i}") for i in range(1, 5)]
    s = replace(Settings(), max_products=2, max_collections=2)
    plain = build_suites(_sm(products=prods, collections=cols), s)
    pdp = [c.id for c in next(x for x in plain if x.id == "product").cases if c.id.startswith("product.pdp.")]
    assert pdp == ["product.pdp.p1", "product.pdp.p2"]        # the normal sample: first two in stock
    # p5 (never sampled) and the sold-out lamp have open incidents; so does collection c4
    sm = _sm(products=prods, collections=cols, pinned_products=["p5", "sold"], pinned_collections=["c4"])
    pinned = build_suites(sm, s)
    pcases = [c for c in next(x for x in pinned if x.id == "product").cases if c.id.startswith("product.pdp.")]
    assert [c.id for c in pcases] == ["product.pdp.p5", "product.pdp.sold", "product.pdp.p1", "product.pdp.p2"]
    assert pcases[0].params["fallbacks"] == [] and pcases[2].params["fallbacks"]   # a pin is judged on its own page
    assert pcases[1].params["expect_buyable"] is False
    ccases = [c.id for c in next(x for x in pinned if x.id == "catalog").cases if c.id.startswith("catalog.collection.")]
    assert ccases == ["catalog.collection.c4", "catalog.collection.c1", "catalog.collection.c2"]   # pins never eat coverage


def test_storage_lists_open_incident_cases_per_device_and_closes_gone_pins(tmp_path):
    from radar.core.storage import Storage
    st = Storage(tmp_path)
    st._touch_incident("x.in|product.pdp.p5|title", "x.in", "product.pdp.p5", "no title")
    st._touch_incident("x.in|product.pdp.p6|title|mobile", "x.in", "product.pdp.p6", "no title")
    st._touch_incident("x.in|catalog.collection.c4|cards", "x.in", "catalog.collection.c4", "no cards")
    st.db.commit()
    assert st.open_incident_cases("x.in") == ["catalog.collection.c4", "product.pdp.p5"]
    assert st.open_incident_cases("x.in", "mobile") == ["product.pdp.p6"]
    assert st.resolve_incidents_for_cases("x.in", {"product.pdp.p5", "product.pdp.p6"}, "gone") == ["x.in|product.pdp.p5|title"]
    inc = {i["case_id"]: i for i in st.incidents("x.in")}
    assert inc["product.pdp.p5"]["status"] == "resolved" and inc["product.pdp.p5"]["last_error"] == "gone"
    assert inc["product.pdp.p6"]["status"] == "open" and inc["catalog.collection.c4"]["status"] == "open"
    assert st.open_incident_cases("x.in") == ["catalog.collection.c4"]


def test_product_data_from_the_store_domain_when_a_headless_subdomain_answers_html():
    """shop095.example re-test (run 38534330264, phone): product pages live on shop.shop095.example, whose /products/<h>.js
    answers the HTML page; the store's own domain answers the data. Radar asks the store's domain next."""
    from types import SimpleNamespace
    from radar.checks import library as L

    calls = []

    def get_json(u):
        calls.append(u)
        if u.startswith("https://shop.example.in/"):
            raise AssertionError("HTTP 200 but the body is not JSON")
        return {"id": 1, "title": "Ring", "variants": [{"id": 11, "price": 99900, "available": True}]}

    sess = SimpleNamespace(get_json=get_json, evaluate=lambda *a: [], store_url="https://example.in",
                           page=SimpleNamespace(url="https://shop.example.in/products/ring-1"))
    seen = []
    ctx = SimpleNamespace(sess=sess, expect=lambda what, e, a, ok=None: seen.append((what, a, ok)))
    p = L._product_js(ctx, "https://shop.example.in/products/ring-1")
    assert p["title"] == "Ring" and calls == ["https://shop.example.in/products/ring-1.js", "https://example.in/products/ring-1.js"]
    assert any("store's own domain" in w for w, _, _ in seen), seen
    # an unrelated store domain is never asked
    sess.store_url, calls[:] = "https://other.in", []
    import pytest
    with pytest.raises(AssertionError):
        L._product_js(ctx, "https://shop.example.in/products/ring-1")
    assert calls == ["https://shop.example.in/products/ring-1.js"]


# ---------- store features (14 Oct): pincode checker, discount box ----------
def test_builder_adds_the_store_feature_cases():
    suites = build_suites(_sm(), replace(Settings(), probe_pincode="560001"))
    pin = next(c for c in next(x for x in suites if x.id == "product").cases if c.id == "product.pincode")
    assert pin.check == "pincode_check" and pin.params["probe"] == "560001" and pin.params["strict"] is False
    assert pin.params["url"].endswith("/products/vase?variant=2")        # the first sampled in-stock product
    code = next(c for c in next(x for x in suites if x.id == "cart").cases if c.id == "cart.discount_code")
    assert code.check == "discount_code" and code.params["variant_id"] == 2 and code.params["strict"] is False
    no_cart = build_suites(_sm(), replace(Settings(), allow_cart_flow=False))
    assert not any(c.id == "cart.discount_code" for x in no_cart for c in x.cases)     # cart flow off = no cart tests
    assert any(c.id == "product.pincode" for x in no_cart for c in x.cases)            # the pincode box is read-only


def test_store_wide_prepaid_discount_is_recognised():
    from radar.checks.library import store_wide_discount
    assert "minus 5%" in store_wide_discount(1149.0, {1092.0, 1697.0}, "EXTRA 5% OFF ON ALL PREPAID ORDERS")
    assert store_wide_discount(1149.0, {1092.0}, "Free shipping on all orders") is None      # no offer named
    assert store_wide_discount(1149.0, {700.0}, "extra 5% off prepaid") is None              # 39% below: not an offer
    assert store_wide_discount(1149.0, {1149.0}, "5% off prepaid") is None                   # not below


def test_storage_opens_concurrently_on_a_fresh_database(tmp_path):
    """shop050.example (new30v): parallel scans opening a new radar.db at once hit 'database is locked'."""
    import threading
    from radar.core.storage import Storage
    errs = []
    def go():
        try:
            Storage(tmp_path / "data").upsert_site("x.in", "https://x.in", "shopify")
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs


def test_per_unit_price_of_a_multi_unit_variant():
    from radar.checks.library import per_unit_price
    t = "1 Month ₹ 1,799 / Bottle 1 Bottle · 30 sachets 2 Months ₹ 1,649 / Bottle 2 Bottles · 60 sachets"
    assert "× 2" in per_unit_price(3298.0, {1799.0, 1649.0, 1499.0}, t)
    assert per_unit_price(3298.0, {1649.0}, "Only ₹1,649 today") is None          # no per-unit wording
    assert per_unit_price(3298.0, {1000.0}, "₹1,000 / bottle, 2 bottles") is None  # does not multiply out


def test_more_links_skip_slideshow_controls():
    """shop006.example (new31b): the hero slideshow's dots are links ('Slide 1, current', 'Load slide 1' ->
    /Hero-slideshow); Radar opened them as pages and failed them. They are carousel controls."""
    from radar.discovery.discover import more_links
    links = [{"where": "homepage section", "text": "Slide 1, current", "url": "https://s.in/Hero-slideshow"},
             {"where": "homepage section", "text": "Load slide 2", "url": "https://s.in/Hero-slideshow-2"},
             {"where": "homepage section", "text": "Our story", "url": "https://s.in/pages/our-story"}]
    got = [x["url"] for x in more_links(links, "https://s.in", [])]
    assert got == ["https://s.in/pages/our-story"], got


def test_more_links_skip_broken_javascript_hrefs():
    """shop053.example (new31e): 'Quick Shop' buttons carry href='javascript void(0)' (no colon), which resolves to
    /javascript%20void(0); a script control, not a page to open."""
    from radar.discovery.discover import more_links
    links = [{"where": "homepage section", "text": "Quick Shop", "url": "https://s.in/javascript%20void(0)"},
             {"where": "homepage section", "text": "Our story", "url": "https://s.in/pages/our-story"}]
    assert [x["url"] for x in more_links(links, "https://s.in", [])] == ["https://s.in/pages/our-story"]


def test_gst_inclusive_price():
    """shop039.example (new31f): ₹32,000 in Shopify, 'Rs. 32,960 INR' on the page = +3% GST."""
    from radar.checks.library import gst_inclusive
    assert "3% GST" in gst_inclusive(32000.0, [32960.0])
    assert "18% GST" in gst_inclusive(1000.0, [1180.0])
    assert gst_inclusive(32000.0, [33500.0]) is None
