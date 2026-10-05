"""Shopify-native data helpers. Pure Python, no browser: unit-tested.

Why: Shopify stores differ in theme CSS but all expose the same data:
  - /collections/<handle>/products.json   (catalog, variant availability)
  - JSON-LD / og: meta on product pages   (title, price, image)
  - /cart.js                              (what is actually in the cart)
Reading these is far more robust than guessing per-theme selectors.
"""
import json


def pick_available(products_json: dict) -> dict | None:
    """First product that has an available variant. Returns
    {handle, title, variant_id, price} or None."""
    for p in (products_json or {}).get("products", []):
        for v in p.get("variants", []):
            if v.get("available"):
                return {
                    "handle": p["handle"],
                    "title": p.get("title", ""),
                    "variant_id": v["id"],
                    "price": v.get("price"),
                }
    return None


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement"):
            if key in node:
                yield from _walk(node[key])


def _is_product(obj: dict) -> bool:
    t = obj.get("@type")
    return t == "Product" or (isinstance(t, list) and "Product" in t)


def parse_product_data(ld_scripts: list[str], meta: dict) -> dict:
    """Extract title/price/image/availability from JSON-LD, falling back to og/product meta.
    Returns dict with keys title, price, image, availability, source (any may be None)."""
    out = {"title": None, "price": None, "image": None, "availability": None, "source": None}
    for raw in ld_scripts or []:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for obj in _walk(data):
            if not _is_product(obj):
                continue
            out["title"] = obj.get("name")
            img = obj.get("image")
            out["image"] = img[0] if isinstance(img, list) and img else img
            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                out["price"] = offers.get("price") or offers.get("lowPrice")
                out["availability"] = offers.get("availability")
            out["source"] = "json-ld"
            break
        if out["source"]:
            break
    meta = meta or {}
    out["title"] = out["title"] or meta.get("og:title")
    out["image"] = out["image"] or meta.get("og:image")
    out["price"] = out["price"] or meta.get("product:price:amount") or meta.get("og:price:amount")
    if out["source"] is None and any(out[k] for k in ("title", "price", "image")):
        out["source"] = "meta"
    return out


def price_ok(price) -> bool:
    try:
        return float(str(price).replace(",", "")) > 0
    except (ValueError, TypeError):
        return False


def cart_has_variant(cart_json: dict, variant_id: int) -> bool:
    return any(i.get("variant_id") == variant_id or i.get("id") == variant_id
               for i in (cart_json or {}).get("items", []))
