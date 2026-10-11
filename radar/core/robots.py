"""robots.txt respect. Radar is an honest monitor: it does not fetch paths a site disallows.
Pure Python, unit-tested. Follows RFC 9309 (the robots.txt standard):
  - robots.txt answered 200            -> its rules apply
  - robots.txt answered 4xx (missing)  -> "unavailable": everything allowed (recorded)
  - robots.txt answered 5xx / no answer -> "unreachable": NOTHING allowed this run (v0.13; before, Radar
    treated this as allow-all, which bench 8 exposed when the Mac's network dropped)
"""
import re
from urllib.parse import urlparse, unquote
from urllib.robotparser import RobotFileParser


def robots_rules(robots_txt: str, token: str) -> list[tuple[bool, str]]:
    """The (allow, path pattern) rules that apply to `token` (RFC 9309: the groups naming the token, else the '*'
    groups). Pure, unit-tested. Kept beside Python's parser because Python's own handling of '*' depends on its
    version (3.12 and older read '*' as a literal character, and 3.13 encodes '&' in patterns), while Shopify's default
    robots.txt is written with wildcards: 'Disallow: /collections/*sort_by*', '/collections/*filter*&*filter*'."""
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    in_rules = True
    for raw in (robots_txt or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules or not groups:
                groups.append(([], []))
            groups[-1][0].append(val.lower())
            in_rules = False
        elif key in ("allow", "disallow") and groups:
            in_rules = True
            if val:                                   # an empty Disallow allows everything: no rule
                groups[-1][1].append((key == "allow", val))
    t = token.lower()
    mine = [r for agents, rules in groups if t in agents for r in rules]
    return mine if any(t in agents for agents, _ in groups) else [r for agents, rules in groups if "*" in agents for r in rules]


def rules_allow(rules: list[tuple[bool, str]], path_and_query: str) -> bool:
    """RFC 9309 matching: '*' matches any characters, a final '$' ends the path, the longest matching rule wins and
    an allow beats a disallow of the same length. Compared after percent-decoding both sides ('%2B' = '+'). Pure."""
    target = unquote(path_and_query or "/")
    best: tuple[int, bool] | None = None
    for allow, pat in rules:
        p = unquote(pat)
        rx = re.escape(p.rstrip("$")).replace(r"\*", ".*") + ("$" if p.endswith("$") else "")
        if re.match(rx, target, re.DOTALL):
            key = (len(p), allow)
            if best is None or key > best:
                best = key
    return True if best is None else best[1]


class Robots:
    def __init__(self, robots_txt: str | None, user_agent: str, unreachable: bool = False):
        self.user_agent = user_agent
        self.loaded = robots_txt is not None
        self.unreachable = unreachable and robots_txt is None
        self._rp = RobotFileParser()
        self._rp.parse((robots_txt or "").splitlines())
        self._rules = robots_rules(robots_txt or "", user_agent.split("/")[0])

    def crawl_delay(self) -> float | None:
        """Seconds the store's robots.txt asks between requests (Crawl-delay) for Radar's token, else None."""
        if not self.loaded:
            return None
        try:
            d = self._rp.crawl_delay(self.user_agent.split("/")[0])
            return float(d) if d else None
        except (TypeError, ValueError):
            return None

    def allowed(self, url: str) -> bool:
        if self.unreachable:
            return False
        if not self.loaded:
            return True
        # Match on the product token (BugRadar), as robots.txt files do.
        token = self.user_agent.split("/")[0]
        path = urlparse(url).path or "/"
        q = urlparse(url).query
        # both must allow: Python's parser, and Radar's own wildcard matcher (see robots_rules: on some Python versions
        # the parser alone lets through '/collections/x?sort_by=…', which Shopify's default robots.txt disallows)
        target = path + ("?" + q if q else "")
        return self._rp.can_fetch(token, target) and rules_allow(self._rules, target)
