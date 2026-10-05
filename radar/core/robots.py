"""robots.txt respect. Radar is an honest monitor: it does not fetch paths a site disallows.
Pure Python, unit-tested. If robots.txt cannot be read, we treat everything as allowed
(standard crawler convention) and record that fact.
"""
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


class Robots:
    def __init__(self, robots_txt: str | None, user_agent: str):
        self.user_agent = user_agent
        self.loaded = robots_txt is not None
        self._rp = RobotFileParser()
        self._rp.parse((robots_txt or "").splitlines())

    def allowed(self, url: str) -> bool:
        if not self.loaded:
            return True
        # Match on the product token (BugRadar), as robots.txt files do.
        token = self.user_agent.split("/")[0]
        path = urlparse(url).path or "/"
        q = urlparse(url).query
        return self._rp.can_fetch(token, path + ("?" + q if q else ""))
