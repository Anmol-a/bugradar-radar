"""Executor: URL in, stored + reported RunResult out. The whole pipeline in one place.

  scan(url)
    -> discovery (own browser context)          SiteMap       -> data/sites/<id>/sitemap.json
    -> generator                                Suites        -> data/sites/<id>/suites.json
    -> for each TestCase: attempts in FRESH contexts with confirm-retry
         pass first time          -> pass
         fail then pass           -> flaky            (reported, no incident)
         2 of 3 attempts fail     -> confirmed_fail   (incident opened / updated)
         robots.txt disallows     -> blocked          (no retry, not a site bug)
         Radar itself offline     -> no_network       (checked before any failure is confirmed; the run stops,
                                                       nothing is reported against the store; bench 8, 6 Oct)
    -> run verdict: down (critical confirmed_fail) | degraded | healthy | unsupported | no_network
    -> SQLite rows + run.json + report.html + site index.html

  scan_devices(url)   = scan() on desktop, then on mobile (the default everywhere since 7 Oct: every check runs
                        on both screen sizes). Mobile is skipped when desktop could not be tested at all
                        (blocked, unreachable, offline, not Shopify): a store that refused or could not be
                        reached is not asked a second time.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from radar.checks.library import REGISTRY, Ctx, Steps, StepFailed
from radar.core.browser import Browser, DEFAULT_DEVICES
from radar.core.config import Settings, normalize_url, site_id_from_url
from radar.core.models import AttemptResult, CaseResult, RunResult, SiteMap, StepResult, TestCase
from radar.core.network import NO_NETWORK_NOTE, online
from radar.core.storage import Storage
from radar.discovery.discover import discover
from radar.generate.builder import build_suites
from radar.healing.llm import LLMClient, make_client
from radar.healing.locator import Healer
from radar.healing.triage import triage
from radar.runner.confirm import Attempt, should_retry, verdict as confirm_verdict, signature

Progress = Callable[[str, dict], None]
MAX_FAILED_REQUESTS = 25
TESTED = ("healthy", "degraded", "down")        # verdicts that mean tests really ran against the store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _attempt(browser: Browser, case: TestCase, run_dir, robots, healer: Healer, n: int,
             assist: bool = False):
    """One attempt in a FRESH browser context. Returns (AttemptResult, Session, blocked)."""
    fn = REGISTRY[case.check]
    t0 = time.time()
    failed: StepFailed | None = None
    blocked = False
    with browser.attempt(run_dir, f"{case.id}.a{n}") as sess:
        sess.robots = robots
        steps = Steps(shooter=sess.step_shot if case.suite == "journey" else None)
        try:
            fn(Ctx(sess, healer, steps, llm_assist=assist), **case.params)
        except StepFailed as f:
            failed, blocked = f, f.blocked
            sess.failed = not f.blocked
        except Exception as e:  # noqa: BLE001  a bug in Radar itself must not kill the run
            failed = StepFailed("radar_internal", f"{type(e).__name__}: {str(e)[:200]}")
            steps.items.append(StepResult("radar_internal", "fail", None, failed.args[0]))
            sess.failed = True
        sess.record_load()               # the page the attempt ended on (evidence only)
    ar = AttemptResult(n, failed is None, steps.items, failed.step if failed else None,
                       str(failed) if failed else None, sess.screenshot, sess.trace,
                       round(time.time() - t0, 2), console=list(sess.console),
                       failed_requests=list(dict.fromkeys(sess.failed_requests))[:MAX_FAILED_REQUESTS],
                       loads=list(sess.loads))
    return ar, sess, blocked


def _run_case(browser: Browser, case: TestCase, run_dir, robots, healer: Healer, retries: int,
              progress: Progress, probes: tuple = (), device: str = "desktop") -> CaseResult:
    cr = CaseResult(case.id, case.suite, case.title, case.check, case.severity)
    confirm_attempts: list[Attempt] = []
    last_fail = None
    while True:
        ar, sess, blocked = _attempt(browser, case, run_dir, robots, healer, len(cr.attempts) + 1)
        cr.attempts.append(ar)
        progress("attempt", {"case": case, "attempt": ar})
        if blocked:
            cr.verdict = "blocked"
            return cr
        if not ar.ok:
            last_fail = (ar, sess)
        confirm_attempts.append(Attempt(ar.ok, ar.failed_step, ar.error))
        if not should_retry(confirm_attempts) or len(cr.attempts) > retries:
            break
    cr.verdict = confirm_verdict(confirm_attempts)
    if cr.verdict == "confirmed_fail" and not online(probes):
        # Radar's own connection is gone: this failure says nothing about the store (bench 8, 6 Oct)
        cr.verdict = "no_network"
        cr.attempts[-1].note = "Radar was offline when this failure would have been confirmed"
        return cr
    if cr.verdict == "confirmed_fail" and last_fail and healer.llm.enabled and case.severity != "seo":
        _triage_and_recheck(browser, case, cr, last_fail, run_dir, robots, healer, progress)
    if cr.verdict == "confirmed_fail" and case.severity != "seo":      # SEO notes never open incidents (no alerts)
        cr.incident_signature = signature(healer.site_id, case.id, cr.attempts[-1].failed_step, device)
    return cr


def _triage_and_recheck(browser, case, cr: CaseResult, last_fail, run_dir, robots, healer: Healer, progress):
    """Confirmed failure -> the LLM looks at the evidence. If it blames Radar, ONE more attempt runs with
    LLM help (product name, overlays). Only a passing re-check (same code assertions) clears the failure."""
    ar, sess = last_fail
    failed = next((s for s in ar.steps if s.status == "fail"), None)
    cr.triage = triage(healer.llm, case.title, case.check, ar.failed_step, ar.error,
                       failed.checks if failed else [], sess.fail_url, sess.fail_text, sess.fail_jpeg)
    progress("triage", {"case": case, "triage": cr.triage})
    if not cr.triage or cr.triage["verdict"] != "radar_problem":
        return
    re_ar, _, blocked = _attempt(browser, case, run_dir, robots, healer, len(cr.attempts) + 1, assist=True)
    re_ar.note = "re-check with LLM help after triage"
    cr.attempts.append(re_ar)
    progress("attempt", {"case": case, "attempt": re_ar})
    if re_ar.ok:
        cr.verdict, cr.healed_after_triage = "pass", True


def perf_summary(cases: list[CaseResult]) -> dict:
    """Page-load and console evidence of a run in a few numbers (pure, unit-tested). From each case's last attempt;
    one value per page (the slowest time seen for that URL). Evidence only: nothing here changes a verdict.
    Empty dict when no page timing was captured."""
    pages: dict[str, float] = {}
    console: dict[tuple, dict] = {}
    failed: set[str] = set()
    for c in cases:
        if not c.attempts:
            continue
        a = c.attempts[-1]
        for l in a.loads:
            if l.get("load") is not None:
                pages[l["url"]] = max(pages.get(l["url"], 0), l["load"])
        for e in a.console:
            console[(e["type"], e["text"][:120])] = e
        failed.update(a.failed_requests)
    if not pages and not console and not failed:
        return {}
    vals = sorted(pages.values())
    median = None
    if vals:
        mid = len(vals) // 2
        median = vals[mid] if len(vals) % 2 else round((vals[mid - 1] + vals[mid]) / 2, 2)
    slow = max(pages.items(), key=lambda kv: kv[1]) if pages else None
    return {"pages": len(pages), "median_load_secs": median,
            "slowest": {"url": slow[0], "load_secs": slow[1]} if slow else None,
            "console_errors": sum(1 for k in console if k[0] in ("error", "pageerror")),
            "console_warnings": sum(1 for k in console if k[0] == "warning"),
            "failed_requests": len(failed)}


def run_verdict(cases: list[CaseResult]) -> str:
    # SEO notes (soft 404, meta tags; severity "seo", 9 Oct) are shown in reports but never make a store
    # degraded or down: a shopper can still find and buy everything.
    cases = [c for c in cases if c.severity != "seo"]
    if any(c.verdict == "confirmed_fail" and c.severity == "critical" for c in cases):
        return "down"
    if any(c.verdict in ("confirmed_fail", "flaky") for c in cases):
        return "degraded"
    if any(c.verdict == "blocked" and c.severity == "critical" for c in cases):
        return "blocked"          # the critical path could not be tested (age gate, robots.txt): not "healthy"
    return "healthy"


def scan(url: str, settings: Settings, device: str = "desktop", storage: Storage | None = None,
         llm: LLMClient | None = None, progress: Progress | None = None,
         only_suites: list[str] | None = None) -> tuple[RunResult, "Path"]:
    progress = progress or (lambda *_: None)
    base = normalize_url(url)
    site_id = site_id_from_url(base)
    s = settings.for_site(site_id)
    storage = storage or Storage(s.data_dir)
    llm = llm or make_client(s)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{device[0]}-" + uuid.uuid4().hex[:4]
    run_dir = storage.run_dir(site_id, run_id)
    run = RunResult(run_id, site_id, base, device, _now())
    progress("start", {"site_id": site_id, "run_id": run_id, "llm": llm.usage(), "device": device})

    with Browser(s, device) as browser:
        # ---- discovery
        with browser.attempt(run_dir, "discovery") as sess:
            try:
                sm = discover(sess, base, s)
            except Exception as e:  # noqa: BLE001
                sess.failed = True
                sm = SiteMap(site_id=site_id, base_url=base, notes=[f"discovery crashed: {e}"[:300]])
            robots = sess.robots
        run.platform = sm.platform
        run.notes = list(sm.notes)
        run.sitemap_summary = {
            "platform": sm.platform, "evidence": sm.platform_evidence, "home_title": sm.home_title,
            "theme": sm.theme, "checkout_app": sm.checkout_app, "access": sm.access,
            "nav": len(sm.nav), "collections": len(sm.collections), "products": len(sm.products),
            "in_stock": sum(p.available for p in sm.products), "search_path": sm.search_path,
            "robots_loaded": sm.robots_loaded}
        storage.save_json(site_id, "sitemap.json", sm.to_dict())
        storage.upsert_site(site_id, sm.base_url, sm.platform)
        progress("discovered", {"sitemap": sm})

        if sm.access == "no_network":
            run.verdict = "no_network"
        elif sm.access in ("password", "bot_blocked", "robots_blocked", "robots_unreachable", "refused"):
            run.verdict = "blocked"
        elif sm.access in ("unreachable", "offsite"):
            run.verdict = "unreachable"
        elif sm.platform != "shopify":
            run.verdict = "unsupported" if sm.home_title or sm.platform_evidence or sm.robots_loaded else "error"
        else:
            suites = build_suites(sm, s)
            if only_suites:
                suites = [x for x in suites if x.id in only_suites]
            storage.save_json(site_id, "suites.json", [
                {"id": x.id, "name": x.name, "description": x.description,
                 "cases": [c.__dict__ for c in x.cases]} for x in suites])
            progress("suites", {"suites": suites})
            healer = Healer(site_id, run_id, storage, llm, s.selectors, device)
            lost = None
            total = sum(len(x.cases) for x in suites)
            for suite in suites:
                for case in suite.cases:
                    cr = _run_case(browser, case, run_dir, robots, healer, s.retries, progress, s.net_probe_urls, device)
                    run.cases.append(cr)
                    if cr.verdict == "no_network":
                        lost = cr
                        break
                if lost:
                    break
            run.healing_events = healer.events
            if lost:
                run.verdict = "no_network"
                run.notes.append(NO_NETWORK_NOTE.format(
                    where=f" during '{lost.title}' ({total - len(run.cases)} of {total} tests not run)"))
            else:
                run.verdict = run_verdict(run.cases)

    run.finished_at = _now()
    run.perf = perf_summary(run.cases)
    run.llm_usage = llm.usage()
    d = run.to_dict()
    (run_dir / "run.json").write_text(json.dumps(d, indent=2, default=str))
    storage.save_run(d, run_dir)
    if run.cases and run.verdict != "no_network":   # nothing tested = nothing proven fixed: incidents stay open
        storage.resolve_missing_incidents(site_id, {c.incident_signature for c in run.cases if c.incident_signature},
                                          device)
    storage.prune(site_id, s.retention_pass_days, s.retention_fail_days)

    from radar.reporting.html import write_run_report, write_site_index
    write_run_report(d, run_dir, storage.history(site_id, 60), storage.incidents(site_id))
    write_site_index(site_id, storage)
    progress("done", {"run": run, "run_dir": run_dir})
    return run, run_dir


def scan_devices(url: str, settings: Settings, devices: tuple[str, ...] = DEFAULT_DEVICES,
                 progress: Progress | None = None, only_suites: list[str] | None = None,
                 ) -> list[tuple[RunResult, "Path"]]:
    """Run the scan on every device, in order (desktop first). Returns one (run, run_dir) per device that RAN.
    A later device is skipped when the first one could not test the store at all (blocked, unreachable, offline,
    not Shopify): same answer, and a store that refused Radar is not asked again."""
    progress = progress or (lambda *_: None)
    out: list[tuple[RunResult, "Path"]] = []
    for dev in devices:
        if out and out[0][0].verdict not in TESTED:
            progress("device_skipped", {"device": dev, "why": f"not tested on {devices[0]} ({out[0][0].verdict}), "
                                                                "so not asked again"})
            continue
        out.append(scan(url, settings, dev, progress=progress, only_suites=only_suites))
    return out
