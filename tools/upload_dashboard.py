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
SHOT_WIDTH = 900
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


def smaller(p: Path) -> tuple[str, str, bytes] | None:
    """A full-page PNG as a JPEG no wider than SHOT_WIDTH: about a fifth of the size, still readable. Needs Pillow."""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(p) as im:
            im = im.convert("RGB")
            if im.width > SHOT_WIDTH:
                im = im.resize((SHOT_WIDTH, round(im.height * SHOT_WIDTH / im.width)))
            buf = BytesIO()
            im.save(buf, "JPEG", quality=72, optimize=True)
        return p.with_suffix(".jpg").name, "image/jpeg", buf.getvalue()
    except Exception:          # no Pillow on this machine, or an unreadable file: the caller falls back to the file as it is
        return None


def evidence(case: dict, run_dir: Path) -> tuple[str, str, bytes] | None:
    """The picture for a failed check, as (name, content type, bytes): the small photo of the step that failed; else the
    full-page one, shrunk to a JPEG when Pillow is there, or as it is when it is under the size limit.
    (13 Oct: phone full-page PNGs are 0.6-1.5 MB, over the limit, so phone failures arrived with no picture at all.)"""
    last = (case.get("attempts") or [{}])[-1]
    steps = last.get("steps") or []
    step = next((s.get("shot") for s in steps if s.get("name") == last.get("failed_step") and s.get("shot")), None) \
        or next((s.get("shot") for s in reversed(steps) if s.get("shot")), None)
    for name in (step, last.get("screenshot")):
        p = run_dir / name if name else None
        if not (p and p.is_file() and p.stat().st_size):
            continue
        if p.suffix.lower() == ".png":
            small = smaller(p)
            if small and len(small[2]) <= MAX_SHOT_BYTES:
                return small
        if p.stat().st_size <= MAX_SHOT_BYTES:
            return p.name, "image/png" if p.suffix.lower() == ".png" else "image/jpeg", p.read_bytes()
    return None


# where a failed check stopped, for the "Open this page" link on the dashboard
PRIVATE_PAGES = ("/cart", "/checkout", "/checkouts", "/account")     # different for every visitor: never linked
NO_PAGE = {"not_found": "test_address"}         # checks that ask for a made-up address: there is no real page to open
PATH_IN_STEP = re.compile(r"(?:^|\s)(/[A-Za-z0-9][^\s]{1,200})")


def failing_page(case: dict, base_url: str) -> tuple[str | None, str | None]:
    """(link, note) for a failed check. The link is always on the store's own address. Order: a page named by a step that
    failed or warned (link checks name the broken link there), else the page the last try ended on. A cart / checkout /
    account page is private to each visitor, so the last product page is linked instead (note 'cart')."""
    if case.get("check") in NO_PAGE:
        return None, NO_PAGE[case["check"]]
    base = urlparse(base_url or "")
    if not base.hostname:
        return None, None
    last = (case.get("attempts") or [{}])[-1]

    def own(u: str | None) -> str | None:
        if not u:
            return None
        q = urlparse(u if "://" in u else f"{base.scheme}://{base.netloc}{u if u.startswith('/') else '/' + u}")
        if q.scheme not in ("http", "https") or (q.hostname or "").removeprefix("www.") != base.hostname.removeprefix("www."):
            return None
        return f"{q.scheme}://{q.netloc}{q.path or '/'}{('?' + q.query) if q.query and len(q.query) < 120 else ''}"[:400]

    private = lambda u: urlparse(u).path.rstrip("/").startswith(PRIVATE_PAGES)       # noqa: E731
    named = [m.group(1) for st in last.get("steps") or [] if st.get("status") in ("fail", "warn")
             for m in [PATH_IN_STEP.search(str(st.get("name") or ""))] if m]
    loads = [l.get("url") for l in last.get("loads") or [] if l.get("url")]
    for cand in [*named, *reversed(loads)]:
        u = own(cand)
        if not u:
            continue
        if private(u):
            product = next((own(x) for x in reversed(loads) if "/products/" in str(x)), None)
            return (product, "cart") if product else (None, "cart")
        # foxtale.in, 13 Oct: the check is named after the first product, but its page was hidden and the next catalog
        # product was tested: the link is that product, so say so (note 'next_product')
        moved = any(str(st.get("name") or "").startswith("loads_next_catalog_product_") and st.get("status") == "pass"
                    for st in last.get("steps") or [])
        return u, "next_product" if moved and cand in loads else None
    return None, None


def steps_of(case: dict) -> list[dict] | None:
    """The steps of the last try, in order, for the 'what Radar did' line (names only; long lists are cut)."""
    steps = [{"name": str(st.get("name") or "")[:70], "status": st.get("status")} for st in ((case.get("attempts") or [{}])[-1].get("steps") or [])
             if st.get("name") and st.get("status") in ("pass", "warn", "fail")]
    return steps[-8:] or None


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


def to_payload(run: dict, run_dir: Path, build: str, ci_url: str | None, extra_meta: dict | None = None) -> dict:
    host = (urlparse(run.get("base_url") or "").hostname or run.get("site_id") or "store").removeprefix("www.")
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
        if status in ("passed", "flaky") and any(s.get("name") == "sold_out_product" for s in last.get("steps") or []):
            # the product page is fine, the product just is not in stock: a tag, not a failure and not a warning
            item["tags"] = ["sold out"]
            item["reason"] = "This product is sold out, so the buy steps were not tested."
        if status == "failed":
            item["page_url"], item["page_note"] = failing_page(c, run.get("base_url") or "")
            item["steps"] = steps_of(c)
        if status == "failed" and shots < MAX_SHOTS_PER_RUN:
            shot = evidence(c, run_dir)
            if shot:
                item["artifacts"] = [{"kind": "screenshot", "name": shot[0], "content_type": shot[1],
                                      "data_base64": base64.b64encode(shot[2]).decode()}]
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
        if site["status"] == "failed" and (extra_meta or {}).get("rechecked"):
            # a daily check's second look (radar/worker.py RECHECK, 21 Oct): the owner should know it wasn't a one-off
            site["reason"] = f"{(site['reason'] or 'The store did not load').rstrip(' .')}. Checked twice, 10 minutes apart."
    # the free check's "your store as Radar saw it" (19 Oct): worker.homepage_shot saves it next to run.json
    home = run_dir / "homepage.jpg"
    if home.is_file() and 0 < home.stat().st_size <= MAX_SHOT_BYTES:
        site["artifacts"] = [{"kind": "screenshot", "name": "homepage.jpg", "content_type": "image/jpeg",
                              "data_base64": base64.b64encode(home.read_bytes()).decode()}]
    results.insert(0, site)
    perf = run.get("perf") or {}
    return {
        "project": host, "project_url": run.get("base_url"),
        "build": {"version": build or build_day(run.get("started_at")), "environment": "production"},
        "run": {"tool": "radar", "language": "python", "framework": "radar", "device": run.get("device"),
                "browser": "chromium", "started_at": run.get("started_at"), "finished_at": run.get("finished_at") or None,
                "ci_url": ci_url, "meta": {**(extra_meta or {}), "radar_run_id": run.get("run_id"), "radar_verdict": run.get("verdict"),
                                           "median_load_secs": perf.get("median_load_secs"), "notes": (run.get("notes") or [])[:3]}},
        "results": results,
    }


def send(payload: dict, key: str) -> dict:
    req = urllib.request.Request(INGEST_URL, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def find_runs(folder: Path, since: float = 0) -> list[Path]:
    """Every finished run.json under the folder (written after `since`), in path order."""
    # two layouts: the engine's own data folder (sites/<store>/runs/<run>/run.json, what a GitHub machine has) and the
    # published results (sites/<store>/<run>/run.json). 13 Oct: only the second was looked for, so nothing was ever sent
    files = [folder / "run.json"] if (folder / "run.json").exists() else sorted(
        set(folder.glob("**/sites/*/runs/*/run.json")) | set(folder.glob("**/sites/*/*/run.json")))
    return [f for f in files if f.stat().st_mtime >= since]


def upload(folder: Path, key: str, since: float = 0, build: str = "", ci_url: str | None = None,
           dry_run: bool = False, log=print, extra_meta: dict | None = None) -> list[dict]:
    """Send every finished run under the folder. Returns one record per run.json found:
    {"file", "project", "device", "radar_run_id", "ok", "run_id", "project_id", "error"}; the worker (15 Oct) reads
    run_id off it to tie a free-scan request to its dashboard run."""
    out: list[dict] = []
    for f in find_runs(folder, since):
        try:
            run = json.loads(f.read_text())
        except ValueError:
            continue
        if not run.get("cases") and not run.get("verdict"):
            continue                       # not a finished run
        payload = to_payload(run, f.parent, build, ci_url, extra_meta)
        rec = {"file": str(f), "project": payload["project"], "device": payload["run"]["device"],
               "radar_run_id": run.get("run_id"), "ok": False, "run_id": None, "project_id": None, "error": None}
        out.append(rec)
        if dry_run:
            log(json.dumps({**payload, "results": f"{len(payload['results'])} results"}, default=str)[:400])
            rec["ok"] = True
            continue
        try:
            ans = send(payload, key)
            rec.update(ok=True, run_id=ans.get("run_id"), project_id=ans.get("project_id"))
            log(f"sent {payload['project']} {payload['run']['device']}: {ans.get('results')} results, "
                f"{ans.get('incidents_opened', 0)} new problems, {ans.get('incidents_resolved', 0)} fixed")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            rec["error"] = str(e)[:300]
            log(f"could not send {payload['project']} {payload['run']['device']}: {e}")
    return out


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
    recs = upload(a.folder, key, a.since, a.build, a.ci_url, a.dry_run,
                  log=lambda m: print(m, file=sys.stderr if m.startswith("could not send") else sys.stdout))
    sent, failed = sum(r["ok"] for r in recs), sum(not r["ok"] for r in recs)
    print(f"dashboard upload: {sent} sent, {failed} failed, {len(recs)} found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
