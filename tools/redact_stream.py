"""Pipe filter: copies stdin to stdout with store names replaced by aliases (see radar/core/redact.py).

    python3 -m radar bench shard.txt 2>&1 | python3 tools/redact_stream.py shard.txt | tee bench.log

Arguments are store-list files whose hosts are always masked; any other web address is masked too unless it is one
of ours or a public service. Covers the bench's child processes, which print to the same pipe.
Only masks on GitHub (GITHUB_ACTIONS=true) unless --always is given.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar.core.redact import Redactor, enabled  # noqa: E402


def main(argv: list[str]) -> int:
    always = "--always" in argv
    files = [a for a in argv if a != "--always"]
    on = always or enabled()
    red = Redactor()
    for f in files:
        p = Path(f)
        if p.is_file():
            red.add(*p.read_text(errors="replace").splitlines())
    out = sys.stdout
    for line in sys.stdin:
        out.write(red.scrub(line) if on else line)
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
