"""Scan profiles (15 Oct 2026): which of the built cases a run keeps.

    full    everything the builder made (daily monitoring, the cloud runs, the bench): the default
    basic   the free first check on the public scan page: ~1-2 min, desktop, read-only, one of each

The basic list (claude/ONBOARDING-how-stores-get-added_15oct.md, decided by Anmol 15 Oct 16:28):
    discovery (always)                    store found, is Shopify, open / password / blocked
    smoke.home_health                     homepage loads healthy (status, title, images, JS errors, speed)
    smoke.nav_links                       menu links open
    catalog.collection.*   (first only)   one collection lists products
    product.pdp.*          (first only)   one product page: name, price, image, buy button
    search.<term>          (first only)   search finds a real product (not no_results / suggestions)
    health.not_found                      missing page answers 404, not 200
    health.meta.home                      homepage meta tags

    core    the FREE plan's daily check (26 Oct, Anmol: "Free + Pro, keep features limited in free"): computer + phone,
            the core of a store: homepage, menu (computer + phone), the clicking shopper journey, one product page,
            add to cart, the cart page with checkout opening. Pro runs 'full'. Shopify app stores also get, on Free
            (29 Oct, Shopify review 2.2.1), the two checks built from their Shopify admin: smoke.shopify_menu and
            product.shopify_admin. The dashboard's web/src/lib/plans.ts isCoreCheck must list the same.

Everything else in 'basic' (the clicking shopper journey, cart / checkout, phone run, variants, sold-out, filters, page 2,
pincode, coupon, footer links, retries across days) is what daily monitoring adds, so the free scan stays fast, light
on the store, never touches a cart and gives the visitor a reason to sign up.
"""
from __future__ import annotations

import re
from dataclasses import replace

from radar.core.config import Settings

PROFILES = ("full", "basic", "core")

# (regex over the case id, how many matching cases to keep); the order here is the run order
BASIC_RULES: tuple[tuple[str, int], ...] = (
    (r"^smoke\.home_health$", 1),
    (r"^smoke\.nav_links$", 1),
    (r"^catalog\.collection\.", 1),
    (r"^product\.pdp\.", 1),
    (r"^search\.(?!no_results$|suggestions$)", 1),
    (r"^health\.not_found$", 1),
    (r"^health\.meta\.home$", 1),
)

# The Free plan (26 Oct). Everything else (collections, filters, page 2, variants, sold-out, pincode / COD, WhatsApp,
# more links, search, discount box, Buy it now, price consistency, cart drawer, Google basics, policy pages) is Pro.
CORE_RULES: tuple[tuple[str, int], ...] = (
    (r"^smoke\.home_health$", 1),
    (r"^smoke\.nav_links$", 1),
    (r"^smoke\.mobile_menu$", 1),
    (r"^smoke\.shopify_menu$", 1),         # Shopify app stores (29 Oct): the menus read from the store's admin
    (r"^journey\.", 1),
    (r"^product\.pdp\.", 1),
    (r"^product\.shopify_admin$", 1),      # Shopify app stores (29 Oct): prices and stock vs the store's admin
    (r"^cart\.add_to_cart$", 1),
    (r"^cart\.edit_and_checkout$", 1),
)
RULES = {"basic": BASIC_RULES, "core": CORE_RULES}


def keep_case(profile: str | None, case_id: str, seen: dict[str, int]) -> bool:
    """Whether a case with this id is run under the profile. `seen` counts the cases already kept per rule, so
    'first only' rules work across suites; pass the same dict for a whole run."""
    if not profile or profile == "full":
        return True
    if profile not in RULES:
        raise ValueError(f"unknown scan profile {profile!r}; one of {', '.join(PROFILES)}")
    for pattern, limit in RULES[profile]:
        if re.search(pattern, case_id):
            if seen.get(pattern, 0) >= limit:
                return False
            seen[pattern] = seen.get(pattern, 0) + 1
            return True
    return False


def filter_suites(suites: list, profile: str | None) -> list:
    """The suites with only the profile's cases; suites left empty are dropped."""
    if not profile or profile == "full":
        return suites
    seen: dict[str, int] = {}
    out = []
    for suite in suites:
        kept = [c for c in suite.cases if keep_case(profile, c.id, seen)]
        if kept:
            out.append(replace(suite, cases=kept))
    return out


def settings_for_profile(profile: str | None, base: Settings) -> Settings:
    """What the profile changes about discovery and limits. basic: one product, one collection, four menu links, no
    cart flow (the builder then makes no cart cases at all, and nothing is ever added to a cart)."""
    if profile == "basic":
        return replace(base, max_products=1, max_collections=1, max_nav_links=4, allow_cart_flow=False)
    return base


def devices_for_profile(profile: str | None, devices: tuple[str, ...]) -> tuple[str, ...]:
    """basic runs on the desktop only."""
    return ("desktop",) if profile == "basic" else devices
