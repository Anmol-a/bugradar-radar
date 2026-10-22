"""Store-feature checks (14 Oct 2026): the widgets Indian D2C stores add on top of Shopify and that a shopper uses
before buying. Each check first looks for the feature on the page; a store without it is INFORMATION ONLY (nothing to
judge). A feature that is there but gives no answer is a finding (warning until a bench measures it; `strict` makes it
a failure). Code decides from the page's own answer; nothing is ordered, no form that sends shopper data is submitted.

  product.pincode     a delivery-pincode checker on the product page answers for a probe pincode; what the page and
                      the answer say about Cash on Delivery must agree (22 Oct)
  cart.discount_code  a discount / coupon box on the cart page rejects a junk code with a message, total unchanged
  smoke.whatsapp      a WhatsApp chat button opens a chat with a real phone number (22 Oct)
"""
from __future__ import annotations

import re
import time

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

# what a page or a pincode answer says about Cash on Delivery (22 Oct). 'COD' only in capitals: wellness stores sell
# cod liver oil ('Cod Liver Oil available in 60 capsules' is not a payment promise)
_COD = r"(?:\bCOD\b|(?i:\bcash\s+on\s+delivery\b|\bpay\s+on\s+delivery\b))"
COD_NO_RX = re.compile(_COD + r"[^.|\n]{0,25}?(?i:\b(?:not\s+(?:available|applicable|eligible|accepted|supported|offered)|unavailable)\b)"
                       r"|(?i:\bno\s+)" + _COD + r"|(?i:\bprepaid\s+(?:orders?\s+)?only\b|\bonly\s+prepaid\b)")
COD_YES_RX = re.compile(_COD + r"[^.|\n]{0,25}?(?i:\b(?:available|accepted|applicable|eligible|offered)\b)"
                        r"|(?i:\b(?:available|accepted)\b)[^.|\n]{0,15}?" + _COD)


def cod_says(text: str) -> str | None:
    """'yes' (COD offered), 'no' (not offered / prepaid only), 'mixed' (both said) or None (not mentioned). Pure."""
    t = text or ""
    no = bool(COD_NO_RX.search(t))
    yes = bool(COD_YES_RX.search(COD_NO_RX.sub(" ", t)))
    return "mixed" if yes and no else "no" if no else "yes" if yes else None


COD_WORDS = {"yes": "says COD is available", "no": "says COD is not available (prepaid only)",
             "mixed": "says both that COD is and isn't available", None: "doesn't mention COD"}


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
  window.__radarPin = {box};
  // the control: a button / submit in the same form or container, with a check-like label, else the form itself (Enter)
  let scope = box.form || box.parentElement; let btn = null;
  for (let k = 0; k < 5 && scope && !btn; k++) {
    btn = [...scope.querySelectorAll('button, input[type=submit], [role=button], a')].find(b => vis(b) &&
      /check|apply|go\b|verify|submit|find|search|ok\b|→|›|>/i.test((b.innerText || b.value || b.getAttribute('aria-label') || '').trim()));
    scope = scope.parentElement; }
  if (btn) btn.setAttribute('data-radar-pin', 'btn');
  window.__radarPin.btn = btn;
  const holder = box.closest('form, [class*="pincode" i], [class*="zip" i], [class*="delivery" i], [id*="pincode" i], section, div') || box.parentElement;
  holder.setAttribute('data-radar-pin', 'holder');
  window.__radarPin.holder = holder;
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
  let holder = null;
  for (const s of [window.__radarPin, window.__radarCode]) if (s && s.holder && document.contains(s.holder)) holder = s.holder;
  if (!holder) for (const s of [window.__radarPin, window.__radarCode]) if (s && s.box && document.contains(s.box)) holder = s.box.closest('form, section, div') || s.box.parentElement;
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
  window.__radarCode = {box};
  let scope = box.form || box.parentElement; let btn = null;
  for (let k = 0; k < 5 && scope && !btn; k++) {
    btn = [...scope.querySelectorAll('button, input[type=submit], [role=button], a')].find(b => vis(b) &&
      /apply|redeem|submit|go\b|check|use|add|ok\b|→|›|>/i.test((b.innerText || b.value || b.getAttribute('aria-label') || '').trim()));
    scope = scope.parentElement; }
  if (btn) btn.setAttribute('data-radar-code', 'btn');
  window.__radarCode.btn = btn;
  const holder = box.closest('form, [class*="discount" i], [class*="coupon" i], [class*="promo" i], section, div') || box.parentElement;
  holder.setAttribute('data-radar-code', 'holder');
  window.__radarCode.holder = holder;
  const segs = h => [...h.querySelectorAll('*'), h].map(e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim()).filter(t => t.length > 1);
  return {placeholder: (box.placeholder || box.name || box.id || '').slice(0, 40), button: btn ? (btn.innerText || btn.value || '').trim().slice(0, 30) : null,
          before: segs(holder).slice(0, 80),
          extraBefore: [...document.querySelectorAll('[role=alert], [role=status], [aria-live], .message, .error, .toast, .notification, ' +
    '[class*="toast" i], [class*="alert" i], [class*="message" i], [class*="error" i], [class*="success" i], [class*="estimate" i], [class*="delivery" i]')].filter(vis)
            .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(t => t.length > 2).slice(0, 40)}; }"""


FINDERS = {"pin": ("__radarPin", PINCODE_BOX_JS), "code": ("__radarCode", DISCOUNT_BOX_JS)}


def _el(ctx: Ctx, kind: str, part: str):
    """The widget's box / button / holder as a live element handle. App-rendered widgets (mcaffeine, plumgoodness,
    boat-lifestyle, 14-15 Oct) re-render themselves and drop any marker Radar set: when the remembered node has left
    the document, the finder runs again and the new node is used."""
    store, finder = FINDERS[kind]
    page = ctx.sess.page
    page.evaluate(f"() => {{ const s = window.{store}; if (!s || !s.box || !document.contains(s.box)) ({finder})(); }}")
    h = page.evaluate_handle(f"() => (window.{store} || {{}}).{part} || null")
    el = h.as_element()
    if el is None:
        raise AssertionError(f"the {kind} widget's {part} is no longer on the page")
    return el


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


BUSY_RX = re.compile(r"checking|loading|please wait|fetching|verifying|\.\.\.|…", re.I)


INVALID_PIN_RX = re.compile(r"(enter|valid|correct)\b[^.]{0,20}\bpin ?code|pin ?code[^.]{0,20}\b(invalid|not valid)", re.I)


def _answer_after(ctx: Ctx, snap: dict, rx: re.Pattern, secs: float = 8.0, busy_secs: float = 25.0,
                  not_this: str | None = None) -> tuple[str | None, str]:
    """Poll the page for text matching rx (holder text that was not there before, or a message element). Returns
    (matched text or None, what was seen last). While the widget says it is still working ('Checking servicability
    ...': boat-lifestyle, 15 Oct) the wait stretches to busy_secs: slow is a warning with the time, not 'no answer'."""
    page, seen, t0 = ctx.sess.page, "", time.time()
    limit = secs
    while time.time() - t0 < limit:
        page.wait_for_timeout(500)
        got = ctx.sess.evaluate(NEW_TEXT_JS, {"before": snap.get("before") or [], "extraBefore": snap.get("extraBefore") or []}) or {}
        texts = [got.get("holder") or ""] + list(got.get("extra") or [])
        seen = " | ".join(t for t in texts if t)[:300]
        for t in texts:
            if t and rx.search(t) and not (BUSY_RX.search(t) and len(t) < 40) and t[:200] != (not_this or "")[:200]:
                took = round(time.time() - t0, 1)
                return (t[:200] + (f" (after {took} s)" if took > secs else "")), seen
        if BUSY_RX.search(seen):
            limit = busy_secs
    return None, seen


def _type_into(ctx: Ctx, kind: str, text: str) -> None:
    """Scroll the box to the middle of the screen (a sticky buy bar or header can cover it: mcaffeine, plumgoodness,
    boat-lifestyle on the first real run, 14 Oct), close any popup, then fill it like a shopper."""
    _dismiss(ctx)
    box = _el(ctx, kind, "box")
    box.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})")
    try:
        box.click(timeout=4000)
    except Exception:  # noqa: BLE001  covered by a sticky bar: focus it directly, typing still goes through the page
        _el(ctx, kind, "box").focus()
    _el(ctx, kind, "box").fill(text)
    # an app-rendered widget that redraws itself can drop the text typed into the node it just replaced: type again
    for _ in range(3):
        try:
            if _el(ctx, kind, "box").input_value(timeout=2000) == text:
                break
            _el(ctx, kind, "box").fill(text)
        except Exception:  # noqa: BLE001  redrawn mid-read: read the new node
            pass


def _press(ctx: Ctx, kind: str) -> str:
    """Click the feature's own button like a shopper (scrolled to the middle, popups closed), or press Enter in the
    box when it has none."""
    store = FINDERS[kind][0]
    has_btn = ctx.sess.page.evaluate(f"() => !!((window.{store} || {{}}).btn)")
    if has_btn:
        try:
            _click(ctx, _el(ctx, kind, "btn"))
            return "pressed its button"
        except Exception as e:  # noqa: BLE001
            # still covered after scrolling (a page that ends right under a sticky bar): fire the button's own click
            # handler, then Enter in the box as well, the two things a shopper's tap / keyboard would do
            _el(ctx, kind, "btn").dispatch_event("click")
            _el(ctx, kind, "box").press("Enter")
            m = re.search(r"<[^>]{0,120}>[^\n]{0,40}intercepts pointer events", str(e))
            why = m.group(0)[:160] if m else str(e).splitlines()[0][:60]
            return f"its button is covered ({why}): clicked it through the cover and pressed Enter"
    _el(ctx, kind, "box").press("Enter")
    return "pressed Enter in the box"


def pincode_check(ctx: Ctx, url: str, probe: str = PROBE_PINCODE, strict: bool = False):
    """A delivery-pincode checker on the product page must answer for a real pincode (deliverable or not: both are
    answers). No checker on the page = information only. A checker that stays silent after a check = finding."""
    soft = not strict
    ctx.steps.run("product_loads", lambda: (_load(ctx, url), _dismiss(ctx))[0])
    # Cash on Delivery (22 Oct): what the page promises, read before anything is typed
    page_cod = cod_says(ctx.sess.evaluate("() => ((document.body && document.body.innerText) || '').slice(0, 200000)") or "")
    found = ctx.sess.evaluate(PINCODE_BOX_JS)
    if not found:
        ctx.steps.info("pincode_not_judged", "no delivery-pincode checker on the product page (information only)")
        ctx.steps.info("cod", f"Cash on Delivery: the product page {COD_WORDS[page_cod]}")
        return
    ctx.steps.info("pincode_box", f"pincode box {found['placeholder']!r}" + (f", button {found['button']!r}" if found['button'] else ", no button (Enter)"))
    errors = _watch_server_errors(ctx)

    def answers():
        _type_into(ctx, "pin", probe)
        how = _press(ctx, "pin")
        hit, seen = _answer_after(ctx, found, PIN_ANSWER_RX)
        if hit and INVALID_PIN_RX.search(hit):
            # a real 6-digit pincode called invalid: the widget redrew and lost the text between typing and the press
            # (pincode_rerender, radar-tests 38891961164): type and press once more, like a shopper would
            first = hit
            _type_into(ctx, "pin", probe)
            how = _press(ctx, "pin") + " (typed again: the first press said the pincode was not valid)"
            # the first answer is still on the page: wait for a NEW one (radar-tests 39054711928 still raced)
            hit, seen = _answer_after(ctx, found, PIN_ANSWER_RX, not_this=first)
            hit = hit or first
        if hit:
            ctx.expect(f"pincode checker answers for {probe}", "a delivery answer", hit, True)
            answer["text"] = hit
            return f"typed {probe}, {how}: {hit!r}"
        waited = "25 s (it kept saying it was checking)" if BUSY_RX.search(seen or "") else "8 s"
        why = f"nothing within {waited} ({how}); page showed: {seen[:120]!r}" if seen else f"nothing within {waited} ({how})"
        if errors:
            why += "; " + "; ".join(errors[:2])
        ctx.expect(f"pincode checker answers for {probe}", "a delivery answer (deliverable / not deliverable / days)", why, False)
    answer: dict = {}
    ctx.steps.run("pincode_answers", answers, soft=soft)
    if "text" not in answer:
        ctx.steps.info("cod", f"Cash on Delivery: the product page {COD_WORDS[page_cod]}")
        return
    ans_cod = cod_says(answer["text"])
    ctx.steps.info("cod", f"Cash on Delivery: the product page {COD_WORDS[page_cod]}; the pincode answer for {probe} "
                          f"{COD_WORDS[ans_cod]}")
    if {page_cod, ans_cod} == {"yes", "no"}:
        # the page promises one thing, the checker for a metro pincode (Mumbai GPO) says the other: a shopper choosing
        # how to pay is told two stories (a warning: the store may limit COD by product or order value)
        def agrees():
            ctx.expect(f"product page and pincode answer agree about Cash on Delivery for {probe}", "the same answer",
                       f"the page {COD_WORDS[page_cod]}, the pincode checker {COD_WORDS[ans_cod]} "
                       f"({answer['text'][:100]!r})", False)
        ctx.steps.run("cod_agrees", agrees, soft=True)


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


# ---- WhatsApp chat button (22 Oct) ------------------------------------------------------------------------------------
# Indian D2C stores put a floating "Chat on WhatsApp" button on every page; a wrong number in it (a typo, no country code,
# a placeholder left by the theme app) sends every shopper with a question to a dead chat, and nobody at the store sees
# it. Radar reads the number the button opens WITHOUT opening WhatsApp: links are read from their address; a button
# without an address is tapped once with every way out to WhatsApp caught (window.open, navigation, a new tab: aborted)
# and the address it tried to open is read instead. Nothing is sent.
WA_HOST_RX = re.compile(r"^(?:https?://)?(?:wa\.me|(?:api|web|chat)\.whatsapp\.com|(?:www\.)?whatsapp\.com)(?:/|$)|^whatsapp:", re.I)
WA_ROUTES = ("**://wa.me/**", "**://*.whatsapp.com/**", "**://whatsapp.com/**")

# visible WhatsApp links and buttons: [{kind: 'link'|'widget', href, label, fixed}], marked data-radar-wa=<i>
WA_FIND_JS = r"""() => {
  const HREF = /^(?:https?:\/\/)?(?:wa\.me|(?:api|web|chat)\.whatsapp\.com|(?:www\.)?whatsapp\.com)(?:\/|$)|^whatsapp:/i;
  const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width >= 8 && r.height >= 8 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
  const fixed = (e) => { for (let x = e; x && x !== document.body; x = x.parentElement) { const p = getComputedStyle(x).position;
    if (p === 'fixed' || p === 'sticky') return true; } return false; };
  const label = (e) => (e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || (e.querySelector('img') || {}).alt || '').trim().slice(0, 60);
  document.querySelectorAll('[data-radar-wa]').forEach(e => e.removeAttribute('data-radar-wa'));
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    if (!HREF.test(h.trim()) || /whatsapp\.com\/channel\//i.test(h) || !seen(a)) continue;
    a.setAttribute('data-radar-wa', String(out.length));
    out.push({kind: 'link', href: a.href || h, label: label(a), fixed: fixed(a)});
  }
  const NAME = /whats\s*-?app/i;
  for (const e of document.querySelectorAll('button, [role=button], div, span, img, svg, a:not([href])')) {
    if (out.length >= 8) break;
    const id = [e.id, typeof e.className === 'string' ? e.className : (e.className && e.className.baseVal) || '',
                e.getAttribute('aria-label') || '', e.getAttribute('title') || '', e.getAttribute('alt') || ''].join(' ');
    if (!NAME.test(id) || /share/i.test(id) || !seen(e) || e.closest('[data-radar-wa]') || e.querySelector('[data-radar-wa]')) continue;
    const r = e.getBoundingClientRect();
    if (r.width > 500 || r.height > 300) continue;            // a section that only mentions WhatsApp, not a button
    const target = e.closest('button, [role=button], a') || e;
    if (target.hasAttribute('data-radar-wa')) continue;
    target.setAttribute('data-radar-wa', String(out.length));
    out.push({kind: 'widget', href: '', label: label(target), fixed: fixed(target)});
  }
  return out;
}"""


def wa_number(url: str) -> tuple[str, str]:
    """(kind, number) for a WhatsApp address. kind: 'chat' (a number to chat with), 'share' (opens WhatsApp to share a
    text, no number: a product's share button), 'empty' (no number, no text), 'other' (not a chat address). Pure."""
    from urllib.parse import parse_qs, unquote, urlparse
    u = (url or "").strip()
    if not WA_HOST_RX.search(u):
        return "other", ""
    p = urlparse(u if "://" in u or u.lower().startswith("whatsapp:") else "https://" + u)
    q = parse_qs(p.query)
    host = (p.hostname or "").lower()
    num = ""
    if host == "wa.me":
        num = unquote(p.path.strip("/").split("/")[0]) if p.path.strip("/") else ""
    if not num:
        num = unquote((q.get("phone") or [""])[0])
    num = num.strip()
    if num:
        return "chat", num
    if (q.get("text") or [""])[0]:
        return "share", ""
    if host == "wa.me" or "send" in p.path or u.lower().startswith("whatsapp:"):
        return "empty", ""
    return "other", ""


def wa_judge(num: str) -> tuple[str, str]:
    """('ok' | 'bad' | 'warn', plain reason) for the number a WhatsApp chat button opens. WhatsApp wants the full
    international number, digits only, no leading 0 or + (wa.me/919876543210). Pure, unit-tested."""
    digits = re.sub(r"[\s\-().+]", "", num or "")
    if not digits:
        return "warn", "opens WhatsApp without a phone number, so the shopper lands on an empty chat picker"
    if not digits.isdigit():
        return "bad", f"opens a chat with {num!r}, which is not a phone number"
    if digits.startswith("0"):
        return "bad", f"opens a chat with {digits}, which starts with 0: WhatsApp needs the country code (91 for India) instead"
    if not 8 <= len(digits) <= 15:
        return "bad", f"opens a chat with {digits}, which has {len(digits)} digits: not a complete phone number"
    if len(digits) == 10 and digits[0] in "6789":
        return "warn", (f"opens a chat with {digits}, an Indian mobile number without the country code: WhatsApp may "
                        f"open the wrong chat or none (it should be 91{digits})")
    return "ok", f"opens a chat with +{digits}"


def whatsapp_button(ctx: Ctx, home: str, product: str | None = None, strict: bool = False):
    """A WhatsApp chat button on the store must open a chat with a real phone number. No button on the homepage (nor on
    the product page) = information only. A number that cannot be a phone number = failure (positive evidence: every
    shopper who taps it reaches no one); a likely missing country code, no number, or a button that does nothing when
    tapped = warning (`strict` makes those failures)."""
    page = ctx.sess.page
    ctx.steps.run("page_loads", lambda: (_load(ctx, home), _dismiss(ctx))[0])
    found, where = ctx.sess.evaluate(WA_FIND_JS) or [], "homepage"
    if not found and product:
        ctx.steps.run("product_page_loads", lambda: (_load(ctx, product), _dismiss(ctx))[0])
        found, where = ctx.sess.evaluate(WA_FIND_JS) or [], "product page"
    if not found:
        ctx.steps.info("whatsapp_not_judged", "no WhatsApp chat button on the homepage or a product page (information only)")
        return
    links = [f for f in found if f["kind"] == "link"]
    chats = [(f, *wa_number(f["href"])) for f in links]
    shares = [c for c in chats if c[1] == "share"]
    chats = [c for c in chats if c[1] in ("chat", "empty")]
    if shares:
        ctx.steps.info("whatsapp_share", f"{len(shares)} WhatsApp share link(s) (share a product, no number): not a chat button")
    if not chats:
        widgets = [i for i, f in enumerate(found) if f["kind"] == "widget"]
        if not widgets:
            ctx.steps.info("whatsapp_not_judged", "only WhatsApp share links on the page, no chat button (information only)")
            return
        got = _tap_whatsapp_widget(ctx, sorted(widgets, key=lambda i: not found[i]["fixed"])[0], found)
        if got is None:
            def dead():
                ctx.expect("WhatsApp button opens a chat", "a WhatsApp address when tapped",
                           f"nothing within 5 s of tapping {found[widgets[0]]['label'] or 'the WhatsApp button'!r} on the {where}", False)
            ctx.steps.run("whatsapp_button_responds", dead, soft=not strict)
            return
        kind, num = wa_number(got)
        if kind == "share":
            ctx.steps.info("whatsapp_share", f"the WhatsApp button opens a share screen ({got[:80]}), not a chat: nothing to judge")
            return
        chats = [({"href": got, "label": found[widgets[0]]["label"], "kind": "widget"}, kind, num)]
    judged: dict[str, tuple[str, str, dict]] = {}
    for f, kind, num in chats:
        key = re.sub(r"\D", "", num) or "(none)"
        if key not in judged:
            judged[key] = (*wa_judge(num if kind == "chat" else ""), f)
    for n, (key, (verdict, why, f)) in enumerate(list(judged.items())[:3]):
        name = f"whatsapp_number{'' if n == 0 else '_' + str(n + 1)}"
        who = f"{f.get('label') or 'WhatsApp button'!r} on the {where}"

        def check(verdict=verdict, why=why, who=who, href=f.get("href", "")):
            ctx.expect(f"WhatsApp button {who} opens a chat with a real number", "a full international number (91…)",
                       f"{why} ({href[:90]})", verdict == "ok")
            return f"{who}: {why}"
        ctx.steps.run(name, check, soft=(verdict == "warn" and not strict))


def _tap_whatsapp_widget(ctx: Ctx, i: int, found: list) -> str | None:
    """Tap a WhatsApp button that has no address of its own and catch where it tries to go, without letting it get
    there: window.open is replaced for the tap, and any request to WhatsApp (this page or a new tab) is aborted. A
    panel that opens with a WhatsApp link in it counts too (its link is read). Returns the WhatsApp address or None."""
    page, caught = ctx.sess.page, []

    def stop(route):
        caught.append(route.request.url)
        try:
            route.abort()
        except Exception:  # noqa: BLE001
            pass
    for pat in WA_ROUTES:
        page.context.route(pat, stop)
    popups: list = []

    def on_page(p):                       # a new tab the button opened (Playwright needs a plain function here)
        popups.append(p)
    page.context.on("page", on_page)
    try:
        page.evaluate("""() => { window.__radarWaOpen = []; const o = window.open;
            window.open = function (u) { try { window.__radarWaOpen.push(String(u || '')); } catch (e) {} return null; }; }""")
        el = page.locator(f'[data-radar-wa="{i}"]').first
        try:
            _click(ctx, el)
        except Exception:  # noqa: BLE001  covered by a popup we could not close: its own click handler
            el.dispatch_event("click")
        for _ in range(10):
            page.wait_for_timeout(500)
            opened = [u for u in (page.evaluate("() => window.__radarWaOpen || []") or []) if WA_HOST_RX.search(u)]
            panel = [f["href"] for f in (ctx.sess.evaluate(WA_FIND_JS) or []) if f["kind"] == "link"]
            for u in opened + caught + [p.url for p in popups] + panel:
                if u and WA_HOST_RX.search(u):
                    return u
        return None
    finally:
        try:
            page.context.remove_listener("page", on_page)
        except Exception:  # noqa: BLE001
            pass
        for p in popups:
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
        for pat in WA_ROUTES:
            try:
                page.context.unroute(pat, stop)
            except Exception:  # noqa: BLE001
                pass


REGISTRY["pincode_check"] = pincode_check
REGISTRY["discount_code"] = discount_code
REGISTRY["whatsapp_button"] = whatsapp_button
