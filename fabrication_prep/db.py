"""Database access: one small pool, one transaction per unit of work, a context setting per transaction.

* The runtime connects as a NON-OWNER role; ``check_role_posture`` refuses to start when that role owns a
  table, is a superuser or bypasses row-level security.
* Each transaction sets ``fabrication_prep.owner`` (the API caller's ``sub``) or ``fabrication_prep.worker``
  with ``set_config(name, value, true)`` — the parameterised form of ``SET LOCAL``. The ``slice_jobs``
  policy shows a job to its owner only, or to the worker context. The worker context is defence in depth
  against a missing WHERE clause, not a security boundary between API and worker: both run the same code
  under the same role.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .logging import describe_db_error
from .settings import get_settings

log = logging.getLogger(__name__)

OWNER_SETTING = "fabrication_prep.owner"
WORKER_SETTING = "fabrication_prep.worker"
EXPECTED_SCHEMA_REVISION = "0002_artifact_retention"
_pool: ConnectionPool | None = None


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached within the startup bound."""


class UnsafeDatabaseRole(RuntimeError):
    """The runtime role would not be subject to row-level security."""


def open_pool(url: str | None = None, application_name: str = "fabrication-prep") -> ConnectionPool:
    """Open the pool (idempotent). Raises DatabaseUnavailable after the bounded startup retry."""
    global _pool
    if _pool is not None:
        return _pool
    s = get_settings()
    conninfo = url or s.app_database_url
    if not conninfo:
        raise DatabaseUnavailable("APP_DATABASE_URL is not set")

    def new_pool() -> ConnectionPool:
        return ConnectionPool(
            conninfo,
            min_size=s.db_pool_min,
            max_size=s.db_pool_max,
            # No server-side prepared statements: works behind a transaction-mode pgbouncer.
            kwargs={
                "row_factory": dict_row,
                "autocommit": False,
                "prepare_threshold": None,
                "application_name": application_name,
            },
            open=False,
            name=application_name,
        )

    pool = new_pool()
    deadline = time.monotonic() + s.db_startup_retry_seconds
    delay = 0.5
    while True:
        try:
            pool.open(wait=True, timeout=min(5.0, max(1.0, s.db_startup_retry_seconds / 3)))
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            break
        except (psycopg.OperationalError, TimeoutError) as exc:
            pool.close()  # connection-level failures only; SQL errors are never retried
            if time.monotonic() >= deadline:
                log.error("database unreachable after startup retry window: %s", describe_db_error(exc))
                raise DatabaseUnavailable("database unreachable within DB_STARTUP_RETRY_SECONDS") from None
            log.warning("database not reachable yet, retrying: %s", describe_db_error(exc))
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
            pool = new_pool()
        except psycopg.Error:
            pool.close()
            raise
    _pool = pool
    return pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise DatabaseUnavailable("the database pool is not open")
    return _pool


@contextmanager
def transaction(*, owner: str | None = None, worker: bool = False) -> Iterator[psycopg.Cursor]:
    """One transaction with at most one context: an API caller (``owner``), the worker, or neither (an
    anonymous transaction sees no jobs; used for artifact metadata behind a signed URL)."""
    if owner is not None and worker:
        raise ValueError("a transaction is either an owner's or the worker's, not both")
    timeout = str(int(get_settings().db_statement_timeout_ms))
    with get_pool().connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT set_config(%s, %s, true), set_config(%s, %s, true), set_config('statement_timeout', %s, true)",
            (OWNER_SETTING, owner or "", WORKER_SETTING, "on" if worker else "", timeout),
        )
        yield cur


def check_role_posture(pool: ConnectionPool | None = None) -> None:
    """Fail closed if the runtime role could see past row-level security."""
    pool = pool or get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user")
        row = cur.fetchone()
        if row is None or row["rolsuper"] or row["rolbypassrls"]:
            raise UnsafeDatabaseRole("the runtime role is a superuser or bypasses row-level security")
        cur.execute(
            """
            SELECT count(*) AS owned FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relkind = 'r'
              AND pg_has_role(current_user, c.relowner, 'USAGE')
            """
        )
        owned = cur.fetchone()["owned"]
        conn.rollback()
    if owned:
        raise UnsafeDatabaseRole("the runtime role owns (or inherits ownership of) tables in its schema")


def schema_revision(pool: ConnectionPool | None = None) -> str | None:
    pool = pool or get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        conn.rollback()
    return row["version_num"] if row else None
