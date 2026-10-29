"""The store's Shopify admin data (29 Oct 2026, Shopify review 2.2.1): the menu links and the products the BugRadar
Shopify app reads through the Admin API, turned into two checks (smoke.shopify_menu, product.shopify_admin)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from radar import worker as w
from radar.checks.library import stock_verdict
from radar.core.config import Settings
from radar.discovery import shopify_admin as sa
from radar.generate.builder import build_suites
from radar.runner import profiles
from radar.runner.executor import scan
from tests.mockstore.server import serve
from tests.test_e2e import _settings
from tests.test_units import _sm
from tools.upload_dashboard import PLAIN_NAMES, plain_reason

NOW = datetime(2026, 10, 29, 6, 30, tzinfo=timezone.utc)
BASE = "https://lumen.in"
DATA = {
    "read_at": "2026-10-29T06:25:45.770Z", "currency": "INR",
    "menus": [
        {"handle": "main-menu", "title": "Main menu", "links": [
            {"title": "Home", "url": "/", "type": "FRONTPAGE", "depth": 1},
            {"title": "Shop", "url": "https://lumen.in/collections/all", "type": "CATALOG", "depth": 1},
            {"title": "Serums", "url": "https://lumen-dev.myshopify.com/collections/serums?sort=new#top", "type": "COLLECTION", "depth": 2},
            {"title": "Instagram", "url": "https://instagram.com/lumen", "type": "HTTP", "depth": 1},
            {"title": "Shop again", "url": "/collections/all/", "type": "CATALOG", "depth": 1},
        ]},
        {"handle": "footer", "title": "Footer menu", "links": [
            {"title": "Contact", "url": "/pages/contact", "type": "PAGE", "depth": 1},
            {"title": "Cart", "url": "/cart", "type": "HTTP", "depth": 1},
            {"title": "Mail", "url": "mailto:hi@lumen.in", "type": "HTTP", "depth": 1},
        ]},
        {"handle": "customer-account-main-menu", "title": "Account", "links": [
            {"title": "Orders", "url": "/account/orders", "type": "HTTP", "depth": 1},
        ]},
    ],
    "products": [
        {"handle": "vit-c", "title": "Vitamin C Serum", "url": "https://lumen.in/products/vit-c", "variants": [
            {"title": "30 ml", "price": "599.00", "available": True}, {"title": "50 ml", "price": "899.00", "available": False}]},
        {"handle": "night-cream", "title": "Night Cream", "url": "/products/night-cream", "variants": [
            {"title": "Default Title", "price": "1,299.00", "available": False}]},
        {"handle": "sample", "title": "Free sample", "url": "/products/sample", "variants": [{"price": "0.00", "available": True}]},
        {"handle": "toner", "title": "Toner", "url": "/products/toner", "variants": [{"price": "450.00", "available": True}]},
        {"handle": "mask", "title": "Clay Mask", "url": "/products/mask", "variants": [{"price": "350.00", "available": True}]},
    ],
}


def test_same_store_joins_relative_links_and_keeps_only_the_stores_own():
    assert sa.same_store("/collections/all", BASE) == "https://lumen.in/collections/all"
    assert sa.same_store("https://www.lumen.in/pages/x", BASE) == "https://lumen.in/pages/x"
    assert sa.same_store("https://lumen-dev.myshopify.com/a?b=1#c", BASE) == "https://lumen.in/a?b=1", "myshopify address -> the store's host"
    assert sa.same_store("https://instagram.com/lumen", BASE) is None
    assert sa.same_store("mailto:hi@lumen.in", BASE) is None and sa.same_store("", BASE) is None
    assert sa.same_store("/x", "http://127.0.0.1:8000") == "http://127.0.0.1:8000/x", "the store address's own scheme"


def test_menu_links_top_level_first_one_per_address_no_account_or_cart():
    rows, note = sa.menu_links(DATA, BASE)
    assert [r["url"] for r in rows] == ["https://lumen.in/", "https://lumen.in/collections/all", "https://lumen.in/pages/contact",
                                        "https://lumen.in/collections/serums?sort=new"]
    assert rows[2]["menu"] == "Footer menu"
    assert note == {"offsite": 2, "skipped": 1, "over_cap": 0}, "instagram + mailto offsite; /cart skipped; account menu ignored"
    capped, note = sa.menu_links(DATA, BASE, cap=2)
    assert len(capped) == 2 and note["over_cap"] == 2
    assert sa.menu_links(None, BASE) == ([], {"offsite": 0, "skipped": 0, "over_cap": 0})


def test_admin_products_in_stock_first_plus_one_sold_out_no_free_ones():
    picks = sa.admin_products(DATA, BASE)
    assert [p["handle"] for p in picks] == ["vit-c", "toner", "night-cream"]
    assert picks[0] == {"handle": "vit-c", "title": "Vitamin C Serum", "url": "https://lumen.in/products/vit-c",
                        "prices": [599.0, 899.0], "available": True}
    assert picks[2]["url"] == "https://lumen.in/products/night-cream" and picks[2]["prices"] == [1299.0]
    only_sold = {"products": [DATA["products"][1]]}
    assert [p["handle"] for p in sa.admin_products(only_sold, BASE)] == ["night-cream"]


def test_prices_are_compared_only_while_fresh():
    assert sa.fresh(DATA, NOW)
    assert not sa.fresh(DATA, NOW + timedelta(hours=25)), "a day old: the owner hasn't opened the app since"
    assert sa.fresh(DATA, NOW - timedelta(minutes=3)), "a clock a few minutes behind"
    assert not sa.fresh({"read_at": "garbage"}, NOW) and not sa.fresh(None, NOW)


def test_stock_verdict_both_ways():
    assert stock_verdict(False, {"text": "Add to cart", "enabled": True}, "")[2] is False, "sold out in admin, buyable on page"
    assert stock_verdict(False, {"text": "Sold out", "enabled": False}, "Sold out")[2] is True
    assert stock_verdict(True, {"text": "Add to cart", "enabled": True}, "")[2] is True
    assert stock_verdict(True, {"text": "Sold out", "enabled": False}, "Sold out")[2] is False, "in stock in admin, page says sold out"
    assert stock_verdict(True, {"text": "Add", "enabled": False}, "")[2] is True, "disabled until a size is chosen"
    assert stock_verdict(True, None, "")[2] is True


def test_worker_settings_carry_the_menu_always_and_products_only_while_fresh():
    m = {"url": BASE, "quick": True, "shopify_data": DATA}
    s = w.settings_for(m, Settings(), now=NOW)
    assert len(s.shopify_menu) == 4 and s.shopify_currency == "INR" and len(s.shopify_products) == 3
    old = w.settings_for(m, Settings(), now=NOW + timedelta(days=2))
    assert len(old.shopify_menu) == 4 and old.shopify_products == ()
    none = w.settings_for({"url": BASE, "quick": True}, Settings(), now=NOW)
    assert none.shopify_menu == () and none.shopify_products == ()
    junk = w.settings_for({"url": BASE, "shopify_data": "not json"}, Settings(), now=NOW)
    assert junk.shopify_menu == ()


def test_builder_adds_the_two_cases_and_free_keeps_them():
    s = w.settings_for({"url": BASE, "quick": True, "shopify_data": DATA}, Settings(), now=NOW)
    suites = build_suites(_sm(), s)
    cases = {c.id: c for x in suites for c in x.cases}
    menu, admin = cases["smoke.shopify_menu"], cases["product.shopify_admin"]
    assert menu.check == "links_resolve" and len(menu.params["urls"]) == 4 and menu.params["max_links"] == 4
    assert "Not opened: 2 to other sites, 1 account / cart pages" in menu.description
    assert admin.check == "admin_match" and admin.params["currency"] == "INR" and len(admin.params["products"]) == 3
    free = {c.id for x in profiles.filter_suites(suites, "core") for c in x.cases}
    assert {"smoke.shopify_menu", "product.shopify_admin"} <= free
    plain = {c.id for x in build_suites(_sm(), Settings()) for c in x.cases}
    assert not {"smoke.shopify_menu", "product.shopify_admin"} & plain, "no admin data, no such cases"


def test_dashboard_names_and_reasons():
    assert PLAIN_NAMES["smoke.shopify_menu"] == "Shopify menu links"
    assert plain_reason("smoke.shopify_menu", None, "all_links_ok", "broken navigation pages: expected 0, got 1: /pages/old") \
        == "A link in your Shopify menus opens a page that doesn't exist: /pages/old."
    assert plain_reason("product.shopify_admin", None, "stock matches admin: 'Toner'", "x").startswith(
        "A product page's stock doesn't match your Shopify admin")


# ---- end to end on the mock store: the admin's data matches the pages, then it doesn't ----
def _admin_scan(tmp_path, products, menu):
    srv, url = serve("healthy")
    try:
        prods = tuple(dict(p, url=url + p["url"]) for p in products)
        links = tuple({"text": t, "url": url + u, "menu": "Main menu"} for t, u in menu)
        run, _ = scan(url, _settings(tmp_path, shopify_menu=links, shopify_products=prods, shopify_currency="INR"))
        return run
    finally:
        srv.shutdown()


def _steps(run, cid):
    case = next(c for c in run.cases if c.case_id == cid)
    return case, {s.name: s for s in case.attempts[-1].steps}


def test_admin_data_matching_the_store_passes(tmp_path):
    run = _admin_scan(tmp_path, [
        {"handle": "ceramic-vase", "title": "Ceramic Flower Vase", "url": "/products/ceramic-vase", "prices": [1299.0, 1399.0], "available": True},
        {"handle": "sold-out-lamp", "title": "Brass Table Lamp", "url": "/products/sold-out-lamp", "prices": [2499.0], "available": False},
    ], [("Home Decor", "/collections/home-decor"), ("Kitchen", "/collections/kitchen")])
    case, steps = _steps(run, "product.shopify_admin")
    assert case.verdict == "pass", [(s.name, s.status, s.error) for s in steps.values()]
    assert steps["price matches admin: 'Ceramic Flower Vase'"].status == "pass"
    assert steps["stock matches admin: 'Brass Table Lamp'"].status == "pass"
    assert steps["compared_with_admin"].detail == "2 product page(s) compared with your Shopify admin"
    menu, msteps = _steps(run, "smoke.shopify_menu")
    assert menu.verdict == "pass", [(s.name, s.status, s.error) for s in msteps.values()]


def test_admin_data_not_matching_the_store_warns(tmp_path):
    run = _admin_scan(tmp_path, [
        {"handle": "ceramic-vase", "title": "Ceramic Flower Vase", "url": "/products/ceramic-vase", "prices": [1199.0], "available": True},
        {"handle": "sold-out-lamp", "title": "Brass Table Lamp", "url": "/products/sold-out-lamp", "prices": [2499.0], "available": True},
    ], [("Gone", "/collections/no-such-collection")])
    case, steps = _steps(run, "product.shopify_admin")
    price, stock = steps["price matches admin: 'Ceramic Flower Vase'"], steps["stock matches admin: 'Brass Table Lamp'"]
    assert price.status == "warn" and "1,199.00" in price.error, price.error
    assert stock.status == "warn" and "in stock in your Shopify admin" in stock.error, stock.error
    assert case.verdict in ("pass", "flaky"), "warnings, not a failure (strict off)"
    menu, _ = _steps(run, "smoke.shopify_menu")
    assert menu.verdict == "confirmed_fail", "a menu link from Shopify that doesn't open is a real finding"
