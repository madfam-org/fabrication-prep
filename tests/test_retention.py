"""Artifact retention: the sweep deletes bytes past the window and marks the rows, never touches rows a worker is
recording, fails visibly without marking, and the API stops issuing URLs for expired artifacts (410 on download)."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

from fabrication_prep import queue, retention
from fabrication_prep.artifacts import FsArtifactStore, file_sha256
from fabrication_prep.settings import Settings, get_settings
from tests.test_worker import make_worker, state, submit

APP_URL = os.environ.get("FABRICATION_PREP_TEST_APP_URL", "")


def put(store: FsArtifactStore, tmp_path: Path, content: bytes, name: str = "plate_1.gcode") -> str:
    path = tmp_path / f"src-{len(content)}-{name}"
    path.write_bytes(content)
    sha = file_sha256(path)
    queue.store_artifact(store, path, sha, "text/x-gcode", name)
    return sha


def age(admin_conn, sha: str, days: int) -> None:
    admin_conn.execute(
        "UPDATE artifacts SET last_used_at = now() - make_interval(days => %s) WHERE sha256 = %s", (days, sha)
    )


def row(admin_conn, sha: str) -> dict:
    cur = admin_conn.execute("SELECT last_used_at, expired_at FROM artifacts WHERE sha256 = %s", (sha,))
    last_used_at, expired_at = cur.fetchone()
    return {"last_used_at": last_used_at, "expired_at": expired_at}


@pytest.fixture
def store(tmp_path) -> FsArtifactStore:
    return FsArtifactStore(tmp_path / "store")


def test_sweep_expires_only_artifacts_past_the_window(clean_db, admin_conn, store, tmp_path):
    old, recent, edge = (put(store, tmp_path, c) for c in (b"old\n", b"recent\n", b"edge\n"))
    age(admin_conn, old, 31)
    age(admin_conn, recent, 2)
    age(admin_conn, edge, 29)
    result = retention.sweep(store, 30, 100)
    assert (result.expired, result.complete) == (1, True)
    assert not store.exists(old) and row(admin_conn, old)["expired_at"] is not None
    for kept in (recent, edge):
        assert store.exists(kept) and row(admin_conn, kept)["expired_at"] is None
    assert retention.sweep(store, 30, 100).expired == 0  # already expired rows are not selected again


def test_sweep_works_in_bounded_batches(clean_db, admin_conn, store, tmp_path, monkeypatch):
    digests = [put(store, tmp_path, f"g{i}\n".encode()) for i in range(5)]
    for sha in digests:
        age(admin_conn, sha, 40)
    monkeypatch.setattr(retention, "MAX_BATCHES_PER_SWEEP", 2)
    first = retention.sweep(store, 30, 2)
    assert (first.expired, first.batches, first.complete) == (4, 2, False)
    second = retention.sweep(store, 30, 2)
    assert (second.expired, second.complete) == (1, True)
    assert not any(store.exists(sha) for sha in digests)


def test_zero_days_disables_the_sweep(clean_db, admin_conn, store, tmp_path):
    sha = put(store, tmp_path, b"keep forever\n")
    age(admin_conn, sha, 4000)
    assert retention.sweep(store, 0, 100) == retention.SweepResult(0, 0, True)
    assert store.exists(sha)


def test_reproduced_bytes_are_uploaded_again_and_unexpired(clean_db, admin_conn, store, tmp_path):
    sha = put(store, tmp_path, b"same bytes\n")
    age(admin_conn, sha, 31)
    assert retention.sweep(store, 30, 100).expired == 1 and not store.exists(sha)
    assert put(store, tmp_path, b"same bytes\n") == sha  # a later job slices the identical output
    after = row(admin_conn, sha)
    assert store.exists(sha) and after["expired_at"] is None
    assert retention.sweep(store, 30, 100).expired == 0  # last_used_at moved to now


def test_sweep_skips_a_row_a_worker_is_recording(clean_db, admin_conn, store, tmp_path):
    sha = put(store, tmp_path, b"in flight\n")
    age(admin_conn, sha, 31)
    with psycopg.connect(APP_URL) as conn:  # hold the row lock, as queue.store_artifact does while uploading
        conn.execute("SELECT set_config('fabrication_prep.worker', 'on', false)")
        conn.execute("SELECT sha256 FROM artifacts WHERE sha256 = %s FOR UPDATE", (sha,))
        assert retention.sweep(store, 30, 100).expired == 0
        assert store.exists(sha)
    assert retention.sweep(store, 30, 100).expired == 1  # once the lock is gone the row is due again


class FailingDelete(FsArtifactStore):
    def delete(self, sha256: str) -> None:
        raise PermissionError("delete refused by the store")


def test_a_failed_delete_marks_nothing_and_is_logged(clean_db, admin_conn, store, tmp_path, caplog):
    sha = put(store, tmp_path, b"refused\n")
    age(admin_conn, sha, 31)
    failing = FailingDelete(store.root)
    with pytest.raises(PermissionError):
        retention.sweep(failing, 30, 100)
    assert row(admin_conn, sha)["expired_at"] is None and store.exists(sha)
    clock = [1000.0]
    schedule = retention.RetentionSchedule(failing, 30, 100, 3600, clock=lambda: clock[0])
    with caplog.at_level(logging.ERROR, logger="fabrication_prep.retention"):
        assert schedule.maybe_run() is None
    assert "artifact retention sweep failed: PermissionError" in caplog.text
    clock[0] += 3600
    schedule.store = store  # the store recovers: the next interval retries and succeeds
    assert schedule.maybe_run().expired == 1


def test_schedule_runs_at_most_once_per_interval(clean_db, store, caplog):
    clock = [0.0]
    schedule = retention.RetentionSchedule(store, 30, 100, 3600, clock=lambda: clock[0])
    assert schedule.maybe_run() == retention.SweepResult(0, 1, True)
    clock[0] += 3599
    assert schedule.maybe_run() is None
    clock[0] += 1
    assert schedule.maybe_run() is not None
    with caplog.at_level(logging.WARNING, logger="fabrication_prep.retention"):
        disabled = retention.RetentionSchedule(store, 0, 100, 3600, clock=lambda: clock[0])
    assert "retention is disabled" in caplog.text
    assert disabled.maybe_run() is None


def test_worker_loop_runs_the_sweep(clean_db, admin_conn, tmp_path):
    worker = make_worker()
    sha = put(worker.store, tmp_path, b"swept by the worker\n")
    age(admin_conn, sha, 31)
    thread = threading.Thread(target=worker.run_forever, kwargs={"install_signals": False})
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while row(admin_conn, sha)["expired_at"] is None and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        worker.stop()
        thread.join(timeout=10)
    assert row(admin_conn, sha)["expired_at"] is not None and not worker.store.exists(sha)


def test_expired_job_has_no_urls_and_downloads_are_410(client, auth_header, admin_conn):
    job_id = submit(client, auth_header)
    assert make_worker().run_once()
    view = client.get(f"/v1/slice-jobs/{job_id}", headers=auth_header()).json()
    url = urlsplit(view["output"]["url"])
    job = state(job_id)
    for sha in (job["output_sha256"], job["slicer_variables_sha256"]):
        age(admin_conn, sha, 31)
    assert retention.sweep(FsArtifactStore(get_settings().artifact_fs_root), 30, 100).expired == 2
    expired = client.get(f"/v1/slice-jobs/{job_id}", headers=auth_header()).json()
    assert expired["status"] == "succeeded"
    assert expired["output"] is None and expired["slicer_variables"] is None
    assert expired["artifacts_expired_at"].endswith("Z")
    download = client.get(f"{url.path}?{url.query}")
    assert download.status_code == 410
    assert [e["code"] for e in download.json()["errors"]] == ["artifact_expired"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_retention_days", -1),
        ("artifact_retention_days", 3651),
        ("artifact_gc_interval_seconds", 59),
        ("artifact_gc_batch", 0),
        ("artifact_gc_batch", 1001),
    ],
)
def test_retention_settings_are_bounded(field, value):
    with pytest.raises(RuntimeError):
        Settings(jwks_path="", fabrication_prep_env="test", **{field: value}).validate_runtime()


def test_retention_defaults():
    s = Settings(jwks_path="", fabrication_prep_env="test")
    assert (s.artifact_retention_days, s.artifact_gc_interval_seconds, s.artifact_gc_batch) == (30, 3600, 100)
