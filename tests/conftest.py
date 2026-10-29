# The tests' mock stores run on 127.0.0.1, which the public-address guard (radar/core/netguard.py) refuses on a real
# worker. Switched off for the test run; tests/test_netguard.py switches it back on where it is the thing under test.
import os

os.environ.setdefault("RADAR_ALLOW_PRIVATE_HOSTS", "1")
