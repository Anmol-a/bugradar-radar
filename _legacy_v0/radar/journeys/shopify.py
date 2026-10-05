"""Shopify buying journey (v2): data-first, selectors only where unavoidable.

Flow: home -> catalog API -> pick IN-STOCK product -> product page (variant pinned by URL)
      -> structured-data checks -> add to cart -> /cart.js confirms -> cart page + checkout button visible.

HARD RULES (see docs/ARCHITECTURE.md):
- Never click the checkout button. We only assert it is visible. No order, no payment.
- Never fill a form, never create an account, never enter a phone number.
- Pause between navigations (access.delay_seconds).

Step kinds: hard steps fail the run; soft steps record a warning only
(used where our own check could be wrong, e.g. visible price text on a custom theme).
"""
import time
from dataclasses import dataclass, field
from playwright.sync_api import Page

from radar.config import SiteConfig
from radar.locate import first_visible
from radar.shopify_data import pick_available, parse_product_data, price_ok, cart_has_variant
from radar.detect import detect_platform
from radar.robots import Robots


class StepFailed(Exception):
    def __init__(self, step: str, message: str):
        self.step = step
        super().__init__(message)


@dataclass
class JourneyResult:
    ok: bool
    steps: list[dict] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    unsupported: bool = False      # site is not a platform Radar supports; do not retry
    robots_loaded: bool = False


def _pause(cfg: SiteConfig):
    time.sleep(cfg.access.delay_seconds)


def _step(result: JourneyResult, name: str, fn, soft: bool = False):
    t0 = time.time()
    try:
        detail = fn()
        result.steps.append({"name": name, "status": "pass", "detail": detail, "secs": round(time.time() - t0, 2)})
        return detail
    except Exception as e:  # noqa: BLE001
        secs = round(time.time() - t0, 2)
        if soft:
            result.steps.append({"name": name, "status": "warn", "error": str(e)[:300], "secs": secs})
            result.warnings.append(f"{name}: {str(e)[:200]}")
            return None
        result.steps.append({"name": name, "status": "fail", "error": str(e)[:300], "secs": secs})
        raise StepFailed(name, str(e)) from e


def _get_json(page: Page, url: str) -> dict:
    resp = page.context.request.get(url, timeout=20000)
    if resp.status >= 400:
        raise AssertionError(f"{url} returned HTTP {resp.status}")
    return resp.json()


def run_journey(page: Page, cfg: SiteConfig) -> JourneyResult:
    s, base = cfg.selectors, cfg.base_url
    res = JourneyResult(ok=False)
    console_errors: list[str] = []
    page.on("pageerror", lambda e: console_errors.append(str(e)[:200]))
    state: dict = {}

    # robots.txt: respected for everything crawl-like. /cart* is the shopper flow (add-to-cart
    # simulation); Shopify's default robots.txt disallows it for crawlers, so it is exempt here.
    # That exemption is a policy decision, see docs/ARCHITECTURE.md.
    try:
        rr = page.context.request.get(base + "/robots.txt", timeout=10000)
        robots = Robots(rr.text() if rr.status == 200 else None, cfg.access.user_agent)
    except Exception:  # noqa: BLE001
        robots = Robots(None, cfg.access.user_agent)
    res.robots_loaded = robots.loaded

    def guard(url: str):
        path = url.split(base, 1)[-1] if url.startswith(base) else url
        if path.startswith("/cart"):
            return
        if not robots.allowed(url):
            raise AssertionError(f"robots.txt disallows {url}; Radar does not fetch it")

    def load(url: str) -> str:
        guard(url)
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp is None or resp.status >= 400:
            raise AssertionError(f"{url} returned HTTP {resp.status if resp else 'none'}")
        if not page.title().strip():
            raise AssertionError(f"{url} has an empty <title>")
        return f"HTTP {resp.status}"

    try:
        _step(res, "home_loads", lambda: load(base + "/"))

        def platform():
            d = detect_platform(page.content())
            if d["platform"] != "shopify":
                res.unsupported = True
                raise AssertionError(
                    "this site does not look like Shopify (markers found: "
                    f"{d['evidence'] or 'none'}); v1 supports Shopify only")
            return f"shopify (markers: {', '.join(d['evidence'])})"
        _step(res, "platform_is_shopify", platform)
        _pause(cfg)

        def nav():
            n = page.locator(s.get("nav_links", ["nav a"])[0]).count()
            if n == 0:
                raise AssertionError("no navigation links found")
            return f"{n} nav links"
        _step(res, "nav_present", nav)

        def catalog():
            coll = cfg.seed.get("collection", "/collections/all")
            last = None
            for url in (f"{base}{coll}/products.json?limit=50", f"{base}/products.json?limit=50"):
                try:
                    guard(url)
                    pick = pick_available(_get_json(page, url))
                except Exception as e:  # noqa: BLE001
                    last = e
                    continue
                if pick:
                    state["pick"] = pick
                    return f"in-stock product: {pick['handle']} (variant {pick['variant_id']})"
                last = AssertionError(f"{url} has no in-stock product")
            raise AssertionError(f"could not pick an in-stock product: {last}")
        _step(res, "catalog_has_in_stock_product", catalog)
        _pause(cfg)

        pick = state["pick"]
        _step(res, "product_page_loads", lambda: load(f"{base}/products/{pick['handle']}?variant={pick['variant_id']}"))

        def pdp_data():
            lds = page.eval_on_selector_all("script[type='application/ld+json']", "els => els.map(e => e.textContent)")
            meta = page.evaluate(
                "() => Object.fromEntries([...document.querySelectorAll('meta[property]')]"
                ".map(m => [m.getAttribute('property'), m.getAttribute('content')]))")
            d = parse_product_data(lds, meta)
            problems = []
            if not (d["title"] or "").strip():
                problems.append("title missing")
            if not price_ok(d["price"]):
                problems.append(f"price missing or not positive ({d['price']!r})")
            if not d["image"]:
                problems.append("image missing")
            if problems:
                raise AssertionError("; ".join(problems) + f" [source={d['source']}]")
            state["pdp"] = d
            return f"title={d['title'][:50]!r} price={d['price']} source={d['source']}"
        _step(res, "pdp_structured_data_ok", pdp_data)

        # Soft: is a price actually visible to a shopper? Theme-dependent, so warn only.
        _step(res, "pdp_visible_price",
              lambda: first_visible(page, "pdp_price", s.get("pdp_price", ["[class*='price']"])).inner_text()[:40],
              soft=True)

        _step(res, "add_to_cart_click",
              lambda: first_visible(page, "add_to_cart", s["add_to_cart"]).click() or "clicked")

        def cart_api():
            for _ in range(8):                      # cart updates are async; poll up to ~6s
                time.sleep(0.75)
                cart = _get_json(page, f"{base}/cart.js")
                if cart_has_variant(cart, pick["variant_id"]):
                    return f"cart.js has variant {pick['variant_id']} (items: {cart.get('item_count')})"
            raise AssertionError("clicked Add to cart but /cart.js never contained the product")
        _step(res, "cart_contains_product", cart_api)
        _pause(cfg)

        def cart_page():
            load(base + "/cart")
            first_visible(page, "checkout_button", s.get("checkout_button", ["button[name='checkout']"]))
            return "cart page loads; checkout button visible, NOT clicked"
        _step(res, "cart_page_and_checkout_button", cart_page)

        res.ok = True
    except StepFailed as f:
        res.failed_step, res.error = f.step, str(f)
    if console_errors:
        res.steps.append({"name": "js_errors_seen", "status": "info", "detail": console_errors[:5]})
    return res
