# radar

BugRadar monitoring engine (v0, layer 1). Read `docs/ARCHITECTURE.md` first.

## Setup (on your machine)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python -m pytest -q          # 15 tests should pass
```

## First run
```bash
python runner.py sites/shop125.yml --headed     # watch it
python runner.py sites/shop125.yml --device mobile
```
Evidence appears in `evidence/shop125.example/<date>/<run_id>/`.
Open a failure trace with: `playwright show-trace evidence/.../attempt1_trace.zip`

## Your first exercise
Edit a selector in `sites/shop125.yml` to something wrong on purpose, run again,
then open the trace and the failure screenshot. That is how every future failure will look.
