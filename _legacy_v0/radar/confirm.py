"""Confirm-then-alert logic. Pure Python, no browser, fully unit-tested.

Rule: a journey is run once. If it fails, it is retried up to MAX_RETRIES times
in fresh browser contexts. 2 failures out of 3 attempts = confirmed incident.
"""
from dataclasses import dataclass

MAX_RETRIES = 2
CONFIRM_FAILS = 2


@dataclass
class Attempt:
    ok: bool
    failed_step: str | None = None
    error: str | None = None


def should_retry(attempts: list[Attempt]) -> bool:
    """Retry only while the latest attempt failed, we have retries left,
    and the incident is not already confirmed."""
    if not attempts or attempts[-1].ok:
        return False
    if len(attempts) > MAX_RETRIES:
        return False
    return sum(not a.ok for a in attempts) < CONFIRM_FAILS


def verdict(attempts: list[Attempt]) -> str:
    """'pass' | 'flaky' | 'confirmed_fail'.
    flaky = failed at least once but did not reach the confirm threshold."""
    fails = sum(not a.ok for a in attempts)
    if fails == 0:
        return "pass"
    if fails >= CONFIRM_FAILS:
        return "confirmed_fail"
    return "flaky"


def signature(site_id: str, journey: str, step: str | None) -> str:
    """Dedupe key: one outage is one incident (later runs append evidence)."""
    return f"{site_id}|{journey}|{step or 'unknown'}"
