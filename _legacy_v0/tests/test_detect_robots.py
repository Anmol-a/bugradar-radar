from radar.detect import detect_platform
from radar.robots import Robots

UA = "BugRadar/0.1 (+bugradar.in)"


def test_shopify_detected_with_two_markers():
    html = '<img src="https://cdn.shopify.com/s/files/x.jpg"><link href="/cdn/shop/files/a.css">'
    got = detect_platform(html)
    assert got["platform"] == "shopify" and len(got["evidence"]) == 2


def test_single_cdn_link_is_not_enough():
    assert detect_platform('<video src="https://cdn.shopify.com/v.webm">')["platform"] == "unknown"


def test_custom_stack_is_unknown():
    html = '<img src="https://cdn.vaaree.com/tenant-123/a.jpg"><script src="/_next/static/x.js">'
    assert detect_platform(html)["platform"] == "unknown"


def test_empty_html():
    assert detect_platform("")["platform"] == "unknown"
    assert detect_platform(None)["platform"] == "unknown"


def test_robots_disallow_products_json():
    r = Robots("User-agent: *\nDisallow: /products.json\nDisallow: /cart\n", UA)
    assert not r.allowed("https://x.com/products.json?limit=50")
    assert not r.allowed("https://x.com/cart")
    assert r.allowed("https://x.com/products/vase")


def test_robots_specific_agent_block():
    r = Robots("User-agent: BugRadar\nDisallow: /\n", UA)
    assert not r.allowed("https://x.com/")


def test_robots_unreadable_means_allowed():
    r = Robots(None, UA)
    assert r.allowed("https://x.com/anything") and not r.loaded
