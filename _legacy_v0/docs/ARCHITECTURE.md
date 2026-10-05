# Radar Architecture (living document)

Updated with every component built. Rule: say what works, what is unproven, and what hurts. No bluff.

Last updated: 3 Oct 2026 · Layer 1, revision 3 (platform detection + robots.txt)

---

## What exists today (Layer 1)

```
sites/vaaree.yml            per-site hints: URLs, selector lists, access rules
radar/config.py             load config, derive site_id (storage key)
radar/locate.py             first_visible(): try selector hints in order
radar/detect.py             platform detection from homepage HTML (Shopify or unknown)
radar/robots.py             robots.txt respect (pure Python)
radar/shopify_data.py       Shopify-native parsing: catalog JSON, JSON-LD, cart.js (unit-tested)
radar/journeys/shopify.py   home -> catalog API -> in-stock product -> PDP data -> add to cart -> cart.js
radar/confirm.py            retry + confirm logic (pure Python)
runner.py                   ties it together, saves evidence locally
tests/                      22 unit tests (no browser needed)
```

### How a run works
1. `runner.py sites/vaaree.yml` loads the config. `site_id` = lowercase host without `www.` (`vaaree.com`).
2. A fresh browser context starts (clear User-Agent `BugRadar/0.1 (+bugradar.in)`, locale en-IN, timezone Asia/Kolkata, tracing on).
3. The journey walks the buying path. Each step is timed and logged pass/fail/warn. Hard steps fail the run; soft steps (our own check might be wrong, e.g. visible price text on a custom theme) only warn.
4. If a step fails: screenshot + Playwright trace are saved, the context is thrown away, and the journey is retried in a **fresh context** (max 2 retries).
5. Verdict: `pass`, `flaky` (failed once, passed on retry), or `confirmed_fail` (2 of 3 attempts failed).
6. Everything lands in `evidence/<site_id>/<yyyymmdd>/<run_id>/` including `run.json`.

### Safety rules in code
- The journey **never clicks checkout**; it only asserts the button is visible. No order, no payment.
- No form fills, no accounts, no phone numbers.
- 2-second pause between navigations (configurable per site).
- No stealth, no fingerprint spoofing. A site that blocks us stays on the manual track.

---

## Honest status: what is proven and what is not

| Part | Status |
|---|---|
| Config, site_id, confirm/retry, Shopify data parsing, in-stock pick, cart check, platform detection, robots rules | **Tested** (22 unit tests pass) |
| Runner, retry in fresh contexts, evidence capture | **Proven on a real run (Vaaree, 3 Oct):** reached the product page, failed, retried, confirmed, saved run.json. |
| Revision-2 journey (catalog API, structured data, cart.js) | **Written and compiles, NOT yet run.** The build workspace has no Chromium and cannot reach vaaree.com. |
| Vaaree selectors | Only add-to-cart, checkout button and nav are still selector-based, and are **unverified guesses**. Price/title/image no longer depend on CSS. |
| Assumption: Vaaree exposes `/collections/all/products.json` and JSON-LD on product pages | **Unverified.** If either is blocked or absent the run fails with a clear reason. |

---

## Findings from real runs and site checks (3 Oct 2026)

- **Vaaree is NOT Shopify.** Its pages use `/products/` and `/collections/` URLs like Shopify, but assets come from `cdn.vaaree.com` and `/products.json` returns 404. Our Shopify-only assumption was wrong for it. Radar now reports verdict `unsupported` for such a site instead of a confusing failure. `sites/vaaree.yml` stays as the negative test.
- **Moxie Beauty and Palmonas are Shopify** (CDN, `/cdn/shop/`, digital-wallet meta). Moxie Beauty is the first real test site. Supply6 could not be checked (its certificate did not match `www.supply6.com`).
- **First real run proved:** browser launch, the fresh-context retry, trace and screenshot capture, `run.json`, and that a wrong assumption surfaces as a precise `reason`.

## Pain points (known, current)

0. **robots.txt vs the shopper flow (open policy decision).** Radar now respects robots.txt for everything crawl-like (home, collections, catalog, product pages). Shopify's default robots.txt disallows `/cart*`, so the add-to-cart simulation is exempt in code. That is defensible for a client who has asked for monitoring, less clear for a prospect who has not. Decide before running the cart step on prospect sites.

1. **Selectors are the fragile part.** Shopify themes differ; the same button can be `button[name=add]`, a custom element, or inside an iframe. Layer 1 handles this with per-site selector lists. This is exactly where LLM healing will plug in (`LocatorNotFound` already carries what was tried), but it is not built.
2. **Custom User-Agent can itself get us blocked.** Honest identification is the principle, but some CDNs and bot filters block unknown agents or headless Chromium regardless. Plan: log the block type, keep the site manual, offer an allowlist to paying clients.
3. **Variants (mitigated).** The journey pins an available variant through the product URL (`?variant=<id>`), so it does not click swatches. Stores whose themes ignore the URL variant could still fail at add-to-cart.
4. **Product choice (mitigated).** The journey picks the first in-stock product from the catalog JSON. Stores that disable `/products.json` fail at `catalog_has_in_stock_product`; a non-JSON crawl is not built.
5. **Add to cart creates a real cart session** on the live store. No order, but it is traffic the site did not agree to. Keep frequency low.
6. **Cart check (fixed in rev 2).** The cart is now confirmed through `/cart.js` containing the exact variant, not through page markup. The checkout-button check is still selector-based.
7. **GoKwik / Shopflo checkouts** (Moxie Beauty, Palmonas, Supply6): v0 stops at the cart, so checkout-widget coverage is **not** tested yet.
8. **Results are local files only.** No Supabase, no dedupe across runs, no alerts. Incident signature is computed but nothing stores or alerts on it yet.
9. **No scheduler.** Run by hand or local cron.
10. **Journey is hand-written config, not auto-built.** The "input a URL only" goal needs the discovery layer (Layer 3).

---

## Storage design (target; only the evidence folder exists today)

- Key: `site_id`. One database, one bucket, isolation by `site_id` plus row-level security.
- Files: `evidence/<site_id>/<yyyymmdd>/<run_id>/` today, same shape in Supabase Storage later.
- Retention (planned): passing screenshots 7 days, failure evidence 90 days, locator and journey caches until invalidated.

## Layer plan

1. **Runner + first journey** (this layer, awaiting first real run)
2. Confirm logic wired to Supabase + incident dedupe
3. Auto-discovery from a URL (Shopify `/products.json`, nav crawl)
4. LLM journey generation + locator healing (provider-agnostic; Haiku 4.5 first)
5. Dashboard (Next.js reading Supabase), each module with its own visual panel

## Change log
- 3 Oct 2026 (rev 3): Vaaree real run showed `/products.json` 404 (not Shopify). Added platform detection (`unsupported` verdict, no retries) and robots.txt checks; added `sites/moxiebeauty.yml`. 22 unit tests pass; Moxie run pending.
- 3 Oct 2026: Layer 1 written. First real Vaaree run: engine, retry and evidence worked; failed at `pdp_has_title_price_image` because a guessed price selector matched nothing (our bug, not Vaaree's).
- 3 Oct 2026 (rev 2): journey rewritten to be data-first (catalog JSON, JSON-LD/meta, `/cart.js`), in-stock + variant pinning, soft vs hard steps. 15 unit tests pass; browser run pending.
