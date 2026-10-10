"""Generator: SiteMap -> Suites of TestCases. No hand-written journeys per site.

Deterministic rules, not an LLM: the same site map always produces the same suites, so
results are comparable run to run. (LLM-proposed extra cases are a later layer.)

Suites and why they exist:
  journey  critical  Can a shopper get from the homepage to checkout by clicking? (one session)
  smoke    critical  Is the store up at all? Homepage health + nav links resolve.
  catalog  major     Can shoppers browse? Each sampled collection loads and lists products.
  product  major     Can shoppers evaluate? PDP loads, has title/price/image, buy button visible.
  cart     critical  Can shoppers buy? Add to cart -> cart has it -> checkout button visible.
  search   major     Can shoppers find? A search for a real product returns results.
  health   minor     SEO/meta basics and a correct 404.
  info     minor     Footer policy + contact pages have real content; the account login page opens.
"""
from __future__ import annotations

import re

from radar.core.config import Settings
from radar.core.models import SiteMap, Suite, TestCase
from radar.discovery.shopify_data import price_ok


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "x"


GENERIC = {"with", "pack", "size", "combo", "free", "mens", "women", "womens", "unisex", "kids", "new", "the",
           "and", "for", "set", "pcs", "piece", "pieces", "travel", "mini", "gift", "copy", "plain", "regular",
           # promo / bundle words: stores often keep these items out of search on purpose (plumgoodness.com
           # 'Mystery Merch', bench 9: the store's search app showed 12 other products for 'mystery')
           "mystery", "merch", "bundle", "bundles", "sampler", "trial", "surprise", "offer", "offers", "freebie",
           "hamper", "limited", "edition", "exclusive", "special", "value", "saver", "deal", "deals", "full", "half"}


NO_MATCH_TERM = "qzxvbugradar"    # a word no store sells (journey 27)


def _stems(w: str) -> set[str]:
    """'plums' -> {'plums', 'plum'}; 'berries' -> {'berries', 'berry'}; 'glasses' -> {'glasses', 'glass'}."""
    out = {w}
    if w.endswith("ies") and len(w) > 4:
        out.add(w[:-3] + "y")
    if w.endswith("es") and len(w) > 4:
        out.add(w[:-2])
    if w.endswith("s") and len(w) > 3:
        out.add(w[:-1])
    return out


def _search_term(handle: str, title: str, brand_words: set[str]) -> str:
    """A word a shopper would search for: from the product handle/title, never the brand name or
    a generic word, letters only, 4+ chars. Pure, unit-tested."""
    def brand(w: str) -> bool:
        # a brand word, or part of a run-together brand like the domain label 'thehouseofrare' ('rare', 'house'):
        # thehouseofrare.com, bench 7 searched 'rare' and every product matched by brand, none by name
        # plural of a brand word too: plumgoodness.com, bench 9 searched 'plums' and got all 246 products by brand
        return any(s in brand_words or any(len(b) >= len(s) + 3 and s in b for b in brand_words) for s in _stems(w))
    for source in (handle.replace("-", " "), title):
        for w in re.findall(r"[A-Za-z]{4,}", source or ""):
            if not brand(w.lower()) and w.lower() not in GENERIC:
                return w.lower()
    return ""


def _brand_words(sm: SiteMap) -> set[str]:
    """Store-name words: the domain label (whole and hyphen-split) and EVERY part of the homepage title
    ('Premium Clothing Brand in India - The House of Rare': the brand is after the dash)."""
    label = sm.site_id.split(".")[0]
    words = {label.replace("-", "")} | set(re.findall(r"[a-z]{3,}", label.replace("-", " ")))
    words |= {w.lower() for w in re.findall(r"[A-Za-z]{3,}", sm.home_title or "")}
    for p in sm.products:                    # Shopify 'vendor' = the brand(s): 'Rare Rabbit', 'Rareism', 'Thor'
        v = getattr(p, "vendor", "") or ""
        words |= {w.lower() for w in re.findall(r"[A-Za-z]{3,}", v)} | ({re.sub(r"[^a-z]", "", v.lower())} if v else set())
    return words


def build_suites(sm: SiteMap, s: Settings) -> list[Suite]:
    if sm.platform != "shopify":
        return []
    base = sm.base_url
    suites: list[Suite] = []

    priced = [p for p in sm.products if price_ok(p.price)]          # skip free samples / gifts
    if sm.collections and priced:
        cart = " → add to cart → cart → checkout button" if s.allow_cart_flow else ""
        suites.append(Suite("journey", "Shopper journey", "Click through the store like a shopper, one session", [
            TestCase("journey.shopper", "journey", f"Home → collection → product{cart}", "shopper_journey",
                     {"home": base + "/", "collection_url": sm.collections[0].url,
                      "product_handles": [p.handle for p in priced if p.available][:15],
                      "allow_cart": s.allow_cart_flow, "cart_path": sm.cart_path}, "critical",
                     "End to end by clicking: menu link, product card, buy button, cart. Checkout never clicked.")]))

    smoke = Suite("smoke", "Smoke", "Store is up and navigable")
    smoke.cases.append(TestCase("smoke.home_health", "smoke", "Homepage loads healthy", "page_health",
                                {"url": base + "/", "max_load_secs": 8}, "critical",
                                "HTTP < 400, has title, no JS errors, images load, under 8s"))
    if sm.nav:
        smoke.cases.append(TestCase("smoke.nav_links", "smoke", "Navigation links resolve", "links_resolve",
                                    {"urls": [n["url"] for n in sm.nav[:15]], "max_links": s.max_nav_links}, "major",
                                    "Each menu page opens in the browser and shows content"))
    suites.append(smoke)

    catalog = Suite("catalog", "Catalog", "Collections load and list products")
    for c in sm.collections[: s.max_collections]:
        catalog.cases.append(TestCase(f"catalog.collection.{_slug(c.handle)}", "catalog",
                                      f"Collection '{c.title}' lists products", "collection_page",
                                      {"url": c.url}, "major"))
    suites.append(catalog)

    picks = [p for p in priced if p.available][: s.max_products] or priced[: s.max_products]
    product = Suite("product", "Product pages", "PDPs show what a shopper needs")
    spare = [p for p in priced if p.available and p not in picks]      # used only if a pick's page is unreachable
    vurl = lambda p: p.url + (f"?variant={p.variant_id}" if p.variant_id else "")
    for i, p in enumerate(picks):
        product.cases.append(TestCase(
            f"product.pdp.{_slug(p.handle)}", "product", f"PDP '{p.title[:50]}'", "product_page",
            {"url": vurl(p), "expect_buyable": p.available,
             "fallbacks": [vurl(x) for x in spare[i * 2:(i + 1) * 2]] if p.available else []}, "major",
            "Structured title/price/image present, price > 0, buy button visible when in stock"))
    if product.cases:
        suites.append(product)

    buyable = next((p for p in priced if p.available and p.variant_id), None)
    if buyable and s.allow_cart_flow:
        suites.append(Suite("cart", "Cart", "Shopper can add to cart and reach checkout", [TestCase(
            "cart.add_to_cart", "cart", f"Add '{buyable.title[:40]}' to cart", "add_to_cart",
            {"url": f"{buyable.url}?variant={buyable.variant_id}", "variant_id": buyable.variant_id,
             "cart_path": sm.cart_path}, "critical",
            "Click add to cart, /cart.js contains the variant, checkout button visible (never clicked)")]))

    terms = []
    for p in picks + spare:
        t = _search_term(p.handle, p.title, _brand_words(sm))
        if t and t not in terms:
            terms.append(t)
        if len(terms) == 3:          # up to 3 words: the search FAILS only if all of them find nothing relevant
            break
    if terms and sm.search_path:
        q = lambda t: f"{base}{sm.search_path}?q={t}&type=product"
        suites.append(Suite("search", "Search", "Search returns real products", [TestCase(
            f"search.{_slug(terms[0])}", "search", f"Search for '{terms[0]}' returns products", "search_results",
            {"url": q(terms[0]), "alt_urls": [q(t) for t in terms[1:]]}, "major"),
            # journey 27: a word no store sells still gives a working 'no results' page; search-as-you-type suggests
            TestCase("search.no_results", "search", "Search for a word no store sells shows a 'no results' page",
                     "search_no_results", {"url": q(NO_MATCH_TERM)}, "minor",
                     "The search page opens (not an error or blank page) and tells the shopper nothing matched"),
            TestCase("search.suggestions", "search", f"Typing '{terms[0]}' in the search box suggests products",
                     "search_suggestions", {"home": base + "/", "term": terms[0]}, "minor",
                     "Judged only when the store has search-as-you-type; Enter is never pressed")]))

    health = Suite("health", "Health & SEO", "Basics that cost traffic when missing")
    health.cases.append(TestCase("health.meta.home", "health", "Homepage meta tags", "meta_tags",
                                 {"url": base + "/"}, "seo"))
    if picks:
        health.cases.append(TestCase("health.meta.pdp", "health", "Product page meta tags", "meta_tags",
                                     {"url": picks[0].url}, "seo"))
    health.cases.append(TestCase("health.not_found", "health", "Missing page returns 404", "not_found",
                                 {"url": base + "/products/bugradar-check-does-not-exist"}, "seo"))
    suites.append(health)

    # Journeys 28 + 29: the pages a shopper checks before trusting a store with money. Only links robots.txt
    # allows reach here (discovery lists the rest in the notes).
    info = Suite("info", "Store info pages", "Policy, contact and account pages a shopper checks before buying")
    if sm.info_pages:
        kinds = ", ".join(p["kind"] for p in sm.info_pages)
        info.cases.append(TestCase("info.policy_pages", "info", f"Footer info pages load with real content ({kinds})",
                                   "info_pages", {"pages": sm.info_pages[:6]}, "minor",
                                   "Each footer policy / contact link opens, is not an error page or the homepage, and "
                                   "shows real text (contact: a form, an email or a phone number)"))
    if sm.account_url:
        info.cases.append(TestCase("info.account_page", "info", "Account login page loads", "account_page",
                                   {"url": sm.account_url}, "minor",
                                   "The header's account link opens a sign-in page. Nothing is typed or submitted"))
    suites.append(info)
    return [x for x in suites if x.cases]
