"""Data model shared by every layer. Plain dataclasses, JSON-serialisable via asdict().

Flow of data:
  URL -> SiteMap (discovery) -> Suites of TestCases (generate)
      -> CaseResult with AttemptResults and StepResults (runner) -> RunResult (storage + report)
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Product:
    handle: str
    title: str
    url: str
    variant_id: int | None = None
    price: str | None = None
    available: bool = True
    variants: int = 1
    vendor: str = ""                  # Shopify 'vendor' (usually the brand: never used as a search word)


@dataclass
class Collection:
    handle: str
    title: str
    url: str
    product_count: int | None = None


@dataclass
class SiteMap:
    site_id: str
    base_url: str
    platform: str = "unknown"
    platform_evidence: list[str] = field(default_factory=list)
    theme: str | None = None          # Shopify theme (schema name, e.g. Dawn), from window.Shopify.theme
    checkout_app: str = ""            # Shopify checkout | GoKwik | Shopflo | Shiprocket Fastrr | Razorpay Magic ...
    access: str = "open"              # open | password | bot_blocked | robots_blocked | robots_unreachable | refused | rate_limited | unreachable | offsite | no_network
    robots_loaded: bool = False
    home_title: str = ""
    nav: list[dict] = field(default_factory=list)            # [{text, url}]
    collections: list[Collection] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)    # sample, in-stock first
    search_path: str | None = None
    cart_path: str = "/cart"
    discovered_at: str = ""
    notes: list[str] = field(default_factory=list)           # honest record of what discovery could NOT do

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SiteMap":
        d = dict(d)
        d["collections"] = [Collection(**c) for c in d.get("collections", [])]
        d["products"] = [Product(**p) for p in d.get("products", [])]
        return SiteMap(**d)


@dataclass
class TestCase:
    id: str                    # stable: "<suite>.<check>.<target-slug>"
    suite: str
    title: str
    check: str                 # name in the checks registry
    params: dict[str, Any] = field(default_factory=dict)
    severity: str = "major"    # critical | major | minor | seo (SEO note: shown, never a verdict, never an incident)
    description: str = ""


@dataclass
class Suite:
    id: str
    name: str
    description: str
    cases: list[TestCase] = field(default_factory=list)


@dataclass
class StepResult:
    name: str
    status: str                # pass | fail | warn | info | skip
    detail: Any = None
    error: str | None = None
    secs: float = 0.0
    healed: dict | None = None # {intent, old, new, method} when a locator was healed
    checks: list[dict] = field(default_factory=list)   # assertions: {what, expected, actual, ok}
    shot: str | None = None    # JPEG of the viewport right after this step (journey), file in the run folder


@dataclass
class AttemptResult:
    n: int
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    failed_step: str | None = None
    error: str | None = None
    screenshot: str | None = None   # relative path inside the run folder
    trace: str | None = None
    secs: float = 0.0
    note: str | None = None         # e.g. "re-check with LLM help after triage"
    # Evidence shown in the report, never judged (a shopper-facing failure is decided by the test's own assertions):
    console: list[dict] = field(default_factory=list)          # {type, text, url}: console errors / warnings
    failed_requests: list[str] = field(default_factory=list)   # requests that got no answer, "GET https://..."
    loads: list[dict] = field(default_factory=list)            # {url, ttfb, dcl, load, lcp} seconds, one per page


@dataclass
class CaseResult:
    case_id: str
    suite: str
    title: str
    check: str
    severity: str
    verdict: str = "pending"   # pass | flaky | confirmed_fail | blocked | skipped | no_network (Radar offline, not the store)
    attempts: list[AttemptResult] = field(default_factory=list)
    incident_signature: str | None = None
    triage: dict | None = None      # LLM review of a confirmed failure: {verdict, category, reason, evidence}
    healed_after_triage: bool = False   # failed, triaged as Radar's mistake, then PASSED the re-check


@dataclass
class RunResult:
    run_id: str
    site_id: str
    base_url: str
    device: str
    started_at: str
    finished_at: str = ""
    verdict: str = "pending"   # healthy | degraded | down | unsupported | blocked | unreachable | no_network | error
    platform: str = "unknown"
    cases: list[CaseResult] = field(default_factory=list)
    healing_events: list[dict] = field(default_factory=list)
    llm_usage: dict = field(default_factory=dict)
    sitemap_summary: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    perf: dict = field(default_factory=dict)       # page-load + console summary of the run (executor.perf_summary)

    def counts(self) -> dict:
        c = {"pass": 0, "flaky": 0, "confirmed_fail": 0, "blocked": 0, "skipped": 0}
        for r in self.cases:
            c[r.verdict] = c.get(r.verdict, 0) + 1
        return c

    def to_dict(self) -> dict:
        d = asdict(self)
        d["counts"] = self.counts()
        return d
