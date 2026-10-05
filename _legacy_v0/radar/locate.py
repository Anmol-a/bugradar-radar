"""Locator helper. Tries a list of selector hints, returns the first visible match.

This is the single place where LLM self-healing will plug in later: when no hint
matches, LocatorNotFound carries everything a healer needs (what was tried).
"""
from playwright.sync_api import Page, Locator


class LocatorNotFound(Exception):
    def __init__(self, name: str, tried: list[str]):
        self.name, self.tried = name, tried
        super().__init__(f"no visible element for '{name}'; tried {tried}")


def first_visible(page: Page, name: str, selectors: list[str], timeout_ms: int = 6000) -> Locator:
    per = max(timeout_ms // max(len(selectors), 1), 800)
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="visible", timeout=per)
            return loc
        except Exception:
            continue
    raise LocatorNotFound(name, selectors)
