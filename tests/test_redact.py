"""Store names stay out of public GitHub logs (radar/core/redact.py, tools/redact_stream.py)."""
import io
import subprocess
import sys
from pathlib import Path

from radar.core import redact
from radar.core.redact import Redactor, alias, enabled, host_of, kept

ROOT = Path(__file__).resolve().parent.parent


def test_alias_is_stable_and_ignores_www_and_case():
    assert alias("shop.example.in") == alias("WWW.Shop.Example.in")
    assert alias("a.in") != alias("b.in")
    assert alias("a.in").startswith("store-") and len(alias("a.in")) == 12


def test_enabled_only_on_github():
    assert enabled({"GITHUB_ACTIONS": "true"})
    assert not enabled({})
    assert not enabled({"GITHUB_ACTIONS": "true", "RADAR_SHOW_STORES": "1"})


def test_host_of_store_list_lines():
    assert host_of("https://www.brand.co.in/   cart") == "brand.co.in"
    assert host_of("brand.store") == "brand.store"
    assert host_of("# comment") is None and host_of("   ") is None


def test_known_hosts_masked_everywhere():
    r = Redactor(["https://brand.co.in cart", "shop.in"])
    a, b = alias("brand.co.in"), alias("shop.in")
    line = "[1/2] brand.co.in  HEALTHY https://www.brand.co.in/products/x orders@shop.in sent shop.in desktop"
    out = r.scrub(line)
    assert "brand" not in out and "shop.in" not in out
    assert out.count(a) == 2 and out.count(b) == 2
    assert "https://" + a + "/products/x" in out


def test_longer_names_not_cut():
    r = Redactor(["shop.in"])
    assert r.scrub("myshop.in and shop.in.evil") == "myshop.in and shop.in.evil"   # neither is the store


def test_unknown_urls_masked_but_ours_kept():
    r = Redactor()
    out = r.scrub("free scan https://newstore.in/ (id 7) see https://github.com/x and https://bugradar.in/scan")
    assert "newstore" not in out and alias("newstore.in") in out
    assert "https://github.com/x" in out and "https://bugradar.in/scan" in out
    assert r.scrub("https://theme-dawn-demo.myshopify.com/") == "https://theme-dawn-demo.myshopify.com/"
    assert kept("cdn.shopify.com") and not kept("brand.myshopify.com")


def test_stream_wrapper_scrubs_split_writes():
    r = Redactor(["brand.in"])
    buf = io.StringIO()
    s = redact._Stream(buf, r)
    s.write("checking bra")
    s.write("nd.in now\nnext ")
    s.flush()
    assert "brand.in" not in buf.getvalue() and alias("brand.in") in buf.getvalue()


def test_install_is_off_locally(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert redact.install(["brand.in"]) is None


def test_pipe_filter(tmp_path):
    lst = tmp_path / "shard.txt"
    lst.write_text("# list\nhttps://brand.co.in  cart\n")
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "redact_stream.py"), str(lst)],
                       input="brand.co.in healthy\nsent brand.co.in mobile\n", capture_output=True, text=True,
                       env={"GITHUB_ACTIONS": "true", "PATH": "/usr/bin:/bin"})
    assert p.returncode == 0, p.stderr
    assert "brand" not in p.stdout and p.stdout.count(alias("brand.co.in")) == 2
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "redact_stream.py"), str(lst)],
                       input="brand.co.in healthy\n", capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert p.stdout == "brand.co.in healthy\n"        # off GitHub: unchanged


def test_worker_masks_claimed_store(monkeypatch, capsys):
    """run_once on GitHub: the free-scan log lines name an alias, never the store."""
    import radar.worker as w
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(redact, "_installed", None)
    red = redact.install()
    try:
        class Q:
            def claim_scan(self, worker):
                return {"id": 1, "url": "https://secretbrand.in/"}
            def finish_scan(self, *a):
                pass
        monkeypatch.setattr(w, "run_scan", lambda req, base, **k: {"verdict": "healthy", "score": 90, "summary": {},
                                                                  "run_id": None, "project_id": None, "note": "ok",
                                                                  "failed": 0})
        w.run_once(Q(), None, "t", log=print)
        sys.stdout.flush()
    finally:
        sys.stdout, sys.stderr = sys.stdout._inner, sys.stderr._inner
        monkeypatch.setattr(redact, "_installed", None)
    out = capsys.readouterr().out
    assert red is not None and "secretbrand" not in out and alias("secretbrand.in") in out
