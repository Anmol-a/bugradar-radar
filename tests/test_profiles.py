"""Scan profiles (radar/runner/profiles.py, 15 Oct): the free scan page's 'basic' first check keeps one of each of
eight read-only cases, on the desktop only, and never a cart case."""
from dataclasses import replace

import pytest

from radar.core.config import Settings
from radar.core.models import Suite, TestCase
from radar.runner import profiles as p
from radar.runner.executor import scan_devices
from tests.mockstore.server import serve
from tests.test_e2e import _settings


def _tc(cid, suite=None):
    return TestCase(cid, suite or cid.split(".")[0], cid, "x")


def test_basic_keeps_one_of_each_and_drops_the_rest():
    suites = [
        Suite("journey", "J", "", [_tc("journey.first_purchase")]),
        Suite("smoke", "S", "", [_tc("smoke.home_health"), _tc("smoke.nav_links"), _tc("smoke.mobile_menu"), _tc("smoke.more_links")]),
        Suite("catalog", "C", "", [_tc("catalog.collection.tees"), _tc("catalog.collection.mugs"), _tc("catalog.more.tees"), _tc("catalog.filter.tees")]),
        Suite("product", "P", "", [_tc("product.pdp.tee"), _tc("product.pdp.mug"), _tc("product.variant_switch"), _tc("product.sold_out"), _tc("product.pincode")]),
        Suite("cart", "K", "", [_tc("cart.add"), _tc("cart.discount_code")]),
        Suite("search", "Q", "", [_tc("search.tee"), _tc("search.no_results"), _tc("search.suggestions")]),
        Suite("health", "H", "", [_tc("health.meta.home"), _tc("health.meta.pdp"), _tc("health.not_found")]),
        Suite("info", "I", "", [_tc("info.policies")]),
    ]
    kept = p.filter_suites(suites, "basic")
    assert [s.id for s in kept] == ["smoke", "catalog", "product", "search", "health"]
    assert [c.id for s in kept for c in s.cases] == [
        "smoke.home_health", "smoke.nav_links", "catalog.collection.tees", "product.pdp.tee", "search.tee",
        "health.meta.home", "health.not_found"]
    assert p.filter_suites(suites, "full") is suites and p.filter_suites(suites, None) is suites
    with pytest.raises(ValueError):
        p.filter_suites(suites, "nope")


def test_basic_settings_and_devices():
    s = p.settings_for_profile("basic", Settings())
    assert (s.max_products, s.max_collections, s.max_nav_links, s.allow_cart_flow) == (1, 1, 4, False)
    assert p.settings_for_profile("full", Settings()) == Settings()
    assert p.devices_for_profile("basic", ("desktop", "mobile")) == ("desktop",)
    assert p.devices_for_profile(None, ("desktop", "mobile")) == ("desktop", "mobile")


def test_basic_profile_on_the_mock_store_runs_only_the_free_checks(tmp_path):
    srv, url = serve("healthy")
    try:
        results = scan_devices(url, _settings(tmp_path), ("desktop", "mobile"), profile="basic")
    finally:
        srv.shutdown()
    assert len(results) == 1 and results[0][0].device == "desktop"          # phone run is daily monitoring's
    run = results[0][0]
    ids = [c.case_id for c in run.cases]
    assert run.verdict == "healthy", [(c.case_id, c.verdict, c.attempts[-1].error) for c in run.cases]
    assert ids[:2] == ["smoke.home_health", "smoke.nav_links"]
    assert sum(i.startswith("catalog.collection.") for i in ids) == 1
    assert sum(i.startswith("product.pdp.") for i in ids) == 1
    assert sum(i.startswith("search.") for i in ids) == 1 and "search.no_results" not in ids
    assert {"health.meta.home", "health.not_found"} <= set(ids)
    assert not any(i.startswith(("cart.", "journey.", "info.")) for i in ids)
    assert "product.pincode" not in ids and "product.variant_switch" not in ids
    assert len(ids) == 7
