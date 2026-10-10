"""Discovery: from a bare URL to a SiteMap. This is the 'only ask for the URL' layer.

Order matters and every step degrades gracefully, recording what it could not do in
sitemap.notes (shown in the report) instead of guessing:

  1. robots.txt                      -> what Radar may fetch
  2. homepage (real browser)         -> status, title, platform detection
  3. navigation links                -> header/nav anchors, same origin
  4. collections                     -> /collections.json, else nav links under /collections/
  5. products                        -> /products.json, else /products/ links + /products/<h>.js
  6. search                          -> search form on the page, else Shopify default /search
"""
from __future__ import annotations

import re
import time

from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from radar.core.browser import Session, RobotsBlocked
from radar.core.config import Settings, site_id_from_url
from radar.core.models import SiteMap, Product, Collection
from radar.core.network import NO_NETWORK_NOTE, online
from radar.core.robots import Robots
from radar.discovery.detect import detect_platform, detect_checkout_app, detect_access
from radar.discovery.shopify_data import products_from_json, product_from_js, price_ok

NAV_JS = r"""() => {
  // what a shopper sees first: visible menu links before hidden mega-menu links; product links are
  // left to the product suite (mega menus can hold hundreds of them)
  const sel = 'header a[href], nav a[href], [role=navigation] a[href]';
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll(sel)) {
    const href = a.href;
    // textContent, not innerText: drawer/mega-menu links are hidden until opened
    const text = (a.textContent || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    if (!href || seen.has(href) || !text || href.startsWith('javascript:')) continue;
    const u = new URL(href);
    if (/\/(cart|account|search|checkout)(\/|$|\?)|#/.test(u.pathname + u.hash) || /\/products\//.test(u.pathname)) continue;
    const r = a.getBoundingClientRect();
    seen.add(href); out.push({text: text.slice(0, 60), url: href, visible: r.width > 0 && r.height > 0});
  }
  return out.sort((x, y) => (y.visible ? 1 : 0) - (x.visible ? 1 : 0)).map(({text, url}) => ({text, url}));
}"""

LINKS_JS = """(pattern) => [...new Set([...document.querySelectorAll('a[href]')]
  .map(a => a.href).filter(h => h.includes(pattern)))]"""

# Footer links (journey 28: policy + contact pages). textContent, not innerText: collapsed footer accordions on
# phones hide their links until opened.
FOOTER_JS = r"""() => {
  const sel = 'footer a[href], [role=contentinfo] a[href], [id*="footer" i] a[href], [class*="footer" i] a[href], ' +
              '.shopify-section-group-footer-group a[href]';
  const seen = new Set(); const out = [];
  for (const a of document.querySelectorAll(sel)) {
    const href = a.href;
    if (!href || seen.has(href) || /^(javascript|mailto|tel):/i.test(href)) continue;
    seen.add(href);
    out.push({text: (a.textContent || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim().slice(0, 60), url: href});
  }
  return out.slice(0, 120);
}"""

# The header's account / login link (journey 29). Old customer accounts: /account or /account/login on the store;
# new customer accounts: Shopify-hosted (shopify.com/<id>/account or account.<store domain>).
ACCOUNT_JS = r"""() => {
  const cands = [...document.querySelectorAll('header a[href], [id*="header" i] a[href], nav a[href], a[href*="/account"]')]
    .filter(a => /\/account(\/login)?\/?$/.test(a.pathname) || /^account\./.test(a.hostname));
  const vis = a => { const r = a.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const a = cands.find(vis) || cands[0];
  return a ? a.href : null;
}"""

# Which footer link is which info page. Order matters: 'Shipping & Returns' is read as the returns page.
INFO_KINDS = (("refund", r"refund|return|exchange|cancell?ation"),
              ("shipping", r"shipping|delivery"),
              ("privacy", r"privacy"),
              ("terms", r"terms|conditions|\btos\b"),
              ("contact", r"contact"))
NOT_INFO = re.compile(r"/(products|collections|cart|account|search|checkout|blogs)(/|$)|/apps/|^/(cdn|files)/|"
                      r"\.[a-z0-9]{2,5}$", re.I)       # files (giva.co links its 'Annual Return' PDF from the footer)
# company filings Indian stores link next to their policies: 'Annual Return FY 2024-25' (MGT-7) is not a returns page
NOT_INFO_TEXT = re.compile(r"annual|investor|\bcsr\b|mgt[- ]?7|financial|shareholder|grievance redressal", re.I)


def info_pages(links: list[dict], base: str) -> list[dict]:
    """Footer links -> the store's info pages, at most one per kind (refund, shipping, privacy, terms, contact),
    same store only, never product / collection / cart / account / app links. Pure, unit-tested."""
    out: dict[str, dict] = {}
    for link in links:
        url, text = link.get("url") or "", link.get("text") or ""
        if not url or not _same_origin(url, base) or NOT_INFO.search(urlparse(url).path) or NOT_INFO_TEXT.search(text):
            continue
        hay = f"{text} {urlparse(url).path.replace('-', ' ').replace('_', ' ')}".lower()
        for kind, rx in INFO_KINDS:
            if kind not in out and re.search(rx, hay):
                out[kind] = {"kind": kind, "text": text[:60], "url": url.split("#")[0]}
                break
    return [out[k] for k, _ in INFO_KINDS if k in out]


def _same_origin(url: str, base: str) -> bool:
    a, b = urlparse(url), urlparse(base)
    strip = lambda h: (h or "").lower().removeprefix("www.")
    return strip(a.hostname) == strip(b.hostname)


def same_site(a: str, b: str) -> bool:
    """True when b is the same store as a (www / subdomain of the same registrable host). Pure."""
    strip = lambda u: (urlparse(u).hostname or "").lower().removeprefix("www.")
    ha, hb = strip(a), strip(b)
    return ha == hb or hb.endswith("." + ha) or ha.endswith("." + hb)


NOT_FOR_TESTS = re.compile(r"(_|-)clone|free[-_ ]?gift|gift[-_ ]?card|e[-_]?gift|sampler|tester|\bsample\b|dummy|test[-_ ]product",
                           re.I)


def token_priced(products: list, floor_share: float = 0.10, max_price: float = 50.0) -> list:
    """Products priced like a freebie SKU next to the store's real prices: at most 10% of the median
    priced product AND at most 50 (currency units). plumgoodness.com lists a Rs 1 'Skincare Duo'
    in /products.json whose page says 'The product is currently unavailable'. Pure, unit-tested."""
    def num(p):
        try:
            return float(str(p.price).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0
    priced = sorted(num(p) for p in products if num(p) > 0)
    if len(priced) < 3:
        return []
    median = priced[len(priced) // 2]
    return [p for p in products if 0 < num(p) <= min(max_price, floor_share * median)]


def _handle(url: str, kind: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    marker = f"/{kind}/"
    if marker not in path:
        return None
    h = path.split(marker, 1)[1].split("/")[0]
    return h or None


def load_robots(sess: Session, base: str, s: Settings) -> Robots:
    """RFC 9309: 200 = rules; 4xx = unavailable (allow all); 5xx or no answer = unreachable (disallow all).
    Three tries, 2 s and 5 s apart, longer timeout each time, before calling it unreachable (bench 11: soulflower.in
    and bummer.in had no answer on two quick tries and were blocked for the run; both answer normally)."""
    status, err = None, ""
    tries = ((10000, 2), (15000, 5), (20000, 0))
    for i, (timeout, gap) in enumerate(tries):
        try:
            r = sess.page.context.request.get(base + "/robots.txt", timeout=timeout, headers=sess.sign(base + "/robots.txt") or None)
            status = r.status
            if r.status == 200:
                return Robots(r.text(), s.user_agent)
            if 400 <= r.status < 500:
                return Robots(None, s.user_agent)
        except Exception as e:  # noqa: BLE001  DNS, TLS, timeout, reset
            status = None
            err = str(e).split("\n")[0][:160]   # evidence: which network error (chemistatplay.com, neemli.in new30c)
        if gap:
            time.sleep(gap)
    rb = Robots(None, s.user_agent, unreachable=True)
    rb.status = status
    rb.error = "" if status else err
    return rb


def _discover_info_pages(sess: Session, sm: SiteMap, base_url: str) -> None:
    """Footer policy / contact pages and the header account link, read from the homepage already open. Links that
    robots.txt disallows (Shopify's default robots.txt disallows /policies/ and /account) are never opened: they
    are listed in the notes instead, so the report says what was not tested and why."""
    try:
        found = info_pages(sess.evaluate(FOOTER_JS) or [], base_url)
        if not found:      # footers some themes render only when the shopper scrolls down to them
            sess.evaluate("async () => { scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'}); "
                          "await new Promise(r => setTimeout(r, 1500)); scrollTo({top: 0, behavior: 'instant'}); }")
            found = info_pages(sess.evaluate(FOOTER_JS) or [], base_url)
        acct = sess.evaluate(ACCOUNT_JS)
    except Exception:  # noqa: BLE001  a page script error must not stop discovery
        return
    skipped = [p for p in found if not sess.allowed(p["url"])]
    sm.info_pages = [p for p in found if p not in skipped]
    if skipped:
        sm.notes.append("footer info page(s) not opened, robots.txt disallows them (Shopify's default for /policies/): "
                        + ", ".join(f"{p['kind']} {urlparse(p['url']).path}" for p in skipped))
    if not found:
        sm.notes.append("no policy or contact links found in the footer")
    if acct:
        if not _same_origin(acct, base_url):
            sm.notes.append(f"account login is hosted elsewhere ({urlparse(acct).hostname}, Shopify customer accounts): "
                            "not opened")
        elif not sess.allowed(acct):
            sm.notes.append(f"account page {urlparse(acct).path} not opened: robots.txt disallows it "
                            "(Shopify's default robots.txt disallows /account)")
        else:
            sm.account_url = acct.split("#")[0]


def discover(sess: Session, base_url: str, s: Settings) -> SiteMap:
    sm = SiteMap(site_id=site_id_from_url(base_url), base_url=base_url,
                 discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # 1. robots
    sess.robots = load_robots(sess, base_url, s)
    sm.robots_loaded = sess.robots.loaded
    if sess.robots.unreachable:
        if not online(s.net_probe_urls):
            sm.access = "no_network"
            sm.notes.append(NO_NETWORK_NOTE.format(where=" before the store could be checked"))
            return sm
        code = getattr(sess.robots, "status", None)
        sm.access = "robots_unreachable"
        why = f"HTTP {code}" if code else ("no answer: " + getattr(sess.robots, "error", "") if getattr(sess.robots, "error", "") else "no answer")
        sm.notes.append(f"robots.txt could not be fetched ({why}, tried 3 times); "
                        "under the robots.txt standard (RFC 9309) that means 'do not crawl', so nothing was tested "
                        + ("this run. The store's domain does not resolve (DNS: no such host): check the address."
                           if "ENOTFOUND" in getattr(sess.robots, "error", "") else
                           "this run. Usually a temporary server problem on the store's side."))
        return sm
    if not sm.robots_loaded:
        sm.notes.append("store has no robots.txt (HTTP 4xx); everything allowed (RFC 9309)")

    # 2. homepage + platform
    try:
        resp, _ = sess.goto(base_url + "/")
    except RobotsBlocked:
        sm.access = "robots_blocked"
        sm.notes.append("robots.txt disallows the whole site for Radar's User-Agent; Radar respects it "
                        "(the store can allow 'BugRadar' in robots.txt)")
        return sm
    except Exception as e:  # noqa: BLE001  DNS, TLS, timeout
        msg = str(e).splitlines()[0][:150]
        if not online(s.net_probe_urls):
            sm.access = "no_network"
            sm.notes.append(NO_NETWORK_NOTE.format(where=f" (homepage: {msg})"))
            return sm
        sm.access = "unreachable"
        hint = " (domain does not resolve: check the store URL)" if "NAME_NOT_RESOLVED" in msg else ""
        sm.notes.append(f"homepage unreachable: {msg}{hint}")
        return sm
    if not same_site(base_url, sess.page.url):
        sm.access = "offsite"
        sm.home_title = sess.evaluate("() => document.title") or ""
        sm.notes.append(f"{urlparse(base_url).hostname} sends visitors to a different site "
                        f"({urlparse(sess.page.url).hostname}, '{sm.home_title[:60]}'): the store URL is probably wrong "
                        "or the domain is parked/for sale")
        return sm
    # A 5xx homepage is waited out twice (20 s, 60 s) before Radar calls the store unreachable: on 9 Oct three Shopify
    # demo stores answered 503 'Something went wrong' within the same 2 minutes from three different machines, a
    # platform hiccup. A store still down after ~80 s is reported as before.
    waited = 0
    for pause in (20, 60):
        if not (resp is not None and 500 <= resp.status <= 599):
            break
        sess.page.wait_for_timeout(int(pause * 1000 * sess.backoff_scale))
        waited += pause
        try:
            resp, _ = sess.goto(base_url + "/")
        except Exception:  # noqa: BLE001
            break
    if waited and resp is not None and resp.status < 400:
        sm.notes.append(f"homepage answered HTTP 5xx at first and recovered after ~{waited} s (short server hiccup)")
    html = sess.evaluate("() => document.documentElement.outerHTML") or ""
    sm.home_title = sess.evaluate("() => document.title") or ""
    sm.access = detect_access(resp.status if resp else None, sm.home_title, html, urlparse(sess.page.url).path)
    det = detect_platform(html)
    if det["platform"] != "shopify" and det["evidence"] and sm.access == "open" and resp is not None and resp.status < 400:
        # One Shopify marker but not two (koskii.com, new30f, 10 Oct: 'not Shopify' on one device and a healthy Shopify
        # store on the other). A store is one platform: load the homepage once more before saying it is not Shopify.
        first = list(det["evidence"])
        sess.page.wait_for_timeout(int(3000 * sess.backoff_scale))
        try:
            resp2, _ = sess.goto(base_url + "/")
            html2 = sess.evaluate("() => document.documentElement.outerHTML") or ""
            det2 = detect_platform(html2)
            if resp2 is not None and resp2.status < 400 and det2["platform"] == "shopify":
                resp, html, det = resp2, html2, det2
                sm.home_title = sess.evaluate("() => document.title") or sm.home_title
                sm.notes.append(f"homepage showed only one Shopify marker ({', '.join(first)}) on the first load; "
                                "Radar loaded it again and it is a Shopify store")
        except Exception:  # noqa: BLE001  the first verdict stands
            pass
    sm.platform, sm.platform_evidence = det["platform"], det["evidence"]
    if sm.access == "password":
        sm.notes.append("store is password-protected (Shopify storefront password); nothing can be tested")
        return sm
    if sm.access == "bot_blocked":
        sm.notes.append("bot protection challenge shown to Radar's identified browser; Radar does not evade it "
                        "(the store can allowlist the BugRadar User-Agent)")
        return sm
    if resp is None or resp.status >= 400:
        code = resp.status if resp else None
        # Not "not Shopify": the store did not serve its homepage to Radar (plumgoodness.com, bench 5: HTTP 423
        # "This store is unavailable" to Radar while the store was live for other visitors).
        sm.access = "refused" if code in (401, 403, 423, 429) else "unreachable"
        sm.notes.append(f"homepage returned HTTP {code or 'none'} ('{sm.home_title[:60]}') to Radar; nothing tested. "
                        + ("The store refused or rate-limited Radar's browser; a later run may differ."
                           if sm.access == "refused" else "The store's server did not serve its homepage."))
        return sm
    base_url = sm.base_url = f"{urlparse(sess.page.url).scheme}://{urlparse(sess.page.url).netloc}"
    th = sess.evaluate("() => { const t = window.Shopify && window.Shopify.theme; return t ? "
                       "{schema: t.schema_name || null, name: t.name || null} : null }")
    if th:
        sm.theme = th.get("schema") or th.get("name")
        if th.get("schema") and th.get("name") and th["name"] != th["schema"]:
            sm.theme = f"{th['schema']} (store copy: {th['name'][:40]})"
    sm.checkout_app = detect_checkout_app(html) if sm.platform == "shopify" else ""
    if sm.platform != "shopify":
        sm.notes.append("platform is not Shopify; v1 builds tests for Shopify only"
                        + (f" (homepage '{sm.home_title[:50]}', {len(html)} chars of HTML, Shopify markers found: "
                           f"{', '.join(sm.platform_evidence)}; 2 needed)" if sm.platform_evidence else ""))
        return sm

    # 3. navigation
    nav = [n for n in sess.evaluate(NAV_JS) if _same_origin(n["url"], base_url)]
    sm.nav = nav[:25]
    if not sm.nav:
        sm.notes.append("no header/nav links found")
    _discover_info_pages(sess, sm, base_url)

    # 4. collections
    try:
        data = sess.get_json(f"{base_url}/collections.json?limit=50")
        for c in data.get("collections", []):
            if c.get("handle") and c["handle"] != "frontpage" and (c.get("products_count") or 0) > 0:
                sm.collections.append(Collection(c["handle"], c.get("title", c["handle"]),
                                                 f"{base_url}/collections/{c['handle']}",
                                                 c.get("products_count")))
    except (RobotsBlocked, AssertionError, ValueError) as e:
        sm.notes.append(f"/collections.json unavailable ({str(e)[:80]}); using nav links")
    if not sm.collections:
        for n in sm.nav:
            h = _handle(n["url"], "collections")
            if h and h not in {c.handle for c in sm.collections}:
                sm.collections.append(Collection(h, n["text"], f"{base_url}/collections/{h}"))
    # Prefer what shoppers actually see (nav-linked), then the biggest collections; skip tiny ones.
    nav_handles = {_handle(n["url"], "collections") for n in sm.nav}
    big = [c for c in sm.collections if c.product_count is None or c.product_count >= 2] or sm.collections
    sm.collections = sorted(big, key=lambda c: (c.handle not in nav_handles, -(c.product_count or 0)))
    if not sm.collections:
        sm.collections.append(Collection("all", "All products", f"{base_url}/collections/all"))
        sm.notes.append("no collections discovered; falling back to /collections/all")

    # 5. products
    try:
        prods = products_from_json(sess.get_json(f"{base_url}/products.json?limit=50"), base_url, limit=20)
        sm.products = [Product(**p) for p in prods]
    except (RobotsBlocked, AssertionError, ValueError) as e:
        sm.notes.append(f"/products.json unavailable ({str(e)[:80]}); scraping product links")
    if not sm.products:
        handles = []
        for url in [base_url + "/"] + [c.url for c in sm.collections[:2]]:
            try:
                if sess.page.url.rstrip("/") != url.rstrip("/"):
                    sess.goto(url)
                for link in sess.evaluate(LINKS_JS, "/products/"):
                    h = _handle(link, "products")
                    if h and h not in handles and _same_origin(link, base_url):
                        handles.append(h)
            except (RobotsBlocked, Exception):  # noqa: BLE001
                continue
            if len(handles) >= 8:
                break
        for h in handles[:8]:
            try:
                p = product_from_js(sess.get_json(f"{base_url}/products/{h}.js"), base_url)
                if p:
                    sm.products.append(Product(**p))
            except (RobotsBlocked, AssertionError, ValueError):
                continue
        sm.products.sort(key=lambda p: not p.available)
    special = [p for p in sm.products if price_ok(p.price) and NOT_FOR_TESTS.search(f"{p.handle} {p.title}")]
    if special:
        sm.products = [p for p in sm.products if p not in special]
        sm.notes.append(f"{len(special)} gift-app clone / gift card / sample product(s) skipped "
                        f"(e.g. {special[0].handle})")
    token = token_priced(sm.products)
    if token:
        sm.products = [p for p in sm.products if p not in token]
        sm.notes.append(f"{len(token)} product(s) priced like a free gift next to the store's other prices "
                        f"(e.g. {token[0].handle} at {token[0].price}) are not used for tests")
    # Prefer products a shopper can reach: the ones listed in the collections Radar browses.
    listed: set[str] = set()
    for c in sm.collections[:2]:
        try:
            listed |= {x.get("handle") for x in sess.get_json(f"{c.url}/products.json?limit=50").get("products", [])}
        except (RobotsBlocked, AssertionError, ValueError, Exception):  # noqa: BLE001
            continue
    if listed and sm.products:
        unlisted = [p for p in sm.products if p.handle not in listed]
        sm.products.sort(key=lambda p: (p.handle not in listed, not p.available))
        if unlisted and len(unlisted) < len(sm.products):
            sm.notes.append(f"{len(unlisted)} product(s) not listed in the browsed collections are tested last "
                            f"(e.g. {unlisted[0].handle})")
    free = [p for p in sm.products if not price_ok(p.price)]
    if free:
        sm.notes.append(f"{len(free)} product(s) priced 0 (free samples/gifts, e.g. {free[0].handle}) "
                        "are not used for product or cart tests")
    if not sm.products:
        sm.notes.append("no products discovered; product and cart suites will be skipped")
    elif not any(p.available for p in sm.products):
        sm.notes.append("every sampled product is sold out; cart suite will be skipped")

    # 6. search
    try:
        if sess.page.url.rstrip("/") != base_url:
            sess.goto(base_url + "/")
        action = sess.evaluate(
            "() => { const f = document.querySelector(\"form[action*='search']\"); return f ? f.getAttribute('action') : null }")
        sm.search_path = urlparse(urljoin(base_url, action)).path if action else "/search"
        if not action:
            sm.notes.append("no search form found on homepage; assuming Shopify default /search")
    except Exception:  # noqa: BLE001
        sm.search_path = "/search"
    return sm
