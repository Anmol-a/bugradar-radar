"""The live runner (14 Oct 2026): one always-on process on our own machine that watches the stores in the dashboard.

    python3 -m radar worker            # run forever: claim a due store, scan it, upload, repeat
    python3 -m radar worker --once     # one due store (or nothing), then exit: what the tests and cron use

Where the list of stores lives: the dashboard's `monitors` table (Supabase). The worker never sees the browser-facing
dashboard; it talks to two SQL functions with the service key:
    claim_monitor(worker)              -> the one store most overdue, locked for this worker (or nothing)
    finish_monitor(id, status, note)   -> unlock + schedule the next run (now + interval_minutes)
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

Why not GitHub Actions for this: cron there is late or skipped under load, minutes cost money once the repo is private,
the machines are in the US, and there is no 'scan now'. GitHub keeps running the test suite; this runs the product.
"""
from __future__ import annotations

import json
import os
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


class SupabaseQueue:
    """The two SQL functions over PostgREST. Standard library only."""

    def __init__(self, url: str, service_key: str, timeout: int = 30):
        self.url, self.key, self.timeout = url.rstrip("/"), service_key, timeout

    def _rpc(self, fn: str, body: dict):
        req = urllib.request.Request(
            f"{self.url}/rest/v1/rpc/{fn}", data=json.dumps(body).encode(), method="POST",
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "Prefer": "return=representation"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None

    def claim(self, worker: str) -> dict | None:
        rows = self._rpc("claim_monitor", {"worker": worker}) or []
        return rows[0] if rows else None

    def finish(self, monitor_id: str, status: str, note: str) -> None:
        self._rpc("finish_monitor", {"monitor_id": monitor_id, "status": status, "note": note[:300]})

    def claim_scan(self, worker: str) -> dict | None:
        rows = self._rpc("claim_scan", {"worker": worker}) or []
        return rows[0] if rows else None

    def scan_progress(self, scan_id: str, progress: dict) -> None:
        self._rpc("scan_progress", {"scan_id": scan_id, "progress": progress})

    def finish_scan(self, scan_id: str, verdict: str, score: int | None, summary: dict,
                    run_id: str | None, project_id: str | None, note: str, failed: bool = False) -> None:
        self._rpc("finish_scan", {"scan_id": scan_id, "verdict": verdict, "score": score, "summary": summary,
                                  "run_id": run_id, "project_id": project_id, "note": note[:300], "failed": failed})


def settings_for(monitor: dict, base: Settings) -> Settings:
    s = base
    if monitor.get("quick", True):
        s = replace(s, max_products=1, max_collections=1, max_nav_links=4)
    return replace(s, allow_cart_flow=bool(monitor.get("allow_cart", False)))


def overall(verdicts: list[str]) -> str:
    return max(verdicts, key=lambda v: ORDER.index(v) if v in ORDER else len(ORDER)) if verdicts else "error"


def run_monitor(monitor: dict, base: Settings, upload: bool = True, log=print) -> tuple[str, str]:
    """Scan one monitor on its devices, upload, return (status, note). Never raises: a crash is status 'error'."""
    from radar.runner.executor import scan_devices
    url = monitor["url"]
    devices = tuple(monitor.get("devices") or ("desktop", "mobile"))
    s = settings_for(monitor, base)
    t0 = time.time()
    try:
        results = scan_devices(url, s, devices)
    except Exception as e:  # noqa: BLE001
        log(f"  {url}: Radar crashed: {e}")
        traceback.print_exc()
        return "error", f"Radar crashed: {str(e)[:200]}"
    verdicts = [r.verdict for r, _ in results]
    note = "; ".join(f"{r.device}: {r.verdict}" for r, _ in results) + f" ({int(time.time() - t0)} s)"
    if upload and results:
        recs, why = upload_results(s.data_dir, t0, log)
        if why:
            note += f"; {why}"
    return overall(verdicts), note


def upload_results(data_dir, since: float, log=print) -> tuple[list[dict], str]:
    """Send every run.json written since `since` under the data dir (one per device) through the same uploader the
    cloud runs use. Returns (records, problem): records as tools.upload_dashboard.upload gives them, problem = a short
    note for the row when nothing could be sent ('' when all went)."""
    key = os.environ.get("BUGRADAR_KEY", "")
    if not key:
        return [], "NOT uploaded (BUGRADAR_KEY unset)"
    from tools.upload_dashboard import upload
    try:
        recs = upload(Path(data_dir), key, since=since, log=log)
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
    problems, counts = [], {"checked": len(cases), "passed": 0, "failed": 0, "warnings": 0, "not_tested": 0}
    for c in cases:
        status = STATUS.get(c.get("verdict"), "warning")
        warn = warning_of(c)
        name = plain_name(c.get("title") or c["case_id"])
        if status in ("passed", "flaky"):
            counts["passed"] += 1
            if warn:
                counts["warnings"] += 1
                problems.append({"key": c["case_id"], "name": name, "severity": c.get("severity") or "major",
                                 "status": "warning", "reason": warn})
        elif status == "failed":
            counts["failed"] += 1
            problems.append({"key": c["case_id"], "name": name, "severity": c.get("severity") or "major",
                             "status": "failed", "reason": reason_of(c)})
        else:
            counts["not_tested"] += 1
    problems.sort(key=lambda x: (0 if x["status"] == "failed" else 1, order.get(x["severity"], 9)))
    return {**counts, "problems": problems[:5], "platform": run.get("platform"),
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
    run = results[0][0]
    d = run.to_dict()
    out = {"verdict": run.verdict, "score": score_of(d), "summary": summarize(d), "run_id": None,
           "project_id": None, "note": f"desktop: {run.verdict} ({int(time.time() - t0)} s)", "failed": False}
    if upload:
        recs, why = upload_results(base.data_dir, t0, log)
        mine = [r for r in recs if r["ok"] and r.get("radar_run_id") == run.run_id] or [r for r in recs if r["ok"]]
        if mine:
            out["run_id"], out["project_id"] = mine[0]["run_id"], mine[0]["project_id"]
        if why:
            out["note"] += f"; {why}"
    return out


def run_once(queue: Queue, base: Settings, worker: str, upload: bool = True, log=print) -> dict | None:
    """Claim one due job and run it: a free scan request first (a visitor is waiting), else one due monitor.
    Returns the row that ran, or None when nothing was due."""
    req = queue.claim_scan(worker) if hasattr(queue, "claim_scan") else None
    if req:
        log(f"[{_now()}] {worker}: free scan {req['url']} ({req['id']})")
        prog = ScanProgress(lambda st: queue.scan_progress(req["id"], st), log) if hasattr(queue, "scan_progress") else None
        r = run_scan(req, base, upload=upload, log=log, progress=prog)
        queue.finish_scan(req["id"], r["verdict"], r["score"], r["summary"], r["run_id"], r["project_id"],
                          r["note"], r["failed"])
        log(f"[{_now()}] {worker}: free scan {req['url']} -> {r['verdict']} score {r['score']} ({r['note']})")
        return req
    m = queue.claim(worker)
    if not m:
        return None
    log(f"[{_now()}] {worker}: {m['url']} on {','.join(m.get('devices') or ['desktop', 'mobile'])}")
    status, note = run_monitor(m, base, upload=upload, log=log)
    queue.finish(m["id"], status, note)
    log(f"[{_now()}] {worker}: {m['url']} -> {status} ({note})")
    return m


def serve(queue: Queue, base: Settings, worker: str, poll_seconds: int = 60, upload: bool = True, log=print) -> None:
    log(f"[{_now()}] Radar worker {worker} started; polling every {poll_seconds} s")
    while True:
        try:
            ran = run_once(queue, base, worker, upload=upload, log=log)
        except (urllib.error.URLError, OSError) as e:        # Supabase unreachable: wait, try again, never die
            log(f"[{_now()}] queue error: {e}")
            ran = None
        if not ran:
            time.sleep(poll_seconds)


def queue_from_env() -> SupabaseQueue:
    url, key = os.environ.get("BUGRADAR_SUPABASE_URL", ""), os.environ.get("BUGRADAR_SERVICE_KEY", "")
    if not url or not key:
        raise SystemExit("BUGRADAR_SUPABASE_URL and BUGRADAR_SERVICE_KEY must be set (see radar/worker.py)")
    return SupabaseQueue(url, key)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="radar worker", description=__doc__.split("\n\n")[0])
    ap.add_argument("--once", action="store_true", help="run one due store (if any) and exit")
    ap.add_argument("--poll", type=int, default=60, help="seconds between checks when nothing is due")
    ap.add_argument("--no-upload", action="store_true", help="scan only; send nothing to the dashboard")
    a = ap.parse_args(argv)
    worker = os.environ.get("RADAR_WORKER_NAME") or socket.gethostname()
    base = load_settings()
    q = queue_from_env()
    if a.once:
        ran = run_once(q, base, worker, upload=not a.no_upload)
        print("nothing due" if ran is None else "done")
        return 0
    serve(q, base, worker, a.poll, upload=not a.no_upload)
    return 0


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
