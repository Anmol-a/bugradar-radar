# The tests' mock stores run on 127.0.0.1, which the public-address guard (radar/core/netguard.py) refuses on a real
# worker. Switched off for the test run; tests/test_netguard.py switches it back on where it is the thing under test.
import os

os.environ.setdefault("RADAR_ALLOW_PRIVATE_HOSTS", "1")

# One store at a time per machine (radar/worker.py OneStore, 17 Nov) shares a lock file in /tmp between processes; the
# tests run many workers side by side, so it is off unless a test turns it on with its own lock file.
os.environ.setdefault("RADAR_STORE_LOCK", "off")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _calm_throttle_governor():
    """radar.core.browser.Session's throttle governor is process-wide (17 Nov): a test that met a 429 must not leave a
    pause behind for the next test."""
    from radar.core.browser import Session
    Session.reset_governor()
    Session.limited_hits = 0
    yield
    Session.reset_governor()
    Session.limited_hits = 0
