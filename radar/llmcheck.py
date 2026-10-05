"""`radar llm-check`: score the configured LLM on fixed failure cases with known right answers.

Used to choose a model on evidence (gpt-4o-mini vs gemini-2.5-flash-lite vs deepseek-chat ...) and to
re-check after every model switch. Cases come from real bench evidence (4 Oct) and the mock store.
Text-only (no screenshots) so every provider, including text-only DeepSeek, is scored the same way.

HELD-OUT cases (held_out=True) were written AFTER the first gpt-4o-mini run (7/12, 5 Oct) and were not
used while improving the prompt: they show whether a prompt change generalises or only fits the 12.
"""
from __future__ import annotations

import time

from radar.healing.llm import LLMClient
from radar.healing.triage import triage

ok_ = lambda what, exp, act: {"what": what, "expected": exp, "actual": act, "ok": True}
bad = lambda what, exp, act: {"what": what, "expected": exp, "actual": act, "ok": False}

CASES = [
    {"id": "snitch_store_moved", "want": {"real_store_problem"}, "source": "snitch.co.in, bench 2",
     "title": "PDP 'Textured Knit Washed Sweater'", "check": "product_page", "step": "shows_title_price_image",
     "error": "product name shown on the page: expected Textured Knit Washed Sweater, got none",
     "checks": [bad("product name shown on the page", "Textured Knit Washed Sweater", "none")],
     "url": "https://www.snitch.co.in/products/relaxed-fit-textured-sweater-4sw0003-03",
     "text": "WE HAVE MOVED TO SNITCH.COM UPGRADE NOW\nLog in\nNEW ARRIVALS\nBESTSELLERS\nSALE\nsnitch.co.in is now "
             "SNITCH.COM Cooler. Bolder. Snappier. UPGRADE NOW\nBag\nSubtotal INR 0"},
    {"id": "plum_hidden_gift_sku", "want": {"radar_problem"}, "source": "plumgoodness.com, bench 2",
     "title": "PDP 'Skincare Duo (face wash 50 ml + sunscreen 30 g)'", "check": "product_page",
     "step": "shows_title_price_image",
     "error": "product name shown on the page: expected Skincare Duo (face wash 50 ml + sunscreen 30 g), got none",
     "checks": [bad("product name shown on the page", "Skincare Duo (face wash 50 ml + sunscreen 30 g)", "none")],
     "url": "https://plumgoodness.com/products/skincare-duo-face-wash-50-ml-sunscreen-30-g",
     "text": "Flat 20% OFF sitewide\nShop Categories Blogs\nMy cart Your cart is empty\nThe product is currently "
             "unavailable, please get back later.\nShop all\n(Radar picked this product from the store's catalog "
             "feed: price Rs 1.00, not listed in any collection.)"},
    {"id": "publisher_title_in_h2", "want": {"radar_problem"}, "source": "theme-publisher demo, bench 2",
     "title": "PDP 'Actual Source Official Flying Disc'", "check": "product_page", "step": "shows_title_price_image",
     "error": "product name shown on the page: expected Actual Source Official Flying Disc, got none",
     "checks": [bad("product name shown on the page", "Actual Source Official Flying Disc", "none")],
     "url": "https://theme-publisher-demo.myshopify.com/products/actual-source-official-flying-disc",
     "text": "Regular price $26.00 USD\nQuantity\nAdd to cart\nBuy it now\nActual Source Official Flying Disc\n"
             "A classic flying disc made for the park."},
    {"id": "spotlight_click_intercepted", "want": {"radar_problem"}, "source": "theme-spotlight demo, bench 2",
     "title": "Home → collection → product → cart", "check": "shopper_journey", "step": "click_into_product",
     "error": "could not click: <div class=\"card__inner\"></div> subtree intercepts pointer events",
     "checks": [ok_("product card a shopper can click", "found", "/products/ebbets-corduroy-cap-yellow")],
     "url": "https://theme-spotlight-demo.myshopify.com/",
     "text": "Featured collection\nEbbets Corduroy Cap - Yellow $48.00\nEbbets Wool C Cap - Camel $48.00\n"
             "Ebbets Wool C Cap - Navy $48.00\nView all"},
    {"id": "newsletter_covers_page", "want": {"radar_problem"}, "source": "mock newsletter_popup",
     "title": "Home → collection → product → cart", "check": "shopper_journey", "step": "click_into_product",
     "error": "product card a shopper can click: expected found, got none on / (24 links: 24 covered by div.klaviyo-form)",
     "checks": [bad("product card a shopper can click", "found", "none on / (24 links: 24 covered by div.klaviyo-form)")],
     "url": "https://example-store.in/",
     "text": "Get 10% off your first order!\nEmail\nSubscribe\n×\nBestsellers\nCeramic Flower Vase Rs 1,299"},
    {"id": "cart_api_500", "want": {"real_store_problem"}, "source": "mock cart_broken",
     "title": "Add 'Ceramic Flower Vase' to cart", "check": "add_to_cart", "step": "cart_received_this_product",
     "error": "cart item count: expected 0 → 1, got 0 → 0",
     "checks": [ok_("variant the page sent to /cart/add", "201", "201 (Ceramic Flower Vase)"),
                bad("add request response", "HTTP 200", "HTTP 500 {\"description\": \"cart service down\"}"),
                bad("cart item count", "0 → 1", "0 → 0")],
     "url": "https://example-store.in/products/ceramic-vase",
     "text": "Ceramic Flower Vase\nRs 1,299.00\nAdd to cart\nSomething went wrong. Please try again."},
    {"id": "wrong_product_added", "want": {"real_store_problem"}, "source": "mock wrong_variant",
     "title": "Add 'Ceramic Flower Vase' to cart", "check": "add_to_cart", "step": "cart_received_this_product",
     "error": "product added to cart: expected Ceramic Flower Vase (variant 201), got Wooden Spoon Set (variant 301)",
     "checks": [bad("variant the page sent to /cart/add", "201 (Ceramic Flower Vase)", "301 (Wooden Spoon Set)"),
                bad("product added to cart", "Ceramic Flower Vase", "Wooden Spoon Set")],
     "url": "https://example-store.in/products/ceramic-vase",
     "text": "Ceramic Flower Vase\nRs 1,299.00\nAdd to cart\nYour cart\nWooden Spoon Set\nCheck out"},
    {"id": "store_503", "want": {"real_store_problem"}, "source": "synthetic outage",
     "title": "Homepage loads healthy", "check": "page_health", "step": "loads",
     "error": "HTTP status: expected < 400, got 503", "checks": [bad("HTTP status", "< 400", "503")],
     "url": "https://example-store.in/", "text": "503 Service Temporarily Unavailable\nnginx"},
    {"id": "price_zero_on_page", "want": {"real_store_problem"}, "source": "mock broken_price",
     "title": "PDP 'Ceramic Flower Vase'", "check": "product_page", "step": "structured_data_valid",
     "error": "structured data: price: expected > 0, got 0",
     "checks": [ok_("structured data: name", "present", "Ceramic Flower Vase"), bad("structured data: price", "> 0", "0")],
     "url": "https://example-store.in/products/ceramic-vase",
     "text": "Ceramic Flower Vase\nRs 0.00\nAdd to cart"},
    {"id": "checkout_disabled", "want": {"real_store_problem"}, "source": "synthetic",
     "title": "Add 'Ceramic Flower Vase' to cart", "check": "add_to_cart", "step": "checkout_button_ready",
     "error": "checkout button enabled: expected True, got False",
     "checks": [ok_("product in cart", "Ceramic Flower Vase", "Ceramic Flower Vase x1"),
                bad("checkout button enabled", True, False)],
     "url": "https://example-store.in/cart",
     "text": "Your cart\nCeramic Flower Vase x1 Rs 1,299.00\nSubtotal Rs 1,299.00\nCheck out (unavailable)"},
    {"id": "page_read_too_early", "want": {"radar_problem", "unsure"}, "source": "synthetic timing",
     "title": "Collection 'Home Decor' lists products", "check": "collection_page", "step": "lists_products",
     "error": "products listed in collection: expected ≥ 1, got 0",
     "checks": [bad("products listed in collection", "≥ 1", "0")],
     "url": "https://example-store.in/collections/home-decor",
     "text": "Home Decor\nLoading products…\nFilter Sort"},
    {"id": "renamed_display_title", "want": {"radar_problem"}, "source": "mock renamed_title",
     "title": "PDP 'Ceramic Flower Vase'", "check": "product_page", "step": "shows_title_price_image",
     "error": "product name shown on the page: expected Ceramic Flower Vase, got none",
     "checks": [bad("product name shown on the page", "Ceramic Flower Vase", "none")],
     "url": "https://example-store.in/products/ceramic-vase",
     "text": "Handmade Vase in Clay\nRs 1,299.00\nAdd to cart\nA ceramic flower vase, hand thrown."},
]


CASES += [
    {"id": "cookie_banner_blocks_buy", "held_out": True, "want": {"radar_problem"}, "source": "held-out",
     "title": "Add 'Linen Shirt' to cart", "check": "add_to_cart", "step": "click_add_to_cart",
     "error": "could not click: <div id=\"cookie-consent\" class=\"cc-window\"></div> subtree intercepts pointer events",
     "checks": [ok_("buy button enabled", True, True)],
     "url": "https://example-store.in/products/linen-shirt",
     "text": "Linen Shirt\nRs 1,899.00\nSize S M L\nAdd to cart\nWe use cookies to improve your experience. "
             "Accept all Decline"},
    {"id": "search_returns_nothing", "held_out": True, "want": {"real_store_problem"}, "source": "held-out",
     "title": "Search for 'vase' returns products", "check": "search_results", "step": "search",
     "error": "products in search results: expected ≥ 1, got 0",
     "checks": [bad("products in search results", "≥ 1", "0")],
     "url": "https://example-store.in/search?q=vase&type=product",
     "text": "Search results\nNo results found for \u201cvase\u201d. Check the spelling or use a different word.\n"
             "Popular: Ceramic Flower Vase, Glass Bud Vase"},
    {"id": "cart_price_differs", "held_out": True, "want": {"real_store_problem"}, "source": "held-out",
     "title": "Add 'Ceramic Flower Vase' to cart", "check": "add_to_cart", "step": "cart_received_this_product",
     "error": "unit price in cart: expected ₹1,299.00, got ₹1,499.00",
     "checks": [ok_("product added to cart", "Ceramic Flower Vase", "Ceramic Flower Vase (variant 201)"),
                bad("unit price in cart", "₹1,299.00", "₹1,499.00")],
     "url": "https://example-store.in/products/ceramic-vase",
     "text": "Ceramic Flower Vase\nRs 1,299.00\nAdd to cart\nYour cart\nCeramic Flower Vase Rs 1,499.00\nCheck out"},
]


# Bench 3 (5 Oct, v0.7.2), real failures checked by hand from their traces. GPT-5 mini mislabelled the
# two soft-404s and mcaffeine's missing buy form as Radar problems; the facts for those were added after.
CASES += [
    {"id": "mcaffeine_no_buy_form", "want": {"real_store_problem"}, "source": "mcaffeine.com, bench 3",
     "title": "PDP 'Brightening Raspberry Rush Body Wash - 25 ml'", "check": "product_page", "step": "buy_button_ready",
     "error": "add-to-cart control for THIS product on its page: expected present, got none: no add-to-cart form for this "
              "product (18 such forms belong to other products' cards); no other buy control found",
     "checks": [bad("add-to-cart control for THIS product on its page", "present",
                    "none: no add-to-cart form for this product (18 such forms belong to other products' cards); "
                    "no other buy control found")],
     "url": "https://www.mcaffeine.com/products/brightening-raspberry-rush-body-wash-25-ml",
     "text": "Buy 2 & Get 2 FREE + FREE Full Size Product\nBrightening Raspberry Rush Body Wash - 25 ml\n(3)\nMRP Rs. 129\n"
             "(Inclusive of all taxes)\nAvailable Offers\nCheck estimated delivery date\nCHECK\nYou may also like\n"
             "Coffee Body Wash Rs. 349 Add to cart\nVitamin C Face Wash Rs. 299 Add to cart"},
    {"id": "soulflower_soft_404", "want": {"real_store_problem"}, "source": "soulflower.in, bench 3",
     "title": "Missing page returns 404", "check": "not_found", "step": "returns_404",
     "error": "HTTP status for a page that does not exist: expected 404, got 200 (redirected to /)",
     "checks": [bad("HTTP status for a page that does not exist", 404, "200 (redirected to /)")],
     "url": "https://soulflower.in/",
     "text": "IT'S OUR BIRTHDAY! BUY 1 GET 1 FREE IS LIVE SITEWIDE\nShop\nHair + Skin Quiz\nLearn\nBestsellers"},
    {"id": "soulflower_icon_popup", "want": {"radar_problem"}, "source": "soulflower.in, bench 3",
     "title": "PDP 'Rosemary Shampoo + Conditioner'", "check": "product_page", "step": "buy_button_ready",
     "error": "could not click: <div class=\"fixed inset-0 z-50 flex justify-center\"></div> subtree intercepts pointer events",
     "checks": [ok_("product name shown on the page", "Rosemary Shampoo + Conditioner", "'ROSEMARY SHAMPOO + CONDITIONER' (h1)")],
     "url": "https://soulflower.in/products/rosemary-shampoo-conditioner",
     "text": "ROSEMARY SHAMPOO + CONDITIONER\n(487)\nNEW LAUNCH: BOMB SIZE ROSEMARY HAIR SPRAY\nYour hair-care essential, now "
             "supersized.\nTRY IT NOW\nPowered by YourLio AI\nYou pay ₹900\nAdd to cart"},
    {"id": "thor_hover_menu", "want": {"radar_problem"}, "source": "thehouseofrare.com, bench 3",
     "title": "Home → collection → product", "check": "shopper_journey", "step": "click_into_product",
     "error": "product card a shopper can click: expected found, got none on / (25 links: 25 not visible)",
     "checks": [bad("product card a shopper can click", "found", "none on / (25 links: 25 not visible)")],
     "url": "https://thehouseofrare.com/",
     "text": "MEN\nWOMEN\nKIDS\nSHOES\nSEARCH\nLOG IN\nWISHLIST\nBAG (0)\nRARE RABBIT\nRAREISM\nRARE ONES\nRARE'Z"},
]


CASES += [
    {"id": "custom_buy_button_not_recognised", "want": {"radar_problem"}, "source": "mock obscure_button",
     "title": "PDP 'Ceramic Flower Vase'", "check": "product_page", "step": "buy_button_ready",
     "error": "add-to-cart control for THIS product on its page: expected present, got none: no add-to-cart form for this "
              "product; no other buy control found",
     "checks": [bad("add-to-cart control for THIS product on its page", "present",
                    "none: no add-to-cart form for this product; no other buy control found")],
     "url": "https://example-store.in/products/ceramic-vase",
     "text": "Ceramic Flower Vase\nRs 1,299.00\nGrab it\nShare\nHandmade in Khurja."},
    {"id": "supplysix_price_hidden_desktop", "want": {"real_store_problem"}, "source": "supplysix.com, bench 4",
     "title": "PDP 'Supply6 360 (Pack of 3)'", "check": "product_page", "step": "shows_title_price_image",
     "error": "selected variant price shown: expected ₹199.00, got not on page (₹199.00 is only inside a hidden element "
              "<product-sticky-form.product-sticky-form.hidden-lap-and-up>, not shown on this screen size)",
     "checks": [ok_("product name shown on the page", "Supply6 360 (Pack of 3)", "'Supply6 360 (Pack Of 3)' (h1, 40px, exact name)"),
                bad("selected variant price shown", "₹199.00", "not on page (₹199.00 is only inside a hidden element "
                    "<product-sticky-form.product-sticky-form.hidden-lap-and-up>, not shown on this screen size)")],
     "url": "https://supplysix.com/products/supply6-360-pack-of-3",
     "text": "Supply6 360 (Pack Of 3)\nDaily nutrition to support modern diets\nLoved by 2L+ Customers\nChoose Your 360:\n"
             "Original 360\n360 Mind + Body\nFlavour:\nUnflavoured\nGreen Apple\nADD TO CART\nZero Sugar\nNo Preservatives"},
]


def run(llm: LLMClient, progress=print) -> dict:
    """Score the model. Returns {rows, correct, total, calls, input_tokens, output_tokens, est_usd, secs}."""
    rows, t0 = [], time.time()
    for c in CASES:
        t = time.time()
        got = triage(llm, c["title"], c["check"], c["step"], c["error"], c["checks"], c["url"], c["text"], None)
        verdict = got["verdict"] if got else "no answer"
        ok = verdict in c["want"]
        rows.append({"id": c["id"], "want": "/".join(sorted(c["want"])), "got": verdict, "ok": ok,
                     "held_out": bool(c.get("held_out")),
                     "reason": (got or {}).get("reason", ""), "secs": round(time.time() - t, 1)})
        tag = " (held-out)" if c.get("held_out") else ""
        progress(f"  {'OK  ' if ok else 'MISS'} {c['id']:30} want {rows[-1]['want']:28} got {verdict}{tag}")
    u = llm.usage()
    return {"rows": rows, "correct": sum(r["ok"] for r in rows), "total": len(rows),
            "held_out": f"{sum(r['ok'] for r in rows if r['held_out'])}/{sum(r['held_out'] for r in rows)}",
            "calls": u["calls"],
            "input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"], "est_usd": u["est_usd"],
            "errors": u["errors"], "secs": round(time.time() - t0, 1)}
