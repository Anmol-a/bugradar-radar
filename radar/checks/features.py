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

from radar.checks.library import REGISTRY, Ctx, _base, _click, _dismiss, _load, _product_js, _wait_qty, same_site_url

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

# what the product's buy area or a pincode answer says about Cash on Delivery (22 Oct; tightened after review). Read
# phrase by phrase (split at . , ; | • & and line ends) so 'COD available, returns not applicable' is about COD and
# returns separately. 'COD' only in capitals and never 'COD liver oil' (themes upper-case product names); questions
# ('check if COD is available') are not promises; 'no COD charges' means COD exists.
_COD = r"(?:\bCOD\b(?!(?i:\s*liver))|(?i:\bcash\s+on\s+delivery\b|\bpay\s+on\s+delivery\b))"
_NOT = r"(?i:(?:is\s+)?not|isn['’]?t|cannot|can['’]?t)\s+(?i:be\s+)?"
_STATE = r"(?i:available|applicable|eligible|accepted|supported|offered|possible)"
COD_NO_RX = re.compile(
    _COD + r".{0,25}?\b" + _NOT + _STATE + r"\b"                                  # COD is not available
    + r"|" + _COD + r".{0,25}?(?i:\bunavailable\b)"                              # COD unavailable
    + r"|(?i:\b(?:not|isn['’]?t)\s+(?:available|applicable|eligible)\s+(?:for|on|with)\s+)" + _COD   # not available for COD
    + r"|(?i:\b(?:do|does|we)\s*(?:not|n['’]?t)\s+(?:offer|accept|provide|support)\s+)" + _COD      # we don't offer COD
    + r"|(?i:\bno\s+)" + _COD + r"(?!(?i:\s+(?:charges?|fees?|extra|handling)))")                    # no COD (not 'no COD charges')
COD_ALONE_NO_RX = re.compile(r"\s*(?i:prepaid\s+(?:orders?\s+)?only|only\s+prepaid(?:\s+orders?)?)\s*")
COD_YES_RX = re.compile(
    _COD + r".{0,25}?\b" + _STATE + r"\b"                                         # COD available / COD charges applicable
    + r"|(?i:\b(?:available|accepted)\b).{0,15}?" + _COD                          # available: COD, UPI
    + r"|(?i:\bno\s+)" + _COD + r"(?i:\s+(?:charges?|fees?))")                    # no COD charges
QUESTION_RX = re.compile(r"\?|(?i:\bcheck\s+(?:if|whether)\b|\bto\s+check\b|\bfind\s+out\b)")
# a condition on the order ('COD not available above ₹5000', 'COD on orders over ₹499'): neither a yes nor a no
CONDITION_RX = re.compile(r"(?i:\b(?:above|over|below|under|more\s+than|less\s+than|up\s*to|min(?:imum)?)\b)\s*(?i:rs\.?|₹|inr)?\s*\d")
PHRASE_SPLIT_RX = re.compile(r"[.;|•&\n]|,\s")


def cod_says(text: str) -> str | None:
    """'yes' (COD offered), 'no' (not offered / prepaid only), 'mixed' (both said) or None (not mentioned). Pure."""
    yes = no = False
    for ph in PHRASE_SPLIT_RX.split(text or ""):
        if not ph.strip() or QUESTION_RX.search(ph) or CONDITION_RX.search(ph):
            continue
        if COD_NO_RX.search(ph) or COD_ALONE_NO_RX.fullmatch(ph):
            no = True
        elif COD_YES_RX.search(ph):
            yes = True
    return "mixed" if yes and no else "no" if no else "yes" if yes else None


COD_WORDS = {"yes": "says COD is available", "no": "says COD is not available",
             "mixed": "says both that COD is and isn't available", None: "doesn't mention COD"}

# the product's buy area: the add-to-cart form and the few blocks around it (price, badges, delivery info), never the
# whole page (footers, FAQs and announcement bars talk about COD in general)
BUY_AREA_JS = r"""() => { const f = document.querySelector('form[action*="/cart/add"]'); if (!f) return '';
  let el = f;
  for (let i = 0; i < 4 && el.parentElement && el.parentElement !== document.body; i++) {
    if (((el.parentElement.innerText) || '').length > 4000) break;
    el = el.parentElement; }
  return ((el.innerText) || '').slice(0, 4000); }"""


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
    """The widget's box / button / holder as a live element handle. App-rendered widgets (shop078, shop040,
    shop017, 14-15 Oct) re-render themselves and drop any marker Radar set: when the remembered node has left
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
    ...': shop017, 15 Oct) the wait stretches to busy_secs: slow is a warning with the time, not 'no answer'."""
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


STALE_RX = re.compile(r"not attached to the DOM|detached|Execution context was destroyed|Element is not attached|"
                      r"no longer on the page|Target closed|frame was detached", re.I)


def _on(ctx: Ctx, kind: str, part: str, act, tries: int = 4):
    """Do `act(element)` on the widget's box / button, finding it again when the widget redrew itself between finding
    and acting (23 Oct: 'ElementHandle.press: Element is not attached to the DOM' on a widget that redraws every 250 ms,
    1 run in 12 on a busy machine). Other errors are raised at once."""
    for i in range(tries):
        try:
            return act(_el(ctx, kind, part))
        except Exception as e:  # noqa: BLE001
            if i == tries - 1 or not STALE_RX.search(str(e)):
                raise
            ctx.sess.page.wait_for_timeout(100)


def _type_into(ctx: Ctx, kind: str, text: str) -> None:
    """Scroll the box to the middle of the screen (a sticky buy bar or header can cover it: shop078, shop040,
    shop017 on the first real run, 14 Oct), close any popup, then fill it like a shopper."""
    _dismiss(ctx)
    _on(ctx, kind, "box", lambda b: b.evaluate("e => e.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'})"))
    try:
        _on(ctx, kind, "box", lambda b: b.click(timeout=4000))
    except Exception:  # noqa: BLE001  covered by a sticky bar: focus it directly, typing still goes through the page
        _on(ctx, kind, "box", lambda b: b.focus())
    _on(ctx, kind, "box", lambda b: b.fill(text))
    # an app-rendered widget that redraws itself can drop the text typed into the node it just replaced: type again
    for _ in range(3):
        try:
            if _on(ctx, kind, "box", lambda b: b.input_value(timeout=2000)) == text:
                break
            _on(ctx, kind, "box", lambda b: b.fill(text))
        except Exception:  # noqa: BLE001  redrawn mid-read: read the new node
            pass


def _press(ctx: Ctx, kind: str) -> str:
    """Click the feature's own button like a shopper (scrolled to the middle, popups closed), or press Enter in the
    box when it has none."""
    store = FINDERS[kind][0]
    has_btn = ctx.sess.page.evaluate(f"() => !!((window.{store} || {{}}).btn)")
    if has_btn:
        try:
            _on(ctx, kind, "btn", lambda b: _click(ctx, b))
            return "pressed its button"
        except Exception as e:  # noqa: BLE001
            # still covered after scrolling (a page that ends right under a sticky bar): fire the button's own click
            # handler, then Enter in the box as well, the two things a shopper's tap / keyboard would do
            _on(ctx, kind, "btn", lambda b: b.dispatch_event("click"))
            _on(ctx, kind, "box", lambda b: b.press("Enter"))
            m = re.search(r"<[^>]{0,120}>[^\n]{0,40}intercepts pointer events", str(e))
            why = m.group(0)[:160] if m else str(e).splitlines()[0][:60]
            return f"its button is covered ({why}): clicked it through the cover and pressed Enter"
    _on(ctx, kind, "box", lambda b: b.press("Enter"))
    return "pressed Enter in the box"


def pincode_check(ctx: Ctx, url: str, probe: str = PROBE_PINCODE, strict: bool = False):
    """A delivery-pincode checker on the product page must answer for a real pincode (deliverable or not: both are
    answers). No checker on the page = information only. A checker that stays silent after a check = finding."""
    soft = not strict
    ctx.steps.run("product_loads", lambda: (_load(ctx, url), _dismiss(ctx))[0])
    # Cash on Delivery (22 Oct): what the buy area promises, read before anything is typed
    page_cod = cod_says(ctx.sess.evaluate(BUY_AREA_JS) or "")
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
# it. Radar reads the number the button opens WITHOUT opening WhatsApp or any other site:
#   - a link is read from its address (never clicked); a link through a URL shortener (wa.link, bit.ly ...) is not
#     judged; a link to the store's own page is asked for its redirect only (no redirect followed);
#   - a button with no address is tapped once while every way out is caught: window.open is replaced, any page load
#     (this tab or a new one) is answered on the spot with an empty reply (HTTP 204, the page stays), requests to
#     WhatsApp are aborted, and app-scheme jumps (whatsapp://, intent://) are read from the browser's navigation event.
# Nothing is sent. A Radar problem while tapping is NOT JUDGED (information), never a finding.
WA_HOST_RX = re.compile(r"^(?:https?://)?(?:wa\.me|(?:api|web|chat)\.whatsapp\.com|(?:www\.)?whatsapp\.com)(?:/|$)|^whatsapp:"
                        r"|^intent://[^#]*#Intent;.*(?:scheme=whatsapp|package=com\.whatsapp)", re.I)
SHORTENER_RX = re.compile(r"^(?:https?://)?(?:www\.)?(?:wa\.link|wa\.click|wa\.ws|bit\.ly|tinyurl\.com|t\.ly|rb\.gy|cutt\.ly|is\.gd|"
                          r"linktr\.ee|s\.id|shorturl\.at)/", re.I)
INVISIBLE_RX = re.compile("[​-‏‪-‮⁠-⁩﻿]")
WA_ROUTES = ("**://wa.me/**", "**://*.whatsapp.com/**", "**://whatsapp.com/**")

# visible WhatsApp links and buttons, marked data-radar-wa=<i>:
#   link     an <a> whose address is WhatsApp's            -> read
#   short    an <a> named WhatsApp through a URL shortener -> not judged
#   redirect an <a> named WhatsApp to the store's own page -> its redirect is asked for (not followed)
#   widget   a WhatsApp-named button with no address       -> tapped once, every way out caught
WA_FIND_JS = r"""() => {
  const HREF = /^(?:https?:\/\/)?(?:wa\.me|(?:api|web|chat)\.whatsapp\.com|(?:www\.)?whatsapp\.com)(?:\/|$)|^whatsapp:/i;
  const SHORT = /^(?:https?:\/\/)?(?:www\.)?(?:wa\.link|wa\.click|wa\.ws|bit\.ly|tinyurl\.com|t\.ly|rb\.gy|cutt\.ly|is\.gd|linktr\.ee|s\.id|shorturl\.at)\//i;
  const NAME = /whats\s*-?app/i;
  const seen = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width >= 8 && r.height >= 8 && s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity !== 0; };
  const fixed = (e) => { for (let x = e; x && x !== document.body; x = x.parentElement) { const p = getComputedStyle(x).position;
    if (p === 'fixed' || p === 'sticky') return true; } return false; };
  const label = (e) => (e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || (e.querySelector('img') || {}).alt || '').trim().slice(0, 60);
  const named = (e) => NAME.test([e.id, typeof e.className === 'string' ? e.className : (e.className && e.className.baseVal) || '',
    e.getAttribute('aria-label') || '', e.getAttribute('title') || '', e.getAttribute('alt') || ''].join(' '));
  const realHref = (a) => { const h = (a.getAttribute('href') || '').trim(); return h && h !== '#' && !/^javascript:/i.test(h) ? h : ''; };
  document.querySelectorAll('[data-radar-wa]').forEach(e => e.removeAttribute('data-radar-wa'));
  const out = [];
  const add = (el, kind, href) => { el.setAttribute('data-radar-wa', String(out.length));
    out.push({kind, href, label: label(el), fixed: fixed(el)}); };
  for (const a of document.querySelectorAll('a[href]')) {
    const h = realHref(a);
    if (!h || !seen(a)) continue;
    if (HREF.test(h)) { if (!/whatsapp\.com\/channel\//i.test(h)) add(a, 'link', a.href || h); continue; }
    const isNamed = named(a) || [...a.querySelectorAll('*')].some(named) || NAME.test(a.innerText || '');
    if (!isNamed) continue;
    if (SHORT.test(h)) add(a, 'short', a.href || h);
    else if (a.host === location.host) add(a, 'redirect', a.href);
  }
  for (const e of document.querySelectorAll('button, [role=button], div, span, img, svg, a')) {
    if (out.length >= 8) break;
    if (!named(e) || /share/i.test(String(e.className && (e.className.baseVal ?? e.className)) + ' ' + (e.getAttribute('aria-label') || ''))
        || !seen(e) || e.closest('[data-radar-wa]') || e.querySelector('[data-radar-wa]')) continue;
    const r = e.getBoundingClientRect();
    if (r.width > 500 || r.height > 300) continue;            // a section that only mentions WhatsApp, not a button
    const target = e.closest('button, [role=button], a') || e;
    if (target.hasAttribute('data-radar-wa')) continue;
    if (target.tagName === 'A' && realHref(target)) continue; // an address of its own: never tapped (read above or left)
    add(target, 'widget', '');
  }
  return out;
}"""


def _clean(u: str) -> str:
    return INVISIBLE_RX.sub("", u or "").strip()


def wa_number(url: str) -> tuple[str, str]:
    """(kind, number) for a WhatsApp address. kind: 'chat' (a number to chat with, as written), 'short' (a WhatsApp
    Business short link, wa.me/message/<code> or wa.me/qr/<code>: the number is behind WhatsApp's own redirect),
    'share' (opens WhatsApp to share a text, no number: a product's share button), 'group' (a group invite link,
    chat.whatsapp.com/<code>: a community, not a chat with the store), 'empty' (no number, no text),
    'other' (not a chat address: a channel, a profile). Pure."""
    from urllib.parse import parse_qs, unquote, urlparse
    u = _clean(url)
    m = re.match(r"intent://([^#]*)#Intent;", u, re.I)
    if m and WA_HOST_RX.search(u):
        u = "whatsapp://" + m.group(1)                    # Android's form of whatsapp://send?phone=...
    if not WA_HOST_RX.search(u):
        return "other", ""
    p = urlparse(u if "://" in u or u.lower().startswith("whatsapp:") else "https://" + u)
    q = parse_qs(p.query)
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0].lower() in ("message", "qr") and host in ("wa.me", "api.whatsapp.com", "whatsapp.com", "www.whatsapp.com"):
        return "short", ""
    if parts and parts[0].lower() in ("channel", "catalog") and host != "wa.me":
        return "other", ""
    if host == "chat.whatsapp.com" and parts:          # a group invite (chat.whatsapp.com/<code>), not a chat (23 Oct)
        return "group", ""
    num = ""
    if host == "wa.me":
        if parts and parts[0].lower() == "c" and len(parts) > 1:      # wa.me/c/<number>: the business's catalog
            parts = parts[1:]
        num = _clean(unquote(parts[0])) if parts else ""
        if not re.match(r"[+\d(]", num):                              # wa.me/send?phone=... ('send' is not a number)
            num = ""
    if not num:
        num = _clean(unquote((q.get("phone") or [""])[0]))
    if num:
        return "chat", num
    if (q.get("text") or [""])[0]:
        return "share", ""
    if host == "wa.me" or "send" in p.path or u.lower().startswith("whatsapp:"):
        return "empty", ""
    return "other", ""


def wa_judge(num: str) -> tuple[str, str]:
    """('ok' | 'bad' | 'warn', what the number is, in words) for the number a WhatsApp chat button opens. WhatsApp wants
    the full international number, digits only, no leading 0 (wa.me/919876543210). 'bad' only on positive evidence
    that nobody can be reached. Pure, unit-tested."""
    t = _clean(num)
    if not t:
        return "warn", "no phone number in the link (the shopper lands on an empty chat picker)"
    m = re.match(r"[\s\-().+]*(\d[\d\s\-().]*)", t)
    if not m:
        return "bad", f"{t[:30]!r} is not a phone number"
    digits, rest = re.sub(r"\D", "", m.group(1)), t[m.end():].strip()
    if digits.startswith("0"):
        return "bad", f"{digits} starts with 0 (WhatsApp needs the country code instead, 91 for India)"
    if not 8 <= len(digits) <= 15:
        return "bad", f"{digits} has {len(digits)} digits, not a complete phone number" + (f" (then {rest[:20]!r})" if rest else "")
    if rest:
        return "warn", f"+{digits} with extra characters after it ({rest[:20]!r}): WhatsApp may not open the chat"
    if len(digits) == 10 and digits[0] in "6789":
        return "warn", f"{digits} may be missing the country code (91{digits} for an Indian number)"
    return "ok", f"+{digits}"


def _same_site_redirect(ctx: Ctx, url: str) -> str | None:
    """Where a link to the store's own page sends the shopper, read from its redirect without following it."""
    try:
        r = ctx.sess.request_get(url, max_redirects=0, timeout=15000)
        return (r.headers or {}).get("location") if 300 <= r.status < 400 else None
    except Exception:  # noqa: BLE001
        return None


def whatsapp_button(ctx: Ctx, home: str, product: str | None = None, strict: bool = False):
    """A WhatsApp chat button on the store must open a chat with a real phone number. No button on the homepage (nor on
    the product page) = information only. A number that cannot be a phone number = failure (positive evidence: every
    shopper who taps it reaches no one); a likely missing country code, no number, extra characters, or a button that
    does nothing when tapped = warning (`strict` makes those failures). Anything Radar cannot read = not judged."""
    ctx.steps.run("page_loads", lambda: (_load(ctx, home), _dismiss(ctx))[0])
    found, where = ctx.sess.evaluate(WA_FIND_JS) or [], "homepage"
    if not found and product:
        ctx.steps.run("product_page_loads", lambda: (_load(ctx, product), _dismiss(ctx))[0])
        found, where = ctx.sess.evaluate(WA_FIND_JS) or [], "product page"
    if not found:
        ctx.steps.info("whatsapp_not_judged", "no WhatsApp chat button on the homepage or a product page (information only)")
        return
    chats, notes = [], []
    for f in found:
        href = f["href"]
        if f["kind"] == "redirect":
            to = _same_site_redirect(ctx, href)
            if not (to and WA_HOST_RX.search(_clean(to))):
                notes.append(f"{f['label'] or 'a WhatsApp link'!r} goes to {href[:70]} (not straight to WhatsApp): not judged")
                continue
            href = to
        if f["kind"] == "short":
            notes.append(f"{f['label'] or 'a WhatsApp link'!r} goes through a link shortener ({href[:60]}): not judged")
            continue
        if f["kind"] in ("link", "redirect"):
            kind, num = wa_number(href)
            if kind in ("chat", "empty"):
                chats.append(({**f, "href": href}, kind, num))
            elif kind == "short":
                notes.append(f"WhatsApp Business short link ({href[:70]}): the number is not in the address, not judged")
            elif kind == "share":
                notes.append(f"WhatsApp share link ({href[:60]}): shares a page, not a chat button")
            elif kind == "group":
                notes.append(f"WhatsApp group invite ({href[:60]}): a community, not a chat with the store; not judged")
            else:
                notes.append(f"WhatsApp link Radar doesn't read a number from ({href[:60]}): not judged")
    for n in dict.fromkeys(notes):
        ctx.steps.info("whatsapp_note", n)
    if not chats:
        widgets = sorted((i for i, f in enumerate(found) if f["kind"] == "widget"), key=lambda i: not found[i]["fixed"])
        if not widgets:
            ctx.steps.info("whatsapp_not_judged", "no WhatsApp chat button whose number Radar can read (information only)")
            return
        i = widgets[0]
        who = f"{found[i]['label'] or 'the WhatsApp button'!r} on the {where}"
        status, got = _tap_whatsapp_widget(ctx, i, found)
        if status == "error":
            ctx.steps.info("whatsapp_not_judged", f"Radar could not tap {who} ({got}): not judged")
            return
        if status == "nothing":
            def dead():
                ctx.expect(f"WhatsApp button {who} opens a chat", "a WhatsApp link when tapped",
                           "nothing within 5 s of tapping it", False)
            ctx.steps.run("whatsapp_button_responds", dead, soft=not strict)
            return
        kind, num = wa_number(got)
        if kind not in ("chat", "empty"):
            ctx.steps.info("whatsapp_not_judged", f"{who} opens {got[:80]}: no number in the address, not judged")
            return
        chats = [({"href": got, "label": found[i]["label"], "kind": "widget"}, kind, num)]
    judged: dict[str, tuple] = {}
    for f, kind, num in chats:
        key = re.sub(r"\D", "", num) or "(none)"
        if key not in judged:
            judged[key] = (*wa_judge(num if kind == "chat" else ""), f)
    for n, (key, (verdict, what, f)) in enumerate(list(judged.items())[:3]):
        name = f"whatsapp_number{'' if n == 0 else '_' + str(n + 1)}"
        who = f"{f.get('label') or 'WhatsApp button'!r} on the {where}"

        def check(verdict=verdict, what=what, who=who, href=f.get("href", "")):
            ctx.expect(f"WhatsApp button {who} opens a chat with a real number", "a full international number like 919876543210",
                       f"{what} ({href[:90]})", verdict == "ok")
            return f"{who}: opens a chat with {what}"
        ctx.steps.run(name, check, soft=(verdict == "warn" and not strict))


TAP_JS = """() => { window.__radarWaOpen = [];
  window.open = function (u) { try { window.__radarWaOpen.push(String(u || '')); } catch (e) {} return null; };
  try { navigation.addEventListener('navigate', e => { try { window.__radarWaOpen.push(e.destination.url); } catch (x) {} }); }
  catch (e) {} }"""


def _tap_whatsapp_widget(ctx: Ctx, i: int, found: list) -> tuple[str, str | None]:
    """Tap a WhatsApp button that has no address of its own and catch where it tries to go without letting it get
    anywhere (see the section's header). Returns ('found', address) | ('nothing', None) | ('error', what went wrong);
    never raises: a Radar problem here is not a store finding (review, 22 Oct)."""
    page, caught, popups = ctx.sess.page, [], []

    def stop_wa(route):                   # any request to WhatsApp itself (a script, a frame)
        caught.append(route.request.url)
        try:
            route.abort()
        except Exception:  # noqa: BLE001
            pass

    def stop_loads(route):                # any page load in a tab: answered here with 'no content', the page stays
        req = route.request
        try:
            nav = req.is_navigation_request() and req.frame.parent_frame is None
        except Exception:  # noqa: BLE001
            nav = False
        try:
            if nav:
                caught.append(req.url)
                route.fulfill(status=204, headers={"x-radar-caught": "1"}, body="")
            else:
                route.fallback()          # on to the context's own routes (Web Bot Auth signing)
        except Exception:  # noqa: BLE001
            pass

    def on_page(p):                       # a new tab (Playwright needs a plain function here)
        popups.append(p)
    try:
        before = {f["href"] for f in (ctx.sess.evaluate(WA_FIND_JS) or []) if f["kind"] == "link"}   # same marks again
        for pat in WA_ROUTES:
            page.context.route(pat, stop_wa)
        page.context.route("**/*", stop_loads)       # added last = asked first
        page.context.on("page", on_page)
        page.evaluate(TAP_JS)
        el = page.locator(f'[data-radar-wa="{i}"]').first
        try:
            _click(ctx, el)
        except Exception:  # noqa: BLE001  covered by a popup we could not close: its own click handler
            el.dispatch_event("click", timeout=5000)
        for _ in range(10):
            page.wait_for_timeout(500)
            try:
                opened = list(page.evaluate("() => window.__radarWaOpen || []") or [])
                panel = [f["href"] for f in (ctx.sess.evaluate(WA_FIND_JS) or [])
                         if f["kind"] == "link" and f["href"] not in before]
            except Exception:  # noqa: BLE001  the page was busy: look again
                opened, panel = [], []
            for u in opened + caught + [p.url for p in popups] + panel:
                u = _clean(u)
                if not u or u == "about:blank":
                    continue
                if WA_HOST_RX.search(u):
                    return "found", u
                if same_site_url(u, page.url) and u.split("#")[0] != page.url.split("#")[0]:
                    to = _same_site_redirect(ctx, u)          # the store's own redirect page (/pages/whatsapp)
                    if to and WA_HOST_RX.search(_clean(to)):
                        return "found", to
        return "nothing", None
    except Exception as e:  # noqa: BLE001
        return "error", str(e).splitlines()[0][:120]
    finally:
        for fn in (lambda: page.context.remove_listener("page", on_page),
                   lambda: page.context.unroute("**/*", stop_loads),
                   *[(lambda pat=pat: page.context.unroute(pat, stop_wa)) for pat in WA_ROUTES],
                   *[(lambda p=p: p.close()) for p in popups]):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass


REGISTRY["pincode_check"] = pincode_check
REGISTRY["discount_code"] = discount_code
REGISTRY["whatsapp_button"] = whatsapp_button
