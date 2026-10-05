"""Load a site config and derive the site_id used as the storage key."""
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
import yaml


def site_id_from_url(url: str) -> str:
    """xyz.in -> 'xyz.in'. Lowercase, strip 'www.', ignore port and path.
    Subdomains stay separate sites (shop.xyz.in != xyz.in)."""
    host = urlparse(url if "://" in url else "https://" + url).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError(f"cannot derive site_id from {url!r}")
    return host


@dataclass
class Access:
    user_agent: str = "BugRadar/0.1 (+bugradar.in)"
    delay_seconds: float = 2.0
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"


@dataclass
class SiteConfig:
    base_url: str
    platform: str
    checkout_provider: str
    seed: dict = field(default_factory=dict)
    selectors: dict = field(default_factory=dict)
    access: Access = field(default_factory=Access)

    @property
    def site_id(self) -> str:
        return site_id_from_url(self.base_url)


REQUIRED_SELECTORS = ["product_card_link", "add_to_cart", "cart_item"]


def load_site(path: str | Path) -> SiteConfig:
    raw = yaml.safe_load(Path(path).read_text())
    if raw.get("platform") != "shopify":
        raise ValueError("v1 supports platform: shopify only")
    missing = [k for k in REQUIRED_SELECTORS if k not in raw.get("selectors", {})]
    if missing:
        raise ValueError(f"config missing selectors: {missing}")
    return SiteConfig(
        base_url=raw["base_url"].rstrip("/"),
        platform=raw["platform"],
        checkout_provider=raw.get("checkout_provider", "native"),
        seed=raw.get("seed", {}),
        selectors=raw["selectors"],
        access=Access(**raw.get("access", {})),
    )
