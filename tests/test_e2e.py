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


def test_renamed_button_without_a_form_is_found_by_what_names_this_product(tmp_path):
    """The buy button lost name="add", sits outside any form and says "Add to Bag", but carries data-vid = this
    product's variant. Until v0.16 only heuristic healing found it; since v0.17 (held-out run, nicobar.com) a control
    that NAMES this product is taken directly, so no guess and no healing is needed. Healing itself is still proven by
    the obscure-button tests. The cart check proves the right product was added."""
    run, _ = _scan("renamed_button", tmp_path)
    assert run.verdict == "healthy", [(c.case_id, c.attempts[-1].error) for c in run.cases if c.verdict != "pass"]
    st = _steps(_case(run, "product.pdp."))
    assert "data-vid names this product" in st["buy_button_ready"].detail, st["buy_button_ready"].detail
    cart = _steps(_case(run, "cart."))
    added = [c for s in cart.values() for c in s.checks if c["what"] == "product added to cart"]
    assert added and all(c["ok"] for c in added), added


def test_broken_structured_price_is_an_seo_note_not_a_failure(tmp_path):
    """Structured price 0 while the page shows the right price (wellbeingnutrition.com JSON-LD price null, 9 Oct):
    a Google Shopping / SEO finding, shown as a warning on the product test; the store stays healthy."""
    run, d = _scan("broken_price", tmp_path)
    case = _case(run, "product.pdp.ceramic")
    assert case.verdict == "pass" and len(case.attempts) == 1
    st = _steps(case)
    assert st["structured_data_valid"].status == "warn" and "structured data: price" in (st["structured_data_valid"].error or "")
    assert not case.incident_signature and run.verdict == "healthy"


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


@pytest.mark.parametrize("mode", ["search_app", "search_app_popular"])
def test_search_app_that_renders_results_late_is_waited_for(tmp_path, mode):
    """bonkerscorner.com + baccabucci.com (empty search page at 0.8 s) and bellavitaorganic.com (placeholder 'popular'
    products) in the new30b held-out run, 9 Oct: the store's search app renders the real results seconds later."""
    srv, url = serve(mode)
    try:
        run = scan(url, _settings(tmp_path), only_suites=["search"])[0]
    finally:
        srv.shutdown()
    c = _case(run, "search.")
    assert c.verdict == "pass", c.attempts[-1].error
    st = _steps(c)
    assert st["returns_relevant_products"].status == "pass", st["returns_relevant_products"].error
    assert "search app" in st["returns_relevant_products"].detail


def test_search_app_that_renders_only_after_a_scroll_is_scrolled(tmp_path):
    """ptron.in desktop (new30c held-out run, 10 Oct): title 'Search: 513 results found for "sonor"', the search app's
    results area stayed a grey block for 10 s. A shopper scrolls; Radar now scrolls the results area and waits again."""
    srv, url = serve("search_app_scroll")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["search"])[0]
    finally:
        srv.shutdown()
    c = _case(run, "search.")
    assert c.verdict == "pass", c.attempts[-1].error
    st = _steps(c)
    assert st["returns_relevant_products"].status == "pass", st["returns_relevant_products"].error
    assert "scroll" in st["returns_relevant_products"].detail


def test_search_app_that_never_renders_is_a_warning_when_shopify_search_finds_the_word(tmp_path):
    """ptron.in (both devices) + kushals.com mobile (new30c held-out run, 10 Oct): Shopify's own count in the title
    ('1000 results found for "zircon"'), but the search app never filled the results area for Radar's browser. Shopify's
    own search (/search/suggest.json) finds the word, so search works on the store; what Radar cannot prove is the
    app's rendering: a WARNING with that evidence, not a failure."""
    srv, url = serve("search_app_never")
    try:
        run = scan(url, _settings(tmp_path), only_suites=["search"])[0]
    finally:
        srv.shutdown()
    c = _case(run, "search.")
    assert c.verdict != "confirmed_fail" and c.verdict != "fail", c.attempts[-1].error
    steps = [s for a in c.attempts for s in a.steps]
    warn = [s for s in steps if s.status == "warn" and "did not render" in (s.error or "")]
    assert warn, [(s.name, s.status, s.error) for s in steps]
    assert "suggest.json" in warn[0].error or "Shopify's own search" in warn[0].error
    assert not [s for s in steps if s.status == "fail"], [(s.name, s.error) for s in steps]


def test_theme_scripts_delayed_until_the_first_interaction_run(tmp_path):
    """baccabucci.com + bellavitaorganic.com (new30b and the 9 Oct re-run): a speed app holds every script until the
    shopper first moves / touches / scrolls; Radar never did, so html stayed 'no-js', the price stayed hidden and the
    search app never rendered. A shopper moves the mouse; so does Radar, then the page is read."""
    from radar.runner.executor import scan_devices
    srv, url = serve("delayed_scripts")
    try:
        runs = [r for r, _ in scan_devices(url, _settings(tmp_path), only_suites=["product", "search"])]
    finally:
        srv.shutdown()
    assert [r.device for r in runs] == ["desktop", "mobile"]
    bad = [(r.device, c.case_id, c.attempts[-1].error) for r in runs for c in r.cases if c.verdict != "pass"]
    assert not bad, bad
    assert all({c.suite for c in r.cases} == {"product", "search"} for r in runs)


def test_store_that_holds_scripts_for_linux_pagespeed_bots_shows_the_emulated_shopper_the_full_page(tmp_path):
    """bonkerscorner.com, bellavitaorganic.com, baccabucci.com (new30b + re-runs, 9 Oct): a speed snippet treats any
    browser reporting navigator.platform 'Linux x86_64' as Google PageSpeed and never runs the theme's scripts: no
    price, no search results. GitHub's machines are Linux, and Playwright's device emulation left the platform at
    'Linux x86_64' even for the Pixel 7. Radar now reports the platform of the device it emulates (desktop: Windows
    Chrome, mobile: Pixel 7 Android), the same as the User-Agent it sends (which still names BugRadar)."""
    from radar.runner.executor import scan_devices
    srv, url = serve("pagespeed_gate")
    try:
        runs = [r for r, _ in scan_devices(url, _settings(tmp_path), only_suites=["product", "search"])]
    finally:
        srv.shutdown()
    assert [r.device for r in runs] == ["desktop", "mobile"]
    bad = [(r.device, c.case_id, c.attempts[-1].error) for r in runs for c in r.cases if c.verdict != "pass"]
    assert not bad, bad
    assert all({c.suite for c in r.cases} == {"product", "search"} for r in runs)


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


# ---------- held-out run (7 Oct, 30 never-seen stores): buy controls a shopper uses but Radar missed ----------
def test_buy_button_shown_only_in_a_sticky_bar_after_scrolling_is_found(tmp_path):
    """true-elements.com: the product form's own button is hidden on desktop; 'ADD TO CART' appears in a sticky bar
    once the shopper scrolls. v0.16 failed all 3 products ('could not find add_to_cart')."""
    run = _product_only("sticky_buy_only", tmp_path, max_products=2)
    assert all(c.verdict == "pass" for c in run.cases), [(c.case_id, c.attempts[-1].error) for c in run.cases]
    st = _steps(_case(run, "product.pdp."))
    assert "sticky bar" in st["buy_button_ready"].detail, st["buy_button_ready"].detail


def test_div_buy_control_named_for_this_product_is_found_and_adds_the_right_product(tmp_path):
    """nicobar.com: the buy control is a <div data-product-handle=...> 'ADD TO BAG', no Shopify cart form, and other
    products' 'Add to Bag' buttons come first on the page. v0.16 failed all 3 products. The cart test proves that
    clicking it adds THIS product, not a recommended one."""
    run, _ = _scan("div_buy_control", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    bad = [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases if c.verdict != "pass"]
    assert not bad, bad
    st = _steps(_case(run, "product.pdp."))
    assert "pdp-addtobag-btn" in st["buy_button_ready"].detail and "data-product-handle" in st["buy_button_ready"].detail, \
        st["buy_button_ready"].detail


# ---------- desktop + mobile in every run (7 Oct) ----------
def _both(mode, tmp_path, **kw):
    from radar.runner.executor import scan_devices
    srv, url = serve(mode)
    try:
        return scan_devices(url, _settings(tmp_path, **kw))
    finally:
        srv.shutdown()


def test_every_scan_tests_desktop_and_mobile_and_mobile_is_a_real_phone(tmp_path):
    results = _both("healthy", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert [r.device for r, _ in results] == ["desktop", "mobile"]
    assert all(r.verdict == "healthy" for r, _ in results), [(r.device, r.verdict) for r, _ in results]
    assert len({d for _, d in results}) == 2 and all((d / "report.html").exists() for _, d in results)
    assert [json.loads((d / "run.json").read_text())["device"] for _, d in results] == ["desktop", "mobile"]
    # the mobile run really was a phone: touch, small viewport, mobile User-Agent with Radar's name still in it
    from radar.core.browser import Browser
    with Browser(_settings(tmp_path), "mobile") as b:
        ctx = b.new_context()
        page = ctx.new_page()
        w, touch = page.evaluate("[screen.width, 'ontouchstart' in window || navigator.maxTouchPoints > 0]")
        assert w == 412 and touch                  # (innerWidth on a blank page is 980: no viewport meta tag yet)
        assert "Mobile" in page.evaluate("navigator.userAgent") and "BugRadar/0.1" in page.evaluate("navigator.userAgent")
        ctx.close()


def test_price_shown_only_on_phones_fails_on_desktop_and_passes_on_mobile(tmp_path):
    """supplysix.com (bench 4): the trial page's only price sits in a bar the theme shows on phones only."""
    results = _both("price_desktop_hidden", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1,
                    max_nav_links=2)
    by = {r.device: r for r, _ in results}
    assert [r.device for r, _ in results] == ["desktop", "mobile"]            # a failing desktop does not stop mobile
    assert _case(by["desktop"], "product.pdp.ceramic").verdict == "confirmed_fail"
    assert _case(by["mobile"], "product.pdp.ceramic").verdict == "pass", _case(by["mobile"], "product.pdp.ceramic").attempts[-1].error
    # and the incident is the desktop one only: a passing mobile run does not close it
    from radar.core.storage import Storage
    inc = Storage(tmp_path).incidents("127.0.0.1_" + results[0][0].base_url.rsplit(":", 1)[1])
    assert inc and {i["device"] for i in inc} == {"desktop"} and all(i["status"] == "open" for i in inc)


def test_mobile_is_not_asked_when_desktop_could_not_test_the_store(tmp_path):
    from radar.runner.executor import scan_devices
    events = []
    srv, url = serve("password")
    try:
        results = scan_devices(url, _settings(tmp_path), progress=lambda e, d: events.append((e, d)))
    finally:
        srv.shutdown()
    assert [r.device for r, _ in results] == ["desktop"] and results[0][0].verdict == "blocked"
    skipped = [d for e, d in events if e == "device_skipped"]
    assert skipped and skipped[0]["device"] == "mobile" and "not asked again" in skipped[0]["why"]


def test_console_errors_failed_requests_and_load_times_are_captured_but_never_fail_a_test(tmp_path):
    run, d = _scan("noisy_console", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict == "healthy", [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases]   # noise is evidence only
    health = _case(run, "health.").attempts[-1]
    texts = [c["text"] for c in health.console]
    assert any("chat widget failed" in t for t in texts) and any(c["type"] == "warning" for c in health.console)
    assert any("127.0.0.1:1/never-answers" in r for r in health.failed_requests)
    assert health.loads and all(l["load"] is not None and l["url"].startswith("/") for l in health.loads)
    j = _case(run, "journey.").attempts[-1]
    assert len(j.loads) >= 3, j.loads                    # home, collection, product (and cart): every page the shopper landed on
    assert len({l["url"] for l in j.loads}) == len(j.loads)           # one entry per page document
    p = run.perf
    assert p["pages"] >= 3 and p["console_errors"] >= 1 and p["console_warnings"] >= 1 and p["failed_requests"] >= 1
    saved = json.loads((d / "run.json").read_text())
    assert saved["perf"]["pages"] == p["pages"] and saved["cases"][0]["attempts"][-1]["loads"]
    html = (d / "report.html").read_text()
    assert "Page timing" in html and "evidencePanel" in html


def test_radars_own_not_found_probe_is_never_shown_as_store_console_noise(tmp_path):
    run, _ = _scan("healthy", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    nf = _case(run, "health.not_found").attempts[-1]
    assert nf.ok and not any("bugradar-check" in e["url"] or "bugradar-check" in e["text"] for e in nf.console)
    assert run.perf["pages"] >= 3 and run.perf["median_load_secs"] is not None


def test_bench_runs_both_devices_per_store_and_one_row_says_where_it_fails(tmp_path):
    from radar.bench import run_bench
    a, url_a = serve("healthy")
    b, url_b = serve("price_desktop_hidden")
    c, url_c = serve("password")
    try:
        out = run_bench([(url_a, False), (url_b, False), (url_c, False)],
                        _settings(tmp_path, max_products=1, max_collections=1, max_nav_links=2), workers=3,
                        progress=lambda *_: None)
    finally:
        for s in (a, b, c):
            s.shutdown()
    data = json.loads((out / "bench.json").read_text())
    rows = {r["input"]: r for r in data["rows"]}
    assert data["devices"] == ["desktop", "mobile"]
    ra, rb, rc = rows[url_a], rows[url_b], rows[url_c]
    assert ra["verdict"] == "healthy" and set(ra["devices"]) == {"desktop", "mobile"}
    assert ra["devices"]["mobile"]["report_rel"].endswith("report.html") and ra["devices"]["mobile"]["perf"]["pages"] >= 3
    assert rb["devices"]["desktop"]["verdict"] == "down" and rb["devices"]["mobile"]["verdict"] in ("healthy", "degraded")
    assert rb["verdict"] == "down" and "product.pdp.ceramic-vase" in rb["device_only"]["desktop"] and rb["device_only"]["mobile"] == []
    assert {f["device"] for f in rb["failures"]} == {"desktop"}
    assert rc["verdict"] == "blocked" and list(rc["devices"]) == ["desktop"]        # password store: mobile not asked
    assert data["totals"]["down"] == 1 and data["totals"]["healthy"] == 1
    html = (out / "bench.html").read_text()
    assert "Mobile" in html and "suites_by_device" in html


def test_preselected_size_without_a_cart_form_is_read_from_the_page(tmp_path):
    """foxtale.in (9 Oct bench, both devices, 3 runs): a headless-style page with no cart form pre-selects its
    200g 'Best Value' size; v0.18 fell back to the first size (75g, ₹349), found that price nowhere and failed a
    working page. Radar must read the size the page marks as selected."""
    run = _product_only("preselected_variant", tmp_path, max_products=2)
    vase = _case(run, "product.pdp.ceramic-vase")
    st = _steps(vase)
    assert vase.verdict == "pass", [(s.name, s.error) for s in vase.attempts[-1].steps if s.status == "fail"]
    assert "202" in st["product_identified"].detail and "1,399" in st["product_identified"].detail, st["product_identified"].detail


def test_closed_drawer_is_never_taken_for_the_popup_and_the_real_popup_is_closed(tmp_path):
    """suta.in (wishlist drawer, 8 Oct) and soulflower.in (cart drawer, 7 Oct), mobile: v0.18's popup finder kept
    picking a closed, off-screen side drawer, its close click timed out, and the real promo popup over the product
    grid was never closed ('26 covered by div'). On both devices the journey must pass, the real popup closed."""
    results = _both("drawer_decoy_popup", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1,
                    max_nav_links=2)
    for run, _ in results:
        j = _case(run, "journey.")
        assert j.verdict == "pass", (run.device, [(s.name, s.error) for s in j.attempts[-1].steps if s.status == "fail"])
        assert len(j.attempts) == 1, (run.device, "needed a retry")


def test_rate_limited_store_is_blocked_never_down(tmp_path, monkeypatch):
    """Own Contabo server, 9 Oct: Shopify answered HTTP 429 to every product-data request and all 3 demo stores were
    reported DOWN. A 429 is the platform throttling Radar: tests are BLOCKED with the reason, no incident, never down."""
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.02, raising=False)
    run, _ = _scan("rate_limited", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict != "down", run.verdict
    hit = [c for c in run.cases if c.suite in ("product", "journey")]
    assert hit and all(c.verdict == "blocked" for c in hit), [(c.case_id, c.verdict) for c in hit]
    assert all("429" in (c.attempts[-1].error or "") and "rate-limiting" in c.attempts[-1].error for c in hit)
    assert not any(c.incident_signature for c in run.cases)


def test_short_rate_limit_is_waited_out(tmp_path, monkeypatch):
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.05, raising=False)
    run = _product_only("rate_limited_once", tmp_path, max_products=1)
    assert all(c.verdict == "pass" and len(c.attempts) == 1 for c in run.cases), \
        [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases]


def test_rate_limited_discovery_is_blocked_not_error(tmp_path, monkeypatch):
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.02, raising=False)
    results = _both("rate_limited_all", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    first = results[0][0]
    assert first.verdict == "blocked" and any("rate-limit" in n for n in first.notes), (first.verdict, first.notes)
    assert len(results) == 1 or results[1][0].verdict in ("blocked", "skipped"), [r.verdict for r, _ in results]


def test_image_link_under_a_slider_layer_falls_back_to_the_product_name(tmp_path):
    """bummer.in (9 Oct cloud run, desktop, 3/3 attempts): a slider layer over the product image swallowed the click and
    Playwright's fallback click was intercepted, so Radar raised 'could not click' before trying the product's name
    link, which a shopper would click. The journey must pass and the dead image link be a store WARNING."""
    run, _ = _scan("slider_over_image", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    j = _case(run, "journey.")
    assert j.verdict == "pass" and len(j.attempts) == 1, [(s.name, s.error) for s in j.attempts[-1].steps if s.status == "fail"]


def test_short_server_error_is_waited_out_not_confirmed(tmp_path, monkeypatch):
    """9 Oct cloud run: Shopify demo stores answered 503 for ~2 minutes; retries seconds apart confirmed it (2 stores
    'down', 1 'unreachable'). Radar must wait before re-checking a 5xx, so a short hiccup is not a store failure."""
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.25, raising=False)
    run, _ = _scan("server_blip", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict == "healthy", (run.verdict, [(c.case_id, c.verdict) for c in run.cases if c.verdict != "pass"])
    bad = [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases if c.verdict == "confirmed_fail"]
    assert not bad, bad


def test_a_real_outage_is_still_confirmed(tmp_path, monkeypatch):
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.02, raising=False)
    run = _product_only("products_down", tmp_path, max_products=1)
    assert run.cases and all(c.verdict == "confirmed_fail" for c in run.cases), [(c.case_id, c.verdict) for c in run.cases]
    assert "503" in (run.cases[0].attempts[-1].error or "")


def test_popup_closed_by_a_bare_x_in_a_div(tmp_path):
    run, _ = _scan("div_x_popup", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    j = _case(run, "journey.")
    assert j.verdict == "pass" and len(j.attempts) == 1, [(s.name, s.error) for s in j.attempts[-1].steps if s.status == "fail"]


def test_login_popup_in_a_cross_origin_iframe_is_closed_inside_the_frame(tmp_path):
    """GoKwik KwikPass (bonkerscorner.com, boldcare.in, consciouschemist.com, 7-9 Oct): a full-screen iframe from
    another origin with no popup-like name (id 'iframe-kp' in div#d2c-pass) and its × inside the frame blocked every
    click: 'could not click: <iframe id="iframe-kp" ...>'. Radar closes it with the frame's own ×, never logs in."""
    run, _ = _scan("kwikpass_popup", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    j = _case(run, "journey.")
    assert j.verdict == "pass" and len(j.attempts) == 1, [(s.name, s.error) for s in j.attempts[-1].steps if s.status == "fail"]
    assert any(k["what"].startswith("popup (iframe)") and "inside the popup frame" in k["actual"]
               and "still open" not in k["actual"] for k in _checks(j))


def test_cards_hidden_until_the_shopper_scrolls_are_found(tmp_path):
    run, _ = _scan("scroll_reveal", tmp_path, allow_cart_flow=False, max_products=1, max_collections=1, max_nav_links=2)
    j = _case(run, "journey.")
    assert j.verdict == "pass" and len(j.attempts) == 1, [(s.name, s.error) for s in j.attempts[-1].steps if s.status == "fail"]


def _signing_key(monkeypatch):
    import secrets as _s
    from radar.core.webbotauth import Signer, b64u
    from tests.mockstore.server import Handler
    seed = b64u(_s.token_bytes(32))
    monkeypatch.setenv("RADAR_SIGNING_KEY", seed)
    monkeypatch.setattr(Handler, "SIGNER_X", Signer(seed).x)


def test_signed_radar_passes_a_store_edge_that_refuses_unsigned_bots(tmp_path, monkeypatch):
    """9 Oct: Shopify's edge (Cloudflare) answered 429 to every unsigned request from Oracle Cloud and Contabo. With
    Web Bot Auth signing (RADAR_SIGNING_KEY), every page, data request, robots.txt and add-to-cart call to the store
    carries a verifiable BugRadar signature: the store is tested normally."""
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.02, raising=False)
    _signing_key(monkeypatch)
    run, _ = _scan("signed_only", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict == "healthy", (run.verdict, run.notes, [(c.case_id, c.verdict, c.attempts[-1].error)
                                                               for c in run.cases if c.verdict != "pass"])


def test_unsigned_radar_is_blocked_rate_limited_by_that_edge(tmp_path, monkeypatch):
    from radar.core.browser import Session
    monkeypatch.setattr(Session, "backoff_scale", 0.02, raising=False)
    monkeypatch.delenv("RADAR_SIGNING_KEY", raising=False)
    run, _ = _scan("signed_only", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    assert run.verdict in ("blocked", "unreachable"), (run.verdict, run.notes)


def test_pincode_gate_is_a_warning_and_the_cart_is_blocked_not_failed(tmp_path):
    """bombaysweetshop.com (new30d held-out run, 10 Oct, both devices): the right variant WAS selected, but the buy button
    stays disabled and says 'PLEASE ENTER YOUR PINCODE TO CHECK AVAILABILITY'. Radar never types a pincode (it submits
    no form but add-to-cart), so: product test PASSES with a pincode WARNING, cart + journey cart steps are BLOCKED
    with the reason, nothing is a failure."""
    run, _ = _scan("pincode_gate", tmp_path)
    bad = [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases if c.verdict in ("confirmed_fail", "flaky")]
    assert not bad, bad
    pdp = [c for c in run.cases if c.case_id.startswith("product.pdp.")]
    assert pdp and all(c.verdict == "pass" for c in pdp), [(c.case_id, c.verdict) for c in pdp]
    steps = [s for c in pdp for s in c.attempts[-1].steps]
    k = [ch for s in steps if s.name == "variant_ready" for ch in s.checks if ch["what"] == "variant selected like a shopper"]
    assert k and all(ch["ok"] and "pincode" in ch["actual"] for ch in k), k
    assert any(s.name == "buy_button_ready" and s.status == "warn" and "pincode" in (s.error or "") for s in steps), \
        [(s.name, s.status, s.error) for s in steps]
    for prefix in ("cart.", "journey."):
        c = _case(run, prefix)
        assert c.verdict == "blocked" and "pincode" in c.attempts[-1].error, (prefix, c.verdict, c.attempts[-1].error)


def test_disabled_buy_button_failure_names_the_button_and_the_enabled_one(tmp_path):
    """littleboxindia.com mobile (new30c + loop cycle 6): 'buy button enabled: false' while an enabled ADD TO CART for the
    same product was on screen. One store = watch, so no behaviour change: the failure must NAME the button Radar read
    and the enabled one, so the next store with this layout is diagnosable from run.json alone."""
    run, _ = _scan("disabled_dup_button", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    errs = [s.error or "" for c in run.cases if c.case_id.startswith("product.pdp.")
            for s in c.attempts[-1].steps if s.name == "buy_button_ready"]
    assert errs and all("btn-mobile-atc" in e and "ARE enabled" in e and "sticky-atc" in e for e in errs), errs


def test_empty_document_title_on_a_rendered_page_is_an_seo_warning_not_down(tmp_path):
    """hairoriginals.com (new30e held-out run, 10 Oct, both devices): every page rendered in full (screenshot), but
    document.title was empty, so every load step failed and the store was 'down'. Shoppers never see the tab title:
    the page loads, and the SEO test 'title' reports the empty title as a warning."""
    run, _ = _scan("empty_doc_title", tmp_path, max_products=1, max_collections=1, max_nav_links=2)
    bad = [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases if c.verdict in ("confirmed_fail", "flaky")]
    assert not bad, bad
    meta = _case(run, "health.meta.home")
    t = [s for s in meta.attempts[-1].steps if s.name == "title"]
    assert t and t[0].status == "warn" and "missing" in (t[0].error or ""), [(s.name, s.status, s.error) for s in t]
