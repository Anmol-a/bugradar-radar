"""Bench: run Radar across many Shopify stores and get ONE table back.

Purpose: prove Radar generalises across themes, checkout apps and store set-ups, and find the
places where Radar itself is wrong (false failures), in batches instead of one store at a time.

Store list format (one per line):
    https://store-a.com            # read-only: no add-to-cart (default for real merchants)
    https://store-b.com  cart      # also run the add-to-cart flow
    # comments and blank lines are ignored

Every store is tested on desktop AND mobile (default since 7 Oct; --device desktop|mobile for one). Mobile is
skipped for a store desktop could not test (blocked, unreachable, offline): it is not asked a second time.
One bench row per store: `verdict` and `suites` are the WORST of the devices, `devices` has each device's own
verdict / suites / report / timings, and every failure says which device it happened on.

Output: data/bench/<timestamp>/bench.html (interactive), bench.json, plus a console table.
Every store's full report stays at data/sites/<site_id>/runs/<run_id>/report.html (one per device).
"""
from __future__ import annotations

import json
import multiprocessing
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from radar.core.browser import DEFAULT_DEVICES
from radar.core.config import Settings, site_id_from_url

SUITES = ["journey", "smoke", "catalog", "product", "cart", "search", "health", "info"]
RANK = {"pass": 0, "skipped": 0, "blocked": 1, "flaky": 2, "confirmed_fail": 3}
# worst-of for a store's verdict across devices. 'no_network' is highest on purpose: the row then says "run again"
# (the device that did run is still shown under `devices`).
VERDICT_RANK = {"healthy": 0, "degraded": 1, "down": 2, "unsupported": 3, "blocked": 3, "unreachable": 3,
                "error": 4, "stopped": 4, "no_network": 5}


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
            "secs": _secs(run), "device": run.get("device", "desktop"), "perf": run.get("perf") or {}}


def combine(per_device: dict[str, dict]) -> dict:
    """One bench row from the per-device rows of ONE store (each from summarise()). Pure, unit-tested.
    Store facts (theme, checkout, notes) come from the first device; verdict and suites are the worst of the
    devices; failures keep the device they happened on; `device_only` lists the test cases that failed
    (confirmed) on one device but not the other (the supplysix kind of finding: fine on mobile, no price on desktop)."""
    devs = list(per_device)
    first = per_device[devs[0]]
    verdict = max((r["verdict"] for r in per_device.values()), key=lambda v: VERDICT_RANK.get(v, 4))
    suites: dict[str, str] = {}
    for r in per_device.values():
        for k, v in r["suites"].items():
            if RANK.get(v, 0) >= RANK.get(suites.get(k, "pass"), 0):
                suites[k] = v
    failures = [dict(f, device=d) for d, r in per_device.items() for f in r["failures"]]
    failed = {d: {f["case"] for f in r["failures"] if f["verdict"] == "confirmed_fail"} for d, r in per_device.items()}
    device_only = {d: sorted(failed[d] - set().union(*(failed[o] for o in devs if o != d))) for d in devs} \
        if len(devs) > 1 else {}
    row = dict(first)
    row.update({
        "verdict": verdict, "suites": suites, "failures": failures,
        "suites_by_device": {d: r["suites"] for d, r in per_device.items()},
        "warnings": sum(r["warnings"] for r in per_device.values()),
        "healed": sum(r["healed"] for r in per_device.values()),
        "radar_suspect": sum(r["radar_suspect"] for r in per_device.values()),
        "secs": sum(r["secs"] for r in per_device.values()),
        "notes": list(dict.fromkeys(n for r in per_device.values() for n in r["notes"])),
        "device_only": device_only,
        "devices": {d: {"verdict": r["verdict"], "report": r["report"], "secs": r["secs"], "perf": r.get("perf") or {},
                        "failures": len(r["failures"]), "warnings": r["warnings"]} for d, r in per_device.items()}})
    row.pop("device", None)
    row.pop("perf", None)
    return row


def _secs(run: dict) -> int:
    try:
        a = datetime.fromisoformat(run["started_at"])
        b = datetime.fromisoformat(run["finished_at"])
        return int((b - a).total_seconds())
    except (KeyError, ValueError, TypeError):
        return 0


def _error_row(url: str, dev: str, step: str, error: str, secs: int, verdict: str = "error") -> dict:
    try:
        sid = site_id_from_url(url)
    except ValueError:
        sid = url
    return {"site_id": sid, "url": url, "verdict": verdict, "platform": None, "theme": None, "checkout": None,
            "access": None, "suites": {}, "failures": [{"case": "radar", "verdict": "error", "step": step,
                                                        "error": error[:300]}],
            "warnings": 0, "healed": 0, "radar_suspect": 0,
            "notes": [error[:300]] if verdict == "stopped" else [], "report": None,
            "secs": secs, "device": dev, "perf": {}}


def _one(args) -> dict:
    """Runs in a worker process: one store on every device, one after the other, quiet.
    With `workdir` (bench), each device's row is written to <workdir>/<device>.json as soon as it is done and
    <workdir>/now.json says which device is running since when: the parent stops a store that runs over its time
    budget and keeps the devices that did finish."""
    url, cart, settings, devices, *rest = args
    workdir = Path(rest[0]) if rest and rest[0] else None
    from radar.core.network import NO_NETWORK_NOTE, online
    from radar.runner.executor import TESTED, scan
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
                "report": None, "secs": 0, "cart": cart, "input": url, "devices": {}, "suites_by_device": {},
                "device_only": {}}
    per_device: dict[str, dict] = {}
    for dev in devices:
        if per_device and next(iter(per_device.values()))["verdict"] not in TESTED:
            break                           # desktop could not test this store: not asked again on mobile
        if workdir:
            (workdir / "now.json").write_text(json.dumps({"device": dev, "since": time.time()}))
        try:
            run, run_dir = scan(url, s, dev)
            d = json.loads((run_dir / "run.json").read_text())
            per_device[dev] = summarise(d, str(run_dir / "report.html"))
        except Exception as e:  # noqa: BLE001  one store must never stop the bench
            per_device[dev] = _error_row(url, dev, "scan", f"{type(e).__name__}: {str(e)[:250]}",
                                         int(time.time() - t0))
        if workdir:
            (workdir / f"{dev}.json").write_text(json.dumps(per_device[dev], default=str))
    row = combine(per_device)
    row["cart"] = cart
    row["input"] = url
    return row


STOPPED_NOTE = ("Radar stopped this store on {device} after {mins} min (time limit per store and device): the page "
                "stopped answering Radar (a script that never ends freezes the tab; a shopper's tab would freeze too). "
                "Look at this store by hand before calling it a store bug.")


def _child(args, workdir: str) -> None:
    """Process entry for one store (bench). Own process group, so the parent can stop the store's browser too."""
    try:
        os.setsid()
    except (AttributeError, OSError):
        pass
    row = _one(tuple(args) + (workdir,))
    (Path(workdir) / "row.json").write_text(json.dumps(row, default=str))


def _stop(proc) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)       # the store's Python process AND its browser
    except (AttributeError, OSError):
        proc.kill()
    proc.join(10)


def _stopped_row(url: str, cart: bool, devices: tuple[str, ...], workdir: Path, budget: int) -> dict:
    """Row for a store stopped over its time budget: the devices that finished keep their result."""
    per_device: dict[str, dict] = {}
    for dev in devices:
        f = workdir / f"{dev}.json"
        if f.exists():
            per_device[dev] = json.loads(f.read_text())
    try:
        now = json.loads((workdir / "now.json").read_text())
    except (OSError, ValueError):
        now = {"device": next((d for d in devices if d not in per_device), devices[0])}
    dev = now["device"]
    mins = max(1, round(budget / 60))
    per_device[dev] = _error_row(url, dev, "time limit", STOPPED_NOTE.format(device=dev, mins=mins), budget,
                                 verdict="stopped")
    row = combine(per_device)
    row["cart"] = cart
    row["input"] = url
    return row


def _run_all(entries, settings: Settings, workers: int, devices: tuple[str, ...], tmp: Path):
    """Yields one row per store as it finishes. Each store runs in its own process (spawn) with a time budget per
    device (settings.device_budget_s): over it, the store's process group (Python + browser) is killed and the
    row says 'stopped'. new30f (10 Oct): a frozen page held a GitHub shard for 2.5 h and its other stores' rows."""
    ctx = multiprocessing.get_context("spawn")
    budget = max(1, int(settings.device_budget_s))
    todo = list(enumerate(entries))
    running: dict[int, tuple] = {}
    try:
        while todo or running:
            while todo and len(running) < max(1, workers):
                i, (url, cart) = todo.pop(0)
                wd = tmp / f"s{i}"
                wd.mkdir(parents=True, exist_ok=True)
                p = ctx.Process(target=_child, args=((url, cart, settings, devices), str(wd)), daemon=False)
                p.start()
                running[i] = (p, url, cart, wd, time.time())
            time.sleep(0.5)
            for i, (p, url, cart, wd, t_start) in list(running.items()):
                if not p.is_alive():
                    p.join()
                    del running[i]
                    f = wd / "row.json"
                    if f.exists():
                        yield json.loads(f.read_text())
                    else:                                 # crashed without a row
                        per = {d: json.loads((wd / f"{d}.json").read_text()) for d in devices
                               if (wd / f"{d}.json").exists()}
                        if not per:
                            per = {devices[0]: _error_row(url, devices[0], "scan",
                                                          f"Radar's process for this store ended (exit code {p.exitcode})",
                                                          int(time.time() - t_start))}
                        row = combine(per)
                        row.update(cart=cart, input=url)
                        yield row
                    continue
                try:
                    since = json.loads((wd / "now.json").read_text())["since"]
                except (OSError, ValueError, KeyError):
                    since = t_start
                if time.time() - since > budget:
                    _stop(p)
                    del running[i]
                    yield _stopped_row(url, cart, devices, wd, budget)
    finally:                                          # interrupted (Ctrl+C): no orphan browsers
        for p, *_ in running.values():
            _stop(p)


def run_bench(entries: list[tuple[str, bool]], settings: Settings, workers: int = 3, progress=print,
              devices: tuple[str, ...] = DEFAULT_DEVICES) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = settings.data_dir / "bench" / stamp
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    # spawn on every OS: same behaviour as macOS (where it is the default), no forked browser state
    import tempfile
    with tempfile.TemporaryDirectory(prefix="radar-bench-") as tmp:
        for i, r in enumerate(_run_all(entries, settings, workers, tuple(devices), Path(tmp)), 1):
            rows.append(r)
            fails = len([x for x in r["failures"] if x["verdict"] == "confirmed_fail"])
            per = " ".join(f"{d[0].upper()}:{v['verdict']}" for d, v in r.get("devices", {}).items())
            progress(f"[{i}/{len(entries)}] {r['site_id'][:32]:32} {str(r['verdict']).upper():11} {per:30} "
                     f"theme={str(r['theme'])[:18]:18} checkout={str(r['checkout'])[:16]:16} "
                     f"fails={fails} warns={r['warnings']} {r['secs']}s")
    order = {u: i for i, (u, _) in enumerate(entries)}
    rows.sort(key=lambda r: order.get(r["input"], 999))
    for r in rows:                                   # report links relative to the bench page
        if r.get("report"):
            r["report_rel"] = os.path.relpath(r["report"], out)
        for dv in r.get("devices", {}).values():
            if dv.get("report"):
                dv["report_rel"] = os.path.relpath(dv["report"], out)
    payload = {"stamp": stamp, "rows": rows, "totals": totals(rows), "devices": list(devices),
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
    head = f"{'store':30} {'verdict':11} {'desktop':9} {'mobile':9} {'theme':16} {'checkout':16} " + \
        " ".join(f"{s[:7]:7}" for s in SUITES)
    sym = {"pass": "ok", "flaky": "FLAKY", "confirmed_fail": "FAIL", "blocked": "blk", "skipped": "-"}
    lines = [head, "-" * len(head)]
    for r in rows:
        dv = r.get("devices") or {}
        lines.append(f"{r['site_id'][:30]:30} {str(r['verdict'])[:11]:11} "
                     f"{str((dv.get('desktop') or {}).get('verdict', '-'))[:9]:9} "
                     f"{str((dv.get('mobile') or {}).get('verdict', '-'))[:9]:9} {str(r['theme'] or '-')[:16]:16} "
                     f"{str(r['checkout'] or '-')[:16]:16} " +
                     " ".join(f"{sym.get(r['suites'].get(s), '.') :7}" for s in SUITES))
    return "\n".join(lines)
