"""End-to-end: the real framework, a real Chromium, a local fake Shopify store.
Each mode proves one promise. Takes ~1 minute. Skip with: pytest -m "not e2e"
"""
import json
import re
from dataclasses import replace

import pytest

from radar.core.config import Settings
from radar.healing.llm import LLMClient
from radar.runner.executor import scan
from tests.mockstore.server import serve

pytestmark = pytest.mark.e2e


def _settings(tmp_path, **kw):
    kw.setdefault("net_probe_urls", ())      # connectivity check off unless a test exercises it
    return replace(Settings(), data_dir=tmp_path, sites_dir=tmp_path / "sites", delay_seconds=0,
                   llm_provider="none", **kw)


def _scan(mode, tmp_path, llm=None, **kw):
    srv, url = serve(mode)
    try:
        return scan(url, _settings(tmp_path, **kw), llm=llm)
    finally:
        srv.shutdown()


def _case(run, prefix):
    return next(c for c in run.cases if c.case_id.startswith(prefix))


def test_healthy_store_all_pass_and_artifacts(tmp_path):
    run, d = _scan("healthy", tmp_path)
    assert run.verdict == "healthy", [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases]
    assert {c.suite for c in run.cases} == {"journey", "smoke", "catalog", "product", "cart", "search", "health"}
    j = {s.name: s for s in _case(run, "journey.").attempts[-1].steps}
    assert all(s.status in ("pass", "info") for s in j.values()), [(s.name, s.error) for s in j.values()]
    checks = {c["what"]: c for s in j.values() for c in s.checks}
    assert checks["opened the product that was clicked"]["ok"]
    assert checks["product added to cart"]["actual"].startswith("Ceramic Flower Vase")
    assert checks["variant the page sent to /cart/add"]["actual"] == "201 (Ceramic Flower Vase)"  # main form, NOT the quick-add card (301)
    assert checks["cart item count"]["actual"] == "0 → 1" and checks["quantity added"]["actual"] == "1"
    assert checks["unit price in cart"]["actual"] == "₹1,299.00"
    assert checks["an in-stock variant exists"]["actual"] == "2 of 2 available"
    # no green tick next to an actual that reads like a failure (the 'none available' label bug, 5 Oct)
    lying = [(c.case_id, ch) for c in run.cases for a in c.attempts for st in a.steps for ch in st.checks
             if ch["ok"] and ch["actual"] != ch["expected"] and re.match(r"(none|not on page|missing|\(missing\)|NOT )", str(ch["actual"]))]
    assert not lying, lying
    assert "drawer" in j["cart_shows_product"].detail and "NOT clicked" in j["checkout_button_ready"].detail
    assert run.sitemap_summary["products"] == 4 and run.sitemap_summary["in_stock"] == 3
    assert run.sitemap_summary["nav"] == 4                       # hidden drawer link found too
    assert not any("sampler" in c.case_id for c in run.cases)   # Rs 0 sampler never tested
    assert any("priced 0" in n for n in run.notes)
    assert not any("gift-pouch" in str(c.__dict__) for c in run.cases)   # Rs 1 hidden freebie never tested
    assert any("priced like a free gift" in n and "gift-pouch" in n for n in run.notes)
    # sold-out product is never chosen for the cart test
    assert _case(run, "cart.").title.find("Lamp") == -1
    for f in ("run.json", "report.html"):
        assert (d / f).exists()
    assert (d.parents[1] / "index.html").exists() and (d.parents[1] / "sitemap.json").exists()
    assert json.loads((d / "run.json").read_text())["verdict"] == "healthy"


def test_renamed_button_is_healed_with_stable_selector(tmp_path):
    run, _ = _scan("renamed_button", tmp_path)
    assert run.verdict == "healthy"
    assert len(run.healing_events) == 1
    ev = run.healing_events[0]
    assert ev["intent"] == "add_to_cart" and ev["method"] == "heuristic"
    assert "nth-of-type" not in ev["new"], ev["new"]           # not a brittle positional path
    # second use came from the cache, not a second healing
    cart_steps = _case(run, "cart.").attempts[-1].steps
    assert any("cache" in str(s.detail) for s in cart_steps if s.name == "click_add_to_cart")


def test_broken_price_confirmed_with_evidence(tmp_path):
    run, d = _scan("broken_price", tmp_path)
    bad = _case(run, "product.pdp.ceramic")
    assert bad.verdict == "confirmed_fail" and len(bad.attempts) == 2
    assert bad.attempts[-1].failed_step == "structured_data_valid"
    assert (d / bad.attempts[-1].screenshot).exists() and (d / bad.attempts[-1].trace).exists()
    assert run.verdict == "degraded"


def test_broken_cart_means_down_and_incident(tmp_path):
    run, d = _scan("cart_broken", tmp_path)
    assert _case(run, "cart.").verdict == "confirmed_fail"
    assert _case(run, "journey.").verdict == "confirmed_fail"
    assert _case(run, "journey.").attempts[-1].failed_step == "cart_received_this_product"
    assert run.verdict == "down"
    from radar.core.storage import Storage
    inc = Storage(tmp_path).incidents(run.site_id)
    assert len(inc) == 2 and all(i["status"] == "open" for i in inc)   # journey + cart test


def test_llm_rung_heals_when_heuristic_cannot(tmp_path):
    def fake(url, headers, body):
        listing = body["messages"][0]["content"]
        m = re.search(r"\[(\d+)\] <span> text='Grab it'", listing)
        idx = int(m.group(1)) if m else None
        return {"content": [{"type": "text", "text": json.dumps({"index": idx, "confidence": 0.9, "reason": "buy CTA"})}],
                "usage": {"input_tokens": 400, "output_tokens": 20}}
    llm = LLMClient("anthropic", "claude-haiku-4-5-20251001", 5, api_key="test", transport=fake)
    run, _ = _scan("obscure_button", tmp_path, llm=llm)
    assert run.verdict == "healthy", [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases]
    assert run.healing_events and run.healing_events[0]["method"] == "llm"
    assert run.llm_usage["calls"] == 1                          # healed once, then cached


def test_obscure_button_without_llm_fails_honestly(tmp_path):
    run, _ = _scan("obscure_button", tmp_path)
    c = _case(run, "cart.")
    assert c.verdict == "confirmed_fail" and "LLM healing disabled" in c.attempts[-1].error


def test_non_shopify_is_unsupported(tmp_path):
    run, _ = _scan("not_shopify", tmp_path)
    assert run.verdict == "unsupported" and run.cases == []


def test_js_redirecting_page_does_not_crash(tmp_path):
    from radar.core.models import Suite, TestCase
    import radar.runner.executor as ex
    orig = ex.build_suites
    ex.build_suites = lambda sm, s: [Suite("product", "P", "", [TestCase(
        "product.pdp.sampler", "product", "Sampler", "product_page",
        {"url": sm.base_url + "/products/free-sampler", "expect_buyable": True})])]
    try:
        run, _ = _scan("healthy", tmp_path)
    finally:
        ex.build_suites = orig
    c = run.cases[0]
    steps = {s.name: s for s in c.attempts[-1].steps}
    assert c.verdict == "pass", c.attempts[-1].error
    assert steps["url_stable"].status == "warn" and "ceramic-vase" in steps["url_stable"].error


def test_no_cart_flag(tmp_path):
    run, _ = _scan("healthy", tmp_path, allow_cart_flow=False)
    assert "cart" not in {c.suite for c in run.cases}
    j = _case(run, "journey.")
    assert j.verdict == "pass" and not any(s.name == "click_add_to_cart" for s in j.attempts[-1].steps)


def test_blank_page_fails(tmp_path):
    from radar.core.models import Suite, TestCase
    import radar.runner.executor as ex
    orig = ex.build_suites
    ex.build_suites = lambda sm, s: [Suite("smoke", "S", "", [TestCase(
        "smoke.blank", "smoke", "Blank page", "page_health", {"url": sm.base_url + "/pages/blank"}, "critical")])]
    try:
        run, _ = _scan("healthy", tmp_path)
    finally:
        ex.build_suites = orig
    c = run.cases[0]
    assert c.verdict == "confirmed_fail" and "blank" in c.attempts[-1].error


def test_store_sending_wrong_product_is_caught_with_names(tmp_path):
    """The store's own buy button sends another product's id: must fail, naming both products."""
    run, _ = _scan("wrong_variant", tmp_path)
    for prefix in ("cart.", "journey."):
        c = _case(run, prefix)
        assert c.verdict == "confirmed_fail", (prefix, c.attempts[-1].error)
        assert c.attempts[-1].failed_step == "cart_received_this_product"
        assert "expected 201 (Ceramic Flower Vase), got 301" in c.attempts[-1].error
    assert run.verdict == "down"


def test_free_gift_added_by_store_passes_with_warning(tmp_path):
    """Moxie adds a free Travel Pouch in a second request: correct store behaviour, not a failure."""
    run, _ = _scan("free_gift", tmp_path, )
    for prefix in ("cart.", "journey."):
        c = _case(run, prefix)
        assert c.verdict == "pass", (prefix, c.attempts[-1].error)
        steps = {s.name: s for s in c.attempts[-1].steps}
        assert steps["cart_received_this_product"].status == "pass"
        assert steps["free_gift_added"].status == "warn" and "Vase Sampler" in steps["free_gift_added"].error
        assert steps["cart_total_increase"].status == "pass"


def _checks(case):
    return [k for s in case.attempts[-1].steps for k in s.checks]


def test_newsletter_popup_is_closed_never_subscribed(tmp_path):
    run, _ = _scan("newsletter_popup", tmp_path)
    j = _case(run, "journey.")
    assert j.verdict == "pass", j.attempts[-1].error
    closed = [k for k in _checks(j) if k["what"] == "popup popup closed"]
    assert closed and all("'×'" in k["actual"] for k in closed)       # the close button, not Subscribe


def test_cookie_banner_declined_not_accepted(tmp_path):
    run, _ = _scan("cookie_banner", tmp_path)
    j = _case(run, "journey.")
    assert j.verdict == "pass", j.attempts[-1].error
    closed = [k for k in _checks(j) if k["what"] == "cookie popup closed"]
    assert closed and all("'Decline'" in k["actual"] for k in closed)


def test_size_must_be_chosen_like_a_shopper(tmp_path):
    run, _ = _scan("variant_required", tmp_path)
    for prefix in ("journey.", "cart."):
        c = _case(run, prefix)
        assert c.verdict == "pass", (prefix, c.attempts[-1].error)
        k = next(k for k in _checks(c) if k["what"] == "variant selected like a shopper")
        assert k["ok"] and "radio Small" in k["actual"]


def test_password_store_is_blocked_not_failed(tmp_path):
    run, _ = _scan("password", tmp_path)
    assert run.verdict == "blocked" and run.cases == []
    assert run.sitemap_summary["access"] == "password"


def test_age_gate_is_never_confirmed(tmp_path):
    run, _ = _scan("age_gate", tmp_path)
    j = _case(run, "journey.")
    assert j.verdict == "blocked" and "does not confirm age" in j.attempts[-1].error
    assert run.verdict in ("blocked", "degraded")


def test_theme_and_checkout_detected(tmp_path):
    run, _ = _scan("healthy", tmp_path, allow_cart_flow=False)
    assert run.sitemap_summary["theme"] == "Dawn" and run.sitemap_summary["checkout_app"] == "Shopify checkout"


def test_bench_runs_many_stores_into_one_table(tmp_path):
    from radar.bench import run_bench
    a, url_a = serve("healthy")
    b, url_b = serve("password")
    c, url_c = serve("wrong_variant")
    try:
        out = run_bench([(url_a, True), (url_b, False), (url_c, True)],
                        _settings(tmp_path, max_products=1, max_collections=1, max_nav_links=2), workers=3,
                        progress=lambda *_: None)
    finally:
        for s in (a, b, c):
            s.shutdown()
    data = json.loads((out / "bench.json").read_text())
    rows = {r["input"]: r for r in data["rows"]}
    assert [r["input"] for r in data["rows"]] == [url_a, url_b, url_c]      # input order kept
    assert rows[url_a]["verdict"] == "healthy" and rows[url_a]["suites"]["journey"] == "pass"
    assert rows[url_b]["verdict"] == "blocked"
    assert rows[url_c]["verdict"] == "down" and rows[url_c]["suites"]["cart"] == "confirmed_fail"
    assert any("Wooden Spoon Set" in f["error"] for f in rows[url_c]["failures"])
    assert (out / "bench.html").exists() and rows[url_a]["report_rel"].endswith("report.html")


def _steps(case):
    return {s.name: s for s in case.attempts[-1].steps}


def test_hostile_theme_everything_that_broke_on_the_bench(tmp_path):
    """Hidden mega menu, dropdown-only collections, disabled menu parent, off-screen card, chat bubble,
    iframe popup, short display title, /search disallowed: Radar must still pass, for the right reasons."""
    run, _ = _scan("hostile", tmp_path)
    assert run.verdict == "healthy", [(c.case_id, c.attempts[-1].error) for c in run.cases if c.verdict != "pass"]
    j = _case(run, "journey.")
    st = _steps(j)
    assert "opened a menu, then link" in st["click_into_collection"].detail      # dropdown, not the disabled link
    assert "/collections/kitchen" in st["click_into_collection"].detail
    assert any(k["what"].startswith("popup (iframe)") and "'×'" in k["actual"] for k in _checks(j))
    assert all("hidden-" not in n for n in [c.case_id for c in run.cases])
    pdp = _steps(_case(run, "product.pdp.ceramic-vase"))
    assert pdp["shows_title_price_image"].status == "pass"                      # a title IS shown
    assert pdp["title_matches_catalog"].status == "warn" and "Handmade Pot" in pdp["title_matches_catalog"].error
    s = _steps(_case(run, "search."))
    assert "typed into the store's search box" in s["returns_relevant_products"].detail


def test_store_without_collection_links_goes_home_to_product(tmp_path):
    run, _ = _scan("no_collection_links", tmp_path)
    j = _case(run, "journey.")
    st = _steps(j)
    assert j.verdict == "pass", j.attempts[-1].error
    assert st["click_into_collection"].status == "warn" and st["click_into_product"].status == "pass"
    assert st["cart_received_this_product"].status == "pass"


def test_product_name_found_wherever_the_theme_puts_it(tmp_path):
    """Bench 2 false failures (Publisher h2 title, title in the theme's own <header>, body class 'card...')."""
    run, _ = _scan("title_layouts", tmp_path, allow_cart_flow=False)
    for c in run.cases:
        if c.suite in ("product", "journey"):
            assert c.verdict == "pass", (c.case_id, c.attempts[-1].error)
    pdp = {c.case_id: c for c in run.cases if c.suite == "product"}
    vase = next(c for k, c in pdp.items() if "ceramic" in k)
    got = next(ch for s in vase.attempts[-1].steps for ch in s.checks if ch["what"] == "product name shown on the page")
    assert "h2" in got["actual"] and "exact name" in got["actual"], got     # not the visually-hidden button label
    spoons = next(c for k, c in pdp.items() if "spoon" in k)
    got = next(ch for s in spoons.attempts[-1].steps for ch in s.checks if ch["what"] == "product name shown on the page")
    assert got["ok"] and "Wooden Spoon Set" in got["actual"], got


def test_product_page_without_a_name_fails(tmp_path):
    run, _ = _scan("no_title", tmp_path, allow_cart_flow=False)
    bad = [c for c in run.cases if c.suite == "product" and c.verdict == "confirmed_fail"]
    assert bad, [(c.case_id, c.verdict) for c in run.cases]
    assert all(c.attempts[-1].failed_step == "shows_title_price_image" for c in bad)
    assert "none" in bad[0].attempts[-1].error


def _on_page(mode, tmp_path, path, fn):
    """Open one page of the mock store in Radar's real browser session and run fn(ctx)."""
    from radar.core.browser import Browser
    from radar.checks.library import Ctx, Steps, _load
    srv, url = serve(mode)
    try:
        s = _settings(tmp_path)
        with Browser(s, "desktop") as b:
            with b.attempt(tmp_path, "probe") as sess:
                ctx = Ctx(sess, None, Steps())
                _load(ctx, url + path)
                return fn(ctx)
    finally:
        srv.shutdown()


def test_card_link_covered_in_the_middle_is_clicked_where_it_is_free(tmp_path):
    """theme-spotlight (bench 2): a layer covers the card link's centre. Playwright's own click hits the
    centre and is intercepted; Radar must click at the point it hit-tested and open the product."""
    from radar.checks.library import _pick, _click_picked, PRODUCT_HREF, _handle
    def go(ctx):
        got = _pick(ctx, "a[href]", PRODUCT_HREF, ["/products/ceramic-vase"])
        assert not got.get("none") and got["via"] == "link", got
        _click_picked(ctx, got)
        return _handle(ctx.sess.page.url)
    assert _on_page("card_layouts", tmp_path, "/collections/home-decor", go) == "ceramic-vase"


def test_card_link_under_an_image_slider_is_clicked_through_its_card(tmp_path):
    """An image slider sits over the whole card link and opens the product itself (bummer.in pattern)."""
    from radar.checks.library import _pick, _click_picked, PRODUCT_HREF, _handle
    def go(ctx):
        got = _pick(ctx, "a[href]", PRODUCT_HREF, ["/products/wooden-spoon-set"])
        assert not got.get("none") and got["via"] == "card", got
        _click_picked(ctx, got)
        return _handle(ctx.sess.page.url)
    assert _on_page("card_layouts", tmp_path, "/", go) == "wooden-spoon-set"


def test_journey_passes_on_real_card_layouts(tmp_path):
    run, _ = _scan("card_layouts", tmp_path)
    j = _case(run, "journey.")
    assert j.verdict == "pass", j.attempts[-1].error


def test_no_card_found_says_why(tmp_path):
    from radar.checks.library import _pick, _why_none, PRODUCT_HREF
    def go(ctx):
        ctx.sess.evaluate("() => document.querySelectorAll('a.card').forEach(a => a.setAttribute('aria-hidden', 'true'))")
        return _why_none(_pick(ctx, "a[href]", PRODUCT_HREF, [], second_pass=False), "/")
    msg = _on_page("healthy", tmp_path, "/", go)
    assert msg.startswith("none on / (") and "hidden from shoppers" in msg, msg


class FakeLLM:
    """Stands in for gpt-4o-mini: answers by which prompt it gets, records what it was sent."""
    def __init__(self, triage_verdict="radar_problem", category="locator"):
        self.triage_verdict, self.category, self.seen = triage_verdict, category, []

    def __call__(self, url, headers, body):
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if p.get("type") == "text")
        self.seen.append({"system": system, "text": text, "images": 0 if isinstance(user, str) else
                          sum(p.get("type") == "image_url" for p in user)})
        if "review FAILED" in system:
            ans = {"verdict": self.triage_verdict, "category": self.category, "reason": "fake triage", "evidence": "fake"}
        elif "read a product page" in system:
            m = re.search(r"\[(\d+)\] <\w+> \d+px 'Handmade Vase in Clay'", text)
            ans = {"index": int(m.group(1)) if m else None}
        elif "popup or overlay" in system:
            m = re.search(r"\[(\d+)\] <button> \"I'll pass\"", text)
            ans = {"index": int(m.group(1)) if m else None}
        else:
            ans = {"index": None, "confidence": 0}
        return {"choices": [{"message": {"content": json.dumps(ans)}}], "usage": {"prompt_tokens": 900, "completion_tokens": 30}}


def _fake_llm(**kw):
    f = FakeLLM(**kw)
    return f, LLMClient("openai", "gpt-4o-mini", 12, base_url="https://api.openai.com/v1", api_key="test", transport=f)


def test_triage_blames_radar_then_llm_finds_renamed_title_and_code_verifies(tmp_path):
    fake, llm = _fake_llm()
    run, d = _scan("renamed_title", tmp_path, llm=llm, allow_cart_flow=False)
    vase = _case(run, "product.pdp.ceramic")
    assert vase.verdict == "pass" and vase.healed_after_triage, (vase.verdict, vase.attempts[-1].error)
    assert vase.triage["verdict"] == "radar_problem"
    assert [a.ok for a in vase.attempts] == [False, False, True] and vase.attempts[-1].note.startswith("re-check")
    got = next(c for s in vase.attempts[-1].steps for c in s.checks if c["what"] == "product name shown on the page")
    assert "Handmade Vase in Clay" in got["actual"] and "LLM help, verified" in got["actual"]
    tri = next(x for x in fake.seen if "review FAILED" in x["system"])
    assert tri["images"] == 1 and "product name shown on the page" in tri["text"]    # saw the screenshot + assertions
    assert run.llm_usage["calls"] == len(fake.seen) and run.llm_usage["est_usd"] > 0


def test_without_llm_the_renamed_title_stays_a_failure(tmp_path):
    run, _ = _scan("renamed_title", tmp_path, allow_cart_flow=False)
    vase = _case(run, "product.pdp.ceramic")
    assert vase.verdict == "confirmed_fail" and vase.triage is None


def test_triage_confirms_real_store_problem_and_no_recheck(tmp_path):
    fake, llm = _fake_llm(triage_verdict="real_store_problem", category="store_bug")
    run, _ = _scan("cart_broken", tmp_path, llm=llm)
    cart = _case(run, "cart.")
    assert cart.verdict == "confirmed_fail" and cart.triage["verdict"] == "real_store_problem"
    assert len(cart.attempts) == 2 and not cart.healed_after_triage          # no re-check for real problems
    assert run.verdict == "down"


def test_unknown_overlay_closed_by_llm_pick_never_by_the_tempting_button(tmp_path):
    fake, llm = _fake_llm(category="popup_or_overlay")
    srv, url = serve("unknown_overlay")
    try:
        run, _ = scan(url, _settings(tmp_path), llm=llm, only_suites=["journey"])
    finally:
        srv.shutdown()
    j = _case(run, "journey.")
    assert j.verdict == "pass" and j.healed_after_triage, [(a.ok, a.error) for a in j.attempts]
    assert any("popup or overlay" in x["system"] for x in fake.seen)
    shots = [s.shot for s in j.attempts[-1].steps if s.shot]
    assert len(shots) >= 5                                      # a picture after every journey step


# ---------- bench 3 (5 Oct): each real-store cause as a mock mode ----------
def _journey_only(mode, tmp_path, **kw):
    srv, url = serve(mode)
    try:
        return scan(url, _settings(tmp_path, **kw), only_suites=["journey"])[0]
    finally:
        srv.shutdown()


def test_hover_mega_menu_is_opened_by_hovering(tmp_path):
    j = _case(_journey_only("hover_menu", tmp_path), "journey.")
    assert j.verdict == "pass", j.attempts[-1].error
    assert "hovered the 'Shop' menu" in _steps(j)["click_into_collection"].detail


def test_brand_landing_homepage_is_crossed_in_one_hop(tmp_path):
    j = _case(_journey_only("brand_landing", tmp_path), "journey.")
    assert j.verdict == "pass", j.attempts[-1].error
    assert "went via 'Shop' (/pages/shop)" in _steps(j)["click_into_collection"].detail


def test_grid_rerendered_under_the_click_is_clicked_again(tmp_path):
    j = _case(_journey_only("rerender_grid", tmp_path), "journey.")
    assert j.verdict == "pass", [(a.ok, a.error) for a in j.attempts]
    assert "clicked again" in _steps(j)["click_into_product"].detail


def _product_only(mode, tmp_path, **kw):
    srv, url = serve(mode)
    try:
        return scan(url, _settings(tmp_path, allow_cart_flow=False, **kw), only_suites=["product"])[0]
    finally:
        srv.shutdown()


def test_icon_only_popup_is_closed_never_the_promo_button(tmp_path):
    run = _product_only("icon_popup", tmp_path)
    assert all(c.verdict == "pass" for c in run.cases), [(c.case_id, c.attempts[-1].error) for c in run.cases]


def test_price_only_in_a_sticky_bar_is_found_after_scrolling(tmp_path):
    run = _product_only("sticky_price", tmp_path)
    vase = _case(run, "product.pdp.ceramic")
    assert vase.verdict == "pass", vase.attempts[-1].error


def test_catalog_product_hidden_from_shoppers_is_a_warning_and_the_next_product_is_tested(tmp_path):
    for mode, words in (("hidden_product", "redirects to / (in the store's catalog but hidden from shoppers)"),
                        ("notfound_product", "but the page does not exist"),
                        ("unavailable_product", "currently unavailable"),           # plumgoodness.com, bench 4
                        ("home_at_product_url", "page shows other content")):      # foxtale.in (headless), bench 4
        run = _product_only(mode, tmp_path / mode, max_products=1)
        vase = _case(run, "product.pdp.ceramic")
        assert vase.verdict == "pass", (mode, vase.attempts[-1].error)
        st = _steps(vase)
        assert st["loads"].status == "warn" and words in st["loads"].error, (mode, st["loads"].error)
        assert st["loads_next_catalog_product_1"].status == "pass"
        assert "wooden-spoon-set" in st["product_identified"].detail.lower() or "Wooden Spoon" in st["product_identified"].detail


def test_product_page_without_its_own_buy_form_says_so(tmp_path):
    run = _product_only("no_buy_form", tmp_path)
    vase = _case(run, "product.pdp.ceramic")
    assert vase.verdict == "confirmed_fail"
    assert "no add-to-cart form for this product" in vase.attempts[-1].error
    assert "belong to other products' cards" in vase.attempts[-1].error


def test_main_buy_button_in_a_quick_add_named_container_behind_a_popup(tmp_path):
    """soulflower.in (bench 3): the product's OWN button lives in a 'quick-add-container' with no /cart/add form,
    and an icon-only popup covers the page. Old rule excluded it by class name."""
    srv, url = serve("quick_named_main")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["product", "cart"])[0]
    finally:
        srv.shutdown()
    assert all(c.verdict == "pass" for c in run.cases), [(c.case_id, c.attempts[-1].error) for c in run.cases]
    buy = next(s for c in run.cases if c.suite == "product" for s in c.attempts[-1].steps if s.name == "buy_button_ready")
    assert "form.cart-form" in str(buy.detail), buy.detail
    cart = _case(run, "cart.")
    got = next(ch for s in cart.attempts[-1].steps for ch in s.checks if ch["what"] == "product added to cart")
    assert got["ok"], got


def test_search_word_from_a_hidden_product_falls_back_to_a_second_word(tmp_path):
    srv, url = serve("search_misses")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["search"])[0]
    finally:
        srv.shutdown()
    c = _case(run, "search.")
    assert c.verdict == "pass", c.attempts[-1].error
    st = _steps(c)
    assert st["returns_relevant_products"].status == "warn" and "ceramic" in st["returns_relevant_products"].error
    assert st["returns_relevant_products_other_word"].status == "pass"


@pytest.mark.parametrize("mode", ["drawer_form_first", "upsell_forms_first"])
def test_hidden_drawer_and_upsell_forms_never_taken_for_the_product_form(tmp_path, mode):
    """boldcare.in + bummer.in (bench 4): hidden cart-drawer / upsell forms named product_form / shopify-product-form
    come first in the DOM, one with ANOTHER product's variant (boldcare), others with an empty id (bummer). v0.8 read
    them as the product's form: 'expected 201, got 301' / 'got  after choosing radio Small'. The own form decides."""
    run = _product_only(mode, tmp_path)
    vase = _case(run, "product.pdp.ceramic")
    assert vase.verdict == "pass", vase.attempts[-1].error
    checks = {c["what"]: c for s in vase.attempts[-1].steps for c in s.checks}
    assert checks["variant selected like a shopper"]["ok"] and "radio Small" in checks["variant selected like a shopper"]["actual"]
    run, _ = _scan(mode, tmp_path / "cart")
    cart = _case(run, "cart.")
    assert cart.verdict == "pass", cart.attempts[-1].error


def test_price_only_in_an_element_hidden_on_this_screen_fails_and_says_where(tmp_path):
    """supplysix.com (bench 4): on desktop the only price is in a mobile-only sticky bar. A shopper sees no
    price, so the test fails; the message says the price is only in a hidden element (not 'Radar looked wrong')."""
    run = _product_only("price_desktop_hidden", tmp_path, max_products=1)
    vase = _case(run, "product.pdp.ceramic")
    assert vase.verdict == "confirmed_fail"
    err = vase.attempts[-1].error
    assert "selected variant price shown" in err and "only inside a hidden element" in err and "hidden-lap-and-up" in err, err


def test_store_refusing_radar_is_blocked_with_the_reason_not_unsupported(tmp_path):
    """plumgoodness.com (bench 5): HTTP 423 'This store is unavailable' to Radar. v0.9 said 'unsupported' (not Shopify)."""
    run, _ = _scan("store_refuses", tmp_path)
    assert run.verdict == "blocked", run.verdict
    assert any("HTTP 423" in n and "This store is unavailable" in n for n in run.notes), run.notes


def test_card_clicked_where_it_is_after_the_page_relays_out(tmp_path):
    """boat-lifestyle.com (bench 5): the page re-lays out ~20 ms after Radar scrolls a card into view; v0.9 clicked
    the old point and opened the card above/below. Every attempt must open the product that was picked."""
    run = scan_journey = None
    srv, url = serve("shift_after_scroll")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["journey"])[0]
    finally:
        srv.shutdown()
    j = _case(run, "journey.")
    assert j.verdict == "pass", [(a.n, a.error) for a in j.attempts]
    assert len(j.attempts) == 1, [(a.n, a.error) for a in j.attempts]


def test_every_request_says_bugradar_with_a_normal_chrome_name(tmp_path):
    """6 Oct: pages AND Radar's own data requests (/products/x.js, /cart.js) carry the Chrome name + BugRadar;
    never 'HeadlessChrome', never without BugRadar. --plain-ua sends BugRadar only."""
    from tests.mockstore.server import Handler
    Handler.SEEN_UA.clear()
    _product_only("healthy", tmp_path / "browser", max_products=1)
    seen = {u for u in Handler.SEEN_UA if u}
    assert seen and all(u.endswith("BugRadar/0.1 (+bugradar.in)") and "Chrome/" in u and "HeadlessChrome" not in u
                        for u in seen), seen
    Handler.SEEN_UA.clear()
    _product_only("healthy", tmp_path / "plain", max_products=1, ua_style="plain")
    assert {u for u in Handler.SEEN_UA if u} == {"BugRadar/0.1 (+bugradar.in)"}, Handler.SEEN_UA


# ---------- bench 8 (6 Oct): Radar's OWN network dropped mid-bench ----------
DEAD = ("http://127.0.0.1:9/",)          # nothing listens: the probe fails = Radar offline


def _scan_net_drop(tmp_path, probes):
    """The store answers the journey, then goes silent (connection refused), like every store did when the Mac's
    Wi-Fi dropped at 14:51 IST in bench 8."""
    from radar.core.network import reset_cache
    reset_cache()
    srv, url = serve("healthy")
    n = {"a": 0}

    def hook(evt, d):
        if evt == "attempt":
            n["a"] += 1
            if n["a"] == 1:
                srv.shutdown()
                srv.server_close()
    try:
        return scan(url, _settings(tmp_path, net_probe_urls=probes), progress=hook)
    finally:
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass


def test_radar_losing_its_own_network_never_blames_the_store(tmp_path):
    """v0.12 called the store DOWN with 10 confirmed failures (reproduced on the v0.12 copy before the fix)."""
    from radar.core.storage import Storage
    run, d = _scan_net_drop(tmp_path, DEAD)
    assert run.verdict == "no_network", (run.verdict, [(c.case_id, c.verdict) for c in run.cases])
    assert not any(c.verdict == "confirmed_fail" for c in run.cases)
    assert _case(run, "journey.").verdict == "pass"                     # what ran before the drop still counts
    assert run.cases[-1].verdict == "no_network"                         # the run stopped at the first lost test
    assert any("Radar lost its OWN internet connection" in n and "tests not run" in n for n in run.notes), run.notes
    assert Storage(tmp_path).incidents(run.site_id) == []               # no incident opened against the store
    assert json.loads((d / "run.json").read_text())["verdict"] == "no_network"


def test_store_dying_while_radar_is_online_is_still_down(tmp_path):
    """The other half of the rule: if Radar IS online, a store that stops answering is a real outage."""
    probe, purl = serve("healthy")
    try:
        run, _ = _scan_net_drop(tmp_path, (purl + "/",))
    finally:
        probe.shutdown()
    assert run.verdict == "down", run.verdict
    assert any(c.verdict == "confirmed_fail" for c in run.cases)


def test_homepage_unreachable_says_radar_offline_or_store_unreachable(tmp_path):
    from radar.core.network import reset_cache
    reset_cache()
    dead_store = "http://127.0.0.1:9"
    run, _ = scan(dead_store, _settings(tmp_path / "off", net_probe_urls=DEAD))
    assert run.verdict == "no_network", (run.verdict, run.notes)
    assert not any("robots.txt not readable" in n for n in run.notes)
    probe, purl = serve("healthy")
    try:
        run2, _ = scan(dead_store, _settings(tmp_path / "on", net_probe_urls=(purl + "/",)))
    finally:
        probe.shutdown()
    # Radar online, store silent: robots.txt has no answer -> RFC 9309 'do not crawl' -> BLOCKED with the reason
    assert run2.verdict == "blocked", (run2.verdict, run2.notes)
    assert any("RFC 9309" in n for n in run2.notes), run2.notes


def test_robots_txt_server_error_means_do_not_crawl(tmp_path):
    """RFC 9309: robots.txt 5xx / no answer = complete disallow. v0.12 treated it as allow-all."""
    run, _ = _scan("robots_500", tmp_path)
    assert run.verdict == "blocked" and run.sitemap_summary["access"] == "robots_unreachable", run.verdict
    assert run.cases == []
    assert any("HTTP 500" in n and "RFC 9309" in n for n in run.notes), run.notes


def test_missing_robots_txt_allows_everything(tmp_path):
    run, _ = _scan("robots_404", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict == "healthy", [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases]
    assert any("no robots.txt" in n for n in run.notes), run.notes


def test_bench_does_not_start_stores_while_radar_is_offline(tmp_path):
    from radar.bench import run_bench
    a, url_a = serve("healthy")
    said = []
    try:
        out = run_bench([(url_a, True)], _settings(tmp_path, net_probe_urls=DEAD), workers=1, progress=said.append)
    finally:
        a.shutdown()
    data = json.loads((out / "bench.json").read_text())
    assert data["rows"][0]["verdict"] == "no_network" and data["totals"]["tested"] == 0
    assert data["totals"]["no_network"] == 1
    assert any("no internet connection" in s for s in said), said
    assert "Radar offline" in (out / "bench.html").read_text()


def test_popup_inside_shadow_dom_is_closed_by_its_x_and_the_journey_passes_first_try(tmp_path):
    """soulflower.in (bench 9): the YourLio promo popup renders inside #chat-widget's shadow root and appears after the
    collection page loads; v0.13 could not see it, the click on the product card was intercepted -> FLAKY journey."""
    srv, url = serve("shadow_popup")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["journey"])[0]
    finally:
        srv.shutdown()
    j = _case(run, "journey.")
    assert j.verdict == "pass" and len(j.attempts) == 1, [(a.n, a.error) for a in j.attempts]
    closed = [c for s in j.attempts[0].steps for c in s.checks if c["what"] == "popup popup closed"]
    assert closed and closed[0]["actual"].startswith("clicked 'close' on:") and "bomb size" in closed[0]["actual"], closed


def test_dead_product_image_link_the_name_opens_it_journey_passes_with_a_store_warning(tmp_path):
    """thefunclab.com (bench 10): on the homepage slider the product IMAGE link does nothing when clicked (the theme's
    script cancels mousedown/click); the product NAME opens it. v0.14 clicked the image twice and failed the journey
    (FLAKY: the retry happened to go via a collection grid). A shopper clicks the name: journey passes, the dead link
    is a store warning with the evidence."""
    srv, url = serve("dead_image_link")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["journey"])[0]
    finally:
        srv.shutdown()
    j = _case(run, "journey.")
    assert j.verdict == "pass" and len(j.attempts) == 1, [(a.n, a.error) for a in j.attempts]
    st = _steps(j)
    assert "product name opened it" in st["click_into_product"].detail, st["click_into_product"].detail
    w = st["every_product_link_opens_the_product"]
    assert w.status == "warn" and "IMAGE link" in w.error and "did nothing when clicked" in w.error, (w.status, w.error)


# ---------- bench 11 (7 Oct): transient misses on two stores each ----------
def test_store_data_answering_empty_once_is_retried_not_a_failure(tmp_path):
    """palmonas.com: /products/x.js answered an empty body once (JSON error); wellbeingnutrition.com: a timeout once.
    Both made the journey FLAKY in v0.15. Radar now retries the data request: every case passes on its first attempt."""
    run, _ = _scan("flaky_data", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict == "healthy", [(c.case_id, c.verdict, [a.error for a in c.attempts]) for c in run.cases]
    assert all(len(c.attempts) == 1 for c in run.cases), [(c.case_id, len(c.attempts)) for c in run.cases]


def test_robots_txt_failing_twice_then_answering_is_read_on_the_third_try(tmp_path):
    """soulflower.in / bummer.in: robots.txt gave no answer on two quick tries, so v0.15 blocked the whole store."""
    run, _ = _scan("robots_500_twice", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict == "healthy", (run.verdict, run.notes)
    assert run.sitemap_summary["robots_loaded"] is True
