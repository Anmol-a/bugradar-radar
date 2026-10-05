"""Platform detection from homepage HTML. Pure Python, unit-tested.
First piece of auto-discovery: before testing anything, know what the site is.
"""

SHOPIFY_MARKERS = [
    "cdn.shopify.com",
    "/cdn/shop/",
    "myshopify.com",
    "shopify-digital-wallet",
    "Shopify.theme",
    "Shopify.shop",
    "shopify-features",
]


def detect_platform(html: str) -> dict:
    """Returns {platform: 'shopify'|'unknown', evidence: [markers found]}.
    Needs >=2 distinct markers to call it Shopify (one stray CDN link, e.g. an
    embedded video, is not enough)."""
    html = html or ""
    found = [m for m in SHOPIFY_MARKERS if m in html]
    return {"platform": "shopify" if len(found) >= 2 else "unknown", "evidence": found}


CHECKOUT_APPS = [
    ("GoKwik", ("gokwik",)),
    ("Shopflo", ("shopflo",)),
    ("Shiprocket Fastrr", ("fastrr", "shiprocket checkout")),
    ("Razorpay Magic Checkout", ("magic-checkout", "magiccheckout", "razorpay magic")),
    ("Simpl", ("getsimpl.com", "simpl-checkout")),
    ("Zecpe", ("zecpe",)),
]


def detect_checkout_app(html: str) -> str:
    """Third-party one-click checkouts common on Indian Shopify stores; else Shopify's own."""
    low = (html or "").lower()
    found = [name for name, keys in CHECKOUT_APPS if any(k in low for k in keys)]
    return " + ".join(found) if found else "Shopify checkout"


def detect_access(status: int | None, title: str, html: str, url_path: str) -> str:
    """open | password | bot_blocked. Pure function, unit-tested."""
    low = (html or "")[:200_000].lower()
    t = (title or "").lower()
    if url_path.rstrip("/").endswith("/password") or 'action="/password"' in low or "form_type=\"storefront_password" in low \
            or 'value="storefront_password"' in low:
        return "password"
    if (status in (403, 429, 503) and ("cf-chl" in low or "challenge-platform" in low or "captcha" in low)) \
            or t.startswith("just a moment") or t.startswith("attention required") or "cf-chl-widget" in low:
        return "bot_blocked"
    return "open"
