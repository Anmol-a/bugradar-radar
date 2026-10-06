"""A small fake Shopify store for end-to-end tests. Runs locally, no network.

Modes (to prove Radar catches and heals what it should):
  healthy          everything works
  renamed_button   add-to-cart button lost name="add", sits outside the form, says "Add to Bag"
                   -> Radar must HEAL the locator and still pass
  broken_price     one product's structured price is 0 -> that product test must FAIL (confirmed)
  cart_broken      /cart/add.js returns 500 -> cart (critical) must FAIL -> run verdict DOWN
  obscure_button   buy button says "Grab it" with no telling attributes -> heuristic cannot match;
                   only the LLM rung can heal it (tests use a fake LLM)
  not_shopify      no Shopify markers -> verdict UNSUPPORTED, no tests generated
  wrong_variant    the STORE's main buy button sends another product's id (a real site bug)
                   -> cart and journey must FAIL with "expected <this product>, got <other>"
  free_gift        after a product is added, the store's script adds a Rs 0 gift in a second
                   request (moxiebeauty.in adds a free Travel Pouch) -> PASS with a warning
  newsletter_popup a full-screen "10% off, subscribe" popup appears ~0.4s after every page load
                   until closed -> Radar closes it (never subscribes) and PASSES
  cookie_banner    a cookie banner with Accept all / Decline -> Radar must click Decline, PASS
  variant_required the vase has no size selected and its buy button is disabled until a size is
                   picked -> Radar picks the first in-stock size like a shopper and PASSES
  password         storefront password page -> verdict BLOCKED, nothing tested
  age_gate         "Are you 18+?" gate on every page -> Radar never confirms age; tests BLOCKED
  hostile          everything that broke Radar on the 4 Oct bench, at once: 40 hidden mega-menu
                   product links before the content, collections only inside a closed dropdown, a
                   disabled (aria-disabled) menu parent link, an off-screen product link, a WhatsApp
                   chat bubble fixed bottom-right, a newsletter popup inside an
                   iframe, a short display title on the PDP, and /search disallowed in robots.txt
                   -> everything PASSES (title mismatch = warning only)
  no_collection_links  homepage has no link to any collection (like the Origin theme demo) and a
                   hamburger menu that opens a full-screen drawer with no collection links in it
                   -> journey closes it (fresh homepage), goes home -> product and PASSES
  title_layouts    product names where real themes put them (bench 2, 4 Oct): the vase's name is an
                   <h2> rich-text block with no <h1> (Publisher theme), the spoons' name sits in the
                   theme's own <header class="product__header">, <body> carries a class containing
                   "card" (bummer.in) -> every product test PASSES
  card_layouts     product cards as real themes build them: on the homepage an image slider sits OVER
                   the card link and opens the product on click (bummer.in's suspected pattern); on
                   collection pages a badge layer covers the card's centre but not its top
                   (theme-spotlight, where Playwright's own centre click was intercepted) -> PASSES
  renamed_title    the vase's page shows it as "Handmade Vase in Clay" in a big div, no <h1>: Radar's rules
                   cannot tell it is the product name -> confirmed fail -> LLM triage blames Radar ->
                   re-check with LLM help finds the name, code verifies it -> PASS (healed after triage)
  unknown_overlay  a full-screen "lucky wheel" overlay Radar's popup rules do not know, with "Try my luck"
                   and "I'll pass" -> journey fails -> triage -> re-check: LLM picks "I'll pass" (never
                   "Try my luck") -> PASS
  hover_menu       homepage shows no products and no visible collection link; the top menu "Shop" opens a
                   mega menu only on mouse HOVER (thehouseofrare.com, bench 3) -> journey hovers, PASSES
  brand_landing    same homepage, no mega menu: "Shop" links to /pages/shop, a landing page that lists
                   collections (thehouseofrare.com MEN -> /pages/rare-rabbit) -> journey hops there, PASSES
  rerender_grid    collection grids are first rendered with dead links, then replaced ~1.5 s later by a
                   search app with working ones (dotandkey.com + SearchTap) -> journey clicks again, PASSES
  icon_popup       product pages open a popup with only utility classes ("fixed inset-0 z-50") and an
                   icon-only X close button (soulflower.in's YourLio popup) -> closed, PASSES
  sticky_price     the product price is only in a sticky bar shown after scrolling (supplysix.com) -> PASSES
  hidden_product   the vase is listed in a collection but its product page redirects to the homepage
                   (boldcare.in) -> product test WARNS "hidden from shoppers" and tests the next product
  notfound_product the vase's page answers 200 "Page Not Found" (foxtale.in) -> same, WARNS + next product
  no_buy_form      the vase's page has no add-to-cart form for the vase, only other products' quick-add
                   buttons (mcaffeine.com) -> product test FAILS saying exactly that
  quick_named_main the MAIN buy button sits in <form class="cart-form"><div class="quick-add-container">, a plain
                   type=button with no /cart/add action, plus the icon-only popup (soulflower.in, bench 3)
                   -> found as the product's own button, cart flow PASSES
  search_misses    searching the first product's word returns only unrelated products (the word came from a
                   product the store hides: foxtale.in 'purify', bench 3) -> a second word is tried; the miss is a
                   WARNING, the search test PASSES on the second word
  drawer_form_first  before the main form, a hidden cart-drawer form named product_form_<n> holding ANOTHER
                   product's variant (boldcare.in, bench 4); the vase needs a size picked -> reads the OWN form, PASSES
  upsell_forms_first  same, but hidden cart-upsell 'shopify-product-form's with an EMPTY id come first (bummer.in,
                   bench 4) -> reads the OWN form, PASSES
  unavailable_product  the vase's page answers 200 with "The product is currently unavailable", body class
                   hidden_product, no buy form for the vase, but Product ld+json and a hidden drawer form for ANOTHER
                   product (plumgoodness.com, benches 4 and 7) -> WARNS + next product
  home_at_product_url  the vase's URL renders the homepage (product cards, no vase name, no form, no data) like a
                   headless storefront hiding a product (foxtale.in, bench 4) -> WARNS + next product
  price_desktop_hidden the vase's only on-page price is in a sticky bar the theme hides on desktop (class
                   hidden-lap-and-up, supplysix.com, bench 4) -> product test FAILS saying the price is only in a
                   hidden element (a real store finding on this screen size)
  store_refuses    the homepage answers HTTP 423 "This store is unavailable" to Radar (plumgoodness.com, bench 5, while
                   it was live for other visitors) -> verdict BLOCKED with the reason, never "unsupported" (not Shopify)
  shift_after_scroll  collection cards are tall blocks; ~10 ms after the first scroll the page re-lays out and
                   every card moves down by one card (boat-lifestyle.com, bench 5: header change after scroll; a click
                   30 ms after the pick opened the NEXT product) -> journey waits for the layout, re-aims, PASSES
  no_title         product pages show no product name at all (snitch.co.in after its move)
                   -> product tests FAIL at shows_title_price_image with "none"

Every product page also carries a quick-add product card BEFORE the main buy button in the DOM
and a cart drawer that opens over the page after adding (both copied from moxiebeauty.in's
theme, where Radar originally clicked the card's button and added the wrong product).

Run by hand to look at it:  python3 -m tests.mockstore.server healthy 8765
"""
from __future__ import annotations

import json
import re
import sys
import threading
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PRODUCTS = [
    {"id": 1, "handle": "sold-out-lamp", "title": "Brass Table Lamp", "price": "2499.00",
     "variants": [{"id": 101, "available": False, "price": "2499.00"}], "collection": "home-decor"},
    {"id": 2, "handle": "ceramic-vase", "title": "Ceramic Flower Vase", "price": "1299.00",
     "variants": [{"id": 201, "available": True, "price": "1299.00", "option1": "Small", "title": "Small"},
                  {"id": 202, "available": True, "price": "1399.00", "option1": "Large", "title": "Large"}],
     "collection": "home-decor"},
    {"id": 3, "handle": "wooden-spoon-set", "title": "Wooden Spoon Set", "price": "499.00",
     "variants": [{"id": 301, "available": True, "price": "499.00"}], "collection": "kitchen"},
]
PRODUCTS.append({"id": 4, "handle": "free-sampler", "title": "Vase Sampler", "price": "0.00",
                 "variants": [{"id": 401, "available": True, "price": "0.00"}], "collection": "home-decor",
                 "redirect": "/products/ceramic-vase"})   # like Moxie's Rs 0 samplers: JS-redirects on load
PRODUCTS.append({"id": 5, "handle": "gift-pouch", "title": "Travel Pouch (gift)", "price": "1.00",
                 "variants": [{"id": 501, "available": True, "price": "1.00"}], "collection": None,
                 "unavailable_page": True})   # like plumgoodness.com's Rs 1 freebie: not in any collection
COLLECTIONS = [{"handle": "home-decor", "title": "Home Decor"}, {"handle": "kitchen", "title": "Kitchen"}]
CARTS: dict[str, list[dict]] = {}

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#c96"/></svg>'


def page(title: str, body: str, extra_head: str = "", shopify: bool = True) -> str:
    marks = ('<link rel="stylesheet" href="https://cdn.shopify.com/s/files/1/0000/theme.css" onerror="this.remove()">'
             '<script>window.Shopify = window.Shopify || {}; Shopify.theme = {name: "Dawn"};</script>') if shopify else ""
    img = "/cdn/shop/files/logo.svg" if shopify else "/static/logo.svg"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<meta name="description" content="Mock Store sells handmade home decor and kitchen goods, shipped across India within five days.">
<link rel="canonical" href="/"><meta property="og:title" content="{title}"><meta property="og:image" content="/cdn/shop/files/og.svg">
{marks}
{extra_head}</head><body>
<header><div class="drawer" style="display:none"><nav><a href="/collections/all">Shop all</a></nav></div><nav><a href="/collections/home-decor">Home Decor</a> <a href="/collections/kitchen">Kitchen</a> <a href="/pages/about">About</a></nav><a class="cart-icon" href="/cart">Cart</a>
<form action="/search" method="get"><input name="q"></form></header>
<main>{body}</main><footer><img src="{img}" width="40" height="40" alt="logo"></footer></body></html>"""


def card(p):
    return f'<a class="card" href="/products/{p["handle"]}"><img src="/cdn/shop/files/{p["handle"]}.svg" width="40" height="40" alt="">{p["title"]}</a>'


class Handler(BaseHTTPRequestHandler):
    mode = "healthy"
    SEEN_UA: set = set()          # every User-Agent that reached the store (tests check Radar's identity)

    def log_message(self, *a):  # quiet
        pass

    def _cart_id(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c["cart"].value if "cart" in c else None

    OVERLAYS = {
        "newsletter_popup": """<script>
if (!document.cookie.includes('nl_closed=1')) setTimeout(() => {
  const d = document.createElement('div');
  d.className = 'klaviyo-form newsletter-popup'; d.setAttribute('role', 'dialog'); d.setAttribute('aria-modal', 'true');
  d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99';
  d.innerHTML = `<div style="background:#fff;margin:15vh auto;width:360px;padding:20px"><h2>Get 10% off your first order!</h2>
    <input type="email" placeholder="Email"><button type="button" class="sub">Subscribe</button>
    <button type="button" class="x" aria-label="Close dialog">×</button></div>`;
  d.querySelector('.sub').onclick = () => { window.SUBSCRIBED = true; d.remove(); };
  d.querySelector('.x').onclick = () => { document.cookie = 'nl_closed=1;path=/'; d.remove(); };
  document.body.appendChild(d);
}, 400);
</script>""",
        "cookie_banner": """<script>
if (!document.cookie.includes('consent=')) document.addEventListener('DOMContentLoaded', () => {
  const d = document.createElement('div');
  d.id = 'cookie-consent'; d.className = 'cookie-banner';
  d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;height:30vh;background:#222;color:#fff;z-index:98;padding:20px';
  d.innerHTML = `We use cookies and similar tracking technologies to improve your experience.
    <button type="button" class="acc">Accept all</button> <button type="button" class="dec">Decline</button>`;
  d.querySelector('.acc').onclick = () => { document.cookie = 'consent=accepted;path=/'; d.remove(); };
  d.querySelector('.dec').onclick = () => { document.cookie = 'consent=declined;path=/'; d.remove(); };
  document.body.appendChild(d);
});
</script>""",
        "age_gate": """<div class="age-gate" role="dialog" aria-modal="true" style="position:fixed;inset:0;background:rgba(0,0,0,.9);color:#fff;z-index:99;padding:20vh 20px">
          <p>Are you 18+? You must be of legal age to enter this site.</p><button type="button">Yes, I am 18+</button> <button type="button">No</button></div>""",
    }

    MAIN_NAV = ('<nav><a href="/collections/home-decor">Home Decor</a> <a href="/collections/kitchen">Kitchen</a> '
                '<a href="/pages/about">About</a></nav>')
    HOSTILE_HEADER = ('<div class="mega-menu" style="display:none">' +
                      "".join(f'<a href="/products/hidden-{i}">Hidden {i}</a>' for i in range(40)) + '</div>')
    HOSTILE_NAV = ('<nav><a href="/collections/home-decor" role="link" aria-disabled="true">Shop</a> '
                   '<details class="menu"><summary aria-expanded="false">Categories</summary>'
                   '<div><a href="/collections/kitchen">Kitchen</a></div></details> <a href="/pages/about">About</a></nav>')
    HOSTILE_TAIL = """<a class="chat-widget__link" aria-label="Chat on WhatsApp" href="https://wa.me/1" style="position:fixed;right:16px;
      bottom:16px;width:140px;height:140px;border-radius:70px;background:#25d366;z-index:90;display:block"></a>
<script>
if (!document.cookie.includes('ifp=1')) setTimeout(() => {
  const f = document.createElement('iframe');
  f.id = 'popup-iframe-open-1';
  f.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;border:0;z-index:99';
  f.srcdoc = `<html><body style="background:rgba(0,0,0,.6)"><div style="background:#fff;width:300px;margin:20vh auto;padding:20px">
    <p>Spin to win 15% off!</p><button class="subscribe">Subscribe</button> <button class="close" aria-label="Close">×</button></div></body></html>`;
  f.addEventListener('load', () => {
    const d = f.contentDocument;
    d.querySelector('.close').addEventListener('click', () => { document.cookie = 'ifp=1;path=/'; f.remove(); });
    d.querySelector('.subscribe').addEventListener('click', () => { window.SUBSCRIBED = true; f.remove(); });
  });
  document.body.appendChild(f);
}, 400);
</script>"""

    def _send(self, code, body, ctype="text/html; charset=utf-8", set_cart=None):
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in self.OVERLAYS:
            body = body.replace("</body>", self.OVERLAYS[self.mode] + "</body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "hostile":
            body = body.replace("<header>", "<header>" + self.HOSTILE_HEADER, 1)
            body = body.replace(self.MAIN_NAV, self.HOSTILE_NAV, 1)
            body = body.replace("<h1>Ceramic Flower Vase</h1>", "<h1>Handmade Pot</h1>")
            if "/collections/" in self.path:            # off-screen card (carousel slide) on collection pages
                body = body.replace('<main>', '<main><a href="/products/ceramic-vase" style="position:absolute;left:-3000px;top:0">'
                                              'Ceramic Flower Vase</a>', 1)
            body = body.replace("</body>", self.HOSTILE_TAIL + "</body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "title_layouts":
            body = body.replace("<body>", '<body class="template-product card-hover-effect-none">', 1)
            body = body.replace("<h1>Ceramic Flower Vase</h1>",
                                '<div class="rich-text"><h2 class="rich-text__heading h1"><span>Ceramic Flower Vase</span></h2></div>'
                                '<span class="visually-hidden" style="position:absolute;width:1px;height:1px;overflow:hidden">'
                                'Decrease quantity for Ceramic Flower Vase</span>')
            body = body.replace("<h1>Wooden Spoon Set</h1>",
                                '<header class="product__header"><div class="product__title" style="font-size:22px">'
                                'Wooden Spoon Set</div></header>')
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "card_layouts":
            path_ = urlparse(self.path).path.rstrip("/") or "/"
            if path_ == "/":
                body = re.sub(r'<a class="card" href="/products/([^"]+)">(.*?)</a>', lambda m: (
                    f'<div class="product-card" style="position:relative;display:inline-block;width:220px;height:260px;margin:8px">'
                    f'<a class="card" href="/products/{m.group(1)}" style="display:block;height:260px">{m.group(2)}</a>'
                    f'<div class="card__media-slider" onclick="location.href=\'/products/{m.group(1)}\'" '
                    f'style="position:absolute;left:0;top:0;width:220px;height:260px;cursor:pointer;background:rgba(0,0,0,.02)"></div></div>'), body)
            elif path_.startswith("/collections/"):
                body = re.sub(r'<a class="card" href="/products/([^"]+)">(.*?)</a>', lambda m: (
                    f'<div class="product-card" style="position:relative;display:inline-block;width:220px;margin:8px">'
                    f'<a class="card" href="/products/{m.group(1)}" style="display:block;height:300px">{m.group(2)}</a>'
                    f'<div class="badge-layer" style="position:absolute;left:0;top:60px;width:220px;height:240px;'
                    f'background:rgba(255,255,255,.01)"></div></div>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "renamed_title":
            body = body.replace("<h1>Ceramic Flower Vase</h1>",
                                '<div class="pdp-name" style="font-size:28px;font-weight:600">Handmade Vase in Clay</div>')
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "unknown_overlay":
            body = body.replace("</body>", """<script>
if (!document.cookie.includes('wheel=1')) document.addEventListener('DOMContentLoaded', () => {
  const d = document.createElement('div'); d.className = 'lucky-wheel';
  d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:99;color:#fff;padding:20vh 30vw';
  d.innerHTML = '<p>Spin the wheel!</p><button type="button" class="try">Try my luck</button> <button type="button" class="pass">I&#39;ll pass</button>';
  d.querySelector('.try').onclick = () => { window.SPUN = true; document.cookie = 'spun=1;path=/'; };
  d.querySelector('.pass').onclick = () => { document.cookie = 'wheel=1;path=/'; d.remove(); };
  document.body.appendChild(d); });
</script></body>""")
        path_now = urlparse(self.path).path.rstrip("/") or "/"
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in ("hover_menu", "brand_landing"):
            if path_now == "/":                       # brand-landing homepage: no products, no collection links
                body = re.sub(r'<a class="card"[^>]*>.*?</a>', "", body)
            mega = ('<div class="mega" style="position:absolute;left:0;top:30px;background:#fff;padding:10px;display:none">'
                    '<a href="/collections/home-decor">Home Decor</a> <a href="/collections/kitchen">Kitchen</a></div>'
                    if self.mode == "hover_menu" else "")
            body = body.replace(self.MAIN_NAV, '<nav><div class="menu-item" style="position:relative;display:inline-block">'
                                f'<a href="/pages/shop">Shop</a>{mega}</div> <a href="/pages/about">About</a></nav>'
                                '<style>.menu-item:hover .mega{display:block !important}</style>', 1)
            body = body.replace('<a href="/collections/all">Shop all</a>', '')
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "rerender_grid" and path_now.startswith("/collections/"):
            body = body.replace("<main>", '<main><div id="grid">', 1).replace("</main>", "</div></main>", 1)
            body = body.replace("</body>", """<script>
document.querySelectorAll('#grid a.card').forEach(a => a.addEventListener('click', e => e.preventDefault()));  // old grid: dead
setTimeout(() => { const g = document.getElementById('grid'); g.innerHTML = g.innerHTML; }, 3000);           // search app re-renders
</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in ("icon_popup", "quick_named_main") and "/products/" in self.path:
            body = body.replace("</body>", """<script>
if (!document.cookie.includes('lio=1')) setTimeout(() => {
  const d = document.createElement('div'); d.className = 'fixed inset-0 z-50 flex justify-center';
  d.style.cssText = 'position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.4)';
  d.innerHTML = '<div class="w-full relative bg-white shadow-2xl" style="position:relative;background:#fff;width:600px;height:340px">'
    + '<button type="button" class="absolute top-2 right-2 z-20 w-6 h-6 rounded-full" style="position:absolute;top:8px;right:8px;width:24px;height:24px">'
    + '<svg viewBox="0 0 24 24" width="24" height="24" class="lucide lucide-x w-3 h-3"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg></button>'
    + '<h2>NEW LAUNCH: BOMB SIZE SPRAY</h2><button type="button" class="try" style="padding:12px">TRY IT NOW</button></div>';
  d.querySelector('.try').onclick = () => { window.TRIED = true; };
  d.querySelector('.absolute').onclick = () => { document.cookie = 'lio=1;path=/'; d.remove(); };
  document.body.appendChild(d); }, 300);
</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "sticky_price" and "/products/" in self.path:
            body = re.sub(r'<div class="price">(₹[^<]*)</div>', r'<div style="height:2200px"></div><div class="sticky-atc" '
                          r'style="position:fixed;bottom:0;left:0;right:0;background:#eee;display:none">You Pay: \1</div>'
                          r'<script>addEventListener("scroll", () => { document.querySelector(".sticky-atc").style.display = '
                          r'scrollY > 300 ? "block" : "none"; });</script>', body, count=1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "quick_named_main" and "/products/" in self.path:
            m = re.search(r'<product-form class="product-form">.*?name="id" value="(\d+)"', body, flags=re.S)
            if m:
                body = re.sub(r'<product-form class="product-form">.*?</product-form>',
                              f'<form class="cart-form"><div class="quick-add-container" data-variant-id="{m.group(1)}">'
                              f'<input type="hidden" name="id" value="{m.group(1)}"><button type="button" class="custom-cart-btn add-btn" '
                              f'onclick="addToCart({m.group(1)})"><span class="btn-text">Add to cart</span></button></div></form>',
                              body, count=1, flags=re.S)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "no_buy_form" and "/products/ceramic-vase" in self.path:
            body = re.sub(r'<product-form class="product-form">.*?</product-form>', '', body, count=1, flags=re.S)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "no_title" and "/products/" in self.path:
            body = re.sub(r"<h1>[^<]*</h1>", "", body, count=1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "no_collection_links":
            body = body.replace(self.MAIN_NAV, '<nav><a href="/pages/about">About</a></nav>'
                                '<details class="menu-drawer"><summary class="header__icon--menu" aria-expanded="false">Menu</summary>'
                                '<div style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:60">'
                                '<a href="/pages/about" style="color:#fff">About us</a></div></details>', 1)
            body = body.replace('<a href="/collections/all">Shop all</a>', '')
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if set_cart:
            self.send_header("Set-Cookie", f"cart={set_cart}; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200, set_cart=None):
        self._send(code, json.dumps(obj), "application/json", set_cart)

    def _cart(self):
        cid = self._cart_id()
        new = None
        if not cid:
            cid = new = uuid.uuid4().hex
        CARTS.setdefault(cid, [])
        return cid, new

    def do_GET(self):
        Handler.SEEN_UA.add(self.headers.get("User-Agent") or "")
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        cid, new = self._cart()
        if self.mode == "password" and path not in ("/password", "/robots.txt") and not path.startswith("/cdn/"):
            self.send_response(302)
            self.send_header("Location", "/password")
            self.end_headers()
            return
        if path == "/password":
            return self._send(200, page("Mock Store - Opening soon",
                                        '<p>Opening soon</p><form action="/password" method="post">'
                                        '<input type="hidden" name="form_type" value="storefront_password">'
                                        '<input type="password" name="password"><button>Enter</button></form>'))
        if path == "/robots.txt":
            extra = "Disallow: /search\n" if self.mode == "hostile" else ""
            return self._send(200, "User-agent: *\nDisallow: /checkout\nDisallow: /cart\nDisallow: /account\n" + extra,
                              "text/plain")
        if path.startswith("/cdn/shop/files/") or path.startswith("/static/"):
            return self._send(200, SVG, "image/svg+xml")
        if path == "/" and self.mode == "store_refuses":
            return self._send(423, page("This store is unavailable", "<h1>This store is unavailable</h1>"))
        if path == "/":
            return self._send(200, page("Mock Store | Handmade Home Goods",
                                        "<h1>Welcome</h1>" + "".join(card(p) for p in PRODUCTS if p["collection"]),
                                        shopify=self.mode != "not_shopify"), set_cart=new)
        if path == "/pages/blank":       # HTTP 200 with a title but nothing on screen (JS crash style)
            return self._send(200, "<!doctype html><html><head><title>Blank | Mock Store</title></head><body><div id=app></div></body></html>")
        if path == "/pages/about":
            return self._send(200, page("About Mock Store", "<p>About us</p>"))
        if path == "/pages/shop":          # brand landing page (brand_landing mode)
            return self._send(200, page("Shop | Mock Store", '<h1>Our brands</h1><a class="tile" href="/collections/home-decor" '
                                        'style="display:block;width:300px;height:200px">Home Decor</a>'), set_cart=new)
        if path == "/collections.json":
            return self._json({"collections": [dict(c, products_count=sum(p["collection"] == c["handle"] for p in PRODUCTS))
                                               for c in COLLECTIONS]})
        if path == "/products.json":
            return self._json({"products": [{"id": p["id"], "handle": p["handle"], "title": p["title"],
                                             "variants": p["variants"]} for p in PRODUCTS]})
        if path.startswith("/collections/"):
            h = path.split("/")[2]
            items = [p for p in PRODUCTS if (h == "all" and p["collection"]) or p["collection"] == h]
            if path.endswith("products.json"):
                return self._json({"products": [{"handle": p["handle"], "title": p["title"], "variants": p["variants"]} for p in items]})
            if not items:
                return self._send(404, page("Not found", "404"))
            if self.mode == "shift_after_scroll":
                cards = "".join(card(p).replace('class="card"', 'class="card" style="display:block;height:200px;border:1px solid #ccc"')
                                for p in items)
                return self._send(200, page(f"{h.title()} Collection | Mock Store",
                    '<div style="height:900px">Banner</div><div id="spacer" style="height:0"></div>' + cards
                    + '<div style="height:2000px"></div><script>addEventListener("scroll", () => setTimeout(() => '
                      'document.getElementById("spacer").style.height = "200px", 8), {once: true});</script>'), set_cart=new)
            return self._send(200, page(f"{h.title()} Collection | Mock Store", "".join(card(p) for p in items)), set_cart=new)
        if path.startswith("/products/"):
            h = path.split("/")[2]
            if h.endswith(".js"):
                p = next((x for x in PRODUCTS if x["handle"] == h[:-3]), None)
                if not p:
                    return self._json({"error": "not found"}, 404)
                return self._json({"id": p["id"], "handle": p["handle"], "title": p["title"],
                                   "variants": [dict(v, price=int(float(v["price"]) * 100)) for v in p["variants"]]})
            p = next((x for x in PRODUCTS if x["handle"] == h), None)
            if not p:
                return self._send(404, page("Page not found", "<h1>404</h1>"))
            if self.mode == "hidden_product" and h == "ceramic-vase":
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if self.mode == "notfound_product" and h == "ceramic-vase":
                return self._send(200, page("404 Page Not Found | Mock Store", "<h1>Page Not Found</h1><p>Sorry.</p>"))
            if self.mode == "unavailable_product" and h == "ceramic-vase":
                # like plumgoodness.com (bench 7): Product ld+json in the head and a hidden cart-drawer form for ANOTHER
                # product, so "any form or ld+json" looked like a product page
                ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": p["title"],
                                 "offers": {"@type": "Offer", "price": p["price"], "priceCurrency": "INR"}})
                return self._send(200, page(f"{p['title']} | Mock Store",
                                            "<h2>The product is currently unavailable, please get back later.</h2>"
                                            "<a href='/collections/all'>Shop all</a><h3>Clay vase</h3><p>Hand-thrown in Khurja.</p>"
                                            '<div class="drawer" style="display:none"><form action="/cart/add" method="post">'
                                            '<input type="hidden" name="id" value="301"><button type="submit">Add</button></form></div>',
                                            f'<script type="application/ld+json">{ld}</script>')
                                  .replace("<body>", '<body class="no-focus-outline hidden_product">', 1), set_cart=new)
            if self.mode == "home_at_product_url" and h == "ceramic-vase":
                return self._send(200, page("Mock Store", "<h2>Bestsellers</h2>" + "".join(
                    card(x) for x in PRODUCTS if x["handle"] in ("wooden-spoon-set", "sold-out-lamp"))), set_cart=new)
            if p.get("unavailable_page"):
                return self._send(200, page(f"{p['title']} | Mock Store",
                                            "<p>The product is currently unavailable, please get back later.</p>"), set_cart=new)
            return self._send(200, self._pdp(p), set_cart=new)
        if path == "/cart.js":
            items = CARTS[cid]
            return self._json({"items": items, "item_count": sum(i["quantity"] for i in items),
                               "total_price": sum(i.get("price", 0) * i["quantity"] for i in items)}, set_cart=new)
        if path == "/cart":
            items = CARTS[cid]
            rows = "".join(f'<div class="cart-item">{i["title"]} x{i["quantity"]}</div>' for i in items) or "<p>Your cart is empty</p>"
            return self._send(200, page("Your Cart | Mock Store",
                                        f'{rows}<form action="/checkout" method="post"><button type="submit" name="checkout">Check out</button></form>'),
                              set_cart=new)
        if path == "/search":
            term = (q.get("q") or [""])[0].lower()
            hits = [p for p in PRODUCTS if term and term in p["title"].lower()]
            if self.mode == "search_misses" and term == "ceramic":
                hits = [p for p in PRODUCTS if p["handle"] == "wooden-spoon-set"]
            return self._send(200, page(f"Search: {term} | Mock Store", "".join(card(p) for p in hits) or "<p>No results</p>"))
        return self._send(404, page("Page not found", "<h1>404</h1>"))

    def _pdp(self, p):
        price = "0" if (self.mode == "broken_price" and p["handle"] == "ceramic-vase") else p["price"]
        ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": p["title"],
                         "image": [f"/cdn/shop/files/{p['handle']}.svg"],
                         "offers": {"@type": "Offer", "price": price, "priceCurrency": "INR",
                                    "availability": "https://schema.org/InStock"}})
        available = any(v["available"] for v in p["variants"])
        vid = next((v["id"] for v in p["variants"] if v["available"]), p["variants"][0]["id"])
        if not available:
            buy = '<button disabled>Sold out</button>'
        elif self.mode == "obscure_button":
            buy = (f'<div class="actions"><span role="button" tabindex="0" class="cta-x" data-vid="{vid}" '
                   f'onclick="addToCart(this.dataset.vid)">Grab it</span><span role="button" class="cta-y">Share</span></div>')
        elif self.mode == "renamed_button":
            buy = (f'<div class="pdp-actions"><button class="btn btn--primary js-bag" data-vid="{vid}" '
                   f'onclick="addToCart(this.dataset.vid)">Add to Bag</button></div>')
        else:
            other = next(x for x in PRODUCTS if x["handle"] != p["handle"] and x["handle"] != "sold-out-lamp"
                         and float(x["price"]) > 0)
            send = other["variants"][0]["id"] if self.mode == "wrong_variant" else vid
            need = self.mode in ("variant_required", "upsell_forms_first", "drawer_form_first") and len(p["variants"]) > 1
            radios = ""
            if need:
                radios = '<fieldset class="product-form__input"><legend>Size</legend>' + "".join(
                    f'<input type="radio" id="opt-{v["id"]}" name="Size" value="{v["option1"]}" '
                    f'onchange="const f=document.getElementById(\'product-form-template__main\');'
                    f'f.querySelector(\'[name=id]\').value=\'{v["id"]}\';f.querySelector(\'[name=add]\').disabled=false;">'
                    f'<label for="opt-{v["id"]}">{v["option1"]}</label>' for v in p["variants"]) + '</fieldset>'
            buy = (f'{radios}<product-form class="product-form"><form id="product-form-template__main" action="/cart/add" method="post" '
                   f'onsubmit="event.preventDefault();addToCart({'this.querySelector(&quot;[name=id]&quot;).value' if need else send})">'
                   f'<input type="hidden" name="id" value="{"" if need else vid}">'
                   f'<button type="submit" name="add"{" disabled" if need else ""}>Add to cart</button></form></product-form>')
        rec = next(x for x in PRODUCTS if x["handle"] != p["handle"] and float(x["price"]) > 0
                   and any(v["available"] for v in x["variants"]))
        rv = rec["variants"][0]["id"]
        quick = (f'<div class="card card--standard"><a href="/products/{rec["handle"]}">{rec["title"]}</a>'
                 f'<div class="quick-add no-js-hidden"><product-form><form id="quick-add-template__main{rec["id"]}" '
                 f'action="/cart/add" method="post" onsubmit="event.preventDefault();addToCart({rv})">'
                 f'<input type="hidden" name="id" value="{rv}"><button type="submit" name="add" '
                 f'id="quick-add-template__main{rec["id"]}-submit" class="quick-add__submit">Add to cart</button>'
                 f'</form></product-form></div></div>')
        script = """<script>
async function addToCart(id){ const r = await fetch('/cart/add.js',{method:'POST',headers:{'content-type':'application/json'},
  body: JSON.stringify({id:Number(id),quantity:1})});
  if(!r.ok){ console.error('add failed', r.status); return; }
  const item = await r.json();
  if (window.GIFT_MODE && Number(id) !== 401) {
    await fetch('/cart/add.js', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({id: 401, quantity: 1})}); }
  const d = document.createElement('cart-drawer'); d.className = 'drawer animate active';
  d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:50';
  d.innerHTML = '<div class="drawer__inner" style="position:absolute;right:0;top:0;width:360px;height:100%;background:#fff;padding:16px">'
    + '<h2>Your cart</h2><div class="cart-item">' + item.product_title + '</div>'
    + '<button type="button" name="checkout">Check out</button></div>';
  document.body.appendChild(d); }
</script>"""
        body = (f'{quick}<h1>{p["title"]}</h1><img src="/cdn/shop/files/{p["handle"]}.svg" width="300" height="300" alt="">'
                f'<div class="price">₹{float(p["price"]):,.2f}</div>{buy}')
        if self.mode == "drawer_form_first":
            body = (f'<div class="drawer__scrollable" style="display:none"><form id="product_form_9{rec["id"]}" '
                    f'class="shopify-product-form" action="/cart/add" method="post"><input type="hidden" name="id" value="{rv}">'
                    f'<button type="submit">Add</button></form></div>' + body)
        if self.mode == "upsell_forms_first":
            # nested like bummer.in's cart drawer: no product link within 6 levels, so it is not "another product's card"
            body = ('<div class="cart-drawer" style="display:none"><div class="cart-upsell"><div class="cart-upsell__list">'
                    + "".join(f'<cart-product-card class="cart-upsell__item"><div class="cart-upsell__detail"><div class="cart-upsell__content">'
                              f'<div class="cart-upsell__right"><product-form-component><form id="NativeCartUpsell-{k}" '
                              f'class="shopify-product-form" action="/cart/add" method="post"><input type="hidden" name="id" value="">'
                              f'<button type="submit" disabled>Add</button></form></product-form-component></div></div></div>'
                              f'</cart-product-card>' for k in range(3)) + '</div></div></div>' + body)
        if self.mode == "price_desktop_hidden" and p["handle"] == "ceramic-vase":
            body = re.sub(r'<div class="price">(₹[^<]*)</div>',
                          r'<style>@media (min-width: 1008px) { .hidden-lap-and-up { display: none !important; } }</style>'
                          r'<product-sticky-form class="product-sticky-form hidden-lap-and-up" hidden>'
                          r'<span class="product-sticky-form__price">You Pay: \1</span></product-sticky-form>', body, count=1)
        if self.mode == "free_gift":
            script += "<script>window.GIFT_MODE = true;</script>"
        if p.get("redirect"):
            script += f"<script>setTimeout(() => location.replace('{p['redirect']}'), 150)</script>"
        return page(f"{p['title']} | Mock Store", body, f'<script type="application/ld+json">{ld}</script>{script}')

    def do_POST(self):
        u = urlparse(self.path)
        cid, new = self._cart()
        if u.path == "/cart/add.js":
            if self.mode == "cart_broken":
                return self._json({"status": 500, "description": "cart service down"}, 500)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            vid = int(body.get("id"))
            p = next((x for x in PRODUCTS for v in x["variants"] if v["id"] == vid), None)
            if not p:
                return self._json({"status": 404}, 404)
            price = int(float(next(v["price"] for v in p["variants"] if v["id"] == vid)) * 100)
            CARTS[cid].append({"id": vid, "variant_id": vid, "product_id": p["id"], "handle": p["handle"],
                               "title": p["title"], "product_title": p["title"], "price": price, "quantity": 1})
            return self._json(CARTS[cid][-1], set_cart=new)
        return self._send(404, "not found")


def serve(mode: str = "healthy", port: int = 0):
    """Start in a background thread. Returns (server, base_url)."""
    handler = type("H", (Handler,), {"mode": mode})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "healthy"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    srv, url = serve(mode, port)
    print(f"mock store ({mode}) at {url}  Ctrl+C to stop")
    threading.Event().wait()
