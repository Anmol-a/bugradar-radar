"""Settings. Everything has a sane default so `radar scan <url>` needs nothing else.

Precedence (later wins): defaults -> radar.yml (project root, optional)
  -> environment variables -> sites/<site_id>.yml (optional per-site overrides).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]


def normalize_url(url: str) -> str:
    """'moxiebeauty.in' -> 'https://moxiebeauty.in'. Keeps the host as given (www kept for
    requests), drops path/query: Radar always starts from the homepage."""
    url = (url or "").strip()
    if not url:
        raise ValueError("empty URL")
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.hostname:
        raise ValueError(f"cannot parse URL {url!r}")
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{p.hostname.lower()}{port}"


def site_id_from_url(url: str) -> str:
    """Storage key. Lowercase host, 'www.' stripped, port kept only for localhost tests.
    Subdomains are separate sites (shop.xyz.in != xyz.in)."""
    p = urlparse(url if "://" in url else "https://" + url)
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError(f"cannot derive site_id from {url!r}")
    if host.startswith("www."):
        host = host[4:]
    if host in ("localhost", "127.0.0.1") and p.port:
        host = f"{host}_{p.port}"
    return host


@dataclass
class Settings:
    data_dir: Path = ROOT / "data"
    sites_dir: Path = ROOT / "sites"
    user_agent: str = "BugRadar/0.1 (+bugradar.in)"   # Radar's identity: always in the User-Agent, robots.txt token
    ua_style: str = "browser"           # browser = normal Chrome name + identity (default) | plain = identity only
    delay_seconds: float = 1.5          # pause between page navigations (politeness)
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"
    headless: bool = True
    slow_mo_ms: int = 0                 # >0 slows every browser action (for watching headed runs)
    nav_timeout_ms: int = 30000
    max_products: int = 3               # products tested per run
    max_collections: int = 2
    max_nav_links: int = 10             # menu pages opened by the smoke suite
    retries: int = 2                    # extra attempts on failure (confirm logic)
    allow_cart_flow: bool = True        # add-to-cart simulation; see ARCHITECTURE.md policy note
    respect_robots: bool = True
    search_is_shopper_flow: bool = True  # /search via the store's own search box is a shopper action
    llm_provider: str = "auto"          # auto | openai | anthropic | openai_compat | none
    llm_model: str = ""                 # "" = provider default (openai: gpt-5-mini, anthropic: claude-haiku-4-5)
    llm_base_url: str = ""              # for openai_compat (Gemini/DeepSeek/OpenAI-compatible)
    llm_max_calls_per_run: int = 12     # hard cost cap (~$0.005 per run with failures on gpt-5-mini)
    retention_pass_days: int = 7
    retention_fail_days: int = 90
    selectors: dict = field(default_factory=dict)   # per-site selector hints (optional)

    def for_site(self, site_id: str) -> "Settings":
        f = self.sites_dir / f"{site_id}.yml"
        if not f.exists():
            return self
        raw = yaml.safe_load(f.read_text()) or {}
        return _apply(self, raw)


_ENV = {
    "RADAR_DATA_DIR": ("data_dir", Path),
    "RADAR_HEADLESS": ("headless", lambda v: v.lower() not in ("0", "false", "no")),
    "RADAR_UA_STYLE": ("ua_style", str),
    "RADAR_DELAY": ("delay_seconds", float),
    "RADAR_LLM_PROVIDER": ("llm_provider", str),
    "RADAR_LLM_MODEL": ("llm_model", str),
    "RADAR_LLM_BASE_URL": ("llm_base_url", str),
    "RADAR_MAX_PRODUCTS": ("max_products", int),
    "RADAR_ALLOW_CART": ("allow_cart_flow", lambda v: v.lower() not in ("0", "false", "no")),
}


def _apply(s: Settings, raw: dict) -> Settings:
    known = {k: v for k, v in raw.items() if k in Settings.__dataclass_fields__}
    for k in ("data_dir", "sites_dir"):
        if k in known:
            known[k] = Path(known[k])
    if "selectors" in known:
        known["selectors"] = {**s.selectors, **(known["selectors"] or {})}
    return replace(s, **known)


def load_dotenv(path: Path | None = None) -> None:
    """KEY=VALUE lines from <project>/.env into the environment (real env vars win). Keeps API keys
    out of code and out of git (.env is git-ignored). No dependency."""
    f = path or ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv()
    s = Settings()
    cfg = path or ROOT / "radar.yml"
    if cfg.exists():
        s = _apply(s, yaml.safe_load(cfg.read_text()) or {})
    for env, (attr, cast) in _ENV.items():
        if os.environ.get(env):
            s = replace(s, **{attr: cast(os.environ[env])})
    return s
