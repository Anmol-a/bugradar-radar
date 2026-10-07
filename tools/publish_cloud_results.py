"""Copy the small part of a cloud run into the `cloud-results` branch folder.

  python3 tools/publish_cloud_results.py <results folder> --data data --log bench.log --request "bench ..."

Writes <results>/runs/<bench stamp>/ with bench.json, bench.html, bench.log, meta.json and, for every store and
device in the bench, sites/<site_id>/<run_id>/run.json plus its failure screenshots (*.png; the per-step journey
JPEGs and the traces stay in the Actions artifact). <results>/LATEST = that stamp. When Radar produced no bench
(crash, refused request) the folder is runs/<time>-no-bench/ with the log and meta, so a failed run is visible too.
Standard library only; never raises on a missing file (a broken run must still publish its log).
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

LOG_TAIL = 400_000      # bytes of the console log kept


def newest_bench(data: Path) -> Path | None:
    benches = sorted((p for p in (data / "bench").glob("*") if p.is_dir()), key=lambda p: p.name)
    return benches[-1] if benches else None


def publish(out: Path, data: Path, log: Path | None, meta: dict) -> Path:
    bench = newest_bench(data)
    stamp = bench.name if bench and (bench / "bench.json").exists() else \
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-no-bench"
    dest = out / "runs" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    if bench and (bench / "bench.json").exists():
        for f in ("bench.json", "bench.html"):
            if (bench / f).exists():
                shutil.copy2(bench / f, dest / f)
        payload = json.loads((bench / "bench.json").read_text())
        meta["totals"] = payload.get("totals")
        for row in payload.get("rows", []):
            for dv in (row.get("devices") or {}).values():
                rep = dv.get("report")
                run_dir = Path(rep).parent if rep else None
                if not run_dir or not (run_dir / "run.json").exists():
                    continue
                tgt = dest / "sites" / row["site_id"] / run_dir.name
                tgt.mkdir(parents=True, exist_ok=True)
                shutil.copy2(run_dir / "run.json", tgt / "run.json")
                for png in run_dir.glob("*.png"):          # failure screenshots only
                    shutil.copy2(png, tgt / png.name)
                copied += 1
    meta["runs_copied"] = copied
    if log and log.exists():
        data_bytes = log.read_bytes()
        (dest / "bench.log").write_bytes(data_bytes[-LOG_TAIL:])
    (dest / "meta.json").write_text(json.dumps(meta, indent=2))
    (out / "LATEST").write_text(stamp + "\n")
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--data", default="data")
    ap.add_argument("--log")
    ap.add_argument("--event", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--sha", default="")
    ap.add_argument("--request", default="")
    a = ap.parse_args(argv)
    meta = {"event": a.event, "run_id": a.run_id, "sha": a.sha, "request": a.request,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    dest = publish(Path(a.out), Path(a.data), Path(a.log) if a.log else None, meta)
    print(f"published {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
