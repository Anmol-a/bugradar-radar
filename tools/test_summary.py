#!/usr/bin/env python3
"""Turn pytest junit XML files into one short Markdown table + verdict (used by .github/workflows/radar-tests.yml).

    python3 tools/test_summary.py junit-*.xml [--label NAME] [--expect N]

Prints a table (passed / failed / errors / skipped per file, then a total), lists every failing test id, and exits 1
when anything failed, errored, or fewer than --expect tests ran (a shard that crashed before writing its XML would
otherwise look like a pass). Standard library only.
"""
import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def read(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failing = []
    for s in suites:
        for k in counts:
            counts[k] += int(s.get(k, 0) or 0)
        for tc in s.iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                failing.append(f"{tc.get('classname', '')}::{tc.get('name', '')}".lstrip(":"))
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return counts, failing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--label", default="tests")
    ap.add_argument("--expect", type=int, default=0, help="fail unless at least this many tests ran")
    a = ap.parse_args()

    rows, total, failing = [], {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}, []
    for f in sorted(a.files):
        p = Path(f)
        if not p.exists():
            rows.append((p.name, "missing", "", "", ""))
            continue
        try:
            c, bad = read(p)
        except ET.ParseError as e:
            rows.append((p.name, f"unreadable: {e}", "", "", ""))
            continue
        rows.append((p.name, c["passed"], c["failures"], c["errors"], c["skipped"]))
        for k in total:
            total[k] += c[k]
        failing += bad

    ok = not failing and total["failures"] == 0 and total["errors"] == 0 and total["tests"] >= a.expect \
        and all(isinstance(r[1], int) for r in rows)
    print(f"## {a.label}: {'✅ PASS' if ok else '❌ FAIL'} — {total['passed']} passed, {total['failures']} failed, "
          f"{total['errors']} errors, {total['skipped']} skipped (of {total['tests']} run"
          + (f", {a.expect} expected" if a.expect else "") + ")\n")
    print("| file | passed | failed | errors | skipped |\n|---|---:|---:|---:|---:|")
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")
    if failing:
        print("\n**Failing tests**\n")
        for t in failing:
            print(f"- `{t}`")
            # also a GitHub annotation: readable from the API / the run page without opening the logs
            print(f"::error title={a.label}: failing test::{t}", file=sys.stderr)
    if a.expect and total["tests"] < a.expect:
        print(f"\n**Only {total['tests']} of {a.expect} tests ran** — a shard is missing or crashed before writing its XML.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
