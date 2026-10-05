"""Per-site storage. Answers 'where does xyz.in's data live vs abc.com's?'

One SQLite database (data/radar.db), every row keyed by site_id. Same schema moves to
Supabase Postgres later, with row-level security on site_id.

Files, one folder per site:
  data/sites/<site_id>/sitemap.json            latest discovery result
  data/sites/<site_id>/suites.json             latest generated suites
  data/sites/<site_id>/runs/<run_id>/          run.json, report.html, screenshots, traces
  data/sites/<site_id>/index.html              run history page for that site
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  site_id TEXT PRIMARY KEY, base_url TEXT, platform TEXT, first_seen TEXT, last_run TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, site_id TEXT, started_at TEXT, finished_at TEXT, device TEXT,
  verdict TEXT, n_pass INT, n_flaky INT, n_fail INT, n_blocked INT, n_skipped INT, folder TEXT
);
CREATE INDEX IF NOT EXISTS runs_site ON runs(site_id, started_at);
CREATE TABLE IF NOT EXISTS case_results (
  run_id TEXT, site_id TEXT, case_id TEXT, suite TEXT, title TEXT, severity TEXT, verdict TEXT,
  attempts INT, failed_step TEXT, error TEXT, PRIMARY KEY (run_id, case_id)
);
CREATE TABLE IF NOT EXISTS incidents (
  signature TEXT PRIMARY KEY, site_id TEXT, case_id TEXT, first_seen TEXT, last_seen TEXT,
  occurrences INT, status TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS locator_cache (
  site_id TEXT, intent TEXT, selector TEXT, method TEXT, updated_at TEXT, hits INT DEFAULT 0,
  PRIMARY KEY (site_id, intent)
);
CREATE TABLE IF NOT EXISTS healing_events (
  site_id TEXT, run_id TEXT, intent TEXT, old_selectors TEXT, new_selector TEXT, method TEXT, at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "radar.db", timeout=60)   # bench runs stores in parallel
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---------- folders ----------
    def site_dir(self, site_id: str) -> Path:
        d = self.data_dir / "sites" / site_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_dir(self, site_id: str, run_id: str) -> Path:
        d = self.site_dir(site_id) / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_json(self, site_id: str, name: str, obj) -> Path:
        p = self.site_dir(site_id) / name
        p.write_text(json.dumps(obj, indent=2, default=str))
        return p

    def load_json(self, site_id: str, name: str):
        p = self.site_dir(site_id) / name
        return json.loads(p.read_text()) if p.exists() else None

    # ---------- sites / runs ----------
    def upsert_site(self, site_id: str, base_url: str, platform: str):
        self.db.execute(
            "INSERT INTO sites VALUES (?,?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET "
            "base_url=excluded.base_url, platform=excluded.platform, last_run=excluded.last_run",
            (site_id, base_url, platform, _now(), _now()))
        self.db.commit()

    def save_run(self, run: dict, folder: Path):
        c = run["counts"]
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["run_id"], run["site_id"], run["started_at"], run["finished_at"], run["device"],
             run["verdict"], c["pass"], c["flaky"], c["confirmed_fail"], c["blocked"], c["skipped"],
             str(folder)))
        for cr in run["cases"]:
            last = cr["attempts"][-1] if cr["attempts"] else {}
            self.db.execute(
                "INSERT OR REPLACE INTO case_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run["run_id"], run["site_id"], cr["case_id"], cr["suite"], cr["title"], cr["severity"],
                 cr["verdict"], len(cr["attempts"]), last.get("failed_step"), last.get("error")))
            if cr.get("incident_signature"):
                self._touch_incident(cr["incident_signature"], run["site_id"], cr["case_id"], last.get("error"))
        self.db.commit()

    def _touch_incident(self, sig, site_id, case_id, err):
        row = self.db.execute("SELECT occurrences FROM incidents WHERE signature=?", (sig,)).fetchone()
        if row:
            self.db.execute("UPDATE incidents SET last_seen=?, occurrences=occurrences+1, status='open', "
                            "last_error=? WHERE signature=?", (_now(), err, sig))
        else:
            self.db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                            (sig, site_id, case_id, _now(), _now(), 1, "open", err))

    def resolve_missing_incidents(self, site_id: str, still_failing: set[str]):
        """Any open incident for this site that did not fail this run is marked resolved."""
        rows = self.db.execute("SELECT signature FROM incidents WHERE site_id=? AND status='open'", (site_id,))
        for (sig,) in rows.fetchall():
            if sig not in still_failing:
                self.db.execute("UPDATE incidents SET status='resolved' WHERE signature=?", (sig,))
        self.db.commit()

    def incidents(self, site_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM incidents WHERE site_id=? ORDER BY last_seen DESC", (site_id,))]

    def history(self, site_id: str, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM runs WHERE site_id=? ORDER BY started_at DESC LIMIT ?", (site_id, limit))]

    def sites(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM sites ORDER BY last_run DESC")]

    # ---------- locator cache (self-healing memory) ----------
    def cached_locator(self, site_id: str, intent: str) -> str | None:
        r = self.db.execute("SELECT selector FROM locator_cache WHERE site_id=? AND intent=?",
                            (site_id, intent)).fetchone()
        return r["selector"] if r else None

    def cache_locator(self, site_id: str, intent: str, selector: str, method: str):
        self.db.execute(
            "INSERT INTO locator_cache VALUES (?,?,?,?,?,1) ON CONFLICT(site_id,intent) DO UPDATE SET "
            "selector=excluded.selector, method=excluded.method, updated_at=excluded.updated_at, hits=hits+1",
            (site_id, intent, selector, method, _now()))
        self.db.commit()

    def drop_locator(self, site_id: str, intent: str):
        self.db.execute("DELETE FROM locator_cache WHERE site_id=? AND intent=?", (site_id, intent))
        self.db.commit()

    def log_healing(self, site_id, run_id, intent, old, new, method):
        self.db.execute("INSERT INTO healing_events VALUES (?,?,?,?,?,?,?)",
                        (site_id, run_id, intent, json.dumps(old), new, method, _now()))
        self.db.commit()

    # ---------- retention ----------
    def prune(self, site_id: str, pass_days: int, fail_days: int) -> int:
        """Delete screenshots/traces of old runs. Passing runs: after pass_days.
        Runs with failures: after fail_days. run.json and DB rows are kept (they are small)."""
        removed = 0
        now = datetime.now(timezone.utc)
        for r in self.history(site_id, limit=10_000):
            age = now - datetime.fromisoformat(r["started_at"])
            keep = timedelta(days=fail_days if (r["n_fail"] or 0) > 0 else pass_days)
            folder = Path(r["folder"])
            if age > keep and folder.exists():
                for f in folder.iterdir():
                    if f.suffix in (".png", ".jpg", ".zip"):
                        f.unlink()
                        removed += 1
        return removed

    def close(self):
        self.db.close()
