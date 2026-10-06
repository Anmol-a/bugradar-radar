"""Overlay handling: newsletter popups, cookie banners, country/currency pickers, age gates.

Real stores show these on a first visit, often after a few seconds, and they block clicks.
Rules (privacy-preserving, honest):
  cookie banner  -> click Decline / Reject / Only necessary; if none, Close. NEVER Accept.
  newsletter etc -> click Close / No thanks / ×. Never fills or submits the form.
  location popup -> close it (stay on the store as reached).
  age gate       -> NOT confirmed on the shopper's behalf: the test is reported as blocked.
Every dismissal is recorded and shown in the report.
"""
from __future__ import annotations

FIND_JS = r"""() => {
  // Search the page AND every open shadow root: app popups increasingly render inside a web component's
  // shadow DOM (soulflower.in's YourLio popup inside #chat-widget, bench 9; document.querySelectorAll
  // cannot see into it, so the popup blocked the product click).
  const hosts = []; const findHosts = r => r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) { hosts.push(e.shadowRoot); findHosts(e.shadowRoot); } });
  findHosts(document);
  const all = sel => { const out = [...document.querySelectorAll(sel)]; hosts.forEach(h => out.push(...h.querySelectorAll(sel))); return out; };
  const within = (root, sel) => { const out = [...root.querySelectorAll(sel)]; root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) out.push(...e.shadowRoot.querySelectorAll(sel)); }); return out; };
  all('[data-radar-target="dismiss"]').forEach(e => e.removeAttribute('data-radar-target'));
  const vw = innerWidth, vh = innerHeight;
  const shown = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || 1) > 0.05; };
  const fixedish = e => { for (let n = e; n && n !== document.body; n = n.parentElement) {
      const p = getComputedStyle(n).position; if (p === 'fixed' || p === 'sticky') return true; } return false; };
  const isCart = e => !!e.closest('cart-drawer, cart-notification, [id*="cart-drawer" i], [class*="cart-drawer" i], ' +
    '[id*="CartDrawer"], [class*="cart" i][class*="drawer" i], [data-radar-target="drawer"]');
  const sel = '[role="dialog"], [aria-modal="true"], dialog[open], [class*="modal" i], [class*="popup" i], [id*="popup" i], ' +
    '[class*="klaviyo" i], [class*="newsletter" i], [class*="cookie" i], [id*="cookie" i], [class*="consent" i], ' +
    '[id*="consent" i], [class*="privy" i], [class*="omnisend" i], [class*="geolocation" i], [class*="country-selector" i], ' +
    '[class*="age-verif" i], [class*="ageverif" i], [class*="agegate" i], [class*="age-gate" i], [id*="age" i][class*="gate" i]';
  const named = all(sel).filter(e => shown(e) && !isCart(e) && !e.closest('header, nav'))
    .filter(e => { const r = e.getBoundingClientRect();
      return e.getAttribute('aria-modal') === 'true' || e.tagName === 'DIALOG' ||
             (fixedish(e) && r.width * r.height >= 0.08 * vw * vh); });
  // Popups with no telling names (utility classes like "fixed inset-0 z-50", soulflower.in's YourLio
  // popup, bench 3): any FIXED layer covering half the screen that takes clicks and holds a small box
  // with a button in it. Cart drawers, the site header and click-through layers are excluded.
  const generic = [...document.querySelectorAll('body *'), ...hosts.flatMap(h => [...h.querySelectorAll('*')])].filter(e => {
    const s = getComputedStyle(e); if (s.position !== 'fixed' || !shown(e) || s.pointerEvents === 'none') return false;
    const r = e.getBoundingClientRect(); if (r.width * r.height < 0.5 * vw * vh || (parseInt(s.zIndex) || 0) < 1) return false;
    if (isCart(e) || e.closest('header, nav, [id*="header" i]')) return false;
    // a popup is small inside: not the page's own content (main, the buy form, a menu full of links)
    if (e.querySelector('main, form[action*="/cart/add"], [role="navigation"]') || e.querySelectorAll('a[href]').length > 12) return false;
    return !!e.querySelector('button, [role="button"]');
  }).slice(0, 4);
  const roots = [...new Set([...named, ...generic])];
  const text = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
  for (const root of roots) {
    const t = text(root).slice(0, 600).toLowerCase();
    if (/\b(18|21)\s*\+|of legal (drinking )?age|age verification|verify your age|are you (over|above) (18|21)|date of birth/.test(t))
      return {kind: 'age_gate', text: t.slice(0, 80)};
    const kind = /cookie|consent|gdpr|tracking technologies/.test(t) ? 'cookie' :
                 /country|region|currency|ship to|location/.test(t) ? 'location' : 'popup';
    const clickables = within(root, 'button, a, [role="button"], [aria-label], .close, [class*="close" i]').filter(shown);
    const pick = (re, attr) => clickables.find(c => re.test(attr ? ((c.getAttribute('aria-label') || '') + ' ' + (c.className || '')) : text(c)));
    let btn = null;
    if (kind === 'cookie') btn = pick(/^(decline|reject|deny|reject all|decline all|only necessary|necessary only|use necessary cookies only)$/i);
    btn = btn || pick(/close|dismiss/i, true) || pick(/^(×|✕|✖|x|close|no thanks|no,? thanks|not now|maybe later|skip|continue browsing|stay here|dismiss)$/i);
    // icon-only close: a text-less button whose icon is an X (svg class x/close/cross, or the usual X path),
    // or a small text-less button in the top-right corner of the popup box
    btn = btn || clickables.find(c => !text(c) && c.matches('button, [role="button"]') && (
      /(^|[\s-])(x|close|cross|times)([\s-]|$)/i.test((c.querySelector('svg') || {}).className?.baseVal || '') ||
      [...c.querySelectorAll('path')].some(p => /^M\s?18 6\s?6 18|M6 6l12 12|M6 18L18 6/i.test(p.getAttribute('d') || ''))));
    btn = btn || clickables.find(c => { if (text(c) || !c.matches('button, [role="button"]')) return false;
      const b = c.getBoundingClientRect(), box = (c.closest('[class*="bg-white" i], [class*="modal" i], [class*="popup" i], [class*="content" i]') || root).getBoundingClientRect();
      return b.width <= 48 && b.height <= 48 && b.left > box.left + box.width * 0.6 && b.top < box.top + box.height * 0.3; });
    if (!btn || /accept|agree|allow all|subscribe|sign ?up|submit|yes/i.test(text(btn))) continue;   // never consent, never sign up
    btn.setAttribute('data-radar-target', 'dismiss');
    return {kind, text: t.slice(0, 60), button: text(btn).slice(0, 30) || (btn.getAttribute('aria-label') || 'close')};
  }
  return null;
}"""


class AgeGate(Exception):
    pass


IFRAME_POPUPS_JS = r"""() => {
  const vw = innerWidth, vh = innerHeight; const out = [];
  document.querySelectorAll('iframe').forEach((f, i) => {
    const r = f.getBoundingClientRect(); const s = getComputedStyle(f);
    const name = ((f.id || '') + ' ' + (f.className || '') + ' ' + (f.title || '') + ' ' + (f.name || '')).toLowerCase();
    let fixed = false; for (let n = f; n && n !== document.body; n = n.parentElement) { if (getComputedStyle(n).position === 'fixed') { fixed = true; break; } }
    if (s.display !== 'none' && s.visibility !== 'hidden' && r.width * r.height >= 0.08 * vw * vh && fixed
        && /popup|modal|klaviyo|newsletter|privy|wisepops|optin|spin|offer|signup|subscribe/.test(name)) {
      f.setAttribute('data-radar-iframe', String(i)); out.push(String(i)); }
  });
  return out;
}"""

IFRAME_CLOSE = ("[aria-label*='close' i], button[class*='close' i], [class*='close' i], button:has-text('×'), "
                "button:has-text('✕'), button:has-text('No thanks'), button:has-text('Not now'), button:has-text('Maybe later')")


def dismiss_overlays(page, rounds: int = 3) -> list[dict]:
    """Dismiss whatever overlays are open now. Returns what was dismissed.
    Raises AgeGate if an age-verification gate is showing."""
    done: list[dict] = []
    for _ in range(rounds):
        try:
            found = page.evaluate(FIND_JS)
        except Exception:  # noqa: BLE001  page navigating
            break
        if not found:
            found = _iframe_popup(page)
            if found:
                done.append(found)
                continue
            break
        if found["kind"] == "age_gate":
            raise AgeGate(f"age verification gate shown ('{found['text']}'); Radar does not confirm age on "
                          "a shopper's behalf, so this test cannot continue")
        try:
            page.locator('[data-radar-target="dismiss"]').first.click(timeout=3000)
        except Exception:  # noqa: BLE001  covered by another layer: try Escape once
            page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        done.append(found)
    return done


def _iframe_popup(page) -> dict | None:
    """Marketing popups rendered inside an <iframe> (e.g. srcdoc popups): find the close control
    inside the frame. Escape as a last resort. Never clicks subscribe/accept."""
    try:
        ids = page.evaluate(IFRAME_POPUPS_JS)
    except Exception:  # noqa: BLE001
        return None
    for i in ids:
        sel = f'iframe[data-radar-iframe="{i}"]'
        fl = page.frame_locator(sel)
        try:
            btn = fl.locator(IFRAME_CLOSE).first
            label = (btn.inner_text(timeout=1500) or btn.get_attribute("aria-label") or "close").strip()[:30]
            if any(w in label.lower() for w in ("subscribe", "accept", "agree", "sign up", "yes")):
                raise ValueError("only a consent/subscribe button found")
            btn.click(timeout=3000)
            how = f"clicked {label!r} inside the popup frame"
        except Exception:  # noqa: BLE001
            page.keyboard.press("Escape")
            how = "pressed Escape"
        page.wait_for_timeout(500)
        still = page.locator(sel).count() and page.locator(sel).first.is_visible()
        return {"kind": "popup (iframe)", "text": f"iframe popup {i}", "button": how + ("" if not still else "; still open")}
    return None
