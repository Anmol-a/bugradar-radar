"""Radar runner: python runner.py sites/vaaree.yml [--device desktop|mobile] [--headed]

One site, one journey run (with confirm-retry), evidence saved locally.
"""
import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from radar.config import load_site
from radar.confirm import Attempt, should_retry, verdict, signature
from radar.journeys.shopify import run_journey

ROOT = Path(__file__).parent


def make_context(browser, cfg, device: str, pw):
    kwargs = dict(
        user_agent=cfg.access.user_agent,
        locale=cfg.access.locale,
        timezone_id=cfg.access.timezone,
    )
    if device == "mobile":
        d = pw.devices["Pixel 7"]
        kwargs.update(viewport=d["viewport"], is_mobile=True, has_touch=True, device_scale_factor=d["device_scale_factor"])
    else:
        kwargs.update(viewport={"width": 1366, "height": 800})
    return browser.new_context(**kwargs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site_config")
    ap.add_argument("--device", default="desktop", choices=["desktop", "mobile"])
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    cfg = load_site(args.site_config)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    day = run_id[:8]
    evid = ROOT / "evidence" / cfg.site_id / day / run_id
    evid.mkdir(parents=True, exist_ok=True)

    attempts: list[Attempt] = []
    unsupported = False
    attempt_logs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        while True:
            n = len(attempts) + 1
            ctx = make_context(browser, cfg, args.device, pw)   # fresh context per attempt
            ctx.tracing.start(screenshots=True, snapshots=True)
            page = ctx.new_page()
            res = run_journey(page, cfg)
            if not res.ok:
                page.screenshot(path=str(evid / f"attempt{n}_fail.png"), full_page=True)
                ctx.tracing.stop(path=str(evid / f"attempt{n}_trace.zip"))   # trace kept for failures only
            else:
                ctx.tracing.stop()
            ctx.close()
            attempts.append(Attempt(res.ok, res.failed_step, res.error))
            if res.unsupported:        # not a Radar-supported platform: retrying is pointless
                unsupported = True
                attempt_logs.append({"attempt": n, "ok": False, "failed_step": res.failed_step, "error": res.error, "steps": res.steps})
                break
            attempt_logs.append({"attempt": n, "ok": res.ok, "failed_step": res.failed_step, "error": res.error, "steps": res.steps})
            if not should_retry(attempts):
                break
        browser.close()

    v = "unsupported" if unsupported else verdict(attempts)
    failed = next((a.failed_step for a in reversed(attempts) if not a.ok), None)
    summary = {
        "run_id": run_id, "site_id": cfg.site_id, "device": args.device, "verdict": v,
        "incident_signature": signature(cfg.site_id, "buy_journey", failed) if v == "confirmed_fail" else None,
        "attempts": attempt_logs,
    }
    (evid / "run.json").write_text(json.dumps(summary, indent=2))
    print(f"[{v.upper()}] {cfg.site_id} ({args.device}) -> {evid}")
    for a in attempt_logs:
        print(f"  attempt {a['attempt']}: {'ok' if a['ok'] else 'FAILED at ' + str(a['failed_step'])}")
        if not a["ok"]:
            print(f"    reason: {a['error']}")
        for st in a["steps"]:
            if st["status"] == "warn":
                print(f"    warning: {st['name']}: {st['error']}")


if __name__ == "__main__":
    main()
