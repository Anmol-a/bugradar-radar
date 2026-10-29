"""Public addresses only (security review #4, 29 Oct): radar/core/netguard.py, the worker's refusal and
Session.request_get's per-hop checks. No browser and no network: names are resolved by a fake resolver."""
import pytest

from radar.core import netguard
from radar.core.netguard import PrivateAddress, browser_url_ok, public_host, public_ip, public_url, require_public


@pytest.fixture(autouse=True)
def guard_on(monkeypatch):
    monkeypatch.delenv("RADAR_ALLOW_PRIVATE_HOSTS", raising=False)
    netguard.clear_cache()
    yield
    netguard.clear_cache()


DNS = {
    "shop.example.com": ["23.227.38.65"],
    "rebind.example.com": ["127.0.0.1"],
    "meta.example.com": ["169.254.169.254"],
    "mixed.example.com": ["23.227.38.65", "10.0.0.7"],
    "v6.example.com": ["2606:4700::6810:1"],
    "v6local.example.com": ["fd00::1"],
    "mapped.example.com": ["::ffff:192.168.1.1"],
}
fake = lambda h: DNS.get(h, [])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.0.10",
                                "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
                                "198.18.0.1", "::1", "::", "fe80::1", "fc00::1", "fd12::1", "::ffff:127.0.0.1",
                                "2002:7f00:1::1", "not-an-ip"])
def test_private_addresses(ip):
    assert public_ip(ip) is False


@pytest.mark.parametrize("ip", ["23.227.38.65", "8.8.8.8", "1.1.1.1", "2606:4700::6810:1", "::ffff:8.8.8.8"])
def test_public_addresses(ip):
    assert public_ip(ip) is True


@pytest.mark.parametrize("host,ok", [
    ("shop.example.com", True), ("rebind.example.com", False), ("meta.example.com", False),
    ("mixed.example.com", False),                 # one private answer is enough to refuse
    ("v6.example.com", True), ("v6local.example.com", False), ("mapped.example.com", False),
    ("localhost", False), ("db.internal", False), ("printer.local", False), ("x.localhost", False),
    ("127.0.0.1", False), ("[::1]", False), ("23.227.38.65", True),
    ("does-not-resolve.example.com", True),       # not ours to judge: the visit fails on its own
    ("", False),
])
def test_public_host(host, ok):
    assert public_host(host, fake) is ok


def test_urls():
    assert public_url("https://shop.example.com/products/x", fake)
    assert not public_url("http://rebind.example.com/", fake)
    assert not public_url("http://127.0.0.1:5432/", fake)
    assert not public_url("http://[::1]:8080/", fake)
    assert not public_url("file:///etc/passwd", fake)
    assert not public_url("ftp://shop.example.com/", fake)
    with pytest.raises(PrivateAddress):
        require_public("http://169.254.169.254/latest/meta-data/", fake)
    require_public("https://shop.example.com/", fake)


def test_browser_route_decision():
    assert browser_url_ok("https://shop.example.com/a.js", fake)
    assert not browser_url_ok("http://meta.example.com/", fake)
    assert not browser_url_ok("ws://127.0.0.1:9222/devtools", fake)
    assert browser_url_ok("data:text/plain,hi", fake)            # not a network request


def test_switch_for_tests(monkeypatch):
    monkeypatch.setenv("RADAR_ALLOW_PRIVATE_HOSTS", "1")
    assert public_url("http://127.0.0.1:8000/")
    assert not public_url("file:///etc/passwd")                  # never a non-web scheme


def test_cache_is_used():
    calls = []
    def counting(h):
        calls.append(h)
        return ["23.227.38.65"]
    assert public_host("cached.example.com", counting) and public_host("cached.example.com", counting)
    assert calls == ["cached.example.com"]


# ---- the worker refuses before Radar starts ----

def test_worker_refuses_private_monitor(monkeypatch):
    import radar.runner.executor as ex
    from radar import worker
    from radar.core.config import Settings
    monkeypatch.setattr(ex, "scan_devices", lambda *a, **k: pytest.fail("must not visit"))
    status, note = worker.run_monitor({"id": "m1", "url": "http://localhost:3000/"}, Settings(), upload=False, log=lambda *_: None)
    assert status == "unsupported" and "public website" in note
    r = worker.run_scan({"id": "s1", "url": "http://127.0.0.1/"}, Settings(), upload=False, log=lambda *_: None)
    assert r["verdict"] == "error" and r["failed"] and "not a public" in r["note"]


# ---- Radar's own requests: each redirect hop is checked ----

class _Resp:
    def __init__(self, status, location=None):
        self.status, self.headers = status, ({"location": location} if location else {})


class _Req:
    def __init__(self, answers):
        self.answers, self.seen = answers, []
    def get(self, url, **kw):
        self.seen.append((url, kw.get("max_redirects"), kw.get("headers")))
        return self.answers[url]


def _session(answers):
    from types import SimpleNamespace
    from radar.core.browser import Session
    sess = Session.__new__(Session)
    sess.page = SimpleNamespace(context=SimpleNamespace(request=_Req(answers)))
    sess.sign = lambda url: {"sig": url} if "shop.example.com" in url else {}
    return sess


def test_request_get_follows_public_redirects(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", fake)
    s = _session({"https://shop.example.com/a": _Resp(301, "/b"), "https://shop.example.com/b": _Resp(200)})
    r = s.request_get("https://shop.example.com/a", timeout=5, headers=s.sign("https://shop.example.com/a"))
    assert r.status == 200
    seen = s.page.context.request.seen
    assert [u for u, *_ in seen] == ["https://shop.example.com/a", "https://shop.example.com/b"]
    assert all(m == 0 for _, m, _ in seen)                       # Playwright never follows a redirect itself
    assert seen[1][2] == {"sig": "https://shop.example.com/b"}   # re-signed for the new address


def test_request_get_stops_at_private_redirect(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", fake)
    s = _session({"https://shop.example.com/a": _Resp(302, "http://169.254.169.254/latest/meta-data/")})
    with pytest.raises(PrivateAddress):
        s.request_get("https://shop.example.com/a", timeout=5)
    assert len(s.page.context.request.seen) == 1                 # the private address was never asked


def test_request_get_refuses_private_start_and_keeps_no_redirect_mode(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", fake)
    s = _session({"https://shop.example.com/x": _Resp(301, "http://127.0.0.1/")})
    with pytest.raises(PrivateAddress):
        s.request_get("http://rebind.example.com/robots.txt", timeout=5)
    r = s.request_get("https://shop.example.com/x", max_redirects=0, timeout=5)
    assert r.status == 301                                       # the redirect itself, as the redirect checks expect
