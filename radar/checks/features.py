"""Store-feature checks (14 Oct 2026): the widgets Indian D2C stores add on top of Shopify and that a shopper uses
before buying. Each check first looks for the feature on the page; a store without it is INFORMATION ONLY (nothing to
judge). A feature that is there but gives no answer is a finding (warning until a bench measures it; `strict` makes it
a failure). Code decides from the page's own answer; nothing is ordered, no form that sends shopper data is submitted.

  product.pincode     a delivery-pincode checker on the product page answers for a probe pincode
  cart.discount_code  a discount / coupon box on the cart page rejects a junk code with a message, total unchanged
"""
from __future__ import annotations

import re

from radar.checks.library import REGISTRY, Ctx, _base, _click, _dismiss, _load, _product_js, _wait_qty

PROBE_PINCODE = "400001"            # Mumbai GPO: a real, deliverable-by-everyone pincode; the store's answer is not judged
JUNK_CODE = "BUGRADAR-TEST-CODE"    # a code no store has: the box must say so, not stay silent

# one answer a pincode checker gives (deliverable or not: both mean the checker works)
PIN_ANSWER_RX = re.compile(
    r"deliver|available|serviceable|\bcod\b|cash on delivery|express|\bdays?\b|\bby\s+(mon|tue|wed|thu|fri|sat|sun|\d)|"
    r"expected|arriv|dispatch|ship|unavailable|not\s+service|sorry|invalid|enter\s+a\s+valid|out\s+of\s+(our\s+)?(delivery|service)",
    re.I)
# what a cart says about a code it does not know
CODE_REJECT_RX = re.compile(
    r"invalid|not\s+valid|isn.?t\s+valid|does\s*n.?t\s+exist|expired|unable|cannot|can.?t\s+be|no\s+such|incorrect|"
    r"enter\s+a\s+valid|not\s+applicable|unavailable|sorry|wrong|not\s+found|doesn.?t\s+match|not\s+recogni", re.I)

# find a visible text box that asks for a pincode / zip, and the control next to it; marks them for Playwright
PINCODE_BOX_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || 1) > 0.05; };
  const RX = /pin\s*-?\s*code|zip\s*-?\s*code|postal|post\s*code|\bpin\b|delivery.*(code|pin)|check.*(delivery|availability)/i;
  document.querySelectorAll('[data-radar-pin]').forEach(e => e.removeAttribute('data-radar-pin'));
  const boxes = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit])')]
    .filter(i => vis(i) && RX.test([i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.className,
                                    (i.labels && i.labels[0] ? i.labels[0].innerText : '')].join(' ')))
    .filter(i => !/search|email|phone|mobile|otp|name|coupon|discount|promo/i.test([i.name, i.id, i.placeholder].join(' ')));
  if (!boxes.length) return null;
  const box = boxes[0]; box.setAttribute('data-radar-pin', 'box');
  // the control: a button / submit in the same form or container, with a check-like label, else the form itself (Enter)
  let scope = box.form || box.parentElement; let btn = null;
  for (let k = 0; k < 5 && scope && !btn; k++) {
    btn = [...scope.querySelectorAll('button, input[type=submit], [role=button], a')].find(b => vis(b) &&
      /check|apply|go\b|verify|submit|find|search|ok\b|→|›|>/i.test((b.innerText || b.value || b.getAttribute('aria-label') || '').trim()));
    scope = scope.parentElement; }
  if (btn) btn.setAttribute('data-radar-pin', 'btn');
  const holder = box.closest('form, [class*="pincode" i], [class*="zip" i], [class*="delivery" i], [id*="pincode" i], section, div') || box.parentElement;
  holder.setAttribute('data-radar-pin', 'holder');
  const segs = h => [...h.querySelectorAll('*'), h].map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim()).filter(t => t.length > 1);
  return {placeholder: (box.placeholder || box.name || box.id || '').slice(0, 40), button: btn ? (btn.innerText || btn.value || '').trim().slice(0, 30) : null,
          before: segs(holder).slice(0, 80),
          extraBefore: [...document.querySelectorAll('[role=alert], [role=status], [aria-live], .message, .error, .toast, .notification, ' +
    '[class*="toast" i], [class*="alert" i], [class*="message" i], [class*="error" i], [class*="success" i], [class*="estimate" i], [class*="delivery" i]')].filter(vis)
            .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t.length > 2).slice(0, 40)}; }"""

# text that appeared on the page since the probe was typed: the holder first, then any element shown now but not before
NEW_TEXT_JS = r"""(snap) => { const before = snap.before, extraBefore = new Set(snap.extraBefore || []);
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && parseFloat(s.opacity || 1) > 0.05; };
  const holder = document.querySelector('[data-radar-pin="holder"], [data-radar-code="holder"]');
  const segs = h => [...h.querySelectorAll('*'), h].map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim()).filter(t => t.length > 1);
  // only text that was NOT in the holder before the probe (its label and button do not count as an answer)
  const was = new Set(before || []);
  const fresh = holder ? segs(holder).filter(t => !was.has(t)).join(' | ').slice(0, 900) : '';
  // toasts / messages drawn elsewhere (role=alert, status, .message, .error, .toast, .notification)
  const extra = [...document.querySelectorAll('[role=alert], [role=status], [aria-live], .message, .error, .toast, .notification, ' +
    '[class*="toast" i], [class*="alert" i], [class*="message" i], [class*="error" i], [class*="success" i], [class*="estimate" i], [class*="delivery" i]')]
    .filter(vis).map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t.length > 2 && t.length < 300 && !extraBefore.has(t));
  return {holder: fresh.slice(0, 400), extra: [...new Set(extra)].slice(0, 6)}; }"""

DISCOUNT_BOX_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || 1) > 0.05; };
  const RX = /discount|coupon|promo|voucher|offer\s*code|gift\s*card|\bcode\b/i;
  document.querySelectorAll('[data-radar-code]').forEach(e => e.removeAttribute('data-radar-code'));
  const boxes = [...document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit])')]
    .filter(i => vis(i) && RX.test([i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.className,
                                    (i.labels && i.labels[0] ? i.labels[0].innerText : '')].join(' ')))
    .filter(i => !/pin\s*code|zip|postal|search|email|phone|otp/i.test([i.name, i.id, i.placeholder].join(' ')));
  if (!boxes.length) return null;
  const box = boxes[0]; box.setAttribute('data-radar-code', 'box');
  let scope = box.form || box.parentElement; let btn = null;
  for (let k = 0; k < 5 && scope && !btn; k++) {
    btn = [...scope.querySelectorAll('button, input[type=submit], [role=button], a')].find(b => vis(b) &&
      /apply|redeem|submit|go\b|check|use|add|ok\b|→|›|>/i.test((b.innerText || b.value || b.getAttribute('aria-label') || '').trim()));
    scope = scope.parentElement; }
  if (btn) btn.setAttribute('data-radar-code', 'btn');
  const holder = box.closest('form, [class*="discount" i], [class*="coupon" i], [class*="promo" i], section, div') || box.parentElement;
  holder.setAttribute('data-radar-code', 'holder');
  const segs = h => [...h.querySelectorAll('*'), h].map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim()).filter(t => t.length > 1);
  return {placeholder: (box.placeholder || box.name || box.id || '').slice(0, 40), button: btn ? (btn.innerText || btn.value || '').trim().slice(0, 30) : null,
          before: segs(holder).slice(0, 80),
          extraBefore: [...document.querySelectorAll('[role=alert], [role=status], [aria-live], .message, .error, .toast, .notification, ' +
    '[class*="toast" i], [class*="alert" i], [class*="message" i], [class*="error" i], [class*="success" i], [class*="estimate" i], [class*="delivery" i]')].filter(vis)
            .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t.length > 2).slice(0, 40)}; }"""


def _watch_server_errors(ctx: Ctx) -> list[str]:
    """HTTP >= 500 answers to the store's own address while a feature is used: the evidence when it stays silent."""
    seen: list[str] = []
    host = (ctx.sess.page.url.split("/")[2] if "://" in ctx.sess.page.url else "").removeprefix("www.")

    def on_resp(r):
        try:
            if r.status >= 500 and host and host in r.url:
                seen.append(f"{r.url.split('?')[0][:100]} answered HTTP {r.status}")
        except Exception:  # noqa: BLE001
            pass
    ctx.sess.page.on("response", on_resp)
    return seen


def _answer_after(ctx: Ctx, snap: dict, rx: re.Pattern, secs: float = 8.0) -> tuple[str | None, str]:
    """Poll the page for text matching rx (holder text that was not there before, or a message element). Returns
    (matched text or None, what was seen last)."""
    page, seen = ctx.sess.page, ""
    for _ in range(int(secs * 2)):
        page.wait_for_timeout(500)
        got = ctx.sess.evaluate(NEW_TEXT_JS, {"before": snap.get("before") or [], "extraBefore": snap.get("extraBefore") or []}) or {}
        texts = [got.get("holder") or ""] + list(got.get("extra") or [])
        seen = " | ".join(t for t in texts if t)[:300]
        for t in texts:
            if t and rx.search(t):
                return t[:200], seen
    return None, seen


def _type_into(ctx: Ctx, kind: str, text: str) -> None:
    """Scroll the box to the middle of the screen (a sticky buy bar or header can cover it: mcaffeine, plumgoodness,
    boat-lifestyle on the first real run, 14 Oct), close any popup, then fill it like a shopper."""
    _dismiss(ctx)
    box = ctx.sess.page.locator(f'[data-radar-{kind}="box"]').first
    box.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
    try:
        box.click(timeout=4000)
    except Exception:  # noqa: BLE001  covered by a sticky bar: focus it directly, typing still goes through the page
        box.focus()
    box.fill(text)


def _press(ctx: Ctx, kind: str) -> str:
    """Click the feature's own button like a shopper (scrolled to the middle, popups closed), or press Enter in the
    box when it has none."""
    page = ctx.sess.page
    btn = page.locator(f'[data-radar-{kind}="btn"]').first
    if btn.count():
        try:
            _click(ctx, btn)
            return "pressed its button"
        except Exception as e:  # noqa: BLE001
            # still covered after scrolling (a page that ends right under a sticky bar): fire the button's own click
            # handler, then Enter in the box as well, the two things a shopper's tap / keyboard would do
            btn.dispatch_event("click")
            page.locator(f'[data-radar-{kind}="box"]').first.press("Enter")
            m = re.search(r"<[^>]{0,120}>[^\n]{0,40}intercepts pointer events", str(e))
            why = m.group(0)[:160] if m else str(e).splitlines()[0][:60]
            return f"its button is covered ({why}): clicked it through the cover and pressed Enter"
    page.locator(f'[data-radar-{kind}="box"]').first.press("Enter")
    return "pressed Enter in the box"


def pincode_check(ctx: Ctx, url: str, probe: str = PROBE_PINCODE, strict: bool = False):
    """A delivery-pincode checker on the product page must answer for a real pincode (deliverable or not: both are
    answers). No checker on the page = information only. A checker that stays silent after a check = finding."""
    soft = not strict
    ctx.steps.run("product_loads", lambda: (_load(ctx, url), _dismiss(ctx))[0])
    found = ctx.sess.evaluate(PINCODE_BOX_JS)
    if not found:
        ctx.steps.info("pincode_not_judged", "no delivery-pincode checker on the product page (information only)")
        return
    ctx.steps.info("pincode_box", f"pincode box {found['placeholder']!r}" + (f", button {found['button']!r}" if found['button'] else ", no button (Enter)"))
    errors = _watch_server_errors(ctx)

    def answers():
        _type_into(ctx, "pin", probe)
        how = _press(ctx, "pin")
        hit, seen = _answer_after(ctx, found, PIN_ANSWER_RX)
        if hit:
            ctx.expect(f"pincode checker answers for {probe}", "a delivery answer", hit, True)
            return f"typed {probe}, {how}: {hit!r}"
        why = f"nothing within 8 s ({how}); page showed: {seen[:120]!r}" if seen else f"nothing within 8 s ({how})"
        if errors:
            why += "; " + "; ".join(errors[:2])
        ctx.expect(f"pincode checker answers for {probe}", "a delivery answer (deliverable / not deliverable / days)", why, False)
    ctx.steps.run("pincode_answers", answers, soft=soft)


def discount_code(ctx: Ctx, url: str, variant_id: int, cart_path: str = "/cart", strict: bool = False):
    """A discount / coupon box on the cart page must reject a code no store has with a message, and leave the total
    as it was. Shopify's own code box lives on the checkout page (never filled by Radar): a cart without a box is
    information only. The item goes into Radar's own cart and is taken out again."""
    base, page, vid = _base(url), ctx.sess.page, int(variant_id)
    soft = not strict
    state: dict = {}
    ctx.steps.run("product_loads", lambda: _load(ctx, url))

    def put_in_cart():
        p = _product_js(ctx, page.url)
        v = next((x for x in p["variants"] if int(x["id"]) == vid), None)
        ctx.expect("variant in the store's product data", vid, v and v["id"], v is not None)
        r = ctx.sess.evaluate("""async (id) => { const r = await fetch('/cart/add.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'},
            body: JSON.stringify({id, quantity: 1})}); return r.status; }""", vid)
        ctx.expect("/cart/add.js answer", "< 400", r, (r or 999) < 400)
        q = _wait_qty(ctx, base, vid, 1, 5)
        ctx.expect("cart quantity of this product", 1, q)
        state["p"] = p
        return f"{p['title']!r} ×1 in Radar's own cart"
    if ctx.steps.run("item_in_cart", put_in_cart, soft=soft) is None:
        return

    def open_cart():
        _load(ctx, base + cart_path)
        _dismiss(ctx)
        c = ctx.sess.get_json(f"{base}/cart.js")
        state["total"] = int(c.get("total_price") or 0)
        found = ctx.sess.evaluate(DISCOUNT_BOX_JS)
        state["box"] = found
        if not found:
            return "no discount / coupon box on the cart page (Shopify asks for codes on the checkout page, which Radar never fills)"
        return f"discount box {found['placeholder']!r}" + (f", button {found['button']!r}" if found['button'] else ", no button (Enter)")
    ctx.steps.run("cart_page", open_cart, soft=soft)
    if state.get("box"):
        errors = _watch_server_errors(ctx)

        def rejects():
            _type_into(ctx, "code", JUNK_CODE)
            how = _press(ctx, "code")
            hit, seen = _answer_after(ctx, state["box"], CODE_REJECT_RX)
            c = ctx.sess.get_json(f"{base}/cart.js")
            total = int(c.get("total_price") or 0)
            ctx.expect("cart total after a code no store has", state["total"], total)
            if hit:
                ctx.expect(f"cart says the code {JUNK_CODE} is not valid", "a 'not valid' message", hit, True)
                return f"{how}: {hit!r}; total unchanged"
            why = f"no message within 8 s ({how}); page showed: {seen[:120]!r}" if seen else f"no message within 8 s ({how})"
            if errors:
                why += "; " + "; ".join(errors[:2])
            ctx.expect(f"cart says the code {JUNK_CODE} is not valid", "a 'not valid' message", why, False)
        ctx.steps.run("junk_code_rejected", rejects, soft=soft)

    def clean():
        c = ctx.sess.get_json(f"{base}/cart.js")
        line = next((n for n, i in enumerate(c.get("items", []), 1) if int(i.get("variant_id") or i.get("id") or 0) == vid), None)
        if line is None:
            return "Radar's cart no longer holds it"
        r = ctx.sess.evaluate("""async (line) => { const r = await fetch('/cart/change.js', {method: 'POST',
            headers: {'content-type': 'application/json', 'accept': 'application/json'},
            body: JSON.stringify({line, quantity: 0})}); return r.status; }""", line)
        ctx.expect("/cart/change.js answer", "< 400", r, (r or 999) < 400)
        q = _wait_qty(ctx, base, vid, 0, 5)
        ctx.expect("Radar's cart empty again", 0, q)
        return "took it out of Radar's own cart"
    ctx.steps.run("cart_cleaned_up", clean, soft=True)


REGISTRY["pincode_check"] = pincode_check
REGISTRY["discount_code"] = discount_code
