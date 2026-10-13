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
# a build is one day of results; the day is counted where the store's shoppers are (a 5 am India run belongs to that
# day, not to the UTC day before). BUGRADAR_TZ overrides.
TZ = os.environ.get("BUGRADAR_TZ", "Asia/Kolkata")
# the triage label comes from an LLM, so the dashboard says so in plain words instead of presenting it as fact
TRIAGE_NOTE = {"radar_problem": "this may be the test's fault, not the store's", "unsure": "not yet confirmed as a store problem"}
# a run with no checks at all still has something to say: the store could not be opened or tested
SITE_KEY, SITE_NAME = "site.reachable", "Store opens and can be tested"
SITE_STATUS = {"unreachable": "failed", "blocked": "blocked"}          # anything else without checks: skipped
SITE_WHY = {"unsupported": "This store does not run on Shopify. Radar tests Shopify stores only for now."}


def build_day(iso: str | None) -> str:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(ZoneInfo(TZ)).date().isoformat()
    except (ValueError, KeyError):
        return (iso or "")[:10]


def plain_name(title: str) -> str:
    """Check names without shop-talk a store owner would not know."""
    return re.sub(r"^PDP\b", "Product page", title)


def evidence(case: dict, run_dir: Path) -> Path | None:
    """The picture for a failed check: the small photo of the step that failed, else the full-page one if it is not huge.
    (13 Oct: phone full-page PNGs are 0.6-1.5 MB, over the limit, so phone failures arrived with no picture at all.)"""
    last = (case.get("attempts") or [{}])[-1]
    steps = last.get("steps") or []
    step = next((s.get("shot") for s in steps if s.get("name") == last.get("failed_step") and s.get("shot")), None) \
        or next((s.get("shot") for s in reversed(steps) if s.get("shot")), None)
    for name in (step, last.get("screenshot")):
        p = run_dir / name if name else None
        if p and p.is_file() and 0 < p.stat().st_size <= MAX_SHOT_BYTES:
            return p
    return None


def reason_of(case: dict) -> str | None:
    """One plain sentence on why a check did not pass."""
    tri = case.get("triage") or {}
    if case.get("verdict") == "confirmed_fail" and tri.get("reason"):
        note = TRIAGE_NOTE.get(tri.get("verdict"))
        text = str(tri["reason"])[:300].rstrip(" .")
        return f"{text} ({note})." if note else text + "."
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
            "key": c["case_id"], "name": plain_name(c.get("title") or c["case_id"]), "suite": c.get("suite"),
            "severity": c.get("severity") or "major", "status": status, "reason": reason_of(c),
            "error": (last.get("error") or None) and str(last["error"])[:4000],
            "duration_ms": int(sum(a.get("secs") or 0 for a in c.get("attempts") or []) * 1000) or None,
            "attempts": len(c.get("attempts") or []) or 1,
        }
        if status == "failed" and shots < MAX_SHOTS_PER_RUN:
            p = evidence(c, run_dir)
            if p:
                ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                item["artifacts"] = [{"kind": "screenshot", "name": p.name, "content_type": ctype,
                                      "data_base64": base64.b64encode(p.read_bytes()).decode()}]
                shots += 1
        results.append(item)
    # one line for the store itself, in every run: it fails when the store cannot be opened, is "not tested" when Radar
    # is not allowed in or the platform is not supported, and passes otherwise (so a store coming back closes the problem)
    note = str((run.get("notes") or [""])[0])[:300] or None
    site = {"key": SITE_KEY, "name": SITE_NAME, "suite": "smoke", "severity": "critical", "attempts": 1, "error": None,
            "duration_ms": None, "status": "passed", "reason": None}
    if not results:
        # the plain part is the reason; a long technical tail in brackets ("(robots.txt request: ...)") goes under "Technical detail"
        plain = re.sub(r"\s*\([^()]*:[^()]{20,}\)\s*$", "", note or "") or note
        site.update(status=SITE_STATUS.get(run.get("verdict"), "skipped"), reason=SITE_WHY.get(run.get("verdict")) or plain,
                    error=note if plain != note else None)
    results.insert(0, site)
    perf = run.get("perf") or {}
    return {
        "project": host, "project_url": run.get("base_url"),
        "build": {"version": build or build_day(run.get("started_at")), "environment": "production"},
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
    ap.add_argument("--since", type=float, default=0, help="only runs written after this unix time (machines that keep old data)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    key = os.environ.get("BUGRADAR_KEY", "")
    if not a.dry_run and not key.startswith("brk_"):
        print("BUGRADAR_KEY is not set; nothing sent.")
        return 0                       # never fail a Radar run because the dashboard isn't set up
    # two layouts: the engine's own data folder (sites/<store>/runs/<run>/run.json, what a GitHub machine has) and the
    # published results (sites/<store>/<run>/run.json). 13 Oct: only the second was looked for, so nothing was ever sent
    files = [a.folder / "run.json"] if (a.folder / "run.json").exists() else sorted(
        set(a.folder.glob("**/sites/*/runs/*/run.json")) | set(a.folder.glob("**/sites/*/*/run.json")))
    sent = failed = 0
    files = [f for f in files if f.stat().st_mtime >= a.since]
    for f in files:
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if not run.get("cases") and not run.get("verdict"):
            continue                       # not a finished run
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
