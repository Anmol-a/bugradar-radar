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
  // Only a layer a shopper can SEE counts: inside the viewport and on top at some point of it. Closed side drawers
  // (wishlist / cart / menu slid off-screen, suta.in + soulflower.in, 7-9 Oct) are never popups, and a candidate
  // whose close did not work is marked tried and skipped (v0.18 retried the same hidden drawer ~10 times).
  const deepHit = (x, y) => { let el = document.elementFromPoint(x, y);
    while (el && el.shadowRoot) { const inner = el.shadowRoot.elementFromPoint(x, y); if (!inner || inner === el) break; el = inner; }
    return el; };
  const inside = (root, el) => { for (let n = el; n; n = n.parentNode || n.host) if (n === root) return true; return false; };
  const onScreen = e => { const r = e.getBoundingClientRect();
    const x1 = Math.max(0, r.left), y1 = Math.max(0, r.top), x2 = Math.min(vw, r.right), y2 = Math.min(vh, r.bottom);
    if (x2 - x1 < 8 || y2 - y1 < 8 || (x2 - x1) * (y2 - y1) < 0.3 * r.width * r.height) return false;
    const pts = [[0.5, 0.5], [0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75], [0.5, 0.15], [0.9, 0.1]];
    return pts.some(([fx, fy]) => inside(e, deepHit(x1 + (x2 - x1) * fx, y1 + (y2 - y1) * fy))); };
  const tried = e => { for (let n = e; n; n = n.parentNode || n.host) if (n.getAttribute && (n.hasAttribute('data-radar-tried') || n.hasAttribute('inert'))) return true; return false; };
  all('[data-radar-popup]').forEach(e => e.removeAttribute('data-radar-popup'));
  const named = all(sel).filter(e => shown(e) && !isCart(e) && !e.closest('header, nav') && !tried(e) && onScreen(e))
    .filter(e => { const r = e.getBoundingClientRect();
      return e.getAttribute('aria-modal') === 'true' || e.tagName === 'DIALOG' ||
             (fixedish(e) && r.width * r.height >= 0.08 * vw * vh); });
  // Popups with no telling names (utility classes like "fixed inset-0 z-50", soulflower.in's YourLio
  // popup, bench 3): any FIXED layer covering half the screen that takes clicks and holds a small box
  // with a button in it. Cart drawers, the site header and click-through layers are excluded.
  const bareX = r => [...r.querySelectorAll('div, span, i, em, b, p'), ...[...r.querySelectorAll('*')].filter(x => x.shadowRoot)
      .flatMap(x => [...x.shadowRoot.querySelectorAll('div, span, i, em, b, p')])].some(c => {
    if (!/^(×|✕|✖)$/.test((c.innerText || '').trim()) || c.children.length > 1) return false;
    const b = c.getBoundingClientRect(); return b.width > 0 && b.width <= 48 && b.height <= 48; });
  const generic = [...document.querySelectorAll('body *'), ...hosts.flatMap(h => [...h.querySelectorAll('*')])].filter(e => {
    const s = getComputedStyle(e); if (s.position !== 'fixed' || !shown(e) || s.pointerEvents === 'none') return false;
    const r = e.getBoundingClientRect(); if (r.width * r.height < 0.5 * vw * vh || (parseInt(s.zIndex) || 0) < 1) return false;
    if (isCart(e) || e.closest('header, nav, [id*="header" i]') || tried(e) || !onScreen(e)) return false;
    // a popup is small inside: not the page's own content (main, the buy form, a menu full of links)
    if (e.querySelector('main, form[action*="/cart/add"], [role="navigation"]') || e.querySelectorAll('a[href]').length > 12) return false;
    // a control to close it: a real button, or a bare "×" drawn by a div/span (soulflower.in birthday popup, 9 Oct)
    return !!e.querySelector('button, [role="button"]') || bareX(e);
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
    // a bare "×" drawn by a div / span / i (no button, no label): small, clickable-looking, near the top of the box
    btn = btn || within(root, 'div, span, i, em, b, p').filter(shown).find(c => {
      if (!/^(×|✕|✖|x)$/i.test(text(c)) || c.children.length > 1) return false;
      const b = c.getBoundingClientRect(); return b.width <= 48 && b.height <= 48 &&
        (getComputedStyle(c).cursor === 'pointer' || /close|dismiss|cross/i.test(String(c.className || ''))); });
    if (!btn || /accept|agree|allow all|subscribe|sign ?up|submit|yes/i.test(text(btn))) continue;   // never consent, never sign up
    btn.setAttribute('data-radar-target', 'dismiss'); root.setAttribute('data-radar-popup', '1');
    return {kind, text: t.slice(0, 60), button: text(btn).slice(0, 30) || (btn.getAttribute('aria-label') || 'close')};
  }
  return null;
}"""


STILL_OPEN_JS = r"""() => { const all = []; const walk = r => r.querySelectorAll('*').forEach(e => { if (e.hasAttribute('data-radar-popup')) all.push(e);
    if (e.shadowRoot) walk(e.shadowRoot); }); walk(document);
  const e = all[0]; if (!e || !e.isConnected) return false;
  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
  const open = r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || 1) > 0.05
    && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  if (open) e.setAttribute('data-radar-tried', '1');
  return open; }"""


class AgeGate(Exception):
    pass


IFRAME_POPUPS_JS = r"""() => {
  const vw = innerWidth, vh = innerHeight; const out = [];
  document.querySelectorAll('iframe').forEach((f, i) => {
    const r = f.getBoundingClientRect(); const s = getComputedStyle(f);
    const name = ((f.id || '') + ' ' + (f.className || '') + ' ' + (f.title || '') + ' ' + (f.name || '') + ' ' +
                  (f.getAttribute('src') || '')).toLowerCase();
    let fixed = false; for (let n = f; n && n !== document.body; n = n.parentElement) { if (getComputedStyle(n).position === 'fixed') { fixed = true; break; } }
    // A fixed iframe over half the screen that is on top at its centre blocks the shopper whatever it is called:
    // GoKwik KwikPass login (id 'iframe-kp', src pdp.gokwik.co/kwikpass; bonkerscorner, boldcare, consciouschemist).
    const x = Math.min(vw - 1, Math.max(0, r.left + r.width / 2)), y = Math.min(vh - 1, Math.max(0, r.top + r.height / 2));
    const covers = r.width * r.height >= 0.5 * vw * vh && document.elementFromPoint(x, y) === f;
    if (s.display !== 'none' && s.visibility !== 'hidden' && r.width * r.height >= 0.08 * vw * vh && fixed
        && (covers || /popup|modal|klaviyo|newsletter|privy|wisepops|optin|spin|offer|signup|subscribe|kwikpass|gokwik/.test(name))) {
      f.setAttribute('data-radar-iframe', String(i)); out.push(String(i)); }
  });
  return out;
}"""



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
        try:
            still = page.evaluate(STILL_OPEN_JS)
        except Exception:  # noqa: BLE001  page navigating: the popup went with it
            still = False
        if still:
            found = {**found, "button": f"{found.get('button')}; did not close it, skipped"}
        done.append(found)
    return done


CLOSE_IN_FRAME_JS = r"""() => {
  // The close control inside a popup frame, as a shopper finds it: a VISIBLE close / dismiss control, a visible
  // '×' / 'No thanks', or an icon-only control (svg / img, no text) small and top-right in the popup box.
  // GoKwik KwikPass (bonkerscorner, run 37958664708): the first '.close' match in the frame was hidden, its real
  // close an icon-only element, so the CSS selector + Escape left it open. Never a join / login / subscribe control.
  document.querySelectorAll('[data-radar-close]').forEach(e => e.removeAttribute('data-radar-close'));
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || 1) > 0.05; };
  const text = e => (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim().replace(/\s+/g, ' ');
  const bad = /subscribe|sign ?up|join|log ?in|login|continue|submit|accept|agree|yes|verify|otp/i;
  const all = [...document.querySelectorAll('button, a, [role="button"], [aria-label], [class*="close" i], [class*="dismiss" i], div, span, i, svg, img')]
    .filter(e => shown(e) && !bad.test(text(e)));
  const named = all.find(e => /close|dismiss/i.test((e.getAttribute('aria-label') || '') + ' ' + String(e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className || '')))
    || all.find(e => /^(×|✕|✖|x|close|no thanks|no,? thanks|not now|maybe later|skip)$/i.test(text(e)) && e.children.length <= 1);
  let btn = named;
  if (!btn) {
    const icon = e => !text(e) && (e.matches('svg, img') || e.querySelector('svg, img'));
    const vw = innerWidth, vh = innerHeight;
    btn = all.filter(e => { if (!icon(e) || e.matches('svg *')) return false;
        const r = e.getBoundingClientRect(); if (r.width > 48 || r.height > 48) return false;
        const box = (e.parentElement || document.body).closest('div[style], div[class], section, form') || document.body;
        const b = box.getBoundingClientRect();
        return r.left > b.left + b.width * 0.6 && r.top < b.top + Math.max(b.height * 0.3, 60) &&
               (getComputedStyle(e).cursor === 'pointer' || e.matches('button, [role="button"], a') || !!e.onclick); })
      .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
  }
  if (!btn) return null;
  const tgt = btn.matches('svg, svg *, img') && btn.parentElement ? btn.closest('button, a, [role="button"], div, span') || btn : btn;
  tgt.setAttribute('data-radar-close', '1');
  return text(tgt).slice(0, 30) || 'close icon';
}"""


def _iframe_popup(page) -> dict | None:
    """Marketing / login popups rendered inside an <iframe> (srcdoc popups, GoKwik KwikPass from another origin):
    find the close control inside the frame like a shopper would (CLOSE_IN_FRAME_JS). Escape as a last resort.
    Never clicks subscribe / accept / join / log in."""
    try:
        ids = page.evaluate(IFRAME_POPUPS_JS)
    except Exception:  # noqa: BLE001
        return None
    for i in ids:
        sel = f'iframe[data-radar-iframe="{i}"]'
        how = ""
        try:
            frame = page.locator(sel).first.element_handle(timeout=1500).content_frame()
            label = frame.evaluate(CLOSE_IN_FRAME_JS) if frame else None
            if label:
                frame.locator('[data-radar-close]').first.click(timeout=3000)
                how = f"clicked {label!r} inside the popup frame"
        except Exception:  # noqa: BLE001  frame gone / navigating / click intercepted
            how = ""
        if not how:
            page.keyboard.press("Escape")
            how = "pressed Escape"
        page.wait_for_timeout(500)
        try:
            still = page.locator(sel).count() and page.locator(sel).first.is_visible()
        except Exception:  # noqa: BLE001
            still = False
        return {"kind": "popup (iframe)", "text": f"iframe popup {i}", "button": how + ("" if not still else "; still open")}
    return None
