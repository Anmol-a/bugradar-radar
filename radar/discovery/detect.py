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


# Journeys 33 + 35 (12 Oct 2026): the hosts each third-party checkout app serves its checkout from (the iframe src, the
# popup / redirect address, the cart page's script src). THE one list: add a new app's hosts here, nothing else.
CHECKOUT_APP_HOSTS: dict[str, tuple[str, ...]] = {
    "GoKwik": ("gokwik.co", "gokwik.in"),
    "Shopflo": ("shopflo.co", "shopflo.com"),
    "Shiprocket Fastrr": ("fastrr.com", "pickrr.com", "shiprocket.in", "shiprocket.co"),
    "Razorpay Magic Checkout": ("razorpay.com", "rzp.io"),
    "Simpl": ("getsimpl.com", "simpl.in"),
    "Zecpe": ("zecpe.com",),
}


def checkout_app_of(url: str) -> str:
    """Which third-party checkout app a URL belongs to: by its host (CHECKOUT_APP_HOSTS, suffix match), else by the
    app's name in the address (e.g. a store-hosted '/apps/gokwik/...' proxy). '' = none. Pure, unit-tested."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url or "")
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    for name, hosts in CHECKOUT_APP_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    rest = (u.path + "?" + u.query).lower()
    return next((name for name, keys in CHECKOUT_APPS if any(k in rest for k in keys)), "")


def checkout_apps_in(urls, html: str = "") -> list[str]:
    """Every third-party checkout app named by these script / iframe URLs or the page's HTML, in CHECKOUT_APPS
    order. Pure, unit-tested."""
    found = {checkout_app_of(u) for u in urls or ()} - {""}
    low = (html or "").lower()
    found |= {name for name, keys in CHECKOUT_APPS if any(k in low for k in keys)}
    return [name for name, _ in CHECKOUT_APPS if name in found]


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
