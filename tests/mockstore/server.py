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
  headless_alias   product links are /products/sku-<id> (a headless storefront's own URL, palmonas.com 14 Oct); their
                   .js answers the HTML page; the page's canonical names the Shopify handle -> Radar reads the product
                   data via that handle, journey PASSES on the first attempt
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
  blank_not_shopify  gozero.in (cycle 34): no robots.txt (404), the homepage answers 200 with an EMPTY title, an empty
                   <div id=root> and no Shopify marker -> verdict UNSUPPORTED with what the homepage showed (v0.19:
                   'error', shown as 'Radar crashed')
  pack_of_two      theformalclub.in (cycle 34): the vase is a 'pack of 2' with ONE option whose values join both items'
                   sizes ('S / 38 / M / 40'); the page shows two pickers labelled 'S' / 'M' and fills the form's id only
                   when both are picked -> Radar clicks 'S' in each picker and PASSES (v0.19: 'variant selected like a
                   shopper ... after choosing nothing (no matching picker)')
  stale_catalog    bombaytrooper.com (cycle 34): /products.json lists the vase in stock, its /products/<h>.js and page
                   say sold out -> the product test notes it (info) and tests the next in-stock catalog product, PASSES
                   (v0.19: 'an in-stock variant exists ... got none available')
  stale_catalog_all  bombaytrooper.com (13 Oct, dashboard): as stale_catalog, but EVERY product is sold out in its .js and on
                   its page, so there is no in-stock product to test instead -> the product test judges the sold-out page
                   (name, price, picture, 'sold out' told), tags it with a 'sold_out_product' step and PASSES (before:
                   'an in-stock variant exists ... got none available', confirmed_fail)
  app_proxy_page   arakucoffee.in (cycle 34): a homepage section links to /tools/product-finder/match-me, an app-proxy
                   quiz page with no theme layout whose content a web component draws in a CLOSED shadow root (body text
                   empty, no <img>) -> smoke.more_links PASSES on what the browser painted (v0.19: 'page not blank: got
                   0 chars, 0 images' -> confirmed_fail)
  slow_page        bluenectar.co.in (cycle 34, desktop): the menu page /pages/about answers after SLOW_PAGE_SECS (4 s;
                   tests use a 3 s navigation timeout) -> opened again with twice the wait: smoke.nav_links PASSES with a
                   'slow page' WARNING (v0.19: 'Timeout exceeded' -> broken navigation page, confirmed_fail)
  robots_404       /robots.txt answers HTTP 404 (no file) -> everything allowed, store tested normally
  (Radar's own network dropping mid-run is not a mode: tests shut the store down from the progress hook and point
   the connectivity probe at a dead port (Radar offline) or at a live server (Radar online, store died).)
  empty_doc_title  every page renders normally but its <title> is empty (hairoriginals.com, new30e held-out)
                   -> pages load (no 'down'); the SEO test 'title' warns
  annual_return_pdf  the footer's first link is 'Annual Return FY 2024-25', a PDF under /cdn/ (giva.co): a company-law
                   filing, not the returns policy -> never picked as an info page, info.policy_pages PASSES
  grid_on_scroll   collection pages show a 1600 px banner and an EMPTY grid that an app fills only when the grid
                   scrolls into view (veirdo.in, new30h: '144 items', Radar saw 0 products) -> collection + journey PASS
  lazy_gallery     the product page's main picture is a lazy gallery slide: an empty 300x300 <img> whose data-src is
                   swapped in 2 s after load (soultree.in, new30h: image on screen, Radar said 'none loaded') -> PASSES
  other_market     every page shows prices converted to USD (Shopify Markets for a US visitor: Shopify.currency
                   {active: USD, rate: 0.012}, '$15.59' where the catalog says ₹1,299; theindianethnicco.com, saltattire.com
                   new30h, tilfi.com new30g) -> product / journey / cart tests are BLOCKED 'location (US runner)', not DOWN
  other_market_norate  same dollar prices, but the page exposes no Shopify.currency (theindianethnicco.com $62.91 and
                   saltattire.com $24.23 kept failing on the rate test alone, cycle 28 re-run) -> BLOCKED 'location'
  cart_dawn_rows   Dawn's real cart rows: hidden 'Decrease quantity for <title>' text inside the quantity box, the price in
                   its own table cell, a discount code (regular price struck, sale price 10% lower) -> journey 23's
                   cart line price PASSES ('discounted in the cart'), not 'line shows no price' (12 theme demos, cycle 28)
  other_market_offer  dollar prices with no Shopify.currency, plus an offer block in ₹ ('Flat ₹250 off, min. order ₹2,500')
                   lower on the product page (theindianethnicco.com, cycle 28 round 2 still DOWN) -> BLOCKED 'location'
  marketplace_only product pages show no price and no cart, only 'Buy on Amazon / Flipkart' links (antesports, beyondsnack)
                   -> product + journey BLOCKED "sells only on marketplaces", never DOWN
  href_space       homepage + collection cards link to '/products/<handle> ' (trailing space in the href; the browser
                   opens /products/<handle>; thechumbakwala.com new30n 14 Oct) -> journey PASSES (was DOWN 'opened the
                   product that was clicked: expected /products/x , got /products/x')
  price_on_request product pages hide the price on purpose: 'Price on Request' + 'Enquire' / 'Chat with us', no cart
                   (houseofquadri.com, artoftimeindia.com, new30n 14 Oct); the spoon page shows no price and only the
                   bar 'We are no longer accepting online orders' (banjarakasuti.com) -> BLOCKED 'not tested', never DOWN
  cart_qty_broken  the cart page's + / − buttons and quantity box do nothing (theme script missing) -> cart.edit_and_checkout
                   WARNS (strict: FAILS) at change_quantity "cart quantity after pressing +: expected 2"
  cart_remove_broken  the cart page's Remove link reloads the cart without removing the line -> WARNS (strict: FAILS) at
                   remove_item
  checkout_big_script  the checkout page carries ~100 kB of inline JSON <script> before the order summary (Shopify's
                   checkout, 12 theme demos, run 38086965951) -> 'order summary names the product' PASSES (it warned on
                   all 24 demo runs while Radar read only the first 40 000 chars of body.textContent)
  checkout_broken  GET /checkout answers HTTP 500 -> WARNS (strict: FAILS) at checkout_opens (checkout never filled)
  cart_drawer      the drawer that opens after adding lists every cart line (name, price, quantity box) and a Subtotal,
                   read from /cart.js (journey 18) -> cart.drawer_and_page PASSES: drawer = cart page = /cart.js
  cart_drawer_stale  same drawer, rendered from a stale cart: quantity 2 and subtotal ₹2,598 while /cart.js and the cart
                   page hold 1 vase at ₹1,299 -> cart.drawer_and_page WARNS (strict: FAILS) at drawer_matches_cart
  cart_no_drawer   adding sends the shopper straight to the cart page (no drawer) -> 'no_drawer' information, the cart
                   page is compared with /cart.js, PASSES (healthy: the drawer names the product only, no quantity or
                   subtotal; those are not judged)
  no_title         product pages show no product name at all (snitch.co.in after its move)
                   -> product tests FAIL at shows_title_price_image with "none"
  (every mode)     footer links to an 'Annual Return FY 2024-25' PDF (downloads; giva.co), Cloudflare's /cdn-cgi/l/email-protection
                   'Email' link (beminimalist.co), /pages/shipping-policy, /policies/refund-policy, /pages/privacy-policy,
                   /pages/terms-of-service, /pages/contact (+ an Instagram link) and a header account link
                   /account/login; robots.txt disallows /policies/ and /account (some stores do: Radar must skip them)
                   -> journey 28 opens shipping, privacy, terms, contact (refund skipped: robots.txt); no account test
  healthy (+)      an announcement bar links /pages/offers; a homepage banner links /pages/our-story and /collections/kitchen
                   (already in the menu) -> journey 15 opens offers + our-story (only in healthy and broken_section_link:
                   the navigation modes must not get extra links)
  broken_section_link  /pages/our-story answers 404, /pages/offers redirects to the homepage, a banner link's href is
                   '/https://www.mockstore.in/collections/sale' (beyondsnack.in) (journey 15) -> smoke.more_links FAILS naming
                   them, the last as a malformed address
  broken_policies  the footer's shipping page answers 404, the privacy page is a heading with no text, the contact page
                   has no form, email or phone (journey 28) -> info.policy_pages FAILS naming shipping; privacy and
                   contact WARN
  account_open     robots.txt allows /account; /account/login shows an email + password form (journey 29)
                   -> info.account_page PASSES, nothing typed
  account_broken   same, but /account/login answers 404 -> info.account_page FAILS
  hamburger_menu   phones only: the header menu hides behind a ☰ button (aria-label Menu) opening a drawer (journey 16)
                   -> smoke.mobile_menu PASSES 'tapped the menu button'; desktop / healthy -> not judged / links shown
  hamburger_broken same, but the button's script throws and the drawer never opens -> smoke.mobile_menu WARNS
  late_cards       collection grids are rendered by a script 1.5 s after the page loads -> the collection test and
                   catalog.more wait for the cards and PASS (they failed at once on the old code)
  search_icon_only the header search form is hidden behind a bare icon link (title="Search", svg icon; no aria-label, not a
                   button, not a /search link) -> Radar opens it and TYPES the search like a shopper (old opener: 'no visible
                   search box; opened /search')
  script_heavy     every page loads 26 app scripts from 26 different third-party hosts (journey 31) -> 'scripts the page
                   loads' WARNS on home, collection and product pages; healthy -> passes with the counts
  collection_js_error  collection pages throw an uncaught TypeError after load (journey 14) -> collection test WARNS
                   'uncaught JavaScript errors', never fails
  layout_shift     home + collection pages: a tall hero, then an offer banner pushes the page down 900 px 300 ms after
                   load (journey 17) -> 'Core Web Vitals' WARNS with the CLS value; the store stays healthy
  sideways_scroll  every page carries a 1700 px promo strip (journey 30) -> home, collection and product pages WARN
                   'page scrolls sideways' naming div.promo-marquee; the store stays healthy
  paginated        'Home Decor' also holds 10 clay bowls (its products.json too), 3 products per page with a 'Next page'
                   link (journey 26) -> catalog.more PASSES: page 2 shows new products. healthy (all fit on one page)
                   -> 'not judged'
  pagination_broken  same, but ?page=2 answers HTTP 500 -> catalog.more FAILS
  load_more        first 3 shown, a 'Load more' button appends the bowls -> PASSES after the click
  infinite_scroll  first 3 shown, the bowls are appended when the shopper reaches the bottom -> PASSES after scrolling
  more_hidden      first 3 shown, no page 2, no button, no scroll loading, while the data has 10 more in stock -> WARNS
  search_error_empty  /search answers HTTP 500 when nothing matches (journey 27) -> search.no_results FAILS
  predictive_search  the header search box sits in a Dawn-style <predictive-search> fed by /search/suggest.json
                   (journey 27) -> search.suggestions PASSES with the suggested product; healthy (no such element)
                   -> 'not judged: no search-as-you-type'
  predictive_broken  same element, but its script never renders suggestions while Shopify's endpoint finds the word
                   -> search.suggestions WARNS, never fails
  tall_sticky_bar  on phones, product pages carry a fixed info bar over the bottom 42% of the screen (journey 30)
                   -> product pages WARN 'screen covered by fixed bars' naming div.sticky-info; the store stays healthy
  healthy (+)      the vase's page has Dawn-style size pills (radio + label) whose script updates the form's variant id,
                   the price and ?variant= (journey 22) -> product.variant_switch PASSES (Small → Large, ₹1,299 → ₹1,399)
  variant_pill_covered  healthy pills under a transparent swatch layer that forwards clicks (dotandkey.com, 12 Oct):
                   Playwright's click on the pill times out ('intercepts pointer events') -> Radar clicks the point like
                   a shopper, variant_switch PASSES (was 'option could not be clicked' warning)
  variant_switch_broken  same pills, but the theme's variant script is missing: clicking Large changes nothing (id, price
                   and address stay Small's) -> product.variant_switch WARNS (strict: FAILS) at variant_switch
  variant_sold_out the vase also has a sold-out 'Extra Large' size (data + page); picking it disables the buy button and
                   makes it say 'Sold out' -> variant_switch and sold_out_blocks_buy PASS
  variant_sold_out_buyable  same, but the buy button stays enabled 'Add to cart' on the sold-out size -> WARNS (strict:
                   FAILS) at sold_out_blocks_buy (the buy button is never clicked)
  variant_no_url_push  (journey 22 round 2: antinorm.co, veirdo.in) an app-built buy box with no form/id field, pill
                   labels carry their price, the theme never pushes ?variant= -> variant_switch PASSES on the pill the
                   page marks as chosen (it used to warn: the address kept the ?variant= Radar opened it with)
  variant_price_late  (theme-taste-demo) the form's id changes at once, the price 1.5 s later -> PASSES
  variant_form_late  section rendering swaps in a NEW product form with the picked id 4 s after the click -> PASSES
  variant_id_on_submit  price, pill and ?variant= change, the id field is set only on add to cart -> 'not judged' (info)
  card_price_stale collection cards show prices (as in healthy: the vase on sale, MRP ₹1,599 struck, 'Save' badge), but
                   the vase's card shows an OLD sale price ₹999 from a stale cached section while its product page and
                   the cart line say ₹1,299 (journey 23) -> cart.price_consistency WARNS (strict: FAILS) at
                   card_matches_product_page; healthy -> card ₹1,299 = page ₹1,299 = cart ₹1,299, PASSES
  soldout_buyable  the sold-out lamp's page shows an ENABLED 'Add to cart' in a product form for its variant and no
                   'sold out' words (journey 24) -> product.sold_out WARNS (strict: FAILS) at shows_sold_out; healthy
                   (disabled 'Sold out' button) PASSES
  soldout_oversell  /cart/add.js ADDS the sold-out lamp (Shopify's 'continue selling when out of stock') -> product.sold_out
                   PASSES with an 'oversell_allowed' note; Radar removes it from its own cart again
  (every mode)     /cart/add.js refuses a sold-out variant with HTTP 422 'already sold out', like Shopify; /cart/change.js
                   also takes {id: <variant id>} instead of a line number
  facets           collection pages carry Dawn-like storefront filters (journey 25): an 'Availability' group (In stock /
                   Out of stock) in a GET form that applies itself on change (fetch + grid swap + pushState), behind a
                   'Filter' button on phones; ?filter.v.availability=1 lists only in-stock products -> catalog.filter
                   PASSES by clicking 'In stock'; healthy (no filters) -> 'this collection offers no filters' (info)
  facets_sort      same, plus a sort_by select inside the filter form, and Shopify's default robots.txt (no sort_by) ->
                   the theme's request would carry sort_by, so Radar opens ?filter.v.availability=1 directly; sorting
                   'not checked: robots.txt disallows'
  facets_sort_allowed  same, but robots.txt allows sort_by and ?sort_by=price-ascending sorts by price -> Radar clicks
                   the filter and chooses 'Price, low to high'; both PASS
  filter_broken    the filters are shown, but ?filter.v.availability=1 still lists everything, the sold-out lamp too
                   -> catalog.filter WARNS (strict: FAILS) 'not matching: sold-out-lamp' (its .js and page: sold out)
  filter_stock_differs  antinorm.co / beminimalist.co (11 Oct): 'In stock' lists the lamp, which the collection's
                   products.json calls sold out, while Shopify's /products/<h>.js and its product page sell it (inventory
                   policy 'continue' / untracked stock) -> catalog.filter PASSES: the storefront's own data decides
  filter_big_collection  maspar.com (11 Oct): only a 'Size' filter; the collection's products.json (Radar reads 250 of a
                   bigger collection) does not carry the vase, the one product ?filter.v.option.size=Small lists ->
                   PASSES (1 listed, the data cannot tell), never '0 products listed with the filter on'
  filter_empty     the 'Availability' filter says 'In stock (2)' but ?filter.v.availability=1 lists nothing ('No products
                   found') -> catalog.filter WARNS (strict: FAILS), naming the store's own count
  filter_app       Dawn's filter form is on the page, but a filter app (script /static/boost-sd-filter.js) owns the grid
                   and empties it on addresses with filter.* -> 'not judged: filter app Boost' (info), not a warning
  contents_cards   every product card's link is display:contents around image + name (valliyan.com, Baseline theme):
                   the <a> has no box of its own -> collection + journey PASS (was 'products listed: 0', 'N not visible')
  zero_box_cards   valliyan.com re-test (cycle 34 fix still failed): the card's display:contents link wraps a block whose
                   image and name FLOAT, so that block has zero height too -> collection + journey PASS (v0.19 cycle 34:
                   'products listed: 0', '15 links: 15 not visible')
  contents_reveal_cards  valliyan.com re-test 2 (run 38326679916): the card's display:contents link carries a
                   scroll-reveal opacity:0 that never lifts (no box to observe); its image and name still paint -> the
                   journey PASSES (v0.19 13 Oct: grid counted, then '15 links: 15 not visible')
  freebie_type     suta.in (full 36, 13 Oct): the catalog's first product is 'Free Fabric' (Rs 1,000, product type
                   'Freebies', also in Home Decor), whose page has no product image -> it is skipped as a gift SKU and a
                   real product is tested: product suite PASSES (v0.19: 'main product image loaded: got none', degraded)
  hidden_link_shown_cards  the card's link is visibility:hidden while its image + name override it (visibility:visible),
                   so the card paints and takes the click -> collection + journey PASS (v0.19 13 Oct: the link's own style
                   decided: 'N not visible'). Visibility is judged by what paints INSIDE the link.
  stretched_link_cards  valliyan.com (Baseline; first-refused evidence 13 Oct: 'a.increase-target{inline,op 1,visible,
                   0x0} > span.visually-hidden'): each card holds an EMPTY inline link whose ::before is position:absolute
                   over the card -> collection + journey PASS (v0.19: '15 links: 15 not visible')
  late_grid        giva.co phone (full 36, 13 Oct): collection pages say '12 products' at once but an app draws the
                   product cards LATE_GRID_SECS (8 s) after load, past Radar's 6 s grid wait -> collection + journey PASS
                   ('rendered 8.0s after the page loaded'); v0.19: 'products listed: 0' on the first attempt (flaky)
  veiled_cards     verdict rule (13 Oct): every product card sits under a transparent fixed layer with NO control to
                   close it -> Radar cannot find a card it is sure a shopper can tap: journey NOT JUDGED (case 'blocked',
                   store 'blocked'), never DOWN (v0.19: 'product card a shopper can click: ... covered by div.veil' = DOWN)
  pdp_no_image     verdict rule: the vase's page has no image at all (suta.in 'Free Fabric') -> product.pdp NOT JUDGED
                   with what was seen; store stays healthy (v0.19: 'main product image loaded: got none' = degraded)
  pdp_broken_image the vase's 300x300 image answers 404 -> positive evidence: product.pdp FAILS 'a large image failed
                   to load' (the rule keeps real breakage a failure)
  refused_mid_run  bombayshirts.com phone (new30l, 13 Oct): discovery sees the store (robots, homepage, data), then the
                   bot wall answers HTTP 403 to every page -> run verdict 'blocked' with a note (v0.19: DOWN, 8 failed checks)
  late_redirect_product  the vase's page renders, then a script sends the shopper to / a moment later (foxtale.in
                   14 Oct) -> loads WARNS 'redirects to / shortly after loading', the next catalog product is tested
  sibling_variant_links  the vase's <h1> shares a box with big 'CHOOSE VARIANTS' cards linking to sibling
                   products (beminimalist.co 14 Oct) -> its name is still found, the journey PASSES first try
  market_path      Shopify Markets: / sends the shopper to /en-us/, links carry the prefix, prices are +20% there and so is
                   /en-us/products/<h>.js; the default /products/<h>.js says the base price (amayrajewellery.com 14 Oct)
                   -> Radar reads the data under the page's market path, journey PASSES first try
  wishlist_404     the header links /pages/wishlist, which answers HTTP 404 with a wishlist app drawn into it
                   (countryflora / kotart / swornyfashion, new30p) -> smoke.nav_links PASSES, the app page noted
  plain_size_buttons  the vase has Size x Colour; colour swatch in a 'swatches' box, sizes as plain buttons in a
                   'pdp-sizes' box, nothing chosen on load (form id empty, 'Choose your size' disabled) (sntvl.com
                   new30r) -> Radar taps the colour AND the size, product PASSES
  hidden_price     product pages show no price near the name and only a disabled 'Unavailable' button (the data has a
                   placeholder price; somuchliving / tastycrafts 15 Oct) -> product BLOCKED 'not tested', never DOWN
  prepaid_price    product pages show the price minus 5% under 'EXTRA 5% OFF ON ALL PREPAID ORDERS' (rasayanam.in)
                   -> shows_title_price_image WARNS (Anmol, 15 Oct), never fails
  customer_auth_link  the header links '/customer_authentication/redirect' (Shopify new customer accounts), which answers
                   404 to a robot (krkjewels.com new30t) -> not a menu page, smoke.nav_links PASSES
  hidden_form_app_button  the product's cart form is hidden and holds no button; the buy control is an app's element
                   outside it (shopkara.store, nubronicforte.com) -> found, the cart check adds the right product, PASSES
  per_unit_price   the vase page shows '₹649.50 / piece' + 'Pack of 2 pieces' for the ₹1,299 variant (ayamveda.com)
                   -> price shown PASSES (per-unit evidence)
  no_product_images  product pages show no picture and /products/<h>.js lists no image (kalamkitaab, lucentpublication)
                   -> shows_title_price_image WARNS 'no image in the store's own data', never not-judged
  span_add_to_cart  hidden, empty cart form + a theme <span class="custom-add-to-cart-btn">Add to Cart</span> far from
                   the name (costosoitaliano.com) -> used, the cart check adds the right product, PASSES
  disabled_form_enabled_bar  the form's own button is disabled, an enabled sticky 'Add to cart' adds the product
                   (arabianaroma.in) -> variant_ready + buy PASS
  gift_only_product  hidden, empty cart form and no buy control anywhere (a gift the store auto-adds; nubronicforte
                   'Performance Kit') -> product BLOCKED 'not tested: offers no way to buy it'
  cod_form_only    hidden buttonless cart form + only an 'Order Now - Cash on Delivery' order-form app button
                   (shopkara.store) -> product BLOCKED 'not tested: sold only through an order-form app'
  grid_names_dead  grid_without_links, but tapping a card does nothing -> NOT JUDGED with what was tapped
  grid_without_links  giva.co desktop (verdict-rule sanity, 13 Oct): the collection page shows its product cards (names,
                   prices) but as script-driven tiles with NO <a href="/products/..."> -> 'products listed' NOT JUDGED
                   (the page shows the products by name), never a failure
  grid_empty_data_full  the collection page shows no products at all while its products.json lists them -> positive
                   evidence: 'products listed' FAILS naming the data
  offsite_hang     nonasties.in phone (new30k): footer links /map and /panjim answer 302 to another site (Google Maps)
                   that never finishes loading for the phone browser -> smoke.more_links PASSES, naming the other site
                   (v0.19: 'page did not load ... nor within 60 s' -> links that fail to open, confirmed_fail)
  landing_collection  /collections/home-decor (the menu's first collection) is a landing page: hero, hidden mega-menu
                   product links, a 'Learn more' tile to /collections/kitchen (quboworld.com 'Smart Entrance') -> the
                   collection test WARNS 'landing page', the journey goes on to the sub-collection and PASSES
  phone_brand_landing  brand_landing, but on phones the top menu sits behind a ☰ drawer (subko.coffee phone) -> the
                   journey opens the menu, hops to /pages/shop, PASSES
  country_modal    a 'Shopping from Outside India? International / Stay Here' modal rendered inside <header> covers the
                   page until answered (teatrunk.in) -> Radar clicks 'Stay Here' (never 'International'), PASSES
  country_modal_div_stay  country_modal, but 'Stay Here' is a plain <div> with a click handler and the backdrop is a
                   sibling of the box (teatrunk.in re-test) -> Radar clicks 'Stay Here', PASSES
  country_modal_stay_on  'Visiting from United States (USD $)?' with 'Shop on US Site' / 'Stay on Mock IN Site' and an
                   unlabelled icon x, inside <header> (vedix.com) -> Radar clicks 'Stay on Mock IN Site' (never the US
                   site), PASSES
  buy_now_ok       (journey 33) the product form carries Shopify's dynamic checkout slot (div.shopify-payment-button); a
                   script renders 'Buy it now' into it 600 ms after load; the click opens /checkouts/cn/buynow?variant=<id>,
                   a checkout (contact + delivery fields) whose order summary names the product -> cart.buy_now PASSES
                   (nothing typed); healthy (no buy-now button) -> 'no buy-now button on this store' (info)
  buy_now_dead     same button, but the dynamic-checkout script never wires it: the click does nothing -> cart.buy_now
                   WARNS (strict: FAILS) at buy_now_opens_checkout 'nothing happened'
  buy_now_error    same button, the checkout it opens answers HTTP 500 'There was a problem with our checkout' -> WARNS
  tp_checkout      (journey 35) the cart page loads a GoKwik-style app (/static/gokwik-checkout.js) that takes over the
                   checkout button: a full-screen iframe from another origin (localhost/gokwik/checkout) shows the order
                   summary and an 'Enter mobile number' (tel) box 800 ms later -> cart.edit_and_checkout checkout_opens
                   PASSES naming GoKwik; no number typed, no OTP asked
  tp_checkout_popup  same with a Shopflo-style app opening its checkout in a POPUP window -> PASSES naming Shopflo
  tp_checkout_blank  the GoKwik iframe opens but stays blank -> checkout_opens WARNS (strict: FAILS) 'blank'
  tp_checkout_script_404  the app script answers HTTP 404, so the checkout button (type=button, owned by the app) does
                   nothing -> checkout_opens WARNS naming the 404
  (every mode)     robots.txt carries Shopify's default collection lines: /collections/*sort_by*,
                   /collections/*filter*&*filter*, /collections/*+* (facets_sort_allowed: without the sort_by lines)

Every product page also carries a quick-add product card BEFORE the main buy button in the DOM
and a cart drawer that opens over the page after adding (both copied from moxiebeauty.in's
theme, where Radar originally clicked the card's button and added the wrong product).

Run by hand to look at it:  python3 -m tests.mockstore.server healthy 8765
"""
from __future__ import annotations

import json
from radar.discovery.shopify_data import rupees
import re
import sys
import threading
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LATE_REDIRECT_MS = 1200
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
FREEBIE = {"id": 9, "handle": "free-fabric", "title": "Free Fabric", "price": "1000.00", "product_type": "Freebies",
           "variants": [{"id": 901, "available": True, "price": "1000.00"}], "collection": "home-decor"}   # freebie_type
COLLECTIONS = [{"handle": "home-decor", "title": "Home Decor"}, {"handle": "kitchen", "title": "Kitchen"}]
# journey 26: in the pagination modes 'Home Decor' also lists 10 clay bowls (collection pages + its products.json only)
EXTRA_DECOR = [{"id": 60 + i, "handle": f"clay-bowl-{i}", "title": f"Clay Bowl No. {i}", "price": "599.00",
                "variants": [{"id": 600 + i, "available": True, "price": "599.00"}], "collection": "home-decor"}
               for i in range(1, 11)]
MORE_MODES = ("paginated", "pagination_broken", "load_more", "infinite_scroll", "more_hidden")
J15_MODES = ("healthy", "broken_section_link")      # announcement bar + homepage banner (journey 15)
# journey 22: size pills on multi-variant product pages (only in these modes: other modes test their own pickers)
VARIANT_MODES = ("healthy", "variant_switch_broken", "variant_sold_out", "variant_sold_out_buyable",
                 "variant_no_url_push", "variant_price_late", "variant_form_late", "variant_id_on_submit",
                 "variant_pill_covered")
# theformalclub.in (cycle 34): a 'pack of 2 shirts' product with ONE option whose values join both shirts' sizes
# ('S / 38 / M / 40'); the page shows two size pickers labelled 'S', 'M' (one per shirt) and sets the variant id only
# when both are picked
PACK_SIZES = (("S", "S / 38"), ("M", "M / 40"))
PACK_VARIANTS = [{"id": 210 + 2 * i + j, "available": True, "price": "1299.00", "option1": f"{a[1]} / {b[1]}",
                  "title": f"{a[1]} / {b[1]}"} for i, a in enumerate(PACK_SIZES) for j, b in enumerate(PACK_SIZES)]
OFFSITE_HANG_SECS = 7.0   # offsite_hang: the other site answers after this long (> 2x the tests' 2 s timeout)
LATE_GRID_SECS = 8.0      # late_grid: the collection app draws the cards this long after load (> 6 s base wait, < 15 s)
SLOW_PAGE_SECS = 4.0      # slow_page: /pages/about answers after this long (tests set a 3 s navigation timeout; the
                          # 6 s second try leaves 2 s for a loaded CI machine: 14 Oct, GitHub shard failed twice at 3 s / 2 s)
SOLD_OUT_SIZE = {"id": 203, "available": False, "price": "1499.00", "option1": "Extra Large", "title": "Extra Large"}
FACET_MODES = ("facets", "facets_sort", "facets_sort_allowed", "filter_broken",     # storefront filters (journey 25)
               "filter_stock_differs", "filter_big_collection", "filter_empty", "filter_app")
# Shopify's default robots.txt lines for collections (every mode): no sort_by, no two filters, no '+' tag joins
SHOPIFY_COLLECTION_RULES = ("Disallow: /collections/*sort_by*\nDisallow: /*/collections/*sort_by*\n"
                            "Disallow: /collections/*filter*&*filter*\nDisallow: /*/collections/*filter*&*filter*\n"
                            "Disallow: /collections/*+*\nDisallow: /collections/*%2B*\nDisallow: /collections/*%2b*\n")


def facets_page(h: str, items: list[dict], q: dict, mode: str) -> str:
    """A collection page with Dawn-like storefront filters (journey 25): an 'Availability' group (In stock / Out of
    stock) inside a GET form that applies itself on change (fetches the filtered page, swaps the grid, pushState),
    behind a 'Filter' button on phones. filter.v.availability=1 lists only in-stock products, except in filter_broken
    (the same unfiltered list, sold-out lamp included). facets_sort*: a sort_by select in the same form."""
    avail = (q.get("filter.v.availability") or [""])[0]
    # the storefront's view of stock (filter_stock_differs: the lamp keeps selling while products.json says sold out)
    instock = lambda p: any(v["available"] for v in p["variants"]) or (mode == "filter_stock_differs"
                                                                        and p["handle"] == "sold-out-lamp")
    shown = items
    if avail in ("0", "1") and mode != "filter_broken":
        shown = [p for p in items if instock(p) == (avail == "1")]
    if avail == "1" and mode == "filter_empty":          # the store's filter is broken: nothing comes back
        shown = []
    group = None
    if mode == "filter_big_collection":                  # maspar.com: only a 'Size' filter (the variants' option values)
        size = (q.get("filter.v.option.size") or [""])[0]
        has = lambda p, z: any(v.get("option1") == z for v in p["variants"])
        sizes = sorted({v["option1"] for p in items for v in p["variants"] if v.get("option1")})
        shown = [p for p in items if has(p, size)] if size else items
        group = ('<details class="facet"><summary>Size</summary><ul>' + "".join(
            f'<li><label for="Filter-size-{z}"><input type="checkbox" name="filter.v.option.size" value="{z}" '
            f'id="Filter-size-{z}"{" checked" if size == z else ""}> {z} ({sum(has(p, z) for p in items)})</label></li>'
            for z in sizes) + '</ul></details>')
    sort = (q.get("sort_by") or [""])[0]
    if sort == "price-ascending" and mode == "facets_sort_allowed":
        shown = sorted(shown, key=lambda p: min(float(v["price"]) for v in p["variants"]))
    n_in = sum(instock(p) for p in items)
    box = lambda v, text, n: (f'<li><label for="Filter-availability-{v}"><input type="checkbox" name="filter.v.availability" '
                              f'value="{v}" id="Filter-availability-{v}"{" checked" if avail == v else ""}> {text} ({n})</label></li>')
    sort_sel = ('<label>Sort by <select name="sort_by"><option value="manual">Featured</option>'
                '<option value="price-ascending">Price, low to high</option>'
                '<option value="price-descending">Price, high to low</option></select></label>'
                if mode in ("facets_sort", "facets_sort_allowed") else "")
    return (f'<button type="button" class="facets-open" onclick="document.getElementById(\'FacetsWrap\').classList.toggle(\'open\')">'
            f'Filter</button><div id="FacetsWrap" class="facets-wrap"><form id="FacetFiltersForm" action="/collections/{h}" '
            f'method="get">' + (group or f'<details class="facet"><summary>Availability</summary><ul>{box("1", "In stock", n_in)}'
                                         f'{box("0", "Out of stock", len(items) - n_in)}</ul></details>') + f'{sort_sel}'
            '<noscript><button type="submit">Apply</button></noscript></form></div>'
            '<style>@media (max-width: 749px) { .facets-wrap { display: none } .facets-wrap.open { display: block } } '
            '@media (min-width: 750px) { .facets-open { display: none } }</style>'
            f'<div id="product-grid">{"".join(card(p) for p in shown) or "<p>No products found</p>"}</div>'
            # filter_app: an app owns the grid and empties it whenever the address carries filter.* (its own params differ)
            # the app OWNS the grid: whenever the address carries filter.* and the grid shows the theme's cards, it
            # replaces them with its own empty list: at script start, on every tick, and after the theme's own fetch
            # re-fills the grid (14 Oct: a once-only flag let the theme's late re-fill win on slow GitHub machines)
            + ('<script src="/static/boost-sd-filter.js"></script><script>const boostSd = () => { const g = document.'
               "getElementById('product-grid'); if (/[?&]filter\\./.test(location.search) && g.querySelector('a.card')) { "
               "g.innerHTML = '<div class=\"boost-sd__product-list\"></div>'; } }; boostSd(); setInterval(boostSd, 50);</script>"
               if mode == "filter_app" else "")
            + """<script>const ff = document.getElementById('FacetFiltersForm');
ff.addEventListener('change', () => { const url = ff.action.split('?')[0] + '?' + new URLSearchParams(new FormData(ff));
  history.pushState({}, '', url);
  fetch(url).then(r => r.text()).then(t => { const d = new DOMParser().parseFromString(t, 'text/html');
    document.getElementById('product-grid').innerHTML = d.getElementById('product-grid').innerHTML; }); });</script>""")
CARTS: dict[str, list[dict]] = {}
BUY_NOW_MODES = ("buy_now_ok", "buy_now_dead", "buy_now_error")                                     # journey 33
TP_APPS = {"tp_checkout": "gokwik", "tp_checkout_popup": "shopflo", "tp_checkout_blank": "gokwik",   # journey 35
           "tp_checkout_script_404": "gokwik"}

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


FOOTER_LINKS = ('<a href="/cdn/shop/t/1/assets/annual-return-fy-2024-25.pdf">Annual Return FY 2024-25</a> '   # giva.co
                '<a href="/pages/shipping-policy">Shipping Policy</a> <a href="/policies/refund-policy">Refund policy</a> '
                '<a href="/pages/privacy-policy">Privacy Policy</a> <a href="/pages/terms-of-service">Terms of Service</a> '
                '<a href="/pages/contact">Contact us</a> <a href="https://instagram.com/mockstore">Instagram</a> '
                '<a href="/cdn-cgi/l/email-protection#7a19">Email</a>')        # Cloudflare's email link (beminimalist.co)
POLICY_TEXT = ("We ship every order within two working days from our studio in Jaipur. Delivery takes three to seven days "
               "across India; remote pin codes can take up to ten days. Shipping is free above Rs 999; below that a flat "
               "Rs 79 applies. You will get a tracking link by SMS and email as soon as the parcel leaves us. ")
INFO_PAGES = {"/pages/shipping-policy": "Shipping Policy", "/pages/privacy-policy": "Privacy Policy",
              "/pages/terms-of-service": "Terms of Service", "/policies/refund-policy": "Refund policy"}


def card(p):
    return f'<a class="card" href="/products/{p["handle"]}"><img src="/cdn/shop/files/{p["handle"]}.svg" width="40" height="40" alt="">{p["title"]}</a>'


J23_MODES = ("healthy", "card_price_stale")       # collection cards show prices (journey 23)


def card_price_html(handle: str, mode: str) -> str:
    """A collection card's price block. The vase is on sale (struck MRP ₹1,599, 'Save' badge, sale price = its lowest
    variant price ₹1,299); card_price_stale shows an OLD sale price (₹999) left in a cached section while the product
    page and the cart charge ₹1,299."""
    p = next((x for x in PRODUCTS + EXTRA_DECOR if x["handle"] == handle), None)
    if not p:
        return ""
    low = min(float(v["price"]) for v in p["variants"])
    if handle == "ceramic-vase":
        sale = 999.0 if mode == "card_price_stale" else low
        return (f'<s class="price-item price-item--regular">₹1,599.00</s> <span class="badge">Save ₹{1599 - sale:,.0f}</span> '
                f'<span class="price-item price-item--sale">₹{sale:,.2f}</span>')
    return f'<span class="price-item price-item--regular">Rs. {low:,.2f}</span>'


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
        if self.mode == "per_unit_price" and isinstance(body, str) and ctype.startswith("text/html") \
                and urlparse(self.path).path.rstrip("/") == "/products/ceramic-vase":
            # ayamveda.com (new30w) / nubronicforte.com: the page shows the price per unit of a multi-unit variant
            body = re.sub(r'<div class="price">₹1,299\.00</div>', '<div class="price">₹649.50 / piece</div><p>Pack of 2 pieces</p>',
                          body, count=1)
        if self.mode == "no_product_images" and isinstance(body, str) and ctype.startswith("text/html") \
                and urlparse(self.path).path.startswith("/products/"):
            # kalamkitaab.com / lucentpublication.com: book pages with no product picture at all
            body = re.sub(r'<img src="/cdn/shop/files/[^"]+\.svg" width="(300|400)" height="(300|400)"[^>]*>', '', body)
        if self.mode == "customer_auth_link" and isinstance(body, str) and ctype.startswith("text/html"):
            # krkjewels.com (new30t, 15 Oct): the header's 'Login' is Shopify's new-accounts hand-off link, 404 for a robot
            body = body.replace('<a href="/pages/about">About</a>',
                                '<a href="/pages/about">About</a> <a href="/customer_authentication/redirect?locale=en">Login</a>', 1)
        if self.mode == "prepaid_price" and isinstance(body, str) and ctype.startswith("text/html") \
                and urlparse(self.path).path.startswith("/products/"):
            # rasayanam.in (new30m, 14 Oct): 'EXTRA 5% OFF ON ALL PREPAID ORDERS', and the page shows the price minus 5%
            body = body.replace("<main>", '<main><div class="announcement">EXTRA 5% OFF ON ALL PREPAID ORDERS</div>', 1)
            body = re.sub(r'(<div class="price">)₹([\d,]+\.\d\d)',
                          lambda m: f"{m.group(1)}₹{round(float(m.group(2).replace(',', '')) * 0.95):,}", body)
        if self.mode == "plain_size_buttons" and isinstance(body, str) and ctype.startswith("text/html") \
                and urlparse(self.path).path.rstrip("/") == "/products/ceramic-vase":
            # sntvl.com (new30r, 15 Oct, both devices, 3 products): colour swatches in a 'swatch' box, sizes as plain
            # <button>s in a box whose class names neither 'variant' nor 'option'; no size chosen on load, the form's id
            # is empty and the button disabled ('Choose your size') until the shopper taps a size
            body = re.sub(r'(<form id="product-form-template__main"[^>]*>)<input type="hidden" name="id" value="\d+">'
                          r'<button type="submit" name="add">Add to cart</button>',
                          r'<div class="swatches"><button type="button" class="swatch" data-value="Natural">Natural</button></div>'
                          r'<div class="pdp-sizes"><button type="button" class="sz" data-id="201">Small</button>'
                          r'<button type="button" class="sz" data-id="202">Large</button></div>'
                          r'\1<input type="hidden" name="id" value=""><button type="submit" name="add" disabled>Choose your size</button>'
                          r'<script>document.querySelectorAll(".sz").forEach(b => b.onclick = () => {'
                          r' const f = document.getElementById("product-form-template__main");'
                          r' f.querySelector("[name=id]").value = b.dataset.id; const a = f.querySelector("[name=add]");'
                          r' a.disabled = false; a.textContent = "Add to cart"; });</script>', body, count=1)
        if self.mode == "wishlist_404" and isinstance(body, str) and ctype.startswith("text/html"):
            body = body.replace('<a href="/pages/about">About</a>', '<a href="/pages/about">About</a> <a href="/pages/wishlist">Wishlist</a>', 1)
        if self.mode == "market_path" and getattr(self, "market", False) and isinstance(body, str):
            if ctype.startswith("text/html"):
                body = re.sub(r'href="/(?!en-us)', 'href="/en-us/', body).replace('href="/en-us/"', 'href="/en-us"')
                body = re.sub(r'action="/(cart|search)', r'action="/en-us/\1', body)
                body = re.sub(r"₹\s?([\d,]+(?:\.\d\d)?)", lambda m: f"₹{float(m.group(1).replace(',', '')) * 1.2:,.2f}", body)
                body = re.sub(r"Rs\.\s?([\d,]+(?:\.\d\d)?)", lambda m: f"Rs. {float(m.group(1).replace(',', '')) * 1.2:,.2f}", body)
            elif ctype.startswith("application/json") and '"variants"' in body:
                data = json.loads(body)
                for v in data.get("variants", []):
                    if isinstance(v.get("price"), int):
                        v["price"] = int(round(v["price"] * 1.2))
                body = json.dumps(data)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "sibling_variant_links" \
                and urlparse(self.path).path.rstrip("/") == "/products/ceramic-vase":
            # beminimalist.co (full 36, 14 Oct, both devices, twice): 'CHOOSE VARIANTS' = big cards linking to the
            # sibling products (other sizes are separate products) inside the same box as the product's own <h1>
            sib = ('<div class="pinfo">\\1<p>CHOOSE VARIANTS</p><div class="sizes">'
                   + "".join([f'<a href="/products/{x["handle"]}" style="display:block;width:420px;height:220px;'
                              f'border:1px solid #ccc">{x["title"]}</a>' for x in PRODUCTS if x["handle"] != "ceramic-vase"][:3])
                   + '</div></div>')
            body = re.sub(r"(<h1>[^<]*</h1>)", sib, body, count=1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "late_redirect_product" \
                and urlparse(self.path).path.rstrip("/") == "/products/ceramic-vase":
            # foxtale.in (full 36 + re-test, 14 Oct, flaky twice): the product page renders, then the storefront's app
            # sends the shopper to the homepage a moment later (a catalog product hidden from shoppers)
            body = body.replace("</body>", f"<script>addEventListener('load', () => setTimeout(() => location.replace('/'), "
                                           f"{LATE_REDIRECT_MS}));</script></body>")
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "headless_alias":
            if getattr(self, "canon", None):
                body = body.replace('<link rel="canonical" href="/">', f'<link rel="canonical" href="/products/{self.canon}">', 1)
            ids = {x["handle"]: x["id"] for x in PRODUCTS}
            body = re.sub(r'(<a [^>]*href=")/products/([a-z0-9-]+)"', lambda m: f'{m.group(1)}/products/sku-{ids[m.group(2)]}"'
                          if m.group(2) in ids else m.group(0), body)
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in J15_MODES:
            # journey 15: an announcement bar above the page (only in these modes: other modes test the journey's
            # navigation and must not get an extra link to follow)
            body = body.replace("<main>", '<div class="announcement-bar"><a href="/pages/offers">Festive offers: up to 20% '
                                'off</a></div><main>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in ("hamburger_menu", "hamburger_broken") \
                and "Mobile" in (self.headers.get("User-Agent") or ""):
            # journey 16, phones only: the header menu hides behind a ☰ button that opens a drawer with the links
            # (hamburger_broken: the button's script throws, the drawer never opens)
            js = ("throw new Error('menu-drawer.js: toggle is not a function')" if self.mode == "hamburger_broken" else
                  "document.getElementById('m-drawer').style.display = 'block'")
            body = body.replace(self.MAIN_NAV, '<button type="button" class="header__icon--menu" aria-label="Menu" '
                                f'onclick="{js}">&#9776;</button><div id="m-drawer" style="display:none"><nav>'
                                '<a href="/collections/home-decor">Home Decor</a> <a href="/collections/kitchen">Kitchen</a> '
                                '<a href="/pages/about">About</a></nav></div>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "search_icon_only":
            # the header's search form stays hidden until a bare icon link is clicked: no aria-label, not a button, not a
            # /search link, only title="Search" and an svg icon (antinorm, plum, soulflower... 36-store run 11 Oct)
            body = body.replace('<form action="/search" method="get"><input name="q"></form>',
                                '<a href="javascript:void(0)" class="header-icon js-drawer-open-top" title="Search" '
                                'onclick="document.getElementById(\'hdr-search\').style.display=\'block\'">'
                                '<svg class="icon icon-search" width="20" height="20"><circle cx="9" cy="9" r="6" stroke="#000" '
                                'fill="none"/></svg></a><form id="hdr-search" action="/search" method="get" style="display:none">'
                                '<input name="q"></form>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "late_cards" and "/collections/" in self.path:
            # the collection grid is rendered by a script 1.5 s after load (thehouseofrare.com phone, 11 Oct)
            body = re.sub(r'(<main>)(.*?)(</main>)', lambda m: m.group(1) + '<div id="grid"></div><template id="cards">'
                          + m.group(2) + '</template><script>setTimeout(() => { document.getElementById("grid").innerHTML = '
                          'document.getElementById("cards").innerHTML; }, 1500);</script>' + m.group(3), body, count=1, flags=re.S)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in J23_MODES and "/collections/" in self.path:
            # journey 23: collection cards show their price like Dawn's card-product (only in these modes: other modes'
            # tests read card text). The vase is 'on sale': MRP struck through, a 'Save' badge, the sale price.
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(.*?)</a>', lambda m: (
                f'<span class="card-wrapper product-card-wrapper" style="display:inline-block;margin:6px">{m.group(0)}'
                f'<span class="card__price price">{card_price_html(m.group(1), self.mode)}</span></span>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "script_heavy":
            # journey 31: 26 app scripts, each from its own third-party host (*.localhost resolves to this machine)
            body = body.replace("</body>", "<script>for (let i = 1; i <= 26; i++) { const s = document.createElement('script'); "
                                "s.src = `http://app${i}.localhost:${location.port}/static/app${i}.js`; document.head.appendChild(s); }"
                                "</script></body>")
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "annual_return_pdf":
            body = body.replace('<div class="footer-menu">', '<div class="footer-menu"><a href="/cdn/shop/t/234/assets/'
                                'annual-return-fy-2024-25.pdf">Annual Return FY 2024-25</a> ')
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "grid_on_scroll" and urlparse(self.path).path.startswith("/collections/"):
            cards = re.findall(r'<a class="card" href="/products/[^"]+">.*?</a>', body)
            for c in cards:
                body = body.replace(c, "", 1)
            body = body.replace("<main>", '<main><div class="sale-banner" style="height:1600px">SALE</div>'
                                '<div id="app-grid" style="min-height:50px"></div>', 1)
            body = body.replace("</body>", "<div style='height:1200px'></div><script>const G = " + json.dumps("".join(cards)) +
                                "; new IntersectionObserver((es, o) => { if (es[0].isIntersecting) { "
                                "document.getElementById('app-grid').innerHTML = G; o.disconnect(); } })"
                                ".observe(document.getElementById('app-grid'));</script></body>", 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "veiled_cards":
            body = body.replace("</body>", '<div class="veil" style="position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,0.001)"></div></body>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "pdp_no_image" and "/products/" in self.path:
            body = re.sub(r'<img src="/cdn/shop/files/[^"]+\.svg" width="300" height="300"[^>]*>', '', body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "lazy_gallery" and "/products/" in self.path:
            body = re.sub(r'<img src="(/cdn/shop/files/[^"]+\.svg)" width="300" height="300"',
                          r'<img class="gallery-slide" data-src="\1" width="300" height="300"', body, count=1)
            body = body.replace("</body>", "<script>setTimeout(() => document.querySelectorAll('img.gallery-slide').forEach("
                                "i => i.src = i.dataset.src), 2000);</script></body>", 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in ("other_market", "other_market_norate", "other_market_offer"):
            body = re.sub(r"₹\s?([\d,]+(?:\.\d\d)?)", lambda m: f"${float(m.group(1).replace(',', '')) * 0.012:,.2f}", body)
            body = re.sub(r"Rs\.\s?([\d,]+(?:\.\d\d)?)", lambda m: f"${float(m.group(1).replace(',', '')) * 0.012:,.2f}", body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "other_market_offer" and "/products/" in self.path:
            body = body.replace("</body>", '<div class="offers">Flat ₹250 off | Use code FIRSTBUY. Valid on 1st order only, '
                                'min. order value ₹2,500</div></body>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "other_market":
            body = body.replace("</head>", "<script>window.Shopify = window.Shopify || {}; Shopify.country = 'US'; "
                                "Shopify.currency = {active: 'USD', rate: '0.012'};</script></head>", 1)
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "href_space":
            body = re.sub(r'<a class="card" href="/products/([^"]+)">', r'<a class="card" href="/products/\1 ">', body)
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
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode in ("hover_menu", "brand_landing", "phone_brand_landing"):
            if path_now == "/":                       # brand-landing homepage: no products, no collection links
                body = re.sub(r'<a class="card"[^>]*>.*?</a>', "", body)
            mega = ('<div class="mega" style="position:absolute;left:0;top:30px;background:#fff;padding:10px;display:none">'
                    '<a href="/collections/home-decor">Home Decor</a> <a href="/collections/kitchen">Kitchen</a></div>'
                    if self.mode == "hover_menu" else "")
            body = body.replace(self.MAIN_NAV, '<nav><div class="menu-item" style="position:relative;display:inline-block">'
                                f'<a href="/pages/shop">Shop</a>{mega}</div> <a href="/pages/about">About</a></nav>'
                                '<style>.menu-item:hover .mega{display:block !important}</style>', 1)
            body = body.replace('<a href="/collections/all">Shop all</a>', '')
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "phone_brand_landing" \
                and "Mobile" in (self.headers.get("User-Agent") or ""):
            # subko.coffee phone (new30j, 11 Oct): the brand-landing top menu ('Shop' -> /pages/shop) sits behind the ☰
            # button on phones; the drawer holds no collection link, only the landing pages
            body = body.replace('<nav><div class="menu-item"', '<button type="button" class="header__icon--menu" '
                                'aria-label="Menu" onclick="document.getElementById(\'m-drawer\').style.display=\'block\'">'
                                '&#9776;</button><div id="m-drawer" style="display:none"><nav><div class="menu-item"', 1)
            body = body.replace('<a href="/pages/about">About</a></nav><style>', '<a href="/pages/about">About</a></nav></div><style>', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "contents_cards":
            # valliyan.com (Baseline theme, new30j, both devices): the product card's link is display:contents around
            # the image + name + price. A shopper sees and taps the card; the <a> itself has no box (0x0 rect), so
            # Radar said 'products listed: 0' and '15 links: 15 not visible'.
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<a class="card" href="/products/{m.group(1)}" style="display:contents">'
                f'<div class="product-item" style="display:inline-block;width:220px;margin:8px;vertical-align:top">'
                f'<div class="product-item__image" style="height:200px;background:#eee">{m.group(2)}</div>'
                f'<div class="product-item__title">{m.group(3)}</div></div></a>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "offsite_hang":
            body = body.replace('<div class="footer-menu">', '<div class="footer-menu"><a href="/map">Assagao, Goa</a> '
                                '<a href="/panjim">Panjim, Goa</a> ', 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "contents_reveal_cards":
            # valliyan.com re-test 2: a boxless card link with a reveal class still at opacity:0 (has no effect: no box)
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<a class="card reveal" href="/products/{m.group(1)}" style="display:contents;opacity:0">'
                f'<div class="product-item" style="display:inline-block;width:220px;margin:8px;vertical-align:top">'
                f'<div class="product-item__image" style="height:200px;background:#eee">{m.group(2)}</div>'
                f'<div class="product-item__title">{m.group(3)}</div></div></a>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "hidden_link_shown_cards":
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<a class="card" href="/products/{m.group(1)}" style="display:inline-block;width:220px;margin:8px;'
                f'visibility:hidden"><div class="product-item" style="visibility:visible">'
                f'<div class="product-item__image" style="height:200px;background:#eee">{m.group(2)}</div>'
                f'<div class="product-item__title">{m.group(3)}</div></div></a>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "stretched_link_cards":
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<div class="product-item" style="position:relative;display:inline-block;width:220px;margin:8px;vertical-align:top">'
                f'<div class="product-item__image" style="height:200px;background:#eee">{m.group(2)}</div>'
                f'<div class="product-item__title">{m.group(3)}</div><a class="increase-target" href="/products/{m.group(1)}">'
                f'<span class="visually-hidden">{re.sub(r"<[^>]+>", "", m.group(3))}</span></a></div>'), body)
            body = body.replace("</head>", "<style>.increase-target::before{content:'';position:absolute;top:0;right:0;"
                                "bottom:0;left:0}.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;"
                                "clip:rect(0 0 0 0);white-space:nowrap}</style></head>", 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "zero_box_cards":
            # valliyan.com re-test: display:contents link > a wrapper whose children float (the wrapper's height is 0)
            body = re.sub(r'<a class="card" href="/products/([^"]+)">(<img[^>]*>)(.*?)</a>', lambda m: (
                f'<a class="card" href="/products/{m.group(1)}" style="display:contents">'
                f'<div class="product-item" style="width:240px">'
                f'<div class="product-item__image" style="float:left;width:220px;height:200px;background:#eee">{m.group(2)}</div>'
                f'<div class="product-item__title" style="float:left;width:220px">{m.group(3)}</div></div></a>'), body)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "landing_collection" \
                and path_now == "/collections/home-decor":
            # quboworld.com (Horizon, new30j): the menu's first collection is an 'entrance' landing page: a hero and
            # tiles with 'Learn more' buttons to sub-collections, no product grid; the mega menu's product links are in
            # the page but hidden ('104 links: 104 not visible')
            body = re.sub(r'(<main>).*?(</main>)', lambda m: m.group(1) + (
                '<section class="hero" style="height:420px;background:#111;color:#fff"><h1>Smarter homes.</h1></section>'
                '<div class="mega-products" style="display:none">'
                + "".join(f'<a href="/products/{p["handle"]}">{p["title"]}</a>' for p in PRODUCTS if p["collection"]) +
                '</div><section class="tiles"><div class="tile"><h3>Kitchen</h3>'
                '<a class="button" href="/collections/kitchen">Learn more</a></div></section>') + m.group(2),
                body, count=1, flags=re.S)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "country_modal":
            # teatrunk.in (new30i, both devices): 'Shopping from Outside India?' with International / Stay Here, rendered
            # inside the theme's <header>, its backdrop (div.ctryModal-Content-overlay) over every card and menu link.
            # Shown until the shopper answers. 'International' leaves the store: never clicked.
            body = body.replace("</header>", """<div class="ctryModal" id="ctryModal" style="position:fixed;inset:0;z-index:100">
<div class="ctryModal-Content-overlay" style="position:absolute;inset:0;background:rgba(0,0,0,.45)"></div>
<div class="ctryModal-Content" style="position:relative;margin:30vh auto 0;width:60%;background:#fff;padding:20px">
<h2>Shopping from Outside India?</h2><p>We have a dedicated store which serves in your country. Would you like us to take you there?</p>
<button type="button" class="ctry-intl">International</button> <button type="button" class="ctry-stay">Stay Here</button></div></div>
<script>(() => { const m = document.getElementById('ctryModal');
  if (document.cookie.includes('ctry=stay')) { m.remove(); return; }
  m.querySelector('.ctry-intl').onclick = () => { location.href = '/pages/international'; };
  m.querySelector('.ctry-stay').onclick = () => { document.cookie = 'ctry=stay;path=/'; m.remove(); }; })();</script></header>""", 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "country_modal_div_stay":
            # teatrunk.in re-test (run 38461923525): same modal, but its backdrop is a sibling of the box inside a
            # section and 'Stay Here' is a plain <div> with a click handler (only 'International' is a <button>).
            body = body.replace("</header>", """<div class="shopify-section" id="ctryModal">
<div class="ctryModal-Content-overlay" style="position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.45)"></div>
<div class="ctryModal-Content" style="position:fixed;top:30vh;left:20%;width:60%;z-index:101;background:#fff;padding:20px">
<h2>Shopping from Outside India?</h2><p>We have a dedicated store which serves in your country. Would you like us to take you there?</p>
<button type="button" class="ctry-intl">International</button> <div class="ctry-stay" style="display:inline-block;cursor:pointer;padding:8px">Stay Here</div></div></div>
<script>(() => { const m = document.getElementById('ctryModal');
  if (document.cookie.includes('ctry=stay')) { m.remove(); return; }
  m.querySelector('.ctry-intl').onclick = () => { location.href = '/pages/international'; };
  m.querySelector('.ctry-stay').onclick = () => { document.cookie = 'ctry=stay;path=/'; m.remove(); }; })();</script></header>""", 1)
        if isinstance(body, str) and ctype.startswith("text/html") and self.mode == "country_modal_stay_on":
            # vedix.com (new30l, desktop): 'Visiting from United States (USD $)?' with 'Shop on US Site' (leaves the
            # store: never clicked), the link 'Stay on Vedix IN Site' and a country dropdown; its x is an unlabelled icon.
            # Re-test (run 38437504227) still not closed: the closed country list ('#' links, hidden) made the layer look
            # like page content (> 12 links); only visible links that go somewhere count.
            body = body.replace("</header>", """<div class="geo-wrap" id="geoModal" style="position:fixed;inset:0;z-index:100">
<div class="geo-bg" style="position:absolute;inset:0;background:rgba(0,0,0,.45)"></div>
<div class="geo-box" style="position:relative;margin:25vh auto 0;width:60%;background:#fff;padding:20px">
<span class="geo-x" style="position:absolute;right:8px;top:8px;cursor:pointer"><svg width="12" height="12" viewBox="0 0 12 12"><path d="M1 1 11 11M11 1 1 11" stroke="#000"/></svg></span>
<h2>Visiting from United States (USD $)?</h2><a class="geo-go" href="/pages/international">Shop on US Site</a>
<a class="geo-stay" href="#">Stay on Mock IN Site</a><p>Or Select Your Country Below</p>
<div class="geo-select"><button type="button" class="geo-toggle">Select</button><ul class="geo-list" style="display:none">"""
                + "".join(f'<li><a href="#" data-value="C{i}">Country {i}</a></li>' for i in range(40)) + """</ul></div></div></div>
<script>(() => { const m = document.getElementById('geoModal');
  if (document.cookie.includes('geo=stay')) { m.remove(); return; }
  const stay = e => { e.preventDefault(); document.cookie = 'geo=stay;path=/'; m.remove(); };
  m.querySelector('.geo-stay').onclick = stay; m.querySelector('.geo-x').onclick = stay; })();</script></header>""", 1)
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
        if self.mode == "wishlist_404" and path == "/pages/wishlist":
            # countryflora.com / kotart.in / swornyfashion.com (new30p, 15 Oct): the wishlist app's page is the 404
            # template with the app drawing the list into it
            return self._send(404, page("Wishlist | Mock Store", '<div id="wishlist-app"><h1>My Wishlist</h1>'
                                        '<p>Your wishlist is empty. Tap the heart on any product to save it here.</p></div>'))
        if self.mode == "market_path":
            # amayrajewellery.com (new30n, 14 Oct): Shopify Markets sends the visitor to /en-us/, where every page link
            # carries the prefix and prices are the US market's (+20%); the default market's /products/<h>.js says less
            self.market = path == "/en-us" or path.startswith("/en-us/")
            if path == "/" and not self.market:
                self.send_response(302)
                self.send_header("Location", "/en-us")
                self.end_headers()
                return
            if self.market:
                self.path = self.path[len("/en-us"):] or "/"
                u = urlparse(self.path)
                path = u.path.rstrip("/") or "/"
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
        if self.mode == "refused_mid_run" and not path.startswith("/cdn/") and path != "/robots.txt" \
                and not path.endswith((".js", ".json")):
            st = type(self).__dict__.get("REFUSED") or {"n": 0}
            type(self).REFUSED = st
            st["n"] += 1
            if st["n"] > 3:                 # discovery's first page loads pass; the wall goes up for the tests
                return self._send(403, page("Access denied", "<h1>Access denied</h1><p>Request blocked.</p>"))
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
        if path == "/robots.txt" and self.mode in ("robots_404", "blank_not_shopify"):
            return self._send(404, "Not Found", "text/plain")
        if path == "/robots.txt":
            extra = "Disallow: /search\n" if self.mode == "hostile" else ""
            # /account and /policies/ disallowed, as some stores do (journeys 28 + 29 must skip them)
            acct = "" if self.mode in ("account_open", "account_broken") else "Disallow: /account\n"
            coll = SHOPIFY_COLLECTION_RULES
            if self.mode == "facets_sort_allowed":           # a store whose robots.txt lets sorting through (journey 25)
                coll = "".join(l + "\n" for l in coll.splitlines() if "sort_by" not in l)
            return self._send(200, "User-agent: *\nDisallow: /checkout\nDisallow: /cart\n" + acct + "Disallow: /policies/\n"
                              + extra + coll, "text/plain")
        if path.endswith(".pdf"):     # a company filing linked from the footer: the browser downloads it (giva.co, 11 Oct)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="annual-return.pdf"')
            self.send_header("Content-Length", "8")
            self.end_headers()
            self.wfile.write(b"%PDF-1.4")
            return
        if path in ("/gokwik/checkout", "/shopflo/checkout"):    # journey 35: the app's checkout (on 'localhost': another origin)
            if self.mode == "tp_checkout_blank":
                return self._send(200, "<html><head><title></title></head><body></body></html>")
            items = (q.get("items") or [""])[0].replace("<", "")
            app = "GoKwik" if path.startswith("/gokwik") else "Shopflo"
            return self._send(200, f"""<html><head><title>{app} checkout</title></head><body><div id="app"></div>
<template id="first"><div class="brand">Mock Store · secured by {app}</div><div class="summary">Order summary: {items}</div>
<h3>Enter mobile number to continue</h3><input type="tel" name="phone" placeholder="Enter mobile number">
<button type="button" class="get-otp">Get OTP</button></template>
<script>setTimeout(() => document.getElementById('app').innerHTML = document.getElementById('first').innerHTML, 800);</script>
</body></html>""")
        if path.startswith("/static/") and path.endswith("-checkout.js") and self.mode in TP_APPS:
            app = TP_APPS[self.mode]
            if self.mode == "tp_checkout_script_404" or app not in path:
                return self._send(404, "not found", "text/plain")
            how = ("window.open(u, 'checkout', 'width=480,height=720');" if app == "shopflo" else
                   "{ const f = document.createElement('iframe'); f.id = '" + app + "-iframe'; f.src = u; "
                   "f.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;border:0;z-index:9999;background:#fff'; "
                   "document.body.appendChild(f); }")
            return self._send(200, "document.querySelectorAll('[name=checkout]').forEach(b => b.addEventListener('click', e => {"
                              " e.preventDefault(); const u = 'http://localhost:' + location.port + '/" + app + "/checkout?items='"
                              " + encodeURIComponent(b.dataset.items || ''); " + how + " }));", "application/javascript")
        if path.startswith("/static/") and path.endswith(".js"):      # third-party app scripts (script_heavy mode)
            return self._send(200, "void 0;", "application/javascript")
        if path.startswith("/cdn/shop/files/") or path.startswith("/static/"):
            if self.mode == "pdp_broken_image" and path.endswith("/ceramic-vase.svg"):
                return self._send(404, "gone", "text/plain")
            return self._send(200, SVG, "image/svg+xml")
        if path == "/" and self.mode == "blank_not_shopify":     # gozero.in (cycle 34): no title, no markers, no robots.txt
            return self._send(200, '<!doctype html><html><head><meta charset="utf-8"><title></title></head>'
                                   '<body><div id="root"></div></body></html>')
        if path == "/" and self.mode == "store_refuses":
            return self._send(423, page("This store is unavailable", "<h1>This store is unavailable</h1>"))
        if path == "/":
            return self._send(200, page("Mock Store | Handmade Home Goods",
                                        "<h1>Welcome</h1>" + "".join(card(p) for p in PRODUCTS if p["collection"])
                                        + ('<section class="story-banner"><a href="/pages/our-story">Read our story</a> '
                                           '<a href="/collections/kitchen">Shop the kitchen</a>'
                                           + (' <a href="/https://www.mockstore.in/collections/sale">Sale</a>'   # beyondsnack.in
                                              if self.mode == "broken_section_link" else "") + '</section>'
                                           if self.mode in J15_MODES else "")
                                        + ('<section class="quiz-banner"><a href="/tools/product-finder/match-me">Match me '
                                           'a cup</a></section>' if self.mode == "app_proxy_page" else ""),
                                        shopify=self.mode != "not_shopify"), set_cart=new)
        if path == "/pages/blank":       # HTTP 200 with a title but nothing on screen (JS crash style)
            return self._send(200, "<!doctype html><html><head><title>Blank | Mock Store</title></head><body><div id=app></div></body></html>")
        if path == "/tools/product-finder/match-me" and self.mode == "app_proxy_page":
            # arakucoffee.in (cycle 34): an app-proxy quiz page, no theme layout, drawn by a web component in a CLOSED
            # shadow root (no text in document.body, no <img>)
            return self._send(200, '<!doctype html><html><head><meta charset="utf-8"><title>Match Me A Cup</title></head>'
                                   '<body><quiz-app style="display:block"></quiz-app><script>customElements.define("quiz-app", '
                                   'class extends HTMLElement { connectedCallback() { const r = this.attachShadow({mode: "closed"}); '
                                   'setTimeout(() => { r.innerHTML = \'<h1 style="font-size:44px">Find your perfect cup</h1>'
                                   '<p style="font-size:20px">Answer three quick questions about how you brew and we will '
                                   'match you with a coffee.</p><button>Start the quiz</button>\'; }, 300); } });</script>'
                                   '</body></html>')
        if self.mode == "offsite_hang" and path in ("/map", "/panjim"):
            # nonasties.in (new30k, phone): the store's footer link redirects to Google Maps, another site
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_address[1]}/offsite{path}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.mode == "offsite_hang" and path.startswith("/offsite/"):
            import time as _t                     # the other site never finishes for this browser (tests: 2 s timeout)
            _t.sleep(OFFSITE_HANG_SECS)
            return self._send(200, page("Maps", "<p>map</p>"))
        if path == "/pages/about" and self.mode == "slow_page":
            import time as _t                     # bluenectar.co.in (cycle 34, desktop): pages slower than the timeout
            _t.sleep(SLOW_PAGE_SECS)
        if path == "/pages/about":
            return self._send(200, page("About Mock Store", "<p>About us</p>"))
        if path in ("/pages/our-story", "/pages/offers"):      # journey 15: a homepage banner, the announcement bar
            if self.mode == "broken_section_link" and path == "/pages/our-story":     # the banner points at a deleted page
                return self._send(404, page("404 Not Found | Mock Store", "<h1>404 Page not found</h1>"))
            if self.mode == "broken_section_link" and path == "/pages/offers":        # an ended offer redirects home
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            name = "Our story" if path.endswith("story") else "Festive offers"
            return self._send(200, page(f"{name} | Mock Store", f"<h1>{name}</h1><p>{POLICY_TEXT}</p>"))
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
            cat = ([FREEBIE] if self.mode == "freebie_type" else []) + PRODUCTS
            return self._json({"products": [{"id": p["id"], "handle": p["handle"], "title": p["title"],
                                             "product_type": p.get("product_type", ""), "tags": p.get("tags", []),
                                             "variants": self._variants(p, catalog=True)} for p in cat]})
        if path.startswith("/collections/"):
            h = path.split("/")[2]
            items = [p for p in PRODUCTS if (h == "all" and p["collection"]) or p["collection"] == h]
            if self.mode in MORE_MODES and h == "home-decor":
                items = items + EXTRA_DECOR
            if path.endswith("products.json"):
                if self.mode == "freebie_type" and h in ("home-decor", "all"):
                    items = [FREEBIE] + items
                if self.mode == "filter_big_collection":     # Radar reads 250 of a bigger collection: the vase is not in them
                    items = [p for p in items if p["handle"] != "ceramic-vase"]
                return self._json({"products": [{"handle": p["handle"], "title": p["title"], "variants":
                                                 PACK_VARIANTS if self.mode == "pack_of_two" and p["handle"] == "ceramic-vase"
                                                 else p["variants"]} for p in items]})
            if not items:
                return self._send(404, page("Not found", "404"))
            if self.mode == "grid_names_dead":       # the same cards, but nothing happens when a shopper taps them
                tiles = "".join(f'<div class="tile" style="display:inline-block;width:200px;margin:8px"><div style="height:160px;'
                                f'background:#eee"></div><div class="tile__title">{p["title"]}</div><div>Rs. {p["price"]}</div></div>'
                                for p in items)
                return self._send(200, page(f"{h.title()} Collection | Mock Store", f"<h2>{len(items)} products</h2>{tiles}"), set_cart=new)
            if self.mode == "grid_without_links":    # giva.co desktop: cards a script navigates, no product links
                tiles = "".join(f'<div class="tile" role="button" data-url="/products/{p["handle"]}" style="display:inline-block;'
                                f'width:200px;margin:8px"><div style="height:160px;background:#eee"></div>'
                                f'<div class="tile__title">{p["title"]}</div><div>Rs. {p["price"]}</div></div>' for p in items)
                return self._send(200, page(f"{h.title()} Collection | Mock Store", f"<h2>{len(items)} products</h2>{tiles}"
                                            "<script>document.querySelectorAll('.tile').forEach(t => t.onclick = () => "
                                            "location.href = t.dataset.url);</script>"), set_cart=new)
            if self.mode == "grid_empty_data_full":  # the page lists nothing; the store's data lists the products
                return self._send(200, page(f"{h.title()} Collection | Mock Store", "<h2>Our collection</h2><p>Loading…</p>"), set_cart=new)
            if self.mode == "late_grid":        # giva.co phone: the count is in the HTML, the cards come from a script later
                cards = "".join(card(p) for p in items).replace("`", "")
                return self._send(200, page(f"{h.title()} Collection | Mock Store",
                                            f"<h2>{len(items)} products</h2><div id=\"late-grid\"></div><script>setTimeout(() => "
                                            f"{{ document.getElementById('late-grid').innerHTML = `{cards}`; }}, "
                                            f"{int(LATE_GRID_SECS * 1000)});</script>"), set_cart=new)
            if self.mode in FACET_MODES:
                return self._send(200, page(f"{h.title()} Collection | Mock Store", facets_page(h, items, q, self.mode),
                                            '<meta name="viewport" content="width=device-width, initial-scale=1">'),
                                  set_cart=new)
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
            if self.mode == "headless_alias" and h.startswith("sku-"):
                # palmonas.com (full 36, 14 Oct): a headless storefront's product URL is a SKU, not the Shopify handle;
                # /products/<sku>.js answers the HTML page; the page's canonical names the real handle.
                p = next((x for x in PRODUCTS if f"sku-{x['id']}" == h.removesuffix(".js")), None)
                if not p:
                    return self._send(404, page("Page not found", "<h1>404</h1>"))
                if h.endswith(".js"):
                    return self._send(200, '<!DOCTYPE html><html lang="en"><head><meta charSet="utf-8"/></head><body>app</body></html>')
                self.canon = p["handle"]
                h = p["handle"]
            if h.endswith(".js"):
                p = next((x for x in PRODUCTS + ([FREEBIE] if self.mode == "freebie_type" else []) if x["handle"] == h[:-3]), None)
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
                if self.mode == "no_product_images":
                    return self._json({"id": p["id"], "handle": p["handle"], "title": p["title"], "images": [],
                                       "featured_image": None,
                                       "variants": [dict(v, price=int(float(v["price"]) * 100)) for v in self._variants(p)]})
                return self._json({"id": p["id"], "handle": p["handle"], "title": p["title"],
                                   "variants": [dict(v, price=int(float(v["price"]) * 100)) for v in self._variants(p)]})
            if self.mode == "freebie_type" and h == "free-fabric":      # suta.in: a gift SKU's page, no product image
                return self._send(200, page("Free Fabric | Mock Store", '<div class="product"><h1>Free Fabric</h1>'
                                            '<span class="badge">FREEBIES</span><div class="price">Rs. 1,000.00</div>'
                                            '<form action="/cart/add" method="post"><input type="hidden" name="id" value="901">'
                                            '<button type="submit" name="add">Add to cart</button></form></div>'))
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
        if path == "/cart/change":            # Shopify: GET /cart/change?line=N&quantity=Q, then back to the cart page
            if self.mode != "cart_remove_broken":
                self._change(cid, int((q.get("line") or ["0"])[0]), int((q.get("quantity") or ["0"])[0]))
            self.send_response(302)
            self.send_header("Location", "/cart")
            self.end_headers()
            return
        if path == "/checkouts/cn/buynow":     # journey 33: Shopify's buy-now checkout holds only that variant, not the cart
            if self.mode == "buy_now_error":
                return self._send(500, page("Checkout error", "<h1>There was a problem with our checkout</h1>"))
            vid = int((q.get("variant") or ["0"])[0] or 0)
            p = next((x for x in PRODUCTS for v in x["variants"] if v["id"] == vid), None)
            if not p:
                return self._send(404, page("Page not found", "<h1>404</h1>"))
            price = int(float(next(v["price"] for v in p["variants"] if v["id"] == vid)) * 100)
            return self._send(200, page("Checkout - Mock Store", '<h2>Contact</h2><input type="email" name="email" '
                                        'placeholder="Email or mobile phone number"><h2>Delivery</h2><input name="firstName" '
                                        'placeholder="First name"><h2>Payment</h2><button type="submit" id="checkout-pay-button">'
                                        f'Pay now</button><aside id="summary"><div class="summary-line">{p["title"]} × 1 '
                                        f'{rupees(price)}</div></aside>'))
        if path in ("/checkout", "/checkouts/cn/mock"):
            items = CARTS[cid]
            if not items:                          # Shopify sends an empty cart back to the cart page
                self.send_response(302)
                self.send_header("Location", "/cart")
                self.end_headers()
                return
            if self.mode == "checkout_broken":
                return self._send(500, page("Checkout error", "<h1>There was a problem with our checkout</h1>"))
            summary = "".join(f'<div class="summary-line">{i["title"]} × {i["quantity"]} {rupees(i["price"] * i["quantity"])}</div>'
                              for i in items)
            return self._send(200, page("Checkout - Mock Store", '<h2>Contact</h2><input type="email" name="email" '
                                        'placeholder="Email or mobile phone number" autocomplete="shipping email"><h2>Delivery</h2>'
                                        '<input name="firstName" placeholder="First name"><h2>Payment</h2>'
                                        f'<button type="submit" id="checkout-pay-button">Pay now</button>'
                                        # checkout_big_script: Shopify's checkout inlines ~100 kB of JSON before the summary
                                        + ('<script type="application/json" id="checkout-data">' + '{"k":"' + 'x' * 100000 + '"}</script>'
                                           if self.mode == "checkout_big_script" else '') + '<aside id="summary"></aside>'
                                        # Shopify's checkout is an app: the order summary renders seconds after the page
                                        f'<template id="sum">{summary}</template><script>setTimeout(() => document.getElementById'
                                        f'("summary").innerHTML = document.getElementById("sum").innerHTML, 2500);</script>',
                                        shopify=True))
        if path.startswith("/apps/discount/apply"):
            return self._send(500, "Internal Server Error", "text/plain")
        if path == "/cart":
            items = CARTS[cid]
            # Dawn-like cart rows: − / quantity / + controls, a remove link, the line price, a subtotal
            rows = "".join(
                # Dawn's real cart (theme demos, cycle 28): a table row; the quantity box carries hidden
                # 'Decrease quantity for <title>' text; the line price sits in its own cell, a discount code shows the
                # regular price struck and a lower sale price
                f'<table><tbody><tr class="cart-item"><td><a href="/products/{i.get("handle", "")}">{i["title"]}</a>'
                f'<div class="price"><s>{rupees(i["price"])}</s> <span>{rupees(i["price"] * 9 // 10)}</span> WELCOME10</div></td>'
                f'<td><quantity-popover><quantity-input><button type="button" name="minus"><span class="visually-hidden" '
                f'style="position:absolute;clip:rect(0 0 0 0);width:1px;height:1px;overflow:hidden">Decrease quantity for {i["title"]}</span>−</button>'
                f'<input class="quantity__input" type="number" name="updates[]" value="{i["quantity"]}" data-line="{n}" '
                f'data-quantity-variant-id="{i["id"]}"><button type="button" name="plus"><span class="visually-hidden" '
                f'style="position:absolute;clip:rect(0 0 0 0);width:1px;height:1px;overflow:hidden">Increase quantity for {i["title"]}</span>+</button>'
                f'</quantity-input></quantity-popover><cart-remove-button><a href="/cart/change?line={n}&quantity=0" '
                f'aria-label="Remove {i["title"]}">Remove</a></cart-remove-button></td>'
                f'<td class="totals"><s>{rupees(i["price"] * i["quantity"])}</s> <span>{rupees(i["price"] * i["quantity"] * 9 // 10)}</span></td></tr></tbody></table>'
                if self.mode == "cart_dawn_rows" else
                f'<div class="cart-item" data-line="{n}">{i["title"]} x{i["quantity"]} '
                f'<span class="line-price">{rupees(i["price"] * i["quantity"])}</span>'
                f'<quantity-input><button type="button" name="minus" aria-label="Decrease quantity for {i["title"]}">−</button>'
                f'<input class="quantity__input" type="number" name="updates[]" value="{i["quantity"]}" data-line="{n}" '
                f'data-quantity-variant-id="{i["id"]}" '
                f'aria-label="Quantity for {i["title"]}"><button type="button" name="plus" '
                f'aria-label="Increase quantity for {i["title"]}">+</button></quantity-input>'
                f'<cart-remove-button><a href="/cart/change?line={n}&quantity=0" aria-label="Remove {i["title"]}">Remove</a>'
                f'</cart-remove-button></div>' for n, i in enumerate(items, 1)) or "<p>Your cart is empty</p>"
            total = sum(i["price"] * i["quantity"] for i in items)
            js = "" if self.mode == "cart_qty_broken" else """<script>
document.querySelectorAll('quantity-input').forEach(q => { const inp = q.querySelector('input');
  const set = v => fetch('/cart/change.js', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({line: +inp.dataset.line, quantity: v})}).then(() => location.reload());
  q.querySelector('[name=plus]').onclick = () => set(+inp.value + 1);
  q.querySelector('[name=minus]').onclick = () => set(Math.max(0, +inp.value - 1));
  inp.onchange = () => set(+inp.value); });
</script>"""
            coupon = ""
            if self.mode in ("coupon_ok", "coupon_dead", "coupon_crash"):
                # store feature (14 Oct): a coupon box on the cart page (a discount app). coupon_ok says the code is
                # invalid; coupon_dead: Apply does nothing; coupon_crash: the app's endpoint answers 500, no message
                act = {"coupon_ok": 'document.getElementById("coupon-msg").textContent = "Invalid discount code: " + v;',
                       "coupon_dead": "",
                       "coupon_crash": 'fetch("/apps/discount/apply?code=" + encodeURIComponent(v)).then(r => { if (!r.ok) console.error("apply failed", r.status); });'
                       }[self.mode]
                coupon = ('<div class="coupon-box"><input type="text" name="discount" placeholder="Enter coupon code">'
                          '<button type="button" class="coupon-apply">Apply</button><div id="coupon-msg" class="message"></div></div>'
                          '<script>document.querySelector(".coupon-apply").onclick = () => { const v = document.querySelector('
                          '".coupon-box input").value; setTimeout(() => { ' + act + ' }, 300); };</script>')
            return self._send(200, page("Your Cart | Mock Store",
                                        f'{coupon}{rows}<p class="totals">Subtotal {rupees(total)}</p>'
                                        + (f'<button type="button" name="checkout" class="{TP_APPS[self.mode]}-checkout-btn" '
                                           f'data-items="{" | ".join(i["title"] + " × " + str(i["quantity"]) for i in items)}">Check out</button>'
                                           f'<script src="/static/{TP_APPS[self.mode]}-checkout.js"></script>' if self.mode in TP_APPS else
                                           '<form action="/checkout" method="post"><button type="submit" name="checkout">Check out</button></form>')
                                        + js),
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

    def _variants(self, p, catalog: bool = False):
        """The product's variants as this mode serves them (journey 22: the vase gains a sold-out size). catalog=True:
        as the store-wide /products.json lists them."""
        if self.mode == "plain_size_buttons" and p["handle"] == "ceramic-vase":
            return [dict(v, option2="Natural", title=f"{v['option1']} / Natural") for v in p["variants"]]
        if self.mode == "pack_of_two" and p["handle"] == "ceramic-vase":
            return PACK_VARIANTS
        if self.mode == "stale_catalog_all" and not catalog:
            return [dict(v, available=False) for v in p["variants"]]      # every product sold out since it was listed
        if self.mode == "stale_catalog" and p["handle"] == "ceramic-vase" and not catalog:
            # bombaytrooper.com (cycle 34): products.json still lists it in stock; its .js and its page say sold out
            return [dict(v, available=False) for v in p["variants"]]
        if self.mode in ("variant_sold_out", "variant_sold_out_buyable") and p["handle"] == "ceramic-vase":
            return p["variants"] + [SOLD_OUT_SIZE]
        if self.mode == "filter_stock_differs" and p["handle"] == "sold-out-lamp":
            # inventory policy 'continue': Shopify's own product data and page sell it; the collection's products.json
            # (which reads p["variants"] directly) still says sold out (antinorm.co / beminimalist.co, 11 Oct)
            return [dict(v, available=True) for v in p["variants"]]
        return p["variants"]

    def _size_pills(self, p, vid):
        """Dawn-style size pills: radio + label per variant. The theme script (left out in variant_switch_broken) puts
        the picked variant's id in the form, shows its price, sets ?variant= and disables the buy button for a sold-out
        size (variant_sold_out_buyable: the button stays enabled 'Add to cart')."""
        vs = self._variants(p)
        pills = ('<variant-radios class="product-form__input"><fieldset><legend>Size</legend>' + "".join(
            f'<input type="radio" id="size-{v["id"]}" name="Size" value="{v["option1"]}"{" checked" if v["id"] == vid else ""}>'
            f'<label for="size-{v["id"]}"{"" if v["available"] else " class=disabled"}>{v["option1"]}</label>' for v in vs)
            + '</fieldset></variant-radios>')
        if self.mode == "variant_switch_broken":
            return pills
        if self.mode in ("variant_no_url_push", "variant_price_late", "variant_form_late", "variant_id_on_submit"):
            return self._theme_pills(vs, vid)
        data = json.dumps([{"id": v["id"], "option1": v["option1"], "available": v["available"],
                            "price": f'₹{float(v["price"]):,.2f}'} for v in vs])
        block = "false" if self.mode == "variant_sold_out_buyable" else "true"
        if self.mode == "variant_pill_covered":      # dotandkey.com (12 Oct): a transparent swatch layer lies over the
            pills = pills.replace('<fieldset>', '<fieldset style="position:relative">').replace(  # pills; it forwards
                '</fieldset>', '<div class="swatch-layer" style="position:absolute;inset:0;z-index:5"></div></fieldset>') + (
                "<script>document.querySelector('.swatch-layer').addEventListener('click', ev => {"   # the click to the
                " const l = ev.currentTarget; l.style.pointerEvents = 'none';"                        # pill under it
                " const t = document.elementFromPoint(ev.clientX, ev.clientY); l.style.pointerEvents = '';"
                " if (t) t.click(); });</script>")
        return pills + ("<script>(() => { const V = " + data + ", BLOCK = " + block + ";\n"
                        "document.querySelectorAll('variant-radios input[type=radio]').forEach(r => r.addEventListener('change', () => {\n"
                        "  const v = V.find(x => x.option1 === r.value); if (!v) return;\n"
                        "  const f = document.getElementById('product-form-template__main'); f.querySelector('[name=id]').value = v.id;\n"
                        "  document.querySelector('main .price').textContent = v.price;\n"
                        "  const b = f.querySelector('[name=add]'); const no = BLOCK && !v.available;\n"
                        "  b.disabled = no; b.textContent = no ? 'Sold out' : 'Add to cart';\n"
                        "  history.replaceState(null, '', location.pathname + '?variant=' + v.id); })); })();</script>")

    def _theme_pills(self, vs, vid):
        """Journey 22 round 2: real themes whose variant switch WORKS but that the first variant_switch misread.
        variant_no_url_push (antinorm.co, veirdo.in): an app-built buy box with no <form>/id field (its button adds the
          picked variant from a script); pill labels carry the pack price ('Large ₹1,399.00'); the theme never pushes
          ?variant=, so the address keeps the ?variant=201 Radar opened it with.
        variant_price_late (theme-taste-demo): the form's id changes at once, the price ~1.5 s later (Dawn-family themes
          fetch the re-rendered price section).
        variant_form_late: section rendering replaces the whole product form (a NEW element) with the picked variant's
          id ~4 s after the click (a slow store).
        variant_id_on_submit: the price, the pill and ?variant= change at once, but the hidden id field is set only when
          the shopper adds to cart (onsubmit) -> contradictory, so 'not judged'."""
        price_in_label = self.mode == "variant_no_url_push"
        pills = ('<variant-radios class="product-form__input"><fieldset><legend>Size</legend>' + "".join(
            f'<input type="radio" id="size-{v["id"]}" name="Size" value="{v["option1"]}"{" checked" if v["id"] == vid else ""}>'
            f'<label for="size-{v["id"]}">{v["option1"]}'
            + (f'<br><small>₹{float(v["price"]):,.2f}</small>' if price_in_label else "") + '</label>' for v in vs)
            + '</fieldset></variant-radios>')
        data = json.dumps([{"id": v["id"], "option1": v["option1"], "price": f'₹{float(v["price"]):,.2f}'} for v in vs])
        act = {
            "variant_no_url_push": "window.__vid = v.id; price(v);",
            "variant_price_late": "f().querySelector('[name=id]').value = v.id; push(v); setTimeout(() => price(v), 1500);",
            "variant_form_late": "setTimeout(() => { const o = f().closest('product-form'); const n = o.cloneNode(true);"
                                 " n.querySelector('[name=id]').value = v.id; o.replaceWith(n); price(v); push(v); }, 4000);",
            "variant_id_on_submit": "price(v); push(v);",
        }[self.mode]
        return pills + ("<script>(() => { const V = " + data + ";\n"
                        "const f = () => document.getElementById('product-form-template__main');\n"
                        "const price = v => { document.querySelector('main .price').textContent = v.price; };\n"
                        "const push = v => history.replaceState(null, '', location.pathname + '?variant=' + v.id);\n"
                        "window.__vid = " + str(vid) + ";\n"
                        "document.querySelectorAll('variant-radios input[type=radio]').forEach(r => r.addEventListener('change', () => {\n"
                        "  const v = V.find(x => x.option1 === r.value); if (!v) return;\n  " + act + " })); })();</script>")

    def _pdp(self, p):
        if self.mode == "hidden_price":
            # somuchliving.com / tastycrafts.in (new30q/s, 15 Oct): the page shows the product, its options and a
            # disabled 'Unavailable' button, but no price anywhere near the name; the data holds a placeholder price
            return page(f"{p['title']} | Mock Store",
                        f'<h1>{p["title"]}</h1><img src="/cdn/shop/files/{p["handle"]}.svg" width="400" height="400" alt="">'
                        '<p>Delivery in 3 weeks. Free design customisation.</p><p>Variant sold out or unavailable</p>'
                        '<button type="button" disabled>Unavailable</button>'
                        '<div style="height:900px"></div><section class="related"><h2>You may also like</h2>'
                        '<p>Another piece Rs. 4,500.00</p></section>')
        if self.mode == "price_on_request":
            body = (f'<h1>{p["title"]}</h1><img src="/cdn/shop/files/{p["handle"]}.svg" width="400" height="400" alt="">')
            if p["handle"] == "wooden-spoon-set":     # banjarakasuti.com: no price, no buy, a store-wide notice
                body = ('<div class="announcement">We are no longer accepting online orders. Visit us at our store.</div>'
                        + body + '<p>Hand-carved, made to share.</p>')
            else:
                body += ('<p>A compact, hand-finished piece.</p><button type="button" class="btn">Price on Request</button> '
                         '<button type="button">Chat with us</button>')
            return page(f"{p['title']} | Mock Store", body)
        if self.mode == "marketplace_only":
            # antesports.com (new30g) / beyondsnack.in (new30e): a brand catalog on Shopify, no price and no cart on the
            # product page, only 'Buy on Amazon / Flipkart' links; products.json still carries prices
            return page(f"{p['title']} | Mock Store", f'<h1>{p["title"]}</h1><img src="/cdn/shop/files/{p["handle"]}.svg" '
                        'width="400" height="400" alt=""><p>A compact, hand-finished piece for everyday use.</p>'
                        '<div class="buy-links"><a href="https://www.amazon.in/dp/B0MOCK">Buy on Amazon</a> '
                        '<a href="https://www.flipkart.com/mock/p/itm1">Buy on Flipkart</a></div>'
                        if p["handle"] != "wooden-spoon-set" else   # beyondsnack 'Diwali Box': the heading, no links at all
                        f'<h1>{p["title"]}</h1><img src="/cdn/shop/files/{p["handle"]}.svg" width="400" height="400" alt="">'
                        '<h2>Shop Now On:</h2><p>A festive assortment, made to share.</p>')
        price = "0" if (self.mode == "broken_price" and p["handle"] == "ceramic-vase") else p["price"]
        ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": p["title"],
                         "image": [f"/cdn/shop/files/{p['handle']}.svg"],
                         "offers": {"@type": "Offer", "price": price, "priceCurrency": "INR",
                                    "availability": "https://schema.org/InStock"}})
        available = any(v["available"] for v in self._variants(p))
        vid = next((v["id"] for v in self._variants(p) if v["available"]), p["variants"][0]["id"])
        if not available and self.mode == "soldout_buyable":
            # journey 24: the theme forgot its sold-out state: an ENABLED 'Add to cart' for the sold-out variant, no
            # 'sold out' words anywhere (Shopify still refuses the add with 422)
            buy = (f'<product-form class="product-form"><form id="product-form-template__main" action="/cart/add" method="post" '
                   f'onsubmit="event.preventDefault();addToCart({vid})"><input type="hidden" name="id" value="{vid}">'
                   f'<button type="submit" name="add">Add to cart</button></form></product-form>')
        elif not available:
            buy = '<button disabled>Sold out</button>'
        elif self.mode == "pack_of_two" and p["handle"] == "ceramic-vase":
            ids = json.dumps({v["option1"]: v["id"] for v in PACK_VARIANTS})
            full = json.dumps(dict(PACK_SIZES))
            group = lambda n, label: (f'<div class="product-option" data-group="{n}"><span>{label}:</span> ' + "".join(
                f'<button type="button" class="size-pill" onclick="packPick({n}, this)">{short}</button>'
                for short, _ in PACK_SIZES) + '</div>')
            buy = (group(0, "Size") + group(1, "Second shirt (Size)") +
                   '<product-form class="product-form"><form id="product-form-template__main" action="/cart/add" method="post" '
                   'onsubmit="event.preventDefault();addToCart(this.querySelector(&quot;[name=id]&quot;).value)">'
                   '<input type="hidden" name="id" value=""><button type="submit" name="add" disabled>Add to cart</button>'
                   f'</form></product-form><script>const PACK_IDS = {ids}, PACK_FULL = {full}, PACK = [null, null];'
                   'function packPick(n, b) { b.parentElement.querySelectorAll("button").forEach(x => x.classList.remove("active"));'
                   ' b.classList.add("active"); PACK[n] = PACK_FULL[b.innerText.trim()];'
                   ' if (PACK[0] && PACK[1]) { const f = document.getElementById("product-form-template__main");'
                   ' f.querySelector("[name=id]").value = PACK_IDS[PACK[0] + " / " + PACK[1]];'
                   ' f.querySelector("[name=add]").disabled = false; } }</script>')
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
  if (window.NO_DRAWER) { location.href = '/cart'; return; }
  let lines = '<div class="cart-item">' + item.product_title + '</div>';
  if (window.DRAWER_MODE) {      // journey 18: Dawn-like drawer lines + subtotal ('stale': one more of each than the cart)
    const c = await (await fetch('/cart.js')).json(), k = window.DRAWER_MODE === 'stale' ? 1 : 0;
    const fmt = p => '₹' + (p / 100).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    lines = c.items.map((i, n) => '<div class="cart-drawer__item"><a href="/products/' + i.handle + '">' + i.product_title
      + '</a> <span class="price">' + fmt(i.price) + '</span> <input type="number" class="quantity__input" name="updates[]" value="'
      + (i.quantity + k) + '" data-index="' + (n + 1) + '" aria-label="Quantity"></div>').join('')
      + '<div class="totals"><h3 class="totals__total">Subtotal</h3><p class="totals__total-value">'
      + fmt(c.items.reduce((s, i) => s + i.price * (i.quantity + k), 0)) + '</p></div>'; }
  const d = document.createElement('cart-drawer'); d.className = 'drawer animate active';
  d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:50';
  d.innerHTML = '<div class="drawer__inner" style="position:absolute;right:0;top:0;width:360px;height:100%;background:#fff;padding:16px">'
    + '<h2>Your cart</h2>' + lines
    + '<button type="button" name="checkout">Check out</button></div>';
  document.body.appendChild(d); }
</script>"""
        body = (f'{quick}<h1>{p["title"]}</h1><img src="/cdn/shop/files/{p["handle"]}.svg" width="300" height="300" alt="">'
                f'<div class="price">₹{float(p["price"]):,.2f}</div>{buy}')
        if self.mode in VARIANT_MODES and available and len(self._variants(p)) > 1 and 'id="product-form-template__main"' in buy:
            pills = self._size_pills(p, vid)
            if self.mode == "variant_no_url_push":        # an app-built buy box: no form, no id field
                body = body.replace(buy, pills + '<div class="app-buy-box"><button type="button" class="app-atc" '
                                    'onclick="addToCart(window.__vid)">Add to cart</button></div>', 1)
            elif self.mode == "variant_id_on_submit":     # the theme sets the id from the picked pill on add to cart
                live = buy.replace(f"addToCart({vid})", "const o = document.querySelector('variant-radios input:checked'); "
                                   "this.querySelector('[name=id]').value = document.getElementById(o.id).id.slice(5); "
                                   "addToCart(this.querySelector('[name=id]').value)", 1)
                body = body.replace(buy, pills + live, 1)
            else:
                body = body.replace(buy, pills + buy, 1)       # journey 22
        if self.mode == "sticky_buy_only":
            sticky = buy.replace('id="product-form-template__main"', 'id="sticky-form"')
            body += ('<div style="height:2400px"></div><div id="sticky-atc" style="display:none;position:fixed;left:0;right:0;'
                     'bottom:0;background:#fff;padding:10px;z-index:20">' + sticky + '</div>'
                     '<style>#product-form-template__main button{display:none}</style>'
                     '<script>addEventListener("scroll", () => { if (scrollY > 300) document.getElementById("sticky-atc")'
                     '.style.display = "block"; });</script>')
        if self.mode == "span_add_to_cart":
            # costosoitaliano.com (new30v): hidden, empty cart form; the theme's buy control is a
            # <span class="custom-add-to-cart-btn">Add to Cart</span> far below the name, with no product handle on it
            body = body.replace(buy, f'<form action="/cart/add" method="post" style="display:none"><input type="hidden" '
                                     f'name="id" value="{vid}"></form><div style="height:700px"><p>Hand-finished. Ships in 3 days.</p></div>'
                                     f'<div class="custom-actions"><span class="custom-add-to-cart-btn" onclick="addToCart({vid})" '
                                     f'style="display:inline-block;padding:10px;background:#222;color:#fff;cursor:pointer">Add to Cart</span></div>')
        if self.mode == "disabled_form_enabled_bar":
            # arabianaroma.in (new30v): the form's own button stays disabled; an enabled sticky 'Add to cart' is the real one
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="submit" name="add" disabled>Add to cart</button>', 1)
            body += (f'<div class="sticky-atc" style="position:fixed;bottom:0;left:0;right:0;background:#fff;padding:8px">'
                     f'<button type="button" class="sticky-atc__btn" onclick="addToCart({vid})">Add to cart</button></div>')
        if self.mode == "gift_only_product":
            # nubronicforte.com (new30t): the 'Performance Kit' gift's page: a hidden, empty cart form, no buy control
            body = body.replace(buy, f'<form action="/cart/add" method="post" style="display:none"><input type="hidden" '
                                     f'name="id" value="{vid}"></form><p>Complimentary with every order. Auto-added at checkout.</p>')
        if self.mode == "cod_form_only":
            # shopkara.store (new30p/t): a hidden, buttonless cart form; the only buy control is a COD order-form app's
            # 'Order Now - Cash on Delivery' popup button
            body = body.replace(buy, f'<form action="/cart/add" method="post" style="display:none"><input type="hidden" '
                                     f'name="id" value="{vid}"></form><div class="es-popup-button" style="cursor:pointer;'
                                     f'padding:10px;background:#0a0;color:#fff;width:260px">Order Now - Cash on Delivery</div>')
        if self.mode == "hidden_form_app_button":
            # shopkara.store (new30p) / nubronicforte.com (new30t): the product's own cart form is there but hidden and
            # holds no button; the buy control a shopper uses is an app's element outside the form
            body = body.replace(buy, f'<form action="/cart/add" method="post" style="display:none"><input type="hidden" '
                                     f'name="id" value="{vid}"></form><div class="pdp-addtobag-btn active" '
                                     f'data-product-handle="{p["handle"]}" onclick="addToCart({vid})" style="cursor:pointer;'
                                     f'padding:10px;background:#222;color:#fff;width:200px"><span class="atc-text">ADD TO CART</span></div>')
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
        if self.mode in ("pincode_ok", "pincode_dead", "pincode_covered", "pincode_rerender", "pincode_slow") and available:
            # store feature (14 Oct): a delivery-pincode checker next to the buy button. pincode_ok answers after a
            # short app call; pincode_dead: the CHECK button is wired to nothing (the app's script failed to load)
            js = ('<script>document.addEventListener("click", e => { if (!e.target.closest(".pincode-btn")) return; const v = document.querySelector('
                  '".pincode-check input").value; setTimeout(() => { document.getElementById("pin-msg").textContent = '
                  '/^\\d{6}$/.test(v) ? "Delivery by Thu, 16 Oct · COD available for " + v : "Please enter a valid pincode"; }, 300); });</script>'
                  if self.mode in ("pincode_ok", "pincode_covered", "pincode_rerender") else
                  # boat-lifestyle (15 Oct): 'Checking servicability ...' for a long time, then the answer
                  '<script>document.addEventListener("click", e => { if (!e.target.closest(".pincode-btn")) return; const m = document.getElementById("pin-msg"); '
                  'm.textContent = "Checking servicability ..."; setTimeout(() => { m.textContent = "Delivery by Sat, 18 Oct"; }, 11000); });</script>'
                  if self.mode == "pincode_slow" else "")
            widget = ('<div class="pincode-check"><label>Check delivery</label><input type="text" name="pincode" '
                      'placeholder="Enter pincode" maxlength="6"><button type="button" class="pincode-btn">CHECK</button>'
                      '<div id="pin-msg" class="delivery-estimate"></div></div>' + js)
            if self.mode == "pincode_covered":
                # mcaffeine / plumgoodness / boat-lifestyle (first real run, 14 Oct): the checker sits far down the page
                # under a sticky buy bar, and a newsletter popup opens a few seconds AFTER the page was read and its
                # first popups closed -> the plain click timed out for 8 s
                widget = ('<div style="height:2400px"></div>' + widget +      # no footer: the page ends under the bar
                          '<div class="sticky-atc" style="position:fixed;left:0;right:0;bottom:0;height:140px;background:#fff;'
                          'z-index:30;padding:10px">Sticky buy bar <button type="button">Add to cart</button></div>'
                          + self.OVERLAYS["newsletter_popup"].replace("}, 400);", "}, 2500);"))
            if self.mode == "pincode_rerender":
                # mcaffeine / plumgoodness / boat-lifestyle (15 Oct re-test): an app-rendered widget redraws itself
                # every few hundred ms (new nodes, typed value kept): anything Radar marked on the old nodes is gone
                widget += ('<script>setInterval(() => { const w = document.querySelector(".pincode-check"); if (!w) return; '
                           'const v = w.querySelector("input").value, m = w.querySelector("#pin-msg").textContent; '
                           'const n = w.cloneNode(true); n.querySelector("input").value = v; n.querySelector("#pin-msg").textContent = m; '
                           # a real app draws fresh nodes from its own state: nothing anyone set on the old ones survives
                           '[n, ...n.querySelectorAll("*")].forEach(e => [...e.attributes].forEach(a => { if (a.name.startsWith("data-radar")) e.removeAttribute(a.name); })); '
                           'w.replaceWith(n); }, 250);</script>')
            body += widget
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
        if self.mode in BUY_NOW_MODES and available:
            # journey 33: Shopify's dynamic checkout slot inside the product form; its script renders 'Buy it now' a moment
            # after load (buy_now_dead: the button is drawn, its click handler never attached)
            go = "" if self.mode == "buy_now_dead" else (
                "b.onclick = () => { const id = document.querySelector('#product-form-template__main [name=id]').value; "
                "location.href = '/checkouts/cn/buynow?variant=' + id; };")
            body = body.replace('<button type="submit" name="add">Add to cart</button>',
                                '<button type="submit" name="add">Add to cart</button><div data-shopify="payment-button" '
                                'class="shopify-payment-button"></div>', 1)
            body += ("<script>setTimeout(() => { const s = document.querySelector('.shopify-payment-button'); if (!s) return; "
                     "const b = document.createElement('button'); b.type = 'button'; "
                     "b.className = 'shopify-payment-button__button shopify-payment-button__button--unbranded'; "
                     "b.textContent = 'Buy it now'; " + go + " s.appendChild(b); }, 600);</script>")
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
        if self.mode in ("cart_drawer", "cart_drawer_stale"):         # journey 18
            script += f"<script>window.DRAWER_MODE = '{'stale' if self.mode == 'cart_drawer_stale' else 'full'}';</script>"
        if self.mode == "cart_no_drawer":
            script += "<script>window.NO_DRAWER = true;</script>"
        if p.get("redirect"):
            script += f"<script>setTimeout(() => location.replace('{p['redirect']}'), 150)</script>"
        return page(f"{p['title']} | Mock Store", body, f'<script type="application/ld+json">{ld}</script>{script}')

    @staticmethod
    def _change(cid, line: int, qty: int):
        items = CARTS.get(cid, [])
        if 1 <= line <= len(items):
            if qty <= 0:
                items.pop(line - 1)
            else:
                items[line - 1]["quantity"] = qty

    def do_POST(self):
        if self.mode == "signed_only" and not self._signed_ok():
            return self._send(429, "Too Many Requests", "text/plain")
        u = urlparse(self.path)
        cid, new = self._cart()
        if u.path == "/cart/change.js":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if body.get("id") and not body.get("line"):      # Shopify also takes the variant id instead of a line number
                body["line"] = next((n for n, i in enumerate(CARTS[cid], 1) if str(i["variant_id"]) == str(body["id"])), 0)
            self._change(cid, int(body.get("line") or 0), int(body.get("quantity") or 0))
            items = CARTS[cid]
            return self._json({"items": items, "item_count": sum(i["quantity"] for i in items)}, set_cart=new)
        if u.path == "/cart/add.js":
            if self.mode == "cart_broken":
                return self._json({"status": 500, "description": "cart service down"}, 500)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            vid = int(body.get("id"))
            p = next((x for x in PRODUCTS for v in x["variants"] if v["id"] == vid), None)
            if not p:
                return self._json({"status": 404}, 404)
            if not next(v["available"] for v in p["variants"] if v["id"] == vid) and self.mode != "soldout_oversell":
                # like Shopify: a sold-out variant is refused (soldout_oversell: 'continue selling when out of stock')
                return self._json({"status": 422, "message": "Cart Error",
                                   "description": f"The product '{p['title']}' is already sold out."}, 422)
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
