"""robots.txt respect. Radar is an honest monitor: it does not fetch paths a site disallows.
Pure Python, unit-tested. Follows RFC 9309 (the robots.txt standard):
  - robots.txt answered 200            -> its rules apply
  - robots.txt answered 4xx (missing)  -> "unavailable": everything allowed (recorded)
  - robots.txt answered 5xx / no answer -> "unreachable": NOTHING allowed this run (v0.13; before, Radar
    treated this as allow-all, which bench 8 exposed when the Mac's network dropped)
"""
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


class Robots:
    def __init__(self, robots_txt: str | None, user_agent: str, unreachable: bool = False):
        self.user_agent = user_agent
        self.loaded = robots_txt is not None
        self.unreachable = unreachable and robots_txt is None
        self._rp = RobotFileParser()
        self._rp.parse((robots_txt or "").splitlines())

    def allowed(self, url: str) -> bool:
        if self.unreachable:
            return False
        if not self.loaded:
            return True
        # Match on the product token (BugRadar), as robots.txt files do.
        token = self.user_agent.split("/")[0]
        path = urlparse(url).path or "/"
        q = urlparse(url).query
        return self._rp.can_fetch(token, path + ("?" + q if q else ""))
