"""The live runner (14 Oct 2026): one always-on process on our own machine that watches the stores in the dashboard.

    python3 -m radar worker            # run forever: claim a due store, scan it, upload, repeat
    python3 -m radar worker --once     # one due store (or nothing), then exit: what the tests and cron use

Where the list of stores lives: the dashboard's `monitors` table (Supabase). The worker never sees the browser-facing
dashboard; it talks to two SQL functions with the service key:
    claim_monitor(worker)              -> the one store most overdue, locked for this worker (or nothing)
    finish_monitor(id, status, note)   -> unlock + schedule the next run (now + interval_minutes)
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
        key = os.environ.get("BUGRADAR_KEY", "")
        if not key:
            note += "; NOT uploaded (BUGRADAR_KEY unset)"
        else:
            from tools.upload_dashboard import main as upload_main
            try:        # the uploader finds every run.json written since t0 under the data dir (one per device)
                rc = upload_main([str(s.data_dir), "--since", str(t0)])
                if rc:
                    note += f"; upload exit {rc}"
            except Exception as e:  # noqa: BLE001
                note += f"; upload failed: {str(e)[:120]}"
    return overall(verdicts), note


def run_once(queue: Queue, base: Settings, worker: str, upload: bool = True, log=print) -> dict | None:
    """Claim one due store and run it. Returns the monitor row that ran, or None when nothing was due."""
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
