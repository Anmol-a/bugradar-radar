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

from radar.core.browser import Session, RobotsBlocked
from radar.core.models import StepResult
from radar.discovery.shopify_data import (parse_product_data, price_ok, assess_add, parse_sent_variant_ids,
                                          rupees, norm_text, prices_in_text)
from radar.core.overlays import dismiss_overlays, AgeGate
from radar.healing.locator import Healer, QUICK_SEL, LocatorNotFound, OTHER_CARD_FN


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
        except (RobotsBlocked, AgeGate) as e:
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
        ctx.expect("HTTP status", "< 400", status, status < 400)
    title = (ctx.sess.evaluate("() => document.title") or "").strip()
    ctx.expect("page <title>", "not empty", title or "(empty)", bool(title))
    shown = ctx.sess.evaluate("""() => ({text: ((document.body && document.body.innerText) || '').trim().length,
        imgs: [...document.images].filter(i => i.getBoundingClientRect().width > 20 && i.naturalWidth > 0).length})""")
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
    """Variant currently selected on the page: the main form's id input, else ?variant=, else first available."""
    vids = [int(v["id"]) for v in product["variants"]]
    val = ctx.sess.evaluate("""(ids) => { const s = new Set(ids.map(String));
        for (const f of document.querySelectorAll('form[action*="/cart/add"]')) {
          const i = f.querySelector('[name="id"]'); if (i && s.has(String(i.value))) return i.value; }
        return null; }""", vids)
    m = re.search(r"[?&]variant=(\d+)", ctx.sess.page.url)
    vid = int(val) if val else (int(m.group(1)) if m else None)
    v = next((x for x in product["variants"] if int(x["id"]) == vid), None)
    return v or next((x for x in product["variants"] if x.get("available")), product["variants"][0])


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
                text: (b.innerText || b.value || '').trim().slice(0, 40)});
    }
  }
  out.sort((x, y) => y.main - x.main);
  if (!out.length) return null;
  out[0].b.setAttribute('data-radar-target', 'buy');
  return {form: out[0].form, variant: out[0].variant, text: out[0].text, candidates: out.length};
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
    values = [target.get(k) for k in ("option1", "option2", "option3") if target.get(k)]
    picked = ctx.sess.evaluate(CHOOSE_JS, [values, QUICK_SEL]) if values else []
    ctx.sess.page.wait_for_timeout(700)
    st = ctx.sess.evaluate(FORM_STATE_JS, [ids, QUICK_SEL])
    got = st["variant"] if st else "(no product form)"
    ctx.expect("variant selected like a shopper", f"{target['id']} ({' / '.join(values) or target.get('title')})",
               f"{got} after choosing {', '.join(picked) or 'nothing (no matching picker)'}",
               bool(st) and str(st["variant"]) == str(target["id"]) and not st["disabled"])
    return target


def _main_buy_button(ctx: Ctx, product: dict):
    """The product's OWN add-to-cart button: in a cart/add form whose variant belongs to this
    product, not inside a product card / quick-add / recommendation. Falls back to the healer."""
    page = ctx.sess.page
    ids = [int(v["id"]) for v in product["variants"]]
    found = ctx.sess.evaluate(MAIN_BUY_JS, [ids, QUICK_SEL])
    if found:
        loc = page.locator('[data-radar-target="buy"]').first
        return loc, f"main product form #{found['form']} (variant {found['variant']}, button {found['text']!r})", None
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
    page = ctx.sess.page
    base = _base(page.url)
    before = ctx.sess.get_json(f"{base}/cart.js")

    def click():
        _dismiss(ctx)
        loc, how, healed = _main_buy_button(ctx, product)
        ctx.expect("buy button enabled", True, loc.is_enabled())
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


def _hidden_price(ctx: Ctx, want: float) -> str:
    """The price is not in the visible text: is it in the page at all, only hidden? (supplysix.com, bench 4:
    '₹ 199.00' only in a sticky bar the theme hides on desktop, class 'hidden-lap-and-up'). Says where,
    so the failure (a shopper on this screen sees no price) is not mistaken for Radar looking wrong."""
    for r in ctx.sess.evaluate(HIDDEN_PRICE_JS) or []:
        if want in prices_in_text(r["text"]):
            return f" (₹{want:,.2f} is only inside a hidden element <{r['by'][:60]}>, not shown on this screen size)"
    return ""


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
        if want not in nums:        # price in a sticky bar / lazy block that appears on scroll (supplysix.com, bench 3)
            ctx.sess.evaluate("""async () => { const h = document.body.scrollHeight;
                for (let y = 0; y <= Math.min(h, 12000); y += Math.round(innerHeight * 0.8)) { scrollTo(0, y); await new Promise(r => setTimeout(r, 250)); }
                scrollTo(0, Math.round(innerHeight * 0.6)); await new Promise(r => setTimeout(r, 400)); }""")
            text = ctx.sess.evaluate("() => document.body.innerText")
            nums = prices_in_text(text)
            ctx.sess.evaluate("() => scrollTo(0, 0)")
        where = "" if want in nums else _hidden_price(ctx, want)
        ctx.expect("selected variant price shown", rupees(v["price"]),
                   rupees(v["price"]) if want in nums else f"not on page{where}", want in nums)
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
    ctx.steps.run("structured_data_valid", structured, soft=soft_data)

    if expect_buyable:
        def variant_ready():
            state["v"] = _ensure_variant(ctx, p)
            return f"variant {state['v']['id']} ({state['v'].get('title')}) selected, in stock"
        ctx.steps.run("variant_ready", variant_ready)

        def buy():
            loc, how, healed = _main_buy_button(ctx, p)
            ctx.expect("buy button enabled", True, loc.is_enabled())
            return (how, healed)
        ctx.steps.run("buy_button_ready", buy)
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


def add_to_cart(ctx: Ctx, url: str, variant_id: int | None = None, cart_path: str = "/cart"):
    _capture_add_requests(ctx)
    ctx.steps.run("product_loads", lambda: _load(ctx, url))
    state = {}

    def identify():
        p = _product_js(ctx, ctx.sess.page.url)
        v = next((x for x in p["variants"] if variant_id and int(x["id"]) == int(variant_id)), None) or _selected_variant(ctx, p)
        state.update(p=p, v=v)
        return f"{p['title']!r}, variant {v['id']} at {rupees(v['price'])}"
    ctx.steps.run("product_identified", identify)

    def variant_ready():
        cur = _ensure_variant(ctx, state["p"])
        if variant_id and int(cur["id"]) != int(variant_id) and next(
                (x for x in state["p"]["variants"] if int(x["id"]) == int(variant_id) and x.get("available")), None):
            cur = next(x for x in state["p"]["variants"] if int(x["id"]) == int(variant_id))
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


def _search_once(ctx: Ctx, url: str, step: str, soft: bool):
    term = (re.search(r"[?&]q=([^&]+)", url) or [None, ""])[1].lower()
    home = _base(url) + "/"
    ctx.steps.run({"returns_relevant_products": "open_homepage", "returns_relevant_products_other_word": "open_homepage_again"}
                  .get(step, "open_homepage_third"), lambda: _load(ctx, home))

    def search():
        page = ctx.sess.page
        _dismiss(ctx)
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
        field = None
        cand = page.locator('input[type="search"], input[name="q"]')
        for i in range(min(cand.count(), 6)):
            if cand.nth(i).is_visible():
                field = cand.nth(i)
                break
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
        links = _product_links(ctx.sess)
        ctx.expect("product results", "≥ 1", len(links), len(links) >= 1)
        match = [l for l in links if term in (l["text"] + " " + l["handle"]).lower()]
        ctx.expect(f"results relevant to '{term}'", "≥ 1 result mentions the term",
                   f"{len(match)} of {len(links)}", len(match) >= 1)
        return f"{how}: {len(links)} results, {len(match)} mention '{term}'"
    return ctx.steps.run(step, search, soft=soft)


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

CLICK_CHECK_JS = r"""([x, y]) => { const t = document.querySelector('[data-radar-target="click"]'); if (!t) return false;
  const c = document.querySelector('[data-radar-target="card"]') || t; const h = document.elementFromPoint(x, y);
  return !!h && (h === t || t.contains(h) || (c.contains(h) && !h.closest('button, input, select, form, [role="button"]'))); }"""


def _why_none(got: dict | None, where: str) -> str:
    """'none on /collections/men (62 links: 58 covered by div.swiper, 4 not visible)'."""
    if not got or not got.get("total"):
        return f"none on {where} (no matching links on the page)"
    why = ", ".join(f"{n} {k}" for k, n in sorted(got.get("why", {}).items(), key=lambda kv: -kv[1])[:3])
    return f"none on {where} ({got['total']} links: {why})"


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
    try:
        page.wait_for_url(lambda u: u != before, timeout=8000)
    except Exception:  # noqa: BLE001  same-page links / slow stores: the caller's assertion decides
        pass
    ctx.sess.settle()


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
        _click_picked(ctx, got)
        again = ""
        if _handle(page.url) != clicked and page.url == listing:
            # nothing happened: the grid was re-rendered under the click (dotandkey.com, bench 3). A shopper clicks again.
            _wait_stable(ctx, PRODUCT_HREF)
            got2 = _pick(ctx, "a[href]", PRODUCT_HREF, [f"/products/{clicked}"] + prefer)
            if not got2.get("none"):
                clicked = _handle(got2["href"])
                _click_picked(ctx, got2)
                again = " (first click did not open it: the page re-rendered; clicked again)"
        ctx.expect("opened the product that was clicked", f"/products/{clicked}", _path(page.url),
                   _handle(page.url) == clicked)
        known = "known in-stock product" if clicked in product_handles else "first clickable product"
        return (f"clicked {known} on {got.get('considered')} candidates → {_path(page.url)}{again}"
                + (f"; {got['overlay']}" if got.get("overlay") else "") + (" (via its card)" if got.get("via") == "card" else ""))
    ctx.steps.run("click_into_product", click_product)

    # product data problems are the product suite's (hard) findings; here they only warn
    p, v = _pdp_assertions(ctx, page.url, expect_buyable=allow_cart, soft_data=True)

    if not allow_cart:
        ctx.steps.info("cart_steps", "skipped (--no-cart / allow_cart_flow: false)")
        return
    _add_and_verify(ctx, p, v)
    _cart_view_and_checkout(ctx, p, cart_path)


REGISTRY: dict[str, Callable] = {
    "page_health": page_health, "links_resolve": links_resolve, "collection_page": collection_page,
    "product_page": product_page, "add_to_cart": add_to_cart, "search_results": search_results,
    "meta_tags": meta_tags, "not_found": not_found, "shopper_journey": shopper_journey,
}
