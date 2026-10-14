"""The slice-job queue on PostgreSQL: enqueue, claim with leases, renew, finish, retry, dead-letter.

* Claim: ``SELECT ... FOR UPDATE SKIP LOCKED`` on queued jobs whose ``run_after`` has passed, then mark the
  row ``running`` with a lease (``lease_owner``, ``lease_expires_at``) in the same statement.
* A worker renews its lease while slicing; every write that ends a job checks ``lease_owner``, so a worker
  that lost its lease cannot overwrite the result of the worker that took over.
* Reaping: a ``running`` job whose lease expired returns to ``queued`` (or, with no attempts left, becomes
  ``dead_lettered``). Each claim cycle reaps first.
* Transient failures retry with linear backoff until ``max_attempts``; permanent ones end in ``failed``.
  Terminal transitions write their outbox event in the same transaction.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import db
from .artifacts import ArtifactStore

# The only text interpolated into SQL in this module is this constant column list (S608 is waived for this
# file in pyproject.toml for that reason; every value travels as a bound parameter).
JOB_COLUMNS = """id, owner_sub, tenant_id, idempotency_key, request_sha256, request, target, status, attempts,
  max_attempts, run_after, lease_owner, lease_expires_at, error_code, error_message, output_sha256,
  slicer_variables_sha256, result, created_at, updated_at, started_at, finished_at"""


def enqueue(
    *,
    owner: str,
    tenant_id: str | None,
    idempotency_key: str | None,
    request: dict[str, Any],
    request_sha256: str,
    target: str,
    max_attempts: int,
) -> tuple[dict, bool]:
    """(job, created). An idempotency key replayed with the same request returns the existing job; with a
    different request it raises ``IdempotencyConflict``."""
    with db.transaction(owner=owner) as cur:
        if idempotency_key:
            cur.execute(
                f"SELECT {JOB_COLUMNS} FROM slice_jobs WHERE owner_sub = %s AND idempotency_key = %s",
                (owner, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                if existing["request_sha256"] != request_sha256:
                    raise IdempotencyConflict()
                return existing, False
        cur.execute(
            f"""INSERT INTO slice_jobs (id, owner_sub, tenant_id, idempotency_key, request_sha256, request, target,
                  status, max_attempts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued', %s)
                ON CONFLICT (owner_sub, idempotency_key) DO NOTHING
                RETURNING {JOB_COLUMNS}""",
            (uuid.uuid4(), owner, tenant_id, idempotency_key, request_sha256, Jsonb(request), target, max_attempts),
        )
        row = cur.fetchone()
    if row is None:  # a concurrent request with the same key won the race
        return enqueue(
            owner=owner,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            request=request,
            request_sha256=request_sha256,
            target=target,
            max_attempts=max_attempts,
        )
    return row, True


class IdempotencyConflict(Exception):
    pass


def get_job(owner: str, job_id: uuid.UUID) -> dict | None:
    with db.transaction(owner=owner) as cur:
        cur.execute(f"SELECT {JOB_COLUMNS} FROM slice_jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def get_artifact(sha256: str) -> dict | None:
    """The artifact's metadata. ``expired_at`` is set once the retention sweep has deleted its bytes."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT sha256, media_type, bytes, filename, last_used_at, expired_at FROM artifacts WHERE sha256 = %s",
            (sha256,),
        )
        return cur.fetchone()


def _event(cur, topic: str, job: dict, payload: dict) -> None:
    cur.execute(
        "INSERT INTO outbox (topic, aggregate_id, payload) VALUES (%s, %s, %s)",
        (topic, job["id"], Jsonb({"job_id": str(job["id"]), "owner_sub": job["owner_sub"], **payload})),
    )


def reap_expired_leases() -> int:
    """Expired leases go back to the queue, or to the dead-letter state when no attempts remain."""
    with db.transaction(worker=True) as cur:
        cur.execute(
            f"""SELECT {JOB_COLUMNS} FROM slice_jobs WHERE status = 'running' AND lease_expires_at < now()
                FOR UPDATE SKIP LOCKED"""
        )
        rows = cur.fetchall()
        for job in rows:
            if job["attempts"] >= job["max_attempts"]:
                cur.execute(
                    """UPDATE slice_jobs SET status = 'dead_lettered', lease_owner = NULL, lease_expires_at = NULL,
                         error_code = 'lease_expired', error_message = 'the worker stopped renewing its lease',
                         finished_at = now(), updated_at = now() WHERE id = %s""",
                    (job["id"],),
                )
                _event(cur, "slice_job.dead_lettered", job, {"error_code": "lease_expired"})
            else:
                cur.execute(
                    """UPDATE slice_jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                         error_code = 'lease_expired', error_message = 'the worker stopped renewing its lease',
                         run_after = now(), updated_at = now() WHERE id = %s""",
                    (job["id"],),
                )
    return len(rows)


def claim(worker_id: str, lease_seconds: int) -> dict | None:
    with db.transaction(worker=True) as cur:
        cur.execute(
            f"""WITH next AS (
                  SELECT id FROM slice_jobs WHERE status = 'queued' AND run_after <= now()
                  ORDER BY run_after, created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE slice_jobs j SET status = 'running', attempts = j.attempts + 1, lease_owner = %s,
                  lease_expires_at = now() + make_interval(secs => %s), started_at = coalesce(j.started_at, now()),
                  updated_at = now()
                FROM next WHERE j.id = next.id
                RETURNING {", ".join("j." + c.strip() for c in JOB_COLUMNS.split(","))}""",
            (worker_id, lease_seconds),
        )
        return cur.fetchone()


def renew_lease(job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> bool:
    with db.transaction(worker=True) as cur:
        cur.execute(
            """UPDATE slice_jobs SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
               WHERE id = %s AND status = 'running' AND lease_owner = %s""",
            (lease_seconds, job_id, worker_id),
        )
        return cur.rowcount == 1


def store_artifact(store: ArtifactStore, path: Path, sha256: str, media_type: str, filename: str) -> None:
    """Record the artifact and make sure its bytes are stored, as one unit against the retention sweep.

    The row is upserted first: ``last_used_at`` moves to now and ``expired_at`` is cleared. That takes the row lock,
    which the sweep's ``FOR UPDATE SKIP LOCKED`` respects, so the sweep cannot delete these bytes between the
    existence check inside ``put_file`` and the commit; if the sweep holds the lock first, this upsert waits for it
    and ``put_file`` then finds the bytes gone and uploads them again. If the upload fails, the transaction rolls
    back and the row is left as it was."""
    with db.transaction(worker=True) as cur:
        cur.execute(
            """INSERT INTO artifacts (sha256, media_type, bytes, filename) VALUES (%s, %s, %s, %s)
               ON CONFLICT (sha256) DO UPDATE SET last_used_at = now(), expired_at = NULL""",
            (sha256, media_type, path.stat().st_size, filename),
        )
        store.put_file(path, sha256, media_type)


def complete(job: dict, worker_id: str, output_sha256: str, variables_sha256: str, result: dict) -> bool:
    with db.transaction(worker=True) as cur:
        cur.execute(
            """UPDATE slice_jobs SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL,
                 output_sha256 = %s, slicer_variables_sha256 = %s, result = %s, error_code = NULL,
                 error_message = NULL, finished_at = now(), updated_at = now()
               WHERE id = %s AND status = 'running' AND lease_owner = %s""",
            (output_sha256, variables_sha256, Jsonb(result), job["id"], worker_id),
        )
        if cur.rowcount != 1:
            return False
        _event(
            cur,
            "slice_job.succeeded",
            job,
            {"output_sha256": output_sha256, "slicer_variables_sha256": variables_sha256, **result},
        )
    return True


def fail(job: dict, worker_id: str, code: str, message: str, transient: bool, backoff_seconds: int) -> str | None:
    """The job's new status ('queued', 'failed', 'dead_lettered'), or None when the lease was lost."""
    message = message[:2000]
    with db.transaction(worker=True) as cur:
        if transient and job["attempts"] < job["max_attempts"]:
            status = "queued"
            cur.execute(
                """UPDATE slice_jobs SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                     error_code = %s, error_message = %s, updated_at = now(),
                     run_after = now() + make_interval(secs => %s)
                   WHERE id = %s AND status = 'running' AND lease_owner = %s""",
                (code, message, backoff_seconds * job["attempts"], job["id"], worker_id),
            )
        else:
            status = "dead_lettered" if transient else "failed"
            cur.execute(
                """UPDATE slice_jobs SET status = %s, lease_owner = NULL, lease_expires_at = NULL,
                     error_code = %s, error_message = %s, finished_at = now(), updated_at = now()
                   WHERE id = %s AND status = 'running' AND lease_owner = %s""",
                (status, code, message, job["id"], worker_id),
            )
        if cur.rowcount != 1:
            return None
        if status != "queued":
            _event(cur, f"slice_job.{'failed' if status == 'failed' else 'dead_lettered'}", job, {"error_code": code})
    return status


def job_request(job: dict) -> dict:
    req = job["request"]
    return req if isinstance(req, dict) else json.loads(req)
