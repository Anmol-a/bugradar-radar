# Radar

BugRadar's monitoring framework. Give it a Shopify store URL; it discovers the store,
generates the tests, runs them in a real browser, heals broken locators, and writes an
interactive report. Read `docs/ARCHITECTURE.md` for how and why.

## Setup
Always `python3` (not `python`) on the Mac, for every command in this project.
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env               # then paste your OPENAI_API_KEY into .env (never into chat or git)
python3 -m pytest -q                 # 105 tests, about 16 minutes (includes real-browser runs)
python3 -m pytest -q -m "not e2e"    # 57 fast unit tests only
```

## Use
```bash
python3 -m radar scan moxiebeauty.in --open            # desktop, opens the report
python3 -m radar scan moxiebeauty.in --device both     # desktop + mobile
python3 -m radar scan moxiebeauty.in --headed          # watch the browser (slowed to 400 ms per action)
python3 -m radar scan moxiebeauty.in --headed --slowmo 1000   # slower still
python3 -m radar scan palmonas.com --no-cart           # skip add-to-cart (prospects)
python3 -m radar scan moxiebeauty.in --suites smoke,cart
python3 -m radar sites                                 # everything scanned so far
python3 -m radar open moxiebeauty.in                   # site history page
```
Results: `data/sites/<site_id>/` (see ARCHITECTURE.md, section 7).

Status (6 Oct, v0.11): bench 6 (v0.10) gave 29 healthy, 3 degraded, 0 down on 32 Shopify stores,
with 0 Radar false failures and 0 flaky: the engine's exit rule is met (ARCHITECTURE.md 4k). The 3
failures are real store findings. v0.11 (normal Chrome name + BugRadar User-Agent) is not benched yet.

## Bench: many Shopify stores, one table
```bash
python3 -m radar bench stores/bench.txt --quick --open      # 33 stores, 3 at a time
python3 -m radar bench stores/bench.txt --workers 4         # full depth
python3 -m radar bench stores/bench.txt --headed --open     # visible Chrome windows (one per worker)
python3 -m radar bench stores/bench.txt --plain-ua          # User-Agent "BugRadar/0.1 (+bugradar.in)" only
```
By default Radar's User-Agent is the normal Chrome name followed by `BugRadar/0.1 (+bugradar.in)`:
always identified, never disguised (no stealth; ARCHITECTURE.md, limits 4).
`stores/bench.txt`: one URL per line; add `cart` after a URL to run add-to-cart there (only on
Shopify's demo stores or stores that agreed). Output: `data/bench/<time>/bench.html`.

## LLM (healing + failure triage)
Put the key in `.env` (template: `.env.example`). Default: OpenAI `gpt-5-mini` (15/15 on llm-check, 5 Oct; gpt-4o-mini scored 14/15).
```bash
python3 -m radar llm-check                    # score the model on 21 known failure cases (~$0.002)
python3 -m radar llm-check --model gpt-4o-mini # try another model without editing .env
```
Switching later (e.g. to Claude once BugRadar earns) is one line in `.env`; run `llm-check` again.
Without a key: heuristic healing only, no triage, and the run says so.

## See it work without touching a real store
```bash
python3 -m tests.mockstore.server renamed_button 8765  # terminal 1
python3 -m radar scan http://127.0.0.1:8765 --open     # terminal 2: watch it heal
```
Modes (30, described at the top of `tests/mockstore/server.py`), e.g. `healthy`, `renamed_button`,
`wrong_variant`, `free_gift`, `hostile`, `title_layouts`, `card_layouts`, `hover_menu`,
`brand_landing`, `rerender_grid`, `icon_popup`, `quick_named_main`, `sticky_price`,
`hidden_product`, `notfound_product`, `no_buy_form`, `search_misses`.

## Per-site overrides (optional)
`sites/<site_id>.yml`, for example `sites/moxiebeauty.in.yml`:
```yaml
delay_seconds: 2.5
max_products: 5
allow_cart_flow: false
selectors:
  add_to_cart: ["button.my-theme-atc"]
```
