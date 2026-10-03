"""Janua RS256 auth: every refusal path is a 401/403; only a valid token with the slice scope passes."""

from __future__ import annotations

import base64
import datetime as dt
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.slicer_fakes import job_body

ROUTES = [
    ("post", "/v1/slice-jobs"),
    ("get", "/v1/slice-jobs/00000000-0000-0000-0000-000000000000"),
    ("get", "/v1/profiles"),
    ("get", "/v1/profiles/printer/bambu-a1-0.4/1"),
]


def call(client, method, path, headers):
    if method == "post":
        return client.post(path, json=job_body(), headers=headers)
    return client.get(path, headers=headers)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_no_token_is_401(client, method, path):
    r = call(client, method, path, {})
    assert r.status_code == 401 and r.json()["errors"][0]["code"] == "missing_token"
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_missing_scope_is_403(client, auth_header, method, path):
    r = call(client, method, path, auth_header(scopes=("asset-shells:read",)))
    assert r.status_code == 403 and r.json()["errors"][0]["code"] == "missing_scope"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "asset-shells-api"},
        {"iss": "https://evil.example"},
        {"exp": dt.datetime(2020, 1, 1, tzinfo=dt.UTC), "iat": dt.datetime(2019, 12, 31, tzinfo=dt.UTC)},
        {"iat": None},
        {"sub": None},
        {"kid": "other-key"},
        {"sub": "x" * 300},
    ],
)
def test_bad_claims_are_401(client, auth_header, overrides):
    r = client.get("/v1/profiles", headers=auth_header(**overrides))
    assert r.status_code == 401 and r.json()["errors"][0]["code"] == "invalid_token"


def test_foreign_signing_key_is_401(client, make_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://auth.madfam.io",
            "aud": "fabrication-prep-api",
            "sub": "s",
            "iat": dt.datetime.now(dt.UTC),
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
            "scope": "fabrication-prep:slice",
        },
        other,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )
    assert client.get("/v1/profiles", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def test_hs256_none_and_kidless_tokens_are_401(client):
    claims = {
        "iss": "https://auth.madfam.io",
        "aud": "fabrication-prep-api",
        "sub": "s",
        "iat": 1,
        "exp": 4102444800,
        "scope": "fabrication-prep:slice",
    }
    hs = jwt.encode(claims, "secret-secret-secret-secret-secret!", algorithm="HS256", headers={"kid": "test-key-1"})
    none = f"{_b64({'alg': 'none', 'kid': 'test-key-1'})}.{_b64(claims)}."
    kidless = f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.c2ln"
    for token in (hs, none, kidless, "not-a-jwt"):
        r = client.get("/v1/profiles", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token


@pytest.mark.parametrize("header", ["Basic abc", "Bearer", "Bearer   ", "token"])
def test_malformed_authorization_is_401(client, header):
    r = client.get("/v1/profiles", headers={"Authorization": header})
    assert r.status_code == 401 and r.json()["errors"][0]["code"] == "invalid_authorization"


def test_unreachable_jwks_fails_closed(client, auth_header, monkeypatch):
    from fabrication_prep import auth, settings

    monkeypatch.setenv("JWKS_PATH", "")
    monkeypatch.setenv("JANUA_JWKS_URL", "http://127.0.0.1:9/.well-known/jwks.json")
    settings.reset_settings_cache()
    auth.reset_key_cache()
    try:
        r = client.get("/v1/profiles", headers=auth_header())
        assert r.status_code == 401
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()
        auth.reset_key_cache()


def test_valid_token_passes(client, auth_header):
    assert client.get("/v1/profiles", headers=auth_header()).status_code == 200
