"""Artifact retention: delete the bytes of artifacts no job has produced for ``ARTIFACT_RETENTION_DAYS``.

Why bytes may go. Every consumer of an artifact's bytes takes them within hours of the slice: pravara records the
digests (output, G-code, slicer-variables, profiles) in its dispatch state when the job succeeds, reads only those
digests when it writes the ManufacturingRecord for the passport, and hands the printer a fresh signed URL when it
dispatches. A re-print is a new dispatch and a new slice job. The rows stay: a job keeps its digests and its
``slicer_variables_sha256`` after the bytes are gone, so the record of what was sliced is permanent.

How. The worker runs one sweep at most every ``ARTIFACT_GC_INTERVAL_SECONDS``, between jobs. A sweep works in
bounded batches; each batch is one transaction that

1. locks up to ``ARTIFACT_GC_BATCH`` rows whose ``last_used_at`` is older than the retention window and whose bytes
   are not yet expired (``FOR UPDATE SKIP LOCKED``: rows a worker is recording right now are skipped, see
   ``queue.store_artifact``);
2. deletes their bytes from the store (idempotent);
3. marks them ``expired_at = now()`` and commits.

A failed delete raises before the commit, so nothing is marked and the next sweep retries the whole batch. Bytes
already deleted in that batch are then briefly unmarked: their signed URLs answer "no longer stored" until the retry
marks them. After expiry the API stops issuing URLs for the artifact (the job view shows ``artifacts_expired_at``)
and the download route answers 410.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import db
from .artifacts import ArtifactStore

log = logging.getLogger("fabrication_prep.retention")

MAX_BATCHES_PER_SWEEP = 20


@dataclass(frozen=True)
class SweepResult:
    expired: int
    batches: int
    complete: bool  # False when the sweep stopped at MAX_BATCHES_PER_SWEEP with candidates left


def expire_batch(store: ArtifactStore, retention_days: int, batch: int) -> list[str]:
    """Expire one batch; returns the expired digests (empty when nothing is due)."""
    with db.transaction(worker=True) as cur:
        cur.execute(
            """SELECT sha256 FROM artifacts
               WHERE expired_at IS NULL AND last_used_at < now() - make_interval(days => %s)
               ORDER BY last_used_at
               LIMIT %s
               FOR UPDATE SKIP LOCKED""",
            (retention_days, batch),
        )
        digests = [row["sha256"] for row in cur.fetchall()]
        for sha256 in digests:
            store.delete(sha256)
        if digests:
            cur.execute("UPDATE artifacts SET expired_at = now() WHERE sha256 = ANY(%s)", (digests,))
    return digests


def sweep(store: ArtifactStore, retention_days: int, batch: int) -> SweepResult:
    """Expire everything due, in at most ``MAX_BATCHES_PER_SWEEP`` batches. ``retention_days`` 0 disables it."""
    if retention_days <= 0:
        return SweepResult(0, 0, True)
    expired = batches = 0
    while batches < MAX_BATCHES_PER_SWEEP:
        digests = expire_batch(store, retention_days, batch)
        batches += 1
        expired += len(digests)
        if len(digests) < batch:
            return SweepResult(expired, batches, True)
    return SweepResult(expired, batches, False)


class RetentionSchedule:
    """Runs ``sweep`` at most once per interval. A failing sweep is logged with its error and retried at the next
    interval; it never stops the worker from slicing."""

    def __init__(self, store: ArtifactStore, retention_days: int, batch: int, interval_seconds: float, clock=None):
        self.store = store
        self.retention_days = retention_days
        self.batch = batch
        self.interval = interval_seconds
        self.clock = clock or time.monotonic
        self._next = 0.0  # the first sweep runs on the first idle poll after start
        if retention_days <= 0:
            log.warning("artifact retention is disabled (ARTIFACT_RETENTION_DAYS=0): artifact bytes are kept forever")

    def maybe_run(self) -> SweepResult | None:
        if self.retention_days <= 0 or self.clock() < self._next:
            return None
        self._next = self.clock() + self.interval
        try:
            result = sweep(self.store, self.retention_days, self.batch)
        except Exception as exc:  # noqa: BLE001 - logged with its type; retried at the next interval
            log.error("artifact retention sweep failed: %s", type(exc).__name__, exc_info=True)
            return None
        if result.expired or not result.complete:
            log.info(
                "artifact retention sweep: %d expired in %d batch(es)%s",
                result.expired,
                result.batches,
                "" if result.complete else "; more are due and the next sweep continues",
            )
        return result
