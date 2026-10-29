"""What the store's own Shopify admin says, for stores that use the BugRadar Shopify app (29 Oct 2026).

Why: Shopify's App Store review (item 2.2.1 "Use Shopify APIs") asked the app to use Shopify's data, not only a browser.
The app now has three read-only scopes and, when the owner opens it, reads the store's menus, live products and live
theme through the Admin API (dashboard: supabase/functions/shopify_app/shopify_data.ts). The answer is kept on the
store's check (monitors.shopify_data) and comes to the worker with the claimed monitor row. Radar uses it for two checks:
    smoke.shopify_menu      every link in the store's Shopify menus opens (the menus as the admin defines them, not as
                            one theme happens to draw them: a link hidden in a drawer or a mega menu is still checked)
    product.shopify_admin   the price and stock a product page shows match the admin (a theme or app that shows an old
                            price, or offers a sold-out item as buyable)
Shape of shopify_data: {read_at, currency, menus:[{handle,title,links:[{title,url,type,depth}]}],
    products:[{handle,title,url,variants:[{title,price,compare_at,available,policy}]}], theme:{id,name,updated_at}, ...}
Links and product urls can be relative ("/collections/all"): Shopify gives no product address while a store is behind
a password. Pure Python, no browser: unit-tested in tests/test_shopify_admin.py.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_MENU_LINKS = 30          # menu links opened per run (a mega menu can list hundreds)
MAX_ADMIN_PRODUCTS = 3       # product pages compared with the admin per run
FRESH_HOURS = 24             # admin prices older than this are not compared (the owner hasn't opened the app since)

# menus a shopper doesn't browse: Shopify's customer-account menu (orders, profile: behind a sign-in)
SKIP_MENUS = {"customer-account-main-menu"}
# addresses that need a sign-in or change something: never opened from a menu check
SKIP_PATH = re.compile(r"^/(account|cart|checkout|checkouts|orders|tools|apps)(/|$)|^/[a-z]{2}(-[a-z]{2})?/(account|cart|checkout)(/|$)", re.I)


def _host(u: str) -> str:
    return (urlsplit(u).hostname or "").lower().removeprefix("www.")


def same_store(url: str, base_url: str) -> str | None:
    """The address on the store's own site for a menu link, or None for a link elsewhere. A relative link is joined to
    the store's address; an absolute one counts when it is on the store's host or the store's *.myshopify.com address
    (moved onto the store's own host: that is where shoppers are). The query is kept, the fragment dropped; the
    scheme is the store address's own."""
    url = (url or "").strip()
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    base = base_url.rstrip("/")
    full = urljoin(base + "/", url)
    p, b = urlsplit(full), urlsplit(base)
    if p.scheme not in ("http", "https"):
        return None
    if _host(full) != _host(base) and not (p.hostname or "").lower().endswith(".myshopify.com"):
        return None
    return urlunsplit((b.scheme or "https", b.netloc, p.path or "/", p.query, ""))


def menu_links(data: dict | None, base_url: str, cap: int = MAX_MENU_LINKS) -> tuple[list[dict], dict]:
    """The store's menu links as Radar opens them: [{text, url, menu}], one per address, in menu order (every menu's
    top level first, then deeper levels), capped. Also a note of what was left out: {offsite, skipped, over_cap}."""
    note = {"offsite": 0, "skipped": 0, "over_cap": 0}
    if not isinstance(data, dict):
        return [], note
    seen, rows = set(), []
    menus = [m for m in data.get("menus") or [] if isinstance(m, dict)]
    for depth in (1, 2, 3):
        for m in menus:
            if m.get("handle") in SKIP_MENUS:
                continue
            for link in m.get("links") or []:
                if not isinstance(link, dict) or int(link.get("depth") or 1) != depth:
                    continue
                url = same_store(str(link.get("url") or ""), base_url)
                if url is None:
                    note["offsite"] += 1
                    continue
                if SKIP_PATH.search(urlsplit(url).path):
                    note["skipped"] += 1
                    continue
                key = url.rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"text": str(link.get("title") or "")[:80], "url": url, "menu": str(m.get("title") or m.get("handle") or "")})
    note["over_cap"] = max(0, len(rows) - cap)
    return rows[:cap], note


def _price(v) -> float | None:
    try:
        x = round(float(str(v).replace(",", "")), 2)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def admin_products(data: dict | None, base_url: str, n: int = MAX_ADMIN_PRODUCTS) -> list[dict]:
    """Products to compare with the admin: [{handle, title, url, prices: [..], available}], in stock first, plus the
    first sold-out one (the stock half of the check needs one) when there is room. Products with no positive price
    (free samples, gifts) are left out."""
    if not isinstance(data, dict):
        return []
    rows = []
    for p in data.get("products") or []:
        if not isinstance(p, dict) or not p.get("handle"):
            continue
        variants = [v for v in p.get("variants") or [] if isinstance(v, dict)]
        prices = sorted({x for x in (_price(v.get("price")) for v in variants) if x is not None})
        if not prices:
            continue
        url = same_store(str(p.get("url") or f"/products/{p['handle']}"), base_url) or f"{base_url.rstrip('/')}/products/{p['handle']}"
        rows.append({"handle": str(p["handle"]), "title": str(p.get("title") or p["handle"])[:80], "url": url,
                     "prices": prices, "available": any(v.get("available") is True for v in variants)})
    live = [r for r in rows if r["available"]]
    sold = [r for r in rows if not r["available"]]
    picks = live[: max(0, n - 1)] if sold else live[:n]
    picks += sold[:1] if sold else []
    if len(picks) < n:          # not enough of one kind: fill with the other
        picks += [r for r in live + sold if r not in picks][: n - len(picks)]
    return picks[:n]


def read_at(data: dict | None) -> datetime | None:
    raw = str((data or {}).get("read_at") or "").strip() if isinstance(data, dict) else ""
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def fresh(data: dict | None, now: datetime | None = None, hours: int = FRESH_HOURS) -> bool:
    """Is the admin's data recent enough to compare prices with (read within the last `hours`)?"""
    t = read_at(data)
    if t is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - t).total_seconds()
    return 0 <= age <= hours * 3600 or -300 <= age < 0      # a clock a few minutes ahead still counts
