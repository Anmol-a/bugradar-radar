"""Web Bot Auth: Radar signs its requests so stores' edge (Shopify / Cloudflare) can verify it is BugRadar.

Why (9 Oct 2026): Shopify's storefront protection answers HTTP 429 to UNSIGNED automated traffic from cloud networks
(Oracle Cloud, Contabo: every request, any User-Agent, even the homepage, server: cloudflare, retry-after: 60), while
signed clients can qualify for normal limits. This is the honest opposite of stealth: every request says, with a
cryptographic proof, "I am BugRadar, here is my public key".

Protocol: HTTP Message Signatures (RFC 9421) with the Web Bot Auth profile (draft-meunier-webbotauth-httpsig-protocol):
  Signature-Agent: "https://bugradar.in"                 (where the key directory lives)
  Signature-Input: sig1=("@authority" "signature-agent");created=..;expires=..;keyid="<JWK thumbprint>";
                   alg="ed25519";nonce="..";tag="web-bot-auth"
  Signature:       sig1=:<base64 Ed25519 signature>:
Public key directory: https://bugradar.in/.well-known/http-message-signatures-directory
  (media type application/http-message-signatures-directory+json, a JWK Set whose kid = the key's thumbprint).

The PRIVATE key never enters the repo or a chat: it is generated on a trusted machine
(python3 -m radar.core.webbotauth keygen) and given to Radar only through the environment variable RADAR_SIGNING_KEY
(a GitHub secret / the runner's environment). No key set = Radar runs unsigned, exactly as before.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from urllib.parse import urlparse

DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_MEDIA_TYPE = "application/http-message-signatures-directory+json"
DEFAULT_AGENT = "https://bugradar.in"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwk_thumbprint(x: str) -> str:
    """RFC 7638 / RFC 8037 A.3 thumbprint of an Ed25519 public key (base64url x)."""
    canon = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x}, separators=(",", ":"), sort_keys=True)
    return b64u(hashlib.sha256(canon.encode()).digest())


def authority(url: str) -> str:
    """@authority per RFC 9421: lowercase host, port only when not the scheme's default."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.port and not ((u.scheme == "https" and u.port == 443) or (u.scheme == "http" and u.port == 80)):
        return f"{host}:{u.port}"
    return host


class Signer:
    """Signs per authority; one signature stays valid for `ttl` seconds, so it is computed once per host per hour."""

    def __init__(self, private_seed_b64u: str, agent: str = DEFAULT_AGENT, ttl: int = 3600, clock=time.time):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self._key = Ed25519PrivateKey.from_private_bytes(b64u_decode(private_seed_b64u))
        raw_pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.x = b64u(raw_pub)
        self.keyid = jwk_thumbprint(self.x)
        self.agent, self.ttl, self.clock = agent, ttl, clock
        self._cache: dict[str, tuple[int, dict]] = {}

    def headers(self, url_or_authority: str) -> dict[str, str]:
        auth = authority(url_or_authority) if "://" in url_or_authority else url_or_authority.lower()
        now = int(self.clock())
        hit = self._cache.get(auth)
        if hit and now < hit[0] - 60:             # reuse until a minute before it expires
            return dict(hit[1])
        created, expires = now, now + self.ttl
        agent_value = f'"{self.agent}"'
        params = (f'("@authority" "signature-agent");created={created};expires={expires};keyid="{self.keyid}";'
                  f'alg="ed25519";nonce="{b64u(secrets.token_bytes(16))}";tag="web-bot-auth"')
        base = f'"@authority": {auth}\n"signature-agent": {agent_value}\n"@signature-params": {params}'
        sig = base64.b64encode(self._key.sign(base.encode())).decode()
        out = {"Signature-Agent": agent_value, "Signature-Input": f"sig1={params}", "Signature": f"sig1=:{sig}:"}
        self._cache[auth] = (expires, out)
        return dict(out)

    def directory(self, valid_days: int = 365) -> dict:
        now = int(self.clock())
        return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": self.x, "kid": self.keyid, "use": "sig",
                          "nbf": now, "exp": now + valid_days * 86400}]}


def signer_from_env(agent: str = DEFAULT_AGENT) -> Signer | None:
    seed = (os.environ.get("RADAR_SIGNING_KEY") or "").strip()
    return Signer(seed, agent) if seed else None


def verify(headers: dict, auth: str, x: str) -> bool:
    """Check a signature made by Signer (used by tests and by `radar.core.webbotauth check`)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    params = headers["Signature-Input"].split("=", 1)[1]
    base = f'"@authority": {auth}\n"signature-agent": {headers["Signature-Agent"]}\n"@signature-params": {params}'
    sig = base64.b64decode(headers["Signature"].split("=", 1)[1].strip(":"))
    try:
        Ed25519PublicKey.from_public_bytes(b64u_decode(x)).verify(sig, base.encode())
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv=None) -> int:
    """python3 -m radar.core.webbotauth keygen      -> prints the PRIVATE key (keep it secret) and writes
                                                     http-message-signatures-directory (public, to publish)
       python3 -m radar.core.webbotauth headers <url> -> signed headers for one URL (needs RADAR_SIGNING_KEY)"""
    a = argv if argv is not None else sys.argv[1:]
    if a[:1] == ["keygen"]:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        seed = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                          serialization.NoEncryption())
        s = Signer(b64u(seed))
        with open("http-message-signatures-directory", "w") as f:
            json.dump(s.directory(), f, indent=2)
        print("PRIVATE key (secret: store it as RADAR_SIGNING_KEY; never paste it in a chat or commit it):")
        print(b64u(seed))
        print(f"\nPUBLIC key directory written to ./http-message-signatures-directory (keyid {s.keyid});")
        print(f"publish it at {DEFAULT_AGENT}{DIRECTORY_PATH} with Content-Type {DIRECTORY_MEDIA_TYPE}")
        return 0
    if a[:1] == ["headers"] and len(a) > 1:
        s = signer_from_env()
        if not s:
            print("RADAR_SIGNING_KEY is not set", file=sys.stderr)
            return 1
        for k, v in s.headers(a[1]).items():
            print(f"{k}: {v}")
        return 0
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
