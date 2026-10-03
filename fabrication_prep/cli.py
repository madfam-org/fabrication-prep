"""Command line.

* ``fabrication-prep migrate`` — ``alembic upgrade head`` with DATABASE_URL (the schema owner); grants the
  runtime privileges to APP_DB_ROLE. Run by the API's init container.
* ``fabrication-prep worker`` — the slice worker loop (the worker Deployment's command).
* ``fabrication-prep worker-health`` — exit 0 while the worker's heartbeat file is fresh (exec probe).
* ``fabrication-prep check-profiles`` — verify every shipped profile against its catalog digest.
* ``fabrication-prep selftest-slice`` — slice a built-in cube with the shipped profiles (no database); the
  image build runs it as the Linux proof of the CLI contract.
* ``fabrication-prep dev-token`` — local only: an RS256 key pair, a JWKS file and a signed token, for use
  with JWKS_PATH (honoured only when FABRICATION_PREP_ENV is local or test). It never talks to Janua.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path


def migrate(database_url: str | None = None, app_role: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "fabrication_prep:migrations")
    if database_url:
        cfg.attributes["database_url"] = database_url
    if app_role:
        cfg.attributes["app_db_role"] = app_role
    command.upgrade(cfg, "head")


def run_worker() -> int:
    from . import db
    from .artifacts import store_from_settings
    from .logging import configure_logging
    from .profiles import get_catalog
    from .settings import get_settings
    from .vocab import get_vocabulary
    from .worker import main_loop

    s = get_settings()
    configure_logging(s.log_level)
    db.open_pool(application_name="fabrication-prep-worker")
    db.check_role_posture()
    try:
        main_loop(s, get_catalog(), get_vocabulary(), store_from_settings(s))
    finally:
        db.close_pool()
    return 0


def worker_health(max_age: float) -> int:
    from .settings import get_settings

    path = Path(get_settings().worker_heartbeat_file)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        print("no heartbeat yet")
        return 1
    print(f"heartbeat age {age:.0f}s")
    return 0 if age <= max_age else 1


def check_profiles() -> int:
    from .profiles import load_catalog

    catalog = load_catalog()
    for p in catalog.profiles:
        print(f"ok {p.kind:8s} {p.ref:32s} {p.sha256}")
    print(f"{len(catalog.profiles)} profiles verified for OrcaSlicer {catalog.orcaslicer['version']}")
    return 0


def dev_token(directory: str, scopes: list[str], tenant: str | None, issuer: str, audience: str) -> str:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    key_file = path / "dev-signing-key.pem"
    if key_file.exists():
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        key_file.chmod(0o600)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "dev-key-1", "alg": "RS256", "use": "sig"})
    (path / "jwks.json").write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")
    now = dt.datetime.now(dt.UTC)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": f"service-account:dev-{uuid.uuid4().hex[:8]}",
        "iat": now,
        "exp": now + dt.timedelta(hours=1),
        "scope": " ".join(scopes),
        "token_use": "client_credentials",
    }
    if tenant:
        claims["tenant_id"] = tenant
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "dev-key-1"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fabrication-prep")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="alembic upgrade head (DATABASE_URL = schema owner)")
    sub.add_parser("worker", help="run the slice worker")
    health = sub.add_parser("worker-health", help="exit 0 while the worker heartbeat is fresh")
    health.add_argument("--max-age", type=float, default=180.0)
    sub.add_parser("check-profiles", help="verify shipped profiles against the catalog digests")
    st = sub.add_parser("selftest-slice", help="slice a built-in cube with the shipped profiles (no database)")
    st.add_argument("--target", choices=["klipper_gcode", "bambu_3mf"], default="klipper_gcode")
    st.add_argument("--workdir", default="")
    st.add_argument("--bin", default="")
    tok = sub.add_parser("dev-token", help="local-only RS256 token + JWKS file")
    tok.add_argument("--dir", default=".dev-keys")
    tok.add_argument("--scope", action="append", default=[])
    tok.add_argument("--tenant")
    tok.add_argument("--issuer", default="https://auth.madfam.io")
    tok.add_argument("--audience", default="fabrication-prep-api")
    args = parser.parse_args(argv)
    if args.command == "migrate":
        migrate()
        return 0
    if args.command == "worker":
        return run_worker()
    if args.command == "worker-health":
        return worker_health(args.max_age)
    if args.command == "check-profiles":
        return check_profiles()
    if args.command == "selftest-slice":
        from .selftest import main as selftest_main
        from .settings import get_settings

        s = get_settings()
        workdir = args.workdir or f"{s.worker_workdir}/selftest-{args.target}"
        return selftest_main(args.bin or s.orcaslicer_bin, args.target, workdir)
    print(dev_token(args.dir, args.scope, args.tenant, args.issuer, args.audience))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
