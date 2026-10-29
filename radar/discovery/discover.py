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
from dataclasses import dataclass

from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from radar.core.browser import Session, RobotsBlocked
from radar.core.config import Settings, site_id_from_url
from radar.core.models import SiteMap, Product, Collection
from radar.core.network import NO_NETWORK_NOTE, online, reset_cache
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
    // sign-in links are not pages: Shopify's new customer accounts link '/customer_authentication/redirect' (shop077.example
    // new30t, 15 Oct: 404 for a robot, a login hand-off for a shopper) and /account/login are judged by the account test
    if (/\/(cart|account|search|checkout|customer_authentication|login|logout)(\/|$|\?)|#/.test(u.pathname + u.hash) || /\/products\//.test(u.pathname)) continue;
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

# Journey 15: links a shopper meets beyond the menu: the announcement bar, homepage sections (banners, 'shop now'
# tiles, 'read our story'), and the footer. Menu links and footer info pages have their own tests.
MORE_LINKS_JS = r"""() => { const out = [], seen = new Set();
  const add = (a, where) => { const h = a.href;
    if (!h || seen.has(h) || /^(javascript|mailto|tel):/i.test(h)) return; seen.add(h);
    out.push({where, text: (a.textContent || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 60), url: h}); };
  document.querySelectorAll('[class*="announcement" i] a[href], [id*="announcement" i] a[href]').forEach(a => add(a, 'announcement bar'));
  document.querySelectorAll('main a[href], [role="main"] a[href], #MainContent a[href]').forEach(a => {
    if (!a.closest('header, footer, nav, [role="navigation"], [role="dialog"]')) add(a, 'homepage section'); });
  document.querySelectorAll('footer a[href], [role=contentinfo] a[href], [id*="footer" i] a[href], [class*="footer" i] a[href]')
    .forEach(a => add(a, 'footer'));
  return out.slice(0, 300); }"""
# /cdn-cgi/: Cloudflare's own paths ('Email' footer links become /cdn-cgi/l/email-protection: shop047.example, 11 Oct)
NOT_MORE = re.compile(r"/(products|cart|account|search|checkout|policies)(/|$)|/apps/|^/(cdn|files|cdn-cgi)/|\.[a-z0-9]{2,5}$",
                      re.I)


# A control whose href is a broken 'javascript void(0)' (no colon: shop053.example 'Quick Shop', new31e) resolves to
# '/javascript%20void(0)': a script control, never a page
JS_HREF = re.compile(r"/javascript(%20|\s|:|$)", re.I)
# A slideshow's own dots / arrows are links on some themes ('Slide 1, current', 'Load slide 1' -> '/Hero-slideshow':
# shop006.example, new31b, both devices DEGRADED): carousel controls, never pages to open
SLIDE_CONTROL = re.compile(r"^\s*((load|go to|show|view) )?slide \d+|^\s*(next|previous|prev) slide|^\s*slide \d+ of \d+", re.I)


def _norm_url(u: str) -> str:
    p = urlparse(u)
    return ((p.hostname or "").lower().removeprefix("www.") + (p.path.rstrip("/") or "/")).lower()


def more_links(links: list[dict], base: str, known: list[str], per_place: int = 2, total: int = 6) -> list[dict]:
    """Links beyond the menu worth opening (journey 15): same store, a page (not a product / cart / account / search /
    policy / file / app link), not a menu link or a footer info page already tested, one per address; at most
    per_place from each of the announcement bar, homepage sections and footer, in that order. Pure, unit-tested."""
    seen = {_norm_url(k) for k in known} | {_norm_url(base + "/")}
    by: dict[str, list[dict]] = {}
    for link in links:
        url = (link.get("url") or "").split("#")[0]
        if not url or not _same_origin(url, base) or NOT_MORE.search(urlparse(url).path) \
                or NOT_INFO_TEXT.search(link.get("text") or "") or SLIDE_CONTROL.search(link.get("text") or "") \
                or JS_HREF.search(urlparse(url).path):
            continue
        key = _norm_url(url)
        if key in seen:
            continue
        seen.add(key)
        place = by.setdefault(link.get("where") or "page", [])
        if len(place) < per_place:
            place.append({"where": link.get("where") or "page", "text": (link.get("text") or "")[:60], "url": url})
    return [x for w in ("announcement bar", "homepage section", "footer") for x in by.get(w, [])][:total]


# Which footer link is which info page. Order matters: 'Shipping & Returns' is read as the returns page.
INFO_KINDS = (("refund", r"refund|return|exchange|cancell?ation"),
              ("shipping", r"shipping|delivery"),
              ("privacy", r"privacy"),
              ("terms", r"terms|conditions|\btos\b"),
              ("contact", r"contact"))
NOT_INFO = re.compile(r"/(products|collections|cart|account|search|checkout|blogs)(/|$)|/apps/|^/(cdn|files|cdn-cgi)/|"
                      r"\.[a-z0-9]{2,5}$", re.I)       # files (shop143.example links its 'Annual Return' PDF from the footer)
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


# + checkout add-ons sold as products: 'Free Returns + Package Protection' (saturn.health, new30m 14 Oct: NOT JUDGED on
#   both devices, its page has no buy button a shopper uses) and shipping / order protection or insurance apps;
#   extended-warranty add-ons (shop104.example new30u: '1 Year Extended Warranty for …' pages show no such product)
NOT_FOR_TESTS = re.compile(r"(_|-)clone|free[-_ ]?gift|gift[-_ ]?card|e[-_]?gift|sampler|tester|\bsample\b|dummy|test[-_ ]product|"
                           r"(package|shipping|order|returns?|delivery|parcel)[-_ ]?(protection|insurance)|route[-_ ]?protection|"
                           r"extended[-_ ]?warranty|warranty[-_ ]?(extension|plan|upgrade)|\b\d[-_ ]?years?[-_ ]?(extended[-_ ]?)?warranty|"
                           r"\b\d[-_ ]?years?[-_ ]?accidental[-_ ]?(damage[-_ ]?)?protection|accidental[-_ ]?damage[-_ ]?(protection|cover)",
                           re.I)


# A gift SKU by the store's own label: product type 'Freebies' or a tag that is exactly 'freebie' / 'free gift' / 'gwp'
# (shop145.example, full 36 13 Oct: 'Free Fabric', Rs 1,000, badge FREEBIES, no product image, was the one product tested ->
# degraded). Only the type or a WHOLE tag counts: 'free-gift-eligible' marks a real product that qualifies for an offer.
GIFT_KIND = re.compile(r"(^|\|)\s*(freebies?|free[-_ ]?gifts?|gift[-_ ]?with[-_ ]?purchase|gwp|complimentary[-_ ]?gifts?)\s*(\||$)",
                       re.I)


def token_priced(products: list, floor_share: float = 0.10, max_price: float = 50.0) -> list:
    """Products priced like a freebie SKU next to the store's real prices: at most 10% of the median
    priced product AND at most 50 (currency units). shop040.example lists a Rs 1 'Skincare Duo'
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


def dead_domain_reason(err: str, host: str = "") -> str:
    """Why a store's address does not lead to a working site, read from the network error of the robots.txt request;
    '' when the error may be temporary (timeout, 5xx, TLS reset). new30g (11 Oct): 6 of 30 never-seen addresses were dead
    (DNS, a domain pointing at 127.0.0.1, another site's certificate) and were reported as 'blocked … usually a temporary
    server problem'. Pure, unit-tested."""
    e = err or ""
    if re.search(r"ENOTFOUND|ERR_NAME_NOT_RESOLVED|NXDOMAIN", e):
        return "the domain does not resolve (DNS: no such host)"
    if "EAI_AGAIN" in e:
        return "the domain did not resolve (DNS lookup failed)"
    m = re.search(r"ECONNREFUSED (127\.0\.0\.1|::1|0\.0\.0\.0)", e)
    if m and not re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?|localhost", (host or "").lower()):   # a NAME that resolves to it
        return f"the domain points to {m.group(1)} (no server there), not to a store"
    m = re.search(r"does not match certificate's altnames.*?DNS:(?:\*\.)?([^,\s]+)", e)
    if m:
        return f"its HTTPS certificate belongs to another site ({m.group(1)}): the domain does not point at the store"
    if re.search(r"self[- ]signed certificate", e):
        return "its HTTPS certificate is self-signed: not a public store"
    if re.search(r"certificate has expired|CERT_DATE_INVALID", e):
        return "its HTTPS certificate has expired"
    return ""


def load_robots(sess: Session, base: str, s: Settings) -> Robots:
    """RFC 9309: 200 = rules; 4xx = unavailable (allow all); 5xx or no answer = unreachable (disallow all).
    Three tries, 2 s and 5 s apart, longer timeout each time, before calling it unreachable (bench 11: shop083.example
    and shop129.example had no answer on two quick tries and were blocked for the run; both answer normally)."""
    status, err = None, ""
    tries = ((10000, 2), (15000, 5), (20000, 0))
    for i, (timeout, gap) in enumerate(tries):
        try:
            r = sess.request_get(base + "/robots.txt", timeout=timeout, headers=sess.sign(base + "/robots.txt") or None)
            status = r.status
            if r.status == 200:
                return Robots(r.text(), s.user_agent)
            if 400 <= r.status < 500:
                return Robots(None, s.user_agent)
        except Exception as e:  # noqa: BLE001  DNS, TLS, timeout, reset
            status = None
            err = str(e).split("\n")[0][:160]   # evidence: which network error (shop026.example, shop133.example new30c)
        if gap:
            time.sleep(gap)
    rb = Robots(None, s.user_agent, unreachable=True)
    rb.status = status
    rb.error = "" if status else err
    return rb


HOME_CHECK_PAUSES = (20, 60)     # seconds between the homepage tries when robots.txt is silent (the homepage's 5xx rule)
# errors that mean Radar's OWN connection dropped while it asked (the store isn't to blame for these)
OWN_NETWORK = re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NETWORK_CHANGED|ERR_NETWORK_IO_SUSPENDED|ENETDOWN|ENETUNREACH")


def _short_error(e: Exception | str, cap: int = 60) -> str:
    """One network error, short and without brackets (the uploader keeps a bracketed tail as 'Technical detail')."""
    msg = str(e).splitlines()[0] if str(e) else type(e).__name__
    msg = re.sub(r"^no answer:\s*", "", msg)
    msg = re.sub(r"\b(?:apiRequestContext|APIRequestContext|page|Page)\.\w+:\s*", "", msg)   # 'apiRequestContext.get: '
    return re.sub(r"[()\[\]]", "", msg).strip()[:cap] or type(e).__name__


@dataclass
class StoreAnswer:
    """What store_answers found. up: the store's server answered (status = that answer). tries: each try, in order
    ('HTTP 503', 'Timeout 30000ms exceeded.', 'browser: HTTP 503'). offline: an error said Radar's own connection dropped."""
    up: bool
    status: int | None
    tries: list
    secs: float
    offline: bool = False
    challenge: bool = False


def _challenge(status: int, body: str) -> bool:
    """A bot-protection page sent with a 5xx (some firewalls answer their challenge with 503): the server is up."""
    m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    return detect_access(status, (m.group(1).strip() if m else ""), body or "", "/") == "bot_blocked"


def _browser_answer(sess: Session, url: str, s: Settings) -> tuple[int | None, str, str]:
    """The homepage asked once by Chromium itself (review, 22 Oct): a firewall can drop or reset Playwright's own HTTP
    client (it is not a browser; browser.py _get_json, 18 Oct) and an incomplete certificate chain fails there while
    Chromium completes it, so 'down' is never decided on that client alone. Only the page itself is let through (no
    scripts, images or styles: aborted), Radar waits for the answer's first bytes ('commit'), then leaves the page.
    Returns (status or None, error, body of a 5xx answer)."""
    page = sess.page

    def only_the_page(route):
        try:
            if route.request.resource_type == "document":
                route.fallback()          # on to the context's own routes (Web Bot Auth signing)
            else:
                route.abort()
        except Exception:  # noqa: BLE001  page closing under the route
            pass
    try:
        page.route("**/*", only_the_page)
    except Exception:  # noqa: BLE001
        pass
    try:
        resp = page.goto(url, wait_until="commit", timeout=s.home_timeout_ms)
        st = resp.status if resp is not None else None
        body = ""
        if st is not None and st >= 500:
            try:
                body = resp.text()[:200_000]
            except Exception:  # noqa: BLE001
                body = ""
        return st, "", body
    except Exception as e:  # noqa: BLE001  timeout, refused, reset, TLS
        return None, _short_error(e), ""
    finally:
        # leave the page BEFORE letting requests through again: after 'commit' the document is still arriving and
        # would load its pictures and scripts the moment the route is gone (caught by the e2e test, 22 Oct)
        try:
            page.goto("about:blank", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.unroute("**/*", only_the_page)
        except Exception:  # noqa: BLE001
            pass


def store_answers(sess: Session, base_url: str, s: Settings) -> StoreAnswer:
    """Is the whole store down? Asked only on daily checks (Settings.check_store_down) after robots.txt gave no answer.
    22 Oct: a store whose server times out or answers 5xx to everything fails at robots.txt, Radar's first request;
    RFC 9309 then means 'do not crawl', the run was 'blocked' (not tested) and the owner heard nothing about the biggest
    outage of all. An availability check, not crawling:
      1. one plain HTTP request for the homepage, no redirects followed, up to 3 times, HOME_CHECK_PAUSES apart, each
         given nav_timeout_ms (the status arrives with the first bytes: 4-13 s on Contabo's worst minute, 21 Oct);
      2. if none answered, the homepage once in Chromium itself (_browser_answer), given home_timeout_ms.
    Any answer below 500 (200, a redirect, 403 / 429 from bot protection) or a bot-protection page sent with a 5xx = the
    store's server is up."""
    url = base_url + "/"
    tries: list[str] = []
    t0 = time.time()
    done = lambda **kw: StoreAnswer(tries=tries, secs=round(time.time() - t0, 1), **kw)
    for i in range(len(HOME_CHECK_PAUSES) + 1):
        if i:
            sess.page.wait_for_timeout(int(HOME_CHECK_PAUSES[i - 1] * 1000 * sess.backoff_scale))
        try:
            r = sess.request_get(url, timeout=s.nav_timeout_ms, headers=sess.sign(url) or None,
                                              max_redirects=0)
            tries.append(f"HTTP {r.status}")
            if r.status < 500:
                return done(up=True, status=r.status)
            try:
                body = r.text()[:200_000]
            except Exception:  # noqa: BLE001
                body = ""
            if _challenge(r.status, body):
                return done(up=True, status=r.status, challenge=True)
        except Exception as e:  # noqa: BLE001  timeout, refused, reset, TLS
            tries.append(_short_error(e))
    st, err, body = _browser_answer(sess, url, s)
    tries.append("browser: " + (f"HTTP {st}" if st is not None else err))
    if st is not None and (st < 500 or _challenge(st, body)):
        return done(up=True, status=st, challenge=st >= 500)
    return done(up=False, status=None, offline=any(OWN_NETWORK.search(t) for t in tries))


def _tries(seen: list[str]) -> str:
    """['HTTP 503', 'HTTP 503', 'HTTP 503', 'browser: HTTP 503'] -> 'HTTP 503 x3, browser: HTTP 503': repeats in a row
    are counted, different answers listed in order."""
    out: list[list] = []
    for x in seen:
        if out and out[-1][0] == x:
            out[-1][1] += 1
        else:
            out.append([x, 1])
    return ", ".join(x + (f" x{n}" if n > 1 else "") for x, n in out)


def _span(secs: float) -> str:
    return f"{int(round(secs))} s" if secs < 90 else f"about {int(round(secs / 60))} minutes"


def down_note(ans: StoreAnswer, robots_why: str) -> str:
    """The store's line on the dashboard and in the 🔴 email: a plain sentence, then the detail in brackets (the
    uploader shows the bracketed tail under 'Technical detail'). Kept under 300 characters."""
    plain = (f"The store didn't answer at all: its homepage didn't load, tried {len(ans.tries)} times over "
             f"{_span(ans.secs)}")
    tail = f"homepage: {_tries(ans.tries)}; robots.txt: {robots_why}, tried 3 times"
    room = 295 - len(plain) - 3
    if len(tail) > room:
        tail = tail[:room - 1].rstrip(" ,;") + "…"
    return f"{plain} ({tail})"


def robots_down_home_up_note(why: str, ans: StoreAnswer) -> str:
    """robots.txt failing while the store's server answers: not tested (RFC 9309), never an outage. Under 300 chars."""
    head = (f"robots.txt could not be fetched ({why[:32].rstrip('. ')}, 3 tries), so nothing was tested: the robots.txt "
            "standard (RFC 9309) says 'do not crawl' then. ")
    if ans.status is not None and 200 <= ans.status < 300:
        return head + (f"The homepage answers (HTTP {ans.status}), so the store is open to shoppers; only robots.txt "
                       "fails, and search engines pause crawling while it does.")
    return head + (f"The store's server answers (HTTP {ans.status}"
                   + (", a bot-protection page" if ans.challenge else "") + "), so it is not down; only robots.txt fails.")


def _discover_info_pages(sess: Session, sm: SiteMap, base_url: str) -> None:
    """Footer policy / contact pages and the header account link, read from the homepage already open. Links the
    store's robots.txt disallows (some disallow /policies/ or /account) are never opened: they are listed in the notes
    instead, so the report says what was not tested and why."""
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
        sm.notes.append("footer info page(s) not opened, the store's robots.txt disallows them: "
                        + ", ".join(f"{p['kind']} {urlparse(p['url']).path}" for p in skipped))
    if not found:
        sm.notes.append("no policy or contact links found in the footer")
    try:
        extra = more_links(sess.evaluate(MORE_LINKS_JS) or [], base_url,
                           [n["url"] for n in sm.nav] + [p["url"] for p in found])
        blocked = [x for x in extra if not sess.allowed(x["url"])]
        sm.more_links = [x for x in extra if x not in blocked]
        if blocked:
            sm.notes.append("links beyond the menu not opened, robots.txt disallows them: "
                            + ", ".join(urlparse(x["url"]).path for x in blocked))
    except Exception:  # noqa: BLE001
        pass
    if acct:
        if not _same_origin(acct, base_url):
            sm.notes.append(f"account login is hosted elsewhere ({urlparse(acct).hostname}, Shopify customer accounts): "
                            "not opened")
        elif not sess.allowed(acct):
            sm.notes.append(f"account page {urlparse(acct).path} not opened: robots.txt disallows it")
        else:
            sm.account_url = acct.split("#")[0]


def _timed_out(e: Exception) -> bool:
    """A navigation that ran out of time: Playwright's own timeout, or Chromium's network timeouts."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    return isinstance(e, PlaywrightTimeout) or any(t in str(e) for t in ("ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT"))


class _Arrived:
    """The homepage's answer when the first navigation finished during the pause (Playwright gives no Response then)."""
    def __init__(self, status: int):
        self.status = status


# The store's own password page (23 Oct, daily checks only; tightened after the review the same day): every Shopify
# development store, and every store before its launch, shows /password to everyone. When the owner gave BugRadar the
# store password, Radar puts it in that ONE box and submits that ONE form, and only when ALL hold:
#   * the page Radar is on is the store's own host (exactly, not a parent domain), robots.txt allows /password;
#   * the form posts to that same host's /password, opens in the same page (no target=_blank / frame), and is one of
#     Shopify's two password forms: the theme's (form_type=storefront_password) or, on development stores in "private
#     mode", Shopify's own page (26 Oct, bugradar-dev: no form_type, an authenticity_token, the box named "password");
#   * after the post the browser is still on the store's own host.
# Nothing else on the page is touched (the "notify me" email box stays empty); no keystrokes or input events (a page
# script would see them); the value is set in the page because many themes keep the box in a closed pop-up.
# Outcomes, each with ONE note put first (notes[0] is the reason the dashboard shows; the Shopify page keys on them):
#   entered -> the store is tested (the cookie it set goes to every later check: executor -> Browser.store_cookies)
#   wrong   -> PASSWORD_WRONG_NOTE: the post came back to /password
#   unused  -> PASSWORD_UNUSED + why (no box, robots, another site, Shopify slowed / challenged Radar, an error)
PASSWORD_JS = r"""(a) => {
  if (location.host !== a.host) return 'other-site';
  const ok = (f) => {
    if (!f || f.method.toLowerCase() !== 'post' || (f.target && f.target !== '_self')) return false;
    const u = new URL(f.action, location.href);
    const els = [...f.elements];
    return u.host === a.host && /^\/password\/?$/.test(u.pathname)
      && (els.some((e) => e.name === 'form_type' && e.value === 'storefront_password')
          || els.some((e) => e.name === 'authenticity_token' && e.type === 'hidden'));
  };
  const box = [...document.querySelectorAll('input[type="password"]')].find((i) => i.name === 'password' && ok(i.form));
  if (!box) return 'no-box';
  box.value = a.pwd;
  if (box.form.requestSubmit) box.form.requestSubmit(); else box.form.submit();
  return 'sent';
}"""
NEEDED_NOTE = "store is password-protected (Shopify storefront password); nothing can be tested"
PASSWORD_WRONG_NOTE = ("The store password BugRadar has didn't open the store: its password page still shows. "
                       "Update the store password in BugRadar and the next check tests everything.")
PASSWORD_UNUSED = "The store password couldn't be used: "


def enter_store_password(sess: Session, base_url: str, s: Settings, sm: SiteMap):
    """Put the owner's store password in the store's own password form. Returns (outcome, response):
    ("entered", homepage response) | ("wrong", None) | ("unused", None) | ("stopped", None) — for "stopped" sm.access
    and the first note are already set (Shopify slowed / challenged Radar, or the store stopped answering)."""
    page = sess.page
    host = urlparse(base_url).netloc
    unused = lambda why: (sm.notes.insert(0, PASSWORD_UNUSED + why), ("unused", None))[1]   # noqa: E731
    try:
        if urlparse(page.url).netloc != host or urlparse(page.url).path.rstrip("/") != "/password":
            sess.goto(base_url + "/password", timeout_ms=s.nav_timeout_ms)
        if urlparse(page.url).netloc != host:
            return unused("the store's password page is on another site, so Radar didn't type it there.")
        if not sess.allowed(base_url + "/password"):
            return unused("robots.txt doesn't let Radar use the store's password page.")
        with page.expect_navigation(wait_until="domcontentloaded", timeout=s.nav_timeout_ms) as nav:
            sent = page.evaluate(PASSWORD_JS, {"pwd": s.store_password, "host": host})
            if sent != "sent":
                raise LookupError(sent)
        r = nav.value
        sess.settle()
    except LookupError as e:
        return unused("the password page changed to another site before Radar typed it." if str(e) == "other-site"
                      else "Radar couldn't find Shopify's password box on the store's password page.")
    except RobotsBlocked:
        return unused("robots.txt doesn't let Radar use the store's password page.")
    except Exception as e:  # noqa: BLE001  Playwright's error text never carries the evaluate argument (review)
        return unused(f"an error while entering it ({str(e).splitlines()[0][:80]}).")
    now = urlparse(page.url)
    if now.netloc != host:
        return unused("after entering it the store sent Radar to another site.")
    status = r.status if r else None
    html = sess.evaluate("() => document.documentElement.outerHTML") or ""
    title = sess.evaluate("() => document.title") or ""
    if status in (403, 429) or now.path.startswith("/challenge") or detect_access(status, title, html, now.path) == "bot_blocked":
        sm.access = "rate_limited" if status == 429 else "bot_blocked"
        sm.notes.insert(0, PASSWORD_UNUSED + ("Shopify slowed Radar down when it entered the store password; the next "
                                              "check tries again." if status == 429 else
                                              "Shopify asked Radar to prove it's human when it entered the store "
                                              "password, which Radar never does."))
        return "stopped", None
    if now.path.rstrip("/") == "/password":
        return "wrong", None
    sess.password_cookies = page.context.cookies(base_url)
    try:
        resp, _ = homepage(sess, base_url, s, sm)
    except RobotsBlocked:
        return unused("robots.txt doesn't let Radar open the homepage.")
    except Exception as e:  # noqa: BLE001  the store stopped answering right after the password worked
        msg = str(e).splitlines()[0][:120]
        sm.access = "no_network" if not online(s.net_probe_urls) else "unreachable"
        sm.notes.insert(0, NO_NETWORK_NOTE.format(where=f" (homepage after the store password: {msg})")
                        if sm.access == "no_network" else f"homepage unreachable after the store password: {msg}")
        return "stopped", None
    return "entered", resp


NAV_STATUS = "() => { const n = performance.getEntriesByType('navigation')[0]; return n && n.responseStatus ? n.responseStatus : null }"


def homepage(sess: Session, base_url: str, s: Settings, sm: SiteMap):
    """The store's homepage, patiently. 21 Oct: from the Contabo box a Shopify store's HTML took 2-21 s to be ready on a
    bad minute and twice that day over the 30 s page limit, so Radar called a working store 'unreachable' (a daily
    check's problem + 🔴 email for nothing). The homepage gets s.home_timeout_ms (60 s). After a timeout the page that is
    still arriving gets a short pause to finish (starting over would throw it away and ask a struggling store again);
    only if nothing arrived is it loaded once more, and a second timeout is the store's answer. sm.home_slow tells the
    worker the run was slow (its first look is then re-checked rather than alerted, radar/worker.py). DNS / refused /
    certificate errors are not retried: they don't get better in 10 s, and robots.txt already tried 3 times."""
    url = base_url + "/"
    try:
        return sess.goto(url, timeout_ms=s.home_timeout_ms)
    except RobotsBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        if not _timed_out(e):
            raise
        first = str(e).splitlines()[0][:100]
    sm.home_slow = True
    pause_ms, t0 = int(10000 * sess.backoff_scale), time.time()
    try:
        # the timed-out navigation keeps going in the browser; the page is still blank until its answer arrives
        sess.page.wait_for_url(lambda u: same_site(base_url, u), wait_until="domcontentloaded", timeout=max(pause_ms, 1))
        status = sess.evaluate(NAV_STATUS)
        if status:
            sess.settle()
            sm.notes.append(f"homepage took over {s.home_timeout_ms // 1000} s ({first}) and finished loading "
                            f"{round(time.time() - t0)} s later; a slow network or a slow store, not a store that is down")
            return _Arrived(int(status)), round(s.home_timeout_ms / 1000 + time.time() - t0, 2)
    except Exception:  # noqa: BLE001  nothing usable arrived: load it again below
        pass
    left = pause_ms - int((time.time() - t0) * 1000)
    if left > 0:
        sess.page.wait_for_timeout(left)
    out = sess.goto(url, timeout_ms=s.home_timeout_ms)
    sm.notes.append(f"homepage took over {s.home_timeout_ms // 1000} s on the first try ({first}) and loaded on the second; "
                    "a slow network or a slow store, not a store that is down")
    return out


def discover(sess: Session, base_url: str, s: Settings, pins: tuple[str, ...] = ()) -> SiteMap:
    """pins: case ids with an OPEN incident (product.pdp.<slug> / catalog.collection.<slug>). Their product or collection
    is tested again in this run whatever the sample picks; one that has left the catalog goes to sm.pins_gone (14 Oct)."""
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
        dead = dead_domain_reason(getattr(sess.robots, "error", ""), urlparse(base_url).hostname or "")
        if dead and not code:
            sm.access = "unreachable"
            sm.notes.append(f"{urlparse(base_url).hostname}: {dead}. Nothing was tested; check the store's address "
                            f"(robots.txt request: {getattr(sess.robots, 'error', '')[:140]})")
            return sm
        sm.access = "robots_unreachable"
        why = f"HTTP {code}" if code else ("no answer: " + getattr(sess.robots, "error", "") if getattr(sess.robots, "error", "") else "no answer")
        if s.check_store_down:
            # a daily check (22 Oct): is the whole store down? (store_answers) Free checks and benches keep 'blocked'.
            rwhy = f"HTTP {code}" if code else _short_error(getattr(sess.robots, "error", "") or "no answer")
            ans = store_answers(sess, base_url, s)
            if not ans.up:
                reset_cache()       # the window was minutes long: ask again now, not from the 30 s cache
                if ans.offline or not online(s.net_probe_urls):
                    sm.access = "no_network"
                    sm.notes.append(NO_NETWORK_NOTE.format(where=" while the store was checked"))
                    return sm
                sm.access = "unreachable"
                sm.notes.append(down_note(ans, rwhy))
                return sm
            sm.store_up_robots_down = ans.status
            sm.notes.append(robots_down_home_up_note(rwhy, ans))
            return sm
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
        resp, _ = homepage(sess, base_url, s, sm)
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
            resp, _ = sess.goto(base_url + "/", timeout_ms=s.home_timeout_ms)
        except Exception:  # noqa: BLE001
            break
    if waited and resp is not None and resp.status < 400:
        sm.notes.append(f"homepage answered HTTP 5xx at first and recovered after ~{waited} s (short server hiccup)")
    html = sess.evaluate("() => document.documentElement.outerHTML") or ""
    sm.home_title = sess.evaluate("() => document.title") or ""
    sm.access = detect_access(resp.status if resp else None, sm.home_title, html, urlparse(sess.page.url).path)
    if sm.access == "password" and s.store_password:
        outcome, after = enter_store_password(sess, base_url, s, sm)
        if outcome == "stopped":
            return sm
        if outcome == "wrong":
            sm.notes.insert(0, PASSWORD_WRONG_NOTE)
        if outcome == "entered":
            resp = after
            html = sess.evaluate("() => document.documentElement.outerHTML") or ""
            sm.home_title = sess.evaluate("() => document.title") or ""
            sm.access = detect_access(resp.status if resp else None, sm.home_title, html, urlparse(sess.page.url).path)
            if sm.access == "password":            # the cookie didn't hold: the same as a wrong password
                sess.password_cookies = None
                sm.notes.insert(0, PASSWORD_WRONG_NOTE)
            else:
                sm.notes.append("store is password-protected; Radar entered the store password its owner gave BugRadar")
    det = detect_platform(html)
    if det["platform"] != "shopify" and det["evidence"] and sm.access == "open" and resp is not None and resp.status < 400:
        # One Shopify marker but not two (shop120.example, new30f, 10 Oct: 'not Shopify' on one device and a healthy Shopify
        # store on the other). A store is one platform: load the homepage once more before saying it is not Shopify.
        first = list(det["evidence"])
        sess.page.wait_for_timeout(int(3000 * sess.backoff_scale))
        try:
            resp2, _ = sess.goto(base_url + "/", timeout_ms=s.home_timeout_ms)
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
        if s.store_password_unread and not s.store_password:
            sm.notes.insert(0, PASSWORD_UNUSED + "BugRadar couldn't read the saved password for this check; the next "
                                                 "check uses it.")
        elif not s.store_password:
            sm.notes.insert(0, NEEDED_NOTE)
        return sm
    if sm.access == "bot_blocked":
        sm.notes.append("bot protection challenge shown to Radar's identified browser; Radar does not evade it "
                        "(the store can allowlist the BugRadar User-Agent)")
        return sm
    if resp is None or resp.status >= 400:
        code = resp.status if resp else None
        # Not "not Shopify": the store did not serve its homepage to Radar (shop040.example, bench 5: HTTP 423
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
                           f"{', '.join(sm.platform_evidence)}; 2 needed)" if sm.platform_evidence else
                           f" (homepage {urlparse(sess.page.url).path or '/'} answered HTTP {resp.status}, title "
                           f"'{sm.home_title[:50]}', {len(html)} chars of HTML, no Shopify marker"
                           + ("" if sm.robots_loaded else ", no robots.txt: every Shopify store serves one") + ")"))
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
    _pin(sess, sm, base_url, pins)
    special = [p for p in sm.products if price_ok(p.price) and (NOT_FOR_TESTS.search(f"{p.handle} {p.title}")
                                                                or GIFT_KIND.search(p.kind))]
    if special:
        sm.products = [p for p in sm.products if p not in special]
        sm.notes.append(f"{len(special)} gift-app clone / gift card / sample / free-gift product(s) skipped "
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


def _pin(sess: Session, sm: SiteMap, base_url: str, pins: tuple[str, ...]) -> None:
    """Open incidents keyed by a product or collection (14 Oct, Anmol: option (a)): keep testing that exact product /
    collection every run until it passes. Case ids carry _slug(handle), which equals the handle for ordinary Shopify
    handles; a product the sample did not fetch is read from /products/<handle>.js. Not in the catalog any more =
    pins_gone (its incident is closed by the executor with that reason)."""
    from radar.generate.builder import _slug
    for cid in pins:
        if cid.startswith("product.pdp."):
            slug = cid[len("product.pdp."):]
            hit = next((p for p in sm.products if _slug(p.handle) == slug), None)
            if hit is None:
                try:
                    d = product_from_js(sess.get_json(f"{base_url}/products/{slug}.js"), base_url)
                    hit = Product(**d) if d else None
                    if hit:
                        sm.products.append(hit)
                except (RobotsBlocked, AssertionError, ValueError, Exception):  # noqa: BLE001
                    hit = None
            if hit:
                sm.pinned_products.append(hit.handle)
            else:
                sm.pins_gone.append(cid)
                sm.notes.append(f"open incident {cid}: product is no longer in the store's catalog (incident closed)")
        elif cid.startswith("catalog.collection."):
            slug = cid[len("catalog.collection."):]
            hit = next((c for c in sm.collections if _slug(c.handle) == slug), None)
            if hit:
                sm.pinned_collections.append(hit.handle)
            else:
                sm.pins_gone.append(cid)
                sm.notes.append(f"open incident {cid}: collection is no longer in the store's catalog (incident closed)")
