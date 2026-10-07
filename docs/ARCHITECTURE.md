# Radar framework: architecture (living document)

Rule for this file: it says what works, what is proven, what is not, and what hurts. No bluff.
It is updated with every change.

Last updated: 7 Oct 2026 · Framework v0.16. Bench 11 (v0.15, 7 Oct 08:32 IST): 27 healthy / 4 degraded / 0 down; thefunclab fix held. No new Radar mistake in what Radar judged; 2 transient patterns (data request miss, robots.txt no answer), each on 2 stores, fixed in v0.16 (4p). **Rules from 7 Oct: the 36-store list is now a regression set only; fixes only for patterns seen on 2+ stores; R1 closes Fri 9 Oct; the real score comes from 30 never-seen stores (stores/new30.txt) and a seeded-bug store.**

---

## 1. What it does in one line

`python3 -m radar scan moxiebeauty.in` → finds out what the store is, discovers its pages and
products, **generates** test suites, runs them in a real browser with retries and self-healing
locators, stores everything per site, and writes an interactive report.

You give it a URL. Nothing else is required.

## 2. The pipeline

```
 URL
  │
  ▼
 ① DISCOVERY  radar/discovery/discover.py
    robots.txt → homepage (real browser) → platform detection → nav links
    → collections (/collections.json, else nav) → products (/products.json, else page links
    + /products/<handle>.js) → search form
  │  SiteMap  → data/sites/<site_id>/sitemap.json
  ▼
 ② GENERATOR  radar/generate/builder.py
    deterministic rules turn the SiteMap into suites:
    journey · smoke · catalog · product · cart · search · health
  │  Suites   → data/sites/<site_id>/suites.json
  ▼
 ③ EXECUTOR   radar/runner/executor.py
    each test case runs in a FRESH browser context
    fail → retry (fresh context) → 2 of 3 failures = confirmed
    robots.txt says no → "blocked" (not a site bug, no retry)
  │       uses ④ CHECKS and ⑤ HEALING
  ▼
 ⑥ STORAGE    radar/core/storage.py   SQLite rows + per-site folders
 ⑦ REPORTS    radar/reporting/        report.html per run, index.html per site
```

## 3. Folder map

```
radar/
  cli.py                  python3 -m radar scan|bench|llm-check|sites|open
  llmcheck.py             12 known failure cases to score the configured LLM (model choice on evidence)
  __main__.py             entry point (the __main__ guard the parallel bench needs)
  bench.py                run a store list in parallel (spawned processes), one table
  core/
    config.py             settings, URL normalising, site_id, .env loader (API keys)
    models.py             SiteMap, Suite, TestCase, StepResult, AttemptResult, CaseResult, RunResult
    browser.py            Chromium, fresh context per attempt, tracing, screenshots, robots-aware goto,
                          settle() for client-side redirects
    overlays.py           cookie banners (declined), newsletter/location popups (closed, also in
                          iframes), age gates (BLOCKED)
    robots.py             robots.txt rules
    storage.py            SQLite + per-site folders, incidents, locator cache, retention
  discovery/
    detect.py             Shopify or not (2+ markers), checkout app, access (open/password/bot_blocked)
    shopify_data.py       parsers: products.json, product .js, JSON-LD/meta, cart.js, cart/add bodies,
                          cart diff (assess_add)
    discover.py           URL -> SiteMap
  generate/builder.py     SiteMap -> Suites
  checks/library.py       the checks (journey, page health, links, collection, product, add to cart,
                          search, meta tags, 404) + step engine (hard vs soft) + click engine (PICK_JS)
  healing/
    locator.py            intent-based self-healing locators
    llm.py                provider-agnostic LLM client (OpenAI default / Anthropic / OpenAI-compatible / none)
    triage.py             LLM review of a confirmed failure: store problem or Radar's mistake
  runner/
    confirm.py            retry and confirm rules
    executor.py           the whole pipeline
  reporting/
    html.py, template.py  self-contained interactive reports (run + site index)
    bench_html.py         bench table page
tests/
  test_units.py, test_shopify_data.py, test_detect_robots.py   49 unit tests, no browser
  test_e2e.py             21 end-to-end runs in real Chromium against the mock store (incl. a 3-store bench)
  mockstore/server.py     fake Shopify store with 15 modes
stores/bench.txt          the bench store list
sites/                    OPTIONAL per-site overrides (<site_id>.yml)
data/                     everything Radar writes (git-ignored)
```

## 4. The generated suites

| Suite | Severity | Check | What it proves |
|---|---|---|---|
| journey | critical | shopper_journey | **End to end in ONE session by clicking, like a shopper:** homepage → menu link to a collection (opens the menu if needed) → product card → product page → add to cart (cart count goes up) → cart → checkout button visible. Checkout never clicked. With `--no-cart` it stops at the product page. |
| smoke | critical / major | page_health, links_resolve | Homepage is up, has a title, images load, no JS errors, fast enough; each menu page (up to 10) opens in the browser and shows content |
| catalog | major | collection_page | Each sampled collection loads and lists products |
| product | major | product_page | Title, price > 0 and image from structured data; visible price (soft); buy button visible and enabled |
| cart | critical | add_to_cart | Click add to cart → `/cart.js` contains that exact variant → cart page → checkout button visible. **Checkout is never clicked.** |
| search | major | search_results | Searching a word from a real product title returns products |
| health | minor | meta_tags, not_found | Title/description/canonical/og tags (soft); a missing page returns real 404 |

**Every page load must show content:** a page with under 40 characters of visible text and no images fails as "rendered blank" (a 200 status alone is not enough).

Hard step fails the test. Soft step only warns (theme-dependent checks, or real but not
shopper-blocking issues). Products: in-stock first; sold-out items are never used for cart.

**Run verdict:** `down` if any critical test is confirmed failing; `degraded` if anything failed
or was flaky; `healthy` otherwise. Before any test runs: `blocked` (password page, bot challenge,
robots.txt disallows the site, age gate on a critical test), `unsupported` (not Shopify),
`unreachable` (domain does not resolve, or sends visitors to a different site).

## 4b. Assertions (what "pass" actually means)

Every step records explicit assertions: **what, expected, actual, ✓/✕**. The report shows them as
a table under each step. A failure message is always `what: expected X, got Y`.

The add-to-cart assertions (journey and cart test), all checked against the store's own data:

| Assertion | Source of truth |
|---|---|
| product identified | `/products/<handle>.js`: product id, title, variants, prices |
| title / selected-variant price / main image shown on the page | product data vs visible page |
| buy button is the product's OWN button | the cart/add form whose `id` input is one of this product's variants, not inside a product card / quick-add / recommendation |
| variant the page sent to `/cart/add` | the browser's actual request body |
| cart item count went up by exactly 1 | `/cart.js` before vs after |
| product added = product opened | `/cart.js` diff, by variant id |
| quantity added = 1, unit price = variant price | `/cart.js` line vs product data |
| no other paid product added | `/cart.js` diff (free gifts are reported as a warning, not a failure) |
| cart total went up by the product price | `/cart.js` total_price (warning only: automatic discounts change totals legitimately) |
| cart (drawer or page) lists the product; checkout button visible + enabled | the page; checkout never clicked |

**Why this exists:** on 3 Oct, on moxiebeauty.in, Radar's old locator ("first add-to-cart
button in the page") clicked a *quick-add button on a product card* that sits before the main
form in the DOM. Product A was open, product B was added, and the journey still passed
because it only checked "cart count went up". Proven from the Playwright trace (locator resolved
to `quick-add-template--…-submit`, request body `id=51469498810690`). It was Radar's bug, not
Moxie's. The mock store now reproduces that layout on every product page, and a `wrong_variant`
mode proves a store that really sends the wrong product is caught and named.

## 4c. Built for every Shopify store, not one

Nothing in `radar/` is store-specific (no store names, no per-store selectors). Truth comes from
endpoints every Shopify Online Store has: `/products.json`, `/products/<handle>.js`, `/cart.js`,
`/cart/add`. Each real-store finding became a GENERAL rule plus a mock-store mode that keeps it tested:

| Pattern (where first seen) | General rule | Mock mode |
|---|---|---|
| Quick-add buttons on product cards (moxiebeauty.in; Dawn and most OS 2.0 themes) | only the form whose variant belongs to the open product; cards/carousels/upsells excluded at every locator rung | every PDP |
| Cart drawer covering the page (Dawn default) | detect the open drawer, assert inside it | every PDP |
| Free gift auto-added (gift-with-purchase apps) | extra Rs 0 item = warning; extra paid item = failure | `free_gift` |
| Rs 0 sample products that JS-redirect | excluded from product/cart tests; page waits for client-side redirects | every store |
| Newsletter / cookie / location popups | closed before clicks; cookies DECLINED, never accepted; never subscribes | `newsletter_popup`, `cookie_banner` |
| Size/colour must be chosen first | picks the first in-stock variant's options (select, radio, swatch) and asserts the form now carries it | `variant_required` |
| Buy button not in a Shopify form (custom JS) | no guessing: the cart diff decides | `renamed_button`, `obscure_button` |
| Age gate | never confirmed for the shopper → BLOCKED | `age_gate` |
| Password-protected store | BLOCKED, nothing tested | `password` |
| Bot challenge (Cloudflare etc.) | BLOCKED, no evasion; store can allowlist the User-Agent | unit-tested |
| Not Shopify / headless | UNSUPPORTED | `not_shopify` |

Detected per store and shown everywhere: **theme** (`window.Shopify.theme.schema_name`, e.g. Dawn),
**checkout app** (Shopify / GoKwik / Shopflo / Shiprocket Fastrr / Razorpay Magic / Simpl / Zecpe),
**access** (open / password / bot_blocked / unreachable).

**Honest limit:** no tool covers literally 100% of Shopify. The promise Radar makes is that it
never blames a store for Radar's own limitation: anything it cannot test is reported as BLOCKED or
UNSUPPORTED with the reason. The bench (below) is how that promise is measured.

## 4d. Bench

`python3 -m radar bench stores/bench.txt [--quick] [--workers N]` runs Radar on every store in the
list (N in parallel, spawned processes), and writes `data/bench/<time>/bench.html` + `bench.json`:
one row per store with verdict, theme, checkout app, a ✓/~/✕ per suite and, on click, each failing
test's step and reason with a link to the full report. The loop: run the bench, sort every failure
into "Radar's bug" vs "real store issue", fix Radar's bugs as general rules (with a mock mode),
rerun, until Radar's own false failures across the list are zero.

`stores/bench.txt` holds Shopify's theme demo stores (cart flow on: no merchant affected), the
BugRadar Phase-3 prospects and other Indian D2C stores (read-only). thesleepcompany.in and
the owner's private exclusion list are deliberately excluded.

## 4e. First bench (36 stores, 4 Oct): root-cause analysis

10 stores came back DOWN. Every DOWN was traced from Radar's own evidence (run.json assertions +
Playwright trace logs/DOM snapshots). **All 10 were Radar's bugs, not store bugs.**

| Radar bug | Stores | Evidence | General fix |
|---|---|---|---|
| Product picker looked at only the first 30 links; all were hidden mega-menu links | plumgoodness | trace: picker never reached a visible card | click engine considers every candidate |
| Clicked elements covered by a chat bubble / hero image, or off-screen carousel slides | suta, rarerabbit, mcaffeine | trace: "WhatsApp chat widget intercepts pointer events", "element is outside of the viewport" | scroll to centre + hit-test (`elementFromPoint`) before choosing |
| Clicked a disabled menu parent (`aria-disabled="true"`) | beminimalist | trace | disabled / inert / aria-hidden elements skipped |
| Opened the wrong dropdown, then gave up | giva | trace: clicked "Product Type" summary | open each header dropdown in turn |
| Required a collection link on the homepage | theme-origin, theme-spotlight, snitch | Origin snapshot: only collection link is inside the empty cart drawer | collection step is soft; journey goes home → product |
| Used Shopify's hidden `frontpage` collection | theme-origin | catalog: 0 products | `frontpage` excluded |
| Popup inside an iframe could not be closed | bummer | trace: `popup-iframe-open-…` intercepts pointer events | close button found inside the frame; Escape fallback |
| Required the page title to equal the catalog title exactly | plum, snitch, rarerabbit | "50 ml" vs "50ml", short display names | hard: a product title is visible; soft: it matches the catalog |
| Tested a gift-app clone product | plumgoodness | handle `…_sca_clone_freegift` | clones, gift cards, samplers excluded |
| A page served with no network response counted as broken | thefunclab, plumgoodness (`/`) | status `null` | Navigation Timing status, else content check |
| Searched the brand name ("rare") | rarerabbit | search | search word from the product handle, brand words excluded |
| "robots.txt blocks the whole site" reported as an error | vaaree | note | verdict BLOCKED |

**Real store findings in that run:** foxtale.in returns HTTP 200 for missing product pages (soft-404),
plus the warnings (JS errors, missing og:image, etc.).
**Wrong entries in the store list (mine):** antinorm.com → redirects to zombo.com, peepbeauty.com →
domain for sale, supply6.com → does not resolve. Radar now says "wrong / dead URL" for these.

**Search and robots.txt:** many stores disallow `/search` in robots.txt (it stops crawlers indexing
endless result pages). Radar now treats a search typed into the store's own search box as a shopper
action, like the cart (`search_is_shopper_flow`, default on). Everything crawl-like still respects robots.txt.

Each fix has a mock-store reproduction (`hostile`, `no_collection_links` modes) so it stays fixed.
**Not yet proven:** that the fixes clear those 10 real stores. The next bench run is that proof.

## 4f. Second bench (33 stores, 4 Oct, 11:09 UTC, v0.6): results and OPEN bugs

Run on Anmol's Mac with v0.6 (files landed 11:07 UTC). The three wrong domains were commented out
(36 → 33 stores). Result file: `data/bench/20261004T110900Z/bench.json` on the Mac.

| | Bench 1 (36 stores, v0.5) | Bench 2 (33 stores, v0.6) |
|---|---|---|
| Healthy | 19 | 21 |
| Degraded | 2 | 2 |
| Down | 10 | **7** |
| Blocked / unsupported / unreachable / error | 0 / 3 / 0 / 2 | 1 / 1 / 1 / 0 |

**What the v0.6 fixes did to bench 1's 10 DOWN stores:**
- 4 now healthy: suta, mcaffeine, beminimalist, giva.
- 1 was a list problem: rarerabbit.in now redirects to thehouseofrare.com, reported as wrong/dead URL.
- 5 still down, some for new reasons: plumgoodness, theme-origin, theme-spotlight, snitch, bummer.
- **2 newly down because of a check I added in v0.6** (a regression, mine): theme-publisher and
  palmonas. They fail "product title heading visible".

**The 7 DOWN stores, each failure as Radar reported it.** None of these has been shown to be a real
store bug. Treat all 7 as Radar false failures until a trace says otherwise.

| Store (theme) | Failing test → step | Radar's message | Suspected cause (NOT yet verified from trace/DOM) |
|---|---|---|---|
| theme-publisher (Dawn-based) | journey + product → shows_title_price_image | product title heading visible: got none | heading check too narrow, see A |
| palmonas (Unsen) | journey → shows_title_price_image | same | A |
| plumgoodness (Focal) | journey + product → shows_title_price_image | same | A |
| snitch (Impulse) | product → shows_title_price_image | same | A |
| snitch | journey → click_into_product | product card a shopper can click: none on / | see B |
| bummer (custom "Bummer 3.0") | product → shows_title_price_image | same heading failure | A |
| bummer | journey → click_into_product | product card: none on /collections/men | B |
| theme-origin (Origin) | journey → click_into_product | product card: none on / | B (Origin's homepage has no collection link, so the journey goes home → product) |
| theme-spotlight (Spotlight) | journey → click_into_product | could not click: `</div>` subtree intercepts pointer events | see C |

**A. Heading check (5 stores).** `_pdp_assertions` looks for `h1, [class*=product][class*=title],
[class*=product-name]` that is visible and NOT inside `header, footer, nav, [class*=card],
[class*=recommend]`. Two likely ways that misses a real title: themes that wrap the product title
in their own `<header>` element (`closest('header')` matches any `<header>`, not just the site
header), and wrappers whose class contains "card" (`[class*=card]` also matches e.g. `product-card`
layouts on the main product). Fix direction: find the main product section first (the form whose
variant id belongs to this product, as for the buy button), look for the title inside or near it,
only exclude the SITE header (`body > header`, `#shopify-section-header`, `[class*=header-group]`),
and fall back to the catalog title text appearing in a large font. Prove it with a mock mode
copying each failing markup.

**B. Product card "none" (3 stores).** `PICK_JS` filters every `/products/` link: visible, not in
cart drawer or quick-add, not `aria-hidden`/`inert`, then scrolls each one to the centre and
hit-tests it. "none" means every candidate failed one of those. Not yet known which filter. First
step for the next session: open the trace for theme-origin and snitch, run PICK_JS by hand on the
DOM snapshot, log which filter removed each card.

**C. Click intercepted after a passing hit-test (theme-spotlight).** `PICK_JS` hit-tests at
`top + min(height/2, 40)` px, but Playwright's `click()` clicks the element's CENTRE. On a tall
card link those are different points, and another `<div>` (image overlay, badge, quick-view
layer) can cover the centre. Likely fix: click at the same point that was hit-tested
(`click(position=...)`), or hit-test the centre too. Verify against the trace first.

**D. Search relevance (foxtale.in, degraded, not down).** Searching "plumping" (word from a product
handle) returned 8 products, none mentioning the word. Foxtale has no search form on the homepage,
so Radar used Shopify's `/search`. Not yet known whether this is a real relevance problem or Radar's
relevance check being too literal (e.g. "plumper" vs "plumping"). Check by hand before reporting.

**Real store findings in bench 2** (not Radar's bugs, checked from the run data):
- soulflower.in: a missing page redirects to `/` with HTTP 200 (soft-404).
- foxtale.in: a missing page returns HTTP 200 (soft-404).
- soulflower.in's journey was flaky (failed once, passed on retry); cause not looked at.

**Store list fixes still to do:** rarerabbit.in → thehouseofrare.com; real domains for Supply6,
Antinorm, Peep Beauty (Anmol to supply). Vaaree stays: robots.txt blocks Radar (expected BLOCKED).
thewholetruthfoods stays as the not-Shopify negative control.

**Checkout apps seen across the 33 stores:**
- Shopify's own checkout: 12 stores.
- GoKwik only: 9.
- GoKwik combined with another app: 7. These are Shopflo (3), Shiprocket Fastrr (2), Shopflo + Razorpay Magic (1), and Razorpay Magic + Simpl (1).
- Shopflo only: 2.
- Not detected: 3. These are the blocked, not-Shopify and wrong-URL stores, where nothing was loaded.

GoKwik is on 16 of the 33 stores, which is why checkout level 2 (up to OTP) matters for India.

**Exit rule for this phase (unchanged):** rerun the bench after every fix batch until Radar's own
false failures across the list are zero. Each fix is a general rule plus a mock mode, never a
store-specific selector.

## 4g. Bench 2 root causes, proven from the traces (5 Oct, v0.7)

Method: each failing page was rebuilt OFFLINE from its Playwright trace (DOM snapshot + the trace's
own CSS/images, no network) and Radar's exact JavaScript was replayed against it. A new mock mode
copies each pattern; the old v0.6 code was run against the new modes and fails with the SAME
message as on the real store; v0.7 passes them.

| Store | Real cause (from the trace) | Whose bug | Fix (general rule) | Mock mode |
|---|---|---|---|---|
| theme-publisher | Product name is an `<h2>` rich-text block; no `<h1>` on the page | Radar | Name found anywhere it is shown: every visible text matching the catalog title is scored (heading + font size), not just `h1`/"title" classes | `title_layouts` |
| palmonas | Name is a 12px `<span>` in `div.px-product-header` | Radar | same | `title_layouts` |
| bummer (PDP) | `<body class="... card-hover-effect-none">`: the "inside a card" exclusion matched `<body>`, so every title was excluded | Radar | "Another product's card" = small box mostly covered by a link to a DIFFERENT product; class names never used; `<body>` never excludes | `title_layouts` |
| plumgoodness | Radar tested a Rs 1 freebie SKU from `/products.json`; its page says "The product is currently unavailable" | Radar (test data) | Token-priced products (≤ 10% of the median price and ≤ 50) are skipped; products listed in the browsed collections are tested first | healthy (`gift-pouch`) |
| snitch | snitch.co.in moved to snitch.com; product pages show only an "upgrade" banner | **Store** (intentional move) | Correct FAIL; bench list now uses snitch.com | `no_title` |
| theme-spotlight | Hit-test passed near the card's top, Playwright then re-scrolled and clicked the centre, where a `<div>` sits | Radar | Click at the exact point that was hit-tested (`mouse.click(x, y)`), link itself preferred over its card | `card_layouts` |
| theme-origin | No collection link on the homepage, so Radar opened the menu drawer; the drawer stayed open and covered the product link | Radar | Fresh homepage after the collection search fails | `no_collection_links` (now with a drawer) |
| bummer (journey) | Does not reproduce offline (58 cards clickable in the snapshot): something live (JS) covered or animated the cards | Probably Radar | (1) a click inside the product's own card counts (image sliders over the link); (2) a second look after 1.2 s (entrance animations); (3) a "none" now says WHY per link ("62 links: 58 covered by div.swiper, 4 not visible") | `card_layouts` |

**Real store findings kept from bench 2:** soulflower.in and foxtale.in soft-404s; foxtale search
relevance still to check by hand.

**Still to prove on the Mac:** rerun the bench with v0.7. Expected: the 6 Radar causes above gone;
snitch.com/thehouseofrare.com/supplysix.com/antinorm.co/peepbeauty.in are new rows; bummer's journey is
either fixed or now explains itself.

## 4h. Third bench (36 stores, 5 Oct, v0.7.2 with gpt-5-mini triage): results and root causes

`data/bench/20261005T080226Z/` on the Mac. 33 testable stores: **25 healthy, 6 degraded, 2 down**
(bench 2: 21 / 2 / 7). All of bench 2's DOWN stores now pass (Publisher, Spotlight, Origin, Plum,
Bummer; Palmonas degraded by one flaky step; Snitch moved to snitch.com, which is not Shopify).
Not testable: vaaree (robots.txt, expected), thewholetruthfoods + snitch.com (not Shopify).

Every failure was diagnosed from its trace (offline DOM replay + network log + screenshots):

| Store | Failure | Real cause | Whose | v0.8 fix | Mock mode (old code fails it with the same message) |
|---|---|---|---|---|---|
| dotandkey (DOWN) | clicked product, stayed on collection | SearchTap re-renders the grid ~1 s after load; the click hit the old grid (no product request in the network log) | Radar | wait until product links stop changing; if a click does not navigate, re-pick and click again (recorded) | `rerender_grid` |
| thehouseofrare (DOWN) | no collection link, no product card on / | brand-landing homepage; menus open on HOVER; top items go to /pages/<brand> | Radar | hover top menu items; else one hop via the first top-menu page | `hover_menu`, `brand_landing` |
| soulflower PDP | add_to_cart not found (score 0.05) | main button sits in `form.cart-form > div.quick-add-container` (no /cart/add action); excluded by class name; plus a YourLio popup with utility classes and an icon-only X | Radar | "another product's card" decided by where nearby product links point (never class names); main button = any container carrying this product's variant id; generic big-fixed-layer popup + icon-only close | `quick_named_main`, `icon_popup` |
| supplysix PDP | price not on page | price only in a sticky bar shown after scrolling | Radar | scroll the page, then re-read the price | `sticky_price` |
| foxtale search | 'purify': 0 of 8 relevant | word taken from a product the store hides | Radar | second word from another product; first miss kept as warning | `search_misses` |
| mcaffeine PDP | add_to_cart not found | **no add-to-cart form for this product on its own page**; the 18 forms on it are other products' cards | Store (or deliberate "not sold alone") | message says exactly that; triage gets it as a fact | `no_buy_form` |
| boldcare PDP, foxtale PDP | URL is a product page: got / | catalog product redirects to the homepage | **Store catalog finding** | recorded as a WARNING "in the catalog but hidden from shoppers"; next catalog product tested | `hidden_product` |
| foxtale PDP | no product name | catalog product page says "Page Not Found" | **Store catalog finding** | same, "the page does not exist" | `notfound_product` |
| soulflower, foxtale health | 404 expected, got 200 | soft 404 | **Store** (minor SEO) | unchanged (correct FAIL); gpt-5-mini had called it Radar's, now a code FACT | n/a |
| palmonas PDP, dotandkey search | flaky | failed once, passed on retry | n/a | reported as flaky, no incident | n/a |

**Triage accuracy on this bench (gpt-5-mini, before v0.8 facts):** 9 confirmed failures; correct on
dotandkey, thehouseofrare, soulflower PDP, foxtale search, foxtale 'Page Not Found' (5); wrong on the
two soft 404s and mcaffeine (called Radar's), and "unsure" on supplysix. These are now `llm-check`
cases (20 total) with code-proven facts for soft 404 and missing buy form.

## 4i. Fourth bench (36 stores, 5 Oct, v0.8): results and root causes

`data/bench/20261005T095646Z/` on the Mac. 33 testable stores: **27 healthy, 6 degraded, 0 down**
(bench 3: 25 / 6 / 2). Every v0.8 fix held on the stores that caused it: dotandkey, thehouseofrare,
soulflower PDP, mcaffeine and boldcare's redirecting product all pass. Not testable: vaaree
(robots.txt), snitch.com + thewholetruthfoods (not Shopify). `llm-check` (20 cases, gpt-5-mini):
19/20, held-out 3/3, ≈ $0.0076; one miss (below).

10 confirmed failures on 6 stores, each diagnosed from its trace (offline DOM replay of the frame
snapshot at the failing call, the call's recorded result, screencast frames):

| Store | Failure | Real cause (proof) | Whose | v0.9 fix | Mock mode (v0.8 fails it with the same message) |
|---|---|---|---|---|---|
| boldcare PDP | variant: expected 46648059855066, got 47923389726938 | first `/cart/add` form in the DOM is a hidden cart-drawer form `product_form_8964884103386` for ANOTHER product; v0.8 accepted any form whose id/class says product_form. The page's own form held the right id (screenshot: "2 Packs" selected, ₹898) | Radar | the product's form = a form carrying one of THIS product's variant ids (visible first, main-named first); a form is never picked by name alone; a nameless-id main form counts only when visible and empty | `drawer_form_first` |
| bummer PDP ×3 | variant: expected 4757735…, got "" after choosing M, Toffee | 14 hidden `NativeCartUpsell` forms (class `shopify-product-form`, empty id) come first; the real form held 47577353257211 all along | Radar | same rule | `upsell_forms_first` |
| plumgoodness PDP | no product name | `/products/…-copy-1` (catalog copy) answers 200 "The product is currently unavailable", body class `hidden_product`, no buy form, no Product data | **Store catalog finding** | treated like a redirect / Page Not Found: WARNING "in the catalog but hidden from shoppers", next catalog product tested | `unavailable_product` |
| foxtale PDP | no product name | headless storefront renders its homepage at `/products/brightening-under-eye-cream`: no form, no Product data, name nowhere on the page | **Store catalog finding** | same (rule: no buy form + no Product data + the product's name nowhere on the page; if every candidate looks like this the last one fails hard) | `home_at_product_url` |
| supplysix PDP | price not on page | on desktop the ONLY price is in `product-sticky-form.hidden-lap-and-up[hidden]`, a mobile-only bar; screencast of the full scroll shows no price, just ADD TO CART. **Bench 3 called this Radar's (sticky bar on scroll); that was wrong for desktop** | **Store** (desktop shoppers see no price before cart) | failure stays; message now says "₹199.00 is only inside a hidden element <…hidden-lap-and-up>, not shown on this screen size"; triage FACT says so (gpt-5-mini had blamed Radar) | `price_desktop_hidden` |
| soulflower, foxtale health | 404 expected, got 200 | soft 404 | **Store** (minor SEO) | unchanged; triage now right (FACT from v0.8) | n/a |

**Radar false failures in bench 4: 4 cases on 2 stores (boldcare 1, bummer 3), one cause.** Two
more (plum, foxtale PDP) were store catalog findings reported as failures instead of warnings.

**Triage on this bench (gpt-5-mini):** right on bummer ×3, boldcare (Radar), plum, both soft 404s
(store) = 6; wrong on supplysix (said Radar; it is the store) and foxtale PDP (said Radar looked at the
homepage; the store serves the homepage there). Both are now code-established (fact / warning).
`llm-check` miss `custom_buy_button_not_recognised`: the model saw only "no add-to-cart form" and
blamed the store. New FACT lists short action-like lines next to the price that Radar's buy words
(add / cart / bag / buy) did not match ("Grab it", "Share"). New case `supplysix_price_hidden_desktop`:
**21 llm-check cases.**

## 4j. Fifth bench (36 stores, 5 Oct, v0.9): results and root causes

`data/bench/20261005T115219Z/` on the Mac. **32 testable: 28 healthy, 4 degraded, 0 down.**
`llm-check` (21 cases, gpt-5-mini): **21/21**, held-out 3/3, ≈ $0.0088. v0.9's fixes held: boldcare and
bummer healthy (variant read from the own form), plum/foxtale hidden products no longer fail.

**Confirmed failures: 3, all real store findings, all triaged correctly by the LLM.**

| Store | Result | Cause | Whose | v0.10 |
|---|---|---|---|---|
| supplysix PDP | FAIL | desktop shows no price (only a mobile-only sticky bar) | Store | — (message + triage right) |
| soulflower, foxtale health | FAIL | soft 404 | Store | — |
| boat-lifestyle journey | FLAKY | attempt 1 clicked Airdopes 161 at (140, 425) but opened Rockerz 412. Trace timing: pick + scrollIntoView at t, hit-test check at t+7 ms (OK), mouse click at t+30 ms; screencast shows the page re-laid out after the scroll (header changes) and the grid moved ~234 px, so the stale point was on the next card. Retry passed | **Radar** | after the pick, wait until the target stops moving (150 ms steps, two equal measurements), re-aim at its current position, then hit-test and click (`AIM_JS`). Mock `shift_after_scroll` (cards move one row ~8 ms after the first scroll): v0.9 fails it 5/5 with the bench message, v0.10 passes 5/5 |
| plumgoodness | "unsupported" | homepage answered **HTTP 423 "This store is unavailable"** to Radar (live for other visitors at the same time, checked) | **Radar verdict** (store refused Radar) | homepage 401/403/423/429 → access `refused` → verdict BLOCKED with the code and page title; other ≥400 → UNREACHABLE. Never "unsupported". Mock `store_refuses` (v0.9: "down") |
| bench row | flaky row empty | the row showed the last (passing) attempt | Radar report | flaky rows show the failed attempt: "failed once, passed on retry: …" |

**Radar false failures in bench 5: 0 confirmed, 1 flaky (boat), 1 wrong verdict (plum).** Both fixed in v0.10.
The exit rule (a bench with zero Radar false failures) needs one more clean bench: v0.10.

## 4k. Sixth bench (36 stores, 6 Oct 00:50 IST, v0.10): R1 exit rule met

`data/bench/20261005T192044Z/` on the Mac. Ran on **v0.10** (started before the v0.11 sync: no
User-Agent line in the header, no `browser` field in bench.json), so v0.11's browser identity is not
benched yet.

**32 testable: 29 healthy, 3 degraded, 0 down, 0 flaky.** Blocked 2, not Shopify 2.

| Store | Result | Cause | Whose |
|---|---|---|---|
| supplysix PDP | FAIL | desktop shows no price (mobile-only sticky bar); LLM: real store problem | Store (same as bench 4, 5) |
| soulflower, foxtale health | FAIL | soft 404; LLM: real store problem | Store (same as benches 3-5) |
| boat-lifestyle | **healthy** | v0.10 re-aim fix held (bench 5: flaky) | — |
| plumgoodness | BLOCKED | HTTP 423 "This store is unavailable" to Radar again (2 runs, hours apart), live for normal visitors. Now reported honestly: "refused or rate-limited Radar's browser" | Store refuses Radar |
| vaaree | BLOCKED | robots.txt disallows | Store's choice |
| snitch.com, thewholetruthfoods | UNSUPPORTED | not Shopify | — |

**Radar false failures: 0. Radar flaky: 0. Every confirmed failure is a real store finding, triaged
correctly by gpt-5-mini.** This is R1's exit rule (handoff, 5 Oct). Engine phase closed on desktop.

Open question carried forward: plum's 423 twice. v0.11 sends a normal Chrome name + BugRadar; if
plum lets that in, its block is a filter on non-browser User-Agents (allowed: Radar still identifies
itself). If it still refuses, it is a deliberate block: stays BLOCKED, never evaded.

## 4l. Seventh bench (36 stores, 6 Oct 10:12 IST, v0.11): new User-Agent; 2 Radar issues found

`data/bench/20261006T044220Z/` on the Mac. First bench with the v0.11 User-Agent (header "User-Agent:
Chrome + BugRadar", bench.json `browser.ua_style = browser`). **33 testable: 28 healthy, 5 degraded,
0 down, 0 flaky.**

**plum answered normally**, after HTTP 423 to `BugRadar/0.1` alone in benches 5 and 6: its block
was a filter on non-browser User-Agent names. Radar still identifies itself in every request.

| Store | Result | Cause (proof) | Whose | v0.12 |
|---|---|---|---|---|
| supplysix PDP | FAIL | desktop shows no price | Store | — |
| soulflower, foxtale health | FAIL | soft 404 | Store | — |
| plumgoodness PDP | FAIL "no product name" | catalog copy page "currently unavailable" (as bench 4). v0.9's hidden-product rule only ran when the page had NO cart/add form and NO Product ld+json; the trace shows Radar's own check returned true (other products' form and/or ld+json present) while no form held THIS product's variant. The v0.9 mock had neither, so it passed | **Radar** (rule gap) | hidden-product rule runs whenever no buy control holds one of THIS product's variant ids (`OWN_HOLDER_JS`: name=id inputs/selects, data-variant-id); still needs the product name absent from the page. Mock `unavailable_product` now carries ld+json + another product's drawer form; v0.11 fails it with the bench message |
| thehouseofrare search | FAIL "0 of 16 mention 'rare'" | Radar searched **'rare', the brand**: brand words came from the domain label as one token (`thehouseofrare`) and from the title only before " - " (brand is after it). Screenshot: search works (Gift Cards, trainers for "RARE") | **Radar** (test word) | brand words = domain label + its compound parts, EVERY title segment, and Shopify `vendor` names (new `Product.vendor`). Unit test: v0.11 picks 'rare', v0.12 picks 'kore' |

**Radar false failures in bench 7: 2** (one exposed only because plum became testable). The R1 exit
rule (met on bench 6) needs one more clean bench on v0.12.

Lesson for diagnosis: Playwright snapshots hold no `<script>` elements, so a DOM replay cannot show
JSON-LD; bench 4's plum replay concluded "no product data" from that blind spot. Trust the recorded
results of Radar's own calls for data questions (`tools/trace_replay.py` docstring).

## 4m. Eighth bench (36 stores, 6 Oct 14:48 IST, v0.12): Radar's own network dropped; 2 Radar rules fixed (v0.13)

| Time (IST) | What the bench shows | Proven by |
|---|---|---|
| 14:48–14:50 | 6 Shopify theme demos healthy (Dawn, Sense, Craft, Refresh, Studio, Taste) | bench.json |
| ~14:51 | every page load: `net::ERR_INTERNET_DISCONNECTED` (Chromium's own "this computer is offline") | every failure message from 14:51 on |
| 14:51–14:52 | crave (journey passed first), origin, colorblock: **DOWN**; 27 stores: "unreachable", 0–1 s each | bench.json |

Nothing in bench 8 says anything about v0.12 or the stores. Two Radar rules were wrong:

| # | Radar rule in v0.12 | Why wrong | v0.13 |
|---|---|---|---|
| 1 | Any failure confirmed 2-of-3 = store failure; homepage error = UNREACHABLE | When Radar itself is offline, every store looks dead. On a schedule this alerts merchants for our outage | Before a failure is confirmed, before a homepage is called unreachable and before robots.txt is called unreachable, Radar checks its own connection (`radar/core/network.py`: 3 always-on hosts, any HTTP answer or TLS error = online, positive answer cached 30 s). Offline = **NO_NETWORK**: the run stops, passes before the drop are kept, no failure, no incident opened OR closed, no alert. The bench does not start stores while offline and prints a warning. Setting `net_probe_urls` / env `RADAR_NET_PROBES` (empty = off) |
| 2 | robots.txt unreadable = allow everything | RFC 9309: 4xx = no file, allow all; **5xx or no answer = complete disallow** | 200 = rules; 4xx = allow (note "store has no robots.txt"); 5xx / no answer, tried twice 2 s apart = access `robots_unreachable` → BLOCKED with the reason (if Radar is online) |
| 3 | Runs that tested nothing still closed open incidents | nothing tested ≠ fixed | incidents resolve only when cases ran and the run is not NO_NETWORK |

Proof on the v0.12 copy before the fix: the net-drop scenario (store answers the journey, then goes silent, probe dead) gave **DOWN with 10 confirmed failures**; mock `robots_500` gave **healthy, 10 tests run**. Both pass the new rules in v0.13. New tests: 6 end-to-end (Radar offline mid-run → NO_NETWORK; store dies while Radar online → still DOWN; dead homepage offline vs online; robots 500 → BLOCKED; robots 404 → healthy; bench offline → no store started) and 4 unit tests (robots unreachable, probe rules, env setting).

Checked against earlier benches: no real store in benches 6–7 had an unfetchable robots.txt (plum's was 423 = 4xx = allow, then its homepage 423 = BLOCKED as before), so rule 2 changes no earlier result.

## 4n. Ninth bench (36 stores, 6 Oct 15:31 IST, v0.13): 2 Radar issues, fixed in v0.14

Totals: 29 healthy / 4 degraded / 0 down / 1 blocked (vaaree, robots.txt) / 2 unsupported (snitch, thewholetruthfoods: not Shopify). 0 no_network rows. v0.12 fixes held: plumgoodness product PASS (hidden-product rule), thehouseofrare search PASS (brand words).

| Store | Result | Root cause (proven) | Owner | v0.14 |
|---|---|---|---|---|
| plumgoodness.com | search FAIL ('mystery', then 'plums') | Words came from 'Mystery Merch' (promo item; the store's search app showed 12 other products) and 'Set of 5 Plums' ('plums' = brand 'Plum' + s, matched all 246 products by brand; 0 of 24 shown mention 'plums'). sitemap.json + recorded results + LLM triage (radar problem) | Radar (test words) | brand words match plurals/stems; promo/bundle words (mystery, merch, bundle, sampler, trial, surprise, offer, hamper, limited, ...) never used; up to **3** words, the search fails only if all 3 find nothing relevant |
| soulflower.in | journey FLAKY (click_into_product) | 'Powered by YourLio AI' promo popup ('NEW LAUNCH: BOMB SIZE ROSEMARY HAIR SPRAY', × + 'TRY IT NOW') rendered inside **#chat-widget's open shadow root**, full-page overlay. Failure screenshot shows it; trace replay of the failing call: v0.13 popup finder → nothing, v0.14 → popup + 'close' | Radar (popup finder) | popup finder searches every open shadow root too; closes by the ×, never the promo button (vetoes unchanged) |
| supplysix.com | product FAIL | ₹199 trial page: price only in hidden mobile sticky bar | Store | — |
| soulflower.in, foxtale.in | health FAIL | soft 404 (made-up URL → 200 home) | Store (SEO note) | — |

Proof on v0.13 before the fix: mock `shadow_popup` → journey confirmed_fail ("3 links: 3 covered by div"); the two new search-term unit tests fail. v0.14: all pass. Note: whether plum's search should find 'Mystery Merch' is not judged (stores often keep promo items out of search on purpose).

## 4o. Tenth bench (36 stores, 6 Oct 16:45 IST, v0.14): 0 Radar false failures; 1 flaky = a real store finding

Totals: 29 healthy / 4 degraded / 0 down / 1 blocked / 2 unsupported / 0 no_network. v0.14 fixes held: plumgoodness search PASS, soulflower journey PASS (no flaky).

| Store | Result | Root cause (proven) | Owner | v0.15 |
|---|---|---|---|---|
| thefunclab.com | journey FLAKY (attempt 1 failed, 2 passed) | Attempt 1: the collection link Radar picked sits in the auto-rotating hero banner; the click timed out twice (soft step), so Radar went home → product card. The card it clicked is in the homepage 'Shop Our Products' slider: its **product IMAGE link does nothing when clicked**. Trace: link picked, hit-test true at (1073,425), two `mouse.click`s 10 s apart, no navigation. **Live in Anmol's Chrome (6 Oct): 2 of 2 image clicks stayed on `/`; the page's own script calls preventDefault on mousedown, mouseup and click (trusted events, so a shopper's click dies too); the product NAME link opened the product.** Attempt 2's collection link worked, so it went via the collection grid and passed | **Store** (dead image link), Radar turned it into a flaky journey | When a product link does nothing twice, Radar clicks that product's other link in the same card (a shopper clicks the name). Opens → journey goes on; a WARNING step `every_product_link_opens_the_product` names the dead link. Nothing opens → the step fails as before |
| supplysix / soulflower / foxtale | FAIL | trial-page desktop price / soft 404 / soft 404 | Store | — |

Proof: new mock `dead_image_link` (image link cancels mousedown/click, name link works). v0.14: journey confirmed_fail "expected /products/ceramic-vase, got /collections/home-decor" (both attempts). v0.15: PASS first attempt + the warning.

Known, not changed: links inside an auto-rotating hero banner time out (Playwright waits for a still element). The step is soft and Radar falls back to the homepage cards; picking static links first is a later improvement (seen once, no false result).

## 4p. Eleventh bench (36 stores, 7 Oct 08:32 IST, v0.15) and the anti-loop rules

Totals: 27 healthy / 4 degraded / 0 down / 3 blocked (vaaree robots, soulflower + bummer robots no answer) / 2 unsupported. thefunclab: healthy (v0.15 held).

| Store | Result | Root cause (proven) | Owner | v0.16 |
|---|---|---|---|---|
| palmonas.com | journey FLAKY | `/products/x.js` answered a non-JSON body once ("Expecting value: line 1 column 1") | Transient, Radar should absorb | **Pattern, 2 stores:** store data requests retried (0, 1 s, 3 s) on no answer, 5xx/429 or non-JSON; a real 4xx is final |
| wellbeingnutrition.com | journey FLAKY | same request timed out once (`connect ETIMEDOUT 64:ff9b::…`, the Mac's NAT64/IPv6 path) | Transient | same |
| soulflower.in, bummer.in | BLOCKED (robots_unreachable) | robots.txt no answer on two quick tries; both answered on every earlier bench | Transient (likely the Mac's network) | **Pattern, 2 stores:** robots.txt 3 tries (10/15/20 s timeouts, 2 s and 5 s apart) before RFC 9309 "do not crawl" |
| foxtale.in | product FAIL | 'Brightening Under Eye Cream' is published in the catalog (products.json, published 24 Sep) but its page sends shoppers to the homepage. **Verified live in Anmol's Chrome 7 Oct.** No spare product left, so the last candidate failed hard (the v0.9 design) | **Store** (catalog) | none; the message ("URL is a product page") and the LLM triage ('radar problem') are wrong in wording only: goes to the report-wording step |
| supplysix, foxtale 404 | FAIL | as before | Store | — |

Proof: mocks `flaky_data` and `robots_500_twice`. v0.15: journey FLAKY with the palmonas error / store BLOCKED "tried twice". v0.16: healthy, every case first attempt.

**Anti-loop rules (Anmol, 7 Oct: "no loop like sync8"):** (1) the 36-store bench is a regression set, not a tuning set; (2) fix only patterns seen on 2+ stores, one-offs become documented limits; (3) R1 closes Friday 9 Oct with its limits written down; (4) the real robustness numbers are first-run false failures on 30 never-seen stores (`stores/new30.txt`, run once, no fixes in between) and the catch rate on a seeded-bug store (target ≤ 1/30 and ≥ 80%).

## 5. Self-healing locators

Checks never hard-code selectors. They ask for an **intent** (`add_to_cart`, `checkout_button`).
`radar/healing/locator.py` resolves it down a ladder, cheapest first:

1. **cache**: what worked last time for this site (SQLite `locator_cache`). Dropped automatically if stale.
2. **hints**: per-site selectors (`sites/<site_id>.yml`), then built-in Shopify defaults.
3. **heuristic**: scores every visible button/link on the page (text such as "add to bag",
   attributes such as `name=add`, form action `/cart/add`; penalties for "sold out",
   "wishlist", disabled). Accepted at score ≥ 0.6.
4. **LLM**: a numbered list of up to 40 candidates goes to the model; it returns
   `{index, confidence}`; accepted at confidence ≥ 0.6.

Every rung picks the first VISIBLE match that is not excluded (never blindly `.first`). For
`add_to_cart`, a button in a quick-add / card / carousel / upsell / recommendation block is excluded
at every rung **only if it belongs to another product's card**: the nearest product links around it
point to a different product (v0.8; class names alone wrongly excluded soulflower.in's main button). `find()` can be scoped to a root (an open cart drawer).

A rung 3 or 4 success is cached, logged in `healing_events`, and shown in the report with the
new selector and the reason. Healed selectors prefer stable forms (id, `name`, `data-testid`,
unique class, unique text) over positional paths.

**Healing never decides pass.** It only finds the element. The outcome check after it (for
example `/cart.js` really contains the product) is what decides.

## 6. LLM layer

`radar/healing/llm.py` (client), `radar/healing/triage.py` (failure review), `radar/llmcheck.py`
(scoring). Standard library only. Keys live in `.env` (git-ignored; template `.env.example`).

**Decision (Anmol, 5 Oct): cheap and robust first, Claude once BugRadar earns.** Default =
OpenAI **`gpt-5-mini`** ($0.25 / $2.00 per 1M tokens, reads screenshots), chosen on evidence 5 Oct
(table below). `gpt-4o-mini` stays as the cheaper fallback (`RADAR_LLM_MODEL=gpt-4o-mini`).

| Setting | Effect |
|---|---|
| `OPENAI_API_KEY` in `.env` | provider = openai, model `gpt-5-mini` (default; reasoning model: no temperature, minimal effort) |
| `RADAR_LLM_MODEL=gpt-4o-mini` | switch model, nothing else changes |
| `ANTHROPIC_API_KEY` (and no OpenAI key) | provider = anthropic, `claude-haiku-4-5-20251001` |
| `RADAR_LLM_PROVIDER=openai_compat` + `RADAR_LLM_BASE_URL` + `RADAR_LLM_API_KEY` | Gemini / DeepSeek / any OpenAI-compatible endpoint |
| nothing set | provider = none; heuristic healing only, no triage; the report says so |

**What the LLM does, and the rule that it never decides pass/fail:**

| When | LLM job | What code checks afterwards |
|---|---|---|
| A locator fails (cache, hints, heuristic all missed) | Pick the element from a numbered list | The outcome (e.g. `/cart.js` really has the product) |
| A test is CONFIRMED failed (2 of 3 fresh browsers) | Triage with screenshot + assertions + page text: `real_store_problem` / `radar_problem` / `unsure`, plus a plain-English reason | Nothing changes yet: label only |
| Triage says `radar_problem` | ONE re-check attempt with LLM assist on: find the product name among the biggest texts; close an overlay Radar's rules missed | Name: ≥16px or a heading, and shares a real word with the catalog title. Overlay: only controls whose text is a pure close ("×", "No thanks", "I'll pass"); subscribe/accept/age/"Try my luck" are vetoed in code. The re-check must PASS the same assertions to clear the failure |

Report and bench show the triage verdict and reason; a cleared failure is tagged "passed on
LLM-assisted re-check"; an uncleared one keeps its FAIL with "Radar suspect (LLM)" or "store
problem (LLM)".

**Choosing / switching models on evidence:** `python3 -m radar llm-check [--model X]` sends 21 fixed
failure cases with known right answers (18 from bench evidence and the mock store, 3 held-out cases
written after the first run and not used for tuning; 15 until v0.7.2, 20 in v0.8, 21 in v0.9) and prints the score, tokens and cost. Run it
before and after any switch.

**First real run (Anmol's Mac, 5 Oct, gpt-4o-mini, 12 cases): 7/12, $0.0012, 37 s.** All 5 misses
blamed the STORE for Radar's own mistakes (product name visible on the page, newsletter covering
the page, page still loading, hidden gift SKU), i.e. the dangerous direction (false alarms to a
store). Fix (v0.7.1), general rather than fitted to the cases: the code now states FACTS before
asking (the "missing" value IS in the page text; a click was covered by element X; loading text;
error/unavailable text; popup wording only when a click was blocked), and the prompt requires
positive evidence before blaming the store, with ordered rules.

**Re-measured (5 Oct, Anmol's Mac, 15 cases incl. 3 held-out):**

| Model | Score | Held-out | Wrong store blames | Cost of 15 calls | ≈ per call |
|---|---|---|---|---|---|
| gpt-4o-mini, v0.7 prompt | 7/12 | n/a | 5 | $0.0012 | $0.0001 |
| gpt-4o-mini, v0.7.1 facts + rules | 14/15 | 3/3 | 1 (Plum freebie; now also skipped at discovery) | $0.0019 | $0.00013 |
| **gpt-5-mini, v0.7.1** | **15/15** | 3/3 | 0 | $0.0054 | $0.00036 |

Decision: default = gpt-5-mini (≈ Rs 20/month more at 50 stores for zero wrong store blames). 15 cases
is a small sample: every real triage that a human checks should be added to `llmcheck.CASES`.

**v0.8, 20 cases (Anmol's Mac, 5 Oct): gpt-5-mini 19/20, held-out 3/3, ≈ $0.0076, 39 s.** Miss:
`custom_buy_button_not_recognised` (blamed the store; v0.9 adds the action-words FACT, 4i).
**v0.9, 21 cases: gpt-5-mini 21/21, held-out 3/3, ≈ $0.0088, 42 s.**

Cost controls: LLM only on a failure path (never on a passing run); healed locators cached; hard cap
`llm_max_calls_per_run` (default 12, ≈ $0.01 per run on gpt-4o-mini); calls, tokens and estimated
USD in every run summary. An LLM error never crashes a run.

## 7. Storage: how xyz.in and abc.com are kept apart

- **site_id** = host, lowercased, `www.` removed (`www.Vaaree.com` → `vaaree.com`). Subdomains are separate sites.
- **One database** `data/radar.db`. Every table carries `site_id`:
  `sites, runs, case_results, incidents, locator_cache, healing_events`.
  Same schema moves to Supabase Postgres with row-level security on `site_id`.
- **One folder per site:**
  ```
  data/sites/xyz.in/sitemap.json
  data/sites/xyz.in/suites.json
  data/sites/xyz.in/index.html                 run history
  data/sites/xyz.in/runs/<run_id>/run.json     full result
  data/sites/xyz.in/runs/<run_id>/report.html  interactive report
  data/sites/xyz.in/runs/<run_id>/*.png|*.zip  failure screenshots + Playwright traces
  ```
- **Incidents** are deduplicated by signature `site|test case|failed step`: one outage is one
  incident with an occurrence count, auto-resolved when the test passes again.
- **Retention:** screenshots/traces removed after 7 days for passing runs, 90 days for runs
  with failures. `run.json` and DB rows are kept.

## 8. Reports

- Per journey attempt: a screenshot strip, one JPEG after every step (pass and fail, quality 50,
  identical consecutive pictures stored once: ~6 files / ~100 KB on the mock journey).
- Per run: radar view (ring = suite, blip = test, red blips pulse; click opens the test),
  counts, discovery facts and notes, filters (failed / flaky / warnings / healed / blocked) and
  search, each test with attempt tabs, step timeline with durations, errors, healing details,
  failure screenshot (click to zoom) and the Playwright trace with the replay command,
  self-healing log, run history bars, incidents. Light and dark, works at phone width.
- Per site: run timeline, all runs, incidents.
- Self-contained HTML (no server, no CDN). The embedded JSON is the same shape the dashboard
  will read.

## 9. Proven vs not proven

| Area | Status (5 Oct) |
|---|---|
| Whole pipeline on a Shopify-like store | **Proven** in real Chromium against the mock store (30 modes) |
| Real Shopify stores | **Bench 7 (v0.11, 33 testable): 28 healthy, 5 degraded, 0 down, 0 flaky.** 3 failures are real store findings; 2 were Radar's (plum rule gap, brand word searched), fixed in v0.12 (4l). Bench 6 (v0.10) had 0 Radar false failures. **v0.12 not yet benched.** |
| Bench-2 bugs reproduced | **Proven**: v0.6 code fails the new mock modes with the same messages as on the real stores; v0.7 passes |
| Add-to-cart identity (product opened = product added) | **Proven** on mock (`wrong_variant`) and on moxiebeauty.in (correct product, free gift warned) |
| Heuristic healing | **Proven** (renamed "Add to Bag" button healed to a stable selector, reused from cache) |
| LLM healing, triage, assisted re-check | **Proven with a fake LLM** end to end (triage → re-check → pass, real problem → no re-check, overlay veto). **Real OpenAI triage proven on 15 known cases**: gpt-5-mini 15/15, gpt-4o-mini 14/15 (5 Oct). Not yet seen on a real failing store run |
| Confirmed failure, flaky, down, incident dedupe | **Proven** (broken price, broken cart modes) |
| Blocked / unsupported / unreachable verdicts | **Proven** on mock and on real stores (vaaree robots-blocked, thewholetruthfoods not Shopify, rarerabbit.in off-site) |
| Mobile device emulation | Code path exists (`--device mobile` / `both`), **not covered by e2e tests and not benched** |
| Screenshots | **Every journey step** (strip in the report) + full PNG and trace on failure |
| Tests | **97 pass: 57 unit + 40 end-to-end** (`python3 -m pytest -q`, ~16 min; 5 Oct, cloud sandbox) |

## 10. Pain points and known limits (current, 5 Oct)

1. **Bench not clean yet (v0.7 not benched).** Bench-2 causes are fixed and proven on mocks (4g),
   but the zero-false-failure exit rule is only met when a real bench rerun says so. Until then a
   DOWN must be checked by hand (or at least carry an LLM triage) before anyone is told.
2. **Cart flow vs robots.txt: DECIDED.** Shopify's default robots.txt disallows `/cart`. Radar
   follows robots.txt except the shopper flow: `/cart*` when the cart flow is on (`allow_cart_flow`)
   and a search typed into the store's own box (`search_is_shopper_flow`). Prospects run
   READ-ONLY (no `cart` flag in the bench list, `--no-cart` for scans). Cart flow only on Shopify's
   demo stores and stores that installed the app or agreed in writing.
3. **Add to cart creates a real cart session** on a live store (no order, no payment). Keep frequency low.
4. **Bot protection.** Some stores may block a clearly-identified automated browser. Radar does
   not evade this (no stealth, by decision 3 Oct; reconfirmed 6 Oct after Anmol raised uc_chrome:
   across 5 benches no store showed a bot challenge, so stealth would have unlocked nothing).
   Verdict BLOCKED with the reason; the store can allowlist `BugRadar`.
   **Browser identity (v0.11, 6 Oct):** the User-Agent is the normal Chrome name of the browser in
   use followed by Radar's name (`... Chrome/141.0.0.0 Safari/537.36 BugRadar/0.1 (+bugradar.in)`),
   not Playwright's `HeadlessChrome`; simple "is this a real browser?" filters let it in, logs still
   say BugRadar. `--plain-ua` (or `RADAR_UA_STYLE=plain`) sends `BugRadar/0.1 (+bugradar.in)` only.
   robots.txt is always matched on the `BugRadar` token. `navigator.webdriver` is NOT hidden.
   `--headed` (scan and bench) runs a visible Chrome window instead of headless.
5. **Variants.** Radar picks the first in-stock variant (URL `?variant=` plus choosing its
   options on the page: select, radio, swatch) and asserts the form carries it. It tests ONE
   variant per product, not every SKU/variant (Revenue Shield lists SKU/variant testing).
6. **Checkout.** Radar stops at the checkout button (checks it is visible and enabled, never
   clicks it). Checkout depth is DECIDED (scope doc, 5 Oct) but NOT BUILT: level 1 open checkout,
   level 2 up to OTP with our own test number, level 3 OTP auto-read later. Never place an order,
   never pick a payment method. Levels 2–3 only on consenting stores.
7. **Icon-only buttons.** A buy button with no text scores below the heuristic threshold unless
   it matches a built-in hint (`form[action*='/cart/add'] button[type=submit]` usually does).
   The LLM rung covers the rest when enabled.
8. **Third-party JS errors** count as warnings (soft), never failures. They can be noisy.
9. **Load time** is DOM-ready time from wherever you run Radar, not real-user metrics.
10. **Speed.** Inside one store, tests run one after another (politeness delay 1.5 s). On
    bench 2 a store took 34 s to 4 min 54 s (average about 1 min 47 s, desktop only). The bench
    runs several stores in parallel (`--workers`, default 3); bench 2 was about 54 minutes of
    store time in total.
11. **Step screenshots are journey-only** (other suites: failure screenshot + trace). Retention in
    production (dashboard phase): latest passing run per store, failures 30 days.
12. **Desktop by default.** Mobile is `--device mobile|both`, not default, not benched.
13. **No scheduler, no alerts, no web dashboard yet.** Local runs only; reports are static HTML.
14. **Brand strings are scattered** (User-Agent `BugRadar/0.1`, report titles). Product name is
    undecided, so these must move into one setting before the app work.
15. **Not auto-built yet:** login/account flows, subscriptions, coupons, checkout pages,
    catalog-integrity checks (gold/Gold facets, test products), app-script fingerprinting.
16. **Mac sync quirk (tooling, not Radar):** when Claude writes files to the Mac through the
    device bridge, re-using an old staging path delivered stale files once (the `slow_mo_ms`
    error). Rule: fresh staging folder each time, overwrite, then md5-check every file on the Mac.

## 11. Next layers (build order from the product scope doc, 5 Oct)

1. **Engine (now):** DONE in v0.7: bench-2 fixes, store list, step screenshots, LLM triage.
   NEXT: Anmol runs `radar llm-check` and the bench on the Mac; fix whatever is left until zero
   Radar false failures; then desktop + mobile by default; checkout levels 1–2 behind a per-store
   opt-in; **store profile + learning loop (section 12, decided 6 Oct; absorbs app-script
   fingerprinting)**; brand strings in one setting.
2. **Dashboard:** real web dashboard on the existing data (stores, run history, incident
   timeline, step-by-step screenshot gallery, a panel per module), built to embed in Shopify
   admin later.
3. **Always-on:** scheduler + queue, Postgres + object storage for screenshots, alerts
   (email/Slack first), incident lifecycle (close after 2 passes).
4. **Shopify app shell:** OAuth install, read-only scopes, Billing API, GDPR webhooks,
   embedded dashboard, free scan page.
5. **Later:** checkout level 3 (OTP auto-read), all-SKU/variant testing, visual self-healing,
   switch the default LLM to Claude once BugRadar has revenue (one line in `.env`, then `llm-check`).

**LLM queue (Anmol, 5 Oct):** try **GLM-4.6V** (Zhipu / z.ai, vision, ~$0.30 / $0.90 per 1M) through
`python3 -m radar llm-check` when convenient. Needs a z.ai key and 4 lines in `.env`
(`RADAR_LLM_PROVIDER=openai_compat`, `RADAR_LLM_BASE_URL=<z.ai OpenAI-compatible endpoint, confirm in their docs>`,
`RADAR_LLM_API_KEY`, `RADAR_LLM_MODEL=glm-4.6v`). Switch only if it scores 15/15 and the data-location question
(China) is acceptable for client stores. Until then the default stays gpt-5-mini.

## 12. Store profile + learning loop (decided 6 Oct 2026, not built yet)

**Why.** Anmol (6 Oct): the LLM should understand the whole store on the first run (pages,
features, which assertions apply), store that in the DB and get smarter run after run, so Radar
is more robust, matches Revenue Shield and goes beyond it. Claude's build call below.

**The one rule that does not change:** the LLM proposes and explains; code assertions decide
pass/fail. An LLM-written assertion never reaches a store's report until code has proven it.

### 12a. Profile build (first scan, and again when the store changes)
| Step | Who | What |
|---|---|---|
| 1 Crawl | Code | Every page TYPE: home, collection, product, cart, search, policies, blog, contact, custom pages; templates; storefront app scripts; checkout app (most of this exists: `sitemap.json`, detection) |
| 2 Understand | LLM, once per page type | One screenshot + page text per type → feature list from a FIXED vocabulary: pincode checker, COD check, subscribe & save, bundle builder, coupon box, offer banner, reviews, size chart, WhatsApp, quantity breaks, free-gift bar, sticky ATC, mobile-only elements |
| 3 Propose | LLM | For each feature, picks check TEMPLATES from the library and fills parameters (locator hints, expected kind of result). No free-form code, no exact-value expectations (prices, stock) |
| 4 Prove | Code | Each proposed check runs 3 times on the live store. 3/3 pass = **active**; otherwise **needs review** (shown, never alerting). Safety vetoes apply as today (never subscribe/accept/age/pay; pincode only with a public test pincode, e.g. 400060) |
| 5 Save | Code → DB | `store_profile` (page map, features, versions), `profile_checks` (template, params, state, evidence), existing `locator_cache` |
| 6 Every run | Code only | Generated suites + active profile checks. No LLM unless a check fails (triage, as today) |
| 7 Change | Code | Diff each run: new/removed app scripts, new page types, template changes, feature element gone. A diff = profile re-build for the changed part only; the diff itself is reported ("new app script: X") |

### 12b. Learning loop (knowledge, not retraining)
| Store | Holds | Effect |
|---|---|---|
| Per-store memory | page map, working locators, quirks (e.g. supplysix trial page price mobile-only) | second run faster and exact; known quirks not re-alerted |
| Cross-store patterns | locator/feature patterns seen on 3+ stores (theme, checkout app, app widgets) | promoted to code hints after a test + mock; new stores start smart |
| Labelled cases | every triaged failure + Anmol's verdict (right/wrong) | grows `llm-check` from 21 to hundreds; any model/prompt switch is measured first |
| Fine-tuning | only with 1,000+ labelled cases | optional, not v1 |

### 12c. Beyond Revenue Shield (what this unlocks)
| Wedge | Revenue Shield (public pages, checked 5 Oct) | Radar with profile |
|---|---|---|
| Store-specific feature checks (pincode, COD, bundles, coupons, free-gift bar) | generic purchase flow | yes, auto-proposed and proven |
| India checkouts (GoKwik/Shopflo/Fastrr) up to OTP | not mentioned | checkout levels 1–2 |
| Catalog integrity (hidden/blank products, soft 404s, desktop-only/mobile-only price) | not mentioned | already partly built |
| Change report ("what changed on your store since yesterday") | app-script fingerprinting | scripts + page types + features |
| Plain-English triage per failure | not mentioned | built (LLM, 21/21) |

### 12d. Build order and exit
1. Profile schema + crawl of all page types + diff (code only). 2. LLM feature list + check
proposal (fixed vocabulary, `llm-check` cases for it). 3. Prove-3-times gate + needs-review
state. 4. Report section "Store profile". Exit: bench shows a profile for every Shopify store,
0 active profile checks failing falsely, LLM cost per profile build logged.
Each new check template gets a mock mode that fails on the old code, as for every rule so far.

## Change log
- 7 Oct 2026, v0.16: bench 11 (4p). Store data requests retried on transient misses; robots.txt 3 tries before
  "do not crawl". Mock modes `flaky_data`, `robots_500_twice`. Anti-loop rules recorded. `stores/new30.txt` (held-out
  set). 122 tests (66 unit, 56 end-to-end).
- 6 Oct 2026, v0.15: bench 10 (4o). A product link that does nothing when clicked (twice) -> Radar clicks the product's
  other link in the card; journey goes on, dead link = store WARNING. New mock mode `dead_image_link`. 120 tests
  (66 unit, 54 end-to-end).
- 6 Oct 2026, v0.14: bench 9 (4n). Search words: brand plurals/stems excluded, promo/bundle words never used, up to 3
  words (fail only if all 3 miss). Popup finder looks inside open shadow roots (YourLio on soulflower). New mock mode
  `shadow_popup`. 119 tests (66 unit, 53 end-to-end).
- 6 Oct 2026, v0.13: bench 8 (4m). Radar checks its own internet connection before blaming a store: NO_NETWORK verdict
  (run stops, no failure/incident/alert; bench skips stores while offline). robots.txt per RFC 9309 (5xx / no answer =
  do not crawl, BLOCKED with reason; 4xx = allow). Incidents close only when tests actually ran. New mock modes
  `robots_500`, `robots_404`; net-drop scenario via the progress hook. 116 tests (64 unit, 52 end-to-end).
- 6 Oct 2026, docs only: section 12 store profile + learning loop decided (LLM proposes, code proves; replaces separate app-script fingerprinting step).
- 6 Oct 2026, v0.12: bench 7 (4l). Hidden-product rule keyed on THIS product's variant id, not "any form or
  ld+json" (plum). Brand words: domain label + compound parts, every homepage-title segment, Shopify vendor
  names (new `Product.vendor`) (thehouseofrare searched 'rare'). trace_replay documents the no-script blind
  spot. 106 tests (60 unit, 46 end-to-end).
- 6 Oct 2026, docs only: bench 6 on v0.10 (4k): 0 Radar false failures, 0 flaky. **R1 exit rule met.**
- 6 Oct 2026, v0.11: browser identity. User-Agent = normal Chrome (or emulated device) name + `BugRadar/0.1
  (+bugradar.in)` by default; `--plain-ua` keeps BugRadar only. robots.txt matched on the BugRadar token
  whatever the style (a Chrome-style name would otherwise have made the token "Mozilla"). `bench --headed`
  for a visible browser. bench.json records the browser settings. No stealth: webdriver flag untouched.
  105 tests (59 unit, 46 end-to-end).
- 5 Oct 2026, v0.10: bench 5 (4j). Clicks wait for the picked card to stop moving and re-aim before
  clicking (boat: page re-lays out after scrolling). Homepage 401/403/423/429 = BLOCKED with the reason,
  other errors = UNREACHABLE, never "unsupported" (plum 423). Flaky bench rows show the failed attempt.
  2 new mock modes (`shift_after_scroll`, `store_refuses`), each failing on v0.9. 103 tests (58 unit, 45 end-to-end).
- 5 Oct 2026, v0.9: bench 4 root causes (4i). Product form read by THIS product's variant id, never by
  name (hidden drawer/upsell forms came first: boldcare, bummer). Catalog product whose page shows no
  product (no buy form, no Product data, name not on the page: plum "currently unavailable", foxtale
  headless homepage) = warning + next product. Price found only in a hidden element is reported as such
  (supplysix desktop). Triage FACTs: hidden price; unrecognised action words next to the price. 21
  llm-check cases. 5 new mock modes, each failing on v0.8 with the bench message. 101 tests (58 unit,
  43 end-to-end).
- 5 Oct 2026, v0.8: bench 3 root causes (4h). Grid-stability wait + re-click; hover menus + landing hop;
  generic popup detection + icon-only close; buy button found by variant id in any container, card
  exclusion by nearby product links; scroll before price; unreachable catalog products = warning +
  next product; second search word; plain "no add-to-cart form for this product" message; triage
  facts for soft 404 and missing buy form; 20 llm-check cases. 10 new mock modes, each failing on
  v0.7.2 with the bench message. 97 tests (57 unit, 40 end-to-end).
- 5 Oct 2026, v0.7.2: default model gpt-5-mini after llm-check (15/15 vs gpt-4o-mini 14/15).
- 5 Oct 2026, v0.7.1: first real gpt-4o-mini run 7/12 (blamed the store for Radar's mistakes). Triage now
  gets code-established FACTS and ordered rules requiring positive evidence for a store problem; 3
  held-out cases added to `llm-check` (15 total). Commands documented as `python3`. 87 tests (56 unit).
- 5 Oct 2026, v0.7: bench-2 root causes proven from traces by offline DOM replay (4g). Product-name
  check rewritten (any matching visible text, scored; site chrome and other products' cards
  excluded by structure, never by class name). Freebie SKUs skipped, collection-listed products
  first. Click at the hit-tested point; card-area clicks; fresh homepage after the menu search;
  second pass after animations; "none" explains why. LLM: OpenAI gpt-4o-mini default via `.env`,
  screenshots sent, triage of confirmed failures, one LLM-assisted re-check (name, overlay) that
  must pass the same assertions, `radar llm-check` scoring, cost in the summary. Screenshot after
  every journey step (deduplicated). Fixed a lying label ("none available" shown on a passing
  variant check) and added a guard test. Store list: supplysix.com, antinorm.co, peepbeauty.in,
  snitch.com, thehouseofrare.com. 86 tests pass (55 unit, 31 end-to-end).
- 5 Oct 2026, docs only (no code change): added 4f (second bench: 33 stores, 21 healthy, 7 DOWN
  presumed Radar false failures, 2 of them caused by the v0.6 heading check). Corrected the test
  count: 70 = 49 unit + 21 end-to-end (the v0.6 line below said 53 unit; that was wrong). Updated
  folder map, verdicts, proven/not-proven, pain points and next layers to the 5 Oct state.
- 4 Oct 2026, v0.6: bench RCA: all 10 DOWN verdicts were Radar bugs (table in 4e). New click engine (all candidates, centre + hit-test, skips disabled/covered/off-screen), journey without collection links, dropdown menus opened in turn, iframe popups, tolerant title check, gift-clone exclusion, no-response pages, brand-free search terms, search via the store's own box, robots-blocked/off-site/dead-URL verdicts, nav discovery visible-first without product links. 70 tests pass (49 unit, 21 end-to-end; first written as "53 unit", corrected 5 Oct).
- 4 Oct 2026, v0.5: generalisation for all Shopify stores: popup handling (decline cookies, close newsletters, age gates blocked), variant selection, theme / checkout-app / password / bot-block detection, BLOCKED verdict, stores without a product form handled by cart diff, `radar bench` (parallel, one table) with a 36-store list. The tests first caught their own broken popup mock (it never rendered) and a no-product-form gap; both fixed. 64 tests pass (45 unit, 19 end-to-end incl. a 3-store parallel bench).
- 4 Oct 2026, v0.4.1: first real run of v0.4 on moxiebeauty.in: correct product clicked and added (main form). Journey flagged a second item the page sent; trace showed it is Moxie's free Travel Pouch (Rs 0, gift with purchase, added in a separate request). Rule changed: our product must be sent and added; extra FREE items = warning, extra PAID items = failure. Added cart-total check and a `free_gift` mock mode. 53 tests pass.
- 4 Oct 2026, v0.4: assertions rebuilt after the Moxie wrong-product bug: product identity from `/products/<handle>.js`, main-form buy button (quick-add cards excluded everywhere), request-body + `/cart.js` diff verification, cart drawer support, expected/actual tables in the report, mock store copies Moxie's layout, `wrong_variant` mode. 52 tests pass (41 unit, 11 end-to-end).
- 3 Oct 2026, v0.3: after the first real Moxie Beauty runs: added the click-through shopper journey (one session), blank-page detection on every load, menu links opened in the real browser, JS-redirect handling, Rs 0 sample products excluded, hidden menu links discovered, collections ranked by menu + size. 45 tests pass.
- 3 Oct 2026, v0.2: full rebuild into a framework (discovery, generator, checks library,
  self-healing ladder with LLM rung, per-site storage, incidents, interactive reports, mock store,
  43 tests). Old single-journey code moved to `_legacy_v0/`.
- 3 Oct 2026, v0.1 rev 3: platform detection + robots.txt after the real Vaaree run.
- 3 Oct 2026, v0.1: first runner; real Vaaree run failed on a guessed price selector.
