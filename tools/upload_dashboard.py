"""Send Radar results to the BugRadar dashboard (one dashboard run per store and device).

  BUGRADAR_KEY=brk_... python3 tools/upload_dashboard.py <results folder or a run folder> [--build 2026-10-12] [--ci-url URL]
  python3 tools/upload_dashboard.py <folder> --dry-run          # print what would be sent, send nothing

Finds every sites/<store>/<run>/run.json under the folder. Each store becomes a dashboard project (named by its address),
each day a build, so old and new results are told apart by date. Screenshots of failing checks go along as evidence.
Standard library only. The upload key comes from the BUGRADAR_KEY environment variable; it is never written anywhere.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

INGEST_URL = os.environ.get("BUGRADAR_INGEST_URL", "https://onijwpgvpojpvgumvfim.supabase.co/functions/v1/ingest")
STATUS = {"pass": "passed", "flaky": "flaky", "confirmed_fail": "failed", "fail": "failed", "blocked": "blocked",
          "skipped": "skipped", "no_network": "skipped", "pending": "skipped"}
MAX_SHOTS_PER_RUN = 6
MAX_SHOT_BYTES = 600_000
SHOT_WIDTH = 900
# a build is one day of results; the day is counted where the store's shoppers are (a 5 am India run belongs to that
# day, not to the UTC day before). BUGRADAR_TZ overrides.
TZ = os.environ.get("BUGRADAR_TZ", "Asia/Kolkata")
# the triage label comes from an LLM. It is NEVER shown to the store owner (28 Oct: a tester read "the monitor likely
# looked in the wrong place ... this may be the test's fault" under a menu link that really answered HTTP 404). It
# goes into the technical `error` field, for engineers, under one of these labels.
TRIAGE_NOTE = {"real_store_problem": "likely a real store problem", "radar_problem": "may be the test's fault, not the store's",
               "unsure": "not yet confirmed as a store problem"}
# a run with no checks at all still has something to say: the store could not be opened or tested
SITE_KEY, SITE_NAME = "site.reachable", "Store opens and can be tested"
SITE_STATUS = {"unreachable": "failed", "blocked": "blocked"}          # anything else without checks: skipped
SITE_WHY = {"unsupported": "This store does not run on Shopify. Radar tests Shopify stores only for now."}


def build_day(iso: str | None) -> str:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(ZoneInfo(TZ)).date().isoformat()
    except (ValueError, KeyError):
        return (iso or "")[:10]


QUOTED = re.compile(r"'([^']{1,80})'")


def _quoted(title: str | None) -> str | None:
    """The product / collection / search word a case title names in quotes: \"PDP 'Vitamin C Serum'\" -> Vitamin C Serum.
    (radar/generate/builder.py writes every title with the name in single quotes.)"""
    m = QUOTED.search(title or "")
    return m.group(1) if m else None


# the title the dashboard shows for a check (it composes "<name>: stopped working ..." in emails, so this must be
# something a store owner would say). Fixed keys first; prefixes carry the product / collection / word from the title.
PLAIN_NAMES = {
    SITE_KEY: SITE_NAME,
    "smoke.nav_links": "Menu links", "smoke.home_health": "Homepage", "smoke.mobile_menu": "Mobile menu",
    "smoke.more_links": "Footer and banner links", "smoke.whatsapp": "WhatsApp button",
    "smoke.shopify_menu": "Shopify menu links", "product.shopify_admin": "Prices and stock match your Shopify admin",
    "journey.shopper": "Shopper's path",
    "cart.add_to_cart": "Add to cart", "cart.edit_and_checkout": "Cart page and checkout button",
    "cart.price_consistency": "Prices match across pages", "cart.drawer_and_page": "Cart drawer",
    "cart.discount_code": "Discount box", "cart.buy_now": "Buy it now",
    "search.no_results": "Search: no-results page", "search.suggestions": "Search suggestions",
    "product.variant_switch": "Variant picker", "product.pincode": "Pincode checker", "product.sold_out": "Sold-out product page",
    "health.meta.home": "Homepage title and description", "health.meta.pdp": "Product page title and description",
    "health.not_found": "Missing-page (404) page",
    "info.policy_pages": "Info pages (shipping, contact)", "info.account_page": "Account login page",
}
PLAIN_PREFIXES = (("product.pdp.", "Product page"), ("catalog.collection.", "Collection"),
                  ("catalog.more.", "Collection paging"), ("catalog.filter.", "Collection filters"), ("search.", "Search"))


def plain_name(title: str, key: str | None = None) -> str:
    """Check names without shop-talk a store owner would not know. With the case id (key) the name is a fixed plain
    one ("Menu links"); a product / collection / search check names its subject from the title ("Product page: Vitamin
    C Serum"). Without a key (older callers) only the 'PDP' shop-talk is replaced."""
    if key in PLAIN_NAMES:
        return PLAIN_NAMES[key]
    for prefix, name in PLAIN_PREFIXES:
        if key and key.startswith(prefix):
            what = _quoted(title)
            return f"{name}: {what}" if what else name
    return re.sub(r"^PDP\b", "Product page", title or key or "")


# ---------------- the owner's one-sentence reason for a failed check ----------------

EXPECT_GOT = re.compile(r"^(?P<what>[^:]{1,120}): expected (?P<exp>.+?), got (?P<got>.+)$", re.S)
HTTP_STATUS = re.compile(r"HTTP status: expected < 400, got (\d{3})")
PAGE_BLANK = re.compile(r"page not blank: expected")
NO_PAGE_WITHIN = re.compile(r"page did not load: no page within \d+ s, nor within (\d+) s")
# the step names of the shopper journey (radar/checks/library.py shopper_journey, _add_and_verify,
# _cart_view_and_checkout) in a shopper's words
JOURNEY_STEPS = {
    "open_homepage": "opening the homepage", "click_into_collection": "opening a collection from the menu",
    "click_into_product": "opening a product from the collection",
    "every_product_link_opens_the_product": "opening a product from the collection",
    "product_identified": "reading the product's price", "variant_ready": "choosing a variant",
    "click_add_to_cart": "the add-to-cart button", "cart_received_this_product": "the cart receiving the product",
    "cart_total_increase": "the cart total", "cart_shows_product": "the cart page showing the product",
    "checkout_button_ready": "the checkout button",
}
# what a product page failed to show, by the assertion that failed (library._pdp_assertions)
PDP_MISSING = (("product name shown on the page", "its name"), ("selected variant price shown", "its price"),
               ("main product image", "its picture"))


def _sentence(text: str, limit: int = 300) -> str:
    """One owner-facing sentence: single spaces, first letter capitalised, no more than `limit` chars, a full stop."""
    s = " ".join(str(text or "").split()).rstrip(" .")
    if len(s) > limit - 1:
        s = s[: limit - 2].rstrip() + "…"
    return (s[:1].upper() + s[1:] + ".") if s else ""


def _cleaned(detail: str) -> str:
    """A step's error without the 'what: expected X, got Y' phrasing: 'what was Y (expected X)'."""
    m = EXPECT_GOT.match(" ".join(str(detail or "").split()))
    if not m:
        return str(detail or "")
    return f"{m['what']} was {m['got']} (expected {m['exp']})"


def _listed(detail: str) -> list[str]:
    """The items after 'got N: a, b' in a links / pages assertion (library.links_resolve, more_links_resolve, info_pages)."""
    m = re.search(r"got \d+: (.+)$", " ".join(str(detail or "").split()))
    return [x.strip() for x in m.group(1).split(", ") if x.strip()] if m else []


def _home_detail(detail: str) -> str:
    m = HTTP_STATUS.search(detail)
    if m:
        return f"the server answered HTTP {m.group(1)}"
    if PAGE_BLANK.search(detail):
        return "the page was blank"
    m = NO_PAGE_WITHIN.search(detail)
    if m:
        return f"it didn't load within {m.group(1)} seconds"
    return _cleaned(detail)


def plain_reason(key: str, title: str | None, step: str | None, detail: str | None) -> str | None:
    """The owner's sentence for a failed check, built from what the robot measured (the failed step's error): never
    from the LLM triage. Returns None when there is nothing to say."""
    detail = " ".join(str(detail or "").split())
    what = _quoted(title)
    step = step or ""
    if key == "smoke.nav_links":
        paths = _listed(detail)
        if len(paths) == 1:
            return _sentence(f"A link in your main menu opens a page that doesn't exist: {paths[0]}")
        if paths:
            return _sentence(f"{len(paths)} links in your main menu open pages that don't exist: {', '.join(paths)}")
        return _sentence("A link in your main menu opens a page that doesn't load" + (f": {_cleaned(detail)}" if detail else ""))
    if key == "smoke.shopify_menu":       # 29 Oct: the menus read from the store's Shopify admin
        paths = _listed(detail)
        if len(paths) == 1:
            return _sentence(f"A link in your Shopify menus opens a page that doesn't exist: {paths[0]}")
        if paths:
            return _sentence(f"{len(paths)} links in your Shopify menus open pages that don't exist: {', '.join(paths)}")
        return _sentence("A link in your Shopify menus opens a page that doesn't load" + (f": {_cleaned(detail)}" if detail else ""))
    if key == "product.shopify_admin":
        if step.startswith("price matches admin"):
            return _sentence(f"A product page doesn't show the price in your Shopify admin: {_cleaned(detail)}")
        if step.startswith("stock matches admin"):
            return _sentence(f"A product page's stock doesn't match your Shopify admin: {_cleaned(detail)}")
        return _sentence("Product pages couldn't be compared with your Shopify admin" + (f": {_cleaned(detail)}" if detail else ""))
    if key == "smoke.more_links":
        links = _listed(detail)
        if len(links) == 1:
            return _sentence(f"A link on your homepage, announcement bar or footer opens a page that doesn't work: {links[0]}")
        if links:
            return _sentence(f"{len(links)} links on your homepage, announcement bar or footer open pages that don't work: {', '.join(links)}")
        return _sentence("A link on your homepage, announcement bar or footer opens a page that doesn't work")
    if key == "smoke.home_health":
        return _sentence("Your homepage didn't load properly" + (f": {_home_detail(detail)}" if detail else ""))
    if key == "smoke.mobile_menu":
        return _sentence("The menu button didn't open the menu in the mobile browser")
    if key == "smoke.whatsapp":
        return _sentence("The WhatsApp button doesn't open a chat with a valid number")
    if key == "cart.add_to_cart":
        return _sentence("Adding a product to the cart didn't work" + (f": {_cleaned(detail)}" if detail else ""))
    if key == "cart.edit_and_checkout":
        return _sentence("The cart page or the checkout button didn't work" + (f": {_cleaned(detail)}" if detail else ""))
    if key == "search.no_results":
        return _sentence("Searching for a word no store sells didn't show a proper 'no results' page")
    if key == "search.suggestions":
        return _sentence(f"Typing '{what}' in the search box didn't suggest any products" if what
                         else "The search box didn't suggest any products while typing")
    if key.startswith("search."):
        return _sentence(f"Search didn't return products for '{what}'" if what
                         else "Search didn't work" + (f": {_cleaned(detail)}" if detail else ""))
    if key.startswith("product.pdp."):
        page = f"The product page '{what}'" if what else "A product page"
        for needle, missing in PDP_MISSING:
            if needle in detail:
                return _sentence(f"{page} is missing {missing}")
        if step == "buy_button_ready" or "buy button enabled" in detail:
            return _sentence(f"{page} doesn't let shoppers add it to the cart: the buy button is missing or disabled")
        return _sentence(f"{page} didn't load properly" + (f": {_home_detail(detail)}" if detail else ""))
    if key == "product.pincode":
        m = re.search(r"pincode checker answers for (\S+?):", detail)
        page = f"The pincode checker on '{what}'" if what else "The pincode checker"
        return _sentence(f"{page} didn't answer" + (f" for {m.group(1)}" if m else ""))
    if key == "product.variant_switch":
        return _sentence((f"Picking another variant of '{what}'" if what else "Picking another variant")
                         + " didn't update the product page" + (f": {_cleaned(detail)}" if detail else ""))
    if key == "product.sold_out":
        return _sentence((f"The sold-out product '{what}'" if what else "A sold-out product")
                         + " isn't shown as sold out or can still be added to the cart" + (f": {_cleaned(detail)}" if detail else ""))
    if key.startswith("catalog.collection."):
        coll = f"The collection '{what}'" if what else "A collection"
        if step == "lists_products" or "products listed" in detail:
            return _sentence(f"{coll} shows no products")
        return _sentence(f"{coll} didn't load properly" + (f": {_home_detail(detail)}" if detail else ""))
    if key.startswith("catalog.more."):
        coll = f"The collection '{what}'" if what else "A collection"
        if step == "loads":
            return _sentence(f"{coll} didn't load properly" + (f": {_home_detail(detail)}" if detail else ""))
        return _sentence(f"{coll} doesn't show more products past its first page")
    if key.startswith("catalog.filter."):
        coll = f"the collection '{what}'" if what else "a collection"
        if step == "loads":
            return _sentence(f"{coll[:1].upper()}{coll[1:]} didn't load properly" + (f": {_home_detail(detail)}" if detail else ""))
        return _sentence(f"Filtering {coll} doesn't narrow its products")
    if key == "journey.shopper":
        where = JOURNEY_STEPS.get(step) or step.replace("_", " ") or "an unknown step"
        return _sentence(f"A shopper's path (menu → product → cart) broke at: {where}")
    if key == "health.not_found":
        return _sentence("A missing page doesn't show a proper 'page not found' page")
    if key.startswith("health.meta."):
        page = "product page" if key.endswith(".pdp") else "homepage"
        if step == "loads" or "HTTP status" in detail or "page did not load" in detail:
            return _sentence(f"The {page} didn't load properly" + (f": {_home_detail(detail)}" if detail else ""))
        return _sentence(f"The {page} is missing its title or description")
    if key == "info.policy_pages":
        pages = _listed(detail)
        if len(pages) == 1:
            return _sentence(f"An info page linked in your footer doesn't open: {pages[0]}")
        if pages:
            return _sentence(f"{len(pages)} info pages linked in your footer don't open: {', '.join(pages)}")
        return _sentence("An info page linked in your footer doesn't open")
    if key == "info.account_page":
        return _sentence("The account login page didn't open properly" + (f": {_cleaned(detail)}" if detail else ""))
    return _sentence(_cleaned(detail)) or None


def triage_note(case: dict) -> str | None:
    """The LLM's triage, for the technical `error` field only: 'Triage (<label>): <reason>'."""
    tri = case.get("triage") or {}
    if not tri.get("reason"):
        return None
    label = TRIAGE_NOTE.get(tri.get("verdict")) or str(tri.get("verdict") or "").replace("_", " ") or "no verdict"
    return f"Triage ({label}): {str(tri['reason'])[:300]}"


def error_of(case: dict) -> str | None:
    """The technical error for engineers: the last try's error, then the triage (never shown to owners)."""
    last = (case.get("attempts") or [{}])[-1]
    parts = [str(last["error"]) for _ in [0] if last.get("error")]
    note = triage_note(case)
    if note:
        parts.append(note)
    return "\n".join(parts)[:4000] or None


def smaller(p: Path) -> tuple[str, str, bytes] | None:
    """A full-page PNG as a JPEG no wider than SHOT_WIDTH: about a fifth of the size, still readable. Needs Pillow."""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(p) as im:
            im = im.convert("RGB")
            if im.width > SHOT_WIDTH:
                im = im.resize((SHOT_WIDTH, round(im.height * SHOT_WIDTH / im.width)))
            buf = BytesIO()
            im.save(buf, "JPEG", quality=72, optimize=True)
        return p.with_suffix(".jpg").name, "image/jpeg", buf.getvalue()
    except Exception:          # no Pillow on this machine, or an unreadable file: the caller falls back to the file as it is
        return None


def evidence(case: dict, run_dir: Path) -> tuple[str, str, bytes] | None:
    """The picture for a failed check, as (name, content type, bytes): the small photo of the step that failed; else the
    full-page one, shrunk to a JPEG when Pillow is there, or as it is when it is under the size limit.
    (13 Oct: phone full-page PNGs are 0.6-1.5 MB, over the limit, so phone failures arrived with no picture at all.)"""
    last = (case.get("attempts") or [{}])[-1]
    steps = last.get("steps") or []
    step = next((s.get("shot") for s in steps if s.get("name") == last.get("failed_step") and s.get("shot")), None) \
        or next((s.get("shot") for s in reversed(steps) if s.get("shot")), None)
    for name in (step, last.get("screenshot")):
        p = run_dir / name if name else None
        if not (p and p.is_file() and p.stat().st_size):
            continue
        if p.suffix.lower() == ".png":
            small = smaller(p)
            if small and len(small[2]) <= MAX_SHOT_BYTES:
                return small
        if p.stat().st_size <= MAX_SHOT_BYTES:
            return p.name, "image/png" if p.suffix.lower() == ".png" else "image/jpeg", p.read_bytes()
    return None


# where a failed check stopped, for the "Open this page" link on the dashboard
PRIVATE_PAGES = ("/cart", "/checkout", "/checkouts", "/account")     # different for every visitor: never linked
NO_PAGE = {"not_found": "test_address"}         # checks that ask for a made-up address: there is no real page to open
PATH_IN_STEP = re.compile(r"(?:^|\s)(/[A-Za-z0-9][^\s]{1,200})")


def failing_page(case: dict, base_url: str) -> tuple[str | None, str | None]:
    """(link, note) for a failed check. The link is always on the store's own address. Order: a page named by a step that
    failed or warned (link checks name the broken link there), else the page the last try ended on. A cart / checkout /
    account page is private to each visitor, so the last product page is linked instead (note 'cart')."""
    if case.get("check") in NO_PAGE:
        return None, NO_PAGE[case["check"]]
    base = urlparse(base_url or "")
    if not base.hostname:
        return None, None
    last = (case.get("attempts") or [{}])[-1]

    def own(u: str | None) -> str | None:
        if not u:
            return None
        q = urlparse(u if "://" in u else f"{base.scheme}://{base.netloc}{u if u.startswith('/') else '/' + u}")
        if q.scheme not in ("http", "https") or (q.hostname or "").removeprefix("www.") != base.hostname.removeprefix("www."):
            return None
        return f"{q.scheme}://{q.netloc}{q.path or '/'}{('?' + q.query) if q.query and len(q.query) < 120 else ''}"[:400]

    private = lambda u: urlparse(u).path.rstrip("/").startswith(PRIVATE_PAGES)       # noqa: E731
    named = [m.group(1) for st in last.get("steps") or [] if st.get("status") in ("fail", "warn")
             for m in [PATH_IN_STEP.search(str(st.get("name") or ""))] if m]
    loads = [l.get("url") for l in last.get("loads") or [] if l.get("url")]
    for cand in [*named, *reversed(loads)]:
        u = own(cand)
        if not u:
            continue
        if private(u):
            product = next((own(x) for x in reversed(loads) if "/products/" in str(x)), None)
            return (product, "cart") if product else (None, "cart")
        # foxtale.in, 13 Oct: the check is named after the first product, but its page was hidden and the next catalog
        # product was tested: the link is that product, so say so (note 'next_product')
        moved = any(str(st.get("name") or "").startswith("loads_next_catalog_product_") and st.get("status") == "pass"
                    for st in last.get("steps") or [])
        return u, "next_product" if moved and cand in loads else None
    return None, None


def steps_of(case: dict) -> list[dict] | None:
    """The steps of the last try, in order, for the 'what Radar did' line (names only; long lists are cut)."""
    steps = [{"name": str(st.get("name") or "")[:70], "status": st.get("status")} for st in ((case.get("attempts") or [{}])[-1].get("steps") or [])
             if st.get("name") and st.get("status") in ("pass", "warn", "fail")]
    return steps[-8:] or None


def reason_of(case: dict) -> str | None:
    """One plain sentence on why a check did not pass, for the store owner. Built from the robot's own evidence (the
    failed step's error) by plain_reason; the LLM triage never reaches it (see error_of)."""
    last = (case.get("attempts") or [{}])[-1]
    if case.get("verdict") in ("blocked", "skipped", "no_network"):
        for st in last.get("steps") or []:
            if st.get("status") in ("blocked", "skipped") and st.get("detail"):
                return str(st["detail"])[:300]
        return (last.get("error") or last.get("note") or None) and str(last.get("error") or last.get("note"))[:300]
    if case.get("verdict") == "pass":
        return None
    if case.get("verdict") == "flaky":
        return f"Passed on try {len(case.get('attempts') or [])} after failing first"
    step = last.get("failed_step")
    detail = next((st.get("error") or st.get("detail") for st in last.get("steps") or []
                   if st.get("name") == step and (st.get("error") or st.get("detail"))), None) or last.get("error")
    return plain_reason(str(case.get("case_id") or ""), case.get("title"), step, detail and str(detail))


def project_name(run: dict, label: str = "") -> str:
    """The dashboard store a run is filed under: the store's host, plus a label for a test bench (22 Oct: the GitHub
    nightly bench uploaded the Dawn demo into the same store as BugRadar's daily check, so a blip on the nightly, which
    has no 10-minute re-check, opened a problem there). Pure."""
    host = (urlparse(run.get("base_url") or "").hostname or run.get("site_id") or "store").removeprefix("www.")
    label = " ".join((label or "").split())[:40]
    return f"{host} ({label})" if label else host


def to_payload(run: dict, run_dir: Path, build: str, ci_url: str | None, extra_meta: dict | None = None,
               project_label: str = "") -> dict:
    host = project_name(run, project_label)
    results, shots = [], 0
    for c in run.get("cases", []):
        status = STATUS.get(c.get("verdict"), "warning")
        last = (c.get("attempts") or [{}])[-1]
        item = {
            "key": c["case_id"], "name": plain_name(c.get("title") or c["case_id"], c["case_id"]), "suite": c.get("suite"),
            "severity": c.get("severity") or "major", "status": status, "reason": reason_of(c),
            "error": error_of(c),
            "duration_ms": int(sum(a.get("secs") or 0 for a in c.get("attempts") or []) * 1000) or None,
            "attempts": len(c.get("attempts") or []) or 1,
        }
        if status in ("passed", "flaky") and any(s.get("name") == "sold_out_product" for s in last.get("steps") or []):
            # the product page is fine, the product just is not in stock: a tag, not a failure and not a warning
            item["tags"] = ["sold out"]
            item["reason"] = "This product is sold out, so the buy steps were not tested."
        if status == "failed":
            item["page_url"], item["page_note"] = failing_page(c, run.get("base_url") or "")
            item["steps"] = steps_of(c)
        if status == "failed" and shots < MAX_SHOTS_PER_RUN:
            shot = evidence(c, run_dir)
            if shot:
                item["artifacts"] = [{"kind": "screenshot", "name": shot[0], "content_type": shot[1],
                                      "data_base64": base64.b64encode(shot[2]).decode()}]
                shots += 1
        results.append(item)
    # one line for the store itself, in every run: it fails when the store cannot be opened, is "not tested" when Radar
    # is not allowed in or the platform is not supported, and passes otherwise (so a store coming back closes the problem)
    note = str((run.get("notes") or [""])[0]) or None
    site = {"key": SITE_KEY, "name": SITE_NAME, "suite": "smoke", "severity": "critical", "attempts": 1, "error": None,
            "duration_ms": None, "status": "passed", "reason": None}
    if not results:
        # the plain part is the reason; a long technical tail in brackets ("(robots.txt request: ...)") goes under
        # "Technical detail". Split BEFORE shortening (review, 22 Oct: cut at 300 first, a long note lost its closing
        # bracket and the owner read the whole technical tail as the reason)
        plain = re.sub(r"\s*\([^()]*:[^()]{20,}\)\s*$", "", note or "") or note
        site.update(status=SITE_STATUS.get(run.get("verdict"), "skipped"),
                    reason=SITE_WHY.get(run.get("verdict")) or (plain[:300] if plain else plain),
                    error=note[:1000] if note and plain != note else None)
        up = (run.get("sitemap_summary") or {}).get("store_up_robots_down")
        if run.get("verdict") == "blocked" and isinstance(up, int):
            # a daily check: robots.txt doesn't load while the store itself answers (22 Oct). Radar still can't test it
            # (RFC 9309), but this is the owner's problem to fix, not a quiet "not tested": search engines read a
            # failing robots.txt as "don't crawl" too. A warning: shown, never a problem or an alert email.
            site.update(status="warning", error=note[:1000] if note else None, reason=(
                ("Your store opens" if 200 <= up < 300 else f"Your store's server answers (HTTP {up})")
                + ", but its robots.txt file isn't loading. Search engines pause crawling a store while that fails, "
                  "and Radar can't test the store until it loads again."))
        if site["status"] == "failed" and (extra_meta or {}).get("rechecked"):
            # a daily check's second look (radar/worker.py RECHECK, 21 Oct): the owner should know it wasn't a one-off
            mins = (extra_meta or {}).get("recheck_minutes")
            earlier = f"{mins} minute{'' if mins == 1 else 's'} earlier" if isinstance(mins, int) and mins > 0 else "on an earlier look"
            site["reason"] = f"{(site['reason'] or 'The store did not load').rstrip(' .')}. It also didn't load {earlier}."
    # the free check's "your store as Radar saw it" (19 Oct): worker.homepage_shot saves it next to run.json
    home = run_dir / "homepage.jpg"
    if home.is_file() and 0 < home.stat().st_size <= MAX_SHOT_BYTES:
        site["artifacts"] = [{"kind": "screenshot", "name": "homepage.jpg", "content_type": "image/jpeg",
                              "data_base64": base64.b64encode(home.read_bytes()).decode()}]
    results.insert(0, site)
    perf = run.get("perf") or {}
    return {
        "project": host, "project_url": run.get("base_url"),
        "build": {"version": build or build_day(run.get("started_at")), "environment": "production"},
        "run": {"tool": "radar", "language": "python", "framework": "radar", "device": run.get("device"),
                "browser": "chromium", "started_at": run.get("started_at"), "finished_at": run.get("finished_at") or None,
                "ci_url": ci_url, "meta": {**(extra_meta or {}), "radar_run_id": run.get("run_id"), "radar_verdict": run.get("verdict"),
                                           "median_load_secs": perf.get("median_load_secs"), "notes": (run.get("notes") or [])[:3]}},
        "results": results,
    }


def mark_also_failed(payloads: list[dict]) -> None:
    """One check, several browsers, one alert (28 Oct re-test). The worker sends desktop and mobile a few seconds apart,
    so the dashboard opens the problem on the first upload and alerts then, before it knows mobile is broken too: the
    alert said "broken in the desktop browser" when both were. Each failed result now names the OTHER browsers of the
    same store, in this same upload batch, where the same check failed too (`also_failed_on`); ingest adds them to
    the alert's "where". Pure, unit-tested."""
    failed: dict[tuple[str, str], set[str]] = {}
    for p in payloads:
        dev = (p.get("run") or {}).get("device")
        for r in p.get("results") or []:
            if dev and r.get("status") == "failed":
                failed.setdefault((p.get("project") or "", r["key"]), set()).add(dev)
    order = {"desktop": 0, "mobile": 1, "tablet": 2}
    for p in payloads:
        dev = (p.get("run") or {}).get("device")
        for r in p.get("results") or []:
            if r.get("status") != "failed":
                continue
            others = sorted(failed.get((p.get("project") or "", r["key"]), set()) - {dev}, key=lambda d: (order.get(d, 9), d))
            if others:
                r["also_failed_on"] = others


def send(payload: dict, key: str) -> dict:
    req = urllib.request.Request(INGEST_URL, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def find_runs(folder: Path, since: float = 0) -> list[Path]:
    """Every finished run.json under the folder (written after `since`), in path order."""
    # two layouts: the engine's own data folder (sites/<store>/runs/<run>/run.json, what a GitHub machine has) and the
    # published results (sites/<store>/<run>/run.json). 13 Oct: only the second was looked for, so nothing was ever sent
    files = [folder / "run.json"] if (folder / "run.json").exists() else sorted(
        set(folder.glob("**/sites/*/runs/*/run.json")) | set(folder.glob("**/sites/*/*/run.json")))
    return [f for f in files if f.stat().st_mtime >= since]


def upload(folder: Path, key: str, since: float = 0, build: str = "", ci_url: str | None = None,
           dry_run: bool = False, log=print, extra_meta: dict | None = None, project_label: str = "") -> list[dict]:
    """Send every finished run under the folder. Returns one record per run.json found:
    {"file", "project", "device", "radar_run_id", "ok", "run_id", "project_id", "error"}; the worker (15 Oct) reads
    run_id off it to tie a free-scan request to its dashboard run."""
    out: list[dict] = []
    ready: list[tuple[Path, dict, dict]] = []
    for f in find_runs(folder, since):
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if not run.get("cases") and not run.get("verdict"):
            continue                       # not a finished run
        ready.append((f, run, to_payload(run, f.parent, build, ci_url, extra_meta, project_label)))
    mark_also_failed([p for _, _, p in ready])
    for f, run, payload in ready:
        rec = {"file": str(f), "project": payload["project"], "device": payload["run"]["device"],
               "radar_run_id": run.get("run_id"), "ok": False, "run_id": None, "project_id": None, "error": None}
        out.append(rec)
        if dry_run:
            log(json.dumps({**payload, "results": f"{len(payload['results'])} results"}, default=str)[:400])
            rec["ok"] = True
            continue
        try:
            ans = send(payload, key)
            rec.update(ok=True, run_id=ans.get("run_id"), project_id=ans.get("project_id"))
            log(f"sent {payload['project']} {payload['run']['device']}: {ans.get('results')} results, "
                f"{ans.get('incidents_opened', 0)} new problems, {ans.get('incidents_resolved', 0)} fixed")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            rec["error"] = str(e)[:300]
            log(f"could not send {payload['project']} {payload['run']['device']}: {e}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--build", default="", help="build label; default: the day the run started")
    ap.add_argument("--ci-url", default=os.environ.get("BUGRADAR_CI_URL"))
    ap.add_argument("--since", type=float, default=0, help="only runs written after this unix time (machines that keep old data)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--project-label", default="", help="file runs under '<store> (<label>)', e.g. 'nightly bench'; "
                                                         "empty = the store itself")
    a = ap.parse_args(argv)
    key = os.environ.get("BUGRADAR_KEY", "")
    if not a.dry_run and not key.startswith("brk_"):
        print("BUGRADAR_KEY is not set; nothing sent.")
        return 0                       # never fail a Radar run because the dashboard isn't set up
    recs = upload(a.folder, key, a.since, a.build, a.ci_url, a.dry_run,
                  log=lambda m: print(m, file=sys.stderr if m.startswith("could not send") else sys.stdout),
                  project_label=a.project_label)
    sent, failed = sum(r["ok"] for r in recs), sum(not r["ok"] for r in recs)
    print(f"dashboard upload: {sent} sent, {failed} failed, {len(recs)} found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
