import json
from radar.shopify_data import pick_available, parse_product_data, price_ok, cart_has_variant


def test_pick_skips_sold_out():
    data = {"products": [
        {"handle": "a", "title": "A", "variants": [{"id": 1, "available": False, "price": "10"}]},
        {"handle": "b", "title": "B", "variants": [{"id": 2, "available": False}, {"id": 3, "available": True, "price": "99.00"}]},
    ]}
    got = pick_available(data)
    assert got["handle"] == "b" and got["variant_id"] == 3 and got["price"] == "99.00"


def test_pick_none_when_all_sold_out_or_empty():
    assert pick_available({"products": [{"handle": "a", "variants": [{"id": 1, "available": False}]}]}) is None
    assert pick_available({}) is None
    assert pick_available(None) is None


def test_jsonld_product_with_offer_dict():
    ld = json.dumps({"@type": "Product", "name": "Vase", "image": ["https://x/i.jpg"],
                     "offers": {"price": "1,299.00", "availability": "https://schema.org/InStock"}})
    got = parse_product_data([ld], {})
    assert got["title"] == "Vase" and got["image"] == "https://x/i.jpg" and got["source"] == "json-ld"
    assert price_ok(got["price"])


def test_jsonld_in_graph_and_offer_list():
    ld = json.dumps({"@graph": [{"@type": "WebSite"}, {"@type": ["Product"], "name": "Rug",
                     "offers": [{"price": "500"}]}]})
    got = parse_product_data([ld], {})
    assert got["title"] == "Rug" and got["price"] == "500"


def test_meta_fallback_and_bad_json_ignored():
    got = parse_product_data(["{not json"], {"og:title": "T", "og:image": "i", "product:price:amount": "10"})
    assert got["source"] == "meta" and got["title"] == "T" and price_ok(got["price"])


def test_nothing_found():
    got = parse_product_data([], {})
    assert got["source"] is None and not price_ok(got["price"])


def test_price_ok_rejects_zero_and_text():
    assert not price_ok("0") and not price_ok("abc") and not price_ok(None) and price_ok("1,299")


def test_cart_has_variant():
    assert cart_has_variant({"items": [{"variant_id": 7}]}, 7)
    assert not cart_has_variant({"items": []}, 7)
