"""The slice worker: claim a job, fetch the bundle, slice with the pinned profiles, store, finish.

One job at a time per process (slicing is CPU-bound; scale with replicas). Between jobs the worker also runs the
artifact retention sweep (``fabrication_prep.retention``) at most once per ``ARTIFACT_GC_INTERVAL_SECONDS``.
The lease is renewed by a background thread while the slicer runs; if renewal fails, the slicer is killed and
the result discarded. At startup the worker checks that the CLI reports the OrcaSlicer version the profiles were
built for and refuses to start otherwise — profiles are pinned to a slicer version.
"""

from __future__ import annotations

import logging
import shutil
import signal
import socket
import threading
import time
import uuid
from pathlib import Path

import httpx

from . import queue
from .artifacts import ArtifactStore, file_sha256
from .canonical import canonical_json, sha256_hex
from .effective import apply_overrides, effective_values
from .inputs import InputError, fetch_bundle
from .profiles import Catalog, ProfileIntegrityError
from .retention import RetentionSchedule
from .settings import Settings
from .slicer import Runner, SlicerError, run_slicer, subprocess_runner
from .slicer_variables import build as build_variables
from .validation import ResolvedProfiles, check_against_requirements, effective_requirements
from .vocab import Vocabulary

log = logging.getLogger("fabrication_prep.worker")


class PermanentJobError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Worker:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        vocab: Vocabulary,
        store: ArtifactStore,
        http: httpx.Client | None = None,
        runner: Runner = subprocess_runner,
        worker_id: str | None = None,
    ):
        self.s = settings
        self.catalog = catalog
        self.vocab = vocab
        self.store = store
        self.http = http or httpx.Client(timeout=settings.input_timeout_seconds)
        self.runner = runner
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.retention = RetentionSchedule(
            store, settings.artifact_retention_days, settings.artifact_gc_batch, settings.artifact_gc_interval_seconds
        )
        self._stop = threading.Event()

    def stop(self, *_args) -> None:
        self._stop.set()

    def heartbeat(self) -> None:
        path = Path(self.s.worker_heartbeat_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def run_forever(self, install_signals: bool = True) -> None:
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        log.info("worker started", extra={"worker": self.worker_id})
        while not self._stop.is_set():
            self.heartbeat()
            busy = self.run_once()
            # Between jobs, at most once per ARTIFACT_GC_INTERVAL_SECONDS (fabrication_prep.retention).
            self.retention.maybe_run()
            if not busy:
                self._stop.wait(self.s.worker_poll_seconds)
        log.info("worker stopped", extra={"worker": self.worker_id})

    def run_once(self) -> bool:
        """Reap expired leases, then process one job. True when a job was processed."""
        queue.reap_expired_leases()
        job = queue.claim(self.worker_id, self.s.worker_lease_seconds)
        if job is None:
            return False
        self.process(job)
        return True

    def _profiles(self, request: dict) -> ResolvedProfiles:
        found = {}
        for kind in ("printer", "process", "filament"):
            ref = request["resolved_profiles"][kind]
            profile = self.catalog.get(kind, ref["id"], ref["version"])
            if profile is None or profile.sha256 != ref["sha256"]:
                raise PermanentJobError(
                    "profile_unavailable",
                    f"{kind} profile {ref['id']}@{ref['version']} "
                    "is not shipped by this worker with the recorded digest",
                )
            found[kind] = profile
        return ResolvedProfiles(**found)

    def process(self, job: dict) -> None:
        extra = {"job_id": str(job["id"]), "attempt": job["attempts"], "worker": self.worker_id}
        log.info("job claimed", extra=extra)
        lost = threading.Event()
        stop_renewal = threading.Event()

        def renew() -> None:
            interval = max(5.0, self.s.worker_lease_seconds / 3)
            while not stop_renewal.wait(interval):
                try:
                    ok = queue.renew_lease(job["id"], self.worker_id, self.s.worker_lease_seconds)
                except Exception:  # noqa: BLE001 - any renewal failure means the lease is not assured
                    ok = False
                if not ok:
                    lost.set()
                    return
                self.heartbeat()

        renewer = threading.Thread(target=renew, name=f"lease-{job['id']}", daemon=True)
        renewer.start()
        workdir = Path(self.s.worker_workdir) / str(job["id"])
        try:
            shutil.rmtree(workdir, ignore_errors=True)
            workdir.mkdir(parents=True)
            outcome = self._slice(job, workdir, lost.is_set)
            stop_renewal.set()
            if lost.is_set() or not queue.complete(job, self.worker_id, *outcome):
                log.warning("lease lost; result discarded", extra=extra)
                return
            log.info("job succeeded", extra=extra)
        except (PermanentJobError, InputError, SlicerError) as exc:
            stop_renewal.set()
            transient = getattr(exc, "transient", False)
            status = queue.fail(job, self.worker_id, exc.code, exc.message, transient, self.s.retry_backoff_seconds)
            log.warning("job %s: %s", status or "lease lost", exc.code, extra=extra)
        except Exception as exc:  # noqa: BLE001 - an unexpected error is retried, then dead-lettered
            stop_renewal.set()
            log.exception("unexpected error in job", extra=extra)
            queue.fail(
                job,
                self.worker_id,
                "internal_error",
                f"unexpected {type(exc).__name__}",
                True,
                self.s.retry_backoff_seconds,
            )
        finally:
            stop_renewal.set()
            renewer.join(timeout=5)
            shutil.rmtree(workdir, ignore_errors=True)

    def _slice(self, job: dict, workdir: Path, lease_lost) -> tuple[str, str, dict]:
        request = queue.job_request(job)
        profiles = self._profiles(request)
        model, input_bytes, instance = fetch_bundle(self.http, request, workdir, self.s)
        docs = apply_overrides(profiles.docs(), request.get("overrides") or {}, self.vocab)
        for kind, doc in docs.items():
            (workdir / f"{kind}.json").write_bytes(canonical_json(doc))
        out = run_slicer(
            self.s.orcaslicer_bin,
            workdir,
            model,
            request["target"],
            self.s.slice_timeout_seconds,
            lease_lost,
            self.runner,
        )
        if out.orcaslicer_version != self.catalog.orcaslicer["version"]:
            raise PermanentJobError(
                "slicer_version_mismatch",
                f"G-code reports OrcaSlicer {out.orcaslicer_version}; "
                f"profiles are pinned to {self.catalog.orcaslicer['version']}",
            )
        effective = effective_values(out.config, self.vocab)
        part = request.get("part") or (instance.part if instance else None)
        req = effective_requirements(request.get("requirements"), part)
        problems = check_against_requirements(effective, req, self.vocab, "slicer", "/effective")
        if problems:
            raise PermanentJobError("requirements_not_met", "; ".join(p.message for p in problems))
        output_sha = file_sha256(out.path)
        output_size = out.path.stat().st_size
        queue.store_artifact(self.store, out.path, output_sha, out.media_type, out.filename)
        output = {
            "sha256": output_sha,
            "media_type": out.media_type,
            "bytes": output_size,
            "filename": out.filename,
            "gcode_sha256": sha256_hex(out.gcode.encode("utf-8")),
        }
        doc = build_variables(
            job_id=str(job["id"]),
            request=request,
            profiles=profiles,
            input_bytes=input_bytes,
            instance=instance,
            effective=effective,
            requirements=req,
            orcaslicer_version=out.orcaslicer_version,
            output=output,
            estimates=out.estimates,
            warnings=out.warnings,
        )
        doc_path = workdir / "slicer-variables.json"
        doc_path.write_bytes(canonical_json(doc))
        doc_sha = file_sha256(doc_path)
        queue.store_artifact(self.store, doc_path, doc_sha, "application/json", "slicer-variables.json")
        summary = {
            "estimates": out.estimates,
            "effective_sha256": doc["effective_sha256"],
            "orcaslicer_version": out.orcaslicer_version,
            "warnings": len(out.warnings),
        }
        return output_sha, doc_sha, summary


def check_slicer(settings: Settings, catalog: Catalog) -> str:
    from .slicer import probe_version

    version = probe_version(settings.orcaslicer_bin)
    if version != catalog.orcaslicer["version"]:
        raise ProfileIntegrityError(
            f"OrcaSlicer {version} is installed; the profiles are pinned to {catalog.orcaslicer['version']}"
        )
    return version


def main_loop(settings: Settings, catalog: Catalog, vocab: Vocabulary, store: ArtifactStore) -> None:
    started = time.monotonic()
    version = check_slicer(settings, catalog)
    log.info("OrcaSlicer %s ready in %.1fs", version, time.monotonic() - started)
    Worker(settings, catalog, vocab, store).run_forever()
