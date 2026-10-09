"""One scoreboard for one cloud run, merged from its parallel machines (shards).

  git fetch origin cloud-results && git worktree add /tmp/results origin/cloud-results   (or a clone of that branch)
  python3 tools/cloud_summary.py /tmp/results                 # newest run
  python3 tools/cloud_summary.py /tmp/results --run-id 3788... # a given GitHub run

Prints per store and device: verdict, failing / flaky / blocked tests with their error, and totals. Standard library only.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def folders(results: Path, run_id: str | None) -> list[Path]:
    metas = []
    for m in (results / "runs").glob("*/meta.json"):
        try:
            metas.append((m.parent, json.loads(m.read_text())))
        except ValueError:
            continue
    if not metas:
        return []
    if not run_id:
        run_id = max(metas, key=lambda x: x[1].get("published_at", ""))[1].get("run_id")
    return sorted(d for d, meta in metas if meta.get("run_id") == run_id)


def summary(results: Path, run_id: str | None = None) -> dict:
    dirs = folders(results, run_id)
    rows, totals = [], Counter()
    for d in dirs:
        for rj in sorted(d.glob("sites/*/*/run.json")):
            r = json.loads(rj.read_text())
            bad = [{"case": c["case_id"], "verdict": c["verdict"],
                    "step": (c.get("attempts") or [{}])[-1].get("failed_step"),
                    "error": ((c.get("attempts") or [{}])[-1].get("error") or "")[:160],
                    "triage": (c.get("triage") or {}).get("verdict")}
                   for c in r.get("cases", []) if c.get("verdict") not in ("pass",)]
            rows.append({"store": r.get("base_url"), "device": r.get("device"), "verdict": r.get("verdict"),
                         "notes": (r.get("notes") or [])[:2], "not_passing": bad,
                         "median_load_secs": (r.get("perf") or {}).get("median_load_secs")})
            totals[r.get("verdict")] += 1
    return {"run_id": run_id or (json.loads((dirs[0] / "meta.json").read_text()).get("run_id") if dirs else None),
            "parts": [d.name for d in dirs], "runs": len(rows), "verdicts": dict(totals), "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--run-id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    s = summary(Path(a.results), a.run_id)
    if a.json:
        print(json.dumps(s, indent=1))
        return 0
    print(f"run {s['run_id']}: {len(s['parts'])} parts, {s['runs']} store runs, {s['verdicts']}")
    for r in sorted(s["rows"], key=lambda x: (x["store"] or "", x["device"] or "")):
        print(f"{(r['store'] or '')[:42]:42} {r['device']:8} {r['verdict']:10} load {r['median_load_secs']}")
        for n in r["notes"]:
            if "rate-limit" in n or "crash" in n:
                print(f"    note: {n[:150]}")
        for b in r["not_passing"]:
            print(f"    {b['verdict']:15} {b['case'][:48]:48} {b['step'] or ''} | {b['error'][:110]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
