from radar.discovery.detect import detect_platform
from radar.core.robots import Robots

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


def test_robots_unreachable_disallows_everything():
    """RFC 9309: robots.txt 5xx or no answer = do not crawl (bench 8, 6 Oct; v0.12 allowed everything)."""
    r = Robots(None, UA, unreachable=True)
    assert not r.allowed("https://x.com/") and not r.allowed("https://x.com/products/a") and r.unreachable


def test_robots_text_wins_over_unreachable_flag():
    r = Robots("User-agent: *\nAllow: /\n", UA, unreachable=True)
    assert r.allowed("https://x.com/") and not r.unreachable


def test_online_probe_rules():
    """Any HTTP answer (even 500) = Radar online; nothing listening = offline; empty list = check off."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from radar.core.network import online, reset_cache

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500); self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        reset_cache()
        assert online(()) is True
        assert online(("http://127.0.0.1:9/",), timeout=2) is False
        assert online(("http://127.0.0.1:9/", f"http://127.0.0.1:{srv.server_address[1]}/"), timeout=2) is True
        reset_cache()
        assert online(("http://127.0.0.1:9/",), timeout=2) is False      # offline is never cached as online
    finally:
        srv.shutdown()


def test_net_probes_setting_from_env(monkeypatch):
    from radar.core.config import load_settings
    from radar.core.network import DEFAULT_PROBES
    monkeypatch.delenv("RADAR_NET_PROBES", raising=False)
    assert load_settings().net_probe_urls == DEFAULT_PROBES
    monkeypatch.setenv("RADAR_NET_PROBES", "https://a.example/, https://b.example/")
    assert load_settings().net_probe_urls == ("https://a.example/", "https://b.example/")
    monkeypatch.setenv("RADAR_NET_PROBES", "")
    assert load_settings().net_probe_urls == ()
