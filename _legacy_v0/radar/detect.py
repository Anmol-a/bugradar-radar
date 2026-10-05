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
