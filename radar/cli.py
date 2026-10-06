"""Command line.

  python3 -m radar scan moxiebeauty.in              discover, generate, test, report
  python3 -m radar scan moxiebeauty.in --device both --headed
  python3 -m radar scan moxiebeauty.in --suites smoke,cart
  python3 -m radar bench stores/bench.txt           many stores, one table (see radar/bench.py)
  python3 -m radar sites                            every site scanned so far
  python3 -m radar open moxiebeauty.in              open the latest report
"""
from __future__ import annotations

import argparse
import sys
import webbrowser
from dataclasses import replace

from radar.core.config import load_settings, site_id_from_url
from radar.core.storage import Storage
from radar.runner.executor import scan

C = {"pass": "\033[32m", "fail": "\033[31m", "warn": "\033[33m", "dim": "\033[2m", "b": "\033[1m", "x": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}

VERDICT_COLOR = {"healthy": "pass", "degraded": "warn", "down": "fail", "unsupported": "warn", "blocked": "warn",
                 "unreachable": "warn", "no_network": "warn", "error": "fail"}


def _progress(evt: str, d: dict):
    if evt == "start":
        llm = d["llm"]
        print(f"{C['b']}RADAR{C['x']} {d['site_id']}  run {d['run_id']}")
        print(f"{C['dim']}LLM (healing + failure triage): {llm['provider']}"
              f"{' (' + llm['model'] + ')' if llm['model'] else ''}"
              f"{'' if llm['provider'] != 'none' else '  · add OPENAI_API_KEY to .env to switch it on'}{C['x']}")
    elif evt == "discovered":
        sm = d["sitemap"]
        print(f"\n{C['b']}Discovery{C['x']}  platform={sm.platform}  theme={sm.theme}  checkout={sm.checkout_app}  "
              f"access={sm.access}  nav={len(sm.nav)}  "
              f"collections={len(sm.collections)}  products={len(sm.products)} "
              f"(in stock {sum(p.available for p in sm.products)})")
        for n in sm.notes:
            print(f"  {C['warn']}note{C['x']} {n}")
    elif evt == "suites":
        total = sum(len(x.cases) for x in d["suites"])
        print(f"\n{C['b']}Generated{C['x']} {len(d['suites'])} suites, {total} test cases\n")
    elif evt == "attempt":
        case, a = d["case"], d["attempt"]
        mark = f"{C['pass']}PASS{C['x']}" if a.ok else f"{C['fail']}FAIL{C['x']}"
        if not a.ok and a.steps and a.steps[-1].status == "skip":
            mark = f"{C['warn']}BLOCKED{C['x']}"
        warns = [s for s in a.steps if s.status == "warn"]
        heals = [s for s in a.steps if s.healed]
        extra = (f" {C['warn']}{len(warns)} warning(s){C['x']}" if warns else "") + \
                (f" {C['warn']}healed{C['x']}" if heals else "")
        retry = f" {C['dim']}(attempt {a.n}{', ' + a.note if a.note else ''}){C['x']}" if a.n > 1 else ""
        print(f"  {mark} {case.suite:8} {case.title[:60]}{retry}{extra}  {C['dim']}{a.secs}s{C['x']}")
        if not a.ok:
            print(f"       {C['dim']}{a.failed_step}: {(a.error or '')[:150]}{C['x']}")
    elif evt == "triage":
        t = d["triage"]
        if t:
            col = C["fail"] if t["verdict"] == "real_store_problem" else C["warn"]
            print(f"       {col}LLM triage: {t['verdict'].replace('_', ' ')}{C['x']} {C['dim']}({t['category']}) "
                  f"{t['reason'][:140]}{C['x']}")
        else:
            print(f"       {C['dim']}LLM triage: no answer (off, over budget, or invalid reply){C['x']}")
    elif evt == "done":
        run, run_dir = d["run"], d["run_dir"]
        c = run.counts()
        col = C[VERDICT_COLOR.get(run.verdict, "warn")]
        print(f"\n{C['b']}Verdict{C['x']} {col}{run.verdict.upper()}{C['x']}  pass {c['pass']}  flaky {c['flaky']}  "
              f"fail {c['confirmed_fail']}  blocked {c['blocked']}  healed {len(run.healing_events)}")
        u = run.llm_usage or {}
        if u.get("calls"):
            cost = f"  ≈ ${u['est_usd']:.4f}" if u.get("est_usd") is not None else ""
            print(f"{C['dim']}LLM: {u['calls']} call(s), {u['input_tokens']} in / {u['output_tokens']} out tokens{cost}"
                  f"{'  errors: ' + '; '.join(u['errors']) if u.get('errors') else ''}{C['x']}")
        print(f"Report  {run_dir / 'report.html'}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="radar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="discover, generate and run tests for a URL")
    sc.add_argument("url")
    sc.add_argument("--device", default="desktop", choices=["desktop", "mobile", "both"])
    sc.add_argument("--headed", action="store_true", help="show the browser window")
    sc.add_argument("--plain-ua", action="store_true", help="send only 'BugRadar/0.1 (+bugradar.in)' as User-Agent")
    sc.add_argument("--slowmo", type=int, default=None, metavar="MS",
                    help="pause MS milliseconds before each browser action (default 400 when --headed)")
    sc.add_argument("--no-cart", action="store_true", help="skip the add-to-cart flow")
    sc.add_argument("--suites", help="comma list: smoke,catalog,product,cart,search,health")
    sc.add_argument("--open", action="store_true", help="open the report when done")
    bp = sub.add_parser("bench", help="run Radar across a list of stores and produce one table")
    bp.add_argument("store_list", help="text file: one URL per line, add 'cart' to enable the cart flow")
    bp.add_argument("--workers", type=int, default=3, help="stores scanned in parallel (default 3)")
    bp.add_argument("--quick", action="store_true", help="fewer pages per store (faster bench)")
    bp.add_argument("--open", action="store_true", help="open the bench page when done")
    bp.add_argument("--headed", action="store_true", help="show the browser windows (a visible Chrome per worker)")
    bp.add_argument("--plain-ua", action="store_true", help="send only 'BugRadar/0.1 (+bugradar.in)' as User-Agent")
    lp = sub.add_parser("llm-check", help="score the configured LLM on 21 known failure cases (needs an API key in .env)")
    lp.add_argument("--model", help="try another model of the same provider without editing .env")
    sub.add_parser("sites", help="list scanned sites")
    op = sub.add_parser("open", help="open latest report for a site")
    op.add_argument("site")
    a = ap.parse_args(argv)

    s = load_settings()
    if a.cmd == "llm-check":
        from dataclasses import replace as _replace
        from radar.healing.llm import make_client
        from radar import llmcheck
        s = _replace(s, llm_max_calls_per_run=100, **({"llm_model": a.model} if a.model else {}))
        llm = make_client(s)
        if not llm.enabled:
            print("No LLM configured. Copy .env.example to .env and paste your OPENAI_API_KEY.")
            return 2
        print(f"{C['b']}LLM check{C['x']} {llm.provider} / {llm.model}: {len(llmcheck.CASES)} failure cases, known answers ({sum(bool(c.get('held_out')) for c in llmcheck.CASES)} held-out)\n")
        r = llmcheck.run(llm)
        col = C["pass"] if r["correct"] == r["total"] else C["warn"]
        cost = f"≈ ${r['est_usd']:.4f}" if r["est_usd"] is not None else "cost unknown for this model"
        print(f"\n{col}{r['correct']}/{r['total']} correct{C['x']} (held-out {r['held_out']})  {r['calls']} calls  {r['input_tokens']} in / "
              f"{r['output_tokens']} out tokens  {cost}  {r['secs']}s")
        if r["errors"]:
            print(f"{C['fail']}errors:{C['x']} " + "; ".join(r["errors"]))
        return 0 if r["correct"] == r["total"] else 1
    if a.cmd == "bench":
        from pathlib import Path
        from radar.bench import parse_store_list, run_bench, console_table
        entries = parse_store_list(Path(a.store_list).read_text())
        if a.quick:
            s = replace(s, max_products=1, max_collections=1, max_nav_links=4)
        if a.headed:
            s = replace(s, headless=False)
        if a.plain_ua:
            s = replace(s, ua_style="plain")
        print(f"{C['b']}RADAR BENCH{C['x']} {len(entries)} stores, {a.workers} at a time"
              f"{' (quick)' if a.quick else ''}{' (headed)' if not s.headless else ''}"
              f"  User-Agent: {'BugRadar only' if s.ua_style == 'plain' else 'Chrome + BugRadar'}\n")
        out = run_bench(entries, s, a.workers)
        import json as _json
        payload = _json.loads((out / "bench.json").read_text())
        print("\n" + console_table(payload["rows"]))
        print(f"\nTotals {payload['totals']}")
        print(f"Bench page  {out / 'bench.html'}")
        if a.open:
            webbrowser.open((out / "bench.html").as_uri())
        return 0
    if a.cmd == "sites":
        st = Storage(s.data_dir)
        for row in st.sites():
            h = st.history(row["site_id"], 1)
            v = h[0]["verdict"] if h else "-"
            print(f"{row['site_id']:30} {row['platform']:10} last {row['last_run']}  {v}")
        return 0
    if a.cmd == "open":
        idx = s.data_dir / "sites" / site_id_from_url(a.site) / "index.html"
        print(idx)
        webbrowser.open(idx.as_uri())
        return 0

    if getattr(a, "plain_ua", False):
        s = replace(s, ua_style="plain")
    if a.headed:
        s = replace(s, headless=False, slow_mo_ms=400 if a.slowmo is None else a.slowmo)
    elif a.slowmo:
        s = replace(s, slow_mo_ms=a.slowmo)
    if a.no_cart:
        s = replace(s, allow_cart_flow=False)
    suites = a.suites.split(",") if a.suites else None
    worst = 0
    for dev in (["desktop", "mobile"] if a.device == "both" else [a.device]):
        run, run_dir = scan(a.url, s, dev, progress=_progress, only_suites=suites)
        if a.open:
            webbrowser.open((run_dir / "report.html").as_uri())
        worst = max(worst, {"healthy": 0, "degraded": 1}.get(run.verdict, 2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
