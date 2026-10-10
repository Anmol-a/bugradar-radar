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
  search_app       /search is filled by a search app ~2.5 s after load: empty until then (bonkerscorner.com, baccabucci.com,
                   held-out new30b 9 Oct: Radar read the page at 0.8 s, '0 results') -> search PASSES
  search_app_popular  same, but 'popular products' (none matching the word) show until then (bellavitaorganic.com
                   'Custom Search': '0 of 23 mention the term') -> search PASSES
  delayed_scripts  a speed app delays every theme script until the shopper's first mousemove / touch / wheel / key:
                   html stays 'no-js' (price hidden by the theme's no-js CSS), window.Shopify undefined, search results
                   rendered by a delayed script (baccabucci.com, bellavitaorganic.com: new30b + re-run 9 Oct, evidence
                   'theme scripts had not run (html.no-js)') -> Radar moves the mouse like a shopper; product + search PASS
  search_app_scroll  the search page title carries Shopify's own count ('Search: N results found for ...') and the
                   search app renders its results only after the shopper scrolls (ptron.in desktop, new30c 10 Oct:
                   grey empty results area) -> Radar scrolls; search PASSES
  search_app_never  same title, but the search app never renders anything (ptron.in mobile, kushals.com mobile,
                   new30c 10 Oct: blank results area after 10 s); Shopify's own /search/suggest.json finds the word
                   -> search WARNS "search app did not render results", never FAILS
  pincode_gate     every in-stock product's buy button is disabled and reads 'PLEASE ENTER YOUR PINCODE TO CHECK
                   AVAILABILITY' with a pincode box + CHECK button below (bombaysweetshop.com, new30d 10 Oct: the right
                   variant WAS selected, Radar failed 'variant selected like a shopper') -> Radar never types a pincode
                   (no form but add-to-cart is ever submitted): product test PASSES with a 'pincode gate' WARNING, the cart
                   test is BLOCKED (cannot add without a pincode), never a failure
  disabled_dup_button  the main form's visible buy button is DISABLED, an enabled sticky ADD TO CART (form=) for the
                   same product is on screen (crossbeats.com mobile, new30e) -> Radar clicks the ENABLED one, PASSES
  healer_disabled_sticky  the form's own button is hidden, the healer finds a DISABLED 'Add to cart' by the title,
                   an enabled sticky ADD TO CART bar (no form link) is on screen (littleboxindia.com mobile) -> PASSES
  disabled_buy_now_only  add-to-cart disabled, only an enabled 'Buy it now' -> never clicked; the failure names it
  buy_now_first    an enabled 'Buy it now' sits before the add-to-cart in the form -> Radar clicks add to cart, PASSES
  stripped_first_home  the FIRST homepage load shows only one Shopify marker (cdn.shopify.com; koskii.com) -> Radar
                   loads it once more before calling it 'not Shopify', PASSES
  new_tab_cards    product links on the homepage and collections open in a NEW TAB (target=_blank; fashor.com,
                   tigc.in) -> journey follows the product into that tab, PASSES with a note
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
                   hidden element (a real store finding on this screen size). On a phone-sized screen (< 1008 px) the
                   theme shows that bar, so MOBILE passes (v0.18: every run tests both)
  store_refuses    the homepage answers HTTP 423 "This store is unavailable" to Radar (plumgoodness.com, bench 5, while
                   it was live for other visitors) -> verdict BLOCKED with the reason, never "unsupported" (not Shopify)
  shift_after_scroll  collection cards are tall blocks; ~10 ms after the first scroll the page re-lays out and
                   every card moves down by one card (boat-lifestyle.com, bench 5: header change after scroll; a click
                   30 ms after the pick opened the NEXT product) -> journey waits for the layout, re-aims, PASSES
  shadow_popup     the same YourLio-style promo popup, but rendered INSIDE a shadow root (#chat-widget), appearing
                   300 ms after collection pages load (soulflower.in, bench 9: the popup sat over the product card and
                   v0.13's finder could not see into shadow DOM) -> popup closed by its x, journey PASSES first try
  dead_image_link  collection cards have an IMAGE link and a NAME link; the theme's slider script cancels mousedown/click on
                   the image (thefunclab.com, bench 10) -> journey clicks the name, PASSES, store WARNING names the dead link
  slider_over_image  collection cards: a slider layer (not a link) sits OVER the product image link and swallows clicks;
                   the product NAME link below opens it (bummer.in /collections/men, 9 Oct cloud run: v0.19 failed 3/3 with
                   'could not click: ... intercepts pointer events' and never tried the name link) -> journey PASSES via the
                   name, store WARNING names the dead image link
  server_blip      the whole store answers HTTP 503 'Something went wrong' to every page for 3 s, starting the first time a
                   collection page is asked for (mid-journey), then normally (9 Oct cloud run: 3 Shopify demo stores 503 within the same 2 minutes; retries seconds apart
                   confirmed it) -> Radar waits before re-checking: homepage and tests PASS
  products_down    every product PAGE answers HTTP 503, always (a real outage) -> product tests are still CONFIRMED failures
                   after Radar's spaced re-checks; never softened into a hiccup
  div_x_popup      every page opens a 'It's Our Birthday' scratch-to-win popup whose only close control is a bare '×' in a
                   <div> (no button, no label, no telling class) (soulflower.in, 9 Oct cloud run) -> closed by its ×,
                   never 'Reveal my reward'; journey PASSES first try
  scroll_reveal    collection product cards are invisible (opacity 0, reveal animation) until the shopper scrolls the page
                   (reequil.com '151 not visible', wearcomet.com, held-out run 9 Oct) -> journey scrolls like a shopper,
                   PASSES
  signed_only      like Shopify's edge for cloud networks (9 Oct): EVERY request without a valid Web Bot Auth signature
                   (verified against Handler.SIGNER_X) answers HTTP 429 -> unsigned Radar is BLOCKED (rate-limited);
                   Radar with RADAR_SIGNING_KEY set PASSES
  flaky_data       every /products/<h>.js answers an EMPTY body the first time it is asked (palmonas.com, bench 11) and
                   JSON after that -> Radar retries the data request; tests PASS on the first attempt
  sticky_buy_only  product pages: the form's own button is hidden on desktop; the same form's button shows in a sticky
                   bar only after the shopper scrolls (true-elements.com, held-out 7 Oct) -> product tests PASS
  div_buy_control  product pages: NO /cart/add form; the buy control is <div class="pdp-addtobag-btn"
                   data-product-handle=...> "ADD TO BAG", after 3 recommendation cards with their own "Add to Bag"
                   buttons for other products (nicobar.com, held-out 7 Oct) -> product + cart PASS, right product added
  preselected_variant  the vase's page has NO cart form (headless-style div buy control) and pre-selects its SECOND
                   size ("Large", marked active + aria-checked); only that size's price is shown (foxtale.in, 9 Oct: the
                   200g Best Value size pre-selected, Radar read the first size's ₹349 and failed "price not on page")
                   -> product test reads the size the page shows and PASSES (v0.18 FAILED: expected the first size's price)
  drawer_decoy_popup  every homepage / collection page carries a CLOSED wishlist drawer (role=dialog, aria-modal, slid
                   off-screen with translateX(100%), close button unclickable) BEFORE a real promo popup in the DOM: a
                   bottom sheet over the product grid, 300 ms after load (suta.in wishlist drawer + soulflower.in cart drawer,
                   7-9 Oct: v0.18 retried the hidden drawer ~10 times and never closed the real popup) -> real popup
                   closed, journey PASSES on desktop and mobile
  rate_limited     every /products/<h>.js answers HTTP 429 Too Many Requests, always (Shopify throttling Radar's own
                   server IP, 9 Oct: all 3 demo stores 'down') -> tests needing product data are BLOCKED (rate-limited),
                   never a store failure, no incident; the run is never 'down'
  rate_limited_all  every Shopify data request (/collections.json, /products.json, /products/<h>.js) answers 429 (own
                   server, 9 Oct, after 3 runs in 40 min) -> discovery stops: verdict BLOCKED (rate-limited), not 'error'
  rate_limited_once  each /products/<h>.js answers 429 (Retry-After: 1) twice, then normally -> Radar backs off, PASSES
  noisy_console    every page logs a console error + warning and fires a request nobody answers (third-party widget)
                   -> everything PASSES; the noise only shows up as report evidence (v0.18)
  robots_500_twice /robots.txt answers HTTP 500 to the first two requests, then normally (soulflower.in / bummer.in,
                   bench 11: no answer on two quick tries) -> third try reads it; store tested, healthy
  robots_500       /robots.txt answers HTTP 500 (bench 8 lesson, RFC 9309) -> "do not crawl": BLOCKED, nothing tested
  robots_404       /robots.txt answers HTTP 404 (no file) -> everything allowed, store tested normally
  (Radar's own network dropping mid-run is not a mode: tests shut the store down from the progress hook and point
   the connectivity probe at a dead port (Radar offline) or at a live server (Radar online, store died).)
  empty_doc_title  every page renders normally but its <title> is empty (hairoriginals.com, new30e held-out)
                   -> pages load (no 'down'); the SEO test 'title' warns
  no_title         product pages show no product name at all (snitch.co.in after its move)
                   -> product tests FAIL at shows_title_price_image with "none"
  (every mode)     footer links to /pages/shipping-policy, /policies/refund-policy, /pages/privacy-policy,
                   /pages/terms-of-service, /pages/contact (+ an Instagram link) and a header account link
                   /account/login; robots.txt disallows /policies/ and /account like Shopify's default
                   -> journey 28 opens shipping, privacy, terms, contact (refund skipped: robots.txt); no account test
  broken_policies  the footer's shipping page answers 404, the privacy page is a heading with no text, the contact page
                   has no form, email or phone (journey 28) -> info.policy_pages FAILS naming shipping; privacy and
                   contact WARN
  account_open     robots.txt allows /account; /account/login shows an email + password form (journey 29)
                   -> info.account_page PASSES, nothing typed
  account_broken   same, but /account/login answers 404 -> info.account_page FAILS
  collection_js_error  collection pages throw an uncaught TypeError after load (journey 14) -> collection test WARNS
                   'uncaught JavaScript errors', never fails
  layout_shift     home + collection pages: a tall hero, then an offer banner pushes the page down 900 px 300 ms after
                   load (journey 17) -> 'Core Web Vitals' WARNS with the CLS value; the store stays healthy
  sideways_scroll  every page carries a 1700 px promo strip (journey 30) -> home, collection and product pages WARN
                   'page scrolls sideways' naming div.promo-marquee; the store stays healthy
  paginated        'Home Decor' also holds 4 clay bowls (its products.json too), 3 products per page with a 'Next page'
                   link (journey 26) -> catalog.more PASSES: page 2 shows new products. healthy (all fit on one page)
                   -> 'not judged'
  pagination_broken  same, but ?page=2 answers HTTP 500 -> catalog.more FAILS
  load_more        first 3 shown, a 'Load more' button appends the bowls -> PASSES after the click
  infinite_scroll  first 3 shown, the bowls are appended when the shopper reaches the bottom -> PASSES after scrolling
  more_hidden      first 3 shown, no page 2, no button, no scroll loading, while the data has 4 more in stock -> WARNS
  search_error_empty  /search answers HTTP 500 when nothing matches (journey 27) -> search.no_results FAILS
  predictive_search  the header search box sits in a Dawn-style <predictive-search> fed by /search/suggest.json
                   (journey 27) -> search.suggestions PASSES with the suggested product; healthy (no such element)
                   -> 'not judged: no search-as-you-type'
  predictive_broken  same element, but its script never renders suggestions while Shopify's endpoint finds the word
                   -> search.suggestions WARNS, never fails
  tall_sticky_bar  on phones, product pages carry a fixed info bar over the bottom 42% of the screen (journey 30)
                   -> product pages WARN 'screen covered by fixed bars' naming div.sticky-info; the store stays healthy

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
# journey 26: in the pagination modes 'Home Decor' also lists 4 clay bowls (collection pages + its products.json only)
EXTRA_DECOR = [{"id": 60 + i, "handle": f"clay-bowl-{i}", "title": f"Clay Bowl No. {i}", "price": "599.00",
                "variants": [{"id": 600 + i, "available": True, "price": "599.00"}], "collection": "home-decor"}
               for i in range(1, 5)]
MORE_MODES = ("paginated", "pagination_broken", "load_more", "infinite_scroll", "more_hidden")
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
<header><div class="drawer" style="display:none"><nav><a href="/collections/all">Shop all</a></nav></div><nav><a href="/collections/home-decor">Home Decor</a> <a href="/collections/kitchen">Kitchen</a> <a href="/pages/about">About</a></nav><a class="cart-icon" href="/cart">Cart</a> <a class="account-icon" href="/account/login" aria-label="Log in">&#128100;</a>
<form action="/search" method="get"><input name="q"></form></header>
<main>{body}</main><footer><img src="{img}" width="40" height="40" alt="logo"><div class="footer-menu">{FOOTER_LINKS}</div></footer></body></html>"""


FOOTER_LINKS = ('<a href="/pages/shipping-policy">Shipping Policy</a> <a href="/policies/refund-policy">Refund policy</a> '
                '<a href="/pages/privacy-policy">Privacy Policy</a> <a href="/pages/terms-of-service">Terms of Service</a> '
                '<a href="/pages/contact">Contact us</a> <a href="https://instagram.com/mockstore">Instagram</a>')
POLICY_TEXT = ("We ship every order within two working days from our studio in Jaipur. Delivery takes three to seven days "
               "across India; remote pin codes can take up to ten days. Shipping is free above Rs 999; below that a flat "
               "Rs 79 applies. You will get a tracking link by SMS and email as soon as the parcel leaves us. ")
INFO_PAGES = {"/pages/shipping-policy": "Shipping Policy", "/pages/privacy-policy": "Privacy Policy",
              "/pages/terms-of-service": "Terms of Service", "/policies/refund-policy": "Refund policy"}


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
        "kwikpass_popup": """<script>
// GoKwik KwikPass login popup (bonkerscorner.com, boldcare.in, consciouschemist.com: 'could not click: <iframe
// id="iframe-kp" class="iframe-kp" src="https://pdp.gokwik.co/kwikpass/kwikpass.html"> from <div id="d2c-pass">'):
// a full-screen iframe from ANOTHER origin, no popup-like name, its × inside the frame. Shown after the theme
// loads, on every page until closed. Its real close is an icon-only div (no label, no 'close' class) while a hidden
// 'close' button comes first in the frame (run 37958664708: 'pressed Escape; still open' with the class selector).
if (!document.cookie.includes('kp=1')) setTimeout(() => {
  const d = document.createElement('div'); d.id = 'd2c-pass';
  d.style.cssText = 'position:fixed;inset:0;z-index:2147483000';
  const f = document.createElement('iframe'); f.id = 'iframe-kp'; f.className = ' iframe-kp'; f.title = '';
  f.setAttribute('allow', 'otp-credentials');
  f.src = 'http://localhost:' + location.port + '/kwikpass/kwikpass.html';
  f.style.cssText = 'width:100%;height:100%;border:0;background:transparent';
  d.appendChild(f); document.body.appendChild(d);
  addEventListener('message', e => { if (e.data === 'kp-close') { document.cookie = 'kp=1;path=/'; d.remove(); } });
}, 500);
</script>""",
        "div_x_popup": """<script>
if (!document.cookie.includes('bday=1')) setTimeout(() => {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;inset:0;z-index:80;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center';
  d.innerHTML = '<div style="position:relative;background:#fff;width:640px;height:520px;border-radius:16px">'
    + '<div class="bd-x" style="position:absolute;top:12px;right:12px;width:22px;height:22px;cursor:pointer;text-align:center">×</div>'
    + "<h2>It's Our Birthday</h2><p>Scratch to see what you won.</p><div class='scratch' style='height:200px;background:pink'>SCRATCH TO WIN</div>"
    + '<span class="reveal" style="text-decoration:underline;cursor:pointer">Reveal my reward</span></div>';
  d.querySelector('.bd-x').onclick = () => { document.cookie = 'bday=1;path=/'; d.remove(); };
  d.querySelector('.reveal').onclick = () => { location.href = '/pages/tried-it'; };
  document.body.appendChild(d); }, 300);
</script>""",
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "frozen_product_page" \
                and "/products/" in self.path:
            # new30f (10 Oct): one shard ran 60+ min on 5 stores (normal: ~11 min) = a page that never answers.
            # A script that never ends freezes the page: every evaluate() Radar sends then waits for ever.
            body = body.replace("</body>", "<script>addEventListener('load', () => setTimeout(() => { for (;;) {} }, 200));</script></body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "stripped_first_home" \
                and urlparse(self.path).path in ("", "/") and not getattr(self.server, "home_served", False):
            # koskii.com (new30f + re-run, 10 Oct): one load of the homepage carried a single Shopify marker
            # (cdn.shopify.com) and Radar said 'not Shopify'; the other device's run of the same store was healthy.
            self.server.home_served = True
            body = re.sub(r"<script>window\.Shopify = .*?</script>", "", body, count=1).replace("/cdn/shop/", "/static/") \
                .replace("</head>", '<link rel="preconnect" href="https://cdn.shopify.com"></head>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "new_tab_cards" \
                and "/products/" not in self.path:
            # fashor.com (new30f) + tigc.in (new30c), 10 Oct: the product card click left the collection unchanged
            # (3 attempts, both devices). Suspected: product links open in a NEW TAB (target=_blank / window.open);
            # the shopper is on the product in that tab, Radar kept looking at the old one.
            body = body.replace("</body>", """<script>document.querySelectorAll('a[href*="/products/"]').forEach(a => a.target = '_blank');</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in ("predictive_search", "predictive_broken"):
            # journey 27: Dawn-style <predictive-search> around the header's search box, fed by /search/suggest.json.
            # predictive_broken: the element is there but its script never renders anything (Shopify's endpoint works).
            render = "" if self.mode == "predictive_broken" else (
                "box.innerHTML = ps.map(p => `<a href=\"${p.url}\" style=\"display:block;padding:6px\">${p.title}</a>`).join('');")
            body = body.replace('<form action="/search" method="get"><input name="q"></form>',
                                '<predictive-search><form action="/search" method="get"><input name="q" autocomplete="off">'
                                '</form><div class="predictive-search__results" style="position:absolute;background:#fff;z-index:30">'
                                '</div></predictive-search>', 1)
            body = body.replace("</body>", """<script>
(() => { const inp = document.querySelector('predictive-search input[name="q"]'); if (!inp) return;
  const box = document.querySelector('.predictive-search__results'); let t;
  inp.addEventListener('input', () => { clearTimeout(t); t = setTimeout(async () => {
    const r = await fetch('/search/suggest.json?q=' + encodeURIComponent(inp.value) + '&resources[type]=product');
    const ps = (((await r.json()).resources || {}).results || {}).products || [];
    """ + render + """ }, 300); }); })();
</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "collection_js_error" \
                and "/collections/" in self.path:
            # journey 14: the collection page's filter script throws (uncaught) after load
            body = body.replace("</body>", "<script>setTimeout(() => { const f = undefined; f.map(x => x); }, 50);</script></body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "layout_shift" \
                and "/products/" not in self.path:
            # journey 17: a tall hero, then an offer banner pushes the whole page down 900 px, 300 ms after load
            # (CLS well above 0.25 on both screen sizes)
            body = body.replace("<main>", '<main><div class="hero" style="height:1500px;background:#eef">Festive collection</div>', 1)
            body = body.replace("</body>", "<script>setTimeout(() => document.querySelector('main').insertAdjacentHTML("
                                "'beforebegin', '<div class=\"offer-banner\" style=\"height:900px;background:#fd0\">"
                                "Diwali offer</div>'), 300);</script></body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "sideways_scroll":
            # a promo strip wider than any screen (journey 30): the shopper can drag every page sideways
            body = body.replace("<main>", '<main><div class="promo-marquee" style="width:1700px;white-space:nowrap;'
                                'background:#fde">Festive sale: flat 20% off on everything, free shipping above Rs 999</div>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "tall_sticky_bar" \
                and "/products/" in self.path and "Mobile" in (self.headers.get("User-Agent") or ""):
            # phones only: a fixed bar over the bottom 42% of product pages, no button in it (journey 30: content
            # covered). On a desktop-sized page the same bar would sit on the buy button: Radar then FAILS the cart
            # ('<div class="sticky-info"> intercepts pointer events'), which is right, but not what this mode proves.
            body = body.replace("</body>", '<div class="sticky-info" style="position:fixed;left:0;right:0;bottom:0;'
                                'height:42vh;background:#222;color:#fff;z-index:20">Free delivery above Rs 999 · '
                                'Easy 7-day returns · Cash on delivery</div></body>')
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "empty_doc_title":
            body = re.sub(r"<title>.*?</title>", "<title></title>", body, count=1, flags=re.S)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in self.OVERLAYS:
            body = body.replace("</body>", self.OVERLAYS[self.mode] + "</body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "noisy_console":
            # a store whose third-party widgets log errors and a request that never gets an answer (7 Oct: evidence only)
            body = body.replace("</body>", '<script>console.error("Third-party chat widget failed to start"); '
                                'console.warn("Deprecated API used by theme"); '
                                'fetch("http://127.0.0.1:1/never-answers").catch(() => {});</script></body>')
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "dead_image_link" and path_now.startswith("/collections/"):
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<div class="card-wrapper" style="display:inline-block;width:220px;margin:8px;vertical-align:top">'
                f'<a class="card__media" href="/products/{m.group(1)}" style="display:block;height:200px;background:#eee">{m.group(2)}</a>'
                f'<a class="card__title" href="/products/{m.group(1)}">{m.group(3)}</a></div>'), body)
            body = body.replace("</body>", """<script>
document.querySelectorAll('a.card__media').forEach(a => ['mousedown', 'mouseup', 'click'].forEach(t =>
  a.addEventListener(t, e => e.preventDefault())));   // drag-to-scroll slider: the image never navigates
</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "slider_over_image" and path_now.startswith("/collections/"):
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<div class="card-wrapper" style="display:inline-block;width:220px;margin:8px;vertical-align:top">'
                f'<div class="card__media" style="position:relative;height:200px">'
                f'<a class="card__img" href="/products/{m.group(1)}" style="display:block;height:200px;background:#eee">{m.group(2)}</a>'
                f'<div class="swiper-wrapper" style="position:absolute;inset:0;z-index:2"></div></div>'
                f'<a class="card__title" href="/products/{m.group(1)}">{m.group(3)}</a></div>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "scroll_reveal" and path_now.startswith("/collections/"):
            # a real collection page is long (filters, more rows, footer): the grid reveals on the first scroll
            body = body.replace("</body>", """<div class="footer-spacer" style="height:2400px"></div>
<style>a.card{opacity:0;transition:opacity .2s} a.card.aos-animate{opacity:1}</style>
<script>addEventListener('scroll', () => document.querySelectorAll('a.card').forEach(a => a.classList.add('aos-animate')),
  {once: true});</script></body>""")
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "drawer_decoy_popup" and \
                (self.path.split("?")[0] == "/" or "/collections/" in self.path):
            body = body.replace("<body>", """<body><div role="dialog" aria-modal="true" class="wishlist-panel" style="position:fixed;top:0;right:0;
width:380px;height:100%;background:#fff;z-index:60;transform:translateX(100%)"><h3>My Wishlist</h3>
<p>Your lists are empty. Start adding products you love.</p><button type="button" class="panel-close" aria-label="Close">×</button></div>""", 1)
            body = body.replace("</body>", """<script>
if (!document.cookie.includes('sheet=1')) setTimeout(() => {
  const d = document.createElement('div'); d.setAttribute('role', 'dialog'); d.className = 'promo-sheet';
  d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;top:8vh;z-index:70;background:#fff;box-shadow:0 -4px 20px rgba(0,0,0,.3)';
  d.innerHTML = '<h2>NEW LAUNCH: ROSE BODY MIST</h2><button type="button" class="try" style="padding:12px">SHOP NOW</button>'
    + '<button type="button" class="sheet-close" aria-label="Close" style="position:absolute;top:8px;right:8px">×</button>';
  d.querySelector('.try').onclick = () => { location.href = '/pages/tried-it'; };
  d.querySelector('.sheet-close').onclick = () => { document.cookie = 'sheet=1;path=/'; d.remove(); };
  document.body.appendChild(d); }, 300);
</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "shadow_popup" and "/collections/" in self.path:
            body = body.replace("</body>", """<div class="shopify-block shopify-app-block"><div id="chat-widget" style="position:relative;display:block;z-index:2147483646"></div></div>
<script>
if (!document.cookie.includes('lio=1')) setTimeout(() => {
  const root = document.getElementById('chat-widget').attachShadow({mode: 'open'});
  root.innerHTML = '<div class="fixed inset-0 z-50 flex justify-center" style="position:fixed;inset:0;z-index:50;display:flex;'
    + 'align-items:center;justify-content:center;background:rgba(0,0,0,.5)"><div class="w-full relative bg-white shadow-2xl" '
    + 'style="position:relative;background:#fff;width:640px;height:360px">'
    + '<button type="button" class="absolute top-2 right-2 z-20 w-6 h-6 rounded-full" style="position:absolute;top:8px;right:8px;width:24px;height:24px">'
    + '<svg viewBox="0 0 24 24" width="24" height="24" class="lucide lucide-x"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg></button>'
    + '<h2>NEW LAUNCH: BOMB SIZE ROSEMARY HAIR SPRAY</h2><button type="button" class="try" style="padding:12px">TRY IT NOW</button></div></div>';
  root.querySelector('.try').onclick = () => { location.href = '/pages/tried-it'; };
  root.querySelector('.absolute').onclick = () => { document.cookie = 'lio=1;path=/'; root.innerHTML = ''; };
}, 300);
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "delayed_scripts":
            # a speed app delays EVERY theme script until the shopper's first interaction (mousemove, touch, wheel,
            # key): html stays 'no-js', window.Shopify is undefined, the price block is hidden by the theme's no-js CSS
            # and the search app has not rendered (baccabucci.com + bellavitaorganic.com, new30b + re-run 9 Oct)
            body = body.replace("<html>", '<html class="no-js">', 1).replace("<script>", '<script type="text/delayed">')
            body = body.replace("</head>", "<style>html.no-js .price{display:none}</style></head>", 1)
            body = body.replace("</body>", """<script>(function () { let done = false; const run = () => { if (done) return; done = true;
  document.documentElement.classList.remove('no-js');
  document.querySelectorAll('script[type="text/delayed"]').forEach(o => { const n = document.createElement('script');
    n.textContent = o.textContent; o.replaceWith(n); }); };
  ['mousemove', 'touchstart', 'wheel', 'keydown'].forEach(t => addEventListener(t, run, {once: true, passive: true})); })();
</script></body>""")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "pagespeed_gate":
            # a 'speed' snippet treats every Linux x86_64 browser as Google PageSpeed / Lighthouse and never runs the
            # theme's scripts for it: html stays 'no-js', the price stays hidden, the search app never renders
            # (bonkerscorner.com, bellavitaorganic.com, baccabucci.com, re-run 9 Oct: the quoted script says
            # 'Detect Google PageSpeed / Lighthouse testing bot (Linux x86_64 ...'). Real shoppers' phones and PCs
            # report Android ('Linux armv81'), iPhone, Win32 or MacIntel and get the full page.
            body = body.replace("<html>", '<html class="no-js">', 1).replace("<script>", '<script type="text/delayed">')
            body = body.replace("</head>", "<style>html.no-js .price{display:none}</style></head>", 1)
            body = body.replace("</body>", """<script>(function () {
  // 2. Detect Google PageSpeed / Lighthouse testing bot (Linux x86_64 desktop)
  if (navigator.platform === 'Linux x86_64') return;
  document.documentElement.classList.remove('no-js');
  document.querySelectorAll('script[type="text/delayed"]').forEach(o => { const n = document.createElement('script');
    n.textContent = o.textContent; o.replaceWith(n); }); })();
</script></body>""")
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

    SIGNER_X = ""

    def _signed_ok(self) -> bool:
        from radar.core.webbotauth import verify
        h = {k: self.headers.get(k) for k in ("Signature", "Signature-Input", "Signature-Agent")}
        return all(h.values()) and verify(h, (self.headers.get("Host") or "").lower(), type(self).SIGNER_X)

    def do_GET(self):
        Handler.SEEN_UA.add(self.headers.get("User-Agent") or "")
        if self.mode == "signed_only" and not self._signed_ok():
            return self._send(429, "Too Many Requests", "text/plain")
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        if path == "/kwikpass/kwikpass.html":      # the KwikPass frame (served on 'localhost': another origin)
            return self._send(200, """<html><body style="margin:0;background:rgba(0,0,0,.55)">
<div style="background:#fff;width:600px;margin:20vh auto;padding:20px;position:relative;border-radius:16px">
<div class="kp-close-confirm" style="display:none"><button class="close">Close</button></div>
<div class="kp-x" style="position:absolute;top:12px;right:12px;cursor:pointer;width:28px;height:28px;border:1px solid #ccc">
<svg viewBox="0 0 24 24" width="20" height="20"><path d="M18 6 6 18M6 6l12 12" stroke="#000"/></svg></div>
<h3>Log in to shop the collection.</h3><input placeholder="Enter Mobile Number*"> <input placeholder="Email*">
<button class="join">Join Us</button></div>
<script>document.querySelector('.kp-x').onclick = () => parent.postMessage('kp-close', '*');
document.querySelector('.join').onclick = () => parent.postMessage('kp-joined', '*');</script></body></html>""")
        if self.mode == "products_down" and path.startswith("/products/") and not path.endswith((".js", ".json")):
            return self._send(503, page("Something went wrong", "<h1>Something went wrong</h1>"))
        if self.mode == "server_blip" and (path == "/" or path.startswith(("/products/", "/collections/"))) \
                and not path.endswith((".js", ".json")):
            import time as _t
            st = type(self).__dict__.get("BLIP") or {}
            type(self).BLIP = st
            if path.startswith("/collections/"):
                st.setdefault("t0", _t.time())
            if "t0" in st and _t.time() - st["t0"] < 3.0:
                return self._send(503, page("Something went wrong", "<h1>Something went wrong</h1>"))
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
        if path == "/robots.txt" and self.mode == "robots_500_twice":
            type(self).ROBOTS_HITS = getattr(type(self), "ROBOTS_HITS", 0) + 1
            if type(self).ROBOTS_HITS <= 2:
                return self._send(500, "Internal Server Error", "text/plain")
        if path == "/robots.txt" and self.mode == "robots_500":
            return self._send(500, "Internal Server Error", "text/plain")
        if path == "/robots.txt" and self.mode == "robots_404":
            return self._send(404, "Not Found", "text/plain")
        if path == "/robots.txt":
            extra = "Disallow: /search\n" if self.mode == "hostile" else ""
            # like Shopify's default robots.txt: /account and /policies/ are disallowed (journeys 28 + 29 must skip them)
            acct = "" if self.mode in ("account_open", "account_broken") else "Disallow: /account\n"
            return self._send(200, "User-agent: *\nDisallow: /checkout\nDisallow: /cart\n" + acct + "Disallow: /policies/\n"
                              + extra, "text/plain")
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
        if path in INFO_PAGES:
            name = INFO_PAGES[path]
            if self.mode == "broken_policies" and path == "/pages/shipping-policy":   # a deleted page the footer still links
                return self._send(404, page("404 Not Found | Mock Store", "<h1>404 Page not found</h1>"))
            text = "" if (self.mode == "broken_policies" and path == "/pages/privacy-policy") else POLICY_TEXT
            return self._send(200, page(f"{name} | Mock Store", f"<h1>{name}</h1><p>{text}</p>"))
        if path == "/pages/contact":
            if self.mode == "broken_policies":      # a contact page with a heading and nothing else
                return self._send(200, page("Contact | Mock Store", "<h1>Contact</h1>"))
            return self._send(200, page("Contact | Mock Store", '<h1>Contact</h1><form action="/contact" method="post">'
                                        '<input type="hidden" name="form_type" value="contact"><input name="contact[name]" '
                                        'type="text"> <input name="contact[email]" type="email"> <textarea '
                                        'name="contact[body]"></textarea><button>Send</button></form>'))
        if path in ("/account/login", "/account"):
            if self.mode == "account_broken":
                return self._send(404, page("404 Not Found | Mock Store", "<h1>404 Page not found</h1>"))
            return self._send(200, page("Account | Mock Store", '<h1>Login</h1><form action="/account/login" method="post">'
                                        '<input type="email" name="customer[email]"> <input type="password" '
                                        'name="customer[password]"><button>Sign in</button></form>'))
        if path == "/pages/shop":          # brand landing page (brand_landing mode)
            return self._send(200, page("Shop | Mock Store", '<h1>Our brands</h1><a class="tile" href="/collections/home-decor" '
                                        'style="display:block;width:300px;height:200px">Home Decor</a>'), set_cart=new)
        if self.mode == "rate_limited_all" and (path.endswith(".json") or (path.startswith("/products/") and path.endswith(".js"))):
            return self._send(429, "Too Many Requests", "text/plain")
        if path == "/collections.json":
            return self._json({"collections": [dict(c, products_count=sum(p["collection"] == c["handle"] for p in PRODUCTS))
                                               for c in COLLECTIONS]})
        if path == "/products.json":
            return self._json({"products": [{"id": p["id"], "handle": p["handle"], "title": p["title"],
                                             "variants": p["variants"]} for p in PRODUCTS]})
        if path.startswith("/collections/"):
            h = path.split("/")[2]
            items = [p for p in PRODUCTS if (h == "all" and p["collection"]) or p["collection"] == h]
            if self.mode in MORE_MODES and h == "home-decor":
                items = items + EXTRA_DECOR
            if path.endswith("products.json"):
                return self._json({"products": [{"handle": p["handle"], "title": p["title"], "variants": p["variants"]} for p in items]})
            if not items:
                return self._send(404, page("Not found", "404"))
            if self.mode in MORE_MODES and h == "home-decor":
                # 3 products per page: page 1 = lamp, vase, sampler; the clay bowls come after (journey 26)
                pg = int((q.get("page") or ["1"])[0] or 1)
                if self.mode == "pagination_broken" and pg > 1:
                    return self._send(500, page("Internal Server Error | Mock Store", "<h1>500 Internal Server Error</h1>"))
                shown = items[(pg - 1) * 3: pg * 3] if self.mode in ("paginated", "pagination_broken") else items[:3]
                rest = "".join(card(p) for p in items[3:])
                extra = ""
                if self.mode in ("paginated", "pagination_broken") and pg * 3 < len(items):
                    extra = f'<nav class="pagination"><a href="/collections/home-decor?page={pg + 1}">Next page</a></nav>'
                elif self.mode == "load_more":
                    extra = (f'<template id="more">{rest}</template><button type="button" class="load-more" onclick="'
                             "this.insertAdjacentHTML('beforebegin', document.getElementById('more').innerHTML); this.remove()"
                             '">Load more</button>')
                elif self.mode == "infinite_scroll":
                    extra = (f'<template id="more">{rest}</template><div id="sentinel" style="height:3200px"></div><script>'
                             "addEventListener('scroll', () => { const t = document.getElementById('more'); if (t && "
                             "innerHeight + scrollY > document.documentElement.scrollHeight - 300) { "
                             "document.getElementById('sentinel').insertAdjacentHTML('beforebegin', t.innerHTML); t.remove(); } });"
                             "</script>")
                return self._send(200, page(f"{h.title()} Collection | Mock Store",
                                            "".join(card(p) for p in shown) + extra), set_cart=new)
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
                if self.mode == "rate_limited":
                    return self._send(429, "Too Many Requests", "text/plain")
                if self.mode == "rate_limited_once":
                    seen = type(self).__dict__.get("RL_SEEN") or {}
                    type(self).RL_SEEN = seen
                    seen[h] = seen.get(h, 0) + 1
                    if seen[h] <= 2:
                        return self._send(429, "Too Many Requests", "text/plain")
                if self.mode == "flaky_data":
                    seen = type(self).__dict__.get("DATA_SEEN") or set()
                    type(self).DATA_SEEN = seen
                    if h not in seen:
                        seen.add(h)
                        return self._send(200, "", "text/html")
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
        if path == "/search/suggest.json":
            term = (q.get("q") or [""])[0].lower()
            hits = [p for p in PRODUCTS if term and term in p["title"].lower()]
            return self._json({"resources": {"results": {"products": [
                {"title": p["title"], "handle": p["handle"], "url": f"/products/{p['handle']}"} for p in hits]}}})
        if path == "/search":
            term = (q.get("q") or [""])[0].lower()
            if self.mode in ("search_app_scroll", "search_app_never"):
                # ptron.in / kushals.com (new30c, 10 Oct): Shopify counts the results in the title, a search app owns the
                # results area and fills it on the first scroll (ptron desktop) or never (ptron + kushals mobile)
                hits = [p for p in PRODUCTS if term and term in p["title"].lower()]
                found = "".join(card(p) for p in hits)
                fill = ("addEventListener('scroll', () => { const a = document.getElementById('app-results'); "
                        "if (!a.dataset.done) { a.dataset.done = 1; a.innerHTML = document.getElementById('app-found').innerHTML; } "
                        "}, {passive: true});") if self.mode == "search_app_scroll" else ""
                return self._send(200, page(f'Search: {len(hits) * 171} results found for "{term}" | Mock Store',
                                            f'<div id="app-results" style="min-height:1400px;background:#f0f0f0"></div>'
                                            f'<template id="app-found">{found}</template><script>{fill}</script>'))
            hits = [p for p in PRODUCTS if term and term in p["title"].lower()]
            if self.mode == "search_misses" and term == "ceramic":
                hits = [p for p in PRODUCTS if p["handle"] == "wooden-spoon-set"]
            if self.mode == "search_error_empty" and not hits:      # journey 27: a search that finds nothing crashes
                return self._send(500, page("Internal Server Error | Mock Store", "<h1>500 Internal Server Error</h1>"))
            found = "".join(card(p) for p in hits) or "<p>No results</p>"
            if self.mode in ("delayed_scripts", "pagespeed_gate"):   # the search app renders from a (delayed) theme script
                return self._send(200, page("Custom Search | Mock Store", f'<div id="app-results"></div>'
                                            f'<template id="app-found">{found}</template><script>'
                                            f'document.getElementById("app-results").innerHTML = '
                                            f'document.getElementById("app-found").innerHTML;</script>'))
            if self.mode in ("search_app", "search_app_popular"):
                # a search app renders results ~2.5 s after the page loads, from its own API (bonkerscorner, baccabucci:
                # empty until then; bellavitaorganic: 'popular products' placeholders until then)
                popular = "".join(card(p) for p in PRODUCTS if p not in hits and float(p["price"]) > 0) \
                    if self.mode == "search_app_popular" else ""
                return self._send(200, page("Custom Search | Mock Store", f'<div id="app-results">{popular}</div>'
                                            f'<template id="app-found">{found}</template><script>setTimeout(() => '
                                            f'document.getElementById("app-results").innerHTML = '
                                            f'document.getElementById("app-found").innerHTML, 2500);</script>'))
            return self._send(200, page(f"Search: {term} | Mock Store", found))
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
        if self.mode == "sticky_buy_only":
            sticky = buy.replace('id="product-form-template__main"', 'id="sticky-form"')
            body += ('<div style="height:2400px"></div><div id="sticky-atc" style="display:none;position:fixed;left:0;right:0;'
                     'bottom:0;background:#fff;padding:10px;z-index:20">' + sticky + '</div>'
                     '<style>#product-form-template__main button{display:none}</style>'
                     '<script>addEventListener("scroll", () => { if (scrollY > 300) document.getElementById("sticky-atc")'
                     '.style.display = "block"; });</script>')
        if self.mode == "div_buy_control":
            others = [x for x in PRODUCTS if x["handle"] != p["handle"] and float(x["price"]) > 0][:3]
            recs = "".join(f'<div class="rec-card"><a href="/products/{x["handle"]}">{x["title"]}</a>'
                           f'<button type="submit" class="_gai-atc-btn" onclick="addToCart({x["variants"][0]["id"]})">Add to Bag</button></div>'
                           for x in others)
            body = recs + body.replace(buy, f'<div class="pdp-varient-form-main"><div class="pdp-addtobag-btn active" '
                                            f'data-product-handle="{p["handle"]}" onclick="addToCart({vid})" style="cursor:pointer;'
                                            f'padding:10px;background:#222;color:#fff;width:200px"><span class="atc-text">ADD TO BAG</span></div></div>')
        if self.mode == "preselected_variant" and len(p["variants"]) > 1:
            pre = p["variants"][1]
            sizes = '<div class="variant-picker" role="radiogroup">' + "".join(
                f'<button type="button" role="radio" class="size-btn{" active" if v is pre else ""}" '
                f'aria-checked="{"true" if v is pre else "false"}">{v["option1"]}</button>' for v in p["variants"]) + '</div>'
            body = re.sub(r'<div class="price">₹[^<]*</div>', f'<div class="price">₹{float(pre["price"]):,.2f}</div>' + sizes, body, count=1)
            body = body.replace(buy, f'<div class="pdp-addtobag-btn" data-product-handle="{p["handle"]}" '
                                     f'onclick="addToCart({pre["id"]})" style="cursor:pointer;padding:10px;background:#222;color:#fff;'
                                     f'width:200px">ADD TO BAG</div>')
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
                          r'<span class="product-sticky-form__price">You Pay: \1</span></product-sticky-form>'
                          # on a phone-sized screen the theme shows the bar (7 Oct: mobile is tested by default)
                          '<script>if (matchMedia("(max-width: 1007px)").matches) '
                          'document.querySelector("product-sticky-form").hidden = false;</script>', body, count=1)
        if self.mode == "pincode_gate" and available:
            # bombaysweetshop.com (new30d held-out, 10 Oct): variant selected, but the store's own buy button stays
            # disabled and says 'PLEASE ENTER YOUR PINCODE TO CHECK AVAILABILITY' until a delivery pincode is checked
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="submit" name="add" disabled>PLEASE ENTER YOUR PINCODE TO CHECK AVAILABILITY</button>')
            body += ('<div class="pincode-check"><input type="text" placeholder="ENTER YOUR PINCODE" maxlength="6">'
                     '<button type="button" class="pincode-btn">CHECK</button></div>')
        if self.mode == "disabled_dup_button" and available:
            # littleboxindia.com mobile (new30c + loop cycle 6): the product form holds TWO buy buttons; the one Radar
            # reads is disabled while an enabled ADD TO CART for the same product is on screen. Radar must name both.
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="submit" name="add" style="display:none">Add to cart</button>'
                                '<button type="submit" class="btn-mobile-atc" disabled>Add to cart</button>')
            body += ('<div class="sticky-bar" style="position:fixed;left:0;right:0;bottom:0;background:#fff;padding:8px;z-index:20">'
                     '<button type="submit" form="product-form-template__main" class="sticky-atc">ADD TO CART</button></div>')
        if self.mode == "disabled_buy_now_only" and available:
            # crossbeats.com-like: the form's native submit is hidden, the visible ADD TO CART is disabled and the only
            # enabled control is 'Buy it now' (checkout) -> Radar must NOT click it; the failure names it first
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="submit" name="add" style="display:none">Add to cart</button>'
                                '<button type="button" class="cf-checkout" disabled>ADD TO CART</button>'
                                '<button type="button" class="shopify-payment-button__button" '
                                'onclick="location.href=\'/checkouts/c/mock\'">Buy it now</button>')
        if self.mode == "buy_now_first" and available:
            # an enabled 'Buy it now' (checkout) comes BEFORE the add-to-cart in the product form -> click add to cart
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="button" class="shopify-payment-button__button" '
                                'onclick="location.href=\'/checkouts/c/mock\'">Buy it now</button>'
                                '<button type="submit" name="add">Add to cart</button>')
        if self.mode == "healer_disabled_sticky" and available:
            # littleboxindia.com mobile (new30c/d/e): the form's own button is not shown, the healer finds a DISABLED
            # 'Add to cart' near the title, and an ENABLED sticky ADD TO CART (no form attribute) is on screen.
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="submit" name="add" style="display:none">Add to cart</button>')
            body = body.replace('</h1>', '</h1><button type="button" class="pdp-atc" disabled>Add to cart</button>', 1)
            body += ('<div class="sticky-bar" style="position:fixed;left:0;right:0;bottom:0;background:#fff;padding:8px;z-index:20">'
                     '<button type="button" class="sticky-atc" onclick="document.getElementById(\'product-form-template__main\')'
                     '.requestSubmit()">ADD TO CART</button></div>')
        if self.mode == "free_gift":
            script += "<script>window.GIFT_MODE = true;</script>"
        if p.get("redirect"):
            script += f"<script>setTimeout(() => location.replace('{p['redirect']}'), 150)</script>"
        return page(f"{p['title']} | Mock Store", body, f'<script type="application/ld+json">{ld}</script>{script}')

    def do_POST(self):
        if self.mode == "signed_only" and not self._signed_ok():
            return self._send(429, "Too Many Requests", "text/plain")
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
