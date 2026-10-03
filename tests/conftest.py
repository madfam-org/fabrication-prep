"""Test harness.

Database tests need two URLs to ONE PostgreSQL database (scripts/test-db-setup.sql):

* ``FABRICATION_PREP_TEST_ADMIN_URL`` — the schema owner (NOSUPERUSER, NOBYPASSRLS): runs the migration and
  truncates between tests;
* ``FABRICATION_PREP_TEST_APP_URL`` — the runtime role the service uses.

If they are missing, every database test ERRORS (never skips): a skipped queue or tenancy proof is not a
pass. Tokens are real RS256 JWTs signed by a per-session key and verified through a JWKS file (JWKS_PATH,
honoured only in the test environment). The slicer is faked by ``fake_runner`` (tests/slicer_fakes.py)
except in tests marked ``orcaslicer``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ADMIN_URL_ENV = "FABRICATION_PREP_TEST_ADMIN_URL"
APP_URL_ENV = "FABRICATION_PREP_TEST_APP_URL"
ISSUER = "https://auth.madfam.io"
AUDIENCE = "fabrication-prep-api"
KID = "test-key-1"
SCOPE = "fabrication-prep:slice"
TABLES = "outbox, slice_jobs, artifacts"
URL_KEY = base64.b64encode(b"k" * 32).decode()
OLD_URL_KEY = base64.b64encode(b"o" * 32).decode()
BUNDLE_HOST = "bundles.test"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; database tests cannot run (they are not skipped)")
    return value


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory) -> tuple[rsa.RSAPrivateKey, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    path = tmp_path_factory.mktemp("jwks") / "jwks.json"
    path.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    return key, path


@pytest.fixture(scope="session", autouse=True)
def test_environment(signing_key, tmp_path_factory) -> Iterator[None]:
    _, jwks_path = signing_key
    root = tmp_path_factory.mktemp("service")
    env = {
        "FABRICATION_PREP_ENV": "test",
        "JWKS_PATH": str(jwks_path),
        "DB_STARTUP_RETRY_SECONDS": "5",
        "ARTIFACT_URL_KEYS": f"k2:{URL_KEY},k1:{OLD_URL_KEY}",
        "ARTIFACT_FS_ROOT": str(root / "artifacts"),
        "PUBLIC_BASE_URL": "http://testserver",
        "INPUT_ALLOWED_HOSTS": BUNDLE_HOST,
        "WORKER_WORKDIR": str(root / "work"),
        "WORKER_HEARTBEAT_FILE": str(root / "work" / "heartbeat"),
        "RETRY_BACKOFF_SECONDS": "0",
        "ORCASLICER_BIN": os.environ.get("ORCASLICER_BIN", "/nonexistent/orca-slicer"),
    }
    if os.environ.get(APP_URL_ENV):
        env["APP_DATABASE_URL"] = os.environ[APP_URL_ENV]
    if os.environ.get(ADMIN_URL_ENV):
        env["DATABASE_URL"] = os.environ[ADMIN_URL_ENV]
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    from fabrication_prep import auth, settings

    settings.reset_settings_cache()
    auth.reset_key_cache()
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def make_token(signing_key) -> Callable[..., str]:
    key, _ = signing_key

    def make(scopes=(SCOPE,), tenant: str | None = None, sub: str = "service-account:pravara", **overrides) -> str:
        now = dt.datetime.now(dt.UTC)
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "iat": now,
            "exp": now + dt.timedelta(minutes=10),
            "scope": " ".join(scopes),
            "token_use": "client_credentials",
        }
        if tenant is not None:
            claims["tenant_id"] = tenant
        headers = {"kid": overrides.pop("kid", KID)}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key, algorithm="RS256", headers=headers)

    return make


@pytest.fixture
def auth_header(make_token) -> Callable[..., dict]:
    def header(scopes=(SCOPE,), **kw) -> dict:
        return {"Authorization": f"Bearer {make_token(scopes, **kw)}"}

    return header


@pytest.fixture(scope="session")
def migrated() -> str:
    admin = _required(ADMIN_URL_ENV)
    _required(APP_URL_ENV)
    from fabrication_prep.cli import migrate

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {TABLES}, alembic_version CASCADE")
    migrate(database_url=admin, app_role="fabrication_prep_app")
    return admin


@pytest.fixture(scope="session")
def pool(migrated) -> Iterator[None]:
    from fabrication_prep import db

    db.open_pool(os.environ[APP_URL_ENV])
    yield
    db.close_pool()


@pytest.fixture
def admin_conn(migrated) -> Iterator[psycopg.Connection]:
    with psycopg.connect(migrated, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clean_db(pool, migrated) -> None:
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")


@pytest.fixture
def client(clean_db):
    from fastapi.testclient import TestClient

    from fabrication_prep.app import app

    return TestClient(app, raise_server_exceptions=False)
