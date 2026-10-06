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


def products_from_json(products_json: dict, base_url: str, limit: int = 10) -> list[dict]:
    """Shopify /products.json -> list of product dicts, in-stock first.
    Each: {handle, title, url, variant_id (first available, else first), price, available, variants}."""
    out = []
    for p in (products_json or {}).get("products", []):
        variants = p.get("variants") or []
        if not variants or not p.get("handle"):
            continue
        avail = [v for v in variants if v.get("available")]
        v = avail[0] if avail else variants[0]
        out.append({
            "handle": p["handle"], "title": p.get("title", ""),
            "url": f"{base_url}/products/{p['handle']}",
            "variant_id": v.get("id"), "price": v.get("price"),
            "available": bool(avail), "variants": len(variants), "vendor": p.get("vendor") or "",
        })
    out.sort(key=lambda d: not d["available"])
    return out[:limit]


def product_from_js(product_js: dict, base_url: str) -> dict | None:
    """Shopify /products/<handle>.js -> same shape as products_from_json items.
    Note: .js prices are in paise/cents (integers); converted to a decimal string."""
    if not product_js or not product_js.get("handle"):
        return None
    variants = product_js.get("variants") or []
    avail = [v for v in variants if v.get("available")]
    v = avail[0] if avail else (variants[0] if variants else {})
    price = v.get("price")
    if isinstance(price, int):
        price = f"{price / 100:.2f}"
    return {
        "handle": product_js["handle"], "title": product_js.get("title", ""),
        "url": f"{base_url}/products/{product_js['handle']}",
        "variant_id": v.get("id"), "price": price,
        "available": bool(avail), "variants": len(variants),
    }


# ---------------- add-to-cart verification (pure, unit-tested) ----------------

def _lines(cart: dict) -> dict:
    """variant_id -> {qty, product_id, title, price(paise), handle} for a /cart.js payload."""
    out = {}
    for i in (cart or {}).get("items", []):
        vid = i.get("variant_id") or i.get("id")
        if vid is None:
            continue
        cur = out.setdefault(int(vid), {"qty": 0, "product_id": i.get("product_id"), "handle": i.get("handle"),
                                        "title": i.get("product_title") or i.get("title") or str(vid),
                                        "price": i.get("price")})
        cur["qty"] += int(i.get("quantity") or 0)
    return out


def parse_sent_variant_ids(post_data: str | None) -> list[int]:
    """Variant ids a page sent to /cart/add: form-urlencoded, multipart, or JSON ({id}|{items:[{id}]})."""
    import json as _json
    import re as _re
    from urllib.parse import parse_qs
    if not post_data:
        return []
    s = post_data.strip()
    ids: list[int] = []
    if s.startswith("{"):
        try:
            d = _json.loads(s)
            items = d.get("items") or [d]
            ids = [int(i["id"]) for i in items if str(i.get("id", "")).isdigit()]
        except (ValueError, TypeError):
            ids = []
    elif "Content-Disposition" in s or "content-disposition" in s:
        ids = [int(v) for v in _re.findall(r'name="(?:id|items\[\d*\]\[id\])"\r?\n\r?\n(\d+)', s)]
    else:
        q = parse_qs(s)
        for k in ("id", "items[][id]", "items[0][id]"):
            ids += [int(v) for v in q.get(k, []) if v.isdigit()]
    return ids


def assess_add(before: dict, after: dict, product: dict, expected_variant: int | None,
               sent_ids: list[int]) -> dict:
    """Compare the cart before and after ONE add-to-cart click for `product` (/products/<h>.js shape).

    Returns {added: [...lines that grew], target: line|None, paid_extras: [...], free_extras: [...],
             sent_ok: bool|None, sent_foreign: [...]} where every line is
             {variant_id, product_id, title, qty_delta, price}."""
    b, a = _lines(before), _lines(after)
    pvars = {int(v["id"]): v for v in product.get("variants", [])}
    added = []
    for vid, line in a.items():
        delta = line["qty"] - b.get(vid, {}).get("qty", 0)
        if delta > 0:
            added.append({"variant_id": vid, "product_id": line["product_id"], "title": line["title"],
                          "qty_delta": delta, "price": line["price"]})
    def is_target(l):
        if expected_variant is not None:
            return l["variant_id"] == int(expected_variant)
        return l["variant_id"] in pvars or (l["product_id"] is not None and l["product_id"] == product.get("id"))
    target = next((l for l in added if is_target(l)), None)
    extras = [l for l in added if l is not target]
    paid = [l for l in extras if (l["price"] or 0) > 0]
    free = [l for l in extras if not (l["price"] or 0) > 0]
    foreign = [i for i in sent_ids if i not in pvars]
    return {"added": added, "target": target, "paid_extras": paid, "free_extras": free,
            "sent_ok": (not foreign) if sent_ids else None, "sent_foreign": foreign}


def rupees(paise) -> str:
    try:
        return f"₹{int(paise) / 100:,.2f}"
    except (TypeError, ValueError):
        return str(paise)


def norm_text(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def prices_in_text(text: str) -> set[float]:
    """Every number that looks like a price in visible page text ('₹1,059.00' -> 1059.0)."""
    import re as _re
    out = set()
    for m in _re.findall(r"\d[\d,]*(?:\.\d{1,2})?", text or ""):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out
