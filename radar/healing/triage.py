"""LLM triage of a CONFIRMED failure: is it the store, or is it Radar?

Called only after the confirm rule (2 of 3 fresh-browser attempts failed). The model sees what a
person reviewing the failure would see: the test, the failed step, every assertion with expected
vs actual, the page URL, the visible page text and the failure screenshot.

It returns a label and a plain-English reason. It never changes a result by itself:
  real_store_problem -> the failure stands (an alert can go out later, R3)
  radar_problem      -> one re-check runs with LLM help switched on (product name, overlays);
                        only a PASSING re-check, judged by the same code assertions, clears it
  unsure             -> the failure stands, marked for a human look
"""
from __future__ import annotations

import re

from radar.healing.llm import LLMClient

VERDICTS = ("real_store_problem", "radar_problem", "unsure")
CATEGORIES = ("store_bug", "store_changed_or_down", "locator", "popup_or_overlay", "timing",
              "test_data", "other")

SYSTEM = (
    "You review FAILED automated checks of an online store, run by a monitoring robot that browses like "
    "a shopper. Decide who is at fault. A wrong 'store problem' sends a false alarm to a real business, so "
    "blame the store only on positive evidence.\n"
    "Apply these rules in order:\n"
    "1. If the FACTS say the thing the failed check could not find IS in the visible page text, it is "
    "radar_problem (category locator): the robot looked in the wrong place.\n"
    "2. If the robot's click or view was blocked by a popup, newsletter, cookie banner, chat widget or other "
    "overlay, it is radar_problem (popup_or_overlay). The robot should have closed it.\n"
    "3. If the page is still loading or empty while loading, it is radar_problem (timing) or unsure.\n"
    "4. If the robot's notes say it tested a product shoppers cannot normally reach (a free gift, sample, "
    "Rs 1 item, not listed in any collection), it is radar_problem (test_data), even if the page says unavailable.\n"
    "5. real_store_problem only with positive evidence a shopper is hurt: an error or 'something went wrong' "
    "message, HTTP 5xx/4xx on a page that should exist, the wrong product or price in the cart, a zero price, a "
    "disabled buy/checkout button, no way to buy a product on its own page, a price shown nowhere a shopper on this screen can see, a missing page answering 200 instead "
    "of 404, the store saying it moved or is closed, a shopper-facing feature returning nothing (e.g. 'no results' "
    "for a product the store sells).\n"
    "6. Otherwise: unsure.\n"
    'Reply with JSON only, fields in this order: {"evidence": "<the facts/page text that decide it>", '
    '"verdict": "real_store_problem|radar_problem|unsure", '
    '"category": "store_bug|store_changed_or_down|locator|popup_or_overlay|timing|test_data|other", '
    '"reason": "<one plain sentence a store owner understands>"}'
)

_POPUP = re.compile(r"subscribe|newsletter|% ?off|discount code|sign ?up|we use cookies|accept all|cookie|"
                    r"spin the wheel|whatsapp|chat with us|enter your email", re.I)
_LOADING = re.compile(r"\bloading\b|please wait|fetching", re.I)
_ERROR = re.compile(r"something went wrong|service (temporarily )?unavailable|\b50[0-4]\b|internal server error|"
                    r"not found|currently unavailable|has moved|we have moved|is now [A-Z.]{4,}|try again", re.I)


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


_PRICE_LINE = re.compile(r"(₹|rs\.?|inr)\s*\d", re.I)
_KNOWN_BUY = re.compile(r"\b(add|cart|bag|buy)\b", re.I)


def _action_lines(text: str) -> list[str]:
    """Short (1-4 word) lines within 6 lines after the first price line that are not prices, not known
    buy words and not numbers: candidates for a custom buy control Radar did not recognise. Pure."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    i = next((k for k, l in enumerate(lines[:60]) if _PRICE_LINE.search(l)), None)
    if i is None:
        return []
    out = []
    for l in lines[i + 1: i + 7]:
        w = l.split()
        if 1 <= len(w) <= 4 and len(l) <= 30 and not _PRICE_LINE.search(l) and not _KNOWN_BUY.search(l) \
                and re.search(r"[a-z]", l, re.I) and not re.search(r"\d", l) \
                and not l.endswith(".") and not l.startswith("("):
            out.append(l)
    return out[:5]


def facts(checks: list[dict], error: str | None, page_text: str | None) -> list[str]:
    """Things the robot can establish in code before asking the model. Pure, unit-tested.
    These keep a cheap model from guessing (gpt-4o-mini blamed the store 5 of 12 times without them)."""
    out, text, ntext = [], page_text or "", _norm(page_text)
    absent = re.compile(r"^\s*(none|not on page|missing|\(missing\)|not found|0|no |nothing)", re.I)
    for c in checks:
        if c.get("ok") or not absent.match(str(c.get("actual") or "")):
            continue          # only "could not find X" failures: a WRONG value is not "X is missing"
        exp = str(c.get("expected") or "")
        if len(_norm(exp)) >= 4 and _norm(exp) in ntext:
            out.append(f"The expected value of the failed check '{c.get('what')}' ({exp[:80]!r}) DOES appear in the "
                       "visible page text, so it is on the page.")
    blob = f"{error or ''} " + " ".join(str(c.get("actual")) for c in checks if not c.get("ok"))
    m = re.search(r"covered by ([\w.#-]+)|<(\w+)[^>]*?(?:class|id)=\\?\"([^\"\\]+)[^>]*>[^\n]{0,40}intercepts pointer events", blob)
    covered = bool(m) or "intercepts pointer events" in blob
    if "no add-to-cart form for this product" in blob:
        others = "belong to other products' cards" in blob
        out.append("Code checked the page: there is NO add-to-cart form for this product on its own page"
                   + ("; the add-to-cart buttons visible belong to OTHER products' cards" if others else "")
                   + ". Radar also found no other button it recognises as this product's buy button. Look at the "
                   "screenshot and page text: if no buy control for THIS product is visible, a shopper cannot buy it here "
                   "(real store problem); if an unusual buy control is visible (e.g. a custom button), it is radar_problem.")
        cands = _action_lines(text)
        if cands:
            out.append("Radar recognises buy buttons only by words like add / cart / bag / buy. Next to the price, the visible "
                       f"page text has short action-like lines it did not recognise: {', '.join(repr(c) for c in cands)}. "
                       "If one of them reads like a way to buy (e.g. 'Grab it', 'Get yours', 'I want this'), this product "
                       "CAN be bought and Radar missed the button: radar_problem (locator).")
    if "only inside a hidden element" in blob:
        out.append("Code checked the page's HTML: the expected price exists ONLY inside an element the store's theme hides "
                   "at this screen size (e.g. a mobile-only sticky bar), and it is nowhere in the visible text after scrolling "
                   "the whole page. The robot did look in the right place; a shopper on this screen sees no price before "
                   "adding to cart: real_store_problem (store_bug).")
    if any("page that does not exist" in str(c.get("what")) for c in checks if not c.get("ok")):
        out.append("A page that should not exist did not return HTTP 404 (a 'soft 404'). This is how the store's server is "
                   "configured, not something the robot did; it is a minor real store problem (search engines index junk pages).")
    if m:
        what = m.group(1) or f"{m.group(2)}.{(m.group(3) or '').split()[0] if m.group(3) else ''}"
        out.append(f"The robot's click target was covered by another element lying on top of it ({what}).")
    elif covered:
        out.append("The robot's click target was covered by another element lying on top of it.")
    # popup wording only matters when something actually blocked a click (an offer banner is not a popup)
    for rx, label in (((_POPUP, "popup/consent/marketing wording"),) if covered else ()) + (
            (_LOADING, "a loading state"), (_ERROR, "an error or unavailable message")):
        hit = rx.search(text)
        if hit:
            i = hit.start()
            out.append(f"The visible page text contains {label}: {text[max(0, i - 40): i + 60]!r}".replace("\n", " "))
    return out


def build_prompt(case_title: str, check: str, failed_step: str | None, error: str | None,
                 checks: list[dict], url: str | None, page_text: str | None) -> str:
    rows = "\n".join(f"- {'OK  ' if c.get('ok') else 'FAIL'} {c.get('what')}: expected {c.get('expected')!s:.120} | "
                     f"actual {c.get('actual')!s:.200}" for c in checks[-12:]) or "- (none recorded)"
    text = (page_text or "").strip().replace("\n\n", "\n")[:2500]
    fs = "\n".join(f"- {f}" for f in facts(checks, error, page_text)) or "- (none)"
    return (f"Test: {case_title} (check: {check})\nFailed step: {failed_step}\nError: {error}\n"
            f"Page URL at failure: {url}\nAssertions in the failed step:\n{rows}\n\n"
            f"FACTS established by the robot's code:\n{fs}\n\n"
            f"Visible page text at failure (first 2500 chars):\n{text}\n\n"
            "The screenshot (if attached) is the browser viewport at the moment of failure.")


def triage(llm: LLMClient, case_title: str, check: str, failed_step: str | None, error: str | None,
           checks: list[dict], url: str | None, page_text: str | None, screenshot: bytes | None) -> dict | None:
    """Returns {verdict, category, reason, evidence} or None (LLM off, over budget, or bad answer)."""
    if not llm or not llm.enabled:
        return None
    ans = llm.complete_json(SYSTEM, build_prompt(case_title, check, failed_step, error, checks, url, page_text),
                            max_tokens=250, images=[screenshot] if screenshot else None)
    return clean(ans)


def clean(ans: dict | None) -> dict | None:
    """Validate the model's answer; anything off-schema is dropped (treated as no triage). Pure."""
    if not isinstance(ans, dict) or ans.get("verdict") not in VERDICTS:
        return None
    cat = ans.get("category") if ans.get("category") in CATEGORIES else "other"
    return {"verdict": ans["verdict"], "category": cat,
            "reason": str(ans.get("reason") or "")[:300], "evidence": str(ans.get("evidence") or "")[:300]}
