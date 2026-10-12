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
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

INGEST_URL = os.environ.get("BUGRADAR_INGEST_URL", "https://onijwpgvpojpvgumvfim.supabase.co/functions/v1/ingest")
STATUS = {"pass": "passed", "flaky": "flaky", "confirmed_fail": "failed", "fail": "failed", "blocked": "blocked",
          "skipped": "skipped", "no_network": "skipped", "pending": "skipped"}
MAX_SHOTS_PER_RUN = 6
MAX_SHOT_BYTES = 600_000


def reason_of(case: dict) -> str | None:
    """One plain sentence on why a check did not pass."""
    tri = case.get("triage") or {}
    if case.get("verdict") == "confirmed_fail" and tri.get("reason"):
        return str(tri["reason"])[:300]
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
    for st in last.get("steps") or []:
        if st.get("name") == step and (st.get("detail") or st.get("error")):
            return str(st.get("detail") or st.get("error"))[:300]
    return (last.get("error") or "")[:300] or None


def to_payload(run: dict, run_dir: Path, build: str, ci_url: str | None) -> dict:
    host = urlparse(run.get("base_url") or "").hostname or run.get("site_id") or "store"
    results, shots = [], 0
    for c in run.get("cases", []):
        status = STATUS.get(c.get("verdict"), "warning")
        last = (c.get("attempts") or [{}])[-1]
        item = {
            "key": c["case_id"], "name": c.get("title") or c["case_id"], "suite": c.get("suite"),
            "severity": c.get("severity") or "major", "status": status, "reason": reason_of(c),
            "error": (last.get("error") or None) and str(last["error"])[:4000],
            "duration_ms": int(sum(a.get("secs") or 0 for a in c.get("attempts") or []) * 1000) or None,
            "attempts": len(c.get("attempts") or []) or 1,
        }
        if status == "failed" and shots < MAX_SHOTS_PER_RUN:
            name = last.get("screenshot") or next((s.get("shot") for s in reversed(last.get("steps") or []) if s.get("shot")), None)
            p = run_dir / name if name else None
            if p and p.is_file() and p.stat().st_size <= MAX_SHOT_BYTES:
                ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                item["artifacts"] = [{"kind": "screenshot", "name": p.name, "content_type": ctype,
                                      "data_base64": base64.b64encode(p.read_bytes()).decode()}]
                shots += 1
        results.append(item)
    perf = run.get("perf") or {}
    return {
        "project": host, "project_url": run.get("base_url"),
        "build": {"version": build or (run.get("started_at") or "")[:10], "environment": "production"},
        "run": {"tool": "radar", "language": "python", "framework": "radar", "device": run.get("device"),
                "browser": "chromium", "started_at": run.get("started_at"), "finished_at": run.get("finished_at") or None,
                "ci_url": ci_url, "meta": {"radar_run_id": run.get("run_id"), "radar_verdict": run.get("verdict"),
                                           "median_load_secs": perf.get("median_load_secs"), "notes": (run.get("notes") or [])[:3]}},
        "results": results,
    }


def send(payload: dict, key: str) -> dict:
    req = urllib.request.Request(INGEST_URL, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--build", default="", help="build label; default: the day the run started")
    ap.add_argument("--ci-url", default=os.environ.get("BUGRADAR_CI_URL"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    key = os.environ.get("BUGRADAR_KEY", "")
    if not a.dry_run and not key.startswith("brk_"):
        print("BUGRADAR_KEY is not set; nothing sent.")
        return 0                       # never fail a Radar run because the dashboard isn't set up
    files = [a.folder / "run.json"] if (a.folder / "run.json").exists() else sorted(a.folder.glob("**/sites/*/*/run.json"))
    sent = failed = 0
    for f in files:
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if not run.get("cases"):
            continue
        payload = to_payload(run, f.parent, a.build, a.ci_url)
        if a.dry_run:
            print(json.dumps({**payload, "results": f"{len(payload['results'])} results"}, default=str)[:400])
            continue
        try:
            out = send(payload, key)
            sent += 1
            print(f"sent {payload['project']} {payload['run']['device']}: {out.get('results')} results, "
                  f"{out.get('incidents_opened', 0)} new problems, {out.get('incidents_resolved', 0)} fixed")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            failed += 1
            print(f"could not send {payload['project']} {payload['run']['device']}: {e}", file=sys.stderr)
    print(f"dashboard upload: {sent} sent, {failed} failed, {len(files)} found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
