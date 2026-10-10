"""Checks library. A TestCase names a check; a check is a list of named steps; a step makes
explicit ASSERTIONS (what, expected, actual), all of which appear in the report.

Step kinds:
  hard (default)  a failed assertion fails the test case
  soft            a failed assertion is a WARNING (theme-dependent, or real but not shopper-blocking)

HARD RULES enforced here: never click checkout, never submit forms other than the store's
own add-to-cart, never create accounts, never enter personal data.
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
  return {marketplaces: [...new Set(mk)].slice(0, 4), own_buy: own}; }"""


def _raise_if_catalog_only(ctx: Ctx) -> None:
    """Called only when the product's price is NOT on its page: marketplace links and no buy control of its own =
    a catalog store (CatalogOnly). A page with its own buy button is judged as usual."""
    m = ctx.sess.evaluate(MARKETPLACE_JS) or {}
    if m.get("marketplaces") and not m.get("own_buy"):
        raise CatalogOnly("not tested: the product page shows no price and no buy button, only links to "
                          f"marketplaces ({', '.join(m['marketplaces'])}): the store sells there, not on its own site")


class StepFailed(Exception):
    def __init__(self, step: str, message: str, blocked: bool = False):
        self.step, self.blocked = step, blocked
        super().__init__(message)


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
        except (RobotsBlocked, AgeGate, RateLimited, PincodeGate, CatalogOnly) as e:
            self.items.append(StepResult(name, "skip", None, str(e), round(time.time() - t0, 2), None, self._checks))
            raise StepFailed(name, str(e), blocked=True) from e
        except Exception as e:  # noqa: BLE001
            msg = str(e).split("\n")[0][:400]
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
    m = re.search(r"/products/([^/?#]+)", url)
    return m.group(1) if m else None


# ---------------- shared page assertions ----------------

def _dismiss(ctx: Ctx) -> None:
    """Close popups (newsletter / cookie: decline / location). Each one is recorded as a passed
    check so the report shows exactly what Radar closed. Raises AgeGate (-> blocked)."""
    for d in dismiss_overlays(ctx.sess.page):
        ctx.expect(f"{d['kind']} popup closed", "closed without accepting or subscribing",
                   f"clicked {d['button']!r} on: {d['text']!r}", True)


def _load(ctx: Ctx, url: str) -> str:
    resp, secs = ctx.sess.goto(url)
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
    shown = ctx.sess.evaluate("""() => ({text: ((document.body && document.body.innerText) || '').trim().length,
        imgs: [...document.images].filter(i => i.getBoundingClientRect().width > 20 && i.naturalWidth > 0).length})""")
    rendered = shown["text"] >= 40 or shown["imgs"] > 0
    # An empty <title> on a page that visibly rendered is an SEO finding (the SEO test 'title' reports it as a
    # warning), not a broken page: shoppers never see the tab title. hairoriginals.com (new30e held-out, 10 Oct: every
    # page fully rendered, document.title empty, both devices) and fablestreet.com (new30c) were 'down' for this alone.
    ctx.expect("page <title>", "not empty", title or ("(empty; the page itself rendered, so this is only an SEO "
                                                      "finding, see the SEO test)" if rendered else "(empty)"),
               bool(title) or rendered)
    ctx.expect("page not blank", "≥ 40 chars of text or an image",
               f"{shown['text']} chars, {shown['imgs']} images", shown["text"] >= 40 or shown["imgs"] > 0)
    _dismiss(ctx)
    final = ctx.sess.page.url
    ctx.redirected_to = _path(final) if _path(final) != _path(url) else None
    return f"HTTP {status} in {secs}s" + (f", redirected to {ctx.redirected_to}" if ctx.redirected_to else "")


def _broken_images(sess) -> list[str]:
    return sess.evaluate("""() => [...document.images]
        .filter(i => i.complete && i.naturalWidth === 0 && i.getBoundingClientRect().width > 0)
        .map(i => i.currentSrc || i.src).slice(0, 10)""")


def _product_links(sess) -> list[dict]:
    return sess.evaluate("""() => { const seen = new Map();
        for (const a of document.querySelectorAll("a[href*='/products/']")) {
          const r = a.getBoundingClientRect(); if (r.width === 0 || r.height === 0) continue;
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
    p = ctx.sess.get_json(f"{_base(url)}/products/{h}.js")
    ctx.expect("product data from Shopify", f"/products/{h}.js with variants",
               f"{p.get('title')!r}, {len(p.get('variants', []))} variant(s)", bool(p.get("variants")))
    return p


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

CHOOSE_JS = r"""([values, quick]) => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const ok = e => !e.closest(quick);
  const done = [];
  for (const val of values) {
    const v = String(val).trim(); const low = v.toLowerCase(); let hit = false;
    for (const sel of document.querySelectorAll('select')) {                       // dropdown pickers
      if (!ok(sel)) continue;
      const o = [...sel.options].find(o => o.value.trim().toLowerCase() === low || o.text.trim().toLowerCase() === low);
      if (o) { sel.value = o.value; sel.dispatchEvent(new Event('change', {bubbles: true})); hit = true; done.push('select ' + v); break; }
    }
    if (hit) continue;
    for (const r of document.querySelectorAll('input[type="radio"]')) {           // pill / swatch radios
      if (!ok(r) || r.value.trim().toLowerCase() !== low) continue;
      const lab = (r.id && document.querySelector('label[for="' + CSS.escape(r.id) + '"]')) || r.closest('label');
      (lab && vis(lab) ? lab : r).click(); hit = true; done.push('radio ' + v); break;
    }
    if (hit) continue;
    const btn = [...document.querySelectorAll('[data-value], [data-option-value], button, li, span')]
      .find(e => ok(e) && vis(e) && e.closest('[class*="variant" i], [class*="swatch" i], [class*="option" i], variant-selects, variant-radios')
              && ((e.getAttribute('data-value') || e.getAttribute('data-option-value') || e.innerText || '').trim().toLowerCase() === low));
    if (btn) { btn.click(); done.push('button ' + v); }
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
        if not st["own"]:
            ctx.expect("add-to-cart control for THIS product on its page", "present",
                       "none: no add-to-cart form for this product"
                       + (f" ({st['other']} such forms belong to other products' cards)" if st["other"] else "")
                       + "; no other buy control found" + ("; LLM healing disabled" if "LLM healing disabled" in str(e) else ""),
                       False)
        raise
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


def collection_page(ctx: Ctx, url: str):
    ctx.steps.run("loads", lambda: _load(ctx, url))

    def has_products():
        n = len(_product_links(ctx.sess))
        ctx.expect("products listed", "≥ 1", n, n >= 1)
        return f"{n} products listed"
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


def _handles(ctx: Ctx) -> set[str]:
    return {l["handle"] for l in (_product_links(ctx.sess) or [])}


def collection_more(ctx: Ctx, url: str, products_json: str):
    """Journey 26. A shopper must be able to see the products past the first page. Radar finds the store's own way:
    a page-2 link, a 'Load more' button, or infinite scroll, and checks that NEW products appear. Judged only when the
    collection holds more in-stock products than the first page shows (from the store's own products.json).
    Hard: a page-2 link that answers an error or a blank page. Soft: no new products / no way to see more."""
    ctx.steps.run("loads", lambda: _load(ctx, url))
    first = set()

    def shown():
        nonlocal first
        first = _handles(ctx)
        ctx.expect("products on the first page", "≥ 1", len(first), len(first) >= 1)
        return f"{len(first)} products on the first page"
    ctx.steps.run("lists_products", shown)
    try:
        data = ctx.sess.get_json(products_json)
        avail = [p.get("handle") for p in (data or {}).get("products", [])
                 if any(v.get("available") for v in p.get("variants", []))]
    except Exception:  # noqa: BLE001  data unavailable: judge only by what the page offers
        avail = []
    nxt = ctx.sess.evaluate(NEXT_PAGE_JS)
    more = ctx.sess.evaluate(LOAD_MORE_JS)
    hidden = [h for h in avail if h not in first]
    if not nxt and not more and len(hidden) <= 2:
        ctx.steps.info("more_products_not_judged", f"every in-stock product of this collection fits on the first page "
                                                   f"({len(first)} shown, {len(avail)} in stock in the store's data)")
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
        else:   # infinite scroll: products appear as the shopper reaches the bottom
            for _ in range(4):
                ctx.sess.evaluate("() => scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'})")
                page.wait_for_timeout(1200)
                if _handles(ctx) - first:
                    break
            how = "scrolled to the bottom"
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
            _raise_if_catalog_only(ctx)
        where = "" if ok else _hidden_price(ctx, want)
        ctx.expect("selected variant price shown", rupees(v["price"]),
                   how if ok else f"not on page{where}", ok)
        img = ctx.sess.evaluate("""() => [...document.images].some(i => { const r = i.getBoundingClientRect();
                return r.width >= 150 && r.height >= 150 && i.naturalWidth > 0 && r.top < 1400; })""")
        ctx.expect("main product image loaded", "image ≥150px rendered", "yes" if img else "none loaded", img)
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


def product_page(ctx: Ctx, url: str, expect_buyable: bool = True, fallbacks: list[str] | tuple = ()):
    """fallbacks: other catalog products, used only when this one's page is unreachable (that is
    recorded as a warning, so the finding is never hidden, and the product checks still run)."""
    cands = [url, *list(fallbacks)[:2]]
    for i, u in enumerate(cands):
        last = i == len(cands) - 1
        ok = ctx.steps.run("loads" if i == 0 else f"loads_next_catalog_product_{i}",
                           lambda u=u: _load_product(ctx, u), soft=not last)
        if ok is not None:
            url = u
            break
    ctx.steps.run("url_stable", lambda: ctx.expect("final URL", _path(url), ctx.redirected_to or _path(url),
                                                   not ctx.redirected_to) and "no redirect", soft=True)
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
            _raise_if_catalog_only(ctx)
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


def _open_search_box(ctx: Ctx):
    """The store's visible search field on the current page, opening it first when it hides behind a search icon /
    drawer like most themes on phones. None when there is none."""
    page = ctx.sess.page
    box = ctx.sess.evaluate("""() => { const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        return !!([...document.querySelectorAll('input[type="search"], input[name="q"]')].find(vis)); }""")
    if not box:
        opener = page.locator("a[href$='/search'], [aria-label*='search' i]:not(input), summary[aria-label*='search' i], "
                              "button[class*='search' i], details-modal summary")
        for i in range(min(opener.count(), 4)):
            try:
                if opener.nth(i).is_visible():
                    _click(ctx, opener.nth(i))
                    break
            except Exception:  # noqa: BLE001
                continue
    cand = page.locator('input[type="search"], input[name="q"]')
    for i in range(min(cand.count(), 6)):
        try:
            if cand.nth(i).is_visible():
                return cand.nth(i)
        except Exception:  # noqa: BLE001
            continue
    return None


def _search_once(ctx: Ctx, url: str, step: str, soft: bool):
    term = (re.search(r"[?&]q=([^&]+)", url) or [None, ""])[1].lower()
    home = _base(url) + "/"
    ctx.steps.run({"returns_relevant_products": "open_homepage", "returns_relevant_products_other_word": "open_homepage_again"}
                  .get(step, "open_homepage_third"), lambda: _load(ctx, home))

    state: dict = {}

    def search():
        page = ctx.sess.page
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
    """Shopify's own predictive search for the word, fetched from the page (same origin, the store's own endpoint),
    plus the result count Shopify printed in the <title>. {'n': products found, 'first': a title, 'title': page title}."""
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


def meta_tags(ctx: Ctx, url: str):
    ctx.steps.run("loads", lambda: _load(ctx, url))
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


def _open_info_page(ctx: Ctx, url: str) -> str:
    """Opens like a shopper clicking the footer link: HTTP < 400 and not blank (_load), and NOT the store's
    'page not found' page served with 200, and not a redirect to the homepage (a deleted page)."""
    out = _load(ctx, url)
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


def _llm(ctx: Ctx):
    llm = getattr(ctx.healer, "llm", None) if ctx.healer else None
    return llm if (ctx.llm_assist and llm is not None and llm.enabled) else None


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
    c = next((x for x in cands if x["i"] == int(ans["index"])), None)
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


def _llm_close_overlay(ctx: Ctx) -> str | None:
    """Re-check rung: something big covers the page and Radar's rules did not close it. The model picks
    the control that CLOSES it; code refuses anything that subscribes, accepts, confirms age, or buys."""
    llm = _llm(ctx)
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


STABLE_JS = r"""(hrefRe) => { const re = new RegExp(hrefRe);
  const ls = [...document.querySelectorAll('a[href]')].filter(a => re.test(a.getAttribute('href') || '')).filter(a => {
    const r = a.getBoundingClientRect(); return r.width > 2 && r.height > 2; });
  return ls.length + '|' + ls.slice(0, 8).map(a => a.getAttribute('href') + '@' + Math.round(a.getBoundingClientRect().top + scrollY)).join(',');
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


def _landing_hop(ctx: Ctx, home: str) -> str | None:
    """Brand-landing homepages (thehouseofrare.com: MEN -> /pages/rare-rabbit): click the first top
    menu link that goes to another page of the store, like a shopper picking a section."""
    page = ctx.sess.page
    for item in (ctx.sess.evaluate(TOP_NAV_JS) or [])[:6]:
        h = item["href"]
        if not h or h.startswith(("#", "javascript")) or re.search(r"/(products|cart|account|search)\b", h):
            continue
        if _path(urljoin(home, h)) in ("/", _path(home)) or not same_site_url(urljoin(home, h), home):
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
  const re = hrefRe ? new RegExp(hrefRe) : null;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || 1) > 0.05; };
  const tag = e => e ? (e.tagName.toLowerCase() + (e.className && typeof e.className === 'string' && e.className.trim()
                    ? '.' + e.className.trim().split(/\s+/)[0] : '')).slice(0, 40) : 'nothing';
  const why = {}; const note = k => { why[k] = (why[k] || 0) + 1; };
  const usable = e => {
    if (re && !re.test(e.getAttribute('href') || '')) return false;
    if (!vis(e)) { note('not visible'); return false; }
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
    e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
    const r = e.getBoundingClientRect();
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
  return {none: true, total: all.length, why};
}"""

AIM_JS = r"""(via) => { const t = document.querySelector('[data-radar-target="click"]'); if (!t) return null;
  const c = document.querySelector('[data-radar-target="card"]') || t;
  const r = t.getBoundingClientRect(); if (r.bottom <= 0 || r.top >= innerHeight || r.width <= 0) return null;
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
    return f"none on {where} ({got['total']} links: {why})"


BIG_FIXED_LAYER_JS = r"""() => { const vw = innerWidth, vh = innerHeight;
  for (const [x, y] of [[vw / 2, vh / 2], [vw / 3, vh / 3], [2 * vw / 3, 2 * vh / 3]]) {
    let e = document.elementFromPoint(x, y);
    while (e && e.shadowRoot) { const i = e.shadowRoot.elementFromPoint(x, y); if (!i || i === e) break; e = i; }
    for (let n = e; n && n !== document.body; n = n.parentNode || n.host) {
      if (!(n instanceof Element)) continue;
      const s = getComputedStyle(n), r = n.getBoundingClientRect();
      if ((s.position === 'fixed') && r.width * r.height >= 0.25 * vw * vh && !n.closest('header, nav')) return true; } }
  return false; }"""


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
    if got and got.get("none") and any(k.startswith("covered by") for k in got.get("why", {})):
        closed = _llm_close_overlay(ctx)            # re-check after triage only
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
        n = len(_product_links(ctx.sess))
        ctx.expect("products listed in collection", "≥ 1", n, n >= 1)
        return f"clicked {how} → {_path(page.url)} ({n} products)" + (f"; {got['overlay']}" if got.get("overlay") else "")
    # soft: some themes (e.g. Origin) put no collection link on the homepage; then home -> product
    if ctx.steps.run("click_into_collection", click_collection, soft=True) is None:
        if "/collections/" not in page.url:
            _load(ctx, home)        # fresh homepage: menus/drawers opened while looking must not cover the cards

    def click_product():
        _dismiss(ctx)
        prefer = [f"/products/{h}" for h in product_handles]
        _wait_stable(ctx, PRODUCT_HREF)
        got = _pick(ctx, "a[href]", PRODUCT_HREF, prefer)
        found = not got.get("none")
        ctx.expect("product card a shopper can click", "found",
                   f"{got['href']} ({got['text']!r})" if found else _why_none(got, _path(page.url)), found)
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
  let best = null;
  for (const inp of inputs) {                 // the line: the input's nearest ancestor that names this product / variant
    let row = inp;
    for (let k = 0; k < 7 && row.parentElement; k++) {
      row = row.parentElement;
      const html = row.outerHTML.slice(0, 20000);
      if ((vid && html.includes(String(vid))) || norm(row.innerText).includes(norm(title))) { best = {inp, row}; break; }
    }
    if (best) break;
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
        ctx.expect("cart page lists the product with a quantity box", f"{title} line", found and f"line found, qty {found['qty']}",
                   bool(found))
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
        resp, _ = ctx.sess.goto(base + "/checkout")
        ctx.sess.settle()
        status, final = (resp.status if resp else None), page.url
        ctx.expect("checkout page HTTP status", "< 400", status, status is None or status < 400)
        ctx.expect("landed on the checkout", "/checkouts/… or /checkout", _path(final),
                   bool(re.search(r"/checkouts?(/|$)", urlparse(final).path)) or "checkout" in (urlparse(final).hostname or ""))
        page.wait_for_timeout(1500)                       # checkout is a single-page app
        info = ctx.sess.evaluate("""() => ({text: (document.body.textContent || '').replace(/\\s+/g, ' ').slice(0, 20000),
            fields: [...document.querySelectorAll('input')].filter(i => { const r = i.getBoundingClientRect();
              return r.width > 0 && r.height > 0 && !/hidden|checkbox|radio/.test(i.type); }).length})""")
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
        ctx.expect("remove control on the product's cart line", "found", found and (found["remove"] or "none"),
                   bool(found and found["remove"]))
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


REGISTRY: dict[str, Callable] = {
    "page_health": page_health, "links_resolve": links_resolve, "collection_page": collection_page,
    "product_page": product_page, "add_to_cart": add_to_cart, "search_results": search_results,
    "meta_tags": meta_tags, "not_found": not_found, "shopper_journey": shopper_journey,
    "info_pages": info_pages, "account_page": account_page,
    "search_no_results": search_no_results, "search_suggestions": search_suggestions,
    "collection_more": collection_more,
    "cart_edit": cart_edit,
}
