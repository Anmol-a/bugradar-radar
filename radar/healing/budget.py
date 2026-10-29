"""AI spend guard (30 Oct 2026; Anmol: use the Max plan's $100/month of Claude API credits, NEVER more than $100).

Every AI call Radar makes, on every machine (Contabo's workers, GitHub's runs), is added to one shared ledger in the
BugRadar database (public.llm_spend, function llm_spend_by_key, dashboard migration 34). The ledger answers "ok" while
today's spend is under $3 and this month's under $90. Once it says no, Radar stops asking the AI until the next day:
checks still run and alert exactly as before, only without the AI's extra judging. $3 a day can't add up to more than
$93 in any month, and the Claude Console's own monthly limit is the backstop above that.

  RemoteBudget   the shared ledger, reached with the operator's upload key (BUGRADAR_KEY) like the worker's other calls.
                 If the ledger can't be reached, the answer is NO (fail closed): no AI rather than unknown spend.
  LocalBudget    no upload key (a laptop, llm-check): a small cap for this process only ($0.50 by default).
Standard library only.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

Post = Callable[[str, dict, dict], object]


def _post(url: str, headers: dict, body: dict) -> object:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


@dataclass
class LocalBudget:
    cap_usd: float = 0.50
    spent: float = 0.0
    why: str = ""

    def allowed(self) -> bool:
        if self.spent >= self.cap_usd:
            self.why = f"AI budget for this run reached (${self.cap_usd:.2f})"
            return False
        return True

    def spend(self, usd: float) -> None:
        self.spent += max(0.0, usd)


@dataclass
class RemoteBudget:
    url: str
    upload_key: str
    anon_key: str
    post: Post = _post
    ok: bool | None = None          # last answer; None = not asked yet
    why: str = ""
    last: dict = field(default_factory=dict)

    def _ask(self, add_usd: float) -> None:
        try:
            r = self.post(f"{self.url.rstrip('/')}/rest/v1/rpc/llm_spend_by_key",
                          {"apikey": self.anon_key, "Authorization": f"Bearer {self.anon_key}"},
                          {"brk": self.upload_key, "add_usd": round(max(0.0, add_usd), 6), "add_calls": 1 if add_usd > 0 else 0})
            self.last = r if isinstance(r, dict) else {}
            self.ok = self.last.get("ok") is True
            if not self.ok:
                self.why = ("AI budget reached: " + ", ".join(
                    f"{k.split('_')[0]} ${float(self.last.get(k, 0)):.2f} of ${float(self.last.get(k.replace('usd', 'cap'), 0)):.0f}"
                    for k in ("day_usd", "month_usd") if k in self.last)) if self.last.get("day_usd") is not None else \
                    f"AI budget ledger refused ({self.last.get('why', 'no answer')})"
        except Exception as e:  # noqa: BLE001  fail closed
            self.ok, self.why = False, f"AI budget ledger unreachable ({type(e).__name__}): AI paused"

    def allowed(self) -> bool:
        if self.ok is None:
            self._ask(0.0)
        return bool(self.ok)

    def spend(self, usd: float) -> None:
        self._ask(usd)


def budget_from_env(anon_key: str) -> LocalBudget | RemoteBudget:
    key = os.environ.get("BUGRADAR_KEY", "")
    if key.startswith("brk_"):
        url = os.environ.get("BUGRADAR_SUPABASE_URL", "") or "https://onijwpgvpojpvgumvfim.supabase.co"
        return RemoteBudget(url, key, anon_key)
    return LocalBudget(float(os.environ.get("RADAR_LLM_LOCAL_CAP_USD", "0.50") or 0.50))
