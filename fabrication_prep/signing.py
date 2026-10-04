"""Short-lived download URLs bound to an artifact's sha256 (ADR-014: never a public bucket URL).

``/v1/artifacts/{sha256}?exp=<unix seconds>&kid=<key id>&sig=<hex HMAC-SHA256>`` where the MAC covers
``"fabrication-prep-artifact/v1\\n{sha256}\\n{exp}"``. The first configured key signs; every configured key
verifies, so keys rotate without breaking URLs already issued. A URL cannot be re-pointed at another
artifact, extended, or forged without the key; it is a bearer capability for ``ttl`` seconds (default 900).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode

DOMAIN = b"fabrication-prep-artifact/v1"


@dataclass(frozen=True)
class SignedUrl:
    url: str
    expires_at: int


class SignatureInvalid(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _mac(key: bytes, sha256: str, exp: int) -> str:
    return hmac.new(key, DOMAIN + b"\n" + sha256.encode() + b"\n" + str(exp).encode(), hashlib.sha256).hexdigest()


def sign(base_url: str, sha256: str, keys: list[tuple[str, bytes]], ttl: int, now: float | None = None) -> SignedUrl:
    kid, key = keys[0]
    exp = int(now if now is not None else time.time()) + ttl
    query = urlencode({"exp": exp, "kid": kid, "sig": _mac(key, sha256, exp)})
    return SignedUrl(f"{base_url.rstrip('/')}/v1/artifacts/{sha256}?{query}", exp)


def verify(
    sha256: str,
    exp: str | None,
    kid: str | None,
    sig: str | None,
    keys: list[tuple[str, bytes]],
    max_ttl: int,
    now: float | None = None,
) -> None:
    if not exp or not kid or not sig:
        raise SignatureInvalid("signature_missing", "The URL is not signed")
    if not exp.isdigit() or len(exp) > 12:
        raise SignatureInvalid("signature_invalid", "The URL signature is not valid")
    current = int(now if now is not None else time.time())
    expires = int(exp)
    if expires < current:
        raise SignatureInvalid("signature_expired", "The URL has expired; request the job again for a fresh one")
    if expires > current + max_ttl + 60:
        raise SignatureInvalid("signature_invalid", "The URL signature is not valid")
    for key_id, key in keys:
        if hmac.compare_digest(key_id, kid) and hmac.compare_digest(_mac(key, sha256, expires), sig):
            return
    raise SignatureInvalid("signature_invalid", "The URL signature is not valid")
