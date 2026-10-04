"""Operational behaviour: scrubbed DB errors, bounded startup retry, readiness, CLI commands."""

from __future__ import annotations

import json
import logging
import os
import time

import psycopg
import pytest

from fabrication_prep import cli, db
from fabrication_prep.logging import DbErrorScrubFilter, JsonFormatter, configure_logging, describe_db_error

SECRET = "hunter2-db-password"


def test_db_error_detail_never_reaches_logs():
    exc = psycopg.errors.UniqueViolation(f"duplicate key DETAIL: Key (owner)=({SECRET})")
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "write failed", (), (type(exc), exc, None))
    DbErrorScrubFilter().filter(record)
    line = JsonFormatter().format(record)
    assert SECRET not in line and "UniqueViolation" in line
    driver = logging.LogRecord("psycopg.pool", logging.WARNING, __file__, 1, "error connecting %s", (SECRET,), None)
    DbErrorScrubFilter().filter(driver)
    assert SECRET not in driver.getMessage()
    assert describe_db_error(psycopg.OperationalError(SECRET)) == "OperationalError sqlstate=-"


def test_configure_logging_is_idempotent_and_json(capsys):
    configure_logging("INFO")
    configure_logging("INFO")
    logging.getLogger("fabrication_prep.test").info("hello", extra={"job_id": "j1"})
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["job_id"] == "j1"


def test_startup_retry_is_bounded_and_scrubbed(monkeypatch, caplog):
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("DB_STARTUP_RETRY_SECONDS", "2")
    from fabrication_prep import settings

    settings.reset_settings_cache()
    started = time.monotonic()
    try:
        with pytest.raises(db.DatabaseUnavailable):
            db.open_pool(f"postgresql://someone:{SECRET}@127.0.0.1:9/nothing")
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()
    assert time.monotonic() - started < 20
    assert SECRET not in caplog.text


def test_missing_url_is_unavailable(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setenv("APP_DATABASE_URL", "")
    from fabrication_prep import settings

    settings.reset_settings_cache()
    try:
        with pytest.raises(db.DatabaseUnavailable):
            db.open_pool()
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()


def test_ready_reports_closed_pool_and_revision_drift(client, admin_conn, monkeypatch):
    assert client.get("/ready").status_code == 200
    monkeypatch.setattr(db, "EXPECTED_SCHEMA_REVISION", "9999_future")
    assert client.get("/ready").json() == {"status": "schema revision mismatch"}
    monkeypatch.undo()
    saved = db._pool
    monkeypatch.setattr(db, "_pool", None)
    r = client.get("/ready")
    assert r.status_code == 503 and r.json() == {"status": "database pool not open"}
    monkeypatch.setattr(db, "_pool", saved)


def test_database_errors_answer_without_detail(client, auth_header, monkeypatch):
    from fabrication_prep import queue

    def boom(*a, **k):
        raise psycopg.errors.InternalError(f"server said {SECRET}")

    monkeypatch.setattr(queue, "get_job", boom)
    r = client.get("/v1/slice-jobs/00000000-0000-0000-0000-000000000000", headers=auth_header())
    assert r.status_code == 500 and SECRET not in r.text and r.json()["errors"][0]["code"] == "database_error"

    def down(*a, **k):
        raise db.DatabaseUnavailable("pool")

    monkeypatch.setattr(queue, "get_job", down)
    r = client.get("/v1/slice-jobs/00000000-0000-0000-0000-000000000000", headers=auth_header())
    assert r.status_code == 503


def test_cli_commands(tmp_path, capsys, monkeypatch, migrated):
    assert cli.main(["check-profiles"]) == 0
    assert "12 profiles verified for OrcaSlicer 2.4.2" in capsys.readouterr().out
    token = cli.dev_token(
        str(tmp_path), ["fabrication-prep:slice"], "org-x", "https://auth.madfam.io", "fabrication-prep-api"
    )
    assert token.count(".") == 2 and (tmp_path / "jwks.json").exists()
    assert cli.dev_token(str(tmp_path), [], None, "i", "a")  # reuses the key file
    hb = tmp_path / "hb"
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(hb))
    from fabrication_prep import settings

    settings.reset_settings_cache()
    try:
        assert cli.main(["worker-health"]) == 1
        hb.touch()
        assert cli.main(["worker-health", "--max-age", "60"]) == 0
        os.utime(hb, (time.time() - 600, time.time() - 600))
        assert cli.main(["worker-health", "--max-age", "60"]) == 1
    finally:
        monkeypatch.undo()
        settings.reset_settings_cache()
    cli.migrate(database_url=migrated, app_role="fabrication_prep_app")  # idempotent at head


def test_migration_refuses_a_bad_or_missing_role(migrated):
    with psycopg.connect(migrated, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS outbox, slice_jobs, artifacts, alembic_version CASCADE")
    try:
        with pytest.raises(RuntimeError):
            cli.migrate(database_url=migrated, app_role="Bad-Role")
        with pytest.raises(Exception, match="does not exist"):
            cli.migrate(database_url=migrated, app_role="no_such_role")
    finally:
        with psycopg.connect(migrated, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS outbox, slice_jobs, artifacts, alembic_version CASCADE")
        cli.migrate(database_url=migrated, app_role="fabrication_prep_app")
