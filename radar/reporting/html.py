"""Interactive HTML reports. Self-contained files (no server, no CDN): open them from disk.

  data/sites/<site_id>/runs/<run_id>/report.html   one run: radar view, suites, steps, evidence
  data/sites/<site_id>/index.html                  run history + incidents for the site

Data is embedded as JSON and rendered by vanilla JS, so the same JSON can later feed the
Next.js dashboard unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from radar.reporting.template import RUN_TEMPLATE, INDEX_TEMPLATE


def _embed(obj) -> str:
    return json.dumps(obj, default=str).replace("</", "<\\/")


def write_run_report(run: dict, run_dir: Path, history: list[dict], incidents: list[dict]) -> Path:
    hist = [{"run_id": h["run_id"], "started_at": h["started_at"], "verdict": h["verdict"],
             "device": h["device"], "n_pass": h["n_pass"], "n_fail": h["n_fail"], "n_flaky": h["n_flaky"]}
            for h in history]
    payload = {"run": run, "history": hist, "incidents": incidents}
    out = run_dir / "report.html"
    out.write_text(RUN_TEMPLATE.replace("/*__DATA__*/null", _embed(payload)))
    return out


def write_site_index(site_id: str, storage) -> Path:
    site_dir = storage.site_dir(site_id)
    payload = {"site_id": site_id, "history": storage.history(site_id, 200),
               "incidents": storage.incidents(site_id), "sitemap": storage.load_json(site_id, "sitemap.json")}
    for h in payload["history"]:
        h["folder"] = f"runs/{h['run_id']}/report.html"
    out = site_dir / "index.html"
    out.write_text(INDEX_TEMPLATE.replace("/*__DATA__*/null", _embed(payload)))
    return out
