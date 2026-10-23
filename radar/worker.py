"""The live runner (14 Oct 2026): one always-on process on our own machine that watches the stores in the dashboard.

    python3 -m radar worker            # run forever: claim a due store, scan it, upload, repeat
    python3 -m radar worker --once     # one due store (or nothing), then exit: what the tests and cron use

Where the list of stores lives: the dashboard's `monitors` table (Supabase). The worker never sees the browser-facing
dashboard; it talks to two SQL functions with the service key:
    claim_monitor(worker)              -> the one store most overdue, locked for this worker (or nothing)
    finish_monitor(id, status, note)   -> unlock + schedule the next run (now + interval_minutes; 10 minutes after a
                                          'recheck': a store that didn't load is looked at again before an alert, 21 Oct)
and, since 15 Oct, two more for the free scan page (onboarding door A): a visitor's "check my store" click is a
`scan_requests` row, claimed BEFORE any daily monitor (someone is waiting on it) and run with the 'basic' profile
(radar/runner/profiles.py: desktop, one of each, no cart, ~1-2 min):
    claim_scan(worker)                 -> the oldest queued scan request, locked for this worker (or nothing)
    finish_scan(id, verdict, score, summary, run_id, project_id, note, failed)
Results go up through the SAME uploader the cloud runs use (tools/upload_dashboard.py, BUGRADAR_KEY), so the
dashboard cannot tell a worker run from a cloud run. Radar's own data dir persists on the machine, so open incidents
pin their product into the next run (executor, 14 Oct).

Environment (never in git):
    BUGRADAR_SUPABASE_URL   https://<ref>.supabase.co
    BUGRADAR_SERVICE_KEY    the project's service-role key (server side only)
    BUGRADAR_KEY            upload key brk_... (as in the cloud workflow)
    RADAR_WORKER_NAME       optional label, default = host name

Free-scan worker WITHOUT the service key (18 Oct): with only BUGRADAR_KEY set, the worker serves the free-scan queue
through the *_by_key functions (dashboard migration 09), which check the upload key the way ingest does. That is how
the GitHub machines run free scans today (.github/workflows/free-scan-worker.yml, every 5 minutes, scans only):
    python3 -m radar worker --scans-only --for 540
Daily monitors still need the service key, and the product's own machine, when a store pays for one.

Why not GitHub Actions for the daily monitors: cron there is late or skipped under load, minutes cost money once the
repo is private, the machines are in the US, and there is no 'scan now'. For a free first check at zero cost it is
good enough; the loading page says "within a few minutes".
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from radar.core.config import Settings, load_settings

# the store's answer as the dashboard's monitor row records it (worst device wins)
ORDER = ["healthy", "degraded", "blocked", "unsupported", "unreachable", "no_network", "down", "error"]


class Queue(Protocol):
    def claim(self, worker: str) -> dict | None: ...
    def finish(self, monitor_id: str, status: str, note: str) -> None: ...
    def claim_scan(self, worker: str) -> dict | None: ...
    def finish_scan(self, scan_id: str, verdict: str, score: int | None, summary: dict,
                    run_id: str | None, project_id: str | None, note: str, failed: bool = False) -> None: ...


# Supabase's public anon key for the BugRadar project: not a secret (every dashboard visitor's browser has it); it only
# lets a caller reach the functions that are granted to anon, and the *_by_key ones demand the upload key on top.
ANON_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im9uaWp3cGd2cG9qcHZndW12ZmltIiwicm9sZSI6"
            "ImFub24iLCJpYXQiOjE3OTE4MTk4MjQsImV4cCI6MjEwNzM5NTgyNH0.UDTcq7XN2mgp_YYxxFw5upLP78qqorgubVVS5HJQmKs")


class SupabaseQueue:
    """The SQL functions over PostgREST. Standard library only.
    service_key given: the full queue (monitors + free scans). upload_key only: free scans and the key's own workspace's
    monitors, through the *_by_key functions (migrations 09 + 14), using the public anon key to reach PostgREST."""

    def __init__(self, url: str, service_key: str | None, timeout: int = 30, upload_key: str | None = None):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.upload_key = None if service_key else upload_key
        self.key = service_key or ANON_KEY
        if not service_key and not upload_key:
            raise ValueError("a service key or an upload key is needed")

    def _by_key(self, fn: str, body: dict):
        """The *_by_key form when only the upload key is held."""
        if self.upload_key:
            return self._rpc(fn + "_by_key", {"brk": self.upload_key, **body})
        return self._rpc(fn, body)

    def _rpc(self, fn: str, body: dict):
        req = urllib.request.Request(
            f"{self.url}/rest/v1/rpc/{fn}", data=json.dumps(body).encode(), method="POST",
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "Prefer": "return=representation"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None

    def claim(self, worker: str) -> dict | None:
        # with the upload key only: monitors of the key's own workspace (dashboard migration 14, 19 Oct), so the box
        # that holds BugRadar's key runs BugRadar's daily monitors without a service key
        rows = self._by_key("claim_monitor", {"worker": worker}) or []
        return rows[0] if rows else None

    def finish(self, monitor_id: str, status: str, note: str) -> None:
        self._by_key("finish_monitor", {"monitor_id": monitor_id, "status": status, "note": note[:300]})

    def claim_scan(self, worker: str) -> dict | None:
        rows = self._by_key("claim_scan", {"worker": worker}) or []
        return rows[0] if rows else None

    def store_password(self, monitor_id: str) -> str | None:
        """The storefront password the store's owner gave BugRadar (dashboard migration 26, 23 Oct), or None. Kept in a
        table nobody can read through the API; only this key-checked function hands it to the worker. Never logged."""
        pwd = self._by_key("monitor_secret", {"monitor_id": monitor_id})
        return pwd if isinstance(pwd, str) and pwd else None

    def scan_progress(self, scan_id: str, progress: dict) -> None:
        self._by_key("scan_progress", {"scan_id": scan_id, "progress": progress})

    def finish_scan(self, scan_id: str, verdict: str, score: int | None, summary: dict,
                    run_id: str | None, project_id: str | None, note: str, failed: bool = False) -> None:
        self._by_key("finish_scan", {"scan_id": scan_id, "verdict": verdict, "score": score, "summary": summary,
                                     "run_id": run_id, "project_id": project_id, "note": note[:300], "failed": failed})


def settings_for(monitor: dict, base: Settings) -> Settings:
    """A daily check's settings. check_store_down (22 Oct): a store that answers nothing at all, not even robots.txt,
    is 'unreachable' (re-checked, then a problem + 🔴), never 'blocked' (silent). store_password (23 Oct): the owner's
    storefront password, typed into the store's own password page only. Free checks keep the base settings."""
    s = base
    if monitor.get("quick", True):
        s = replace(s, max_products=1, max_collections=1, max_nav_links=4)
    return replace(s, allow_cart_flow=bool(monitor.get("allow_cart", False)), check_store_down=True,
                   store_password=monitor.get("store_password") or None)


def overall(verdicts: list[str]) -> str:
    return max(verdicts, key=lambda v: ORDER.index(v) if v in ORDER else len(ORDER)) if verdicts else "error"


# A daily check whose store didn't load is looked at again before anyone is told (21 Oct: one 30 s homepage timeout on
# the Dawn demo opened a problem; the page loaded fine minutes later). A look where any device didn't load uploads
# nothing and finishes as RECHECK; the dashboard's finish_monitor (migration 23) schedules the next look 10 minutes out
# and keeps recheck_since (when the store first didn't load) and rechecks (how many in a row) on the monitor row.
#   second look, store still didn't load      -> uploaded: the problem is filed and alerted (~10 min after the first)
#   second look, store loads / has findings   -> uploaded as usual; a blip leaves no problem and no email
#   second look, Radar couldn't tell (offline, crashed, robots.txt silent while the homepage answers) -> one more
#                                                re-check (at most MAX_RECHECKS in a row), so a real outage isn't dropped
# A store that answers nothing at all, robots.txt included, is 'unreachable' on a daily check (settings_for
# check_store_down, discover.store_answers, 22 Oct), so it takes the re-check -> problem + 🔴 path above.
# A 'recheck' older than RECHECK_FRESH_MINUTES (a paused check resumed later, a lock taken over) starts over.
RECHECK = "recheck"
RECHECK_VERDICTS = ("unreachable",)
CANT_TELL = ("no_network", "error", "blocked")     # Radar learned nothing about whether the store loads
RECHECK_MINUTES = 10            # set in SQL (migration 23); here only for the notes
RECHECK_FRESH_MINUTES = 60      # the same rule is in SQL
MAX_RECHECKS = 2                # the same cap is in SQL


def _when(iso) -> datetime | None:
    """A Postgres timestamp as PostgREST sends it ('2026-10-21T10:15:52.63058+00:00'), or None."""
    m = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d(?::?\d\d)?)?$", str(iso or "").strip())
    if not m:
        return None
    frac = (m.group(3) or "0")[:6].ljust(6, "0")
    tz = m.group(4) or "+00:00"
    tz = "+00:00" if tz == "Z" else tz[:3] + ":" + (tz[3:].lstrip(":") or "00")
    try:
        return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}.{frac}{tz}")
    except ValueError:
        return None


def recheck_state(monitor: dict, now: datetime | None = None) -> tuple[bool, int, int]:
    """(a second look?, re-checks so far, whole minutes since the store first didn't load) for a claimed monitor row."""
    since = _when(monitor.get("recheck_since"))
    if monitor.get("last_status") != RECHECK or since is None:
        return False, 0, 0
    mins = ((now or datetime.now(timezone.utc)) - since).total_seconds() / 60
    if not 0 <= mins <= RECHECK_FRESH_MINUTES:
        return False, 0, 0
    return True, int(monitor.get("rechecks") or 0), max(1, round(mins))


def run_monitor(monitor: dict, base: Settings, upload: bool = True, log=print) -> tuple[str, str]:
    """Scan one monitor on its devices, upload, return (status, note). Never raises: a crash is status 'error'.
    A store that didn't load is not uploaded until a second look (status RECHECK, see above)."""
    from radar.runner.executor import scan_devices
    url = monitor["url"]
    devices = tuple(monitor.get("devices") or ("desktop", "mobile"))
    s = settings_for(monitor, base)
    second_look, rechecks, since_min = recheck_state(monitor)
    try_again = second_look and rechecks < MAX_RECHECKS
    t0 = time.time()
    try:
        results = scan_devices(url, s, devices)
    except Exception as e:  # noqa: BLE001
        log(f"  {url}: Radar crashed: {e}")
        traceback.print_exc()
        if try_again:
            return RECHECK, f"Radar crashed on a second look ({str(e)[:150]}); trying again in {RECHECK_MINUTES} minutes"
        return "error", f"Radar crashed: {str(e)[:200]}"
    verdicts = [r.verdict for r, _ in results]
    note = "; ".join(f"{r.device}: {r.verdict}" for r, _ in results) + f" ({int(time.time() - t0)} s)"
    # a run whose homepage needed more than its time limit (radar/discovery/discover.py homepage(): a slow network or
    # a slow store) can fail later pages for the same reason: not alerted on a first look either (review, 21 Oct)
    slow = any((getattr(r, "sitemap_summary", None) or {}).get("home_slow") for r, _ in results)
    if not second_look and (any(v in RECHECK_VERDICTS for v in verdicts) or (slow and any(v != "healthy" for v in verdicts))):
        why = "didn't load" if any(v in RECHECK_VERDICTS for v in verdicts) else "was very slow and not everything passed"
        log(f"  {url}: {why}; nothing uploaded, looking again in {RECHECK_MINUTES} minutes")
        return RECHECK, f"{note}; {why}, looking again in {RECHECK_MINUTES} minutes"
    if try_again and verdicts and all(v in CANT_TELL for v in verdicts):
        log(f"  {url}: couldn't test it on a second look; nothing uploaded, trying again in {RECHECK_MINUTES} minutes")
        return RECHECK, f"{note}; couldn't test it on a second look, trying again in {RECHECK_MINUTES} minutes"
    if second_look:
        # the trace of a blip: Pilot stores shows this note until the next check, the run carries recheck_minutes
        note += (f"; still didn't load {since_min} min after the first look" if any(v in RECHECK_VERDICTS for v in verdicts)
                 else f"; second look, {since_min} min after it didn't load")
    if upload and results:
        # monitor_id: the dashboard files the run in the monitor's own workspace and store (a pilot store's runs land
        # in that store's workspace, never in BugRadar's; dashboard migration 15 + ingest, 19 Oct). rechecked: this is
        # a second look after the store didn't load (a store still down then says so on its own line).
        meta: dict = {"monitor_id": monitor["id"]} if monitor.get("id") else {}
        if second_look:
            meta.update(rechecked=True, recheck_minutes=since_min)
        recs, why = upload_results(s.data_dir, t0, log, extra_meta=meta or None)
        if why:
            note += f"; {why}"
    return overall(verdicts), note


def upload_results(data_dir, since: float, log=print, extra_meta: dict | None = None) -> tuple[list[dict], str]:
    """Send every run.json written since `since` under the data dir (one per device) through the same uploader the
    cloud runs use. Returns (records, problem): records as tools.upload_dashboard.upload gives them, problem = a short
    note for the row when nothing could be sent ('' when all went)."""
    key = os.environ.get("BUGRADAR_KEY", "")
    if not key:
        return [], "NOT uploaded (BUGRADAR_KEY unset)"
    from tools.upload_dashboard import upload
    try:
        recs = upload(Path(data_dir), key, since=since, log=log, extra_meta=extra_meta)
    except Exception as e:  # noqa: BLE001
        return [], f"upload failed: {str(e)[:120]}"
    bad = [r for r in recs if not r["ok"]]
    return recs, (f"upload failed: {bad[0]['error']}" if bad else "")


# ---- the free scan page (door A, 15 Oct) ----------------------------------------------------------------------------
# One number for the visitor. A store Radar could not test has no score (None): nothing was measured.
SCORE_WEIGHT = {"critical": 30, "major": 15, "minor": 6, "seo": 4}
WARNING_WEIGHT = 3
TESTED_VERDICTS = ("healthy", "degraded", "down")


def warning_of(case: dict) -> str | None:
    """A check that passed but left a 'warn' step (slow page, a widget that stayed silent): the step's words."""
    if case.get("verdict") not in ("pass", "flaky"):
        return None
    last = (case.get("attempts") or [{}])[-1]
    for st in last.get("steps") or []:
        if st.get("status") == "warn":
            return str(st.get("detail") or st.get("error") or st.get("name") or "")[:300] or None
    return None


def score_of(run: dict) -> int | None:
    """100 minus the weight of every confirmed failure (by severity) and every warning; never below 0.
    `run` is the run.json shape (RunResult.to_dict())."""
    cases = run.get("cases") or []
    if run.get("verdict") not in TESTED_VERDICTS or not cases:
        return None
    lost = 0
    for c in cases:
        if c.get("verdict") == "confirmed_fail":
            lost += SCORE_WEIGHT.get(c.get("severity"), SCORE_WEIGHT["major"])
        elif warning_of(c):
            lost += WARNING_WEIGHT
    return max(0, 100 - lost)


def summarize(run: dict) -> dict:
    """What the result page shows before it loads the full list: the top problems in plain words, and the counts.
    `run` is the run.json shape, the same the uploader reads, so the reasons match the dashboard word for word."""
    from tools.upload_dashboard import plain_name, reason_of, STATUS
    order = {"critical": 0, "major": 1, "minor": 2, "seo": 3}
    cases = run.get("cases") or []
    problems, notes, counts = [], [], {"checked": len(cases), "passed": 0, "failed": 0, "warnings": 0, "not_tested": 0}
    for c in cases:
        status = STATUS.get(c.get("verdict"), "warning")
        warn = warning_of(c)
        name = plain_name(c.get("title") or c["case_id"])
        if status in ("passed", "flaky"):
            counts["passed"] += 1
            if warn:
                # a check that WORKS with a note is not a problem (19 Oct: "Homepage loads healthy" was stored under
                # problems); it goes under warning_checks, which the result page shows as "works, with a note"
                counts["warnings"] += 1
                notes.append({"key": c["case_id"], "name": name, "severity": c.get("severity") or "major",
                              "status": "warning", "reason": warn})
        elif status == "failed":
            counts["failed"] += 1
            problems.append({"key": c["case_id"], "name": name, "severity": c.get("severity") or "major",
                             "status": "failed", "reason": reason_of(c)})
        else:
            counts["not_tested"] += 1
    problems.sort(key=lambda x: order.get(x["severity"], 9))
    notes.sort(key=lambda x: order.get(x["severity"], 9))
    return {**counts, "problems": problems[:5], "warning_checks": notes[:5], "platform": run.get("platform"),
            "notes": [str(n)[:200] for n in (run.get("notes") or [])[:2]]}


class ScanProgress:
    """The loading screen's live list (15 Oct): Radar's progress events -> one small JSON on the scan_requests row.
        {"stage": "opening" | "testing" | "finishing", "store": {platform, theme, products, collections},
         "checks": [{"key", "name", "state": waiting | running | passed | warning | retrying | failed | not_tested}]}
    Writes go through `send` (queue.scan_progress); a failed write never stops the scan."""

    def __init__(self, send, log=print):
        self.send, self.log = send, log
        self.state: dict = {"stage": "opening", "store": {}, "checks": []}

    def _push(self):
        try:
            self.send(self.state)
        except Exception as e:  # noqa: BLE001
            self.log(f"  progress not saved: {str(e)[:120]}")

    def _mark_next_running(self):
        for c in self.state["checks"]:
            if c["state"] == "waiting":
                c["state"] = "running"
                return

    def __call__(self, event: str, d: dict) -> None:
        if event == "start":
            self._push()
        elif event == "discovered":
            sm = d["sitemap"]
            self.state["store"] = {"platform": sm.platform, "theme": sm.theme, "products": len(sm.products),
                                   "collections": len(sm.collections), "title": (sm.home_title or "")[:120]}
        elif event == "suites":
            from tools.upload_dashboard import plain_name
            self.state["stage"] = "testing"
            self.state["checks"] = [{"key": c.id, "name": plain_name(c.title), "state": "waiting"}
                                    for s in d["suites"] for c in s.cases]
            self._mark_next_running()
            self._push()
        elif event == "attempt":
            key, ar = d["case"].id, d["attempt"]
            for c in self.state["checks"]:
                if c["key"] == key:
                    if ar.ok:
                        c["state"] = "warning" if any(s.status == "warn" for s in ar.steps) else "passed"
                    else:
                        c["state"] = "retrying"
                    break
            if ar.ok:
                self._mark_next_running()
            self._push()
        elif event == "done":
            self.state["stage"] = "finishing"
            run = d["run"]
            final = {c.case_id: c.verdict for c in run.cases}
            for c in self.state["checks"]:
                v = final.get(c["key"])
                if v == "confirmed_fail":
                    c["state"] = "failed"
                elif v in ("blocked", "skipped", "no_network"):
                    c["state"] = "not_tested"
                elif v in ("pass", "flaky") and c["state"] in ("waiting", "running", "retrying"):
                    c["state"] = "passed"
            self._push()


HOME_SHOT = "homepage.jpg"


def homepage_shot(url: str, settings: Settings, run_dir, log=print) -> bool:
    """The free result page's "your store as Radar saw it" (19 Oct): one desktop picture of the homepage, saved next to
    run.json as homepage.jpg; the uploader attaches it to the store's own line. Only after a run that could test the
    store (so robots.txt and the platform already said yes). Same browser, user agent and signature as the run. Never
    raises: a store with no picture still gets its result."""
    try:
        from radar.core.browser import Browser
        with Browser(settings, "desktop", store_url=url) as b:
            ctx = b.new_context()
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:  # noqa: BLE001  (chat widgets and analytics never go quiet: the page is shown anyway)
                    pass
                page.wait_for_timeout(1200)
                (Path(run_dir) / HOME_SHOT).write_bytes(page.screenshot(type="jpeg", quality=70, full_page=False))
                return True
            finally:
                ctx.close()
    except Exception as e:  # noqa: BLE001
        log(f"  {url}: homepage picture skipped: {str(e)[:120]}")
        return False


def run_scan(req: dict, base: Settings, upload: bool = True, log=print, progress=None) -> dict:
    """One free basic check: desktop only, the 'basic' profile, uploaded like any run (so the result page can show the
    dashboard's results + screenshots), then the verdict, score and summary for the scan_requests row. Never raises.
    progress: a ScanProgress (or any callable taking Radar's progress events) for the live loading screen."""
    from radar.runner.executor import scan_devices
    url = req["url"]
    t0 = time.time()
    try:
        results = scan_devices(url, replace(base, allow_cart_flow=False), ("desktop",), profile="basic",
                               progress=progress)
    except Exception as e:  # noqa: BLE001
        log(f"  {url}: Radar crashed: {e}")
        traceback.print_exc()
        return {"verdict": "error", "score": None, "summary": {}, "run_id": None, "project_id": None,
                "note": f"Radar crashed: {str(e)[:200]}", "failed": True}
    if not results:
        return {"verdict": "error", "score": None, "summary": {}, "run_id": None, "project_id": None,
                "note": "nothing ran", "failed": True}
    run, run_dir = results[0]
    d = run.to_dict()
    if run.verdict in TESTED_VERDICTS:
        homepage_shot(url, replace(base, allow_cart_flow=False), run_dir, log)
    out = {"verdict": run.verdict, "score": score_of(d), "summary": summarize(d), "run_id": None,
           "project_id": None, "note": f"desktop: {run.verdict} ({int(time.time() - t0)} s)", "failed": False}
    if upload:
        # meta.scan_request_id: the dashboard's ingest files a free scan under the "Free scans" workspace, not the key's
        # own (18 Oct: visitors' stores were filling Anmol's store list)
        recs, why = upload_results(base.data_dir, t0, log, extra_meta={"scan_request_id": req["id"]})
        mine = [r for r in recs if r["ok"] and r.get("radar_run_id") == run.run_id] or [r for r in recs if r["ok"]]
        if mine:
            out["run_id"], out["project_id"] = mine[0]["run_id"], mine[0]["project_id"]
        if why:
            out["note"] += f"; {why}"
    return out


def run_once(queue: Queue, base: Settings, worker: str, upload: bool = True, log=print, scans_only: bool = False) -> dict | None:
    """Claim one due job and run it: a free scan request first (a visitor is waiting), else one due monitor.
    Returns the row that ran, or None when nothing was due. scans_only: never a monitor (the GitHub free-scan worker)."""
    req = queue.claim_scan(worker) if hasattr(queue, "claim_scan") else None
    if req:
        log(f"[{_now()}] {worker}: free scan {req['url']} ({req['id']})")
        prog = ScanProgress(lambda st: queue.scan_progress(req["id"], st), log) if hasattr(queue, "scan_progress") else None
        r = run_scan(req, base, upload=upload, log=log, progress=prog)
        queue.finish_scan(req["id"], r["verdict"], r["score"], r["summary"], r["run_id"], r["project_id"],
                          r["note"], r["failed"])
        log(f"[{_now()}] {worker}: free scan {req['url']} -> {r['verdict']} score {r['score']} ({r['note']})")
        return req
    if scans_only:
        return None
    m = queue.claim(worker)
    if not m:
        return None
    log(f"[{_now()}] {worker}: {m['url']} on {','.join(m.get('devices') or ['desktop', 'mobile'])}")
    if hasattr(queue, "store_password") and m.get("id"):
        try:
            m["store_password"] = queue.store_password(m["id"])
        except Exception as e:  # noqa: BLE001  an old dashboard without migration 26, or a blip: test without it
            log(f"  {m['url']}: couldn't read the store password ({type(e).__name__}); testing without it")
    status, note = run_monitor(m, base, upload=upload, log=log)
    queue.finish(m["id"], status, note)
    log(f"[{_now()}] {worker}: {m['url']} -> {status} ({note})")
    return m


def serve(queue: Queue, base: Settings, worker: str, poll_seconds: int = 60, upload: bool = True, log=print,
          scans_only: bool = False, _stop=lambda: False) -> None:
    """Run for ever. scans_only: free checks only, never a daily monitor (19 Oct: this always-on mode ignored the flag,
    which only showed once monitors became claimable by key; Contabo slot 1 then took a monitor)."""
    log(f"[{_now()}] Radar worker {worker} started; polling every {poll_seconds} s" + (" (free scans only)" if scans_only else ""))
    while not _stop():
        try:
            ran = run_once(queue, base, worker, upload=upload, log=log, scans_only=scans_only)
        except (urllib.error.URLError, OSError) as e:        # Supabase unreachable: wait, try again, never die
            log(f"[{_now()}] queue error: {e}")
            ran = None
        if not ran:
            time.sleep(poll_seconds)


def queue_from_env() -> SupabaseQueue:
    url = os.environ.get("BUGRADAR_SUPABASE_URL", "") or "https://onijwpgvpojpvgumvfim.supabase.co"
    service, upload = os.environ.get("BUGRADAR_SERVICE_KEY", ""), os.environ.get("BUGRADAR_KEY", "")
    if not service and not upload.startswith("brk_"):
        raise SystemExit("set BUGRADAR_SERVICE_KEY (full worker) or BUGRADAR_KEY (free scans only); see radar/worker.py")
    return SupabaseQueue(url, service or None, upload_key=upload or None)


def serve_for(queue: Queue, base: Settings, worker: str, seconds: int, upload: bool = True, log=print,
              scans_only: bool = False, poll_seconds: int = 15) -> int:
    """Drain the queue for up to `seconds` (a GitHub job), then exit. Returns how many jobs ran."""
    t_end, n = time.time() + seconds, 0
    log(f"[{_now()}] Radar worker {worker}: serving for {seconds} s" + (" (free scans only)" if scans_only else ""))
    while time.time() < t_end:
        try:
            ran = run_once(queue, base, worker, upload=upload, log=log, scans_only=scans_only)
        except (urllib.error.URLError, OSError) as e:
            log(f"[{_now()}] queue error: {e}")
            ran = None
        if ran:
            n += 1
        elif time.time() + poll_seconds < t_end:
            time.sleep(poll_seconds)
        else:
            break
    log(f"[{_now()}] Radar worker {worker}: {n} job(s) done")
    return n


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="radar worker", description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true", help="run one due store (if any) and exit")
    ap.add_argument("--poll", type=int, default=60, help="seconds between checks when nothing is due")
    ap.add_argument("--no-upload", action="store_true", help="scan only; send nothing to the dashboard")
    ap.add_argument("--scans-only", action="store_true", help="serve free scan requests only, never a daily monitor")
    ap.add_argument("--for", dest="seconds", type=int, default=0, metavar="SECS",
                    help="drain the queue for this many seconds, then exit (a GitHub job); 0 = run for ever")
    a = ap.parse_args(argv)
    worker = os.environ.get("RADAR_WORKER_NAME") or socket.gethostname()
    base = load_settings()
    q = queue_from_env()
    if a.once:
        ran = run_once(q, base, worker, upload=not a.no_upload, scans_only=a.scans_only)
        print("nothing due" if ran is None else "done")
        return 0
    if a.seconds:
        serve_for(q, base, worker, a.seconds, upload=not a.no_upload, scans_only=a.scans_only)
        return 0
    serve(q, base, worker, a.poll, upload=not a.no_upload, scans_only=a.scans_only)
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
