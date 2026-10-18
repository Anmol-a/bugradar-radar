"""Is a free-scan request waiting longer than N seconds? Prints `yes` or `no` (18 Oct 2026).

    python3 tools/queue_age.py --older-than 360

The GitHub fallback job (.github/workflows/free-scan-worker.yml) runs the worker only on `yes`: our own box in Mumbai
is the first choice, GitHub's US machines step in when it has not picked the queue up. Reads the dashboard's public
scan_queue_age() function with the public anon key; no secret needed. Any error prints `no` (the next 5-minute run asks
again) and the reason on stderr. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

from radar.worker import ANON_KEY

URL = os.environ.get("BUGRADAR_SUPABASE_URL", "https://onijwpgvpojpvgumvfim.supabase.co").rstrip("/")


def queue_age(url: str = URL, key: str = ANON_KEY, timeout: int = 20) -> tuple[int, int]:
    """(requests waiting, age of the oldest in seconds)."""
    req = urllib.request.Request(f"{url}/rest/v1/rpc/scan_queue_age", data=b"{}", method="POST",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        rows = json.loads(r.read() or b"[]")
    row = rows[0] if isinstance(rows, list) and rows else (rows or {})
    return int(row.get("queued") or 0), int(row.get("oldest_seconds") or 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--older-than", type=int, default=360, metavar="SECS")
    a = ap.parse_args(argv)
    try:
        n, age = queue_age()
    except Exception as e:  # noqa: BLE001
        print(f"queue age unknown: {e}", file=sys.stderr)
        print("no")
        return 0
    print(f"{n} waiting, oldest {age} s", file=sys.stderr)
    print("yes" if n and age >= a.older_than else "no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
