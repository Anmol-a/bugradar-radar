"""Self-healing locators. Checks ask for an INTENT ("add_to_cart"), not a CSS selector.

Resolution ladder (first hit wins, each rung is cheaper than the next):
  1. cache      selector that worked last time for this site+intent (SQLite locator_cache)
  2. hints      per-site selectors from sites/<site_id>.yml, then built-in Shopify defaults
  3. heuristic  score every interactive element on the page against the intent
                (text, name, class, form action); accept if score >= threshold
  4. LLM        send a compact numbered list of candidates; model returns an index + confidence
A rung 3/4 success is written back to the cache and logged as a healing event, so the
report shows exactly what was healed and how. The OUTCOME check that follows (e.g. cart.js
contains the product) is the real oracle: healing finds an element, it never decides pass.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from playwright.sync_api import Locator, Page

from radar.healing.llm import LLMClient

INTENTS = {
    "add_to_cart": {
        "describe": "the primary button that adds the current product to the shopping cart",
        "defaults": ["form[action*='/cart/add'] button[type=submit]", "button[name='add']",
                     "[data-testid*='add-to-cart']", "button:has-text('Add to cart')"],
        "positive": r"add to (cart|bag|basket)|add to my bag|^add$|^buy$",
        "attr_positive": r"add[-_]?to[-_]?cart|addtocart|\batc\b|product-form__submit|\bname=add\b|/cart/add",
        "negative": r"notify|sold out|wishlist|wish list|quick ?view|remove|subscribe|compare",
        # quick-add buttons on product CARDS add a different product: never the main buy button
        "attr_negative": r"quick[-_ ]?add|quickadd|in_card=true",
    },
    "checkout_button": {
        "describe": "the button or link on the cart page that proceeds to checkout",
        "defaults": ["button[name='checkout']", "a[href*='/checkout']", "button:has-text('Checkout')",
                     "button:has-text('Check out')", "button:has-text('Place order')", "button:has-text('Buy now')"],
        "positive": r"check ?out|proceed to (checkout|buy|pay|payment)|place order|^buy now$|secure checkout",
        "attr_positive": r"checkout|gokwik|shopflo|onclick=[^ ]*checkout",
        "negative": r"continue shopping|remove|update|apply|view cart",
        "attr_negative": r"$^",
    },
}

QUICK_SEL = ("[id*='quick-add' i], [class*='quick-add' i], [class*='quickadd' i], [class*='quick_add' i], "
             "[class*='card' i] [class*='quick' i], .swiper-slide, [class*='upsell' i], [class*='recommend' i]")

# Is this element a buy control on ANOTHER product's card? Decided by where the nearest product links
# point, never by class names alone: soulflower.in's MAIN buy button sits in a "quick-add-container"
# (bench 3), while moxiebeauty.in's quick-add cards link to other products (bench 0). Off product pages
# every quick-add container counts as a card, as before.
OTHER_CARD_FN = r"""(e, quick) => { const c = e.closest(quick); if (!c) return false;
  const m = location.pathname.match(/\/products\/([^/?#]+)/); const here = m ? decodeURIComponent(m[1]) : null;
  if (!here) return true;
  const h = a => { const x = (a.getAttribute('href') || '').match(/\/products\/([^/?#]+)/); return x ? decodeURIComponent(x[1]) : null; };
  for (let a = c, k = 0; a && a !== document.body && k < 6; a = a.parentElement, k++) {
    const hs = [...a.querySelectorAll('a[href*="/products/"]')].map(h).filter(Boolean);
    if (hs.length) return hs.some(x => x !== here);
  }
  return false; }"""

CANDIDATES_JS = r"""(root) => {
  const QUICK = "__QUICK__";
  const otherCard = __OTHER_CARD__;
  const scope = (root && document.querySelector(root)) || document;
  const els = [...scope.querySelectorAll(
    'button, input[type=submit], input[type=button], a[href], [role=button]')];
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const uniq = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch(e) { return false; } };
  const q = v => '"' + String(v).replace(/"/g, '\\"') + '"';
  const cssPath = el => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body) {
      let p = el.tagName.toLowerCase();
      if (el.id && uniq('#' + CSS.escape(el.id))) { parts.unshift('#' + CSS.escape(el.id)); break; }
      const sib = [...el.parentNode.children].filter(c => c.tagName === el.tagName);
      if (sib.length > 1) p += ':nth-of-type(' + (sib.indexOf(el) + 1) + ')';
      parts.unshift(p); el = el.parentElement;
    }
    return parts.join(' > ');
  };
  // Prefer selectors that survive layout changes; positional path is the last resort.
  const stable = el => {
    const tag = el.tagName.toLowerCase();
    if (el.id && uniq('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
    for (const a of ['data-testid', 'name', 'aria-label', 'data-action']) {
      const v = el.getAttribute(a); if (v && uniq(`${tag}[${a}=${q(v)}]`)) return `${tag}[${a}=${q(v)}]`;
    }
    const cls = [...el.classList].filter(c => /^[a-zA-Z_-][\w-]*$/.test(c)).map(c => '.' + c).join('');
    if (cls && uniq(tag + cls)) return tag + cls;
    const txt = (el.innerText || '').trim();
    if (txt && txt.length < 40) {
      const same = [...document.querySelectorAll(tag)].filter(x => (x.innerText || '').trim() === txt);
      if (same.length === 1) return `${tag}:text-is(${q(txt)})`;
    }
    return cssPath(el);
  };
  return els.filter(vis).slice(0, 300).map((e, i) => {
    const form = e.closest('form');
    return {
      i, tag: e.tagName.toLowerCase(),
      text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 80),
      attrs: ['name','class','id','type','href','data-testid','aria-label','onclick']
        .map(a => e.getAttribute(a) ? a + '=' + e.getAttribute(a).slice(0, 80) : '').filter(Boolean).join(' ')
        + (form ? ' form_action=' + (form.getAttribute('action') || '') : '')
        + (otherCard(e, QUICK) ? ' in_card=true' : ''),
      disabled: !!e.disabled,
      selector: stable(e),
    };
  });
}""".replace("__QUICK__", QUICK_SEL).replace("__OTHER_CARD__", OTHER_CARD_FN)


def score(intent: str, cand: dict) -> float:
    """0..1 heuristic match of one candidate element to an intent. Pure function, unit-tested."""
    spec = INTENTS[intent]
    text = (cand.get("text") or "").lower()
    attrs = (cand.get("attrs") or "").lower()
    if re.search(spec["negative"], text) or re.search(spec.get("attr_negative", r"$^"), attrs):
        return 0.0
    s = 0.0
    if re.search(spec["positive"], text):
        s += 0.6
    if re.search(spec["attr_positive"], attrs):
        s += 0.35
    if cand.get("tag") in ("button", "input"):
        s += 0.05
    if cand.get("disabled"):
        s -= 0.3
    return max(0.0, min(1.0, s))


@dataclass
class Found:
    locator: Locator
    selector: str
    method: str          # cache | hint | heuristic | llm
    healed: dict | None  # set when rung 3/4 was needed


class LocatorNotFound(Exception):
    def __init__(self, intent: str, tried: list[str], note: str = ""):
        self.intent, self.tried = intent, tried
        super().__init__(f"could not find '{intent}' (tried {len(tried)} selectors, heuristic"
                         f" and LLM){': ' + note if note else ''}")


class Healer:
    HEURISTIC_MIN = 0.6
    LLM_MIN_CONF = 0.6

    def __init__(self, site_id: str, run_id: str, storage, llm: LLMClient, site_hints: dict | None = None,
                 device: str = "desktop"):
        self.site_id, self.run_id, self.storage, self.llm = site_id, run_id, storage, llm
        # Remembered selectors are per device: a desktop-only control cached for mobile (or the reverse) would
        # fail, get dropped, be re-found and overwrite the other device's entry on every run (7 Oct).
        self.cache_id = site_id if device == "desktop" else f"{site_id}@{device}"
        self.hints = site_hints or {}
        self.events: list[dict] = []

    def _visible(self, page: Page, selector: str, timeout: int, intent: str = "", root: str | None = None):
        """First VISIBLE match that is not excluded. (Using .first was the Moxie bug: the first
        add-to-cart form in the DOM was a quick-add button on a product card.)"""
        base = page.locator(root) if root else page
        deadline = time.time() + timeout / 1000
        while True:
            try:
                loc = base.locator(selector)
                for i in range(min(loc.count(), 25)):
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    if intent == "add_to_cart" and el.evaluate(f"(e, q) => ({OTHER_CARD_FN})(e, q)", QUICK_SEL):
                        continue
                    return el
            except Exception:  # noqa: BLE001  invalid selector for this engine, detached frame
                return None
            if time.time() > deadline:
                return None
            page.wait_for_timeout(200)

    def find(self, page: Page, intent: str, timeout_ms: int = 8000, root: str | None = None) -> Found:
        """root: optional CSS selector to search inside (e.g. an open cart drawer)."""
        tried: list[str] = []
        key = intent + ("@scoped" if root else "")
        cached = self.storage.cached_locator(self.cache_id, key)
        if cached:
            tried.append(cached)
            loc = self._visible(page, cached, timeout_ms // 2, intent, root)
            if loc:
                return Found(loc, cached, "cache", None)
            self.storage.drop_locator(self.cache_id, key)   # stale: forget it

        hints = list(self.hints.get(intent, [])) + INTENTS[intent]["defaults"]
        per = max(timeout_ms // max(len(hints), 1), 700)
        for i, sel in enumerate(hints):
            if sel in tried:
                continue
            tried.append(sel)
            loc = self._visible(page, sel, per if i == 0 else 700, intent, root)
            if loc:
                self.storage.cache_locator(self.cache_id, key, sel, "hint")
                return Found(loc, sel, "hint", None)

        cands = page.evaluate(CANDIDATES_JS, root)
        ranked = sorted(((score(intent, c), c) for c in cands), key=lambda t: -t[0])
        if ranked and ranked[0][0] >= self.HEURISTIC_MIN:
            sc, c = ranked[0]
            return self._healed(page, intent, c["selector"], "heuristic", tried,
                                f"score {sc:.2f}, text {c['text']!r}", key)

        if self.llm.enabled and cands:
            shortlist = [c for sc, c in ranked if not re.search(INTENTS[intent].get("attr_negative", r"$^"),
                                                                 (c.get("attrs") or "").lower())][:40]
            listing = "\n".join(f"[{c['i']}] <{c['tag']}> text={c['text']!r} {c['attrs']}" for c in shortlist)
            ans = self.llm.complete_json(
                "You pick UI elements for an automated website monitor. Reply with JSON only: "
                '{"index": <number or null>, "confidence": <0..1>, "reason": "<short>"}',
                f"Page URL: {page.url}\nFind: {INTENTS[intent]['describe']}.\n"
                f"Candidates (visible interactive elements):\n{listing}\n"
                "If none of them is clearly it, return index null.")
            if ans and ans.get("index") is not None and float(ans.get("confidence", 0)) >= self.LLM_MIN_CONF:
                match = next((c for c in cands if c["i"] == int(ans["index"])), None)
                if match:
                    return self._healed(page, intent, match["selector"], "llm", tried,
                                        f"confidence {ans.get('confidence')}, {ans.get('reason', '')[:80]}", key)
        best = f"best heuristic score {ranked[0][0]:.2f}" if ranked else "no interactive elements"
        raise LocatorNotFound(intent, tried, best + ("" if self.llm.enabled else "; LLM healing disabled"))

    def _healed(self, page, intent, selector, method, tried, why, key) -> Found:
        loc = self._visible(page, selector, 2000, intent)
        if not loc:
            raise LocatorNotFound(intent, tried + [selector], f"{method} pick was not visible")
        self.storage.cache_locator(self.cache_id, key, selector, method)
        self.storage.log_healing(self.site_id, self.run_id, intent, tried, selector, method)
        ev = {"intent": intent, "old": tried, "new": selector, "method": method, "why": why}
        self.events.append(ev)
        return Found(loc, selector, method, ev)
