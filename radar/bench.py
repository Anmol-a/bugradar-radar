"""Bench: run Radar across many Shopify stores and get ONE table back.

Purpose: prove Radar generalises across themes, checkout apps and store set-ups, and find the
places where Radar itself is wrong (false failures), in batches instead of one store at a time.

Store list format (one per line):
    https://store-a.com            # read-only: no add-to-cart (default for real merchants)
    https://store-b.com  cart      # also run the add-to-cart flow
    # comments and blank lines are ignored

Output: data/bench/<timestamp>/bench.html (interactive), bench.json, plus a console table.
Every store's full report stays at data/sites/<site_id>/runs/<run_id>/report.html.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from radar.core.config import Settings, site_id_from_url

SUITES = ["journey", "smoke", "catalog", "product", "cart", "search", "health"]
RANK = {"pass": 0, "skipped": 0, "blocked": 1, "flaky": 2, "confirmed_fail": 3}


def parse_store_list(text: str) -> list[tuple[str, bool]]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        out.append((parts[0], any(p.lower() == "cart" for p in parts[1:])))
    return out


def summarise(run: dict, report: str) -> dict:
    """One bench row from a run.json dict. Pure, unit-tested."""
    suites: dict[str, str] = {}
    failures, warns = [], 0
    for c in run.get("cases", []):
        v = c["verdict"]
        if RANK.get(v, 0) >= RANK.get(suites.get(c["suite"], "pass"), 0):
            suites[c["suite"]] = v
        last = c["attempts"][-1] if c["attempts"] else {}
        warns += sum(1 for s in last.get("steps", []) if s.get("status") == "warn")
        if v in ("confirmed_fail", "flaky", "blocked"):
            t = c.get("triage") or {}
            # a FLAKY case passed on its last attempt: show the attempt that failed (boat-lifestyle.com, bench 5
            # showed an empty row)
            bad = next((a for a in reversed(c["attempts"]) if not a.get("ok") and a.get("error")), last)
            failures.append({"case": c["case_id"], "verdict": v, "step": bad.get("failed_step"),
                             "error": (("failed once, passed on retry: " if v == "flaky" else "") + (bad.get("error") or ""))[:300],
                             "llm": f"{t['verdict'].replace('_', ' ')}: {t.get('reason', '')}"[:200] if t else ""})
    sm = run.get("sitemap_summary", {})
    return {"site_id": run.get("site_id"), "url": run.get("base_url"), "verdict": run.get("verdict"),
            "platform": sm.get("platform"), "theme": sm.get("theme"), "checkout": sm.get("checkout_app"),
            "access": sm.get("access"), "suites": suites, "failures": failures, "warnings": warns,
            "healed": len(run.get("healing_events", [])) + sum(bool(c.get("healed_after_triage")) for c in run.get("cases", [])),
            "radar_suspect": sum(1 for c in run.get("cases", []) if c["verdict"] == "confirmed_fail"
                                 and (c.get("triage") or {}).get("verdict") == "radar_problem"), "notes": run.get("notes", []), "report": report,
            "secs": _secs(run)}


def _secs(run: dict) -> int:
    try:
        a = datetime.fromisoformat(run["started_at"])
        b = datetime.fromisoformat(run["finished_at"])
        return int((b - a).total_seconds())
    except (KeyError, ValueError, TypeError):
        return 0


def _one(args) -> dict:
    """Runs in a worker process: one store, quiet."""
    url, cart, settings = args
    from radar.core.network import NO_NETWORK_NOTE, online
    from radar.runner.executor import scan
    s = replace(settings, allow_cart_flow=cart)
    t0 = time.time()
    if not online(s.net_probe_urls):      # Radar offline: do not even start (bench 8, 6 Oct)
        try:
            sid = site_id_from_url(url)
        except ValueError:
            sid = url
        return {"site_id": sid, "url": url, "verdict": "no_network", "platform": None, "theme": None,
                "checkout": None, "access": "no_network", "suites": {}, "failures": [], "warnings": 0, "healed": 0,
                "radar_suspect": 0, "notes": [NO_NETWORK_NOTE.format(where=" when this store's turn came")],
                "report": None, "secs": 0, "cart": cart, "input": url}
    try:
        run, run_dir = scan(url, s, "desktop")
        d = json.loads((run_dir / "run.json").read_text())
        row = summarise(d, str(run_dir / "report.html"))
    except Exception as e:  # noqa: BLE001  one store must never stop the bench
        try:
            sid = site_id_from_url(url)
        except ValueError:
            sid = url
        row = {"site_id": sid, "url": url, "verdict": "error", "platform": None, "theme": None, "checkout": None,
               "access": None, "suites": {}, "failures": [{"case": "radar", "verdict": "error", "step": "scan",
                                                           "error": f"{type(e).__name__}: {str(e)[:250]}"}],
               "warnings": 0, "healed": 0, "notes": [], "report": None, "secs": int(time.time() - t0)}
    row["cart"] = cart
    row["input"] = url
    return row


def run_bench(entries: list[tuple[str, bool]], settings: Settings, workers: int = 3, progress=print) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = settings.data_dir / "bench" / stamp
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    # spawn on every OS: same behaviour as macOS (where it is the default), no forked browser state
    with ProcessPoolExecutor(max_workers=max(1, workers), mp_context=multiprocessing.get_context("spawn")) as pool:
        futs = {pool.submit(_one, (u, c, settings)): u for u, c in entries}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            rows.append(r)
            fails = len([x for x in r["failures"] if x["verdict"] == "confirmed_fail"])
            progress(f"[{i}/{len(entries)}] {r['site_id'][:32]:32} {str(r['verdict']).upper():11} "
                     f"theme={str(r['theme'])[:18]:18} checkout={str(r['checkout'])[:16]:16} "
                     f"fails={fails} warns={r['warnings']} {r['secs']}s")
    order = {u: i for i, (u, _) in enumerate(entries)}
    rows.sort(key=lambda r: order.get(r["input"], 999))
    for r in rows:                                   # report links relative to the bench page
        if r.get("report"):
            r["report_rel"] = os.path.relpath(r["report"], out)
    payload = {"stamp": stamp, "rows": rows, "totals": totals(rows),
               "browser": {"ua_style": settings.ua_style, "identity": settings.user_agent,
                           "headless": settings.headless}}
    (out / "bench.json").write_text(json.dumps(payload, indent=2, default=str))
    from radar.reporting.bench_html import write_bench_html
    write_bench_html(payload, out / "bench.html")
    lost = payload["totals"].get("no_network", 0)
    if lost:
        progress(f"WARNING: Radar had no internet connection for {lost} of {len(rows)} stores. Those rows say "
                 "nothing about the stores; run the bench again on a stable connection.")
    return out


def totals(rows: list[dict]) -> dict:
    t = {"stores": len(rows)}
    for r in rows:
        t[r["verdict"]] = t.get(r["verdict"], 0) + 1
    t["tested"] = sum(1 for r in rows if r["verdict"] in ("healthy", "degraded", "down"))   # no_network never counts
    return t


def console_table(rows: list[dict]) -> str:
    head = f"{'store':30} {'verdict':11} {'theme':16} {'checkout':16} " + " ".join(f"{s[:7]:7}" for s in SUITES)
    sym = {"pass": "ok", "flaky": "FLAKY", "confirmed_fail": "FAIL", "blocked": "blk", "skipped": "-"}
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r['site_id'][:30]:30} {str(r['verdict'])[:11]:11} {str(r['theme'] or '-')[:16]:16} "
                     f"{str(r['checkout'] or '-')[:16]:16} " +
                     " ".join(f"{sym.get(r['suites'].get(s), '.') :7}" for s in SUITES))
    return "\n".join(lines)
