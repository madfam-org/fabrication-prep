"""The Postgres queue: SKIP LOCKED claims, leases, reaping, retries, dead-letter, outbox, RLS, grants."""

from __future__ import annotations

import os
import threading

import psycopg
import pytest

from fabrication_prep import db, queue
from fabrication_prep.canonical import canonical_sha256

APP_URL = os.environ.get("FABRICATION_PREP_TEST_APP_URL", "")


def enqueue(owner="service-account:a", key=None, max_attempts=3, body=None):
    request = body or {"target": "klipper_gcode", "n": owner}
    job, created = queue.enqueue(
        owner=owner,
        tenant_id=None,
        idempotency_key=key,
        request=request,
        request_sha256=canonical_sha256(request),
        target="klipper_gcode",
        max_attempts=max_attempts,
    )
    return job


def outbox(admin_conn):
    return admin_conn.execute("SELECT topic, payload FROM outbox ORDER BY id").fetchall()


def test_claim_marks_running_with_a_lease(clean_db):
    job = enqueue()
    claimed = queue.claim("w1", 60)
    assert claimed["id"] == job["id"] and claimed["status"] == "running" and claimed["attempts"] == 1
    assert claimed["lease_owner"] == "w1" and claimed["lease_expires_at"] is not None
    assert queue.claim("w2", 60) is None


def test_concurrent_claims_never_share_a_job(clean_db):
    ids = {enqueue(owner=f"o{i}")["id"] for i in range(8)}
    got: list = []
    lock = threading.Lock()

    def worker(name):
        while True:
            job = queue.claim(name, 60)
            if job is None:
                return
            with lock:
                got.append(job["id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids) and len(got) == len(set(got))


def test_skip_locked_lets_a_second_claimer_pass_a_locked_row(clean_db):
    first, second = enqueue(owner="a"), enqueue(owner="b")
    with psycopg.connect(APP_URL) as conn:  # hold a row lock on the first job, as a claiming worker would
        conn.execute("SELECT set_config('fabrication_prep.worker', 'on', false)")
        conn.execute("SELECT id FROM slice_jobs WHERE id = %s FOR UPDATE", (first["id"],))
        claimed = queue.claim("w2", 60)
        assert claimed["id"] == second["id"]


def test_renew_complete_and_lost_lease(clean_db, admin_conn):
    enqueue()
    job = queue.claim("w1", 60)
    assert queue.renew_lease(job["id"], "w1", 60)
    assert not queue.renew_lease(job["id"], "intruder", 60)
    queue.record_artifact("a" * 64, "text/x-gcode", 10, "plate_1.gcode")
    queue.record_artifact("b" * 64, "application/json", 5, "slicer-variables.json")
    assert not queue.complete(job, "intruder", "a" * 64, "b" * 64, {})
    assert queue.complete(job, "w1", "a" * 64, "b" * 64, {"estimates": {"print_time_s": 1}})
    assert not queue.complete(job, "w1", "a" * 64, "b" * 64, {})  # already finished
    row = queue.get_job("service-account:a", job["id"])
    assert row["status"] == "succeeded" and row["lease_owner"] is None and row["finished_at"] is not None
    assert [t for t, _ in outbox(admin_conn)] == ["slice_job.succeeded"]


def test_transient_failure_retries_then_dead_letters(clean_db, admin_conn):
    enqueue(max_attempts=2)
    job = queue.claim("w1", 60)
    assert queue.fail(job, "w1", "input_unavailable", "503", True, 0) == "queued"
    job = queue.claim("w1", 60)
    assert job["attempts"] == 2
    assert queue.fail(job, "w1", "input_unavailable", "503", True, 0) == "dead_lettered"
    assert [t for t, _ in outbox(admin_conn)] == ["slice_job.dead_lettered"]
    assert queue.claim("w1", 60) is None


def test_backoff_delays_the_retry(clean_db):
    enqueue()
    job = queue.claim("w1", 60)
    assert queue.fail(job, "w1", "input_unavailable", "503", True, 3600) == "queued"
    assert queue.claim("w1", 60) is None  # run_after is an hour away


def test_permanent_failure_is_final(clean_db, admin_conn):
    enqueue()
    job = queue.claim("w1", 60)
    assert queue.fail(job, "intruder", "x", "y", False, 0) is None
    assert queue.fail(job, "w1", "input_digest_mismatch", "bytes differ", False, 0) == "failed"
    topic, payload = outbox(admin_conn)[0]
    assert topic == "slice_job.failed" and payload["error_code"] == "input_digest_mismatch"


def test_expired_leases_are_reaped(clean_db, admin_conn):
    enqueue(owner="a", max_attempts=1)
    enqueue(owner="b", max_attempts=3)
    first, second = queue.claim("w1", 60), queue.claim("w1", 60)
    # FORCE RLS binds the owner too: without a context its UPDATE reaches nothing.
    assert admin_conn.execute("UPDATE slice_jobs SET lease_expires_at = now()").rowcount == 0
    admin_conn.execute("SELECT set_config('fabrication_prep.worker', 'on', false)")
    admin_conn.execute("UPDATE slice_jobs SET lease_expires_at = now() - interval '1 second'")
    assert queue.reap_expired_leases() == 2
    states = dict(admin_conn.execute("SELECT owner_sub, status FROM slice_jobs").fetchall())
    assert states == {"a": "dead_lettered", "b": "queued"}
    assert [t for t, _ in outbox(admin_conn)] == ["slice_job.dead_lettered"]
    assert queue.claim("w2", 60)["id"] in (first["id"], second["id"])


def test_idempotent_enqueue(clean_db):
    a = enqueue(key="k1")
    again, created = queue.enqueue(
        owner="service-account:a",
        tenant_id=None,
        idempotency_key="k1",
        request=a["request"],
        request_sha256=a["request_sha256"],
        target="klipper_gcode",
        max_attempts=3,
    )
    assert not created and again["id"] == a["id"]
    with pytest.raises(queue.IdempotencyConflict):
        enqueue(key="k1", body={"different": True})


def test_rls_hides_other_owners_jobs_from_the_runtime_role(clean_db):
    enqueue(owner="a")
    enqueue(owner="b")
    with db.transaction(owner="a") as cur:
        cur.execute("SELECT owner_sub FROM slice_jobs")
        assert [r["owner_sub"] for r in cur.fetchall()] == ["a"]
        cur.execute("UPDATE slice_jobs SET status = 'failed' WHERE owner_sub = 'b'")
        assert cur.rowcount == 0
    with db.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM slice_jobs")
        assert cur.fetchone()["n"] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction(owner="a") as cur:
        cur.execute(
            "INSERT INTO slice_jobs (id, owner_sub, request_sha256, request, target, status, max_attempts) "
            "VALUES (gen_random_uuid(), 'b', repeat('a', 64), '{}', 'klipper_gcode', 'queued', 1)"
        )
    with pytest.raises(ValueError):
        with db.transaction(owner="a", worker=True):
            pass


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM outbox",
        "DELETE FROM slice_jobs",
        "TRUNCATE slice_jobs",
        "DELETE FROM artifacts",
        "ALTER TABLE slice_jobs NO FORCE ROW LEVEL SECURITY",
        "UPDATE artifacts SET bytes = 0",
    ],
)
def test_runtime_role_lacks_dangerous_privileges(clean_db, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction(worker=True) as cur:
        cur.execute(statement)


def test_outbox_insert_requires_the_worker_context(clean_db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction(owner="a") as cur:
        cur.execute(
            "INSERT INTO outbox (topic, aggregate_id, payload) VALUES ('slice_job.failed', gen_random_uuid(), '{}')"
        )


def test_role_posture_refuses_the_owner(migrated, pool):
    from psycopg_pool import ConnectionPool

    owner_pool = ConnectionPool(
        migrated, min_size=1, max_size=1, kwargs={"row_factory": psycopg.rows.dict_row}, open=True
    )
    try:
        with pytest.raises(db.UnsafeDatabaseRole):
            db.check_role_posture(owner_pool)
    finally:
        owner_pool.close()
    db.check_role_posture()  # the runtime role passes
