"""Checks library. A TestCase names a check; a check is a list of named steps; a step makes
explicit ASSERTIONS (what, expected, actual), all of which appear in the report.

Step kinds:
  hard (default)  a failed assertion fails the test case
  soft            a failed assertion is a WARNING (theme-dependent, or real but not shopper-blocking)

HARD RULES enforced here: never click checkout, never submit forms other than the store's
own add-to-cart, never create accounts, never enter personal data. Exceptions, inside the cart
flow only (stores that authorised Radar): journey 33 clicks 'Buy it now' and journey 35 clicks
the cart's checkout button under a third-party checkout app, ONLY to see the checkout render;
nothing is typed or submitted there, no OTP is requested, nothing is paid.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse, urljoin

from radar.core.browser import Session, RobotsBlocked, RateLimited
from radar.core.models import StepResult
from radar.discovery.shopify_data import (parse_product_data, price_ok, assess_add, parse_sent_variant_ids,
                                          rupees, norm_text, prices_in_text)
from radar.core.overlays import dismiss_overlays, AgeGate
from radar.healing.locator import Healer, QUICK_SEL, LocatorNotFound, OTHER_CARD_FN


class PincodeGate(Exception):
    """The store's buy button stays disabled until the shopper checks a delivery pincode (bombaysweetshop.com,
    new30d 10 Oct). Radar never types one (it submits no form but add-to-cart), so the cart cannot be tested:
    BLOCKED with the reason, never a store failure."""


class CatalogOnly(Exception):
    """The product page shows no price and no buy control, only links to marketplaces (antesports.com new30g,
    beyondsnack.in new30e: 'Buy on Amazon / Flipkart'). The store is a catalog; its own checkout cannot be tested:
    BLOCKED with the reason, never a store failure (Shopify's products.json still lists prices)."""


MARKETPLACE_JS = r"""() => { const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const scope = [...document.querySelectorAll('a[href]')].filter(a => vis(a) && !a.closest('footer, header, nav'));
  const mk = scope.filter(a => /(^|\.)(amazon\.(in|com)|amzn\.(in|to)|flipkart\.com|myntra\.com|nykaa\.com|ajio\.com|meesho\.com|jiomart\.com|tatacliq\.com|blinkit\.com|zeptonow\.com|swiggy\.com|bigbasket\.com)$/i
                               .test((() => { try { return new URL(a.href).hostname; } catch (e) { return ''; } })()))
                 .map(a => (a.innerText || a.getAttribute('aria-label') || new URL(a.href).hostname).trim().slice(0, 30));
  const own = [...document.querySelectorAll('form[action*="/cart/add"] button, form[action*="/cart/add"] input[type=submit], button, [role=button]')]
    .some(b => vis(b) && !b.closest('header, footer, nav') && /add to (cart|bag|basket)|buy (it )?now|^add$|^buy$/i.test((b.innerText || b.value || '').trim()));
  const main = document.querySelector('main, [role=main], #MainContent') || document.body;
  const so = ((main.innerText || '').match(/\b(shop (now )?on|available on|buy (it )?on|also available on)\s*:/i) || [])[0];
  const ask = (((document.body.innerText || '').match(/\b(price (up)?on request|request (a |the )?price|(enquire|inquire|call|contact us) for (the )?price|no longer (accepting|taking) (online )?orders|not (accepting|taking) online orders)\b/i) || [])[0]);
  return {marketplaces: [...new Set(mk)].slice(0, 4), own_buy: own, shop_on: so || null, ask: ask || null}; }"""


def _raise_if_catalog_only(ctx: Ctx, title: str = "") -> None:
    """Called only when the product's price is NOT on its page: prices converted for another country = location
    (OtherMarket); marketplace links and no buy control of its own = a catalog store (CatalogOnly). A page with its
    own buy button is judged as usual."""
    # Round 2 (cycle 28 re-run): theindianethnicco + saltattire still said 'not on page' with the rate test alone, so
    # foreign prices on the page itself count too: a $ / US$ / € / £ / AED price and no ₹ / Rs / INR price anywhere.
    # Round 3 (cycle 29 re-run): theindianethnicco shows '$62.91' under the product name while an offer block lower
    # down says 'Flat ₹250 off | Min. order value ₹2,500', so 'no ₹ anywhere' never held. The price block right under
    # the product name decides: a foreign price there and no ₹ price there = location.
    cur = ctx.sess.evaluate(r"""(title) => { const c = (window.Shopify || {}).currency || {};
        const t = document.body.innerText || '';
        const fx = /(?:US\$|CA\$|A\$|\$|€|£|AED|USD|EUR|GBP)\s?\d[\d,]*(?:\.\d\d)?/, rx = /(?:₹|Rs\.?|INR)\s?\d/;
        let foreign = rx.test(t) ? '' : ((t.match(fx) || [])[0] || '');
        const norm = x => (x || '').toLowerCase().replace(/\s+/g, ' ');
        const at = title ? norm(t).indexOf(norm(title)) : -1;
        if (!foreign && at >= 0) { const w = t.replace(/\s+/g, ' ').slice(at, at + title.length + 160);
          const m = w.match(fx); if (m && !rx.test(w.slice(0, w.indexOf(m[0])))) foreign = m[0]; }
        return {active: c.active || '', rate: parseFloat(c.rate || '1') || 1, country: (window.Shopify || {}).country || '',
                foreign}; }""", title) or {}
    active = (cur.get("active") or "").upper()
    if (active and active != "INR" and abs(cur.get("rate", 1) - 1) > 1e-6) or cur.get("foreign"):
        shown = f" (page shows {cur['foreign']!r})" if cur.get("foreign") else ""
        raise OtherMarket(f"location (US runner): the store shows this visitor prices in {active or 'another currency'}{shown}, "
                          f"market {cur.get('country') or '?'}, rate {cur.get('rate', 1):g}; the catalog price in ₹ is not on "
                          "the page. Radar's cloud machines are in the US: check from India")
    m = ctx.sess.evaluate(MARKETPLACE_JS) or {}
    if m.get("own_buy"):
        return
    if m.get("marketplaces"):
        raise CatalogOnly("not tested: the product page shows no price and no buy button, only links to "
                          f"marketplaces ({', '.join(m['marketplaces'])}): the store sells there, not on its own site")
    if m.get("shop_on"):         # beyondsnack 'Diwali Box': the 'Shop Now On:' heading with no marketplace listed yet
        raise CatalogOnly(f"not tested: the product page shows no price and no buy button, only {m['shop_on']!r}: "
                          "the store sells elsewhere, not on its own site")
    if m.get("ask"):             # houseofquadri / artoftimeindia 'Price on Request', banjarakasuti 'no longer accepting
        raise CatalogOnly(        # online orders' (new30n 14 Oct): the price is hidden on purpose, nothing to buy here
            f"not tested: the product page shows no price and no buy button, only {m['ask']!r}: the store does not "
            "sell this product online at a listed price")


class OtherMarket(Exception):
    """The store converts its prices for this visitor's country (Shopify Markets: window.Shopify.currency.rate is not 1,
    e.g. USD at 0.012 for a US visitor) and the catalog price in ₹ is not on the page (theindianethnicco.com $62.91,
    saltattire.com $24.23, new30h 11 Oct; tilfi.com $497, new30g). Radar's cloud machines are in the US, so this is
    'location (US runner)': BLOCKED with the reason, never a store failure."""


class StepFailed(Exception):
    def __init__(self, step: str, message: str, blocked: bool = False, not_judged: bool = False):
        self.step, self.blocked, self.not_judged = step, blocked, not_judged
        super().__init__(message)


class NotJudged(Exception):
    """Radar could not find what it needed on the page and holds NO positive evidence of breakage. The verdict rule
    (Anmol, 13 Oct): a store is failed only on positive evidence: an HTTP error, a 'page not found' page, the store's
    own data saying one thing while the page shows another, a control that is there but disabled or broken. 'Could not
    find it' is reported as NOT JUDGED with the evidence (what was looked for, what the page had), never as broken.
    The step is 'skip' ('not judged: ...'), the case 'blocked' (no retries, no triage), the screenshot is kept."""


class StoreWarning(AssertionError):
    """A finding a hard step reports as a WARNING: the step could judge the store and found a merchandising issue a
    shopper can get past (a collection page with no product grid while its sub-collections list products)."""


def _short(v, n=160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


class Steps:
    def __init__(self, shooter: Callable[[str], str | None] | None = None):
        self.items: list[StepResult] = []
        self._checks: list[dict] | None = None
        self.shooter = shooter          # set for journeys: a screenshot after every step, pass or fail

    def _shot(self, name: str) -> str | None:
        return self.shooter(name) if self.shooter else None

    def expect(self, what: str, expected, actual, ok: bool | None = None):
        """Record one assertion. Raises AssertionError('what: expected X, got Y') when it fails."""
        if ok is None:
            ok = expected == actual
        rec = {"what": what, "expected": _short(expected), "actual": _short(actual), "ok": bool(ok)}
        if self._checks is not None:
            self._checks.append(rec)
        if not ok:
            raise AssertionError(f"{what}: expected {rec['expected']}, got {rec['actual']}")
        return actual

    def run(self, name: str, fn: Callable, soft: bool = False):
        t0 = time.time()
        self._checks = []
        try:
            out = fn()
            healed = None
            if isinstance(out, tuple) and len(out) == 2 and (out[1] is None or isinstance(out[1], dict)):
                out, healed = out
            self.items.append(StepResult(name, "pass", out, None, round(time.time() - t0, 2), healed, self._checks,
                                         self._shot(name)))
            return out if out is not None else True
        except (RobotsBlocked, AgeGate, RateLimited, PincodeGate, CatalogOnly, OtherMarket) as e:
            self.items.append(StepResult(name, "skip", None, str(e), round(time.time() - t0, 2), None, self._checks))
            raise StepFailed(name, str(e), blocked=True) from e
        except NotJudged as e:
            msg = "not judged: " + str(e).split("\n")[0][:600]
            self.items.append(StepResult(name, "skip", None, msg, round(time.time() - t0, 2), None, self._checks,
                                         self._shot(name)))
            raise StepFailed(name, msg, blocked=True, not_judged=True) from e
        except Exception as e:  # noqa: BLE001
            msg = str(e).split("\n")[0][:400]
            soft = soft or isinstance(e, StoreWarning)
            self.items.append(StepResult(name, "warn" if soft else "fail", None, msg,
                                         round(time.time() - t0, 2), None, self._checks, self._shot(name)))
            if not soft:
                raise StepFailed(name, msg) from e
            return None
        finally:
            self._checks = None

    def info(self, name: str, detail):
        self.items.append(StepResult(name, "info", detail))


@dataclass
class Ctx:
    sess: Session
    healer: Healer
    steps: Steps
    redirected_to: str | None = None
    sent_add_requests: list[str] = field(default_factory=list)
    llm_assist: bool = False     # True only in the re-check after triage blamed Radar (see healing/triage.py)
    buy_evidence: str = ""       # which buy button _main_buy_button read (shown when it is disabled)
    layout_cache: tuple | None = None   # (url, layout verdicts): both layout steps read one measurement

    def expect(self, what, expected, actual, ok=None):
        return self.steps.expect(what, expected, actual, ok)


def same_site_url(a: str, b: str) -> bool:
    ha, hb = (urlparse(a).hostname or "").removeprefix("www."), (urlparse(b).hostname or "").removeprefix("www.")
    return bool(ha) and ha == hb


def _path(url: str) -> str:
    return urlparse(url).path.rstrip("/") or "/"


def _base(url: str) -> str:
    return re.match(r"https?://[^/]+", url).group(0)


def _handle(url: str) -> str | None:
    """The product handle in a URL. A trailing space or %20 (thechumbakwala.com, new30n 14 Oct: the card's href was
    '/products/custom-spiritual-desk-calendar ' and the browser opened the same page without it) is not part of it."""
    m = re.search(r"/products/([^/?#]+)", url)
    return m.group(1).replace("%20", " ").strip() or None if m else None


def _market_prefix(url: str) -> str:
    """Shopify Markets' locale / country path the page lives under ('/en-us', '/fr'), else ''. Product data asked
    under the same prefix carries that market's prices: amayrajewellery.com (new30n, 14 Oct, both devices) sent the
    US runner to /en-us/, where the page showed Small at ₹1,798,800 while the default market's .js said ₹1,499,000."""
    m = re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)(?=/|$)", urlparse(url).path.lower())
    return f"/{m.group(1)}" if m and m.group(1) not in ("cart", "apps") else ""


# ---------------- shared page assertions ----------------

def _dismiss(ctx: Ctx) -> None:
    """Close popups (newsletter / cookie: decline / location). Each one is recorded as a passed
    check so the report shows exactly what Radar closed. Raises AgeGate (-> blocked)."""
    for d in dismiss_overlays(ctx.sess.page):
        ctx.expect(f"{d['kind']} popup closed", "closed without accepting or subscribing",
                   f"clicked {d['button']!r} on: {d['text']!r}", True)


# What a shopper sees on the page: text and decoded images, also inside open shadow roots, plus what the browser itself
# painted. arakucoffee.in (cycle 34, both devices): the app-proxy quiz page /tools/perfect-product-finder/… answered 200
# with a title and a largest contentful paint at 1.6 s, yet document.body.innerText was empty and there was no <img>
# (an app draws it inside a web component): 'page not blank: got 0 chars, 0 images'. 'painted' = the largest paint the
# browser recorded (≥ 100x100 px), a visible iframe / canvas / video ≥ 200x150 px (an embedded app); else ''.
SHOWN_JS = r"""() => new Promise(done => {
  let text = ((document.body && document.body.innerText) || '').trim().length, imgs = 0;
  const big = (e, w, h) => { const r = e.getBoundingClientRect(); return r.width >= w && r.height >= h; };
  const walk = (root, deep) => { root.querySelectorAll('img').forEach(i => { if (i.getBoundingClientRect().width > 20 && i.naturalWidth > 0) imgs++; });
    if (deep) { const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      while (w.nextNode()) { const p = w.currentNode.parentElement;
        if (p && !/^(STYLE|SCRIPT|TEMPLATE)$/.test(p.tagName)) text += w.currentNode.textContent.replace(/\s+/g, ' ').trim().length; } }
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot, true); }); };
  walk(document, false);
  const emb = [...document.querySelectorAll('iframe, canvas, video, embed, object')].find(e => big(e, 200, 150));
  const out = paint => done({text, imgs, painted: emb ? 'a ' + Math.round(emb.getBoundingClientRect().width) + 'x' +
    Math.round(emb.getBoundingClientRect().height) + ' ' + emb.tagName.toLowerCase() : paint});
  if (text >= 40 || imgs > 0 || emb) return out('');
  try {
    const po = new PerformanceObserver(l => { const e = l.getEntries().pop(); po.disconnect();
      out(e && e.size >= 10000 ? 'content of ' + e.size + ' px² at ' + (e.startTime / 1000).toFixed(1) + ' s (largest contentful paint)' : ''); });
    po.observe({type: 'largest-contentful-paint', buffered: true});
    setTimeout(() => { po.disconnect(); out(''); }, 250);
  } catch (e) { out(''); }
})"""


def _goto_patiently(ctx: Ctx, url: str):
    """Open url; a page that does not even start rendering within the navigation timeout is opened once more with
    twice the wait before Radar calls it broken. bluenectar.co.in (cycle 34, desktop): three collection pages took
    9-33 s (one 'HTTP 200 in 32.78 s'), two hit the 30 s timeout on every attempt and were reported as broken links
    while the phone run, same store, same hour, opened all of them. A page that loads on the second try is a SLOW page
    (a warning naming the time), not a broken one; one that times out twice still fails as before."""
    try:
        return ctx.sess.goto(url)
    except Exception as e:  # noqa: BLE001
        if not re.search(r"Timeout \d+ms exceeded", str(e)):
            raise
        first = ctx.sess.s.nav_timeout_ms
        try:
            resp, secs = ctx.sess.goto(url, timeout_ms=2 * first)
        except Exception as e2:  # noqa: BLE001
            if re.search(r"Timeout \d+ms exceeded", str(e2)):
                raise AssertionError(f"page did not load: no page within {first // 1000} s, nor within {2 * first // 1000} s "
                                     f"on a second try ({str(e2).splitlines()[0][:80]})") from e2
            raise
        ctx.steps.items.append(StepResult(f"slow page {_path(url)[:40]}", "warn", None,
                                          f"slow page: {_path(url)[:80]} did not load within {first // 1000} s; it loaded on "
                                          f"a second try in {secs} s (shoppers on a slow connection may give up)"))
        return resp, secs


def _load(ctx: Ctx, url: str) -> str:
    resp, secs = _goto_patiently(ctx, url)
    status = resp.status if resp else ctx.sess.evaluate(
        "() => { const n = performance.getEntriesByType('navigation')[0]; return n && n.responseStatus ? n.responseStatus : null }")
    if status is None:      # served without a network response (cache / service worker / same-document)
        ctx.expect("HTTP status", "< 400", "no network response (cache or service worker); content checked below", True)
    else:
        if status == 429:                     # wait once (Shopify's limit resets in seconds), then give up as BLOCKED
            ctx.sess.page.wait_for_timeout(int(8000 * ctx.sess.backoff_scale))
            resp, secs = ctx.sess.goto(url)
            status = resp.status if resp else 200
        if status == 429:
            raise RateLimited(f"{_path(url)} answered HTTP 429 (Too Many Requests): the platform is rate-limiting Radar, "
                              "this is not a store failure")
        ctx.expect("HTTP status", "< 400", status, status < 400)
    title = (ctx.sess.evaluate("() => document.title") or "").strip()
    for _ in range(4):                      # some themes / apps set the title from a script after load
        if title:
            break
        ctx.sess.page.wait_for_timeout(500)
        title = (ctx.sess.evaluate("() => document.title") or "").strip()
    shown = ctx.sess.evaluate(SHOWN_JS)
    for _ in range(8):    # app pages (quiz / app-proxy pages) draw after load: up to ~4 s more before 'blank'
        if shown["text"] >= 40 or shown["imgs"] > 0 or shown["painted"]:
            break
        ctx.sess.page.wait_for_timeout(500)
        shown = ctx.sess.evaluate(SHOWN_JS)
    rendered = shown["text"] >= 40 or shown["imgs"] > 0 or bool(shown["painted"])
    # An empty <title> on a page that visibly rendered is an SEO finding (the SEO test 'title' reports it as a
    # warning), not a broken page: shoppers never see the tab title. hairoriginals.com (new30e held-out, 10 Oct: every
    # page fully rendered, document.title empty, both devices) and fablestreet.com (new30c) were 'down' for this alone.
    ctx.expect("page <title>", "not empty", title or ("(empty; the page itself rendered, so this is only an SEO "
                                                      "finding, see the SEO test)" if rendered else "(empty)"),
               bool(title) or rendered)
    ctx.expect("page not blank", "≥ 40 chars of text or an image",
               f"{shown['text']} chars, {shown['imgs']} images"
               + (f"; the browser painted {shown['painted']}" if shown["painted"] and shown["text"] < 40
                  and not shown["imgs"] else ""), rendered)
    _dismiss(ctx)
    final = ctx.sess.page.url
    ctx.redirected_to = _path(final) if _path(final) != _path(url) else None
    return f"HTTP {status} in {secs}s" + (f", redirected to {ctx.redirected_to}" if ctx.redirected_to else "")


def _broken_images(sess) -> list[str]:
    return sess.evaluate("""() => [...document.images]
        .filter(i => i.complete && i.naturalWidth === 0 && i.getBoundingClientRect().width > 0)
        .map(i => i.currentSrc || i.src).slice(0, 10)""")


# The box a shopper sees for a link. A link styled display:contents has no box of its own (0x0 rect) while its image,
# name and price render and take the click (valliyan.com, Baseline theme, new30j: 'products listed: 0', '15 links: 15
# not visible' under plainly visible cards). Its box is then the union of its children's boxes. The same holds for any
# link or wrapper with an empty box whose content still renders: a wrapper whose children all float or are positioned
# has zero height (valliyan.com re-test, cycle 34: the display:contents fix alone still gave '15 links: 15 not
# visible' under the same visible grid), so every empty box, not only display:contents, takes its children's union.
# Hidden content stays hidden: display:none children have no box, visibility/opacity are checked on the link.
# A 'stretched link' (valliyan.com, Baseline theme; seen by the first-refused evidence, 13 Oct): an empty inline
# <a class="increase-target"> (0x0, only a visually-hidden name inside) whose ::before is position:absolute over the
# whole card. A shopper taps the card and hits the link. Its box is its pseudo-element's box, placed in the nearest
# positioned ancestor (the card); a pseudo inside the link's own positioned box is not a stretch.
BOX_JS = r"""const stretchOf = e => { for (const p of ['::before', '::after']) { const s = getComputedStyle(e, p);
      if (!s.content || s.content === 'none' || s.content === 'normal' || s.position !== 'absolute') continue;
      const w = parseFloat(s.width), h = parseFloat(s.height); if (!(w > 2 && h > 2)) continue;
      let c = e; while (c && c !== document.body && getComputedStyle(c).position === 'static') c = c.parentElement;
      if (!c || c === e || c === document.body) continue;
      const cr = c.getBoundingClientRect(), l = parseFloat(s.left), t = parseFloat(s.top);
      return new DOMRect(cr.left + (isNaN(l) ? 0 : l), cr.top + (isNaN(t) ? 0 : t), w, h); }
    return null; };
  const boxOf = e => { const r = e.getBoundingClientRect();
    if ((r.width <= 2 || r.height <= 2) && getComputedStyle(e).display !== 'none') { const st = stretchOf(e); if (st) return st; }
    if ((r.width > 0 && r.height > 0) || getComputedStyle(e).display === 'none') return r;
    let l = Infinity, t = Infinity, R = -Infinity, B = -Infinity;
    for (const c of e.children) { const b = boxOf(c); if (b.width > 0 && b.height > 0) {
      l = Math.min(l, b.left); t = Math.min(t, b.top); R = Math.max(R, b.right); B = Math.max(B, b.bottom); } }
    return R > l ? new DOMRect(l, t, R - l, B - t) : r; };"""


def _product_links(sess) -> list[dict]:
    return sess.evaluate("""() => { const seen = new Map(); """ + BOX_JS + """
        for (const a of document.querySelectorAll("a[href*='/products/']")) {
          const r = boxOf(a); if (r.width === 0 || r.height === 0) continue;
          const h = (a.pathname.match(/\\/products\\/([^/?#]+)/) || [])[1];
          if (h && !seen.has(h)) seen.set(h, (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80));
        } return [...seen].map(([handle, text]) => ({handle, text})); }""")


def _images_ok(ctx: Ctx) -> str:
    broken = _broken_images(ctx.sess)
    ctx.expect("broken images", 0, len(broken), not broken)
    return "all visible images loaded"


def _js_errors(ctx: Ctx):
    errs = ctx.sess.console_errors
    ctx.expect("uncaught JavaScript errors", 0, f"{len(errs)}" + (f": {errs[0]}" if errs else ""), not errs)
    return "none"


def title_match(catalog: str, heading: str, page_text: str) -> tuple[bool, str]:
    """Catalog title vs what the page shows. Tolerant of spacing/punctuation ('50 ml' = '50ml') and of
    shorter display names. Pure, unit-tested."""
    compact = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    c, h, pg = compact(catalog), compact(heading), compact(page_text)
    if c and (c in pg or (h and (h in c or c in h))):
        return True, "same title"
    words = [w for w in re.findall(r"[a-z0-9]+", (catalog or "").lower()) if len(w) >= 3]
    have = [w for w in words if w in (heading or "").lower() or w in (page_text or "").lower()[:4000]]
    ratio = len(have) / len(words) if words else 0
    return ratio >= 0.6, f"{len(have)}/{len(words)} catalog words shown"


def _product_js(ctx: Ctx, url: str) -> dict:
    """The product's own data from Shopify: id, title, variants with ids and prices (paise)."""
    h = _handle(url)
    ctx.expect("URL is a product page", "/products/<handle>", _path(url), bool(h))
    try:
        p = ctx.sess.get_json(f"{_base(url)}{_market_prefix(url)}/products/{h}.js")
    except (RobotsBlocked, RateLimited):
        raise
    except Exception as first:  # noqa: BLE001
        # headless storefronts: palmonas.com (full 36 + re-test, 14 Oct) sends product pages to shop.palmonas.com,
        # whose /products/<h>.js answers the HTML page while the store's own domain answers the data; some URLs are a
        # SKU (/products/8904097333505), not the Shopify handle, which the page's canonical / og:url / ld+json names
        tried, p = [], None
        for b, a, why in _product_data_candidates(ctx, url, h):
            tried.append(f"{b}/products/{a}.js")
            try:
                q = ctx.sess.get_json(f"{b}/products/{a}.js")
            except (RobotsBlocked, RateLimited):
                raise
            except Exception:  # noqa: BLE001
                continue
            if q.get("variants"):
                p = q
                ctx.expect("product data via " + why, f"{_base(url)}/products/{h}.js answered no data",
                           f"{b}/products/{a}.js", True)
                break
        if p is None:
            raise AssertionError(f"{first}; no other source answered"
                                 + (f" (tried {', '.join(tried)})" if tried else " (the page names no other handle)")) from first
    ctx.expect("product data from Shopify", f"/products/{h}.js with variants",
               f"{p.get('title')!r}, {len(p.get('variants', []))} variant(s)", bool(p.get("variants")))
    return p


def _product_data_candidates(ctx: Ctx, url: str, h: str) -> list[tuple[str, str, str]]:
    """Other places Shopify's product data may answer when the page's own /products/<h>.js does not: the store's own
    domain (a headless storefront on a subdomain), then the handle the page itself names. At most 4. Pure apart from
    reading the page."""
    page_b, store = _base(url), getattr(ctx.sess, "store_url", None)
    bases = [page_b]
    hs, hp = ((urlparse(u).hostname or "").lower().removeprefix("www.") for u in (store or "", url))
    if store and _base(store) != page_b and hs and (hs == hp or hp.endswith("." + hs) or hs.endswith("." + hp)):
        bases.insert(0, _base(store))
    try:
        alts = [x for x in dict.fromkeys(_handle(u or "") for u in (ctx.sess.evaluate(PAGE_PRODUCT_URLS_JS) or [])) if x and x != h]
    except Exception:  # noqa: BLE001
        alts = []
    mp = _market_prefix(url)
    out = [(b + mp, h, "the store's own domain") for b in bases if b != page_b]
    out += [(b + mp, a, "the handle the page names") for a in alts[:2] for b in bases]
    if mp:
        out.append((page_b, h, "the default market's data (the market path answered none)"))
    return out[:5]


PAGE_PRODUCT_URLS_JS = r"""() => { const out = [document.querySelector('link[rel=canonical]')?.href,
    document.querySelector('meta[property="og:url"]')?.content];
  for (const sc of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const walk = o => { if (!o || typeof o !== 'object') return; if (Array.isArray(o)) return o.forEach(walk);
      if (/product/i.test(String(o['@type'] || ''))) { out.push(o.url, o['@id']); (o.offers ? [].concat(o.offers) : []).forEach(f => out.push(f && f.url)); }
      Object.values(o).forEach(v => typeof v === 'object' && walk(v)); }; walk(JSON.parse(sc.textContent)); } catch (e) {} }
  return out.filter(u => typeof u === 'string' && /\/products\//.test(u)).map(u => new URL(u, location.href).href); }"""


def _selected_variant(ctx: Ctx, product: dict) -> dict:
    """Variant currently selected on the page: the main form's id input, else the option the page visibly marks
    as chosen, else ?variant=, else first available. The page's own mark beats ?variant= because Radar opens
    product links WITH ?variant=<first available> itself, and headless pages ignore it (foxtale.in, 9 Oct)."""
    vids = [int(v["id"]) for v in product["variants"]]
    val = ctx.sess.evaluate("""(ids) => { const s = new Set(ids.map(String));
        for (const f of document.querySelectorAll('form[action*="/cart/add"]')) {
          const i = f.querySelector('[name="id"]'); if (i && s.has(String(i.value))) return i.value; }
        return null; }""", vids)
    m = re.search(r"[?&]variant=(\d+)", ctx.sess.page.url)
    vid = int(val) if val else (int(m.group(1)) if m else None)
    if not val:
        marked = _page_marked_variant(ctx, product)
        if marked:
            return marked
    v = next((x for x in product["variants"] if int(x["id"]) == vid), None)
    return v or next(
        (x for x in product["variants"] if x.get("available")), product["variants"][0])


SELECTED_OPTIONS_JS = r"""() => {
  // option labels the page itself marks as chosen: checked radios (and their labels), selected <option>s,
  // aria-checked / aria-pressed / aria-selected, or an active/selected/current class on a small control
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const t = e => (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('data-value') || '').trim();
  const out = new Set();
  document.querySelectorAll('input[type=radio]:checked').forEach(i => { out.add(i.value);
    const l = i.id && document.querySelector('label[for="' + CSS.escape(i.id) + '"]'); if (l) out.add(t(l)); });
  document.querySelectorAll('select option:checked').forEach(o => out.add(t(o)));
  document.querySelectorAll('[aria-checked="true"], [aria-pressed="true"], [aria-selected="true"], ' +
    'button[class*="active" i], button[class*="selected" i], [role="radio"][class*="active" i], ' +
    'li[class*="active" i], li[class*="selected" i], [class*="swatch" i][class*="active" i], [class*="swatch" i][class*="selected" i]')
    .forEach(e => { if (vis(e) && t(e).length <= 40 && !e.closest('header, nav, footer, [class*="cart" i]')) out.add(t(e)); });
  return [...out].filter(Boolean);
}"""


def _page_marked_variant(ctx: Ctx, product: dict) -> dict | None:
    """No cart form and no ?variant= (headless / app-built pages: foxtale.in pre-selects its 200g 'Best Value'
    size): the variant whose option values ALL match controls the page marks as chosen, and whose price is shown.
    Only an unambiguous match counts; otherwise None (caller falls back to the first available variant)."""
    try:
        chosen = {norm_text(x) for x in (ctx.sess.evaluate(SELECTED_OPTIONS_JS) or [])}
        shown = set(prices_in_text(ctx.sess.evaluate("() => document.body.innerText") or ""))
    except Exception:  # noqa: BLE001
        return None
    if len(product.get("variants") or []) < 2:
        return None

    def opts(x):
        vals = [x.get(k) for k in ("option1", "option2", "option3") if x.get(k)]
        return vals or [s for s in str(x.get("title") or "").split(" / ") if s and s != "Default Title"]
    price = lambda x: round(int(x["price"]) / 100, 2) if isinstance(x["price"], int) else round(float(x["price"]), 2)
    hits = [x for x in product["variants"] if opts(x) and all(norm_text(o) in chosen for o in opts(x))]
    hits = [x for x in hits if price(x) in shown] or hits
    return hits[0] if len(hits) == 1 else None


MAIN_BUY_JS = r"""([ids, quick]) => {
  const otherCard = __OTHER_CARD__;
  const s = new Set(ids.map(String)); const out = []; const seen = new Set();
  document.querySelectorAll('[data-radar-target="buy"]').forEach(e => e.removeAttribute('data-radar-target'));
  // every place that carries THIS product's variant id: a cart/add form, or a theme's own container
  // (soulflower.in: <form class="cart-form"><div class="quick-add-container"><input name="id">, no action)
  const holders = [...document.querySelectorAll('form[action*="/cart/add"]'),
                   ...[...document.querySelectorAll('input[name="id"], select[name="id"]')].map(i => i.closest('form') || i.parentElement)];
  for (const f of holders) {
    if (!f || seen.has(f)) continue; seen.add(f);
    const idEl = f.querySelector('[name="id"]'); const v = idEl ? String(idEl.value || '') : '';
    if (!s.has(v) || otherCard(f, quick) || (f.closest(quick) && otherCard(idEl, quick))) continue;
    let btns = [...f.querySelectorAll('button[type=submit], button:not([type]), input[type=submit], button[type=button]')]
      .filter(b => /add|cart|bag|buy/i.test((b.innerText || b.value || b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('name') || '') + ' ' + (b.className || '')));
    const fid = f.getAttribute('id') || '';          // not f.id: an <input name="id"> shadows it
    if (fid) btns = btns.concat([...document.querySelectorAll('[form="' + CSS.escape(fid) + '"]')]);
    for (const b of btns) {
      const r = b.getBoundingClientRect(); const st = getComputedStyle(b);
      if (!(r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none')) continue;
      out.push({b, form: fid || (f.tagName.toLowerCase() + '.' + String(f.className || '').split(' ')[0]), variant: v,
                main: /main|product-form|product_form/i.test(fid + ' ' + f.getAttribute('class')) ? 1 : 0,
                text: (b.innerText || b.value || '').trim().slice(0, 40), off: !!(b.disabled || b.getAttribute('aria-disabled') === 'true'),
                tag: b.tagName.toLowerCase() + (b.getAttribute('name') ? '[name=' + b.getAttribute('name') + ']' : '')
                     + (typeof b.className === 'string' && b.className.trim() ? '.' + b.className.trim().split(/\s+/)[0] : '')});
    }
  }
  // add-to-cart wording before 'buy it now' (that one goes to checkout, which Radar never does)
  const now = o => /buy\s*(it\s*)?now|checkout/i.test(o.text) && !/add|cart|bag/i.test(o.text) ? 1 : 0;
  out.sort((x, y) => (y.main - x.main) || (now(x) - now(y)));
  if (!out.length) return null;
  // The first pick is disabled but another ADD-TO-CART button for THIS product is enabled: a shopper uses the
  // enabled one (crossbeats.com mobile, new30e: disabled <button.cf-checkout> in the main form + an enabled one;
  // littleboxindia.com mobile: enabled sticky ADD TO CART). Never a 'buy it now' (that goes to checkout).
  if (out[0].off) {
    const alt = out.find(o => !o.off && /add|cart|bag/i.test(o.text) && !/buy\s*(it\s*)?now/i.test(o.text));
    if (alt) { const was = out[0]; out.splice(out.indexOf(alt), 1); out.unshift(alt);
               alt.skipped = was.tag + ' ' + JSON.stringify(was.text); }
  }
  out[0].b.setAttribute('data-radar-target', 'buy');
  // evidence for a 'buy button disabled' failure: which button was read, and whether another one for THIS product
  // is enabled (littleboxindia.com mobile, new30c + loop cycle 6: disabled pick, enabled sticky ADD TO CART on screen)
  return {form: out[0].form, variant: out[0].variant, text: out[0].text, candidates: out.length, tag: out[0].tag,
          skipped_disabled: out[0].skipped || '',
          enabled_others: out.slice(1).filter(o => !o.off).map(o => o.tag + ' ' + JSON.stringify(o.text)
                          + (o.main ? '' : ' (form ' + String(o.form).slice(-20) + ')')).slice(0, 3)};
}""".replace("__OTHER_CARD__", OTHER_CARD_FN)


# The page's OWN buy control when it is not a Shopify cart form button (nicobar.com, held-out run 7 Oct: a
# <div class="pdp-addtobag-btn" data-product-handle="saanjh-shawl-chartreuse"> "ADD TO BAG", no /cart/add form,
# and 10 "Add to Bag" buttons for OTHER products in recommendation cards on the same page). Owned when an attribute
# on it or an ancestor (4 levels) names THIS product (handle, product id or a variant id), or when it sits with the
# product's title and no link to another product is closer. Innermost match only; header/nav/footer/drawers ignored.
# Used ONLY when the page has no Shopify form/input carrying this product's variant id.
OWN_CONTROL_JS = r"""([handle, ids, pid, quick]) => {
  const otherCard = __OTHER_CARD__;
  document.querySelectorAll('[data-radar-target="buy"]').forEach(e => e.removeAttribute('data-radar-target'));
  const mine = new Set([String(handle), String(pid), ...ids.map(String)]);
  const words = /^(add to (bag|cart|basket)|buy( it)? now|add)$/i;
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const here = (location.pathname.match(/\/products\/([^/?#]+)/) || [])[1];
  const prodOf = a => ((a.getAttribute('href') || '').match(/\/products\/([^/?#]+)/) || [])[1];
  const named = e => { for (let a = e, k = 0; a && a !== document.body && k < 5; a = a.parentElement, k++)
      for (const at of a.attributes || []) if (mine.has(String(at.value).trim())) return at.name; return null; };
  const title = document.querySelector('h1');
  const withTitle = e => { if (!title) return false;
    for (let a = e.parentElement, k = 0; a && a !== document.body && k < 7; a = a.parentElement, k++) {
      const others = [...a.querySelectorAll('a[href*="/products/"]')].map(prodOf).filter(h => h && h !== here);
      if (others.length) return false;
      if (a.contains(title)) return true; }
    return false; };
  const all = [...document.querySelectorAll('button, a, [role="button"], input[type=submit], div, span')]
    .filter(e => words.test(txt(e)) && txt(e).length <= 25 && vis(e)
      && !e.closest('header, nav, footer, cart-drawer, [id*="cart-drawer" i], [class*="cart-drawer" i], [class*="mini-cart" i]')
      && !otherCard(e, quick));
  const inner = all.filter(e => !all.some(o => o !== e && e.contains(o)));
  for (const e of inner) {
    const by = named(e) ? `${named(e)} names this product` : (withTitle(e) ? 'next to the product title' : null);
    if (!by) continue;
    const t = e.closest('button, a, [role="button"], [onclick], [class*="btn" i], [class*="button" i]') || e;
    t.setAttribute('data-radar-target', 'buy');
    return {tag: t.tagName.toLowerCase() + (t.className && typeof t.className === 'string' ? '.' + t.className.trim().split(/\s+/)[0] : ''),
            text: txt(e).slice(0, 30), why: by};
  }
  return null; }""".replace("__OTHER_CARD__", OTHER_CARD_FN)


# The healer found a DISABLED buy control, but the page also shows an ENABLED add-to-cart for this product
# (littleboxindia.com mobile, new30c/d/e: a disabled button read by the healer, an enabled sticky ADD TO CART bar
# at the bottom of the screen). Only exact add-to-cart wording, visible, not in header/nav/footer/cart drawer, not
# on another product's card; never 'buy it now' (checkout). The cart check then proves what it really added.
ENABLED_ADD_JS = r"""([quick]) => {
  const otherCard = __OTHER_CARD__;
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const fixed = e => { for (let a = e; a && a !== document.body; a = a.parentElement) {
      const p = getComputedStyle(a).position; if (p === 'fixed' || p === 'sticky') return 1; } return 0; };
  const c = [...document.querySelectorAll('button, input[type=submit], [role="button"]')]
    .filter(e => /^add to (cart|bag|basket)$/i.test(txt(e)) && vis(e)
      && !(e.disabled || e.getAttribute('aria-disabled') === 'true')
      && !e.closest('header, nav, footer, cart-drawer, [id*="cart-drawer" i], [class*="cart-drawer" i], [class*="mini-cart" i]')
      && !otherCard(e, quick))
    .sort((x, y) => fixed(y) - fixed(x));
  if (!c.length) return null;
  document.querySelectorAll('[data-radar-target="buy"]').forEach(e => e.removeAttribute('data-radar-target'));
  const t = c[0]; t.setAttribute('data-radar-target', 'buy');
  return {tag: t.tagName.toLowerCase() + (typeof t.className === 'string' && t.className.trim() ? '.' + t.className.trim().split(/\s+/)[0] : ''),
          text: txt(t).slice(0, 30), sticky: !!fixed(t), count: c.length};
}""".replace("__OTHER_CARD__", OTHER_CARD_FN)


FORM_STATE_JS = r"""([ids, quick]) => { const s = new Set(ids.map(String)); const otherCard = __OTHER_CARD__;
  // The product's own form = one carrying one of THIS product's variant ids. A form is never picked
  // for its name alone: hidden cart-drawer / upsell forms are also called product-form / product_form
  // and come first in the DOM (boldcare.in drawer form for a free gift, bummer.in's 14 empty
  // NativeCartUpsell 'shopify-product-form's, bench 4). Visible beats hidden, main-named beats not.
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const own = [], named = [];
  for (const f of document.querySelectorAll('form[action*="/cart/add"]')) {
    if (otherCard(f, quick)) continue;
    const i = f.querySelector('[name="id"]'); if (!i) continue;
    const fid = f.getAttribute('id') || '';
    const b = f.querySelector('button[type=submit], button:not([type]), input[type=submit]') ||
              (fid && document.querySelector('[form="' + CSS.escape(fid) + '"]'));
    const row = {variant: String(i.value || ''), disabled: !!(b && (b.disabled || b.getAttribute('aria-disabled') === 'true')),
                 vis: vis(f) || !!(b && vis(b)), main: /main|product-form|product_form/i.test(fid + ' ' + f.getAttribute('class')) ? 1 : 0};
    if (s.has(row.variant)) own.push(row);
    else if (row.main && row.vis && !row.variant) named.push(row);   // a theme that empties id until an option is picked
  }
  const best = rows => rows.sort((x, y) => (y.vis - x.vis) || (y.main - x.main))[0];
  const r = best(own) || best(named);
  return r ? {variant: r.variant, disabled: r.disabled} : null; }""".replace("__OTHER_CARD__", OTHER_CARD_FN)

PINCODE_GATE_JS = r"""([ids, quick]) => { const s = new Set(ids.map(String));
  // The product's own (disabled) buy button, or a visible pincode box on the page, asks for a delivery pincode first
  // (bombaysweetshop.com 'PLEASE ENTER YOUR PINCODE TO CHECK AVAILABILITY' + 'ENTER YOUR PINCODE' box, new30d 10 Oct).
  const rx = /pin\s*-?\s*code|zip\s*code|postal\s*code|delivery\s+(location|availability)|check\s+(availability|delivery|serviceab)/i;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const txt = e => ((e.innerText || e.value || '') + ' ' + (e.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
  for (const f of document.querySelectorAll('form[action*="/cart/add"]')) {
    const i = f.querySelector('[name="id"]'); if (!i || !s.has(String(i.value || ''))) continue;
    const fid = f.getAttribute('id') || '';
    for (const b of [...f.querySelectorAll('button, input[type=submit]'), ...(fid ? document.querySelectorAll('[form="' + CSS.escape(fid) + '"]') : [])])
      if ((b.disabled || b.getAttribute('aria-disabled') === 'true') && rx.test(txt(b))) return 'buy button says ' + JSON.stringify(txt(b).slice(0, 80));
  }
  const box = [...document.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([type=checkbox])')]
    .find(e => vis(e) && !e.closest(quick) && !e.closest('header, footer, [class*="newsletter" i]')
               && rx.test([e.placeholder, e.name, e.id, e.getAttribute('aria-label'), e.className].join(' ')));
  return box ? 'page asks for a delivery pincode (' + JSON.stringify((box.placeholder || box.name || box.id || '').slice(0, 40)) + ' box)' : null; }"""

# Pick option values like a shopper. A control used for one value is never used again for the next one: a 'pack of 2'
# product shows the same sizes twice (two pickers), and the second 'S' is the second picker's. A value no control shows
# whole is tried by its ' / ' parts: theformalclub.in (cycle 34) has ONE option whose values are 'S / 38 / M / 40' and
# shows two size pickers labelled 'S', 'M', ... (one per shirt); the shopper clicks 'S' in each.
CHOOSE_JS = r"""([values, quick]) => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ok = e => !e.closest(quick);
  const done = [], used = new Set(), usedNames = new Set();
  const pick = v => { const low = v.toLowerCase();
    for (const sel of document.querySelectorAll('select')) {                       // dropdown pickers
      if (!ok(sel) || used.has(sel)) continue;
      const o = [...sel.options].find(o => o.value.trim().toLowerCase() === low || o.text.trim().toLowerCase() === low);
      if (o) { sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); used.add(sel); done.push('select ' + v); return true; }
    }
    for (const r of document.querySelectorAll('input[type="radio"]')) {           // pill / swatch radios
      if (!ok(r) || r.value.trim().toLowerCase() !== low || used.has(r) || (r.name && usedNames.has(r.name))) continue;
      const lab = (r.id && document.querySelector('label[for="' + CSS.escape(r.id) + '"]')) || r.closest('label');
      (lab && vis(lab) ? lab : r).click(); used.add(r); if (r.name) usedNames.add(r.name); done.push('radio ' + v); return true;
    }
    const btn = [...document.querySelectorAll('[data-value], [data-option-value], button, li, span')]
      .find(e => ok(e) && vis(e) && !used.has(e) && ![...used].some(u => u.contains(e) || e.contains(u))
              && e.closest('[class*="variant" i], [class*="swatch" i], [class*="option" i], variant-selects, variant-radios')
              && ((e.getAttribute('data-value') || e.getAttribute('data-option-value') || e.innerText || '').trim().toLowerCase() === low));
    if (btn) { btn.click(); used.add(btn); done.push('button ' + v); return true; }
    return false; };
  for (const val of values) {
    const v = String(val).trim();
    if (pick(v)) continue;
    const parts = v.split(' / ').map(x => x.trim()).filter(Boolean);
    if (parts.length > 1) parts.forEach(pick);
  }
  return done;
}"""


def _ensure_variant(ctx: Ctx, product: dict) -> dict:
    """Make sure a purchasable variant is selected on the page (themes often require choosing a
    size/colour first). Picks the first in-stock variant's options like a shopper would and asserts
    the product form now carries that variant."""
    ids = [int(v["id"]) for v in product["variants"]]
    st = ctx.sess.evaluate(FORM_STATE_JS, [ids, QUICK_SEL])
    target = next((v for v in product["variants"] if v.get("available")), None)
    n_avail = sum(bool(v.get("available")) for v in product["variants"])
    ctx.expect("an in-stock variant exists", "≥ 1 available variant",
               f"{n_avail} of {len(product['variants'])} available" if target else "none available", target is not None)
    if st is None:
        # custom/JS buy button with no Shopify product form: the page does not expose which variant is
        # selected. Do not guess a failure; the cart check afterwards proves what was really added.
        ctx.expect("selected variant readable from page", "product form", "no product form; cart check will verify",
                   True)
        return dict(target, _unverified=True)
    cur = next((v for v in product["variants"] if st["variant"] and int(v["id"]) == int(st["variant"])), None)
    if cur and cur.get("available") and not st["disabled"]:
        return cur
    if cur and cur.get("available"):        # right variant, button disabled: a delivery-pincode gate?
        gate = ctx.sess.evaluate(PINCODE_GATE_JS, [ids, QUICK_SEL])
        if gate:
            ctx.expect("variant selected like a shopper", f"{cur['id']} ({cur.get('title')})",
                       f"{cur['id']} (already selected); buy button disabled until the shopper checks a delivery pincode",
                       True)
            return dict(cur, _pincode_gate=gate)
    values = [target.get(k) for k in ("option1", "option2", "option3") if target.get(k)]
    picked = ctx.sess.evaluate(CHOOSE_JS, [values, QUICK_SEL]) if values else []
    ctx.sess.page.wait_for_timeout(700)
    st = ctx.sess.evaluate(FORM_STATE_JS, [ids, QUICK_SEL])
    if not picked and not (st and str(st["variant"]) == str(target["id"])):
        # No control on the page shows any of the variant's option values (an app-built picker, labels Radar cannot
        # tie to the values): Radar cannot pick like a shopper, which says nothing about the store. Not judged here;
        # the buy button check and the cart check (what really lands in the cart) still judge the page.
        ctx.expect("variant selected like a shopper", f"{target['id']} ({' / '.join(values) or target.get('title')})",
                   f"not judged: no option control on the page shows {' / '.join(values)!r}; the page's form holds "
                   f"{(st or {}).get('variant') or 'no variant yet'} (the cart check verifies what is added)", True)
        return dict(target, _unverified=True)
    got = st["variant"] if st else "(no product form)"
    right = bool(st) and str(st["variant"]) == str(target["id"])
    if right and st["disabled"]:
        gate = ctx.sess.evaluate(PINCODE_GATE_JS, [ids, QUICK_SEL])
        if gate:
            ctx.expect("variant selected like a shopper", f"{target['id']} ({' / '.join(values) or target.get('title')})",
                       f"{got} after choosing {', '.join(picked) or 'nothing'}; buy button disabled until the shopper "
                       f"checks a delivery pincode", True)
            return dict(target, _pincode_gate=gate)
    ctx.expect("variant selected like a shopper", f"{target['id']} ({' / '.join(values) or target.get('title')})",
               f"{got} after choosing {', '.join(picked) or 'nothing (no matching picker)'}"
               + ("; but the buy button is still disabled" if right and st["disabled"] else ""),
               right and not st["disabled"])
    return target


def _expect_buy_enabled(ctx: Ctx, loc):
    """'buy button enabled', and when it is NOT: name the button Radar read and any enabled one for the same
    product, so a disabled-button failure is diagnosable from run.json alone (littleboxindia.com mobile)."""
    en = loc.is_enabled()
    ctx.expect("buy button enabled", True, True if en else f"False ({ctx.buy_evidence or 'button found by the healer'})", en)


def _main_buy_button(ctx: Ctx, product: dict):
    """The product's OWN add-to-cart button: in a cart/add form whose variant belongs to this
    product, not inside a product card / quick-add / recommendation. Falls back to the healer."""
    page = ctx.sess.page
    ids = [int(v["id"]) for v in product["variants"]]
    found = ctx.sess.evaluate(MAIN_BUY_JS, [ids, QUICK_SEL])
    own, scrolled = None, False
    ctx.buy_evidence = ""
    # A page WITH a Shopify form for this product keeps the old path (healer for a renamed/moved button); the
    # page's-own-control rule is only for pages that have no such form at all (nicobar.com's div control).
    has_form = ctx.sess.evaluate("""(ids) => { const mine = new Set(ids.map(String));
        return [...document.querySelectorAll('[name="id"]')].some(i => mine.has(String(i.value || ''))); }""", ids)
    # Not visible yet? A shopper scrolls: themes show the buy button in a sticky bar only after scrolling
    # (true-elements.com, held-out run 7 Oct: the form's own button is hidden on desktop).
    for step in range(5):
        if found:
            break
        if not has_form:
            own = ctx.sess.evaluate(OWN_CONTROL_JS, [product.get("handle") or _handle(page.url) or "", ids,
                                                      product.get("id") or 0, QUICK_SEL])
        if own or step == 4:
            break
        page.evaluate("() => window.scrollBy(0, Math.round(innerHeight * 0.6))")
        page.wait_for_timeout(500)
        scrolled = True
        found = ctx.sess.evaluate(MAIN_BUY_JS, [ids, QUICK_SEL])
    if found:
        loc = page.locator('[data-radar-target="buy"]').first
        # enabled others FIRST: the evidence is clipped to 160 chars in run.json (crossbeats.com, loop cycle 8: the
        # enabled button's text was cut off behind a 47-char form id)
        ctx.buy_evidence = ((f"enabled for this product but not add-to-cart wording: {', '.join(found['enabled_others'])}; "
                             if found.get("enabled_others") else "")
                            + f"read <{found.get('tag') or 'button'}> {found['text']!r} in form #{found['form'][-24:]}")
        return loc, (f"main product form #{found['form']} (variant {found['variant']}, button {found['text']!r})"
                     + (" — shown only after scrolling (sticky bar)" if scrolled else "")
                     + (f"; skipped disabled {found['skipped_disabled']} (an enabled one for this product exists)"
                        if found.get("skipped_disabled") else "")), None
    if own:
        loc = page.locator('[data-radar-target="buy"]').first
        return loc, (f"the page's own buy control <{own['tag']}> {own['text']!r} ({own['why']}; no Shopify cart form,"
                     f" the cart check verifies what it adds)" + (" — shown after scrolling" if scrolled else "")), None
    try:
        f = ctx.healer.find(page, "add_to_cart")
    except LocatorNotFound as e:
        # Say exactly what is (not) there, so nobody has to guess whose problem it is (mcaffeine.com, bench 3).
        st = ctx.sess.evaluate("""(ids) => { const mine = new Set(ids.map(String)); let own = 0, other = 0;
            for (const f of document.querySelectorAll('form[action*="/cart/add"]')) {
              const v = (f.querySelector('[name="id"]') || {}).value; if (mine.has(String(v))) own++; else other++; }
            return {own, other}; }""", ids) or {"own": 0, "other": 0}
        # verdict rule (13 Oct): no buy control Radar can find is not proof there is none: NOT JUDGED with the facts
        raise NotJudged("add-to-cart control for THIS product on its page: "
                        + ("none: no add-to-cart form for this product" if not st["own"]
                           else f"{st['own']} add-to-cart form(s) for this product, but no buy button Radar could use")
                        + (f" ({st['other']} such forms belong to other products' cards)" if st["other"] else "")
                        + "; no other buy control found" + ("; LLM healing disabled" if "LLM healing disabled" in str(e) else "")) from e
    try:
        healed_on = f.locator.is_enabled()
    except Exception:  # noqa: BLE001  detached
        healed_on = True
    if not healed_on:
        alt = ctx.sess.evaluate(ENABLED_ADD_JS, [QUICK_SEL])
        ctx.buy_evidence = f"healer found a disabled control ({f.method}: {f.selector[:60]}); no enabled add-to-cart for this product on the page"
        if alt:
            return (page.locator('[data-radar-target="buy"]').first,
                    f"enabled <{alt['tag']}> {alt['text']!r}" + (" (sticky bar)" if alt["sticky"] else "")
                    + f"; the healer's pick ({f.selector[:50]}) is disabled — the cart check verifies what it adds", None)
    else:
        ctx.buy_evidence = f"healer: {f.method}: {f.selector[:60]}"
    return f.locator, f"{f.method}: {f.selector[:70]}", f.healed


def _capture_add_requests(ctx: Ctx):
    def on_req(r):
        try:
            if r.method == "POST" and re.search(r"/cart/add(\.js)?(\?|$)", r.url):
                ctx.sent_add_requests.append(r.post_data or "")
        except Exception:  # noqa: BLE001  binary multipart bodies
            ctx.sent_add_requests.append("")
    ctx.sess.page.on("request", on_req)


def _add_and_verify(ctx: Ctx, product: dict, variant: dict):
    """Click the product's own buy button and prove, from the store's /cart.js, that exactly this
    product was added once at its own price. Each claim is a separate recorded assertion."""
    if variant.get("_pincode_gate"):
        def gated():
            raise PincodeGate("cart not tested: the buy button stays disabled until the shopper checks a delivery "
                              f"pincode ({variant['_pincode_gate']}); Radar never enters one")
        ctx.steps.run("click_add_to_cart", gated)
    page = ctx.sess.page
    base = _base(page.url)
    before = ctx.sess.get_json(f"{base}/cart.js")

    def click():
        _dismiss(ctx)
        loc, how, healed = _main_buy_button(ctx, product)
        _expect_buy_enabled(ctx, loc)
        ctx.sent_add_requests.clear()
        loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
        try:
            loc.click(timeout=8000)
        except Exception:  # noqa: BLE001  late popup covering the button
            _dismiss(ctx)
            try:
                loc.click(timeout=8000)
            except Exception as e:  # noqa: BLE001
                m = re.search(r"<[^>]{0,160}>[^\n]{0,60}intercepts pointer events", str(e))
                raise AssertionError("buy button could not be clicked: " +
                                     (m.group(0)[:200] if m else str(e).splitlines()[0][:160])) from e
        return (f"clicked {how}", healed)
    ctx.steps.run("click_add_to_cart", click)

    state: dict = {}

    def verify():
        after = before
        for _ in range(14):                                   # cart updates are async
            page.wait_for_timeout(600)
            after = ctx.sess.get_json(f"{base}/cart.js")
            if (after.get("item_count") or 0) > (before.get("item_count") or 0):
                page.wait_for_timeout(800)                     # let gift/upsell apps finish
                after = ctx.sess.get_json(f"{base}/cart.js")
                break
        sent = [i for body in ctx.sent_add_requests for i in parse_sent_variant_ids(body)]
        exact = not variant.get("_unverified")
        r = assess_add(before, after, product, int(variant["id"]) if exact else None, sent)
        if not exact and r["target"]:                     # judge price against the variant actually added
            v_added = next((v for v in product["variants"] if int(v["id"]) == r["target"]["variant_id"]), None)
            if v_added:
                variant.clear()
                variant.update(v_added)
        state["r"] = r
        names = {int(v["id"]): v for v in product["variants"]}
        titles = {l["variant_id"]: f"{l['title']}, {rupees(l['price'])}" for l in r["added"]}
        titles.update({int(v["id"]): product["title"] for v in product["variants"]})
        if sent:
            # our product must be among what the page sent; anything else the page sent is judged
            # below by what it became in the cart (free gift = warning, paid product = failure)
            ctx.expect("variant the page sent to /cart/add",
                       f"{variant['id']} ({product['title']})" if exact else f"a variant of {product['title']}",
                       ", ".join(f"{i} ({titles.get(i, 'a different product')})" for i in sent),
                       (int(variant["id"]) in sent) if exact else any(i in {int(v['id']) for v in product['variants']} for i in sent))
        ctx.expect("cart item count", f"{before.get('item_count', 0)} → {before.get('item_count', 0) + 1}",
                   f"{before.get('item_count', 0)} → {after.get('item_count', 0)}",
                   (after.get("item_count") or 0) == (before.get("item_count") or 0) + 1 + sum(l["qty_delta"] for l in r["free_extras"]))
        got = r["target"]
        others = ", ".join(f"{l['title']} (variant {l['variant_id']})" for l in r["added"] if l is not got) or "nothing else"
        ctx.expect("product added to cart", f"{product['title']} (variant {variant['id']})",
                   f"{got['title']} (variant {got['variant_id']})" if got else f"NOT added; cart got: {others}",
                   got is not None)
        ctx.expect("quantity added", 1, got["qty_delta"])
        ctx.expect("unit price in cart", rupees(variant["price"]), rupees(got["price"]),
                   int(got["price"] or 0) == int(variant["price"]))
        ctx.expect("other paid products added", "none",
                   ", ".join(f"{l['title']} {rupees(l['price'])}" for l in r["paid_extras"]) or "none",
                   not r["paid_extras"])
        state["totals"] = (before.get("total_price"), after.get("total_price"))
        return f"cart has {product['title']!r} ×1 at {rupees(got['price'])}"
    ctx.steps.run("cart_received_this_product", verify)

    b_tot, a_tot = state.get("totals", (None, None))
    if b_tot is not None and a_tot is not None:
        # soft: an automatic discount legitimately changes the total
        ctx.steps.run("cart_total_increase", lambda: ctx.expect(
            "cart total went up by", rupees(variant["price"]), rupees(int(a_tot) - int(b_tot)),
            int(a_tot) - int(b_tot) == int(variant["price"])) and f"{rupees(b_tot)} → {rupees(a_tot)}", soft=True)

    if state.get("r") and state["r"]["free_extras"]:
        def gifts():
            ctx.expect("free items added automatically by the store (e.g. gift with purchase)",
                       "none, or intended by the store",
                       ", ".join(f"{l['title']} ({rupees(l['price'])})" for l in state["r"]["free_extras"]), False)
        ctx.steps.run("free_gift_added", gifts, soft=True)


DRAWER_JS = r"""() => {
  document.querySelectorAll('[data-radar-target="drawer"]').forEach(e => e.removeAttribute('data-radar-target'));
  // most specific first: a generic "active drawer" could be the menu
  const sels = ['cart-drawer', '#CartDrawer', '[id*="cart-drawer" i]', '[class*="cart-drawer" i]', 'cart-notification',
                'aside[class*="cart" i]', '[role="dialog"][aria-modal="true"]', '[class*="drawer" i][class*="active" i]'];
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 150 && r.height > 150 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  for (const e of sels.flatMap(s => [...document.querySelectorAll(s)])) {
    let t = vis(e) ? e : [...e.querySelectorAll('*')].find(c => vis(c) && c.children.length > 1);
    if (t && /cart|bag|basket|checkout/i.test(e.outerHTML.slice(0, 3000) + (e.innerText || '').slice(0, 500))) {
      t.setAttribute('data-radar-target', 'drawer'); return true; }
  }
  return false;
}"""


def _cart_view_and_checkout(ctx: Ctx, product: dict, cart_path: str):
    """Where the shopper now sees their cart (drawer if one opened, else the cart page): assert the
    product is listed and the checkout button is visible + enabled. Checkout is NEVER clicked."""
    page = ctx.sess.page
    base = _base(page.url)
    where: dict = {}

    def open_cart():
        page.wait_for_timeout(500)
        if ctx.sess.evaluate(DRAWER_JS):
            where["root"] = '[data-radar-target="drawer"]'
            text = page.locator(where["root"]).inner_text()
            how = "cart drawer opened after adding"
        else:
            link = None
            for sel in (f'header a[href$="{cart_path}"]', f'a[href$="{cart_path}"]'):
                cand = page.locator(sel)
                for i in range(min(cand.count(), 5)):
                    if cand.nth(i).is_visible():
                        link = cand.nth(i)
                        break
                if link:
                    break
            how = "cart page"
            if link is not None:
                try:
                    link.click(timeout=5000)
                    ctx.sess.settle()
                    how = "clicked cart icon"
                except Exception:  # noqa: BLE001
                    pass
            if ctx.sess.evaluate(DRAWER_JS):
                where["root"] = '[data-radar-target="drawer"]'
                text = page.locator(where["root"]).inner_text()
                how += " → cart drawer"
            else:
                if _path(page.url) != cart_path:
                    _load(ctx, base + cart_path)
                where["root"] = None
                text = ctx.sess.evaluate("() => document.body.innerText")
                how += " → cart page"
        ctx.expect("cart shows the product", product["title"], "listed" if norm_text(product["title"]) in norm_text(text)
                   else "not found in cart text", norm_text(product["title"]) in norm_text(text))
        return how
    ctx.steps.run("cart_shows_product", open_cart)

    def checkout():
        f = ctx.healer.find(page, "checkout_button", root=where.get("root"))
        ctx.expect("checkout button enabled", True, f.locator.is_enabled())
        return (f"checkout visible + enabled via {f.method} ({'drawer' if where.get('root') else 'cart page'}); NOT clicked",
                f.healed)
    ctx.steps.run("checkout_button_ready", checkout)


# ---------------- checks ----------------

def page_health(ctx: Ctx, url: str, max_load_secs: float = 8):
    ctx.steps.run("loads", lambda: _load(ctx, url))
    secs = ctx.sess.last_load_secs
    ctx.steps.run("load_time", lambda: ctx.expect("DOM ready time", f"≤ {max_load_secs}s", f"{secs}s",
                                                  secs <= max_load_secs) and f"{secs}s", soft=True)
    ctx.sess.page.wait_for_timeout(1500)   # let lazy images and scripts settle
    ctx.steps.run("images_load", lambda: _images_ok(ctx), soft=True)
    ctx.steps.run("no_js_errors", lambda: _js_errors(ctx), soft=True)
    layout_steps(ctx)


def links_resolve(ctx: Ctx, urls: list[str], max_links: int = 10):
    """Open each navigation link in the real browser: loads, not an error page, not blank."""
    bad, opened, skipped = [], 0, 0
    for u in urls[:max_links]:
        if not ctx.sess.allowed(u):
            skipped += 1
            continue
        opened += 1
        if ctx.steps.run(f"open {_path(u)[:40]}", lambda u=u: _load(ctx, u), soft=True) is None:
            bad.append(_path(u))
    ctx.steps.run("all_links_ok", lambda: ctx.expect(
        "broken navigation pages", 0, f"{len(bad)}" + (f": {', '.join(bad[:5])}" if bad else ""), not bad)
        and f"{opened} pages open and show content" + (f", {skipped} skipped by robots.txt" if skipped else ""))


PROMISES_PRODUCTS_JS = r"""() => { if (document.readyState !== 'complete') return 'still loading';
  const t = (document.body && document.body.innerText || '').slice(0, 20000);
  const m = t.match(/\b(\d{1,3}(?:,\d{3})*|\d+)\s+(products?|items?|results?|designs?|styles?)\b/i);
  return m && parseInt(m[1].replace(/,/g, ''), 10) > 0 ? m[0] : null; }"""


def _wait_for_products(ctx: Ctx, base_s: float = 6.0, long_s: float = 15.0) -> tuple[int, float]:
    """Product links on the page, waiting for a grid a script renders after load: up to 6 s (thehouseofrare phone,
    11 Oct), or up to 15 s while the page itself promises products (a '4,643 products' heading, or still loading).
    giva.co phone, full 36 run 38379683273: the collection app drew its grid after Radar's 6 s on a cold visit, four
    collections counted 0, the retry passed (flaky, store 'degraded'); the failure screenshot shows the cards."""
    n, waited, promise = len(_product_links(ctx.sess)), 0.0, None
    while not n:
        if waited >= base_s:
            promise = ctx.sess.evaluate(PROMISES_PRODUCTS_JS)
            if not promise or waited >= long_s:
                break
        ctx.sess.page.wait_for_timeout(500)
        waited += 0.5
        n = len(_product_links(ctx.sess))
    return n, waited


def _reveal_product_grid(ctx: Ctx) -> str:
    """No product link on a collection page yet: scroll down like a shopper, a screen at a time, until the grid shows.
    veirdo.in (new30h, desktop + phone): a 520 px sale banner, then '144 items' and a grid an app renders only when it
    scrolls into view; Radar never scrolled and said 'products listed: 0' (collection, journey, phone menu link)."""
    if _product_links(ctx.sess):
        return ""
    found = ctx.sess.evaluate("""async () => { """ + BOX_JS + """ const has = () => [...document.querySelectorAll("a[href*='/products/']")]
        .some(a => { const r = boxOf(a); return r.width > 0 && r.height > 0; });
      for (let y = 0; y <= Math.min(document.body.scrollHeight, 15000); y += Math.round(innerHeight * 0.7)) {
        scrollTo(0, y); for (let i = 0; i < 4; i++) { await new Promise(r => setTimeout(r, 250)); if (has()) return y; } }
      return -1; }""")
    return f" (the grid appeared after scrolling {found}px down)" if isinstance(found, (int, float)) and found >= 0 else ""


SUB_COLLECTIONS_JS = r"""() => { const here = location.pathname.replace(/\/$/, ''); const out = [];
  for (const a of document.querySelectorAll('a[href*="/collections/"]')) {
    if (a.closest('header, nav, footer, [role="navigation"]')) continue;
    const r = a.getBoundingClientRect(); if (r.width <= 2 || r.height <= 2) continue;
    let u; try { u = new URL(a.href); } catch (x) { continue; }
    const p = u.pathname.replace(/\/$/, '');
    if (u.origin !== location.origin || p === here || !/^\/collections\/[^/]+$/.test(p) || /\/frontpage$/.test(p) || out.includes(p)) continue;
    out.push(p); }
  return out.slice(0, 3); }"""


def _collection_data_titles(ctx: Ctx, url: str) -> list[str] | None:
    """The product titles the store's own collection data lists (up to 50), None when it cannot be read."""
    try:
        data = ctx.sess.get_json(url.split("?")[0].rstrip("/") + "/products.json?limit=50")
        ps = data.get("products") if isinstance(data, dict) else None
        return [str(p.get("title") or "") for p in ps] if isinstance(ps, list) else None
    except Exception:  # noqa: BLE001  robots.txt, no data, not JSON
        return None


def _collection_data_count(ctx: Ctx, url: str) -> int | None:
    """How many products the store's own collection data lists (up to 50), None when it cannot be read."""
    titles = _collection_data_titles(ctx, url)
    return None if titles is None else len(titles)


def _titles_on_page(ctx: Ctx, titles: list[str]) -> list[str]:
    """Which of the store's product titles a shopper can read on the page (visible text, case-insensitive, the first
    40 characters of each title). The second, independent signal behind 'the page shows no products': giva.co desktop
    (verdict-rule sanity run 38403594396) counted 0 product LINKS on a grid whose failure screenshot shows 8 cards with
    names and prices; the data said 50, Radar said the page showed none, and the page was right."""
    text = (ctx.sess.evaluate("() => (document.body && document.body.innerText || '').toLowerCase()") or "")
    out = []
    for t in titles:
        key = re.sub(r"\s+", " ", t.strip().lower())[:40]
        if len(key) >= 6 and key in text:
            out.append(t)
    return out


def _why_no_grid(ctx: Ctx, url: str) -> str | None:
    """A collection page that lists no products, judged as a shopper would. A WARNING (returned reason), not an outage:
    the collection is empty in the store's own data, or it is a landing page whose sub-collections list products
    (quboworld.com 'Smart Entrance', new30j: hero + 'Learn more' tiles, no grid; failed as 'products listed: 0' = DOWN).
    None: neither; the page should list products and does not (a real failure)."""
    try:
        data = ctx.sess.get_json(url.split("?")[0].rstrip("/") + "/products.json?limit=1")
        if isinstance(data, dict) and data.get("products") == []:
            return "the collection is empty in the store's own data (its products.json lists none)"
    except Exception:  # noqa: BLE001  robots.txt / no data: judge by the page alone
        pass
    subs = ctx.sess.evaluate(SUB_COLLECTIONS_JS) or []
    found = None
    for p in subs:
        try:
            ctx.sess.goto(urljoin(url, p))
        except Exception:  # noqa: BLE001
            continue
        n = len(_product_links(ctx.sess))
        if not n:
            ctx.sess.page.wait_for_timeout(1500)
            n = len(_product_links(ctx.sess))
        if n:
            found = f"{p}: {n} products"
            break
    if subs:
        try:
            ctx.sess.goto(url)          # the steps after this one judge the collection page itself
        except Exception:  # noqa: BLE001
            pass
    if found:
        return (f"a landing page with no product grid; it links to {len(subs)} collection(s) that list products "
                f"({found})")
    return None


NAME_TARGET_JS = r"""(name) => { const key = name.trim().toLowerCase().replace(/\s+/g, ' ').slice(0, 40);
  let best = null;
  for (const e of document.querySelectorAll('body *')) {
    if (e.children.length > 3) continue;
    const t = (e.innerText || '').trim().toLowerCase().replace(/\s+/g, ' ');
    if (!t.includes(key) || t.length > 200) continue;
    const r = e.getBoundingClientRect(); if (r.width < 4 || r.height < 4) continue;
    if (!best || t.length < best.t.length) best = {e, t}; }
  if (!best) return null;
  best.e.scrollIntoView({block: 'center'});
  const r = best.e.getBoundingClientRect(), e = best.e;
  const a = e.closest('a[href]'), cl = e.closest('[onclick], [role="button"], [role="link"], [data-url], [data-href]');
  return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
          what: `<${e.tagName.toLowerCase()}${e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/)[0] : ''}>`
            + (a ? ` in <a href="${a.getAttribute('href').slice(0, 60)}">` : '') + (cl && !a ? ` in <${cl.tagName.toLowerCase()} ${cl.getAttribute('role') ? 'role=' + cl.getAttribute('role') : 'clickable'}>` : '')}; }"""


def _tap_listed_name(ctx: Ctx, url: str, name: str) -> tuple[str | None, str]:
    """Cards Radar cannot read as product links (giva.co desktop, full 36 on 14 Oct, both times NOT JUDGED with all 50
    names on the page): tap a listed product's name as a shopper would. A product page opening = the listing works.
    Returns (the product path opened or None, evidence). The collection page is opened again afterwards."""
    page = ctx.sess.page
    try:
        t = ctx.sess.evaluate(NAME_TARGET_JS, name)
    except Exception as e:  # noqa: BLE001
        return None, f"could not read the name on the page ({str(e).splitlines()[0][:80]})"
    if not t:
        return None, "the name is in the page text but in no element a shopper can tap"
    before = page.url
    try:
        page.mouse.click(t["x"], t["y"])
        for _ in range(16):
            page.wait_for_timeout(500)
            if "/products/" in page.url:
                break
    except Exception:  # noqa: BLE001  a navigation mid-wait
        pass
    now = page.url
    opened = _path(now) if "/products/" in now and now != before else None
    if now != before:
        try:
            ctx.sess.goto(url)       # the steps after this one judge the collection page itself
        except Exception:  # noqa: BLE001
            pass
    return opened, (f"tapping the name ({t['what']}) opened {_path(now)}" if now != before
                    else f"tapping the name ({t['what']}) opened nothing")


def collection_page(ctx: Ctx, url: str):
    ctx.steps.run("loads", lambda: _load(ctx, url))

    def has_products():
        n, waited = _wait_for_products(ctx)
        scrolled = _reveal_product_grid(ctx) if not n else ""
        n = n or len(_product_links(ctx.sess))
        if not n:
            why = _why_no_grid(ctx, url)
            if why:
                raise StoreWarning(f"products listed: expected ≥ 1, got 0: {why}")
            n, again = _wait_for_products(ctx)      # the page was opened once more by _why_no_grid: judge what it shows now
            waited += again
        if not n:
            # verdict rule (13 Oct): the page shows no product link Radar recognises. Positive evidence = the store's
            # own collection data lists products AND none of their names is on the page; data unreadable, or the
            # names ARE on the page (cards Radar cannot read as links) = NOT JUDGED with the facts.
            titles = _collection_data_titles(ctx, url)
            if titles is None:
                raise NotJudged("products listed: no product link found on the page and the store's collection data "
                                "could not be read to confirm the collection has products")
            shown = _titles_on_page(ctx, titles)
            if shown:
                opened, how = _tap_listed_name(ctx, url, shown[0])
                if opened:
                    ctx.expect("a listed product opens when a shopper taps its name", "a product page",
                               f"{shown[0][:50]!r} → {opened}", True)
                    return (f"{len(shown)} of the collection's {len(titles)} products shown by name (cards a script "
                            f"opens, no product links); tapping {shown[0][:40]!r} opened {opened}")
                raise NotJudged(f"products listed: the page shows {len(shown)} of the collection's {len(titles)} products "
                                f"by name (e.g. {shown[0][:50]!r}) but no product link Radar recognises; {how}")
            ctx.expect("products listed", f"≥ 1 (the store's own data lists {len(titles)}, none of their names is on the page)",
                       n, n >= 1)
        ctx.expect("products listed", "≥ 1", n, n >= 1)
        return f"{n} products listed" + (f" (rendered {waited:.1f}s after the page loaded)" if waited else "") + scrolled
    ctx.steps.run("lists_products", has_products)
    ctx.sess.page.wait_for_timeout(1000)
    ctx.steps.run("images_load", lambda: _images_ok(ctx), soft=True)
    ctx.steps.run("no_js_errors", lambda: _js_errors(ctx), soft=True)       # journey 14: home, collection, product
    layout_steps(ctx)


# ---------------- journey 26: more products beyond the first page (pagination / load more / infinite scroll) ----------------

NEXT_PAGE_JS = r"""() => { const here = location.pathname.replace(/\/$/, '');
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const links = [...document.querySelectorAll('a[href*="page=2"], link[rel="next"], a[rel="next"]')].filter(a => {
    try { const u = new URL(a.href, location.href); return u.pathname.replace(/\/$/, '') === here && u.searchParams.get('page') === '2'; }
    catch (e) { return false; } });
  const a = links.find(l => l.tagName === 'A' && vis(l)) || links[0];
  return a ? {href: a.href, shown: a.tagName === 'A' && vis(a)} : null; }"""

LOAD_MORE_JS = r"""() => { document.querySelectorAll('[data-radar-more]').forEach(e => e.removeAttribute('data-radar-more'));
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && !e.disabled && e.getAttribute('aria-disabled') !== 'true'; };
  const b = [...document.querySelectorAll('button, a, [role="button"]')].filter(vis)
    .find(e => /^(load|show|view|see) more( products| items)?\b|^more products$/i.test((e.innerText || e.value || '').trim()));
  if (!b) return null; b.setAttribute('data-radar-more', '1'); return (b.innerText || '').trim().slice(0, 40); }"""


MORE_MIN_HIDDEN = 8      # in-stock products missing from page 1 before 'past the first page' is judged


def _handles(ctx: Ctx) -> set[str]:
    return {l["handle"] for l in (_product_links(ctx.sess) or [])}


def collection_more(ctx: Ctx, url: str, products_json: str):
    """Journey 26. A shopper must be able to see the products past the first page. Radar finds the store's own way:
    a page-2 link, a 'Load more' button, or infinite scroll, and checks that NEW products appear. Judged only when the
    collection holds more in-stock products than the first page shows (from the store's own products.json).
    Hard: a page-2 link that answers an error or a blank page. Soft: no new products / no way to see more."""
    ctx.steps.run("loads", lambda: _load(ctx, url))
    first = _handles(ctx)
    for i in range(12):        # cards that render late or on the first scroll (thehouseofrare phone: 0, then 20 on retry)
        if first:
            break
        if i == 2:
            ctx.sess.evaluate("() => scrollBy({top: Math.round(innerHeight * 0.6), behavior: 'instant'})")
        ctx.sess.page.wait_for_timeout(500)
        first = _handles(ctx)
    if not first:              # listing itself is judged by the collection test (journey 4), not here
        ctx.steps.info("more_products_not_judged", "no product cards on the first page after 6 s (the collection test "
                                                   "judges the listing)")
        return
    ctx.steps.info("lists_products", f"{len(first)} products on the first page")
    try:
        data = ctx.sess.get_json(products_json)
        avail = [p.get("handle") for p in (data or {}).get("products", [])
                 if any(v.get("available") for v in p.get("variants", []))]
    except Exception:  # noqa: BLE001  data unavailable: judge only by what the page offers
        avail = []
    nxt = ctx.sess.evaluate(NEXT_PAGE_JS)
    more = ctx.sess.evaluate(LOAD_MORE_JS)
    hidden = [h for h in avail if h not in first]
    if not nxt and not more and len(hidden) < max(MORE_MIN_HIDDEN, 0.25 * len(avail)):
        # a few in-stock products missing from page 1 is not 'pagination broken': themes hide some products, and on
        # GitHub's US machines Shopify Markets hides products not sold to the US (peepbeauty, supplysix: 6-8, 11 Oct)
        ctx.steps.info("more_products_not_judged", f"the first page shows the collection ({len(first)} shown, "
                                                   f"{len(avail)} in stock in the store's data, {len(hidden)} not shown)")
        return
    if nxt:
        ctx.steps.run("page_2_opens", lambda: _load(ctx, nxt["href"]))

        def page2_new():
            new = _handles(ctx) - first
            ctx.expect("new products on page 2", "≥ 1 product not on page 1", f"{len(new)}", len(new) >= 1)
            return f"page 2 shows {len(new)} more products" + ("" if nxt["shown"] else " (page-2 link found in the page head)")
        ctx.steps.run("page_2_shows_more", page2_new, soft=True)
        return

    def load_more():
        page = ctx.sess.page
        how = ""
        if more:
            _click(ctx, page.locator('[data-radar-more="1"]').first)
            how = f"clicked {more!r}"
        else:   # infinite scroll: scroll down like a shopper, a screen at a time (a jump to the bottom can skip the
                # loader's sentinel: giva.co, 226 in-stock products past the first page, 11 Oct), then once more at the end
            for i in range(30):
                at_end = ctx.sess.evaluate("() => { scrollBy({top: Math.round(innerHeight * 0.8), behavior: 'instant'}); "
                                           "return innerHeight + scrollY >= document.documentElement.scrollHeight - 4; }")
                page.wait_for_timeout(350)
                if _handles(ctx) - first:
                    break
                if at_end:
                    page.wait_for_timeout(1500)
                    if _handles(ctx) - first or i > 25:
                        break
                    later = ctx.sess.evaluate(LOAD_MORE_JS)       # a 'Load more' that appears only near the end
                    if later:
                        _click(ctx, page.locator('[data-radar-more="1"]').first)
                        page.wait_for_timeout(1500)
                        break
            how = "scrolled down the page"
        import time
        t0 = time.monotonic()
        new = _handles(ctx) - first
        while not new and time.monotonic() - t0 < 6:
            page.wait_for_timeout(500)
            new = _handles(ctx) - first
        ctx.expect("more products after " + ("'" + more + "'" if more else "scrolling"),
                   "≥ 1 product not shown before",
                   f"{len(new)}" + ("" if new else f"; the store's data has {len(hidden)} more in stock"), len(new) >= 1)
        return f"{how}: {len(new)} more products"
    ctx.steps.run("shows_more_products", load_more, soft=True)



# ---------------- journey 25: a collection filter narrows the products (sort only where robots.txt allows it) ----------------
# Shopify's storefront filtering (Online Store 2.0) names its inputs filter.v.availability, filter.v.price.gte/lte,
# filter.p.vendor, filter.p.product_type, filter.p.tag, filter.v.option.<name>. Shopify's default robots.txt disallows
# /collections/*sort_by*, /collections/*filter*&*filter* (two filters) and /collections/*+*, so Radar applies ONE filter,
# checks robots.txt for the exact address first, and sorts only on stores whose robots.txt allows it.

# On screen: has a size, and is not inside a closed <details> (Chrome keeps a size for the hidden content of a closed
# group, so the size alone says 'shown' for every option of Dawn's closed 'Availability' group)
FILTER_VIS_JS = r"""e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && !e.closest('details:not([open]) > :not(summary)')
      && (!e.checkVisibility || e.checkVisibility({visibilityProperty: true})); }"""

FILTER_OPTIONS_JS = r"""() => {
  document.querySelectorAll('[data-radar-filter]').forEach(e => e.removeAttribute('data-radar-filter'));
  const vis = FILTER_VIS;
  const out = []; let n = 0;
  for (const i of document.querySelectorAll('input[name^="filter."]')) {
    if (!/^(checkbox|radio)$/i.test(i.type) || i.disabled || i.checked) continue;
    const l = (i.id && document.querySelector('label[for="' + CSS.escape(i.id) + '"]')) || i.closest('label');
    i.setAttribute('data-radar-filter', String(n));
    out.push({i: n++, kind: 'input', name: i.name, value: i.value, shown: vis(i) || !!(l && vis(l)),
              label: ((l && (l.innerText || l.textContent)) || i.getAttribute('aria-label') || i.value || '').replace(/\s+/g, ' ').trim().slice(0, 60)});
  }
  for (const a of document.querySelectorAll('a[href*="filter."]')) {     // themes that render filters as links
    let u; try { u = new URL(a.href, location.href); } catch (e) { continue; }
    if (u.origin !== location.origin || u.pathname.replace(/\/$/, '') !== location.pathname.replace(/\/$/, '')) continue;
    const ps = [...u.searchParams].filter(([k, v]) => k.startsWith('filter.') && v);
    if (ps.length !== 1) continue;
    a.setAttribute('data-radar-filter', String(n));
    out.push({i: n++, kind: 'link', name: ps[0][0], value: ps[0][1], href: u.href, shown: vis(a),
              label: (a.innerText || a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 60)});
  }
  return out; }"""

# The address the theme would ask for if the shopper made this choice: the control's own GET form, and (Dawn on
# desktop sends them together) every facet / sort form on the page. The control is set only for the moment it takes
# to read the forms, then set back; no event fires, so nothing is sent.
PREDICT_JS = r"""([sel, value]) => { const el = document.querySelector(sel); if (!el) return null;
  if (el.tagName === 'A') return {own: el.href, all: el.href};
  const isGet = f => (f.getAttribute('method') || 'get').toLowerCase() === 'get';
  const was = el.tagName === 'SELECT' ? el.value : el.checked;
  if (el.tagName === 'SELECT') el.value = value; else el.checked = true;
  const q = forms => { const p = new URLSearchParams();
    for (const f of forms) for (const [k, v] of new FormData(f)) if (typeof v === 'string') p.append(k, v);
    return p.toString(); };
  const own = el.form ? [el.form] : [];
  const all = [...new Set([...own, ...[...document.querySelectorAll('form')].filter(f => isGet(f) &&
                f.querySelector('[name^="filter."], [name="sort_by"]'))])];
  const qo = q(own), qa = q(all);
  if (el.tagName === 'SELECT') el.value = was; else el.checked = was;
  let path = location.pathname;
  if (el.form && el.form.getAttribute('action')) { try { path = new URL(el.form.action, location.href).pathname; } catch (e) {} }
  const one = encodeURIComponent(el.name) + '=' + encodeURIComponent(value || el.value);
  const at = s => location.origin + path + '?' + (s || one);
  return {own: at(qo), all: at(qa)}; }"""

REVEAL_JS = r"""(sel) => { document.querySelectorAll('[data-radar-open]').forEach(e => e.removeAttribute('data-radar-open'));
  const vis = FILTER_VIS;
  const el = document.querySelector(sel); if (!el) return {gone: true};
  const l = el.tagName === 'INPUT' ? ((el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label')) : null;
  if (vis(el) || (l && vis(l))) return {shown: true};
  const closed = []; for (let d = el.parentElement; d; d = d.parentElement) if (d.tagName === 'DETAILS' && !d.open) closed.unshift(d);
  for (const d of closed) { const s = d.querySelector(':scope > summary');      // the outermost closed group on screen
    if (s && vis(s)) { s.setAttribute('data-radar-open', '1'); return {opener: (s.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40)}; } }
  const b = [...document.querySelectorAll('button, summary, a, [role="button"]')].filter(e => vis(e) && !e.hasAttribute('data-radar-tried'))
    .find(e => /^(filter|filters|filter (and|&) sort|filter by|sort (and|&) filter|refine|show filters)\b/i
                 .test((e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()));
  if (!b) return {};
  b.setAttribute('data-radar-open', '1'); b.setAttribute('data-radar-tried', '1');      // a phone's 'Filter' drawer button
  return {opener: (b.innerText || b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 40)}; }"""

CLICK_TARGET_JS = r"""(sel) => { document.querySelectorAll('[data-radar-pick]').forEach(e => e.removeAttribute('data-radar-pick'));
  const vis = FILTER_VIS;
  const el = document.querySelector(sel); if (!el) return false;
  const l = el.tagName === 'INPUT' ? ((el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label')) : null;
  (l && vis(l) ? l : el).setAttribute('data-radar-pick', '1'); return true; }"""

# 'Apply' of the facet form only: a GET form on a collection page (it changes the address of Radar's own page;
# nothing is sent to the store). Any other form is never submitted.
APPLY_JS = r"""(sel) => { const vis = FILTER_VIS;
  const el = document.querySelector(sel); const f = el && el.form;
  if (!f || (f.getAttribute('method') || 'get').toLowerCase() !== 'get') return null;
  try { if (!new URL(f.action || location.href, location.href).pathname.startsWith('/collections/')) return null; } catch (e) { return null; }
  const btns = [...f.querySelectorAll('button[type=submit], button:not([type]), input[type=submit]'),
                ...(f.id ? [...document.querySelectorAll('[form="' + CSS.escape(f.id) + '"]')] : [])];
  const b = btns.filter(vis).find(b => /apply|show|view|see|done|filter|result|update/i.test(b.innerText || b.value || b.getAttribute('aria-label') || ''));
  if (!b) return null; b.setAttribute('data-radar-apply', '1'); return (b.innerText || b.value || '').trim().slice(0, 30); }"""

SORT_JS = r"""() => { const s = [...document.querySelectorAll('select[name="sort_by"]')];
  document.querySelectorAll('[data-radar-sort]').forEach(e => e.removeAttribute('data-radar-sort'));
  const vis = FILTER_VIS;
  const ok = s.filter(e => [...e.options].some(o => o.value === 'price-ascending'));
  const el = ok.find(vis) || ok[0]; if (!el) return s.length ? {no_price: true} : null;
  el.setAttribute('data-radar-sort', '1'); return {label: [...el.options].find(o => o.value === 'price-ascending').text.trim()}; }"""
FILTER_OPTIONS_JS, REVEAL_JS, CLICK_TARGET_JS, APPLY_JS, SORT_JS = (
    js.replace("FILTER_VIS", FILTER_VIS_JS) for js in (FILTER_OPTIONS_JS, REVEAL_JS, CLICK_TARGET_JS, APPLY_JS, SORT_JS))


def applied_filters(url: str) -> list[tuple[str, str]]:
    """The storefront filters an address applies: its non-empty filter.* parameters (Dawn also sends the empty price
    boxes 'filter.v.price.gte=': not a filter). Pure, unit-tested."""
    from urllib.parse import parse_qsl
    return [(k, v) for k, v in parse_qsl(urlparse(url).query) if k.startswith("filter.") and v.strip()]


def one_filter_url(url: str, name: str, value: str) -> str:
    """The collection address with exactly one choice and nothing else (no sort_by, no empty fields). Pure."""
    from urllib.parse import urlencode
    return urlparse(url)._replace(query=urlencode({name: value}), fragment="").geturl()


def facet_count(label: str) -> int | None:
    """The product count a theme shows next to a filter option ('Small (43)', 'In stock (1,204)', with the invisible
    direction marks some themes add: maspar.com 'Small (43)\\u200e'), or None. Pure, unit-tested."""
    m = re.search(r"\(\s*(\d[\d,]*)\s*\)[\s‎‏​]*$", label or "")
    return int(m.group(1).replace(",", "")) if m else None


# Filter apps that draw a collection's grid themselves, from their own parameters rather than Shopify's filter.*
# address: Radar cannot drive them, so an empty grid under them is not judged. Read from the page's script addresses.
FILTER_APP_JS = r"""() => { const apps = [['Boost', /boost-?(sd|pfs)|bc-sf-filter|boostcommerce/i],
    ['Globo Smart Product Filter', /globo[\w.-]*filter|filter[\w.-]*globo/i], ['Searchanise', /searchanise/i],
    ['Klevu', /klevu/i], ['Algolia', /algolia/i], ['Fast Simon', /fastsimon|instantsearchplus/i],
    ['SearchPie', /searchpie/i], ['Doofinder', /doofinder/i]];
  const srcs = [...document.scripts].map(s => s.src || '').join(' ');
  const hit = apps.find(([, re]) => re.test(srcs));
  return hit ? hit[0] : null; }"""


def _storefront_stock(ctx: Ctx, base: str, h: str) -> tuple[bool | None, str]:
    """A product the 'In stock' filter lists while the collection's products.json calls it sold out: does the
    storefront sell it? Shopify's filter counts products that keep selling (inventory policy 'continue', untracked
    stock) as in stock, and products.json can lag or differ from this visitor's market (antinorm.co, beminimalist.co,
    11 Oct). Asked like the store: its /products/<h>.js first, then its own page. True: sold; False: the page itself
    says sold out (or its buy button is off); None: cannot tell (page hidden, redirected, no buy control found)."""
    try:
        p = ctx.sess.get_json(f"{base}/products/{h}.js") or {}
    except Exception:  # noqa: BLE001  data unavailable: the page decides
        p = {}
    vs = p.get("variants") or []
    if p.get("available") or any(v.get("available") for v in vs):
        return True, f"/products/{h}.js says available"
    url = f"{base}/products/{h}"
    if not vs or not ctx.sess.allowed(url):
        return None, "its product data and page could not be read"
    try:
        ctx.sess.goto(url)
        _dismiss(ctx)
        gone = _gone_product_page(ctx, url)
        if gone:
            return None, f"its page {gone}"
        b = _sold_out_button(ctx, p)
        words = sold_out_wording((ctx.sess.evaluate(SOLD_OUT_TEXT_JS, [QUICK_SEL]) or "") + " \n " + ((b or {}).get("text") or ""))
    except Exception as e:  # noqa: BLE001  (robots, rate limit, a page that would not open: not judged)
        return None, f"its page could not be read ({_short(str(e), 60)})"
    if words or (b and not b.get("enabled")):
        return False, (f"/products/{h}.js and its page say sold out"
                       + (f" ({words!r})" if words else f" (buy button {(b.get('text') or '').strip()[:30]!r} disabled)"))
    if b:
        return True, f"its page sells it (buy button {(b.get('text') or '').strip()[:30]!r} enabled)"
    return None, "its page shows no buy button and no sold-out words"


def pick_filter(options: list[dict]) -> dict | None:
    """Which filter a shopper-like test applies: 'In stock' first (the store's data says what is in stock), then a
    vendor / type / option / tag value (also in the data), else any other option. Never a price box, never an option
    the theme shows with a zero count. Pure, unit-tested."""
    def rank(o):
        n, v = o.get("name", ""), str(o.get("value", ""))
        if ".price." in n or facet_count(o.get("label") or "") == 0:
            return None
        if n == "filter.v.availability":
            return 0 if v == "1" else 9
        if n in ("filter.p.vendor", "filter.p.product_type", "filter.p.tag") or n.startswith("filter.v.option."):
            return 1
        return 2
    ranked = sorted((r, k) for k, o in enumerate(options) if (r := rank(o)) is not None)
    return options[ranked[0][1]] if ranked else None


def filter_matches(product: dict, name: str, value: str) -> bool | None:
    """Does a product from the store's products.json match one storefront filter? None: the data cannot tell (a
    metafield, a price range, 'Out of stock'). Pure, unit-tested."""
    vs = product.get("variants") or []
    want = norm_text(value)
    if name == "filter.v.availability":
        return any(v.get("available") for v in vs) if str(value) == "1" else None
    if name.startswith("filter.v.option."):
        return any(want == norm_text(str(v.get(k) or "")) for v in vs for k in ("option1", "option2", "option3"))
    if name in ("filter.p.vendor", "filter.p.product_type"):
        key = name.rsplit(".", 1)[1]
        return norm_text(str(product[key])) == want if key in product else None
    if name == "filter.p.tag" and "tags" in product:
        tags = product["tags"] if isinstance(product["tags"], list) else str(product["tags"]).split(",")
        return want in {norm_text(t) for t in tags}
    return None


def price_order_ok(handles: list[str], prices: dict[str, float]) -> tuple[bool, str]:
    """'Price, low to high': the listed products (those the data knows) never get cheaper further down. Pure."""
    seq = [(h, prices[h]) for h in handles if h in prices]
    bad = next(((a, b) for a, b in zip(seq, seq[1:]) if b[1] < a[1] - 0.005), None)
    if bad:
        return False, f"{bad[1][0]} (₹{bad[1][1]:,.2f}) listed after {bad[0][0]} (₹{bad[0][1]:,.2f})"
    return (True, f"{len(seq)} products from ₹{seq[0][1]:,.2f} to ₹{seq[-1][1]:,.2f}") if seq else (False, "no known products listed")


def _listed(ctx: Ctx) -> list[str]:
    return [l["handle"] for l in (_product_links(ctx.sess) or [])]


def _has_param(name: str, value: str) -> Callable[[str], bool]:
    from urllib.parse import parse_qsl
    return lambda u: (name, value) in parse_qsl(urlparse(u).query)


def _reveal(ctx: Ctx, sel: str) -> tuple[bool, list[str]]:
    """Open what hides a control, like a shopper: a closed group ('Availability') or a phone's 'Filter' drawer button."""
    how: list[str] = []
    r: dict = {}
    for _ in range(6):
        r = ctx.sess.evaluate(REVEAL_JS, sel) or {}
        if r.get("shown") or r.get("gone") or not r.get("opener"):
            break
        _click(ctx, ctx.sess.page.locator('[data-radar-open="1"]').first)
        ctx.sess.page.wait_for_timeout(400)
        how.append(f"opened {r['opener']!r}")
    return bool(r.get("shown")), how


def _settle_grid(ctx: Ctx, has: Callable[[str], bool], secs: float = 8) -> bool:
    """Wait for the address to carry the choice, then for the grid to stop changing (themes swap it in by script)."""
    page, t0 = ctx.sess.page, time.monotonic()
    while not has(page.url) and time.monotonic() - t0 < secs:
        page.wait_for_timeout(300)
    if not has(page.url):
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:  # noqa: BLE001  (a chat widget keeps the network busy: the steady grid below decides)
        pass
    last, same, t1 = None, 0, time.monotonic()
    while same < 3 and time.monotonic() - t1 < 6:
        now = _listed(ctx)
        same, last = (same + 1 if now == last else 0), now
        page.wait_for_timeout(500)
    return True


def _choose(ctx: Ctx, sel: str, name: str, value: str, label: str, is_select: bool = False) -> str:
    """Make one choice (a filter option or a sort order) the store's own way, or open its one-choice address directly
    when the theme's request would carry something robots.txt disallows (Dawn adds sort_by and empty price boxes).
    Raises RobotsBlocked when the one-choice address is disallowed too."""
    page = ctx.sess.page
    has = _has_param(name, value)
    pred = ctx.sess.evaluate(PREDICT_JS, [sel, value]) or {}
    direct = one_filter_url(page.url, name, value)
    if pred and ctx.sess.allowed(pred["all"]) and ctx.sess.allowed(pred["own"]):
        shown, how = _reveal(ctx, sel)
        if shown:
            if is_select:
                page.locator(sel).first.select_option(value, timeout=6000)
                what = f"chose {label!r}"
            else:
                ctx.sess.evaluate(CLICK_TARGET_JS, sel)
                _click(ctx, page.locator('[data-radar-pick="1"]').first)
                what = f"clicked {label!r}"
            if not _settle_grid(ctx, has, 5):
                btn = None if is_select else ctx.sess.evaluate(APPLY_JS, sel)
                if btn:
                    _click(ctx, page.locator('[data-radar-apply="1"]').first)
                    what += f", pressed {btn!r}"
                    _settle_grid(ctx, has, 8)
            return ", ".join(how + [what])
        why = f"{label!r} could not be brought on screen" + (f" ({', '.join(how)})" if how else "")
    else:
        why = (f"the theme's own request (?{_short(pred.get('all', '').split('?', 1)[-1], 90)}) is disallowed by robots.txt"
               if pred else "the control left the page")
    if not ctx.sess.allowed(direct):
        raise RobotsBlocked(f"robots.txt disallows {direct}")
    _load(ctx, direct)
    _settle_grid(ctx, has, 4)
    return f"opened {_path(direct)}?{urlparse(direct).query} directly: {why}"


def collection_filter(ctx: Ctx, url: str, products_json: str, strict: bool = False):
    """Journey 25. Can a shopper narrow a collection? Radar finds the theme's filters (Shopify storefront filtering:
    filter.* inputs, often in a closed group or behind a phone's 'Filter' button), applies ONE option like a shopper
    ('In stock' first), and checks that the address carries exactly that filter and that the listed products match it
    in the store's own products.json. Sorting is checked only where robots.txt allows sort_by (Shopify's default
    robots.txt does not). No filters at all is information, not a failure. Warnings unless strict (not yet measured
    on a bench). Read-only: only the facet GET form, which changes the address of Radar's own page."""
    soft = not strict
    ctx.steps.run("loads", lambda: _load(ctx, url))
    first = _listed(ctx)
    for _ in range(8):                       # grids a script renders after load
        if first:
            break
        ctx.sess.page.wait_for_timeout(500)
        first = _listed(ctx)
    if not first:
        ctx.steps.info("filter_not_judged", "no product cards on the collection page (the collection test judges the listing)")
        return
    try:
        data = {p.get("handle"): p for p in (ctx.sess.get_json(products_json) or {}).get("products", [])}
    except Exception:  # noqa: BLE001  data unavailable: judged by the page alone
        data = {}
    known = lambda hs: [h for h in hs if h in data] if data else list(hs)
    opts = ctx.sess.evaluate(FILTER_OPTIONS_JS) or []
    if not opts:                             # filters a script adds after the grid
        ctx.sess.page.wait_for_timeout(1500)
        opts = ctx.sess.evaluate(FILTER_OPTIONS_JS) or []
    pick = pick_filter(opts)
    if not pick:
        ctx.steps.info("filter_not_judged", "this collection offers no filters (no storefront filter options on the page)"
                       if not opts else f"only price filters or empty options offered ({len(opts)} options)")
    else:
        name, value, label = pick["name"], str(pick["value"]), pick.get("label") or str(pick["value"])
        names = list(dict.fromkeys(o["label"] or o["value"] for o in opts if o["name"] == name))
        ctx.steps.info("filters_offered", f"{len(opts)} filter options (e.g. {', '.join(names[:4])}); applying {label!r} "
                                          f"= {name}={value}")
        same = [o for o in opts if o["name"] == name and str(o["value"]) == value]
        sel = f'[data-radar-filter="{(next((o for o in same if o.get("shown")), same[0]))["i"]}"]'

        def apply():
            how = _choose(ctx, sel, name, value, label)
            got = applied_filters(ctx.sess.page.url)
            ctx.expect("filters in the page address", f"exactly one: {name}={value}",
                       ", ".join(f"{k}={v}" for k, v in got) or "none (the address did not change)", got == [(name, value)])
            return how
        if ctx.steps.run("applies_filter", apply, soft=soft) is not None:
            listed = _listed(ctx)
            for _ in range(6):               # a grid a script draws after the filtered page loads
                if listed:
                    break
                ctx.sess.page.wait_for_timeout(500)
                listed = _listed(ctx)
            said = facet_count(label)
            app = None if listed else ctx.sess.evaluate(FILTER_APP_JS)
            if app:                          # the app draws the grid from its own parameters: Radar cannot drive it
                ctx.steps.info("filter_not_judged", f"not judged: filter app {app} draws this collection's products and "
                                                    f"listed none for Shopify's filter address {name}={value}"
                                                    + (f" (the theme's own option says {said})" if said else ""))
                _collection_sort(ctx, url, data, soft)
                return

            def narrows():
                # counted from the page itself: a collection bigger than the 250 products Radar reads lists products
                # the data does not carry (maspar.com, 11 Oct: '0 products' counted only the known ones)
                after = known(listed)
                unknown = len(listed) - len(after) if data else 0
                ctx.expect("products listed with the filter on", "≥ 1" + (f" (the store's own filter says {said})" if said else ""),
                           len(listed), len(listed) >= 1)
                cap = max(len(first), len(data))    # never more than the collection holds
                ctx.expect("products listed: filtered vs unfiltered", f"≤ {cap}", len(listed), len(listed) <= cap)
                verdicts = {h: filter_matches(data[h], name, value) for h in after if h in data}
                wrong = [h for h, ok in verdicts.items() if ok is False]
                rechecked: list[str] = []
                if wrong and name == "filter.v.availability":
                    # products.json is not the storefront's view of stock: ask the store's own product data and page
                    here = ctx.sess.page.url
                    for h in wrong[:3]:
                        verdicts[h], why = _storefront_stock(ctx, _base(here), h)
                        rechecked.append(f"{h}: {why}")
                    if _path(ctx.sess.page.url) != _path(here):
                        try:                 # back to the filtered grid (the step's screenshot shows it)
                            ctx.sess.goto(here)
                        except Exception:  # noqa: BLE001
                            pass
                    wrong = [h for h, ok in verdicts.items() if ok is False]
                told = [h for h, ok in verdicts.items() if ok is not None]
                seen = f" (products.json says sold out; rechecked {'; '.join(rechecked)})" if rechecked else ""
                if told:
                    ctx.expect(f"listed products matching {label!r} (store's data)", f"all {len(told)}",
                               f"{len(told) - len(wrong)} of {len(told)}" + (f"; not matching: {', '.join(wrong[:4])}" if wrong else "")
                               + seen, not wrong)
                return (f"{label!r}: {len(listed)} products listed ({len(first)} without the filter)"
                        + (", all match it in the store's data" if told else ", the store's data cannot tell which match")
                        + seen + (f"; {unknown} of them not in the store's data Radar read ({len(data)} products)"
                                  if unknown else ""))
            ctx.steps.run("filter_narrows_results", narrows, soft=soft)
    _collection_sort(ctx, url, data, soft)


def _collection_sort(ctx: Ctx, url: str, data: dict, soft: bool) -> None:
    """'Price, low to high', checked only where robots.txt allows sort_by (never on Shopify's default robots.txt)."""
    if _path(ctx.sess.page.url) != _path(url) or urlparse(ctx.sess.page.url).query:
        try:                                 # back to the unfiltered collection (robots-checked like every page)
            ctx.sess.goto(url)
            _dismiss(ctx)
        except Exception as e:  # noqa: BLE001
            ctx.steps.info("sort_not_judged", f"the collection did not open again: {_short(str(e), 120)}")
            return
    s = ctx.sess.evaluate(SORT_JS)
    if not s or s.get("no_price"):
        ctx.steps.info("sort_not_judged", "no sort control with 'price, low to high'" if s else "no sort control on the page")
        return
    direct = one_filter_url(url, "sort_by", "price-ascending")
    pred = ctx.sess.evaluate(PREDICT_JS, ['[data-radar-sort="1"]', "price-ascending"]) or {}
    if not ctx.sess.allowed(direct) and not (pred and ctx.sess.allowed(pred["all"]) and ctx.sess.allowed(pred["own"])):
        ctx.steps.info("sort_not_judged", f"sorting not checked: the store's robots.txt disallows {_path(direct)}?sort_by=… "
                                          "(Shopify's default) and Radar always respects robots.txt")
        return
    prices = {h: min(float(v.get("price") or 0) for v in p["variants"]) for h, p in data.items() if p.get("variants")}

    def sort():
        how = _choose(ctx, '[data-radar-sort="1"]', "sort_by", "price-ascending", s["label"], is_select=True)
        q = urlparse(ctx.sess.page.url).query
        ctx.expect("sort in the page address", "sort_by=price-ascending", q or "none", _has_param("sort_by", "price-ascending")(ctx.sess.page.url))
        ok, detail = price_order_ok(_listed(ctx), prices)
        ctx.expect("products ordered by price, low to high (store's data)", "never cheaper further down", detail, ok)
        return f"{how}: {detail}"
    ctx.steps.run("sorts_by_price", sort, soft=soft)


# Where is the product's name on its own page? Themes differ wildly (h1, an h2 rich-text block, a div in
# the theme's own <header>, a 12px span), so: score every visible text that matches the catalog
# title, prefer headings and big type. Excluded: the SITE header/footer/nav, drawers and dialogs,
# buttons/labels ("Decrease quantity for X"), and anything inside another product's card
# (an ancestor whose product links all point to a DIFFERENT product). Class names like "card" on
# <body> are never used to exclude (bummer.in has <body class="card-hover-effect-none">).
# Fallback when no text matches: the page's main heading (stores rename products for display).
TITLE_JS = r"""(title) => {
  const norm = s => (s || '').toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, ' ').trim();
  const want = norm(title), wt = want.split(' ').filter(w => w.length > 2);
  const m = location.pathname.match(/\/products\/([^/?#]+)/), handle = m ? decodeURIComponent(m[1]) : null;
  const CHROME = 'body > header, #shopify-section-header, [id*="header-group" i], .shopify-section-group-header-group, ' +
    'footer, [id*="footer" i], nav, [role="navigation"], [role="dialog"], cart-drawer, [id*="cart-drawer" i], ' +
    '[class*="cart-drawer" i], button, label, option, select, [class*="breadcrumb" i], [class*="visually-hidden" i], .sr-only';
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && parseFloat(s.opacity || 1) > 0.05; };
  // Another product's card = the nearest box holding product links, when those links all go to a
  // different product AND they cover a good part of that box (a card is mostly its link). A big
  // section with one "you may also like" link in it is not a card.
  const area = r => Math.max(1, r.width * r.height);
  const otherProduct = e => {
    const link = e.closest('a[href*="/products/"]');
    if (link) return !!handle && !(link.getAttribute('href') || '').includes('/products/' + handle);
    // the page's own main heading is never another product's card (beminimalist.co, 14 Oct: its <h1> shared a box with
    // big 'CHOOSE VARIANTS' cards linking to sibling products; the name was 'not shown' until the LLM re-check)
    if (e.closest('h1')) return false;
    for (let a = e.parentElement, k = 0; a && a !== document.body && k < 6; a = a.parentElement, k++) {
      const ls = [...a.querySelectorAll('a[href*="/products/"]')];
      if (!ls.length) continue;
      const hs = ls.map(l => ((l.getAttribute('href') || '').match(/\/products\/([^/?#]+)/) || [])[1]).filter(Boolean).map(decodeURIComponent);
      if (!handle || hs.includes(handle)) return false;
      const covered = ls.reduce((t, l) => t + area(l.getBoundingClientRect()), 0);
      return covered >= 0.25 * area(a.getBoundingClientRect());
    }
    return false;
  };
  const score = txt => { const n = norm(txt); if (!n || !want) return 0;
    if (n === want) return 3;
    if (n.length <= want.length * 2 + 10 && (n.includes(want) || (want.includes(n) && n.length >= want.length * 0.6))) return 2;
    const have = wt.filter(w => n.split(' ').includes(w)).length;
    return (wt.length && have / wt.length >= 0.6 && n.length <= want.length * 2 + 10) ? 1 : 0; };
  const seen = new Set(), found = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const el = w.currentNode.parentElement;
    if (!el || seen.has(el) || !w.currentNode.textContent.trim()) continue;
    seen.add(el);
    if (/^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|TITLE)$/.test(el.tagName)) continue;
    const txt = (el.innerText || '').trim(), sc = score(txt);
    if (!sc || !vis(el) || el.closest(CHROME) || otherProduct(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
    const head = el.closest('h1') ? 3 : el.closest('h2, h3, [role="heading"]') ? 1 : 0;
    found.push({el, txt, sc, fs, head, rank: sc * 10 + head * 4 + Math.min(fs, 48) / 6});
  }
  found.sort((a, b) => b.rank - a.rank);
  const tagOf = f => (f.el.closest('h1, h2, h3') || f.el).tagName.toLowerCase();
  if (found.length) { const f = found[0];
    return {text: f.txt.replace(/\s+/g, ' ').slice(0, 160), tag: tagOf(f), size: Math.round(f.fs) + 'px',
            how: ['', 'most words match', 'contains the name', 'exact name'][f.sc]}; }
  const h1 = [...document.querySelectorAll('h1')].find(e => vis(e) && !e.closest(CHROME) && !otherProduct(e) && (e.innerText || '').trim());
  if (h1) return {text: h1.innerText.trim().replace(/\s+/g, ' ').slice(0, 160), tag: 'h1',
                  size: Math.round(parseFloat(getComputedStyle(h1).fontSize)) + 'px', how: 'main heading, name differs from catalog'};
  return null;
}"""


HIDDEN_PRICE_JS = r"""() => { const out = [];
  const shown = e => { for (let a = e; a && a !== document.documentElement; a = a.parentElement) {
      const st = getComputedStyle(a); if (a.hidden || st.display === 'none' || st.visibility === 'hidden') return a; }
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 ? null : e; };
  for (const e of document.querySelectorAll('body *')) {
    if (e.children.length || /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT)$/.test(e.tagName)) continue;
    const t = (e.textContent || '').trim(); if (!t || t.length > 80 || !/₹|rs\.?\s*\d|inr/i.test(t)) continue;
    if (e.closest('[class*="cart" i], [class*="drawer" i], [class*="card" i], [class*="recommend" i]')) continue;
    const h = shown(e); if (!h) continue;
    out.push({text: t, by: h.tagName.toLowerCase() + (h.className && typeof h.className === 'string' ? '.' + h.className.trim().split(/\s+/).slice(0, 2).join('.') : '')});
    if (out.length > 30) break;
  } return out; }"""


def price_shown(want: float, nums) -> tuple[bool, str]:
    """Is the variant's price on the page? Exact, or rounded to whole rupees the way many themes display it
    (thelabellife.com shows ₹3,392 for ₹3,391.50, salty.co.in ₹727 for ₹727.18: held-out run 9 Oct). Pure."""
    nums = set(nums)
    if want in nums:
        return True, f"₹{want:,.2f}"
    import math
    whole = {round(want), math.floor(want), math.ceil(want)}
    hit = next((n for n in nums if n in whole and abs(n - want) < 1), None)
    if hit is not None and want != int(want):
        return True, f"₹{hit:,.0f} (the page rounds ₹{want:,.2f} to whole rupees)"
    return False, ""


def _hidden_price(ctx: Ctx, want: float) -> str:
    """The price is not in the visible text: is it in the page at all, only hidden? (supplysix.com, bench 4:
    '₹ 199.00' only in a sticky bar the theme hides on desktop, class 'hidden-lap-and-up'). Says where,
    so the failure (a shopper on this screen sees no price) is not mistaken for Radar looking wrong."""
    for r in ctx.sess.evaluate(HIDDEN_PRICE_JS) or []:
        if want in prices_in_text(r["text"]):
            return f" (₹{want:,.2f} is only inside a hidden element <{r['by'][:60]}>, not shown on this screen size{_page_market(ctx)})"
    return ""


def _page_market(ctx: Ctx) -> str:
    """Evidence for a price the page hides: which Shopify market the store served this visitor and whether the
    theme's scripts ran (bellavitaorganic.com + baccabucci.com, new30b 9 Oct: price and gallery empty from a US
    runner; a market other than India points to location, html.no-js to theme scripts that never ran)."""
    try:
        m = ctx.sess.evaluate("""() => ({country: (window.Shopify || {}).country || '',
            currency: ((window.Shopify || {}).currency || {}).active || '',
            nojs: document.documentElement.classList.contains('no-js'),
            gate: (() => { const re = /navigator\\.platform|x86_64|lighthouse|gtmetrix|pagespeed|isbot|\bbot\b/i;
              for (const sc of document.scripts) { const t = sc.textContent || ''; const m = t.match(re);
                if (m) return (sc.type || 'js') + ': ' + t.slice(Math.max(0, m.index - 50), m.index + 70).replace(/\s+/g, ' '); }
              return ''; })()})""") or {}
    except Exception:  # noqa: BLE001
        return ""
    bits = []
    if m.get("country") or m.get("currency"):
        bits.append(f"store served market {m.get('country') or '?'}/{m.get('currency') or '?'}")
    if m.get("nojs"):
        bits.append("theme scripts had not run (html.no-js)")
        if m.get("gate"):            # evidence for the next cycle: a script that holds the theme for 'bots' / Linux
            bits.append(f"page script mentions {m['gate'][:130]!r}")
    return ("; " + ", ".join(bits)) if bits else ""


# The product's main picture as a shopper sees it: an <img> (also inside open shadow roots) that decoded, or a large
# element painted with a CSS background image (zoom galleries). soultree.in (new30h, both devices): the gallery image
# is plainly on screen in the evidence screenshot, yet 'main product image loaded' said none: the check now waits for
# lazy galleries, reads shadow roots and backgrounds, and on failure names what it saw.
MAIN_IMAGE_JS = r"""() => { const big = r => r.width >= 150 && r.height >= 150 && r.top < 1400 && r.bottom > 0;
  const imgs = []; const walk = root => { root.querySelectorAll('img').forEach(i => imgs.push(i));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); }); };
  walk(document);
  for (const i of imgs) { const r = i.getBoundingClientRect();
    if (big(r) && i.naturalWidth > 0) return {ok: true, how: 'img ' + Math.round(r.width) + 'x' + Math.round(r.height)}; }
  for (const e of document.querySelectorAll('main *, [role=main] *, .product *, [class*=product] *')) {
    const bg = getComputedStyle(e).backgroundImage; if (!bg || !/url\(/.test(bg)) continue;
    const r = e.getBoundingClientRect(); if (!big(r)) continue;
    const u = (bg.match(/url\(["']?([^"')]+)/) || [])[1] || '';
    const done = performance.getEntriesByType('resource').some(x => u && x.name.split('?')[0] === new URL(u, location.href).href.split('?')[0]);
    if (done) return {ok: true, how: 'background image ' + Math.round(r.width) + 'x' + Math.round(r.height)}; }
  const broken = imgs.map(i => ({i, r: i.getBoundingClientRect()})).find(x => big(x.r) && x.i.complete && x.i.naturalWidth === 0
    && (x.i.currentSrc || x.i.src));
  const seen = imgs.map(i => ({i, r: i.getBoundingClientRect()})).filter(x => x.r.width * x.r.height > 2000)
    .sort((a, b) => b.r.width * b.r.height - a.r.width * a.r.height).slice(0, 3)
    .map(x => Math.round(x.r.width) + 'x' + Math.round(x.r.height) + '@' + Math.round(x.r.top) + ' natural ' + x.i.naturalWidth +
      (x.i.complete ? '' : ' loading') + ' ' + (x.i.currentSrc || x.i.src || x.i.getAttribute('data-src') || '').split('/').pop().slice(0, 40));
  return {ok: false, seen: seen.join('; '), broken: broken ? (broken.i.currentSrc || broken.i.src).split('/').pop().slice(0, 60) : null}; }"""


def _pdp_assertions(ctx: Ctx, url: str, expect_buyable: bool, soft_data: bool = False):
    """Everything a product page must show, asserted against Shopify's own product data."""
    state: dict = {}

    def identify():
        p = _product_js(ctx, ctx.sess.page.url)
        v = _selected_variant(ctx, p)
        state.update(p=p, v=v)
        return f"{p['title']!r}, selected variant {v['id']} at {rupees(v['price'])}"
    ctx.steps.run("product_identified", identify)
    p, v = state["p"], state["v"]

    def shown():
        text = ctx.sess.evaluate("() => document.body.innerText")
        t = ctx.sess.evaluate(TITLE_JS, p["title"]) or _llm_find_name(ctx, p["title"]) or {}
        heading = t.get("text") or ""
        state["heading"] = heading
        ctx.expect("product name shown on the page", p["title"],
                   f"{heading!r} ({t.get('tag')}, {t.get('size')}, {t.get('how')})" if heading
                   else "none: no visible text matching the product name, and no main heading", bool(heading))
        want = round(int(v["price"]) / 100, 2)
        nums = prices_in_text(text)
        if not price_shown(want, nums)[0]:        # price in a sticky bar / lazy block that appears on scroll (supplysix.com, bench 3)
            ctx.sess.evaluate("""async () => { const h = document.body.scrollHeight;
                for (let y = 0; y <= Math.min(h, 12000); y += Math.round(innerHeight * 0.8)) { scrollTo(0, y); await new Promise(r => setTimeout(r, 250)); }
                scrollTo(0, Math.round(innerHeight * 0.6)); await new Promise(r => setTimeout(r, 400)); }""")
            text = ctx.sess.evaluate("() => document.body.innerText")
            nums = prices_in_text(text)
            ctx.sess.evaluate("() => scrollTo(0, 0)")
        ok, how = price_shown(want, nums)
        if not ok:
            _raise_if_catalog_only(ctx, p["title"])
        where = "" if ok else _hidden_price(ctx, want)
        ctx.expect("selected variant price shown", rupees(v["price"]),
                   how if ok else f"not on page{where}", ok)
        img = ctx.sess.evaluate(MAIN_IMAGE_JS)
        for _ in range(8):            # lazy galleries decode the first slide a moment after the price shows
            if img and img.get("ok"):
                break
            ctx.sess.page.wait_for_timeout(600)
            img = ctx.sess.evaluate(MAIN_IMAGE_JS)
        ok_img = bool(img and img.get("ok"))
        if not ok_img and not (img or {}).get("broken"):
            # verdict rule (13 Oct): a large image that FAILED to load is evidence (below); no large image Radar can
            # find is not (galleries in canvas / video / app widgets): NOT JUDGED with what was seen
            raise NotJudged("main product image: no image ≥150px found in the page's markup, which is not proof it is "
                            f"missing for a shopper (largest seen: {(img or {}).get('seen') or 'none'})")
        ctx.expect("main product image loaded", "image ≥150px rendered",
                   f"yes ({img.get('how')})" if ok_img else f"a large image failed to load: {img.get('broken')}", ok_img)
        return "title, price and image visible"
    ctx.steps.run("shows_title_price_image", shown)

    def title_matches():
        ok, how = title_match(p["title"], state.get("heading", ""),
                              ctx.sess.evaluate("() => document.body.innerText"))
        ctx.expect("shown title matches catalog title", p["title"], f"{state.get('heading')!r} ({how})", ok)
        return how
    # soft: stores often show a shorter display name than the catalog/SEO title; not shopper-blocking
    ctx.steps.run("title_matches_catalog", title_matches, soft=True)

    def structured():
        lds = ctx.sess.evaluate("() => [...document.querySelectorAll(\"script[type='application/ld+json']\")].map(e => e.textContent)")
        meta = ctx.sess.evaluate("() => Object.fromEntries([...document.querySelectorAll('meta[property]')]"
                                 ".map(m => [m.getAttribute('property'), m.getAttribute('content')]))")
        d = parse_product_data(lds, meta)
        ctx.expect("structured data: name", "present", d["title"] or "(missing)", bool((d["title"] or "").strip()))
        ctx.expect("structured data: price", "> 0", d["price"], price_ok(d["price"]))
        ctx.expect("structured data: image", "present", "present" if d["image"] else "(missing)", bool(d["image"]))
        return f"via {d['source']}"
    # SEO note, never a failure (9 Oct): wellbeingnutrition.com shows the right price to shoppers while its JSON-LD
    # price is null. That costs Google Shopping / rich results, not a sale, so it is a warning on the product test.
    ctx.steps.run("structured_data_valid", structured, soft=True)

    if expect_buyable:
        def variant_ready():
            state["v"] = _ensure_variant(ctx, p)
            return f"variant {state['v']['id']} ({state['v'].get('title')}) selected, in stock"
        ctx.steps.run("variant_ready", variant_ready)

        def buy():
            loc, how, healed = _main_buy_button(ctx, p)
            gate = (state.get("v") or {}).get("_pincode_gate")
            if gate and not loc.is_enabled():
                # a store choice, not a broken buy: shown as a WARNING with the evidence (bombaysweetshop.com)
                ctx.expect("buy button enabled", True, f"disabled until the shopper checks a delivery pincode: {gate}", False)
            _expect_buy_enabled(ctx, loc)
            return (how, healed)
        ctx.steps.run("buy_button_ready", buy, soft=bool((state.get("v") or {}).get("_pincode_gate")))
    return p, state["v"]


NOT_FOUND_RX = re.compile(r"page not found|404|doesn.t exist|does not exist|no longer available|couldn.t find|could not find", re.I)


def _load_product(ctx: Ctx, url: str) -> str:
    """Open a catalog product like a shopper would from a link, and check its page really is that
    product. A product listed in Shopify's catalog whose page redirects to the homepage or says
    'Page Not Found' is a CATALOG finding (boldcare.in, foxtale.in, bench 3), not a broken product page."""
    out = _load(ctx, url)
    final = _path(ctx.sess.page.url)
    if not final.startswith("/products/"):
        ctx.expect("catalog product page reachable", f"{_path(url)} shows the product",
                   f"redirects to {final} (in the store's catalog but hidden from shoppers)", False)
    has_product = ctx.sess.evaluate("""() => !!document.querySelector('form[action*="/cart/add"]') ||
        [...document.querySelectorAll("script[type='application/ld+json']")].some(s => /"@type"\\s*:\\s*"Product"/.test(s.textContent))""")
    head = (ctx.sess.evaluate("() => (document.title + ' ' + ((document.querySelector('h1') || {}).innerText || '')).slice(0, 300)") or "")
    if not has_product and NOT_FOUND_RX.search(head):
        ctx.expect("catalog product page reachable", f"{_path(url)} shows the product",
                   f"shows {head.strip()[:60]!r} (in the store's catalog but the page does not exist)", False)
    elif not _own_buy_holder(ctx, ctx.sess.page.url):
        # No buy control for THIS product's variants. Other products' forms (cart drawer, upsells) or a Product
        # ld+json do not make it a product page: plumgoodness.com, bench 7 ("currently unavailable" page with
        # a drawer form + product data) slipped past the old "any form or ld+json" test.
        # judged on the product actually shown: a JS redirect to ANOTHER product is the url_stable warning's job
        why = _hidden_product_page(ctx, ctx.sess.page.url)
        if why:
            ctx.expect("catalog product page reachable", f"{_path(url)} shows the product",
                       f"{why} (in the store's catalog but hidden from shoppers)", False)
    # foxtale.in (full 36 + re-test, 14 Oct: 'flaky' twice): the page renders the product, then the storefront's app
    # sends the shopper to the homepage a moment later; the first try read the product, the next step found '/'.
    # The page is given 2.5 s to settle; leaving the product page in that time is the same catalog finding.
    for _ in range(5):
        ctx.sess.page.wait_for_timeout(500)
        later = _path(ctx.sess.page.url)
        if not later.startswith("/products/"):
            ctx.expect("catalog product page reachable", f"{_path(url)} shows the product",
                       f"redirects to {later} shortly after loading (in the store's catalog but hidden from shoppers)", False)
    return out


OWN_HOLDER_JS = r"""(ids) => { const s = new Set(ids.map(String));
  return [...document.querySelectorAll('input[name="id"], select[name="id"], [data-variant-id], [data-product-id]')]
    .some(e => s.has(String(e.value || e.getAttribute('data-variant-id') || ''))); }"""


def _own_buy_holder(ctx: Ctx, url: str) -> bool:
    """Does the page carry one of THIS product's variant ids anywhere a buy control would (form input, select,
    data-variant-id)? Unknown product data -> True (do not guess a hidden product)."""
    try:
        p = ctx.sess.get_json(f"{_base(url)}/products/{_handle(url)}.js") or {}
        ids = [int(v["id"]) for v in p.get("variants", [])]
    except Exception:  # noqa: BLE001
        return True
    return True if not ids else bool(ctx.sess.evaluate(OWN_HOLDER_JS, ids))


UNAVAILABLE_RX = re.compile(r"(product|item) is (currently )?(unavailable|not available)|currently unavailable", re.I)


def _hidden_product_page(ctx: Ctx, url: str) -> str | None:
    """A catalog product whose page shows NO product at all: no buy form, no Product data, and the
    product's own name nowhere on the page. Seen as an 'unavailable' template (plumgoodness.com,
    body class hidden_product, bench 4) and as a headless storefront rendering its homepage at the
    product URL (foxtale.in, bench 4). Only called when the page has no cart/add form and no Product
    ld+json, and the name test keeps headless stores' real product pages (name shown) out of it.
    product_page() then tests the next catalog product; if every candidate looks like this, the last
    one fails hard, so a store-wide broken product template is never softened."""
    sig = ctx.sess.evaluate("""() => ({cls: (document.body && document.body.className) || '',
        text: ((document.querySelector('main') || document.body || {}).innerText || '').slice(0, 6000)})""") or {}
    text = sig.get("text", "")
    try:
        title = (ctx.sess.get_json(f"{_base(url)}/products/{_handle(url)}.js") or {}).get("title") or ""
    except Exception:
        return None
    if not title or norm_text(title) in norm_text(text):
        return None
    m = UNAVAILABLE_RX.search(text)
    if m:
        return ("shows " + repr(text[max(0, m.start() - 20): m.end() + 30].strip())).replace("\n", " ")
    if re.search(r"hidden[_-]?product", sig.get("cls", ""), re.I):
        return f"page uses the store's hidden-product template (body class {sig['cls'][:60]!r})"
    return f"page shows other content, not {title[:60]!r} (no buy form, no product data, name not on the page)"


def _sold_out_since_listed(ctx: Ctx, url: str) -> str | None:
    """A product the catalog (products.json) listed in stock whose own Shopify data (/products/<h>.js, what its page
    renders from) now has no available variant: the catalog answer was stale (sold out since, or cached). Returns the
    evidence, else None. Unknown data -> None (the product test judges it as before)."""
    try:
        p = ctx.sess.get_json(f"{_base(url)}{_market_prefix(url)}/products/{_handle(url)}.js") or {}
    except Exception:  # noqa: BLE001
        return None
    vs = p.get("variants") or []
    if not vs or any(v.get("available") for v in vs):
        return None
    return (f"was listed in stock by the store's products.json, but its /products/{_handle(url)}.js has 0 of "
            f"{len(vs)} variant(s) available (sold out since the catalog was read)")


def product_page(ctx: Ctx, url: str, expect_buyable: bool = True, fallbacks: list[str] | tuple = ()):
    """fallbacks: other catalog products, used only when this one's page is unreachable (that is
    recorded as a warning, so the finding is never hidden, and the product checks still run)."""
    cands = [url, *list(fallbacks)[:2]]
    sold_out = None
    for i, u in enumerate(cands):
        last = i == len(cands) - 1
        ok = ctx.steps.run("loads" if i == 0 else f"loads_next_catalog_product_{i}",
                           lambda u=u: _load_product(ctx, u), soft=not last)
        if ok is None:
            continue
        gone = _sold_out_since_listed(ctx, u) if expect_buyable else None
        if gone and not last:
            # bombaytrooper.com (cycle 34, both devices): products.json listed it in stock, its .js and page said
            # SOLD OUT. Shoppers see a consistent sold-out page; Radar tests an in-stock product instead.
            ctx.steps.info(f"sold_out_since_listed_{i}", f"not tested: {_path(u)} {gone}; the next in-stock catalog "
                                                         "product is tested instead")
            continue
        sold_out = gone
        url = u
        break
    ctx.steps.run("url_stable", lambda: ctx.expect("final URL", _path(url), ctx.redirected_to or _path(url),
                                                   not ctx.redirected_to) and "no redirect", soft=True)
    if sold_out:
        # Anmol, 13 Oct (bombaytrooper.com, 3 'broken' product pages per device on the dashboard, all sold out): a
        # sold-out product is not a broken page and not a warning. With no in-stock candidate left, the page is judged
        # as what it is: name, price and picture must show (hard, as for any product), and the shopper must be told it
        # is sold out (a warning if an enabled buy button says nothing, as in the sold-out page test). The buy checks
        # are not run. The 'sold_out_product' step is what reports and the dashboard tag it by.
        ctx.steps.info("sold_out_product", f"sold out: {_path(url)} {sold_out}; no in-stock catalog product was left to "
                                           "test instead, so the buy checks are not run")
        p, _ = _pdp_assertions(ctx, url, False)

        def told():
            _dismiss(ctx)
            b = _sold_out_button(ctx, p)
            text = (ctx.sess.evaluate(SOLD_OUT_TEXT_JS, [QUICK_SEL]) or "") + " \n " + ((b or {}).get("text") or "")
            exp, actual, ok = sold_out_verdict(b, sold_out_wording(text))
            ctx.expect("shopper is told the product is sold out", exp, actual + (f" ({b['how']})" if b else ""), ok)
            return actual
        ctx.steps.run("shows_sold_out", told, soft=True)
        ctx.steps.run("no_js_errors", lambda: _js_errors(ctx), soft=True)
        return
    _pdp_assertions(ctx, url, expect_buyable)
    ctx.steps.run("no_js_errors", lambda: _js_errors(ctx), soft=True)
    layout_steps(ctx)


def add_to_cart(ctx: Ctx, url: str, variant_id: int | None = None, cart_path: str = "/cart"):
    _capture_add_requests(ctx)
    ctx.steps.run("product_loads", lambda: _load(ctx, url))
    state = {}

    def identify():
        p = _product_js(ctx, ctx.sess.page.url)
        v = next((x for x in p["variants"] if variant_id and int(x["id"]) == int(variant_id)), None) or _selected_variant(ctx, p)
        state.update(p=p, v=v)
        if not price_shown(round(int(v["price"]) / 100, 2), prices_in_text(ctx.sess.evaluate("() => document.body.innerText")))[0]:
            _raise_if_catalog_only(ctx, p["title"])
        return f"{p['title']!r}, variant {v['id']} at {rupees(v['price'])}"
    ctx.steps.run("product_identified", identify)

    def variant_ready():
        cur = _ensure_variant(ctx, state["p"])
        if variant_id and int(cur["id"]) != int(variant_id) and next(
                (x for x in state["p"]["variants"] if int(x["id"]) == int(variant_id) and x.get("available")), None):
            cur = dict(next(x for x in state["p"]["variants"] if int(x["id"]) == int(variant_id)),
                       **({"_pincode_gate": cur["_pincode_gate"]} if cur.get("_pincode_gate") else {}))
        state["v"] = cur
        return f"variant {cur['id']} selected"
    ctx.steps.run("variant_ready", variant_ready)
    _add_and_verify(ctx, state["p"], state["v"])
    _cart_view_and_checkout(ctx, state["p"], cart_path)


def search_results(ctx: Ctx, url: str, alt_urls: list[str] | tuple = ()):
    """Search like a shopper: type into the store's own search box when it has one, else open the
    search URL. Results must be products, and at least one must mention the searched word.
    alt_urls: searches for words from OTHER products, tried only if the first word finds nothing
    relevant (the first word can come from a product the store hides: foxtale.in 'purify', bench 3).
    The miss is kept as a warning."""
    tries = [url, *list(alt_urls)[:2]]     # 3 words: one odd product must not fail the store's search
    for i, u in enumerate(tries):
        last = i == len(tries) - 1
        if _search_once(ctx, u, ("returns_relevant_products", "returns_relevant_products_other_word",
                                 "returns_relevant_products_third_word")[i], soft=not last) is not None:
            return


def _settled_search_links(ctx: Ctx, term: str, cap: float = 10.0) -> tuple[list[dict], float]:
    """Product links on the search page once the store's search app has rendered. Search apps fill the page
    after it loads, from their own API: first nothing (bonkerscorner.com, baccabucci.com) or 'popular products'
    placeholders (bellavitaorganic.com 'Custom Search'), real results seconds later (held-out new30b, 9 Oct).
    Returns at once when a result mentions the word; else when the links have not changed for 1.5 s after a
    minimum wait (6 s for an empty page, 4 s for unrelated links); never longer than cap. (links, seconds waited)."""
    import time
    t0 = time.monotonic()
    links = _product_links(ctx.sess) or []
    key, since = None, t0
    while True:
        if any(term in (l["text"] + " " + l["handle"]).lower() for l in links):
            break
        now = time.monotonic()
        k = tuple(sorted(l["handle"] for l in links))
        if k != key:
            key, since = k, now
        if now - t0 >= cap or (now - t0 >= (4.0 if links else 6.0) and now - since >= 1.5):
            break
        ctx.sess.page.wait_for_timeout(400)
        links = _product_links(ctx.sess) or []
    waited = time.monotonic() - t0
    return links, (waited if waited >= 1 else 0.0)


SEARCH_FIELD = 'input[type="search"], input[name="q"], input[placeholder*="search" i], input[aria-label*="search" i]'

# The control that opens a hidden search box: any visible clickable thing in the page's top part whose label, title,
# class, id, data attributes, link or icon says 'search' (themes: an <a class="js-search-toggle" title="Search"> with an
# svg icon, a <span class="search-icon" role="button">, a details summary, an icon button with no label at all). The
# best one is marked for the click; never a submit button of a form with text typed into it (nothing is typed yet).
SEARCH_OPENER_JS = r"""() => { document.querySelectorAll('[data-radar-search-open]').forEach(e => e.removeAttribute('data-radar-search-open'));
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 4 && r.height > 4 && r.top < Math.max(260, innerHeight * 0.35) && s.visibility !== 'hidden' && s.display !== 'none'; };
  const says = e => { const a = [e.getAttribute('aria-label'), e.getAttribute('title'), typeof e.className === 'string' ? e.className : '',
      e.id, e.getAttribute('href'), e.getAttribute('aria-controls'), e.getAttribute('data-action'), e.getAttribute('data-target'),
      e.getAttribute('data-toggle'), (e.innerText || '').trim().slice(0, 20)].join(' ');
    const icon = e.querySelector('svg[class*="search" i], svg[id*="search" i], use[href*="search" i], use[*|href*="search" i], [class*="icon-search" i], img[alt*="search" i]');
    return /search/i.test(a) || !!icon; };
  const cands = [...document.querySelectorAll('a, button, summary, [role="button"], span[class*="search" i], div[class*="search" i][onclick], label[for*="search" i]')]
    .filter(e => !e.closest('form') || e.tagName === 'SUMMARY' || !e.closest('form').querySelector('input:not([type="hidden"])') ||
                 !vis(e.closest('form').querySelector('input:not([type="hidden"])')))
    .filter(e => vis(e) && says(e));
  const rank = e => (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button' || e.tagName === 'SUMMARY' ? 0 : 1) + (e.closest('header, [id*="header" i], [class*="header" i]') ? 0 : 2);
  cands.sort((a, b) => rank(a) - rank(b));
  if (!cands.length) return false; cands[0].setAttribute('data-radar-search-open', '1'); return true; }"""


def _open_search_box(ctx: Ctx):
    """The store's visible search field on the current page, opening it first when it hides behind a search icon /
    drawer like most themes on phones. None when there is none."""
    page = ctx.sess.page

    def field():
        cand = page.locator(SEARCH_FIELD)
        for i in range(min(cand.count(), 8)):
            try:
                if cand.nth(i).is_visible() and cand.nth(i).is_editable():
                    return cand.nth(i)
            except Exception:  # noqa: BLE001
                continue
        return None
    got = field()
    if got is not None:
        return got
    opener = page.locator("a[href$='/search'], [aria-label*='search' i]:not(input), summary[aria-label*='search' i], "
                          "button[class*='search' i], details-modal summary")
    tried = False
    for i in range(min(opener.count(), 4)):
        try:
            if opener.nth(i).is_visible():
                _click(ctx, opener.nth(i))
                tried = True
                break
        except Exception:  # noqa: BLE001
            continue
    for _ in range(4):                     # drawers slide in: give the field a moment
        got = field()
        if got is not None or not tried:
            break
        page.wait_for_timeout(400)
    if got is None and ctx.sess.evaluate(SEARCH_OPENER_JS):
        # wider net (11 Oct, 36-store run: 7 of 22 real stores' search boxes stayed closed: antinorm, cava, dotandkey,
        # mcaffeine, plum, soulflower, supplysix): any control near the top that says 'search' in its label / class / icon
        try:
            _click(ctx, page.locator('[data-radar-search-open="1"]').first)
        except Exception:  # noqa: BLE001
            return None
        for _ in range(5):
            got = field()
            if got is not None:
                break
            page.wait_for_timeout(400)
    return got


def _search_once(ctx: Ctx, url: str, step: str, soft: bool):
    term = (re.search(r"[?&]q=([^&]+)", url) or [None, ""])[1].lower()
    home = _base(url) + "/"
    ctx.steps.run({"returns_relevant_products": "open_homepage", "returns_relevant_products_other_word": "open_homepage_again"}
                  .get(step, "open_homepage_third"), lambda: _load(ctx, home))

    state: dict = {}

    def search():
        page = ctx.sess.page
        if not ctx.sess.allowed(url):      # typing into the box opens /search: same robots.txt rule as opening it
            raise RobotsBlocked(f"robots.txt disallows {_path(url)}: search not tested on this store")
        _dismiss(ctx)
        field = _open_search_box(ctx)
        if field is not None:
            field.fill(term)
            field.press("Enter")
            ctx.sess.settle()
            how = "typed into the store's search box"
            if "/search" not in page.url:                    # predictive-search themes may not navigate
                _load(ctx, url)
                how += " (no results page; opened /search)"
        else:
            _load(ctx, url)
            how = "no visible search box; opened /search"
        page.wait_for_timeout(800)
        links, waited = _settled_search_links(ctx, term)
        if waited:
            how += f" (waited {waited:.0f}s for the store's search app to render results)"
        if not _relevant(links, term):
            # a search app that fills its results area only once the shopper scrolls (ptron.in desktop, new30c 10 Oct)
            ctx.sess.evaluate("""async () => { const h = Math.min(document.documentElement.scrollHeight, 6000);
                for (let y = 0; y <= h; y += Math.round(innerHeight * 0.7)) { scrollTo(0, y); await new Promise(r => setTimeout(r, 250)); }
                scrollTo(0, 0); await new Promise(r => setTimeout(r, 300)); }""")
            more, w2 = _settled_search_links(ctx, term, cap=6.0)
            if _relevant(more, term) or (len(more) > len(links)):
                links = more
                how += " (results shown after scrolling)"
        counted = _title_count(page.title() if not page.is_closed() else "")
        if not _relevant(links, term) and (not links or (counted or 0) > len(links) + 3):
            # results area (nearly) empty while Shopify itself counted many results: the app did not render.
            # Unrelated results with no such count stay a search miss (search_misses, foxtale.in 'purify').
            shop = _shopify_search(ctx, term)
            if shop["n"]:
                # The app owning the results area showed nothing relevant (ptron.in, kushals.com mobile, new30c 10 Oct:
                # blank area, title 'Search: 1000 results found'), yet Shopify's own search finds the word: search works
                # on the store; what Radar cannot prove is the app's rendering in its browser -> warning with evidence.
                ctx.expect("Shopify's own search (/search/suggest.json) finds the word", "≥ 1 product",
                           f"{shop['n']}, e.g. {shop['first']!r}", True)
                state["app_blank"] = (f"search app did not render results for '{term}' in Radar's browser "
                                      f"({len(links)} product links shown{', title: ' + repr(shop['title']) if shop['title'] else ''}); "
                                      f"Shopify's own search (/search/suggest.json) finds {shop['n']} product(s), e.g. {shop['first']!r}")
                return f"{how}: search app showed {len(links)} results; Shopify's own search finds {shop['n']} for '{term}'"
        ctx.expect("product results", "≥ 1", f"{len(links)}{_page_market(ctx) if not links else ''}", len(links) >= 1)
        match = [l for l in links if term in (l["text"] + " " + l["handle"]).lower()]
        ctx.expect(f"results relevant to '{term}'", "≥ 1 result mentions the term",
                   f"{len(match)} of {len(links)}", len(match) >= 1)
        return f"{how}: {len(links)} results, {len(match)} mention '{term}'"
    out = ctx.steps.run(step, search, soft=soft)
    if state.get("app_blank"):
        ctx.steps.items.append(StepResult(step.replace("returns_relevant_products", "search_app_rendered"), "warn",
                                          None, state["app_blank"]))
    return out


# ---------------- journey 27: a search that finds nothing, and suggestions while typing ----------------

NO_RESULTS_RX = re.compile(r"no results|\b0 results|no products?( were| was)? found|nothing (was )?found|no matches|"
                           r"did ?n.?t match|did not match|could ?n.?t find|could not find|no items found|did ?n.?t find|"
                           r"did not find|try (a different|another|again)|check (the|your) spelling|0 items|no product matches|"
                           r"returned no|no search results|we found 0", re.I)

MAIN_TEXT_JS = """() => { const r = document.querySelector('main, [role="main"], #MainContent') || document.body;
  return (r.innerText || '').replace(/\\s+/g, ' ').slice(0, 4000); }"""


def no_results_message(text: str) -> str:
    """The 'nothing matched' sentence a search page shows, or ''. Pure, unit-tested."""
    m = NO_RESULTS_RX.search(text or "")
    if not m:
        return ""
    a = max(0, m.start() - 30)
    return (text[a:m.end() + 40]).strip()


def search_no_results(ctx: Ctx, url: str):
    """A word no store sells: the search page must still open and work (hard: HTTP < 400, not blank), and tell the
    shopper nothing matched (soft: search apps often show 'popular products' instead)."""
    ctx.steps.run("loads", lambda: _load(ctx, url))

    def says():
        import time
        t0, msg, links = time.monotonic(), "", []
        while True:            # search apps render their 'no results' text after the page loads (up to ~6 s)
            msg = no_results_message(ctx.sess.evaluate(MAIN_TEXT_JS) or "")
            links = _product_links(ctx.sess) or []
            if msg or time.monotonic() - t0 >= 6:
                break
            ctx.sess.page.wait_for_timeout(500)
        ctx.expect("shopper is told nothing matched", "a 'no results' message, or no products listed",
                   f"message {msg[:90]!r}" if msg else f"no message, {len(links)} products listed", bool(msg) or not links)
        return f"says {msg[:90]!r}" if msg else "no products listed"
    ctx.steps.run("says_no_results", says, soft=True)


# Search-as-you-type: Shopify's Dawn-family <predictive-search>, theme data attributes and the common search apps.
PREDICTIVE_JS = r"""() => !!document.querySelector('predictive-search, [data-predictive-search], [data-predictive-search-url], ' +
  'form[action*="search"] [class*="predictive" i], [class*="predictive-search" i], [id*="predictive" i], [class*="search-autocomplete" i], ' +
  '[class*="instant-search" i], [class*="boost-sd" i], [class*="searchanise" i], [id*="searchanise" i], [class*="searchtap" i], ' +
  '[class*="klevu" i], [class*="algolia" i], [class*="search-suggest" i]')"""

# Product links on screen that were NOT there before typing: every link present before is marked first (a suggestion
# can be a product the homepage already shows as a card, so handles alone cannot tell them apart).
MARK_PRODUCTS_JS = r"""() => { document.querySelectorAll('a[href*="/products/"]').forEach(a => a.setAttribute('data-radar-before', '1')); }"""
NEW_PRODUCTS_JS = r"""() => [...document.querySelectorAll('a[href*="/products/"]:not([data-radar-before])')]
  .filter(a => { const r = a.getBoundingClientRect(), s = getComputedStyle(a);
                 return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && s.visibility !== 'hidden'; })
  .map(a => ((a.pathname.match(/\/products\/([^/?#]+)/) || [])[1] || '') + '|' + (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60))"""


def search_suggestions(ctx: Ctx, home: str, term: str):
    """Type a real product word into the store's search box (never press Enter) and watch for product suggestions.
    Judged only where the store HAS search-as-you-type (a predictive-search element or search app on the page) and
    Shopify's own /search/suggest.json finds the word: then no suggestion is a WARNING. Never a failure."""
    ctx.steps.run("open_homepage", lambda: _load(ctx, home))

    def suggest():
        page = ctx.sess.page
        if not ctx.sess.allowed(_base(home) + "/search/suggest.json"):
            return "not tested: the store's robots.txt disallows /search"
        _dismiss(ctx)
        field = _open_search_box(ctx)
        if field is None:
            return "not judged: no visible search box on the homepage"
        field.click()
        page.wait_for_timeout(300)
        ctx.sess.evaluate(MARK_PRODUCTS_JS)               # after the box opened: its own 'popular products' are not suggestions
        field.press_sequentially(term, delay=90)          # like a shopper typing; Enter is never pressed
        import time
        t0, new = time.monotonic(), []
        while time.monotonic() - t0 < 6:
            page.wait_for_timeout(400)
            new = ctx.sess.evaluate(NEW_PRODUCTS_JS) or []
            if new:
                break
        has_predictive = bool(ctx.sess.evaluate(PREDICTIVE_JS))
        if new:
            first = new[0].split("|")
            return f"{len(new)} product suggestion(s) while typing '{term}', e.g. {(first[1] or first[0])[:60]!r}"
        shop = _shopify_search(ctx, term)
        if not has_predictive or not shop["n"]:
            return ("not judged: this store's search box has no search-as-you-type" if not has_predictive else
                    f"not judged: no suggestions for '{term}' and Shopify's own search finds none either")
        ctx.expect(f"product suggestions while typing '{term}'", "≥ 1 (the store has search-as-you-type)",
                   f"none after 6 s; Shopify's own search finds {shop['n']}, e.g. {shop['first']!r}", False)
    ctx.steps.run("suggestions_while_typing", suggest, soft=True)


def _title_count(title: str) -> int | None:
    """Shopify's own result count printed in the search page title ('Search: 513 results found for "sonor"'). Pure."""
    m = re.search(r"(\d[\d,]*)\s+results?\b", title or "", re.I)
    return int(m.group(1).replace(",", "")) if m else None


def _relevant(links: list[dict], term: str) -> bool:
    return any(term in (l["text"] + " " + l["handle"]).lower() for l in links)


def _shopify_search(ctx: Ctx, term: str) -> dict:
    """Never called when robots.txt disallows /search (it returns 'not found' then). Shopify's own predictive search for the word, fetched from the page (same origin, the store's own endpoint),
    plus the result count Shopify printed in the <title>. {'n': products found, 'first': a title, 'title': page title}."""
    if not ctx.sess.allowed(ctx.sess.page.url.split("/", 3)[0] + "//" + ctx.sess.page.url.split("/", 3)[2] + "/search/suggest.json"):
        return {"n": 0, "first": "", "title": ""}
    try:
        r = ctx.sess.evaluate("""async (q) => { try {
            const res = await fetch('/search/suggest.json?q=' + encodeURIComponent(q) + '&resources[type]=product&resources[limit]=10',
                                    {credentials: 'same-origin', headers: {accept: 'application/json'}});
            if (!res.ok) return {n: 0, first: '', title: document.title};
            const j = await res.json(); const ps = ((j.resources || {}).results || {}).products || [];
            const hit = ps.filter(p => ((p.title || '') + ' ' + (p.handle || '')).toLowerCase().includes(q));
            return {n: hit.length, first: (hit[0] || {}).title || '', title: document.title};
          } catch (e) { return {n: 0, first: '', title: document.title}; } }""", term)
    except Exception:  # noqa: BLE001
        r = None
    return r or {"n": 0, "first": "", "title": ""}


def meta_tags(ctx: Ctx, url: str, fallbacks: list[str] | tuple = ()):
    """fallbacks (product pages only): other catalog products, used when this one's page is unreachable. The meta
    check judges the store's product-page template, not one product; a catalog product whose page is gone is already
    the product test's warning (giva.co, full 36 run 38379683273: 'silver-classic-bracelet-for-charms' 404 made
    health.meta.pdp a second, hard failure for the same fact and the store 'degraded')."""
    cands = [url, *list(fallbacks)[:2]]
    for i, u in enumerate(cands):
        last = i == len(cands) - 1
        if ctx.steps.run("loads" if i == 0 else f"loads_next_catalog_product_{i}",
                         lambda u=u: _load_product(ctx, u) if "/products/" in u else _load(ctx, u), soft=not last) is not None:
            url = u
            break
    m = ctx.sess.evaluate("""() => ({
        title: document.title,
        desc: document.querySelector('meta[name=description]')?.content || '',
        canonical: document.querySelector('link[rel=canonical]')?.href || '',
        og_title: document.querySelector('meta[property="og:title"]')?.content || '',
        og_image: document.querySelector('meta[property="og:image"]')?.content || '' })""")

    def length(key, label, lo, hi):
        v = m[key] or ""
        return lambda: ctx.expect(f"{label} length", f"{lo}–{hi} chars", f"{len(v)}: {v[:60]!r}" if v else "missing",
                                  lo <= len(v) <= hi) and v[:70]
    ctx.steps.run("title", length("title", "<title>", 10, 70), soft=True)
    ctx.steps.run("meta_description", length("desc", "meta description", 50, 170), soft=True)
    ctx.steps.run("canonical", lambda: ctx.expect("canonical link", "present", m["canonical"] or "missing",
                                                  bool(m["canonical"])), soft=True)

    def og():
        ctx.expect("og:title (link previews)", "present", m["og_title"] or "missing", bool(m["og_title"]))
        ctx.expect("og:image (link previews)", "present", m["og_image"] or "missing", bool(m["og_image"]))
        return "present"
    ctx.steps.run("og_tags", og, soft=True)


def not_found(ctx: Ctx, url: str):
    def status():
        resp, _ = ctx.sess.goto(url)
        landed = _path(ctx.sess.page.url)
        actual = f"{resp.status if resp else None}" + (f" (redirected to {landed})" if landed != _path(url) else "")
        ctx.expect("HTTP status for a page that does not exist", 404, actual, bool(resp) and resp.status == 404)
        return "HTTP 404"
    ctx.steps.run("returns_404", status)


# ---------------- store info pages (journeys 28 + 29) ----------------

# Text of the page's own content: <main> (or the body), never the site header / footer / menus / dialogs, which
# every page repeats. A contact page counts as real when it has a form with a text field, a mailto: / tel: link, or
# an email address / phone number in its text.
INFO_TEXT_JS = r"""() => {
  const root = document.querySelector('main, [role="main"], #MainContent') || document.body;
  const skip = 'header, footer, nav, [role="navigation"], [role="dialog"], [aria-modal="true"], script, style, noscript, template';
  const count = r => { let n = 0; const w = document.createTreeWalker(r, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) { const el = w.currentNode.parentElement; if (!el || el.closest(skip)) continue;
      const s = getComputedStyle(el); if (s.display === 'none' || s.visibility === 'hidden') continue;
      n += w.currentNode.textContent.replace(/\s+/g, ' ').trim().length; } return n; };
  const n = Math.max(count(root), root === document.body ? 0 : count(document.body));
  const txt = (root.innerText || '');
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return {chars: n,
          form: [...document.querySelectorAll('form textarea, form input[type="email"], form input[name*="email" i], form input[type="text"]')]
                  .some(e => vis(e) && !e.closest('header, footer, [role="dialog"], [aria-modal="true"]')),
          reach: !!document.querySelector('main a[href^="mailto:"], main a[href^="tel:"], [role="main"] a[href^="mailto:"], [role="main"] a[href^="tel:"]')
                 || /[\w.+-]+@[\w-]+\.[\w.]+|(\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}|\b1800[\s-]?\d{3}[\s-]?\d{4}\b/.test(txt),
          heading: ((document.querySelector('main h1, h1') || {}).innerText || '').trim().slice(0, 80)};
}"""

SOFT_404 = re.compile(r"\b404\b|page not found|not be found|doesn.t exist|does not exist", re.I)


def _offsite_redirect(ctx: Ctx, url: str):
    """(other host, status) when the store answers url with a redirect to another site, else None. One request to the
    store's own address (robots.txt already allowed it), redirects not followed."""
    try:
        ctx.sess._polite()
        r = ctx.sess.page.context.request.get(url, max_redirects=0, timeout=15000, headers=ctx.sess.sign(url) or None)
    except Exception:  # noqa: BLE001
        return None
    loc = r.headers.get("location") if 300 <= r.status < 400 else None
    if not loc:
        return None
    to = urljoin(url, loc)
    host = (urlparse(to).hostname or "").lower()
    return (host, r.status) if host and not same_site_url(to, url) else None


def _open_info_page(ctx: Ctx, url: str) -> str:
    """Opens like a shopper clicking the footer link: HTTP < 400 and not blank (_load), and NOT the store's
    'page not found' page served with 200, and not a redirect to the homepage (a deleted page). A link the store
    redirects to ANOTHER site that then never finishes loading is the store's link working: the other site is not
    judged (nonasties.in phone, new30k: footer /map and /panjim -> Google Maps, 'page did not load' twice)."""
    try:
        out = _load(ctx, url)
    except AssertionError as e:
        if "page did not load" not in str(e):
            raise
        off = _offsite_redirect(ctx, url)
        if not off:
            raise
        ctx.expect("the store's link answers", "HTTP < 400", f"HTTP {off[1]}, a redirect to {off[0]}", True)
        return (f"sends shoppers to another site ({off[0]}, HTTP {off[1]}); that site did not finish loading in "
                f"Radar's browser and is not judged")
    title = (ctx.sess.evaluate("() => document.title") or "").strip()
    h1 = (ctx.sess.evaluate("() => ((document.querySelector('main h1, h1') || {}).innerText || '').trim()") or "")[:80]
    ctx.expect("not the store's 'page not found' page", "the page itself", f"title {title[:60]!r}, heading {h1!r}",
               not (SOFT_404.search(title) or SOFT_404.search(h1)))
    ctx.expect("stays on the page (not sent to the homepage)", _path(url), ctx.redirected_to or _path(url),
               ctx.redirected_to != "/")
    return out


def _info_content(ctx: Ctx, kind: str) -> str:
    c = {"chars": 0, "form": False, "reach": False, "heading": ""}
    for _ in range(9):     # policy apps render their text after the page loads: up to ~4 s
        c = ctx.sess.evaluate(INFO_TEXT_JS) or c
        if c["chars"] >= 200 or (kind == "contact" and (c["form"] or c["reach"])):
            break
        ctx.sess.page.wait_for_timeout(500)
    if kind == "contact":
        ok = c["form"] or c["reach"] or c["chars"] >= 150
        ctx.expect("a way to contact the store", "a contact form, an email address or a phone number",
                   ("contact form" if c["form"] else "email / phone shown" if c["reach"] else
                    f"{c['chars']} chars of text" if ok else f"none: no form, no email or phone, {c['chars']} chars of text"), ok)
        return "contact form shown" if c["form"] else "email / phone shown" if c["reach"] else f"{c['chars']} chars of text"
    ctx.expect("policy text on the page", "≥ 200 chars (not an empty page)", f"{c['chars']} chars"
               + (f" under {c['heading']!r}" if c["heading"] else ""), c["chars"] >= 200)
    return f"{c['chars']} chars of policy text"


def info_pages(ctx: Ctx, pages: list[dict]):
    """Journey 28: every footer policy / contact page opens (hard: a broken link is a store finding) and shows real
    content (soft: apps sometimes render policy text in a frame Radar does not read)."""
    bad, opened = [], 0
    for p in pages:
        url, kind = p["url"], p["kind"]
        if not ctx.sess.allowed(url):
            continue
        opened += 1
        if ctx.steps.run(f"{kind}: opens {_path(url)[:40]}", lambda u=url: _open_info_page(ctx, u), soft=True) is None:
            bad.append(f"{kind} ({_path(url)})")
            continue
        ctx.steps.run(f"{kind}: has real content", lambda k=kind: _info_content(ctx, k), soft=True)
    ctx.steps.run("all_info_pages_open", lambda: ctx.expect(
        "footer info pages that fail to open", 0, f"{len(bad)}" + (f": {', '.join(bad)}" if bad else ""), not bad)
        and f"{opened} pages open")


def more_links_resolve(ctx: Ctx, links: list[dict]):
    """Journey 15: links a shopper meets beyond the menu open like the menu's do (hard: a broken banner or footer link
    is a store finding), judged like footer info pages ('page not found' served with 200 and homepage redirects count)."""
    bad, opened = [], 0
    for link in links:
        url = link["url"]
        if not ctx.sess.allowed(url):
            continue
        opened += 1
        if ctx.steps.run(f"{link['where']}: opens {_path(url)[:40]}", lambda u=url: _open_info_page(ctx, u), soft=True) is None:
            # a theme-editor slip seen on beyondsnack.in (11 Oct): href="/https://www.store.in/collections/…" -> 404
            if re.match(r"^/https?:/", urlparse(url).path):
                name = f"'{link['text'][:30]}'" if link.get("text") else "a link (image or icon, no text)"
                ctx.steps.info(f"{link['where']}: malformed link", f"{name} points to {_path(url)[:90]}: "
                               "the address in the page starts with '/http' (a typo in the theme settings), so it 404s")
                bad.append(f"{link['where']} malformed link {name if link.get('text') else '(image link)'}")
            else:
                bad.append(f"{link['where']} {_path(url)[:50]}" + (f" ('{link['text'][:25]}')" if link.get("text") else ""))
    ctx.steps.run("all_links_ok", lambda: ctx.expect(
        "links beyond the menu that fail to open", 0, f"{len(bad)}" + (f": {', '.join(bad[:5])}" if bad else ""), not bad)
        and f"{opened} pages open")


ACCOUNT_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; };
  const outside = e => !e.closest('header, footer, [role="dialog"], [aria-modal="true"]');
  const f = [...document.querySelectorAll('input[type="email"], input[name*="email" i], input[type="password"], input[type="tel"], ' +
      'input[name*="phone" i], input[name*="mobile" i], input[placeholder*="mobile" i], input[placeholder*="phone" i], ' +
      'input[placeholder*="email" i], input[autocomplete="username"], input[autocomplete="tel"]')]
    .filter(e => vis(e) && outside(e));
  const b = [...document.querySelectorAll('button, input[type="submit"], a')].filter(e => vis(e) && outside(e))
    .map(e => (e.innerText || e.value || '').trim()).find(t => t.length <= 40 &&
      /^(continue|verify)$|\b(sign ?in|log ?in|login|send otp|get otp|request otp|login with otp)\b/i.test(t));
  return {fields: f.map(e => e.type || e.name).slice(0, 4), button: b || ''};
}"""


def account_page(ctx: Ctx, url: str):
    """Journey 29: the header's account link opens a sign-in page. Radar never types or submits anything."""
    ctx.steps.run("loads", lambda: _open_info_page(ctx, url))

    def sign_in():
        landed = ctx.sess.page.url
        if landed.startswith("about:") or not same_site_url(landed, url):
            return (f"not judged: the account link hands sign-in to "
                    f"{'another app (the page went blank)' if landed.startswith('about:') else urlparse(landed).hostname}")
        for _ in range(6):     # login apps render their form after load
            a = ctx.sess.evaluate(ACCOUNT_JS) or {"fields": [], "button": ""}
            if a["fields"] or a["button"]:
                break
            ctx.sess.page.wait_for_timeout(500)
        ok = bool(a["fields"]) or bool(a["button"])
        ctx.expect("sign-in form", "an email / phone / password field or a sign-in button (nothing typed)",
                   (f"fields: {', '.join(a['fields'])}" if a["fields"] else "") + (f" button {a['button']!r}" if a["button"] else "")
                   or "none on the page", ok)
        return "sign-in form shown (nothing typed)"
    ctx.steps.run("shows_sign_in", sign_in, soft=True)


# ---------------- layout (journey 30): judged on every page Radar already opens, both devices ----------------

# Can the shopper scroll the page sideways? Measured by asking the window to scroll right ('instant': themes set
# scroll-behavior: smooth), then put back. The elements sticking out past the right edge are evidence only.
SIDEWAYS_JS = r"""() => {
  const vw = innerWidth, x0 = scrollX, y0 = scrollY;
  const sw = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  try { scrollTo({left: sw, top: y0, behavior: 'instant'}); } catch (e) { scrollTo(sw, y0); }
  const moved = Math.round(scrollX);
  try { scrollTo({left: x0, top: y0, behavior: 'instant'}); } catch (e) { scrollTo(x0, y0); }
  const out = [];
  if (moved > 10) {
    // fixed layers (closed side drawers) never make the page scroll: never named; nor are clipped elements
    const clips = e => { for (let a = e; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
        const s = getComputedStyle(a); if (s.position === 'fixed' || (a !== e && s.overflowX !== 'visible')) return true; } return false; };
    for (const e of document.querySelectorAll('body *')) {
      const r = e.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || r.right <= vw + 4 + x0) continue;
      const p = e.parentElement && e.parentElement.getBoundingClientRect();
      if (p && p.right > vw + 4 + x0 && e.parentElement !== document.body) continue;   // report the outermost only
      if (clips(e)) continue;
      const cls = typeof e.className === 'string' ? e.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
      out.push(`${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}${cls ? '.' + cls : ''} (${Math.round(r.width)}px wide, ` +
               `${Math.round(r.right - vw - x0)}px past the edge)`);
      if (out.length >= 3) break;
    }
  }
  return {vw, sw, moved, by: out};
}"""

# How much of the screen is under fixed layers (sticky bars, chat widgets, banners, popups Radar could not close)?
# A grid of points; a point counts when the topmost element there sits in a position:fixed layer. A fixed layer that
# holds the page's own content (an app shell with <main> inside, or one that scrolls itself) is not a covering bar.
COVERED_JS = r"""() => {
  const vw = innerWidth, vh = innerHeight, cols = 8, rows = 12; let hit = 0; const by = new Map();
  // a popup / modal Radar could not close is not a layout bar: counted apart (popup), never in the % judged
  const POPUP = /popup|modal|overlay|newsletter|klaviyo|cookie|consent|dialog|lightbox|gls-|privy|omnisend|wheel|spin/i;
  const isPopup = n => { for (let a = n; a && a !== document.body; a = a.parentElement) {
      if (a.getAttribute('role') === 'dialog' || a.getAttribute('aria-modal') === 'true' || a.tagName === 'DIALOG' ||
          POPUP.test((typeof a.className === 'string' ? a.className : '') + ' ' + (a.id || ''))) return true; } return false; };
  let popup = 0;
  const fixedLayer = el => { for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const s = getComputedStyle(n); if (s.position !== 'fixed') continue;
      if (n.querySelector('main, #MainContent') || (/(auto|scroll)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 20 && n.getBoundingClientRect().height > vh * 0.8)) return null;
      if (isPopup(n)) { popup++; return null; }
      return n; } return null; };
  for (let i = 0; i < cols; i++) for (let j = 0; j < rows; j++) {
    const el = document.elementFromPoint((i + 0.5) * vw / cols, (j + 0.5) * vh / rows);
    const f = el && fixedLayer(el); if (!f) continue; hit++;
    const cls = typeof f.className === 'string' ? f.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    const name = `${f.tagName.toLowerCase()}${f.id ? '#' + f.id : ''}${cls ? '.' + cls : ''}`;
    by.set(name, (by.get(name) || 0) + 1);
  }
  return {pct: Math.round(100 * hit / (cols * rows)), popup_pct: Math.round(100 * popup / (cols * rows)),
          by: [...by].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([n, k]) => `${n} ${Math.round(100 * k / (cols * rows))}%`)};
}"""

COVERED_MAX = 35      # % of the screen; a sticky header + a sticky buy bar + a chat bubble stay well under it
SIDEWAYS_MAX = 10     # px: a few px of sideways play (boat-lifestyle, palmonas phones: 5 px, 36-store run 11 Oct) is noise


def layout_verdicts(sideways: dict, covered: dict) -> list[tuple[str, str, str, bool]]:
    """(what, expected, actual, ok) for the two layout assertions. Pure, unit-tested."""
    moved, pct = int(sideways.get("moved") or 0), int(covered.get("pct") or 0)
    by = sideways.get("by") or []
    return [("page scrolls sideways", f"no, it fits the screen width (≤ {SIDEWAYS_MAX}px of play)",
             f"yes, by {moved}px" + (f": {'; '.join(by)}" if by else "") if moved > SIDEWAYS_MAX else f"fits ({sideways.get('vw')}px)",
             moved <= SIDEWAYS_MAX),
            ("screen covered by fixed bars", f"≤ {COVERED_MAX}%",
             f"{pct}%" + (f": {', '.join(covered.get('by') or [])}" if pct > COVERED_MAX else "")
             + (f" (a popup Radar could not close covered {covered['popup_pct']}%, not counted)" if covered.get("popup_pct") else ""),
             pct <= COVERED_MAX)]


def _layout(ctx: Ctx, which: int) -> str:
    if ctx.layout_cache is None or ctx.layout_cache[0] != ctx.sess.page.url or which == 0:
        ctx.layout_cache = (ctx.sess.page.url, layout_verdicts(ctx.sess.evaluate(SIDEWAYS_JS) or {},
                                                               ctx.sess.evaluate(COVERED_JS) or {}))
    what, expected, actual, ok = ctx.layout_cache[1][which]
    ctx.expect(what, expected, actual, ok)
    return actual


def layout_steps(ctx: Ctx) -> None:
    """Journey 30, as WARNINGS on pages already open (no extra page loads): no sideways scrolling, and fixed bars /
    overlays leave most of the screen visible. Journey 17 rides along: Core Web Vitals of the same page."""
    ctx.steps.run("layout_fits_screen", lambda: _layout(ctx, 0), soft=True)
    ctx.steps.run("layout_not_covered", lambda: _layout(ctx, 1), soft=True)
    ctx.steps.run("web_vitals", lambda: _vitals(ctx), soft=True)
    ctx.steps.run("scripts_weight", lambda: _scripts(ctx), soft=True)


# Journey 31 (Revenue Shield's 'script size' check): JavaScript the page loads and from how many third-party hosts.
# Bytes come from resource timing: same-origin and Timing-Allow-Origin scripts report their size, other third-party
# scripts report 0, so the total is a floor ('at least'). Hosts are always known.
SCRIPTS_JS = r"""() => { const here = location.hostname.replace(/^www\./, '');
  const mine = h => { h = h.replace(/^www\./, ''); return h === here || h.endsWith('.' + here) || /(^|\.)(shopify\.com|shopifycdn\.com|shopifysvc\.com)$/.test(h) || h === 'cdn.shopify.com'; };
  const rs = performance.getEntriesByType('resource').filter(e => e.initiatorType === 'script' || /\.m?js(\?|$)/.test(e.name));
  const third = new Map(); let bytes = 0, sized = 0;
  for (const e of rs) { let h = ''; try { h = new URL(e.name).hostname; } catch (x) { continue; }
    const b = e.transferSize || e.encodedBodySize || 0; if (b) { bytes += b; sized++; }
    if (!mine(h)) third.set(h, (third.get(h) || 0) + 1); }
  return {scripts: rs.length, sized, kb: Math.round(bytes / 1024), third: third.size,
          top: [...third].sort((a, b) => b[1] - a[1]).slice(0, 5).map(([h, n]) => `${h} (${n})`)}; }"""

SCRIPTS_MAX_KB, THIRD_PARTY_MAX = 4096, 25     # warn only at the extreme: most stores stay well under both


def scripts_verdict(s: dict) -> tuple[str, str, bool]:
    """(expected, actual, ok) for the page's scripts. Pure, unit-tested."""
    kb, third = int(s.get("kb") or 0), int(s.get("third") or 0)
    size = f"at least {kb / 1024:.1f} MB" if kb >= 1024 else f"at least {kb} KB"
    actual = (f"{s.get('scripts', 0)} scripts, {size} of JavaScript, {third} third-party script hosts"
              + (f": {', '.join(s.get('top') or [])}" if third else ""))
    return (f"< {SCRIPTS_MAX_KB // 1024} MB of JavaScript and < {THIRD_PARTY_MAX} third-party script hosts", actual,
            kb < SCRIPTS_MAX_KB and third < THIRD_PARTY_MAX)


def _scripts(ctx: Ctx) -> str:
    expected, actual, ok = scripts_verdict(ctx.sess.evaluate(SCRIPTS_JS) or {})
    ctx.expect("scripts the page loads", expected, actual, ok)
    return actual


# Journey 17: Core Web Vitals of the page as Radar's browser saw it. LCP = the last largest-contentful-paint entry;
# CLS = the largest session window of layout shifts without recent input (shifts < 1 s apart, window ≤ 5 s), as
# web.dev defines it. Both read from buffered entries, so nothing has to be set up before the page loads.
VITALS_JS = r"""() => new Promise(res => { const out = {lcp: null, cls: 0, shifts: 0, by: ''};
  try { new PerformanceObserver(l => { const e = l.getEntries(); if (e.length) out.lcp = e[e.length - 1].startTime; })
          .observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { let win = 0, first = 0, last = 0, worst = null;
      for (const e of l.getEntries()) { if (e.hadRecentInput) continue; out.shifts++;
        if (win && e.startTime - last < 1000 && e.startTime - first < 5000) { win += e.value; } else { win = e.value; first = e.startTime; }
        last = e.startTime; if (win > out.cls) out.cls = win;
        if (!worst || e.value > worst.value) worst = e; }
      if (worst && worst.sources && worst.sources[0] && worst.sources[0].node) { const n = worst.sources[0].node;
        const cls = typeof n.className === 'string' ? n.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
        out.by = (n.tagName || '#text').toLowerCase() + (n.id ? '#' + n.id : '') + (cls ? '.' + cls : ''); }
    }).observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  setTimeout(() => res(out), 300); })"""

LCP_POOR_S, CLS_POOR = 4.0, 0.25      # web.dev 'poor' thresholds: only a poor page is a warning


def vitals_verdict(v: dict) -> tuple[str, str, bool]:
    """(expected, actual, ok) for one page's Core Web Vitals. Pure, unit-tested."""
    lcp = v.get("lcp")
    lcp_s = None if lcp is None else round(lcp / 1000, 2)
    cls = round(float(v.get("cls") or 0), 3)
    bad = []
    if lcp_s is not None and lcp_s > LCP_POOR_S:
        bad.append(f"LCP {lcp_s}s (poor > {LCP_POOR_S}s)")
    if cls > CLS_POOR:
        bad.append(f"CLS {cls} (poor > {CLS_POOR})" + (f", biggest shift: {v['by']}" if v.get("by") else ""))
    actual = "; ".join(bad) if bad else f"LCP {f'{lcp_s}s' if lcp_s is not None else 'n/a'}, CLS {cls}"
    return f"LCP ≤ {LCP_POOR_S}s and CLS ≤ {CLS_POOR} (from Radar's runner)", actual, not bad


def _vitals(ctx: Ctx) -> str:
    expected, actual, ok = vitals_verdict(ctx.sess.evaluate(VITALS_JS) or {})
    ctx.expect("Core Web Vitals", expected, actual, ok)
    return actual


# ---------------- journey 16: the phone menu opens (judged alone, not only inside the journey) ----------------

MENU_BUTTONS = ("button[aria-label*='menu' i], summary[aria-label*='menu' i], [role='button'][aria-label*='menu' i], "
                ".header__icon--menu, .menu-toggle, .hamburger, button[class*='burger' i], [class*='menu-drawer' i] > summary, "
                "a[aria-label*='menu' i], [data-action*='menu' i], [aria-controls*='menu' i], [aria-controls*='drawer' i]")
MARK_LINKS_JS = r"""() => { let n = 0; for (const a of document.querySelectorAll('a[href]')) { const r = a.getBoundingClientRect();
  const v = r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && getComputedStyle(a).visibility !== 'hidden';
  if (v) { a.setAttribute('data-radar-was', '1'); n++; } } return n; }"""
NEW_LINKS_JS = r"""() => [...document.querySelectorAll('a[href]:not([data-radar-was])')].filter(a => { const r = a.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && getComputedStyle(a).visibility !== 'hidden'
         && !/\/(cart|account|search)(\/|$|\?)/.test(a.pathname); }).map(a => (a.innerText || '').trim().slice(0, 30)).filter(Boolean)"""
NAV_VISIBLE_JS = r"""() => [...document.querySelectorAll('header a[href], nav a[href], [role="navigation"] a[href]')].filter(a => {
  const r = a.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < innerHeight && getComputedStyle(a).visibility !== 'hidden'
    && /\/(collections|pages|blogs)\//.test(a.pathname); }).length"""


def mobile_menu(ctx: Ctx, home: str):
    """Journey 16. On a phone-sized screen the menu hides behind a button: tapping it must show the menu's links.
    Judged only on phone-sized screens with a menu button; a menu that never opens is a WARNING (the journey's own
    critical click-through already fails when a shopper cannot reach a collection at all)."""
    ctx.steps.run("open_homepage", lambda: _load(ctx, home))

    def opens():
        page = ctx.sess.page
        if (ctx.sess.evaluate("() => Math.min(innerWidth, screen.width)") or 0) >= 900:
            return "not judged: desktop-sized screen, the menu sits in the header (judged on phones)"
        if (ctx.sess.evaluate(NAV_VISIBLE_JS) or 0) >= 3:
            return "menu links are shown without a button on this phone layout"
        btns = page.locator(MENU_BUTTONS)
        tried = []
        for i in range(min(btns.count(), 4)):
            b = btns.nth(i)
            try:
                if not b.is_visible():
                    continue
                label = (b.get_attribute("aria-label") or b.inner_text() or b.get_attribute("class") or "")[:30].strip()
                ctx.sess.evaluate(MARK_LINKS_JS)
                _click(ctx, b)
                page.wait_for_timeout(900)
                new = ctx.sess.evaluate(NEW_LINKS_JS) or []
                if len(new) >= 3:
                    return f"tapped the menu button ({label!r}): {len(new)} links shown, e.g. {', '.join(new[:3])}"
                tried.append(label or f"button {i + 1}")
            except Exception:  # noqa: BLE001
                continue
        if not tried:
            return "not judged: no menu button found on this phone layout"
        ctx.expect("menu opens when its button is tapped", "≥ 3 menu links appear", f"none after tapping {', '.join(map(repr, tried))}",
                   False)
    ctx.steps.run("menu_opens", opens, soft=True)


# ---------------- LLM assist (re-check after triage only; code verifies every answer) ----------------

NAME_CANDIDATES_JS = r"""() => {
  const CHROME = 'body > header, #shopify-section-header, [id*="header-group" i], footer, nav, [role="dialog"], ' +
                 'cart-drawer, [class*="cart-drawer" i], button, label, option, select';
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && r.top < innerHeight * 2 && s.visibility !== 'hidden' && parseFloat(s.opacity || 1) > 0.05; };
  document.querySelectorAll('[data-radar-name]').forEach(e => e.removeAttribute('data-radar-name'));
  const out = []; const seen = new Set();
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) { const el = w.currentNode.parentElement; if (!el || seen.has(el)) continue; seen.add(el);
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (t.length < 3 || t.length > 120 || /^(SCRIPT|STYLE|NOSCRIPT|TITLE)$/.test(el.tagName) || !vis(el) || el.closest(CHROME)) continue;
    out.push({el, t, fs: parseFloat(getComputedStyle(el).fontSize) || 0, tag: el.tagName.toLowerCase()}); }
  out.sort((a, b) => b.fs - a.fs);
  return out.slice(0, 25).map((c, i) => { c.el.setAttribute('data-radar-name', String(i));
    return {i, text: c.t, size: Math.round(c.fs), tag: c.tag}; });
}"""

_GENERIC_WORDS = {"with", "pack", "size", "combo", "free", "mens", "women", "womens", "unisex", "kids", "the",
                  "and", "for", "set", "pcs", "piece", "pieces", "copy", "new", "online", "buy"}


def _llm(ctx: Ctx, last_rung: bool = False):
    """The model, only in the re-check after triage (ctx.llm_assist), or as the LAST rung before a step gives up as
    NOT JUDGED (verdict rule, 13 Oct: a not-judged case ends after one attempt, so the rung runs here, not after
    triage)."""
    llm = getattr(ctx.healer, "llm", None) if ctx.healer else None
    return llm if ((ctx.llm_assist or last_rung) and llm is not None and llm.enabled) else None


def _llm_find_name(ctx: Ctx, title: str) -> dict | None:
    """Re-check rung: the model points at the product's name among the page's biggest texts. Accepted
    only if code agrees: big type (>= 16px) or a heading, and it shares a real word with the catalog title."""
    llm = _llm(ctx)
    if not llm:
        return None
    cands = ctx.sess.evaluate(NAME_CANDIDATES_JS) or []
    if not cands:
        return None
    listing = "\n".join(f"[{c['i']}] <{c['tag']}> {c['size']}px {c['text']!r}" for c in cands)
    shot = None
    try:
        shot = ctx.sess.page.screenshot(type="jpeg", quality=55)
    except Exception:  # noqa: BLE001
        pass
    ans = llm.complete_json(
        "You help a store-monitoring robot read a product page. Reply with JSON only: "
        '{"index": <number or null>, "reason": "<short>"}',
        f"Catalog product name: {title!r}\nPage URL: {ctx.sess.page.url}\nVisible texts, biggest first:\n{listing}\n"
        "Which one is THIS product's name as shown to shoppers (it may be worded differently)? "
        "null if the page does not show this product.", max_tokens=120, images=[shot] if shot else None)
    if not ans or ans.get("index") is None:
        return None
    try:                 # the model sometimes answers with the name instead of the index (beminimalist, cycle 28)
        idx = int(ans["index"])
    except (TypeError, ValueError, KeyError):
        return None
    c = next((x for x in cands if x["i"] == idx), None)
    words = {w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in _GENERIC_WORDS}
    if not c or not (c["size"] >= 16 or c["tag"] in ("h1", "h2", "h3")) or not (words & set(re.findall(r"[a-z]{4,}", c["text"].lower()))):
        return None
    return {"text": c["text"], "tag": c["tag"], "size": f"{c['size']}px", "how": "found with LLM help, verified"}


OVERLAY_CONTROLS_JS = r"""() => {
  document.querySelectorAll('[data-radar-close]').forEach(e => e.removeAttribute('data-radar-close'));
  const big = [...document.querySelectorAll('body *')].filter(e => { const s = getComputedStyle(e);
    if (!/fixed|sticky|absolute/.test(s.position) || s.display === 'none' || s.visibility === 'hidden') return false;
    const r = e.getBoundingClientRect(); return r.width * r.height > innerWidth * innerHeight * 0.25 && (parseInt(s.zIndex) || 0) > 0; });
  const out = [];
  for (const o of big.slice(0, 5)) for (const b of o.querySelectorAll('button, a, [role="button"], [aria-label*="close" i]')) {
    const r = b.getBoundingClientRect(); if (r.width < 2 || r.height < 2 || out.length >= 25) continue;
    b.setAttribute('data-radar-close', String(out.length));
    out.push({i: out.length, text: (b.innerText || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim().slice(0, 60),
              tag: b.tagName.toLowerCase(), cls: (b.className || '').toString().slice(0, 60)});
  }
  return out;
}"""

NEVER_CLICK = re.compile(r"subscribe|sign ?up|accept|agree|allow|yes|confirm|i am|18|21|continue|submit|buy|add|login|"
                         r"log in|register|join|claim|get|spin|play|try|luck|win|redeem|unlock|reveal|collect|shop now|"
                         r"verify|enter|ok\b|okay|send|apply|start", re.I)
SAFE_CLOSE = re.compile(r"^(×|✕|✖|x|close|dismiss|no thanks|no,? thanks|not now|maybe later|later|skip|decline|"
                        r"i'?ll pass|no,? i'?ll pass|not interested|close dialog|close popup)$", re.I)


def safe_to_close(text: str, cls: str = "") -> bool:
    """Code's veto over an LLM-picked overlay control. Pure, unit-tested."""
    t = (text or "").strip()
    if SAFE_CLOSE.match(t):
        return True
    if not t and re.search(r"close|dismiss", cls or "", re.I):       # icon-only close buttons
        return True
    return False


def _llm_close_overlay(ctx: Ctx, last_rung: bool = False) -> str | None:
    """Last rung: something big covers the page and Radar's rules did not close it. The model picks
    the control that CLOSES it; code refuses anything that subscribes, accepts, confirms age, or buys."""
    llm = _llm(ctx, last_rung)
    if not llm:
        return None
    ctrls = ctx.sess.evaluate(OVERLAY_CONTROLS_JS) or []
    if not ctrls:
        return None
    shot = None
    try:
        shot = ctx.sess.page.screenshot(type="jpeg", quality=55)
    except Exception:  # noqa: BLE001
        pass
    listing = "\n".join(f"[{c['i']}] <{c['tag']}> {c['text']!r} class={c['cls']!r}" for c in ctrls)
    ans = llm.complete_json(
        "A popup or overlay covers an online store page. Pick the control that only CLOSES/DISMISSES it "
        "(x, close, no thanks, decline, later). Never pick subscribe, accept, agree, confirm age, sign up, or "
        'buy. Reply with JSON only: {"index": <number or null>, "reason": "<short>"}',
        f"Controls inside the overlay:\n{listing}", max_tokens=80, images=[shot] if shot else None)
    if not ans or ans.get("index") is None:
        return None
    c = next((x for x in ctrls if x["i"] == int(ans["index"])), None)
    if not c or not safe_to_close(c["text"], c["cls"]) or NEVER_CLICK.search(c["text"]) and not SAFE_CLOSE.match(c["text"].strip()):
        return None
    try:
        ctx.sess.page.locator(f'[data-radar-close="{c["i"]}"]').first.click(timeout=3000)
        ctx.sess.page.wait_for_timeout(500)
        return f"closed overlay via {c['text'] or c['cls']!r} (LLM pick, safe-listed)"
    except Exception:  # noqa: BLE001
        return None


STABLE_JS = r"""(hrefRe) => { const re = new RegExp(hrefRe); """ + BOX_JS + r"""
  const ls = [...document.querySelectorAll('a[href]')].filter(a => re.test(a.getAttribute('href') || '')).filter(a => {
    const r = boxOf(a); return r.width > 2 && r.height > 2; });
  return ls.length + '|' + ls.slice(0, 8).map(a => a.getAttribute('href') + '@' + Math.round(boxOf(a).top + scrollY)).join(',');
}"""


def _wait_stable(ctx: Ctx, href_re: str, max_ms: int = 5000) -> bool:
    """Wait until the clickable product links stop changing (search/filter apps such as SearchTap
    re-render the grid ~1 s after load: dotandkey.com, bench 3). True if it settled."""
    page, last, t0 = ctx.sess.page, None, time.time()
    while (time.time() - t0) * 1000 < max_ms:
        try:
            sig = ctx.sess.evaluate(STABLE_JS, href_re)
        except Exception:  # noqa: BLE001
            sig = None
        if sig is not None and sig == last:
            return True
        last = sig
        page.wait_for_timeout(400)
    return False


TOP_NAV_JS = r"""() => { const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && r.top < 260 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-radar-nav]').forEach(e => e.removeAttribute('data-radar-nav'));
  const items = [...document.querySelectorAll('header a, header button, header summary, nav a, [role="menubar"] a, [class*="header" i] a')]
    .filter(vis).filter(e => (e.innerText || '').trim().length > 1 && (e.innerText || '').trim().length < 30);
  const skip = /search|log ?in|sign ?in|account|cart|bag|wishlist|track|help|contact|about|blog/i;
  const out = []; const seen = new Set();
  for (const e of items) { const t = e.innerText.trim(); if (skip.test(t) || seen.has(t.toLowerCase())) continue; seen.add(t.toLowerCase());
    e.setAttribute('data-radar-nav', String(out.length));
    out.push({i: out.length, text: t.slice(0, 30), href: e.getAttribute('href') || ''}); if (out.length >= 8) break; }
  return out; }"""


def _hover_menus_for(ctx: Ctx, href_re: str, prefer) -> tuple[dict | None, str]:
    """Mega menus that open on HOVER (thehouseofrare.com, bench 3): hover each top menu item like a
    shopper's mouse, then look for a matching link in what opened."""
    page = ctx.sess.page
    for item in (ctx.sess.evaluate(TOP_NAV_JS) or [])[:8]:
        try:
            page.locator(f'[data-radar-nav="{item["i"]}"]').first.hover(timeout=2000)
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            continue
        got = _pick(ctx, "a[href]", href_re, prefer, second_pass=False)
        if not got.get("none"):
            return got, f"hovered the '{item['text']}' menu, then link"
    return None, ""


def _other_collection(ctx: Ctx, home: str, skip: list[str]) -> dict | None:
    """A collection link a shopper can click that is none of `skip` (collection paths that listed no products): on this
    page first (a landing page's tiles to its sub-collections), else on the homepage."""
    handles = "|".join(re.escape(p.split("/collections/", 1)[1]) for p in skip if "/collections/" in p)
    href_re = rf"/collections/(?!(?:frontpage{'|' + handles if handles else ''})(?:[/?#]|$))[^/?#]+/?(?:[?#].*)?$"
    got = _pick(ctx, "a[href]", href_re, [], second_pass=False)
    if got.get("none"):
        _load(ctx, home)
        got = _pick(ctx, "a[href]", href_re, [], second_pass=False)
    return None if got.get("none") else got


MENU_LINKS_JS = r"""() => { const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < innerHeight && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-radar-nav]').forEach(e => e.removeAttribute('data-radar-nav'));
  return [...document.querySelectorAll('a[href]:not([data-radar-shown])')].filter(vis)
    .filter(e => (e.innerText || '').trim().length > 1 && (e.innerText || '').trim().length < 30).slice(0, 12)
    .map((e, i) => { e.setAttribute('data-radar-nav', String(i));
      return {i, text: (e.innerText || '').trim().replace(/\s+/g, ' '), href: e.getAttribute('href') || ''}; }); }"""


def _menu_drawer_links(ctx: Ctx) -> list[dict]:
    """Links a shopper finds by opening the menu (☰): those visible only after the menu button is clicked."""
    page = ctx.sess.page
    toggles = page.locator(MENU_TOGGLES)
    for i in range(min(toggles.count(), 6)):
        tg = toggles.nth(i)
        try:
            if not tg.is_visible():
                continue
            ctx.sess.evaluate("() => document.querySelectorAll('a[href]').forEach(a => { const r = a.getBoundingClientRect();"
                              " if (r.width > 2 && r.height > 2) a.setAttribute('data-radar-shown', '1'); })")
            _click(ctx, tg)
        except Exception:  # noqa: BLE001
            continue
        items = ctx.sess.evaluate(MENU_LINKS_JS) or []
        if items:
            return items
    return []


def _hop_target(home: str, h: str) -> bool:
    """A menu link that leads to another page of the store (not a product, cart, account or search, not home)."""
    if not h or h.startswith(("#", "javascript")) or re.search(r"/(products|cart|account|search)\b", h):
        return False
    return _path(urljoin(home, h)) not in ("/", _path(home)) and same_site_url(urljoin(home, h), home)


def _landing_hop(ctx: Ctx, home: str) -> str | None:
    """Brand-landing homepages (thehouseofrare.com: MEN -> /pages/rare-rabbit): click the first top
    menu link that goes to another page of the store, like a shopper picking a section. On phones the top menu sits
    behind the menu button (subko.coffee phone, new30j: desktop hopped via 'Specialty Coffee', the phone found no header
    link on screen): the menu is opened and its links are used."""
    page = ctx.sess.page
    items = ctx.sess.evaluate(TOP_NAV_JS) or []
    if not any(_hop_target(home, it["href"]) for it in items):
        items = _menu_drawer_links(ctx)
    for item in items[:6]:
        if not _hop_target(home, item["href"]):
            continue
        try:
            before = page.url
            page.locator(f'[data-radar-nav="{item["i"]}"]').first.click(timeout=4000)
            try:
                page.wait_for_url(lambda u: u != before, timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            ctx.sess.settle()
            _dismiss(ctx)
            if page.url != before:
                return f"'{item['text']}' ({_path(page.url)})"
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------- the end-to-end shopper journey ----------------

MENU_TOGGLES = ("header details > summary, header [aria-haspopup='true'], header [aria-expanded='false'], "
                "button[aria-label*='menu' i], summary[aria-label*='menu' i], .header__icon--menu, .menu-toggle, "
                ".hamburger, button[class*='burger' i]")

NOT_SHOPPING = ("cart-drawer, cart-notification, [id*='cart-drawer' i], [class*='cart-drawer' i], [id*='CartDrawer'], "
                "[class*='quick-add' i], [data-radar-target='drawer']")

PICK_JS = r"""([sel, hrefRe, prefer, exclude]) => {
  document.querySelectorAll('[data-radar-target]').forEach(e => { if (e.getAttribute('data-radar-target') !== 'drawer') e.removeAttribute('data-radar-target'); });
  const re = hrefRe ? new RegExp(hrefRe) : null; """ + BOX_JS + r"""
  // Visible = something INSIDE the link paints: the link or a descendant with a box > 2 px, not visibility:hidden,
  // whose opacity multiplied down from the link (display:contents elements skipped: they paint nothing, so their own
  // opacity has no effect in Chromium) is > 0.05. A child may override the link's visibility. Opacity of ANCESTORS
  // above the link is not judged here (scroll-reveal sections fade in when scrolled to). valliyan.com (3 re-tests,
  // 13 Oct): the grid counted 15 products, yet '15 links: 15 not visible' under the visible cards.
  const shows = (n, op) => { const s = getComputedStyle(n); if (s.display === 'none') return false;
    const boxless = s.display === 'contents', o = boxless ? op : op * parseFloat(s.opacity || 1);
    if (o <= 0.05) return false;
    if (!boxless && s.visibility !== 'hidden') { const r = n.getBoundingClientRect(); if (r.width > 2 && r.height > 2) return true; }
    for (const c of n.children) if (shows(c, o)) return true;
    return false; };
  const vis = e => { const s = getComputedStyle(e);      // a stretched link: visible where its card is
    if (s.visibility !== 'hidden' && parseFloat(s.opacity || 1) > 0.05 && stretchOf(e)) return true;
    return shows(e, 1); };
  const invis = [];
  // evidence for run.json when nothing qualifies: the first refused link and its first descendants (tag.class
  // {display, opacity, visibility, WxH}), so a theme like valliyan's is fixed from facts, not guesses
  const desc = (e, depth) => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
    const me = tag(e) + '{' + s.display + ',op ' + s.opacity + ',' + s.visibility + ',' + Math.round(r.width) + 'x' + Math.round(r.height) + '}';
    const c = e.firstElementChild; return depth > 0 && c ? me + ' > ' + desc(c, depth - 1) : me; };
  const tag = e => e ? (e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' && e.className.trim()
                    ? '.' + e.className.trim().split(/\s+/)[0] : '')).slice(0, 40) : 'nothing';
  const why = {}; const note = k => { why[k] = (why[k] || 0) + 1; };
  const usable = e => {
    if (re && !re.test(e.getAttribute('href') || '')) return false;
    if (!vis(e)) { note('not visible'); invis.push(e); return false; }
    if (exclude && e.closest(exclude)) { note('inside cart drawer / quick-add'); return false; }
    if (e.getAttribute('aria-disabled') === 'true') { note('disabled'); return false; }
    if (e.closest('[aria-hidden="true"], [inert]')) { note('hidden from shoppers (aria-hidden/inert)'); return false; }
    return true; };
  const path = h => { try { return new URL(h, location.href).pathname.replace(/\/$/, ''); } catch (x) { return h; } };
  // The clickable "card" of a link: the biggest box around it whose links all go to the same place
  // (image + title + price of ONE product). A click anywhere in it that is not a button opens that product.
  const cardOf = e => { const want = path(e.getAttribute('href') || ''); let card = e;
    for (let a = e.parentElement, k = 0; a && a !== document.body && k < 5; a = a.parentElement, k++) {
      const ls = [...a.querySelectorAll('a[href]')];
      if (!ls.length || ls.some(l => path(l.getAttribute('href') || '') !== want)) break;
      card = a; }
    return card; };
  const selfHit = (e, hit) => !!hit && (hit === e || e.contains(hit));
  const cardHit = (e, card, hit) => !!hit && card !== e && card.contains(hit)
    && !hit.closest('button, input, select, form, [role="button"]') && !(exclude && hit.closest(exclude));
  const rank = (e, i) => { const h = e.getAttribute('href') || '';
    const p = prefer.findIndex(x => h.includes(x));
    return [(p < 0 ? 1000 : p) + (e.closest('header, nav, footer') ? 500 : 0), i]; };
  const all = [...document.querySelectorAll(sel)].filter(e => !re || re.test(e.getAttribute('href') || ''));
  const cands = all.map((e, i) => [e, i]).filter(([e]) => usable(e))
    .map(([e, i]) => [e, rank(e, i)]).sort((a, b) => a[1][0] - b[1][0] || a[1][1] - b[1][1]).slice(0, 80);
  for (const [e] of cands) {
    if (e.getBoundingClientRect().width > 0) e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    else { const b = boxOf(e); scrollBy(0, b.top + b.height / 2 - innerHeight / 2); }    // display:contents: no box to scroll to
    const r = boxOf(e);
    if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) { note('off screen (carousel slide)'); continue; }
    const card = cardOf(e);
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
    const pts = [[cx, r.top + r.height / 2], [cx, r.top + Math.min(r.height / 2, 40)], [r.left + Math.min(r.width / 2, 20), r.top + Math.min(r.height / 2, 12)]]
      .map(([x, y]) => [x, Math.min(Math.max(y, 1), innerHeight - 1)]);
    let at = null, via = 'link', cover = null; const hits = pts.map(([x, y]) => document.elementFromPoint(x, y));
    const si = hits.findIndex(h => selfHit(e, h));                  // the link itself first
    const ci = si < 0 ? hits.findIndex(h => cardHit(e, card, h)) : -1;   // else its card (image slider over the link)
    if (si >= 0) at = pts[si]; else if (ci >= 0) { at = pts[ci]; via = 'card'; }
    if (!at) { cover = hits.find(h => h) || null; note('covered by ' + tag(cover)); continue; }
    e.setAttribute('data-radar-target', 'click');
    if (card !== e) card.setAttribute('data-radar-target', 'card');
    return {href: e.getAttribute('href') || '', text: (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60),
            considered: cands.length, x: Math.round(at[0]), y: Math.round(at[1]), via};
  }
  return {none: true, total: all.length, why, refused: invis.length ? desc(invis[0], 3).slice(0, 300) : null};
}"""

AIM_JS = r"""(via) => { const t = document.querySelector('[data-radar-target="click"]'); if (!t) return null;
  const c = document.querySelector('[data-radar-target="card"]') || t; """ + BOX_JS + r"""
  const r = boxOf(t); if (r.bottom <= 0 || r.top >= innerHeight || r.width <= 0) return null;
  const cx = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
  const pts = [[cx, r.top + r.height / 2], [cx, r.top + Math.min(r.height / 2, 40)], [r.left + Math.min(r.width / 2, 20), r.top + Math.min(r.height / 2, 12)]]
    .map(([x, y]) => [Math.round(x), Math.round(Math.min(Math.max(y, 1), innerHeight - 1))]);
  const hits = pts.map(([x, y]) => document.elementFromPoint(x, y));
  // same priority as the pick: the link itself first; its card only when the pick had to use the card
  let i = hits.findIndex(h => h && (h === t || t.contains(h)));
  if (i < 0 && via === 'card') i = hits.findIndex(h => h && c !== t && c.contains(h) && !h.closest('button, input, select, form, [role="button"]'));
  return i < 0 ? null : {x: pts[i][0], y: pts[i][1], top: Math.round(r.top)}; }"""

MARK_TRIED_JS = r"""() => { const t = document.querySelector('[data-radar-target="click"]'); if (!t) return null;
  t.setAttribute('data-radar-tried', '1');
  const txt = (t.innerText || t.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const kind = t.querySelector('img, picture, video') && !txt ? 'the product IMAGE link' : `the link '${txt}'`;
  return `${kind} <a${t.className && typeof t.className === 'string' ? '.' + t.className.trim().split(/\s+/)[0] : ''}>`; }"""

CLICK_CHECK_JS = r"""([x, y]) => { const t = document.querySelector('[data-radar-target="click"]'); if (!t) return false;
  const c = document.querySelector('[data-radar-target="card"]') || t; const h = document.elementFromPoint(x, y);
  return !!h && (h === t || t.contains(h) || (c.contains(h) && !h.closest('button, input, select, form, [role="button"]'))); }"""


def _why_none(got: dict | None, where: str) -> str:
    """'none on /collections/men (62 links: 58 covered by div.swiper, 4 not visible)'."""
    if not got or not got.get("total"):
        return f"none on {where} (no matching links on the page)"
    why = ", ".join(f"{n} {k}" for k, n in sorted(got.get("why", {}).items(), key=lambda kv: -kv[1])[:3])
    return f"none on {where} ({got['total']} links: {why})" + (f"; first refused: {got['refused']}" if got.get("refused") else "")


BIG_FIXED_LAYER_JS = r"""() => { const vw = innerWidth, vh = innerHeight;
  for (const [x, y] of [[vw / 2, vh / 2], [vw / 3, vh / 3], [2 * vw / 3, 2 * vh / 3]]) {
    let e = document.elementFromPoint(x, y);
    while (e && e.shadowRoot) { const i = e.shadowRoot.elementFromPoint(x, y); if (!i || i === e) break; e = i; }
    for (let n = e; n && n !== document.body; n = n.parentNode || n.host) {
      if (!(n instanceof Element)) continue;
      const s = getComputedStyle(n), r = n.getBoundingClientRect();
      if ((s.position === 'fixed') && r.width * r.height >= 0.25 * vw * vh && !n.closest('header, nav')) return true; } }
  return false; }"""


# The layer actually covering the page, found from what is on top at the screen's centre (not from popup-like names):
# teatrunk.in (new30i, both devices) showed 'Shopping from Outside India? International | Stay Here' inside the theme's
# <header>, which the popup rules skip (menus live there); every card was 'covered by div.ctryModal-Content-overlay' and
# no popup was closed. Only a control a shopper uses to stay (close / × / no thanks / stay here ...) is clicked, never
# one that leaves, accepts or subscribes; a layer with many links or the page's main content is not a popup.
COVER_CLOSE_JS = r"""() => {
  document.querySelectorAll('[data-radar-target="cover-close"]').forEach(e => e.removeAttribute('data-radar-target'));
  const vw = innerWidth, vh = innerHeight;
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || 1) > 0.05; };
  const text = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const SAFE = /^(×|✕|✖|x|close|no thanks|no,? thanks|not now|maybe later|skip|continue browsing|continue shopping|stay here|stay on this (site|store|page)|stay on [\w .'&-]{1,40}|dismiss)$/i;
  const BAD = /accept|agree|allow|subscribe|sign ?up|submit|yes|international|go to|take me|switch|redirect/i;
  const navLinks = r => [...r.querySelectorAll('a[href]')].filter(a => shown(a) && !/^(#|javascript:)/i.test(a.getAttribute('href') || '')).length;
  const why = [];
  for (const [fx, fy] of [[0.5, 0.5], [0.33, 0.33], [0.67, 0.67], [0.2, 0.5], [0.8, 0.5]]) {
    let layer = null;
    for (let n = document.elementFromPoint(vw * fx, vh * fy); n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const s = getComputedStyle(n), r = n.getBoundingClientRect();
      if (s.position === 'fixed' && r.width * r.height >= 0.25 * vw * vh) layer = n; }      // the outermost big fixed layer
    if (!layer) continue;
    // the layer itself, or its parent when the layer is only the backdrop beside the dialog box
    for (const root of [layer, layer.parentElement]) {
      const tag = root ? root.tagName.toLowerCase() + (root.classList[0] ? '.' + root.classList[0] : '') : 'none';
      // a page's own links are visible and go somewhere; a closed country list ('#' links) does not count
      if (!root || root === document.body || navLinks(root) > 12
          || root.querySelector('main, form[action*="/cart/add"]')) { why.push(tag + ': page content (' + (root ? navLinks(root) : 0) + ' links)'); break; }
      // also a plain <div>/<span> or <input type=button> a shopper can tap (teatrunk.in re-test: 'Stay Here' was not a
      // <button>, only 'International' was listed); a non-button counts only when its own text is a stay control
      const tappable = e => /^(button|submit)$/i.test(e.type || '') && e.tagName === 'INPUT'
        || (getComputedStyle(e).cursor === 'pointer' && e.children.length <= 2 && SAFE.test(text(e)));
      const cands = [...root.querySelectorAll('button, a, [role="button"], [aria-label]')]
        .concat([...root.querySelectorAll('div, span, p, li, label, input')].filter(tappable)).filter(shown);
      const btn = cands.find(c =>
        !BAD.test(text(c)) && (SAFE.test(text(c)) || /close|dismiss/i.test(c.getAttribute('aria-label') || '')));
      if (!btn) { why.push(tag + ': no stay/close control in [' + cands.slice(0, 6).map(c => text(c).slice(0, 20) || c.tagName.toLowerCase()).join(' | ') + ']'); continue; }
      const b = btn.getBoundingClientRect(), hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
      if (!hit || !(hit === btn || btn.contains(hit))) { why.push(tag + ': ' + text(btn).slice(0, 20) + ' covered'); continue; }
      btn.setAttribute('data-radar-target', 'cover-close');
      const t = text(root);
      return {button: text(btn).slice(0, 30) || 'close', text: t.slice(0, 60).toLowerCase(),
              kind: /country|region|currency|ship to|location/i.test(t) ? 'location' : 'popup'};
    } }
  return why.length ? {none: true, why: [...new Set(why)].join('; ').slice(0, 300)} : null; }"""


def _close_covering_layer(ctx: Ctx) -> str | None:
    """Close the large fixed layer on top of the page with its own stay / close control (COVER_CLOSE_JS)."""
    found = ctx.sess.evaluate(COVER_CLOSE_JS)
    if found and found.get("none"):    # evidence for the not-judged step: why the covering layer stayed
        ctx.expect("layer covering the page", "closed with its own stay / close control", f"not closed: {found['why']}", True)
        return None
    if not found:
        return None
    try:
        ctx.sess.page.locator('[data-radar-target="cover-close"]').first.click(timeout=3000)
    except Exception:  # noqa: BLE001
        return None
    ctx.sess.page.wait_for_timeout(500)
    ctx.expect(f"{found['kind']} popup closed", "closed without accepting or subscribing",
               f"clicked {found['button']!r} on: {found['text']!r}", True)
    return f"clicked {found['button']!r} on a layer covering the page"


def _pick(ctx: Ctx, sel: str, href_re: str | None = None, prefer=(), exclude: str | None = NOT_SHOPPING,
          second_pass: bool = True):
    """First element a shopper could actually click: visible, enabled, not covered, on screen.
    Returns the pick (with the exact point that was hit-tested), or {'none': True, ...reasons}.
    If nothing qualifies, waits once for entrance animations / lazy cards and looks again."""
    got = ctx.sess.evaluate(PICK_JS, [sel, href_re, list(prefer), exclude])
    if got and got.get("none") and second_pass:
        ctx.sess.page.wait_for_timeout(1200)
        _dismiss(ctx)
        got = ctx.sess.evaluate(PICK_JS, [sel, href_re, list(prefer), exclude])
    if got and got.get("none") and second_pass and got.get("why", {}).get("not visible"):
        # cards exist but none is visible: themes reveal them with an animation on the first scroll (reequil.com,
        # wearcomet.com, held-out run 9 Oct). A shopper scrolls; so does Radar, then looks again from the top.
        ctx.sess.evaluate("""async () => { const h = Math.min(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight), 15000);
            for (let y = 0; y <= h; y += Math.round(innerHeight * 0.7)) { scrollTo(0, y); await new Promise(r => setTimeout(r, 200)); }
            scrollTo(0, 0); await new Promise(r => setTimeout(r, 400)); }""")
        got = ctx.sess.evaluate(PICK_JS, [sel, href_re, list(prefer), exclude])
    if got and got.get("none") and second_pass and any(k.startswith("covered by") for k in got.get("why", {})) \
            and ctx.sess.evaluate(BIG_FIXED_LAYER_JS):
        # every card is covered by a large fixed layer our rules could not close (soulflower.in's 'It's Our Birthday'
        # scratch popup, 9 Oct): Escape closes most modals and never consents to anything
        ctx.sess.page.keyboard.press("Escape")
        ctx.sess.page.wait_for_timeout(500)
        got = ctx.sess.evaluate(PICK_JS, [sel, href_re, list(prefer), exclude])
        if not got.get("none"):
            got["overlay"] = "pressed Escape to close a layer covering the cards"
    if got and got.get("none") and second_pass and any(k.startswith("covered by") for k in got.get("why", {})):
        closed = _close_covering_layer(ctx)
        if closed:
            got = ctx.sess.evaluate(PICK_JS, [sel, href_re, list(prefer), exclude])
            if not got.get("none"):
                got["overlay"] = closed
    if got and got.get("none") and any(k.startswith("covered by") for k in got.get("why", {})):
        closed = _llm_close_overlay(ctx, last_rung=True)    # the model's pick before the step gives up as not judged
        if closed:
            got = ctx.sess.evaluate(PICK_JS, [sel, href_re, list(prefer), exclude])
            got["overlay"] = closed
    return got


def _click_picked(ctx: Ctx, got: dict | None = None) -> None:
    """Click the picked element AT THE POINT that was hit-tested (Playwright's own click re-scrolls and
    clicks the centre, which a sticky header or a badge can cover: theme-spotlight, bench 2)."""
    page = ctx.sess.page
    loc = page.locator('[data-radar-target="click"]').first
    before = page.url
    clicked = False
    # Links that open in a NEW TAB (target=_blank or window.open: fashor.com, tigc.in, 10 Oct): for a shopper the click
    # worked, the page is in that tab. Collect tabs opened during this click and follow them below.
    opened: list = []

    def on_page(p):                         # a plain function: Playwright tags its handlers (list.append cannot be)
        opened.append(p)
    page.context.on("page", on_page)
    try:
        _click_at(ctx, page, loc, got, before, opened)
    finally:
        try:
            page.context.remove_listener("page", on_page)
        except Exception:  # noqa: BLE001
            pass
    if page.url == before and opened:
        tab = opened[0]
        try:
            tab.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        url = tab.url
        for t in opened:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass
        if url and urlparse(url).netloc == urlparse(before).netloc:
            # continue in the same browser tab at the address the new tab opened (same session, same cookies)
            ctx.sess.goto(url)
            if got is not None:
                got["new_tab"] = _path(url)
            return
    ctx.sess.settle()


def _click_at(ctx: Ctx, page, loc, got: dict | None, before: str, opened: list) -> None:
    clicked = False
    if got and got.get("x") is not None:
        # The pick scrolled the card into view. Some stores re-lay out right after a scroll (boat-lifestyle.com,
        # bench 5: the header changes ~20 ms after scrolling and a click 30 ms after the pick landed on the NEXT
        # card). So wait until the target stops moving, then aim again at where it is now.
        last = None
        for _ in range(6):
            page.wait_for_timeout(150)
            aim = ctx.sess.evaluate(AIM_JS, got.get("via") or "link")
            if not aim:
                break
            if last and aim == last:
                got = dict(got, x=aim["x"], y=aim["y"])
                break
            last = aim
    if got and got.get("x") is not None and ctx.sess.evaluate(CLICK_CHECK_JS, [got["x"], got["y"]]):
        page.mouse.click(got["x"], got["y"])
        clicked = True
        if got.get("via") == "card":        # the card area did not navigate: fall back to the link itself
            try:
                page.wait_for_url(lambda u: u != before, timeout=4000)
            except Exception:  # noqa: BLE001
                clicked = False
            if not clicked and opened:
                return                              # it opened a new tab: the caller follows it (no second click)
    if not clicked:
        try:
            loc.click(timeout=6000)
        except Exception:  # noqa: BLE001  a popup appeared between pick and click
            _dismiss(ctx)
            try:
                loc.click(timeout=6000)
            except Exception as e:  # noqa: BLE001
                m = re.search(r"<[^>]{0,160}>[^\n]{0,60}intercepts pointer events", str(e))
                raise AssertionError("could not click: " + (m.group(0)[:200] if m else str(e).splitlines()[0][:160])) from e
    for _ in range(16):                     # up to 8 s: the page moves on, or the link opened a new tab
        if page.url != before or opened:
            break
        try:
            page.wait_for_url(lambda u: u != before, timeout=500)
        except Exception:  # noqa: BLE001  same-page links / slow stores: the caller's assertion decides
            pass


def _click(ctx: Ctx, el) -> None:
    """Click a specific locator like a shopper (used for menu toggles)."""
    _dismiss(ctx)
    el.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
    try:
        el.click(timeout=6000)
    except Exception:  # noqa: BLE001
        _dismiss(ctx)
        el.click(timeout=6000)
    ctx.sess.settle()


COLLECTION_HREF = r"/collections/(?!frontpage(?:[/?#]|$))[^/?#]+/?(?:[?#].*)?$"
PRODUCT_HREF = r"/products/[^/?#]+"


def shopper_journey(ctx: Ctx, home: str, collection_url: str, product_handles: list[str],
                    allow_cart: bool = True, cart_path: str = "/cart"):
    """One continuous session, moving by CLICKS like a shopper (never typing URLs):
    home -> a collection (menu, homepage tile, or opened dropdown) -> product card -> product page
    -> add to cart -> cart (drawer or page) -> checkout button ready. Checkout is never clicked.
    Stores with no collection link on the homepage go home -> product directly (still a shopper path)."""
    page = ctx.sess.page
    _capture_add_requests(ctx)
    ctx.steps.run("open_homepage", lambda: _load(ctx, home))
    prefer_coll = [_path(collection_url)]

    def click_collection():
        _dismiss(ctx)
        got, how = _pick(ctx, "a[href]", COLLECTION_HREF, prefer_coll, second_pass=False), "visible link"
        if got.get("none"):
            # a layer the popup rules missed covers the page (teatrunk.in): close it before opening menus (which it
            # covers too); runs on the fresh homepage only, so it never closes a menu Radar opened
            closed = _close_covering_layer(ctx)
            if closed:
                got = _pick(ctx, "a[href]", COLLECTION_HREF, prefer_coll, second_pass=False)
                if not got.get("none"):
                    got["overlay"] = closed
        if got.get("none"):                                 # links inside closed dropdowns / hamburger
            toggles = page.locator(MENU_TOGGLES)
            for i in range(min(toggles.count(), 6)):
                tg = toggles.nth(i)
                try:
                    if not tg.is_visible():
                        continue
                    _click(ctx, tg)
                except Exception:  # noqa: BLE001
                    continue
                got = _pick(ctx, "a[href]", COLLECTION_HREF, prefer_coll, second_pass=False)
                if not got.get("none"):
                    how = "opened a menu, then link"
                    break
        if got.get("none"):                                 # menus that open on hover (fresh page: no drawer left open)
            _load(ctx, home)
            hov, hhow = _hover_menus_for(ctx, COLLECTION_HREF, prefer_coll)
            if hov:
                got, how = hov, hhow
        if got.get("none"):                                 # brand-landing homepage: one hop via the top menu
            _load(ctx, home)
            hop = _landing_hop(ctx, home)
            if hop:
                got = _pick(ctx, "a[href]", COLLECTION_HREF, prefer_coll)
                how = f"went via {hop}, then link"
        found = not got.get("none")
        ctx.expect("collection link a shopper can click on the homepage", "found",
                   f"{got['href']} ({got['text']!r})" if found else "none (menus opened too)", found)
        _click_picked(ctx, got)
        ctx.expect("landed on that collection", _path(got["href"]), _path(page.url), "/collections/" in page.url)

        def listed() -> int:
            n, _ = _wait_for_products(ctx)
            if not n:
                _wait_stable(ctx, PRODUCT_HREF)
                _reveal_product_grid(ctx)
                n = len(_product_links(ctx.sess))
            return n
        n, empty = listed(), []
        while not n and len(empty) < 3:
            # This collection lists no products: an 'entrance' landing page with tiles to sub-collections (quboworld.com
            # 'Smart Entrance', new30j: '104 links: 104 not visible' = hidden mega-menu links) or an empty collection.
            # A shopper goes on to another collection: one this page links to first, else another from the homepage.
            empty.append(_path(page.url))
            alt = _other_collection(ctx, home, empty)
            if not alt:
                break
            _click_picked(ctx, alt)
            if "/collections/" not in page.url:
                break
            n = listed()
        ctx.expect("products listed in collection", "≥ 1", n, n >= 1)
        went = f"; {', '.join(empty)} listed no products, went on to {_path(page.url)}" if empty else ""
        return (f"clicked {how} → {_path(page.url)} ({n} products){went}"
                + (f"; {got['overlay']}" if got.get("overlay") else ""))
    # soft: some themes (e.g. Origin) put no collection link on the homepage; then home -> product
    if ctx.steps.run("click_into_collection", click_collection, soft=True) is None:
        if "/collections/" not in page.url:
            _load(ctx, home)        # fresh homepage: menus/drawers opened while looking must not cover the cards

    def click_product():
        _dismiss(ctx)
        prefer = [f"/products/{h}" for h in product_handles]
        _wait_stable(ctx, PRODUCT_HREF)
        if "/collections/" in page.url:
            _reveal_product_grid(ctx)
        got = _pick(ctx, "a[href]", PRODUCT_HREF, prefer)
        found = not got.get("none")
        if not found:
            # verdict rule (13 Oct): no card Radar could judge usable is not proof the page is broken (valliyan's
            # stretched links, teatrunk's slider cells, popups it could not close): NOT JUDGED, with the evidence
            raise NotJudged(f"product card a shopper can click: {_why_none(got, _path(page.url))}; Radar could not "
                            "find a card it is sure a shopper can tap, which is not proof the page is broken")
        ctx.expect("product card a shopper can click", "found", f"{got['href']} ({got['text']!r})", True)
        clicked = _handle(got["href"])
        listing = page.url
        first = ctx.sess.evaluate(MARK_TRIED_JS)
        errors: list[str] = []

        tabs: list = []

        def try_click(g):
            tabs.append(g)
            # A click another layer intercepts is, for the shopper, a click that did nothing: the fallbacks below
            # (click again, the product's other link) must still get their turn (bummer.in, 9 Oct: a slider layer over
            # the image link; v0.19 raised here and never tried the product name).
            try:
                _click_picked(ctx, g)
            except AssertionError as e:
                errors.append(str(e))
        try_click(got)
        again = ""
        if _handle(page.url) != clicked and page.url == listing:
            # nothing happened: the grid was re-rendered under the click (dotandkey.com, bench 3). A shopper clicks again.
            _wait_stable(ctx, PRODUCT_HREF)
            got2 = _pick(ctx, "a[href]", PRODUCT_HREF, [f"/products/{clicked}"] + prefer)
            if not got2.get("none"):
                clicked = _handle(got2["href"])
                ctx.sess.evaluate(MARK_TRIED_JS)
                try_click(got2)
                again = " (first click did not open it: the page re-rendered; clicked again)"
        if _handle(page.url) != clicked and page.url == listing and clicked:
            # Still nothing: that LINK does nothing when clicked (thefunclab.com, bench 10: the homepage slider's
            # script cancels mousedown/click on the product IMAGE; the product NAME opens it, also for a shopper).
            # A shopper then clicks the product's other link in the same card. If that opens it, the journey goes
            # on and the dead link is reported as a store WARNING; if not, the step fails as before.
            got3 = _pick(ctx, "a[href]", rf"/products/{re.escape(clicked)}(?:[/?#]|$)", [f"/products/{clicked}"],
                         exclude=NOT_SHOPPING + ", [data-radar-tried]", second_pass=False)
            if not got3.get("none"):
                try_click(got3)
                if _handle(page.url) == clicked:
                    dead["link"] = (f"{first or 'a product link'} on {_path(listing)} did nothing when clicked (twice); "
                                    f"the product's other link ({got3['text']!r}) opened it")
                    again = " (its first link did nothing; the product name opened it)"
        if _handle(page.url) != clicked and errors:
            raise AssertionError(errors[0])           # same evidence as before when nothing worked
        ctx.expect("opened the product that was clicked", f"/products/{clicked}", _path(page.url),
                   _handle(page.url) == clicked)
        known = "known in-stock product" if clicked in product_handles else "first clickable product"
        return (f"clicked {known} on {got.get('considered')} candidates → {_path(page.url)}{again}"
                + (f"; {got['overlay']}" if got.get("overlay") else "") + (" (via its card)" if got.get("via") == "card" else "")
                + (" (the link opened it in a new tab; followed it there)" if any(g.get("new_tab") for g in tabs) else ""))
    dead: dict = {}
    ctx.steps.run("click_into_product", click_product)
    if dead.get("link"):
        # a store finding, not a broken journey: shown as a WARNING step with the evidence
        ctx.steps.run("every_product_link_opens_the_product",
                      lambda: ctx.expect("product link opens the product when clicked", "opens the product",
                                         dead["link"], False), soft=True)

    # product data problems are the product suite's (hard) findings; here they only warn
    p, v = _pdp_assertions(ctx, page.url, expect_buyable=allow_cart, soft_data=True)

    if not allow_cart:
        ctx.steps.info("cart_steps", "skipped (--no-cart / allow_cart_flow: false)")
        return
    _add_and_verify(ctx, p, v)
    _cart_view_and_checkout(ctx, p, cart_path)


# ---------------- cart edit + checkout page (journeys #19, #20, #21) ----------------

# The cart line of one variant and its controls, the way themes build them: a quantity box (name="updates[]",
# type=number, Dawn's .quantity__input), + / − buttons (name="plus", aria-label "Increase quantity"), a remove control
# (/cart/change?...quantity=0 link, <cart-remove-button>, "Remove" / trash icon). Marks what it found for the locators.
CART_LINE_JS = r"""([vid, title]) => {
  document.querySelectorAll('[data-radar-cart]').forEach(e => e.removeAttribute('data-radar-cart'));
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = t => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const qtySel = 'input[name="updates[]"], input[name^="updates"], input.quantity__input, input[name="quantity"], ' +
                 'input[type="number"], input[data-quantity-input], input[class*="qty" i], input[class*="quantity" i]';
  const inputs = [...document.querySelectorAll(qtySel)].filter(i => vis(i) && !i.closest('form[action*="/cart/add"]'));
  // The LINE is the input's highest ancestor that still holds only this one quantity box and no cart-level control
  // (checkout button, the cart form/page itself). Dawn puts the variant id on the input itself and the remove link in a
  // sibling of <quantity-input>, so the nearest ancestor naming the variant is too small (12 theme demos, run 38083178824).
  const cartLevel = e => e === document.body || /^(FORM|MAIN|BODY)$/.test(e.tagName) || e.matches('cart-items, cart-drawer, [role=dialog]') ||
    !!e.querySelector('[name="checkout"], a[href$="/checkout"], button[onclick*="checkout"]');
  let best = null;
  for (const inp of inputs) {
    let row = inp;
    while (row.parentElement && !cartLevel(row.parentElement) &&
           [...row.parentElement.querySelectorAll(qtySel)].filter(vis).length <= 1) row = row.parentElement;
    const html = row.outerHTML.slice(0, 40000);
    if ((vid && html.includes(String(vid))) || norm(row.innerText).includes(norm(title))) { best = {inp, row}; break; }
  }
  if (!best) return null;
  const {inp, row} = best;
  inp.setAttribute('data-radar-cart', 'qty');
  const btns = [...row.querySelectorAll('button, a, [role="button"], span[class*="plus" i], span[class*="minus" i]')].filter(vis);
  const label = b => ((b.getAttribute('name') || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + (b.className || '') +
                      ' ' + (b.getAttribute('data-action') || '') + ' ' + (b.innerText || '').trim()).toLowerCase();
  const plus = btns.find(b => /\bplus\b|increase|increment|\binc\b|add one|qty-up|\bup\b/.test(label(b)) ||
                              /^\+$/.test((b.innerText || '').trim()));
  if (plus) plus.setAttribute('data-radar-cart', 'plus');
  const rem = [...row.querySelectorAll('a[href*="/cart/change"][href*="quantity=0"], cart-remove-button a, cart-remove-button button, ' +
                                       'button, a, [role="button"]')].filter(vis)
    .find(b => /cart\/change.*quantity=0/.test(b.getAttribute('href') || '') || b.closest('cart-remove-button') ||
               /remove|delete|trash|bin\b/.test(label(b)));
  if (rem) rem.setAttribute('data-radar-cart', 'remove');
  return {qty: inp.value, plus: plus ? label(plus).slice(0, 60) : null, remove: rem ? label(rem).slice(0, 60) : null};
}"""

EMPTY_CART_RX = re.compile(r"cart is empty|bag is empty|basket is empty|no items|nothing in your (cart|bag)|"
                           r"haven.t added|cart is currently empty|empty cart|your cart is empty", re.I)


def _cart_qty(ctx: Ctx, base: str, vid: int) -> int:
    c = ctx.sess.get_json(f"{base}/cart.js")
    return sum(int(i.get("quantity") or 0) for i in c.get("items", []) if int(i.get("variant_id") or i.get("id") or 0) == vid)


def _wait_qty(ctx: Ctx, base: str, vid: int, want: int, secs: float = 8) -> int:
    got = -1
    for _ in range(int(secs / 0.6)):
        got = _cart_qty(ctx, base, vid)
        if got == want:
            break
        ctx.sess.page.wait_for_timeout(600)
    return got


def cart_edit(ctx: Ctx, url: str, variant_id: int, cart_path: str = "/cart", strict: bool = False):
    """Journeys #19-#21 in one session, on the store's own cart PAGE: raise the quantity (the line and the subtotal
    update), open the checkout page (it must render; NEVER filled, nothing typed, no payment), remove the item (the cart
    is empty). The item is put in Radar's own cart by the same request the store's buy button sends (/cart/add.js);
    the buy-button click itself is the cart suite's add_to_cart test. Warnings unless strict (not yet measured on a
    bench). Only cart actions in Radar's own browser session; no order, no form that sends data to the store."""
    base, page, vid = _base(url), ctx.sess.page, int(variant_id)
    soft = not strict
    state: dict = {}
    app_errors = _watch_app_errors(ctx)        # journey 35: a checkout app's script that answers 404 / 5xx is evidence
    ctx.steps.run("product_loads", lambda: _load(ctx, url))

    def put_in_cart():
        p = _product_js(ctx, page.url)
        v = next((x for x in p["variants"] if int(x["id"]) == vid), None)
        ctx.expect("variant in the store's product data", vid, v and v["id"], v is not None)
        state.update(p=p, v=v)
        r = ctx.sess.evaluate("""async (id) => { const r = await fetch('/cart/add.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'},
            body: JSON.stringify({id, quantity: 1})}); return r.status; }""", vid)
        ctx.expect("/cart/add.js answer", "< 400", r, (r or 999) < 400)
        q = _wait_qty(ctx, base, vid, 1, 5)
        ctx.expect("cart quantity of this product", 1, q)
        return f"{p['title']!r} ×1 in Radar's own cart"
    # soft like the rest: a cart that cannot take the item at all is cart.add_to_cart's (critical) finding, not a second
    # incident from this case (mock cart_broken: one incident for the journey, one for add_to_cart)
    if ctx.steps.run("item_in_cart", put_in_cart, soft=soft) is None:
        return
    title, price = state["p"]["title"], int(state["v"]["price"])

    def open_cart():
        _load(ctx, base + cart_path)
        _dismiss(ctx)
        found = ctx.sess.evaluate(CART_LINE_JS, [vid, title])
        if not found:       # verdict rule (13 Oct): /cart.js holds the product; a line Radar cannot recognise is not proof
            raise NotJudged(f"cart page lists the product with a quantity box: no cart line for {title!r} that Radar "
                            "recognises (the store's cart data does hold it)")
        ctx.expect("cart page lists the product with a quantity box", f"{title} line", f"line found, qty {found['qty']}", True)
        state["line"] = found
        return (f"cart page line: qty {found['qty']}; + control: {found['plus'] or 'none (quantity box)'}; "
                f"remove control: {found['remove'] or 'none found'}")
    if ctx.steps.run("cart_page_line", open_cart, soft=soft) is None:
        return

    def change_qty():
        line = state["line"]
        if line["plus"]:
            page.locator('[data-radar-cart="plus"]').first.click(timeout=8000)
            how = f"pressed + ({line['plus'][:30]!r})"
        else:
            box = page.locator('[data-radar-cart="qty"]').first
            box.fill("2")
            box.dispatch_event("change")
            box.press("Tab")
            how = "typed 2 in the quantity box"
            upd = page.locator('button[name="update"], input[name="update"]')
            if upd.count() and upd.first.is_visible():
                upd.first.click(timeout=5000)
                how += ", pressed Update"
        q = _wait_qty(ctx, base, vid, 2)
        ctx.expect("cart quantity after " + ("pressing +" if line["plus"] else "setting 2"), 2, q)
        ctx.sess.settle()
        found = ctx.sess.evaluate(CART_LINE_JS, [vid, title])
        text = ctx.sess.evaluate("() => document.body.innerText")
        ok, shown = price_shown(price * 2 / 100, prices_in_text(text))
        # soft inside: a theme may show the line price only at unit price; the quantity box must show 2
        ctx.expect("quantity shown on the page", "2", found and found["qty"], bool(found) and str(found["qty"]).strip() == "2")
        ctx.expect("line / subtotal shows 2 × price", rupees(price * 2), shown or "not on page", ok)
        return f"{how}: /cart.js quantity 1 → 2, page shows {shown}"
    ctx.steps.run("change_quantity", change_qty, soft=soft)

    def checkout_opens():
        # Journey 35: a third-party checkout app (GoKwik, Shopflo, Fastrr, Razorpay Magic, Simpl) takes over the cart's
        # checkout button; GET /checkout would walk past it. On the cart page Radar reads the page's scripts / iframes;
        # when an app is named there, it clicks the store's own checkout button like a shopper and judges what opens
        # (popup, iframe or redirect). Never types a phone number, never asks for an OTP, never submits.
        if _path(page.url) != _path(base + cart_path):
            _load(ctx, base + cart_path)
            _dismiss(ctx)
        apps = _checkout_apps_here(ctx)
        if apps:
            return _third_party_checkout(ctx, apps, title, app_errors)
        resp, _ = ctx.sess.goto(base + "/checkout")
        ctx.sess.settle()
        status, final = (resp.status if resp else None), page.url
        ctx.expect("checkout page HTTP status", "< 400", status, status is None or status < 400)
        ctx.expect("landed on the checkout", "/checkouts/… or /checkout", _path(final),
                   bool(re.search(r"/checkouts?(/|$)", urlparse(final).path)) or "checkout" in (urlparse(final).hostname or ""))
        info: dict = {}
        for _ in range(20):                              # checkout is an app: fields first, the order summary seconds later
            page.wait_for_timeout(750)
            # visible-or-collapsed TEXT only: Shopify's checkout puts ~100 kB of inline JSON <script> in the body before
            # the order summary, so body.textContent cut at 40 000 chars never reached the product name (12 theme
            # demos, run 38086965951: 24/24 'not found'). A collapsed mobile summary is still in the DOM text.
            info = ctx.sess.evaluate("""() => ({text: (() => { const out = []; let n = 0;
                const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {acceptNode: t =>
                  /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(t.parentNode.nodeName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
                while (w.nextNode() && n < 200000) { const v = w.currentNode.nodeValue; if (v.trim()) { out.push(v); n += v.length; } }
                return out.join(' ').replace(/\\s+/g, ' '); })(),
                fields: [...document.querySelectorAll('input')].filter(i => { const r = i.getBoundingClientRect();
                  return r.width > 0 && r.height > 0 && !/hidden|checkbox|radio/.test(i.type); }).length})""")
            if info["fields"] and norm_text(title) in norm_text(info["text"]):
                break
        ctx.expect("checkout shows a form a shopper would fill", "≥ 1 field (NOT filled by Radar)", f"{info['fields']} fields",
                   info["fields"] >= 1)
        ctx.expect("checkout order summary names the product", title, "named" if norm_text(title) in norm_text(info["text"])
                   else "not found in the checkout page", norm_text(title) in norm_text(info["text"]))
        return f"checkout rendered at {_path(final)} ({info['fields']} fields shown, none filled; nothing submitted)"
    ctx.steps.run("checkout_opens", checkout_opens, soft=soft)

    def remove():
        _load(ctx, base + cart_path)
        _dismiss(ctx)
        found = ctx.sess.evaluate(CART_LINE_JS, [vid, title])
        if not (found and found["remove"]):     # verdict rule (13 Oct): not found is not proof there is none
            raise NotJudged("remove control on the product's cart line: " + ("no remove control Radar recognises on the line"
                            if found else f"no cart line for {title!r} that Radar recognises"))
        ctx.expect("remove control on the product's cart line", "found", found["remove"], True)
        page.locator('[data-radar-cart="remove"]').first.click(timeout=8000)
        q = _wait_qty(ctx, base, vid, 0)
        ctx.expect("cart quantity of this product after remove", 0, q)
        ctx.sess.settle()
        text = ctx.sess.evaluate("() => document.body.innerText")
        gone = not ctx.sess.evaluate(CART_LINE_JS, [vid, title])
        ctx.expect("cart page no longer lists the product", "gone (or 'cart is empty')",
                   "gone" + (", 'empty' message shown" if EMPTY_CART_RX.search(text) else "") if gone else "still listed", gone)
        return f"pressed {found['remove'][:30]!r}: /cart.js has 0 of it, the cart page no longer lists it"
    ctx.steps.run("remove_item", remove, soft=soft)


# ---------------- checkout surfaces: Shopify's checkout or a third-party app (journeys #33 + #35) ----------------
#
# Both journeys click a store control that should open a checkout (the product page's 'Buy it now', the cart's checkout
# button under a third-party app) and judge only what a shopper SEES first. Radar NEVER types (no phone number, no
# email, no OTP request), never submits, never pays; a popup it opened is closed afterwards.

# The visible text (scripts / styles skipped: Shopify's checkout inlines ~100 kB of JSON), the fields a shopper would
# fill (counted, never touched), a phone / mobile-number box (third-party first screens), OTP wording, error wording.
CHECKOUT_VIEW_JS = r"""() => { const out = []; let n = 0;
  if (!document.body) return {text: '', fields: 0, phone: '', otp: false, error: ''};
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {acceptNode: t =>
    /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(t.parentNode.nodeName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
  while (w.nextNode() && n < 200000) { const v = w.currentNode.nodeValue; if (v.trim()) { out.push(v); n += v.length; } }
  const text = out.join(' ').replace(/\s+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const inputs = [...document.querySelectorAll('input')].filter(i => vis(i) && !/hidden|checkbox|radio/.test(i.type));
  const ph = inputs.find(i => i.type === 'tel' || /phone|mobile|contact number|whatsapp/i.test(
    [i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.autocomplete].join(' ')));
  const err = (text.match(/something went wrong|an error occurred|unable to (load|process)|failed to load|try again later|page not found|\b(404|500|502|503)\b|there was a problem/i) || [])[0] || '';
  return {text: text.slice(0, 20000), fields: inputs.length, otp: /\bOTP\b|one[- ]time password|verification code/i.test(text),
          phone: ph ? (ph.placeholder || ph.name || ph.getAttribute('aria-label') || ph.type || '').slice(0, 40) : '', error: err}; }"""

# The cart page's own clues to a checkout app: script and iframe addresses, and the page's HTML (inline app config).
PAGE_SOURCES_JS = r"""() => ({urls: [...[...document.scripts].map(s => s.src), ...[...document.querySelectorAll('iframe')].map(f => f.src || ''),
                                 ...[...document.querySelectorAll('link[href]')].map(l => l.href)].filter(Boolean).slice(0, 400),
                          html: document.documentElement.outerHTML.slice(0, 600000)})"""


def _watch_app_errors(ctx: Ctx) -> list[str]:
    """Responses >= 400 (and failed loads) for a checkout app's own files during this check: 'the app's script answered
    404' is the evidence when its checkout never opens. Never raises."""
    from radar.discovery.detect import checkout_app_of
    seen: list[str] = []

    def on_resp(r):
        try:
            if r.status >= 400 and checkout_app_of(r.url):
                seen.append(f"{checkout_app_of(r.url)} file {r.url[:110]} answered HTTP {r.status}")
        except Exception:  # noqa: BLE001  never break a page event
            pass

    def on_fail(r):
        try:
            if checkout_app_of(r.url):
                seen.append(f"{checkout_app_of(r.url)} file {r.url[:110]} did not load ({(r.failure or '')[:40]})")
        except Exception:  # noqa: BLE001
            pass
    ctx.sess.page.on("response", on_resp)
    ctx.sess.page.on("requestfailed", on_fail)
    return seen


def _checkout_apps_here(ctx: Ctx) -> list[str]:
    from radar.discovery.detect import checkout_apps_in
    src = ctx.sess.evaluate(PAGE_SOURCES_JS) or {}
    return checkout_apps_in(src.get("urls") or [], src.get("html") or "")


def checkout_view_verdict(view: dict, title: str, third_party: bool) -> tuple[bool, str]:
    """Did the checkout's FIRST screen render? Shopify's checkout: a field to fill and the product named in the order
    summary. A third-party app: its phone-number / OTP screen or an order summary naming the product. Blank, or an error
    message with neither = not rendered. Pure, unit-tested."""
    text = view.get("text") or ""
    named = bool(title) and norm_text(title) in norm_text(text)
    if len(text) < 20 and not view.get("fields"):
        return False, "blank (no text, no field)"
    if third_party:
        if view.get("phone"):
            return True, f"phone-number screen ({view['phone']!r} box shown, NOT filled)" + ("; order summary names the product" if named else "")
        if view.get("otp"):
            return True, "OTP screen shown (no OTP requested)"
        if named:
            return True, "order summary names the product"
        if view.get("error"):
            return False, f"error message {view['error']!r}, no phone screen or order summary"
        return False, f"no phone-number / OTP screen and no order summary naming the product ({len(text)} chars of text)"
    if view.get("error") and not named:
        return False, f"error message {view['error']!r}"
    if not view.get("fields"):
        return False, "no field a shopper would fill" + ("" if named else "; product not named")
    if not named:
        return False, f"{view['fields']} fields, but the order summary does not name the product"
    return True, f"{view['fields']} fields shown (none filled), order summary names the product"


def _frame_box(frame):
    try:
        el = frame.frame_element()
        return el.bounding_box() if el.is_visible() else None
    except Exception:  # noqa: BLE001  detached frame
        return None


def _click_and_find_checkout(ctx: Ctx, loc, base: str, secs: float = 12.0) -> dict:
    """Click a control that should open a checkout and see WHERE it opened: a popup (new tab / window), an iframe over
    the page (a known app's host, or any new frame covering ≥ 30% of the screen), or the page moving to a checkout
    (Shopify's /checkouts/… on the store, or another site). {kind, target (Page/Frame), url, status, app} or
    {kind: None} when nothing happened."""
    from radar.discovery.detect import checkout_app_of
    page = ctx.sess.page
    popups: list = []
    docs: list = []
    on_page = lambda p: popups.append(p)
    on_resp = lambda r: docs.append((r.url, r.status)) if r.request.resource_type == "document" and r.frame == page.main_frame else None
    page.context.on("page", on_page)
    page.on("response", on_resp)
    vp = page.viewport_size or {"width": 1366, "height": 850}
    big = lambda box: bool(box) and box["width"] * box["height"] >= 0.3 * vp["width"] * vp["height"]
    # frames already on screen and big before the click (a chat widget, KwikPass) are not what the click opened; an
    # app's hidden iframe that the click SHOWS counts
    before_url = page.url
    before_big = {f for f in page.frames if f != page.main_frame and big(_frame_box(f))}
    store_host = (urlparse(base).hostname or "").removeprefix("www.")
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
        loc.click(timeout=8000)
        t_end = time.time() + secs
        while time.time() < t_end:
            page.wait_for_timeout(500)
            if popups:
                pop = popups[0]
                try:
                    pop.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:  # noqa: BLE001
                    pass
                return {"kind": "popup", "target": pop, "url": pop.url, "status": None, "app": checkout_app_of(pop.url)}
            try:
                now = page.url
            except Exception:  # noqa: BLE001
                continue
            if now != before_url:
                host = (urlparse(now).hostname or "").removeprefix("www.")
                shopify = host == store_host and bool(re.search(r"/checkouts?(/|$)", urlparse(now).path))
                if shopify or host != store_host:
                    ctx.sess.settle()
                    st = next((s for u, s in reversed(docs) if u.split("#")[0] == page.url.split("#")[0]), None) or \
                        (docs[-1][1] if docs else None)
                    return {"kind": "redirect", "target": page, "url": page.url, "status": st,
                            "app": "" if shopify else (checkout_app_of(page.url) or host)}
            for f in page.frames:
                if f in before_big or f == page.main_frame or not f.url or f.url == "about:blank":
                    continue
                app, box = checkout_app_of(f.url), _frame_box(f)
                if big(box) or (app and box and box["width"] >= 200 and box["height"] >= 200):
                    try:
                        f.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:  # noqa: BLE001
                        pass
                    return {"kind": "iframe", "target": f, "url": f.url, "status": None,
                            "app": app or (urlparse(f.url).hostname or "")}
        if page.url != before_url:      # moved to another store page that is not a checkout: judged as landed there
            st = docs[-1][1] if docs else None
            return {"kind": "redirect", "target": page, "url": page.url, "status": st, "app": ""}
        return {"kind": None, "target": None, "url": page.url, "status": None, "app": ""}
    finally:
        page.context.remove_listener("page", on_page)
        page.remove_listener("response", on_resp)


def _judge_checkout(ctx: Ctx, opened: dict, title: str, third_party: bool, secs: float = 12.0) -> str:
    """Read the opened checkout's first screen (polling: checkouts are apps that render seconds after load) and assert
    it rendered. Nothing is typed or clicked inside it."""
    view: dict = {}
    ok, why = False, "not read"
    for _ in range(int(secs / 0.75)):
        try:
            view = opened["target"].evaluate(CHECKOUT_VIEW_JS) or {}
        except Exception:  # noqa: BLE001  the frame / page is still navigating
            view = {}
        ok, why = checkout_view_verdict(view, title, third_party)
        if ok:
            break
        ctx.sess.page.wait_for_timeout(750)
    if opened.get("status") is not None:
        ctx.expect("checkout page HTTP status", "< 400", opened["status"], opened["status"] < 400)
    ctx.expect("checkout first screen rendered" + (f" ({opened['app']})" if opened.get("app") else ""),
               "phone-number / OTP screen or order summary (nothing typed)" if third_party
               else "≥ 1 field (NOT filled by Radar) + order summary names the product", why, ok)
    return why


def _close_opened(ctx: Ctx, opened: dict) -> str:
    """Leave the checkout: a popup is closed; an iframe / redirected page is left behind by the next page load."""
    if opened.get("kind") == "popup":
        try:
            opened["target"].close()
            return "popup closed"
        except Exception:  # noqa: BLE001
            return "popup already closed"
    return "left without typing anything"


def _third_party_checkout(ctx: Ctx, apps: list[str], title: str, app_errors: list[str]) -> str:
    """Journey 35: the cart page names a checkout app. Click the store's own checkout button, name the app that opened
    and confirm its first screen (phone number / OTP screen or order summary) renders. Never typed, never submitted."""
    page = ctx.sess.page
    named = " + ".join(apps)
    try:
        f = ctx.healer.find(page, "checkout_button")
    except LocatorNotFound as e:        # verdict rule (13 Oct): a button Radar cannot find is not proof there is none
        raise NotJudged(f"checkout button on the cart page: none Radar could find ({str(e)[:160]})") from e
    ctx.expect("checkout button on the cart page", "visible + enabled", f"via {f.method}", f.locator.is_enabled())
    opened = _click_and_find_checkout(ctx, f.locator, _base(page.url))
    if not opened["kind"]:
        ctx.expect(f"{named} checkout opens from the cart's checkout button", "a popup, iframe or checkout page",
                   "nothing opened within 12 s" + (f"; {'; '.join(dict.fromkeys(app_errors))[:200]}" if app_errors else ""), False)
    third = bool(opened["app"])
    if not third:     # the app did not take over: Shopify's own checkout opened (fine for the shopper)
        ctx.expect("landed on the checkout", "/checkouts/… or /checkout", _path(opened["url"]),
                   bool(re.search(r"/checkouts?(/|$)", urlparse(opened["url"]).path)))
        why = _judge_checkout(ctx, opened, title, third_party=False)
        _close_opened(ctx, opened)
        return f"{named} on the cart page, but Shopify's checkout opened at {_path(opened['url'])}: {why}; nothing typed or submitted"
    ctx.expect("checkout app named", named, f"{opened['app']} ({opened['kind']}: {(urlparse(opened['url']).hostname or '')})", True)
    why = _judge_checkout(ctx, opened, title, third_party=True)
    left = _close_opened(ctx, opened)
    return (f"{opened['app']} checkout opened ({opened['kind']}, {urlparse(opened['url']).hostname}): {why}; "
            f"no phone number typed, no OTP requested, nothing submitted; {left}")


# ---------------- journey 33: 'Buy it now' on the product page opens a working checkout ----------------

# Shopify's dynamic checkout button (<div class="shopify-payment-button"> … 'Buy it now') or a theme / app 'Buy now'
# button for THIS product: visible, not on another product's card, not in header / footer / cart drawer. Branded
# wallet buttons (Shop Pay, G Pay, Apple Pay, PayPal) are never picked: they open payment sheets.
BUY_NOW_JS = r"""([ids, quick]) => {
  const otherCard = __OTHER_CARD__;
  document.querySelectorAll('[data-radar-target="buynow"]').forEach(e => e.removeAttribute('data-radar-target'));
  const mine = new Set(ids.map(String));
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const wallet = /shop ?pay|g ?pay|google|apple|paypal|amazon|venmo|more payment options/i;
  const c = [...document.querySelectorAll('button, a, [role="button"], input[type=submit], input[type=button]')]
    .filter(e => vis(e) && txt(e).length <= 30 && !wallet.test(txt(e))
      && (/^buy\s*(it\s*)?now\b/i.test(txt(e)) || (e.closest('.shopify-payment-button, [data-shopify="payment-button"]') && /\bbuy\b/i.test(txt(e))))
      && !e.closest('header, nav, footer, cart-drawer, [id*="cart-drawer" i], [class*="cart-drawer" i], [class*="mini-cart" i]')
      && !otherCard(e, quick));
  const own = e => { const f = e.closest('form') || (e.getAttribute('form') && document.getElementById(e.getAttribute('form')));
    const i = f && f.querySelector('[name="id"]'); return i && mine.has(String(i.value || '')) ? 1 : 0; };
  c.sort((x, y) => own(y) - own(x) || (!!y.closest('.shopify-payment-button') - !!x.closest('.shopify-payment-button')));
  const pending = !!document.querySelector('.shopify-payment-button, [data-shopify="payment-button"]') && !c.length;
  if (!c.length) return {found: null, pending};
  const on = c.filter(e => !(e.disabled || e.getAttribute('aria-disabled') === 'true'));
  const t = on[0] || c[0]; t.setAttribute('data-radar-target', 'buynow');
  return {found: {text: txt(t).slice(0, 30), off: !on.length, own: !!own(t),
                  tag: t.tagName.toLowerCase() + (typeof t.className === 'string' && t.className.trim() ? '.' + t.className.trim().split(/\s+/)[0] : '')},
          pending: false}; }""".replace("__OTHER_CARD__", OTHER_CARD_FN)


def buy_now(ctx: Ctx, url: str, variant_id: int | None = None, strict: bool = False):
    """Journey 33: the product page's 'Buy it now' (Shopify's dynamic checkout button, or a theme / checkout-app 'Buy
    now') opens a checkout that renders and shows this product. Clicked once in Radar's own browser session; the
    checkout (Shopify's /checkouts/…, or a third-party popup / iframe) is only looked at: NOTHING typed, nothing
    submitted, no payment; a popup is closed. No buy-now button = information only. Warnings unless strict.
    (The add-to-cart tests never click 'buy it now'; this is its own case.)"""
    soft = not strict
    page, base = ctx.sess.page, _base(url)
    app_errors = _watch_app_errors(ctx)
    state: dict = {}

    def load():
        out = _load(ctx, url)
        state["p"] = _product_js(ctx, page.url)
        return out
    ctx.steps.run("product_loads", load)
    p = state["p"]
    ids = [int(v["id"]) for v in p["variants"]]
    if not ctx.sess.allowed(base + "/checkouts/"):
        ctx.steps.info("buy_now_not_judged", "robots.txt disallows the checkout for Radar on this store; buy-now not clicked")
        return
    if not any(v.get("available") for v in p["variants"]):
        ctx.steps.info("buy_now_not_judged", "product is sold out now; buy-now not judged")
        return
    found: dict = {}
    for _ in range(8):           # Shopify renders the dynamic checkout button from a script after load
        found = ctx.sess.evaluate(BUY_NOW_JS, [ids, QUICK_SEL]) or {}
        if found.get("found") or not found.get("pending"):
            break
        page.wait_for_timeout(500)
    btn = found.get("found")
    if not btn:
        ctx.steps.info("buy_now_not_judged", "no buy-now button on this store" + (
            " (Shopify's payment-button slot is on the page but rendered no 'Buy it now')" if found.get("pending") else ""))
        return
    if btn["off"]:
        ctx.steps.info("buy_now_not_judged", f"the buy-now button <{btn['tag']}> {btn['text']!r} is disabled; not clicked")
        return

    def opens():
        opened = _click_and_find_checkout(ctx, page.locator('[data-radar-target="buynow"]').first, base)
        ctx.expect(f"<{btn['tag']}> {btn['text']!r} opens a checkout", "a checkout page, popup or iframe",
                   opened["kind"] or ("nothing happened within 12 s (page stayed at "
                                      f"{_path(page.url)})" + (f"; {'; '.join(dict.fromkeys(app_errors))[:200]}" if app_errors else "")),
                   bool(opened["kind"]))
        third = bool(opened["app"])
        if not third:
            ctx.expect("landed on Shopify's checkout", "/checkouts/… or /checkout", _path(opened["url"]),
                       bool(re.search(r"/checkouts?(/|$)", urlparse(opened["url"]).path)))
        why = _judge_checkout(ctx, opened, p["title"], third_party=third)
        left = _close_opened(ctx, opened)
        where = (f"{opened['app']} {opened['kind']} ({urlparse(opened['url']).hostname})" if third
                 else f"Shopify checkout at {_path(opened['url'])}")
        return f"clicked {btn['text']!r}: {where}: {why}; nothing typed or submitted, no payment; {left}"
    ctx.steps.run("buy_now_opens_checkout", opens, soft=soft)


# ---------------- journey 22: variant switch ----------------

def variant_options(v: dict) -> list[str]:
    """A variant's option values ('Large', 'Blue'): option1..3, else its title split on ' / '. Pure."""
    vals = [str(v.get(k)).strip() for k in ("option1", "option2", "option3") if v.get(k)]
    return vals or [s.strip() for s in str(v.get("title") or "").split(" / ") if s.strip() and s.strip() != "Default Title"]


def changed_options(target: dict, current: dict) -> list[str]:
    """The option values a shopper must pick to go from current to target (positions where they differ). Pure."""
    mine = variant_options(current)
    return [v for i, v in enumerate(variant_options(target)) if i >= len(mine) or mine[i].lower() != v.lower()]


def variant_switch_targets(variants: list[dict], current_id) -> tuple[dict | None, dict | None]:
    """Which variant a shopper switches to, and which sold-out variant proves 'sold out blocks buy'. Only variants
    whose option values differ from the selected one count (same values = nothing to click). Switch target: in stock
    first, then a DIFFERENT price (the price change is what the shopper must see), then the fewest options to change.
    Sold-out: the one needing the fewest option changes. Pure, unit-tested."""
    cur = next((v for v in variants if str(v.get("id")) == str(current_id)), variants[0] if variants else None)
    if cur is None:
        return None, None
    diff = lambda v: len(changed_options(v, cur))
    others = [v for v in variants if str(v.get("id")) != str(cur.get("id")) and variant_options(v) and diff(v) > 0]
    price = lambda v: int(v.get("price") or 0)
    avail = sorted((v for v in others if v.get("available")), key=lambda v: (price(v) == price(cur), diff(v)))
    sold = sorted((v for v in others if not v.get("available")), key=diff)
    return (avail[0] if avail else None), (sold[0] if sold else None)


# The control a shopper clicks for one option value: a dropdown (not Shopify's hidden id <select>), a radio's label
# (Dawn's pills), or a swatch / button / list item inside a variant / swatch / option picker. Exact text first, then
# 'Large - Sold out' style labels. Never inside another product's card, the header, the footer or a cart drawer.
OPTION_CONTROL_JS = r"""([value, quick]) => {
  const otherCard = __OTHER_CARD__;
  document.querySelectorAll('[data-radar-variant]').forEach(e => e.removeAttribute('data-radar-variant'));
  const vis = e => { if (!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = s => String(s || '').toLowerCase().replace(/\s+/g, ' ').trim();
  const want = norm(value);
  const same = t => norm(t) === want;
  const near = t => { t = norm(t); return t.length > want.length && t.startsWith(want) && !/[a-z0-9]/.test(t[want.length]); };
  const away = e => !!(e.closest(quick) || otherCard(e, quick) || e.closest('header, nav, footer, cart-drawer, ' +
    '[id*="cart-drawer" i], [class*="cart-drawer" i], [class*="mini-cart" i], [role="dialog"]'));
  const off = e => !!(e && (e.disabled || e.getAttribute('aria-disabled') === 'true'));
  const mark = (e, kind, how, extra) => { e.setAttribute('data-radar-variant', 'opt');
    return Object.assign({kind, how, disabled: off(e)}, extra || {}); };
  for (const match of [same, near]) {
    for (const sel of document.querySelectorAll('select')) {
      if (sel.name === 'id' || !vis(sel) || away(sel)) continue;
      const o = [...sel.options].find(o => match(o.value) || match(o.text));
      if (o) return mark(sel, 'select', 'dropdown option ' + JSON.stringify(o.text.trim()), {value: o.value, disabled: off(sel) || o.disabled});
    }
    for (const r of document.querySelectorAll('input[type="radio"]')) {
      const lab = (r.id && document.querySelector('label[for="' + CSS.escape(r.id) + '"]')) || r.closest('label');
      if (!(match(r.value) || (lab && match(lab.innerText))) || away(r)) continue;
      const t = lab && vis(lab) ? lab : (vis(r) ? r : null);
      if (t) return mark(t, 'click', (t === r ? 'radio ' : 'option ') + JSON.stringify((lab && lab.innerText.trim()) || r.value), {disabled: off(r)});
    }
    const picker = '[class*="variant" i], [class*="swatch" i], [class*="option" i], variant-selects, variant-radios, ' +
                   '[role="radiogroup"], [role="listbox"], fieldset';
    const hits = [...document.querySelectorAll('[data-value], [data-option-value], [role="radio"], [role="option"], button, li, a, span, div, label')]
      .filter(e => vis(e) && e.closest(picker) && !away(e) &&
        [e.getAttribute('data-value'), e.getAttribute('data-option-value'), e.getAttribute('aria-label'),
         (e.innerText || '').length <= 40 ? e.innerText : ''].some(t => t && match(t)));
    const inner = hits.filter(e => !hits.some(o => o !== e && e.contains(o)));
    if (inner.length) {
      const t = inner[0].closest('button, a, label, li, [role="radio"], [role="option"], [data-value], [data-option-value]') || inner[0];
      const cls = typeof t.className === 'string' ? t.className : '';
      return mark(t, 'click', t.tagName.toLowerCase() + ' ' + JSON.stringify((t.innerText || value).trim().slice(0, 30)),
                  {disabled: off(t) || /\b(disabled|unavailable|sold-?out)\b/i.test(cls)});
    }
  }
  return null; }""".replace("__OTHER_CARD__", OTHER_CARD_FN)

# What the product's own buy form says now: its variant id and its visible buy buttons (text, disabled). The form is
# the one holding one of THIS product's variant ids (not a quick-add card); visible beats hidden, main-named beats not.
BUY_STATE_JS = r"""([ids, quick]) => { const s = new Set(ids.map(String)); const otherCard = __OTHER_CARD__;
  const vis = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  const rows = [], seen = new Set(), all = [];
  for (const i of document.querySelectorAll('input[name="id"], select[name="id"]')) {
    if ((i.type === 'radio' || i.type === 'checkbox') && !i.checked) continue;   // per-variant radios: only the picked one
    const f = i.closest('form') || i.parentElement;
    if (!f || !s.has(String(i.value || '')) || otherCard(f, quick)) continue;
    all.push(String(i.value));
    if (seen.has(f)) continue; seen.add(f);
    const fid = f.getAttribute('id') || '';
    const btns = [...f.querySelectorAll('button, input[type=submit]'), ...(fid ? document.querySelectorAll('[form="' + CSS.escape(fid) + '"]') : [])]
      .filter(b => vis(b) && (b.type === 'submit' || /add|cart|bag|buy|sold|unavailable|stock|notify/i.test(txt(b))))
      .map(b => ({text: txt(b).slice(0, 40), off: !!(b.disabled || b.getAttribute('aria-disabled') === 'true')}));
    rows.push({variant: String(i.value), btns, vis: vis(f) || btns.length > 0 ? 1 : 0,
               main: /main|product-form|product_form/i.test(fid + ' ' + f.getAttribute('class')) ? 1 : 0});
  }
  rows.sort((x, y) => (y.vis - x.vis) || (y.main - x.main));
  return rows[0] ? Object.assign(rows[0], {all}) : null; }""".replace("__OTHER_CARD__", OTHER_CARD_FN)

SOLD_OUT_RX = re.compile(r"sold\s*out|out\s*of\s*stock|unavailable|not\s+available|notify\s+me|coming\s+soon", re.I)


def sold_out_blocks(btns: list[dict]) -> tuple[bool, str]:
    """After a sold-out variant is picked: does the product's own form stop the shopper buying it? Blocked when no
    visible buy button is left enabled with buy wording (disabled, or it says 'Sold out' / 'Notify me'). Pure."""
    if not btns:
        return True, "no buy button shown"
    live = [b for b in btns if not b.get("off") and not SOLD_OUT_RX.search(b.get("text") or "")]
    shown = ", ".join(f"{b.get('text')!r}" + (" (disabled)" if b.get("off") else "") for b in btns)
    return (not live), shown


def _variant_label(v: dict) -> str:
    return f"{v['id']} ({' / '.join(variant_options(v)) or v.get('title')}, {rupees(v['price'])})"


# Where a shopper's click on an option control lands: the control's centre, and whether the element on top there is
# part of the same picker (a swatch tooltip / transparent layer inside the option box: dotandkey.com, 12 Oct) or
# something else covering it (a popup, a sticky bar).
OPTION_HIT_JS = r"""e => { const r = e.getBoundingClientRect(); const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const h = document.elementFromPoint(x, y); if (!h) return {x, y, own: false, by: 'nothing'};
  const lab = e.closest('label') || (e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]'));
  const box = e.closest('variant-radios, variant-selects, fieldset, [role="radiogroup"], [class*="swatch" i], ' +
                        '[class*="variant" i], [class*="option" i]');
  const own = e.contains(h) || h.contains(e) || !!(lab && lab.contains(h)) ||
              !!(box && box.contains(h) && !h.closest('[role="dialog"], [aria-modal="true"]'));
  const cls = typeof h.className === 'string' ? h.className.trim().split(/\s+/)[0] : '';
  return {x, y, own, by: h.tagName.toLowerCase() + (cls ? '.' + cls : '')}; }"""


def _click_covered_option(ctx: Ctx, loc, c: dict, err: Exception) -> None:
    """Playwright would not click the option (another element receives the click). A shopper clicks where the option
    shows: when the element on top belongs to the same picker, click that point with the mouse; otherwise close what
    covers it once and retry. Still covered = the shopper cannot pick it either -> the check fails, naming the cover."""
    hit = loc.evaluate(OPTION_HIT_JS)
    if hit.get("own"):
        ctx.sess.page.mouse.click(hit["x"], hit["y"])
        return
    _dismiss(ctx)
    try:
        loc.click(timeout=3000)
    except Exception as e2:  # noqa: BLE001
        raise AssertionError(f"option {c['how']} could not be clicked: covered by {hit.get('by')} "
                             f"({str(err).splitlines()[0][:120]})") from e2


def _pick_variant(ctx: Ctx, product: dict, target: dict, current: dict) -> dict:
    """Click (or choose in a dropdown) each option value where target differs from current, like a shopper.
    Returns {how: [...], disabled: bool, missing: value or None}."""
    page, out = ctx.sess.page, {"how": [], "disabled": False, "missing": None}
    for val in changed_options(target, current):
        _dismiss(ctx)
        c = ctx.sess.evaluate(OPTION_CONTROL_JS, [val, QUICK_SEL])
        if not c:
            out["missing"] = val
            return out
        loc = page.locator('[data-radar-variant="opt"]').first
        if c["kind"] == "select":
            loc.select_option(c["value"], timeout=5000)
        elif c.get("disabled"):
            out["disabled"] = True              # the theme does not let a shopper pick it at all
        else:
            loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
            try:
                loc.click(timeout=5000)
            except Exception as e:  # noqa: BLE001
                _click_covered_option(ctx, loc, c, e)
        out["how"].append(c["how"] + (" (disabled)" if c.get("disabled") else ""))
    return out


def marks_values(chosen, values) -> bool:
    """Does the page mark every one of these option values as chosen? chosen = texts of the controls the page marks
    (checked radios and their labels, aria-checked, active swatches). A label may carry more than the value
    ('2 Pack Rs. 2,099 Save 12%' for '2 Pack'). Pure."""
    marks = [norm_text(c) for c in chosen or [] if c]
    vals = [norm_text(v) for v in values or [] if norm_text(v)]
    return bool(vals) and all(any(m == v or m.startswith(v + " ") for m in marks) for v in vals)


def switch_verdict(target_id, before_vid, form: dict | None, url_vid, marks_target: bool, price_new: bool,
                   page_vid=None) -> tuple[str, str | None, str]:
    """After picking a variant: did the page select it? -> (ok | bad | unjudged, the id the page holds, where/why).
    form = the product's own buy form ({variant, all: every id field's value}); before_vid = ?variant= in the address
    BEFORE the pick. Radar opens product links with ?variant=<first available> itself, so an address that still says
    that id is NOT the page keeping the old variant: many themes never push ?variant= (antinorm.co, veirdo.in, 11 Oct).
    A form holding the target in ANY id field counts (themes keep a no-js <select name=id> copy, sticky bars a 2nd
    form). A form that stays old while the page shows the pick (address pushed, or the new price appeared) is
    contradictory: some themes set the id only when the shopper adds to cart, which this journey never does. Pure."""
    t = str(target_id)
    url_new = str(url_vid) if url_vid and str(url_vid) != str(before_vid or "") else None
    if form:
        if form.get("variant") == t or t in (form.get("all") or []):
            return "ok", t, "the product form's variant id"
        if url_new == t or (marks_target and price_new):
            shown = ("the address now says ?variant=" + t) if url_new == t else "its option is marked chosen and its price appeared"
            return "unjudged", form.get("variant"), (f"the page shows the picked variant ({shown}) but the buy form's id "
                                                     f"stayed {form.get('variant')}; the theme may set it only on add to "
                                                     "cart, which this check never clicks")
        return "bad", form.get("variant"), "the product form's variant id"
    if url_new:
        return ("ok" if url_new == t else "bad"), url_new, "?variant= in the address (no product form)"
    if marks_target:
        return "ok", t, "the options the page marks as chosen (no product form)"
    if page_vid and str(page_vid) != t:
        return "bad", str(page_vid), "the options the page marks as chosen (no product form)"
    return "unjudged", None, ("no product form to read, the address keeps the ?variant= Radar opened the page with "
                              "(the theme does not push it), and the page marks no option as chosen")


def _price_here(ctx: Ctx) -> set:
    return set(prices_in_text(ctx.sess.evaluate("() => document.body.innerText") or ""))


def _wait_marked(ctx: Ctx, product: dict, target: dict, current: dict, before_url: str, before_prices: set,
                 secs: float = 6.0) -> dict:
    """Poll (themes re-render the product section over the network, often 1-4 s) until the page selects target:
    see switch_verdict. Re-reads the page every time (a re-rendered form is a new element)."""
    ids = [int(v["id"]) for v in product["variants"]]
    want = round(int(target["price"]) / 100, 2)
    m0 = re.search(r"[?&]variant=(\d+)", before_url or "")
    before_vid = m0.group(1) if m0 else None
    vals = changed_options(target, current)
    res: dict = {}
    for _ in range(max(1, int(secs / 0.3))):
        st = ctx.sess.evaluate(BUY_STATE_JS, [ids, QUICK_SEL])
        m = re.search(r"[?&]variant=(\d+)", ctx.sess.page.url)
        url_vid = m.group(1) if m else None
        marks = marks_values(ctx.sess.evaluate(SELECTED_OPTIONS_JS) or [], vals)
        price_new = (int(target["price"]) != int(current["price"]) and price_shown(want, _price_here(ctx))[0]
                     and not price_shown(want, before_prices)[0])
        page_vid = None
        if not st and not marks and not (url_vid and url_vid != before_vid):
            pm = _page_marked_variant(ctx, product)
            page_vid = str(pm["id"]) if pm else None
        verdict, got, where = switch_verdict(target["id"], before_vid, st, url_vid, marks, price_new, page_vid)
        res = {"verdict": verdict, "ok": verdict == "ok", "got": got, "where": where, "state": st, "url_vid": url_vid}
        if verdict == "ok":
            break
        ctx.sess.page.wait_for_timeout(300)
    return res


def _wait_price(ctx: Ctx, want: float, secs: float = 4.0) -> tuple[bool, str]:
    """The new price may arrive after the variant id (Dawn-family themes set the id at once, then fetch the
    re-rendered price section: theme-taste-demo, 11 Oct). Poll for it."""
    ok, how = False, ""
    for _ in range(max(1, int(secs / 0.3))):
        ok, how = price_shown(want, _price_here(ctx))
        if ok:
            break
        ctx.sess.page.wait_for_timeout(300)
    return ok, how


def variant_switch(ctx: Ctx, url: str, strict: bool = False):
    """Journey 22. On a product with several variants a shopper picks another one (size, colour): the page must then
    select THAT variant (its id in the buy form, or ?variant= in the address) and show its price; a sold-out variant,
    once picked, must not be buyable (buy button disabled or saying sold out). Options are clicked like a shopper (radio
    label, swatch, button, dropdown); the buy button is NEVER clicked, nothing is submitted. A page with no clickable
    option control, or whose signals cannot be read or contradict each other, is 'not judged'. Warnings unless strict
    (not yet measured on a bench)."""
    soft = not strict
    state: dict = {}
    ctx.steps.run("product_loads", lambda: _load(ctx, url))

    def options():
        p = _product_js(ctx, ctx.sess.page.url)
        cur = _selected_variant(ctx, p)
        target, sold = variant_switch_targets(p["variants"], cur["id"])
        state.update(p=p, cur=cur, target=target, sold=sold)
        return (f"{len(p['variants'])} variants; selected {_variant_label(cur)}; switch to "
                + (_variant_label(target) if target else "none (no other in-stock variant with other options)")
                + "; sold-out variant: " + (_variant_label(sold) if sold else "none"))
    if ctx.steps.run("variant_options", options, soft=soft) is None:
        return
    p, cur, target, sold = state["p"], state["cur"], state["target"], state["sold"]
    if not target and not sold:
        ctx.steps.info("variant_switch_not_judged", "no other variant with different option values to pick")
        return
    if target:
        c = ctx.sess.evaluate(OPTION_CONTROL_JS, [changed_options(target, cur)[0], QUICK_SEL])
        if not c:
            ctx.steps.info("variant_switch_not_judged", f"no visible option control for {' / '.join(variant_options(target))!r} "
                                                        "(the theme may pick variants another way)")
        else:
            def switch():
                before_url, before_prices = ctx.sess.page.url, _price_here(ctx)
                picked = _pick_variant(ctx, p, target, cur)
                ctx.expect("option control for the other variant", " / ".join(variant_options(target)),
                           ", ".join(picked["how"]) or f"none for {picked['missing']!r}", not picked["missing"])
                got = _wait_marked(ctx, p, target, cur, before_url, before_prices)
                if got["verdict"] == "unjudged":
                    state["unjudged"] = f"chose {', '.join(picked['how'])}: {got['where']}"
                    return state["unjudged"]
                ctx.expect("page selects the picked variant", _variant_label(target),
                           f"{got['got'] or 'nothing readable'} ({got['where']}) after choosing {', '.join(picked['how'])}", got["ok"])
                bits = [f"chose {', '.join(picked['how'])}: page selects {_variant_label(target)}"]
                if got["url_vid"] == str(target["id"]) and got["url_vid"] not in before_url:
                    bits.append(f"address now ?variant={target['id']}")
                if int(target["price"]) != int(cur["price"]):
                    ok, how = _wait_price(ctx, round(int(target["price"]) / 100, 2))
                    ctx.expect("new variant's price shown", rupees(target["price"]), how or "not on page", ok)
                    bits.append(f"price {rupees(cur['price'])} → {how}")
                return "; ".join(bits)
            ctx.steps.run("variant_switch", switch, soft=soft)
            if state.get("unjudged") and ctx.steps.items and ctx.steps.items[-1].name == "variant_switch":
                ctx.steps.items.pop()           # unreadable or contradictory signals: 'not judged', never a warning
                ctx.steps.info("variant_switch_not_judged", state["unjudged"])

    if sold:
        now = _selected_variant(ctx, p)

        def sold_out():
            before_url, before_prices = ctx.sess.page.url, _price_here(ctx)
            picked = _pick_variant(ctx, p, sold, now)
            if picked["missing"]:
                return f"not judged: no visible option control for {picked['missing']!r}"
            if picked["disabled"]:
                ctx.expect("sold-out variant cannot be bought", "blocked", f"its option is disabled: {', '.join(picked['how'])}", True)
                return f"{_variant_label(sold)} is sold out: its option cannot be picked ({', '.join(picked['how'])})"
            got = _wait_marked(ctx, p, sold, now, before_url, before_prices)
            if got["verdict"] == "unjudged":
                return f"chose {', '.join(picked['how'])}; not judged: {got['where']}"
            ctx.expect("page selects the sold-out variant", _variant_label(sold),
                       f"{got['got'] or 'nothing readable'} ({got['where']}) after choosing {', '.join(picked['how'])}", got["ok"])
            st = got["state"]
            if not st:
                return f"chose {', '.join(picked['how'])}; not judged: no product form to read the buy button from"
            blocked, shown = sold_out_blocks(st["btns"])
            ctx.expect("sold-out variant cannot be bought", "buy button disabled or saying sold out", shown, blocked)
            return f"chose {', '.join(picked['how'])}: {_variant_label(sold)} selected, buy blocked ({shown}); buy never clicked"
        ctx.steps.run("sold_out_blocks_buy", sold_out, soft=soft)
# ---------------- price consistency: card = product page = cart (journey #23) ----------------

# The price texts inside one element, as a shopper sees them: the DEEPEST visible elements whose short text holds a
# currency sign and a number (a theme may split '₹' and '1,299' into two spans; the parent then holds both). Each says
# whether it is struck through (<s>/<del>, line-through, a 'compare at' class): a card on sale shows the MRP struck
# and the sale price beside it, and only the sale price is what the shopper pays.
PRICE_ITEMS_FN = r"""(root) => {
  const cur = /(₹|\brs\.?\s*\d|\binr\b|\$|€|£)/i;
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const hit = e => { const t = (e.textContent || '').replace(/\s+/g, ' ').trim(); return t.length <= 40 && /\d/.test(t) && cur.test(t); };
  const struck = e => { for (let a = e; a && a !== root.parentElement; a = a.parentElement) {
      if (/^(S|DEL|STRIKE)$/.test(a.tagName)) return true;
      if ((getComputedStyle(a).textDecorationLine || '').includes('line-through')) return true;
      if (/compare|was-price|price--was|\bmrp\b|strike/i.test(typeof a.className === 'string' ? a.className : '')) return true; }
    return false; };
  const out = [];
  for (const e of root.querySelectorAll('*')) {
    if (/^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT)$/.test(e.tagName) || !hit(e) || !vis(e)) continue;
    if ([...e.children].some(hit)) continue;
    out.push({text: (e.textContent || '').replace(/\s+/g, ' ').trim(), struck: struck(e)});
    if (out.length >= 12) break;
  }
  return out; }"""

# The product card of the first of these handles that is on the collection page: the card is the link's highest
# ancestor that still links to no OTHER product (the grid around it does), never main/body. Returns its price texts.
CARD_PRICES_JS = r"""([handles]) => {
  const items = """ + PRICE_ITEMS_FN + r""";
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const hOf = a => (a.pathname.match(/\/products\/([^/?#]+)/) || [])[1];
  const links = [...document.querySelectorAll("a[href*='/products/']")].filter(vis);
  const out = {cards: 0, found: []};
  for (const h of handles) {
    const a = links.find(x => hOf(x) === h); if (!a) continue;
    out.cards++;
    let card = a;
    while (card.parentElement && !/^(MAIN|BODY|HTML|FORM|HEADER|NAV|FOOTER)$/.test(card.parentElement.tagName) &&
           !card.parentElement.matches('[role=main], #MainContent') &&
           ![...card.parentElement.querySelectorAll("a[href*='/products/']")].some(x => hOf(x) && hOf(x) !== h)) card = card.parentElement;
    out.found.push({handle: h, prices: items(card)});
    if (out.found.length >= 4) break;
  }
  return out; }"""

# The cart line of the variant Radar added: from the quantity box CART_LINE_JS marked, up to the first ancestor that
# names the product (the line row), never the cart itself. Returns that row's price texts.
CART_LINE_PRICES_JS = r"""([title]) => {
  const items = """ + PRICE_ITEMS_FN + r""";
  const norm = t => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const qtySel = 'input[name="updates[]"], input[name^="updates"], input.quantity__input, input[name="quantity"], ' +
                 'input[type="number"], input[data-quantity-input], input[class*="qty" i], input[class*="quantity" i]';
  const cartLevel = e => e === document.body || /^(FORM|MAIN|BODY|TABLE|TBODY)$/.test(e.tagName) ||
    e.matches('cart-items, cart-drawer, [role=dialog]') || !!e.querySelector('[name="checkout"], a[href$="/checkout"]');
  let row = document.querySelector('[data-radar-cart="qty"]'); if (!row) return null;
  // Round 2 (12 theme demos, cycle 28 re-run): Dawn's quantity box carries hidden 'Decrease quantity for <title>'
  // text, so stopping at the first ancestor that names the product kept only the quantity box (no price). The line is
  // the highest ancestor that still holds this one quantity box and no cart-level control (as CART_LINE_JS).
  while (row.parentElement && !cartLevel(row.parentElement) &&
         [...row.parentElement.querySelectorAll(qtySel)].filter(vis).length <= 1) row = row.parentElement;
  return items(row); }"""

_PRICE_RX = re.compile(r"(?:₹|\brs\.?|\binr\b|\$|€|£)\s*(\d[\d,]*(?:\.\d{1,2})?)|(\d[\d,]*(?:\.\d{1,2})?)\s*(?:\binr\b|₹)", re.I)
_NOT_A_PRICE_RX = re.compile(r"\bsave\b|\boff\b|discount|\bextra\b|cashback|\bper\b|/\s*\d*\s*(ml|g|kg|l|pc|unit)", re.I)


def card_price(items: list[dict]) -> tuple[float | None, bool]:
    """The price a product card asks the shopper to pay, from its price texts ({text, struck}): the lowest price that is
    not struck through (MRP / 'compare at' are struck or higher) and not a 'Save ₹300' / '20% off' badge. Second value:
    the card says 'From' (a product with several prices shows its lowest). (None, False) when no price is shown. Pure."""
    best, from_ = None, False
    for it in items or []:
        t = it.get("text") or ""
        if it.get("struck") or _NOT_A_PRICE_RX.search(t):
            continue
        for m in _PRICE_RX.finditer(t):
            try:
                n = round(float((m.group(1) or m.group(2)).replace(",", "")), 2)
            except ValueError:
                continue
            if n > 0 and (best is None or n < best):
                best, from_ = n, bool(re.search(r"\bfrom\b|\bstarting\b", t, re.I))
    return best, from_


def card_price_agrees(shown: float, want_paise: int, low_paise: int | None = None) -> tuple[bool, str]:
    """Does the card's price match the product? Equal to the selected variant's price (exact or rounded to whole rupees,
    like price_shown), or to the product's lowest variant price: cards show one price for a product, usually the lowest
    ('From ₹X' or not). Pure."""
    ok, how = price_shown(round(want_paise / 100, 2), [shown])
    if ok:
        return True, how
    if low_paise is not None and low_paise != want_paise:
        ok, how = price_shown(round(low_paise / 100, 2), [shown])
        if ok:
            return True, f"{how}, the product's lowest price"
    return False, f"₹{shown:,.2f}"


def price_consistency(ctx: Ctx, collection_url: str, product_handles: list[str] | tuple, cart_path: str = "/cart",
                      strict: bool = False):
    """Journey #23: the price on the collection card = the price on the product page = the price on the cart line, all
    checked against Shopify's own product data. A stale cached section or an app that rewrites one of them makes the
    shopper see one price and pay another. The item goes in Radar's own cart by the request the store's buy button sends
    (/cart/add.js) and is taken out again (/cart/change.js) at the end. Warnings unless strict. No form is submitted to
    the store, checkout is never opened."""
    base, page = _base(collection_url), ctx.sess.page
    soft = not strict
    state: dict = {"handle": (list(product_handles) or [None])[0]}
    if not state["handle"]:
        ctx.steps.info("not_judged", "no in-stock priced product to compare")
        return

    def card():
        _load(ctx, collection_url)
        got = ctx.sess.evaluate(CARD_PRICES_JS, [list(product_handles)[:10]]) or {"cards": 0, "found": []}
        for f in got["found"]:
            price, from_ = card_price(f["prices"])
            if price is not None:
                state.update(handle=f["handle"], card=price, card_from=from_,
                             card_texts=[("struck " if x["struck"] else "") + x["text"] for x in f["prices"]])
                return f"card of {f['handle']!r} shows {'From ' if from_ else ''}₹{price:,.2f} (price texts: " \
                       f"{'; '.join(state['card_texts'])[:120]})"
        if got["found"]:
            state["handle"] = got["found"][0]["handle"]
            return f"not judged: {len(got['found'])} product card(s) on the page show no price; card not compared"
        return "not judged: none of the products is on the collection page's first screen; card not compared"
    ctx.steps.run("collection_card_price", card, soft=soft)
    last = ctx.steps.items[-1]
    if "card" not in state and last.name == "collection_card_price" and last.status == "pass":
        last.status = "info"             # nothing to compare is not a finding

    def pdp():
        _load(ctx, f"{base}/products/{state['handle']}")
        p = _product_js(ctx, page.url)
        v = _selected_variant(ctx, p)
        if not v.get("available"):
            v = next((x for x in p["variants"] if x.get("available")), v)
        state.update(p=p, v=v, low=min((int(x["price"]) for x in p["variants"] if int(x["price"] or 0) > 0), default=None))
        ok, how = price_shown(round(int(v["price"]) / 100, 2), prices_in_text(ctx.sess.evaluate("() => document.body.innerText")))
        ctx.expect("product page shows the variant's price", rupees(v["price"]), how or "not on page", ok)
        return f"{p['title']!r} variant {v['id']}: product page shows {how} (store data {rupees(v['price'])})"
    if ctx.steps.run("product_page_price", pdp, soft=soft) is None and "v" not in state:
        return
    want = int(state["v"]["price"])

    if "card" in state:
        def compare():
            ok, how = card_price_agrees(state["card"], want, state["low"])
            ctx.expect("collection card price = product page price", rupees(want),
                       how if ok else f"card shows ₹{state['card']:,.2f} ({'; '.join(state['card_texts'])[:100]})", ok)
            return f"card {how} = product page {rupees(want)}"
        ctx.steps.run("card_matches_product_page", compare, soft=soft)

    vid = int(state["v"]["id"])

    def cart_line():
        state["before"] = _cart_qty(ctx, base, vid)
        r = ctx.sess.evaluate("""async (id) => { const r = await fetch('/cart/add.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'},
            body: JSON.stringify({id, quantity: 1})}); return r.status; }""", vid)
        ctx.expect("/cart/add.js answer", "< 400", r, (r or 999) < 400)
        state["added"] = True
        qty = _wait_qty(ctx, base, vid, state["before"] + 1, 5)
        ctx.expect("cart quantity of this product", state["before"] + 1, qty)
        _load(ctx, base + cart_path)
        _dismiss(ctx)
        title = state["p"]["title"]
        line = ctx.sess.evaluate(CART_LINE_JS, [vid, title])
        ctx.expect("cart page lists the product", f"{title} line", "line found" if line else "not found", bool(line))
        texts = ctx.sess.evaluate(CART_LINE_PRICES_JS, [title]) or []
        nums = [n for x in texts if not x["struck"] for n in prices_in_text(x["text"])]
        ok, how = price_shown(round(want * qty / 100, 2), nums)
        if not ok and qty > 1:              # a line that shows the unit price
            ok, how = price_shown(round(want / 100, 2), nums)
        if not ok:                          # a discount code / automatic discount: the regular price struck, a lower one
            struck = [n for x in texts if x["struck"] for n in prices_in_text(x["text"])]       # (craft demo FIRSTTIMER)
            for unit in {round(want * qty / 100, 2), round(want / 100, 2)}:
                if price_shown(unit, struck)[0] and any(0 < n < unit for n in nums):
                    ok, how = True, f"{rupees(round(unit * 100))} struck, discounted in the cart to {rupees(round(min(n for n in nums if 0 < n < unit) * 100))}"
                    break
        ev = "" if texts else ctx.sess.evaluate(r"""() => { const q = document.querySelector('[data-radar-cart="qty"]');
            if (!q) return 'no quantity box marked'; let r = q; for (let k = 0; k < 4 && r.parentElement; k++) r = r.parentElement;
            return r.tagName.toLowerCase() + '.' + String(r.className).slice(0, 40) + ': ' + (r.innerText || '').replace(/\s+/g, ' ').slice(0, 160); }""")
        # evidence for round 2: all 12 theme demos (cycle 28 run 38123470941) said 'no price' on the line
        ctx.expect("cart line price = product page price", rupees(want * qty),
                   how if ok else f"line shows {'; '.join(x['text'] for x in texts)[:100] or 'no price'}"
                   + (f" (line area: {ev})" if ev else ""), ok)
        return f"cart line shows {how} = product page {rupees(want)}"
    ctx.steps.run("cart_line_price", cart_line, soft=soft)

    if state.get("added"):
        def clean():
            c = ctx.sess.get_json(f"{base}/cart.js")
            line = next((n for n, i in enumerate(c.get("items", []), 1) if int(i.get("variant_id") or i.get("id") or 0) == vid), None)
            if line is None:
                return "Radar's cart no longer holds it"
            r = ctx.sess.evaluate("""async ([line, quantity]) => { const r = await fetch('/cart/change.js', {method: 'POST',
                headers: {'content-type': 'application/json', 'accept': 'application/json'},
                body: JSON.stringify({line, quantity})}); return r.status; }""", [line, state["before"]])
            ctx.expect("/cart/change.js answer", "< 400", r, (r or 999) < 400)
            q = _wait_qty(ctx, base, vid, state["before"], 5)
            ctx.expect("Radar's cart back as it was", state["before"], q)
            return f"took it out of Radar's own cart (quantity {q})"
        # housekeeping, not a price finding: always a warning at most
        ctx.steps.run("cart_cleaned_up", clean, soft=True)


REGISTRY: dict[str, Callable] = {
    "page_health": page_health, "links_resolve": links_resolve, "collection_page": collection_page,
    "product_page": product_page, "add_to_cart": add_to_cart, "search_results": search_results,
    "meta_tags": meta_tags, "not_found": not_found, "shopper_journey": shopper_journey,
    "info_pages": info_pages, "account_page": account_page,
    "search_no_results": search_no_results, "search_suggestions": search_suggestions,
    "collection_more": collection_more, "more_links_resolve": more_links_resolve, "mobile_menu": mobile_menu,
    "cart_edit": cart_edit,
    "variant_switch": variant_switch,      # journey 22
    "price_consistency": price_consistency,
    "collection_filter": collection_filter,
    "buy_now": buy_now,                    # journey 33
}


# ---------------- journey 24: a sold-out product says so, and its buy button is off ----------------

# What a store shows on a sold-out product page instead of (or next to) its buy button. English only; the match is
# read from the product's own area of the page (other products' cards excluded), never from a hidden template.
SOLD_OUT_RX = re.compile(
    r"\bsold[\s-]*out\b|\bout[\s-]+of[\s-]+stock\b|\bnotify\s+me\b|\bcoming\s+soon\b|\bunavailable\b|"
    r"\b(currently|temporarily)\s+not\s+available\b|\bno\s+longer\s+available\b|\bback\s+in\s+stock\s+soon\b|"
    r"\b(email|alert|tell|let)\s+me\s+(know\s+)?when\s+(it'?s\s+)?(back|available|in\s+stock)\b", re.I)


def sold_out_wording(text: str) -> str:
    """The 'sold out' words a page shows ('Sold out', 'Out of stock', 'Notify me when available', 'Coming soon',
    'Unavailable'), as written on the page, or ''. Pure, unit-tested."""
    m = SOLD_OUT_RX.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def sold_out_verdict(button: dict | None, wording: str) -> tuple[str, str, bool]:
    """(expected, actual, ok) for 'the shopper is told the product is sold out'. button: the product's own add-to-cart
    control ({text, enabled}) or None when the page has none. Only an ENABLED button with no sold-out wording anywhere
    near it fails: the shopper can try to buy what the store cannot sell. Pure, unit-tested."""
    exp = "buy button disabled or absent, or 'sold out' wording near it"
    says = f"; page says {wording!r}" if wording else ""
    if button is None:
        return exp, ("no add-to-cart button for this product" + (says or "; no sold-out wording either")), True
    t = (button.get("text") or "").strip()[:40] or "(no text)"
    if not button.get("enabled"):
        return exp, f"buy button {t!r} disabled{says}", True
    if wording:
        return exp, f"buy button {t!r} enabled, but the page says {wording!r}", True
    return exp, (f"buy button {t!r} ENABLED and no sold-out wording: a shopper can try to buy a product the store "
                 "cannot sell"), False


# Visible text of the product's own part of the page: <main> (or the body) without the site header / nav / footer /
# drawers / dialogs and without other products' cards or links (a 'Sold out' badge on a recommendation is about THAT
# product). Button values and aria-labels are added (innerText misses <input type=submit value="Sold out">).
SOLD_OUT_TEXT_JS = r"""([quick]) => {
  const otherCard = __OTHER_CARD__;
  const m = location.pathname.match(/\/products\/([^/?#]+)/), here = m ? decodeURIComponent(m[1]) : null;
  const prodOf = a => { const x = (a.getAttribute('href') || '').match(/\/products\/([^/?#]+)/); return x ? decodeURIComponent(x[1]) : null; };
  const CHROME = 'header, nav, footer, [role="dialog"], cart-drawer, [id*="cart-drawer" i], [class*="cart-drawer" i], ' +
                 '[class*="mini-cart" i], script, style, noscript, template';
  const shown = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none' && parseFloat(st.opacity || 1) > 0.05; };
  const other = e => { const a = e.closest('a[href*="/products/"]'); if (a && prodOf(a) && prodOf(a) !== here) return true;
    return otherCard(e, quick); };
  const root = document.querySelector('main, [role="main"], #MainContent') || document.body;
  const parts = [];
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (w.nextNode() && parts.length < 1500) {
    const t = w.currentNode.textContent.trim(), el = w.currentNode.parentElement;
    if (t && el && !el.closest(CHROME) && shown(el) && !other(el)) parts.push(t);
  }
  for (const b of root.querySelectorAll('input[type=submit], input[type=button], button[aria-label], [role=button][aria-label]'))
    if (shown(b) && !b.closest(CHROME) && !other(b)) parts.push(b.value || b.getAttribute('aria-label') || '');
  return parts.join(' \n ').slice(0, 30000); }""".replace("__OTHER_CARD__", OTHER_CARD_FN)


def _gone_product_page(ctx: Ctx, url: str) -> str | None:
    """Why the opened page is not the product at all (redirected away, 'Page not found', a hidden-product template), or
    None. The same signs as _load_product, read without asserting: product_page() owns those findings."""
    final = _path(ctx.sess.page.url)
    if not final.startswith("/products/"):
        return f"redirects to {final}"
    has_product = ctx.sess.evaluate("""() => !!document.querySelector('form[action*="/cart/add"]') ||
        [...document.querySelectorAll("script[type='application/ld+json']")].some(s => /"@type"\\s*:\\s*"Product"/.test(s.textContent))""")
    head = (ctx.sess.evaluate("() => (document.title + ' ' + ((document.querySelector('h1') || {}).innerText || '')).slice(0, 300)") or "")
    if not has_product and NOT_FOUND_RX.search(head):
        return f"shows {head.strip()[:60]!r}"
    if not _own_buy_holder(ctx, ctx.sess.page.url):
        return _hidden_product_page(ctx, ctx.sess.page.url)
    return None


def _sold_out_button(ctx: Ctx, product: dict) -> dict | None:
    """The product's OWN add-to-cart control as the page shows it now ({text, enabled, how}), or None. Never the healer:
    on a sold-out page it would happily return another product's quick-add button. Read only, never clicked."""
    page = ctx.sess.page
    ids = [int(v["id"]) for v in product["variants"]]
    found = ctx.sess.evaluate(MAIN_BUY_JS, [ids, QUICK_SEL])
    how = f"product form #{found['form'][:40]}" if found else ""
    if not found:
        found = ctx.sess.evaluate(OWN_CONTROL_JS, [product.get("handle") or _handle(page.url) or "", ids,
                                                   product.get("id") or 0, QUICK_SEL])
        how = f"the page's own buy control ({found['why']})" if found else ""
    if not found:
        return None
    loc = page.locator('[data-radar-target="buy"]').first
    # aria-disabled / a 'disabled' class count as off too: themes grey out a div or link that has no disabled property
    off = loc.evaluate("""e => !!(e.disabled || e.getAttribute('aria-disabled') === 'true' || e.closest('fieldset[disabled]')
        || /(^|\\s)(disabled|is-disabled|btn--disabled|sold-?out)(\\s|$)/i.test(typeof e.className === 'string' ? e.className : ''))""")
    return {"text": found.get("text") or "", "enabled": bool(loc.is_enabled() and not off), "how": how}


def sold_out_page(ctx: Ctx, url: str, strict: bool = False, cart_probe: bool = True):
    """Journey 24. A product the store's products.json lists as sold out: its page must TELL the shopper (the buy
    button is disabled / absent / replaced, or the page says 'Sold out', 'Out of stock', 'Notify me', 'Coming soon',
    'Unavailable' near it). An ENABLED add-to-cart with no such words is the finding: a shopper tries to buy what the
    store cannot sell. Shopify's live /products/<h>.js is read again first: a product back in stock since discovery
    is not judged. Hidden / redirected / 'not found' pages are not judged here (the product page test covers them).
    Evidence (never a failure): /cart/add.js for the variant in Radar's own session, which Shopify should refuse
    (HTTP 422); a store set to keep selling when out of stock adds it, which is the store's choice, and Radar removes
    it again. Never clicks buy, never opens checkout, never submits a 'Notify me' form. Warnings unless strict."""
    soft = not strict
    page = ctx.sess.page
    state: dict = {}

    def load():
        out = _load(ctx, url)
        state["gone"] = _gone_product_page(ctx, url)
        return out + (f"; not the product page: {state['gone']}" if state["gone"] else "")
    # hard: a product page that does not open at all (HTTP 5xx) is an outage, not a sold-out finding (mock products_down:
    # every product case must confirm it)
    ctx.steps.run("loads", load)
    if state.get("gone"):
        ctx.steps.info("sold_out_not_judged", f"not judged: {state['gone']} (the product page test covers hidden pages)")
        return

    def still_sold_out():
        p = _product_js(ctx, page.url)
        n = sum(bool(v.get("available")) for v in p["variants"])
        state.update(p=p, back=n > 0)
        return (f"{n} of {len(p['variants'])} variant(s) available now: back in stock since discovery" if n else
                f"Shopify's product data: 0 of {len(p['variants'])} variant(s) available")
    if ctx.steps.run("sold_out_in_store_data", still_sold_out, soft=soft) is None:
        return
    if state["back"]:
        ctx.steps.info("back_in_stock", "not judged: the product is in stock again (products.json said sold out at "
                                        "discovery); the product page test judges in-stock products")
        return
    p = state["p"]

    def told():
        _dismiss(ctx)
        b = _sold_out_button(ctx, p)
        text = (ctx.sess.evaluate(SOLD_OUT_TEXT_JS, [QUICK_SEL]) or "") + " \n " + ((b or {}).get("text") or "")
        exp, actual, ok = sold_out_verdict(b, sold_out_wording(text))
        ctx.expect("shopper is told the product is sold out", exp, actual + (f" ({b['how']})" if b else ""), ok)
        return actual
    ctx.steps.run("shows_sold_out", told, soft=soft)

    if not cart_probe:
        return
    vid, base = int(p["variants"][0]["id"]), _base(page.url)

    def refuses():
        before = _cart_qty(ctx, base, vid)
        r = ctx.sess.evaluate("""async (id) => { const r = await fetch('/cart/add.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'}, body: JSON.stringify({id, quantity: 1})});
            let d = ''; try { const j = await r.json(); d = j.description || j.message || ''; } catch (e) {}
            return {status: r.status, said: String(d).slice(0, 120)}; }""", vid) or {}
        st = int(r.get("status") or 0)
        said = f": {r['said']!r}" if r.get("said") else ""
        ctx.expect("/cart/add.js answer for the sold-out variant", "HTTP 422 (Shopify refuses), or added (store keeps selling)",
                   f"HTTP {st}{said}", 0 < st < 500)
        if st >= 400:
            return f"Shopify refused to add it (HTTP {st}{said})"
        added = _wait_qty(ctx, base, vid, before + 1, 4)
        ctx.sess.evaluate("""async (id) => { await fetch('/cart/change.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'},
            body: JSON.stringify({id: String(id), quantity: 0})}); }""", vid)
        left = _wait_qty(ctx, base, vid, 0, 5)
        ctx.expect("Radar's own cart cleaned up", 0, left)
        state["oversell"] = f"/cart/add.js ADDED the sold-out variant ({before} → {added} in Radar's own cart; removed again)"
        return state["oversell"]
    # evidence only (a warning at most, even when strict): the shopper-facing finding is shows_sold_out above
    ctx.steps.run("store_refuses_sold_out_add", refuses, soft=True)
    if state.get("oversell"):
        ctx.steps.info("oversell_allowed", state["oversell"] + ": the store keeps selling this product when out of stock "
                                           "(a Shopify setting, the store's choice), not a bug")


REGISTRY["sold_out_page"] = sold_out_page


# ---------------- journey 18: the cart drawer, the cart page and /cart.js agree ----------------

# What one view of the cart (the drawer Radar marked, or the cart page's main area) shows: its text, and the quantity box
# of the product's line. The line is the box's highest ancestor that still holds only this one quantity box (like
# CART_LINE_JS), and it must name the variant id or the product. Inputs of an add-to-cart form are never cart lines.
CART_VIEW_JS = r"""([rootSel, vid, title]) => {
  const root = rootSel ? document.querySelector(rootSel) : (document.querySelector('main, [role="main"], #MainContent') || document.body);
  if (!root) return null;
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const norm = t => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const qtySel = 'input[name="updates[]"], input[name^="updates"], input.quantity__input, input[name="quantity"], ' +
                 'input[type="number"], input[data-quantity-input], input[class*="qty" i], input[class*="quantity" i]';
  const inputs = [...root.querySelectorAll(qtySel)].filter(i => vis(i) && !i.closest('form[action*="/cart/add"]'));
  let qty = null;
  for (const inp of inputs) {
    let row = inp;
    while (row.parentElement && row.parentElement !== root &&
           [...row.parentElement.querySelectorAll(qtySel)].filter(vis).length <= 1) row = row.parentElement;
    if (row.outerHTML.slice(0, 40000).includes(String(vid)) || norm(row.innerText).includes(norm(title))) { qty = inp.value; break; }
  }
  return {text: (root.innerText || '').slice(0, 20000), qty, boxes: inputs.length}; }"""

# /cart.js read by the page itself (the request the theme's own drawer script makes), at the store's locale root.
CART_JSON_IN_PAGE_JS = r"""async () => { const root = ((window.Shopify || {}).routes || {}).root || '/';
  const r = await fetch(root.replace(/\/?$/, '/') + 'cart.js', {headers: {accept: 'application/json'}, cache: 'no-store'});
  if (!r.ok) return {status: r.status}; return await r.json(); }"""

_SUBTOTAL_RX = re.compile(r"\bsub\s*-?\s*total\b|\b(?:estimated |order |cart |grand )?total\b", re.I)
_NOT_SUBTOTAL_RX = re.compile(r"sav(e|ing)|discount|\boff\b|tax|shipping|free\s+delivery|reward|points?\b", re.I)
_QTY_RX = re.compile(r"\b(?:qty|quantity)\s*[:\-]?\s*(\d{1,3})\b|(?:^|\s)[x×]\s?(\d{1,3})\b", re.I)


def subtotal_in_text(text: str) -> float | None:
    """The cart subtotal a view shows: the price on the first 'Subtotal' line (else a 'Total' line), or on the line
    right after the label (themes put label and amount in two elements). Only amounts with a currency sign count, so
    'Subtotal (2 items)' is never read as ₹2; savings / tax / shipping lines are skipped. None when not shown. Pure."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for want_sub in (True, False):
        for i, ln in enumerate(lines):
            m = _SUBTOTAL_RX.search(ln)
            if not m or _NOT_SUBTOTAL_RX.search(ln) or want_sub != m.group(0).lower().startswith("sub"):
                continue
            for cand in (ln[m.end():], lines[i + 1] if i + 1 < len(lines) else ""):
                for p in _PRICE_RX.finditer(cand):
                    try:
                        n = round(float((p.group(1) or p.group(2)).replace(",", "")), 2)
                    except ValueError:
                        continue
                    if n > 0:
                        return n
    return None


def qty_in_text(text: str, title: str) -> int | None:
    """The quantity a cart view prints next to the product when it has no quantity box ('Qty: 2', 'Quantity 2', '× 2'):
    searched after the product's name on its line and on the 3 lines after it. None when no quantity is named. Pure."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    t = norm_text(title)
    for i, ln in enumerate(lines):
        if t and t in norm_text(ln):
            k = ln.lower().find((title or "").lower())
            for cand in [ln[k + len(title):] if k >= 0 else ln] + lines[i + 1:i + 4]:
                m = _QTY_RX.search(cand)
                if m:
                    return int(m.group(1) or m.group(2))
            return None
    return None


def cart_view_mismatches(where: str, view: dict, truth: dict, drawer: dict | None = None) -> list[str]:
    """What one view of the cart (the drawer or the cart page) gets wrong against /cart.js, in plain words.
    view: {listed, empty, qty, subtotal}; truth: {qty, totals (paise: total_price, items_subtotal_price, ...)};
    drawer: what the drawer showed, named in the cart page's messages. A quantity or subtotal the view does not show
    is not judged. Empty list = consistent. Pure."""
    out = []
    other = drawer if drawer is not None and drawer is not view else None
    if not view.get("listed"):
        out.append(f"{where} does not list the product" + (" and says the cart is empty" if view.get("empty") else "")
                   + (" (the cart drawer showed it)" if other and other.get("listed") else ""))
    if view.get("qty") is not None and int(view["qty"]) != int(truth["qty"]):
        d = f", the drawer showed {other['qty']}" if other and other.get("qty") is not None else ""
        out.append(f"quantity differs: {where} shows {view['qty']}, /cart.js says {truth['qty']}{d}")
    totals = [int(t) for t in truth.get("totals") or [] if t is not None]
    if view.get("subtotal") is not None and totals and \
            not any(price_shown(round(t / 100, 2), [view["subtotal"]])[0] for t in totals):
        d = f", the drawer showed ₹{other['subtotal']:,.2f}" if other and other.get("subtotal") is not None else ""
        out.append(f"subtotal differs: {where} shows ₹{view['subtotal']:,.2f}, /cart.js says {rupees(totals[0])}{d}")
    return out


def _view_summary(v: dict) -> str:
    return (("lists the product" if v.get("listed") else "product NOT listed")
            + (f", quantity {v['qty']}" if v.get("qty") is not None else ", no quantity shown")
            + (f", subtotal ₹{v['subtotal']:,.2f}" if v.get("subtotal") is not None else ", no subtotal shown"))


def _read_view(ctx: Ctx, root: str | None, vid: int, title: str) -> dict:
    got = ctx.sess.evaluate(CART_VIEW_JS, [root, vid, title]) or {"text": "", "qty": None}
    text, qty = got.get("text") or "", got.get("qty")
    try:
        qty = int(str(qty).strip()) if qty not in (None, "") else None
    except ValueError:
        qty = None
    if qty is None:
        qty = qty_in_text(text, title)
    return {"listed": norm_text(title) in norm_text(text), "empty": bool(EMPTY_CART_RX.search(text)), "qty": qty,
            "subtotal": subtotal_in_text(text)}


def _cart_truth(ctx: Ctx, base: str, vid: int) -> dict:
    """/cart.js as the page's own scripts read it (fetched in the page); the store's answer directly if that fails."""
    try:
        c = ctx.sess.evaluate(CART_JSON_IN_PAGE_JS) or {}
    except Exception:  # noqa: BLE001  a page that is navigating
        c = {}
    if "items" not in c:
        c = ctx.sess.get_json(f"{base}/cart.js")
    qty = sum(int(i.get("quantity") or 0) for i in c.get("items", []) if int(i.get("variant_id") or i.get("id") or 0) == vid)
    totals = [c.get(k) for k in ("total_price", "items_subtotal_price", "original_total_price") if c.get(k) is not None]
    return {"qty": qty, "totals": list(dict.fromkeys(totals))}


def cart_drawer(ctx: Ctx, url: str, variant_id: int | None = None, cart_path: str = "/cart", strict: bool = False):
    """Journey #18: the cart DRAWER (or mini-cart) the theme opens after adding, the cart PAGE and Shopify's own /cart.js
    must tell the shopper the same thing: the product, its quantity, the subtotal. A drawer rendered from a stale cached
    section shows the shopper one cart while the cart page and checkout hold another. The item goes in by the store's
    own buy button (the drawer only opens on that click), is read in the drawer, then on the cart page in the same
    session, each compared with /cart.js, then taken out again (/cart/change.js). No drawer after adding nor after
    clicking the cart icon (the theme goes straight to the cart page) = information, not a finding. Warnings unless
    strict (not yet measured on a bench). Checkout is never opened; no form but the store's add-to-cart is submitted."""
    base, page = _base(url), ctx.sess.page
    soft = not strict
    state: dict = {}
    ctx.steps.run("product_loads", lambda: _load(ctx, url))

    def add():
        p = _product_js(ctx, page.url)
        v = _ensure_variant(ctx, p)
        if v.get("_pincode_gate"):
            raise PincodeGate("cart drawer not tested: the buy button stays disabled until the shopper checks a delivery "
                              f"pincode ({v['_pincode_gate']}); Radar never enters one")
        ids = {int(x["id"]) for x in p["variants"]}
        vid_of = lambda i: int(i.get("variant_id") or i.get("id") or 0)
        had = {vid_of(i): int(i.get("quantity") or 0) for i in ctx.sess.get_json(f"{base}/cart.js").get("items", [])}
        _dismiss(ctx)
        loc, how, healed = _main_buy_button(ctx, p)
        _expect_buy_enabled(ctx, loc)
        loc.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
        loc.click(timeout=8000)
        added = None
        for _ in range(14):                                   # cart updates are async
            page.wait_for_timeout(600)
            items = ctx.sess.get_json(f"{base}/cart.js").get("items", [])
            added = next((vid_of(i) for i in items if vid_of(i) in ids and int(i.get("quantity") or 0) > had.get(vid_of(i), 0)), None)
            if added:
                break
        ctx.expect("the store's cart received the product", p["title"], f"variant {added}" if added else "not added", bool(added))
        state.update(p=p, vid=added, before=had.get(added, 0))
        return (f"clicked {how}: {p['title']!r} (variant {added}) is in Radar's own cart", healed)
    # soft: a buy button that adds nothing is cart.add_to_cart's (critical) finding, not a second incident from this case
    if ctx.steps.run("add_with_buy_button", add, soft=soft) is None:
        return
    title, vid = state["p"]["title"], state["vid"]

    def find_drawer() -> str:
        page.wait_for_timeout(500)
        if _path(page.url) == cart_path:               # the theme sent the shopper to the cart page
            return ""
        if ctx.sess.evaluate(DRAWER_JS):
            return "opened by itself after adding"
        link = next((c.nth(i) for sel in (f'header a[href$="{cart_path}"]', f'a[href$="{cart_path}"]')
                     for c in [page.locator(sel)] for i in range(min(c.count(), 5)) if c.nth(i).is_visible()), None)
        if link is None:
            return ""
        try:
            link.click(timeout=5000)
            ctx.sess.settle()
        except Exception:  # noqa: BLE001
            return ""
        return "opened by clicking the cart icon" if _path(page.url) != cart_path and ctx.sess.evaluate(DRAWER_JS) else ""
    how = find_drawer()
    if not how:
        ctx.steps.info("no_drawer", "info: no drawer on this theme (adding to cart and the cart icon lead to the cart "
                                    "page); the cart page is compared with /cart.js only")
    else:
        def drawer():
            v: dict = {}
            for _ in range(8):                          # drawers render their lines a moment after opening
                v = _read_view(ctx, '[data-radar-target="drawer"]', vid, title)
                if v["listed"]:
                    break
                page.wait_for_timeout(500)
            truth = _cart_truth(ctx, base, vid)
            state["drawer"] = v
            bad = cart_view_mismatches("the cart drawer", v, truth)
            ctx.expect("cart drawer = /cart.js (product, quantity, subtotal)",
                       f"lists {title}, quantity {truth['qty']}, subtotal {rupees(truth['totals'][0]) if truth['totals'] else '?'}",
                       "; ".join(bad) if bad else _view_summary(v), not bad)
            return f"drawer {how}: {_view_summary(v)}; /cart.js agrees (quantity {truth['qty']})"
        ctx.steps.run("drawer_matches_cart", drawer, soft=soft)

    def cart_page():
        if _path(page.url) != cart_path:
            _load(ctx, base + cart_path)
        _dismiss(ctx)
        v: dict = {}
        for _ in range(4):
            v = _read_view(ctx, None, vid, title)
            if v["listed"]:
                break
            page.wait_for_timeout(500)
        truth = _cart_truth(ctx, base, vid)
        bad = cart_view_mismatches("the cart page", v, truth, state.get("drawer"))
        ctx.expect("cart page = /cart.js" + (" = cart drawer" if state.get("drawer") else "") + " (product, quantity, subtotal)",
                   f"lists {title}, quantity {truth['qty']}, subtotal {rupees(truth['totals'][0]) if truth['totals'] else '?'}",
                   "; ".join(bad) if bad else _view_summary(v), not bad)
        return f"cart page {_view_summary(v)}; /cart.js agrees (quantity {truth['qty']})"
    ctx.steps.run("cart_page_matches_cart", cart_page, soft=soft)

    def clean():
        c = ctx.sess.get_json(f"{base}/cart.js")
        line = next((n for n, i in enumerate(c.get("items", []), 1) if int(i.get("variant_id") or i.get("id") or 0) == vid), None)
        if line is None:
            return "Radar's cart no longer holds it"
        r = ctx.sess.evaluate("""async ([line, quantity]) => { const r = await fetch('/cart/change.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'},
            body: JSON.stringify({line, quantity})}); return r.status; }""", [line, state["before"]])
        ctx.expect("/cart/change.js answer", "< 400", r, (r or 999) < 400)
        q = _wait_qty(ctx, base, vid, state["before"], 5)
        ctx.expect("Radar's cart back as it was", state["before"], q)
        return f"took it out of Radar's own cart (quantity {q})"
    # housekeeping, not a cart finding: always a warning at most
    ctx.steps.run("cart_cleaned_up", clean, soft=True)


REGISTRY["cart_drawer"] = cart_drawer


# ---------------- store features (14 Oct): widgets Indian D2C stores add on top of Shopify ----------------
import radar.checks.features  # noqa: E402,F401  (registers itself into REGISTRY; imported last: it needs this module's helpers)
