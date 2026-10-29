import pytest
from radar.config import site_id_from_url, load_site
from radar.confirm import Attempt, should_retry, verdict, signature

OK, BAD = Attempt(True), Attempt(False, "add_to_cart", "x")


def test_site_id_normalisation():
    assert site_id_from_url("https://www.Shop125.example/products/x") == "shop125.example"
    assert site_id_from_url("xyz.in") == "xyz.in"
    assert site_id_from_url("https://shop.xyz.in:8080/") == "shop.xyz.in"
    with pytest.raises(ValueError):
        site_id_from_url("")


def test_pass_needs_no_retry():
    assert not should_retry([OK]) and verdict([OK]) == "pass"


def test_one_fail_then_pass_is_flaky_not_incident():
    assert should_retry([BAD])
    assert verdict([BAD, OK]) == "flaky"


def test_two_of_three_confirms():
    assert not should_retry([BAD, BAD])          # confirmed already, stop retrying
    assert verdict([BAD, BAD]) == "confirmed_fail"
    assert verdict([BAD, OK, BAD]) == "confirmed_fail"


def test_signature_dedupes_same_outage():
    assert signature("shop125.example", "buy_journey", "add_to_cart") == "shop125.example|buy_journey|add_to_cart"


def test_vaaree_config_loads():
    cfg = load_site("sites/shop125.yml")
    assert cfg.site_id == "shop125.example" and cfg.access.user_agent.startswith("BugRadar/0.1")


def test_non_shopify_rejected(tmp_path):
    f = tmp_path / "x.yml"
    f.write_text("base_url: https://a.com\nplatform: woo\nselectors: {}\n")
    with pytest.raises(ValueError):
        load_site(f)
