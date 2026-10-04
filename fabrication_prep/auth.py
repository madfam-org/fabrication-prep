"""Janua RS256 service tokens — the only identity fabrication-prep knows.

* Signature: RS256 only, the key chosen by the token's ``kid`` from Janua's JWKS (cached). A token
  without ``kid``, or with any other ``alg`` (HS256, none), is rejected before any key lookup.
* Claims: ``iss`` must equal the configured issuer and ``aud`` must contain ``fabrication-prep-api``;
  ``exp``, ``iat`` and ``sub`` are required.
* Scope: every ``/v1`` route except the signed artifact download needs ``fabrication-prep:slice``
  (space-separated ``scope`` claim, Janua's service-token shape).
* Ownership: a job belongs to the token's ``sub`` (the calling client); only that client can read it.

Fail-closed: no token, an invalid token, a missing scope or an unreachable JWKS endpoint is a refusal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import jwt
from fastapi import Request

from .errors import forbidden, unauthorized
from .settings import get_settings

log = logging.getLogger(__name__)

SCOPE_SLICE = "fabrication-prep:slice"
MAX_SUB_LENGTH = 200

_jwks_client: jwt.PyJWKClient | None = None
_local_keys: dict[str, jwt.PyJWK] | None = None


def reset_key_cache() -> None:
    global _jwks_client, _local_keys
    _jwks_client = None
    _local_keys = None


def _load_local_jwks(path: str) -> dict[str, jwt.PyJWK]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {jwk["kid"]: jwt.PyJWK(jwk) for jwk in data.get("keys", []) if jwk.get("kid")}


def signing_key_for(kid: str):
    global _jwks_client, _local_keys
    s = get_settings()
    if s.jwks_path:
        if _local_keys is None:
            _local_keys = _load_local_jwks(s.jwks_path)
        if kid not in _local_keys:
            raise jwt.InvalidKeyError("unknown kid")
        return _local_keys[kid].key
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(s.effective_jwks_url, cache_keys=True, lifespan=s.jwks_cache_seconds, timeout=5)
    return _jwks_client.get_signing_key(kid).key


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str | None
    scopes: frozenset[str] = field(default_factory=frozenset)

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def verify_token(token: str) -> Principal:
    s = get_settings()
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise jwt.InvalidAlgorithmError("only RS256 is accepted")
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise jwt.InvalidTokenError("token has no kid")
    claims = jwt.decode(
        token,
        signing_key_for(kid),
        algorithms=["RS256"],
        audience=s.janua_audience,
        issuer=s.janua_issuer,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        leeway=30,
    )
    sub = str(claims["sub"])
    if not sub or len(sub) > MAX_SUB_LENGTH:
        raise jwt.InvalidTokenError("unsupported sub")
    scope_claim = claims.get("scope", "")
    scopes = frozenset(scope_claim.split()) if isinstance(scope_claim, str) else frozenset()
    tenant = claims.get("tenant_id")
    return Principal(sub=sub, tenant_id=tenant if isinstance(tenant, str) and tenant else None, scopes=scopes)


def require_slice_principal(request: Request) -> Principal:
    """FastAPI dependency: a verified token carrying ``fabrication-prep:slice``."""
    header = request.headers.get("authorization")
    if header is None:
        raise unauthorized("missing_token", "This operation requires a Janua service token")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise unauthorized("invalid_authorization", "Authorization must be 'Bearer <token>'")
    try:
        principal = verify_token(token.strip())
    except jwt.PyJWTError as exc:
        # PyJWKClientError (JWKS unreachable) is a PyJWTError too: refused, never let through.
        log.info("token rejected: %s", type(exc).__name__)
        raise unauthorized("invalid_token", f"Token rejected ({type(exc).__name__})") from None
    if not principal.has(SCOPE_SLICE):
        raise forbidden("missing_scope", f"The token lacks the scope '{SCOPE_SLICE}'")
    request.state.principal_sub = principal.sub
    return principal
