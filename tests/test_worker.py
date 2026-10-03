"""The worker end to end (API -> queue -> worker with a fake slicer -> artifacts), and every failure class."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path

import jsonschema
import pytest

from fabrication_prep import queue
from fabrication_prep.artifacts import FsArtifactStore
from fabrication_prep.canonical import canonical_json, canonical_sha256
from fabrication_prep.profiles import ProfileIntegrityError, get_catalog
from fabrication_prep.settings import get_settings
from fabrication_prep.slicer import SlicerError, probe_version, subprocess_runner
from fabrication_prep.vocab import get_vocabulary
from fabrication_prep.worker import Worker, check_slicer
from tests.slicer_fakes import BASE, STL, FakeRunner, bundle_files, job_body, sha, sidecar_for, transport

SCHEMA = json.loads(
    (Path(__file__).parent.parent / "fabrication_prep" / "schemas" / "slicer-variables.schema.json").read_text()
)


def make_worker(runner=None, files=None, store=None):
    s = get_settings()
    return Worker(
        s,
        get_catalog(),
        get_vocabulary(),
        store or FsArtifactStore(s.artifact_fs_root),
        http=transport(files if files is not None else bundle_files()),
        runner=runner or FakeRunner(),
        worker_id="w-test",
    )


def submit(client, auth_header, body=None):
    r = client.post("/v1/slice-jobs", json=body or job_body(), headers=auth_header())
    assert r.status_code == 202, r.text
    return r.json()["id"]


def state(job_id):
    import uuid

    return queue.get_job("service-account:pravara", uuid.UUID(job_id))


def stored(sha256: str) -> bytes:
    s = get_settings()
    return (Path(s.artifact_fs_root) / sha256[:2] / sha256).read_bytes()


def test_klipper_job_end_to_end(client, auth_header, admin_conn):
    body = job_body(
        overrides={"wall_loops": 4, "sparse_infill_density": 40},
        requirements={"process_parameters": {"wall_loops": {"min": 3}}},
    )
    job_id = submit(client, auth_header, body)
    runner = FakeRunner()
    assert make_worker(runner).run_once()
    job = state(job_id)
    assert job["status"] == "succeeded", (job["error_code"], job["error_message"])
    cmd = runner.calls[0]
    assert "--export-3mf" not in cmd and cmd[cmd.index("--slice") + 1] == "0"
    gcode = stored(job["output_sha256"])
    assert hashlib.sha256(gcode).hexdigest() == job["output_sha256"]
    doc = json.loads(stored(job["slicer_variables_sha256"]))
    jsonschema.validate(doc, SCHEMA)
    assert canonical_json(doc) == stored(job["slicer_variables_sha256"])  # stored canonically
    assert doc["effective"]["wall_loops"] == 4 and doc["effective"]["sparse_infill_density"] == 40
    assert doc["effective_sha256"] == canonical_sha256(doc["effective"])
    assert doc["overrides"] == {"wall_loops": 4, "sparse_infill_density": 40}
    catalog = get_catalog()
    assert doc["profiles"]["process"]["sha256"] == catalog.resolve("process", "standard-0.20-klipper").sha256
    assert doc["generator_output"]["instance_id"] == json.loads(sidecar_for(STL))["instance_id"]
    assert doc["input"] == {"sha256": sha(STL), "media_type": "model/stl", "bytes": len(STL)}
    assert doc["slicer"] == {"name": "OrcaSlicer", "version": "2.4.2"}
    assert doc["estimates"] == {
        "print_time_s": 2314,
        "filament_g": 5.22,
        "filament_mm": 1779.21,
        "filament_cm3": 4.28,
        "source": "gcode",
    }
    assert doc["output"]["media_type"] == "text/x-gcode" and doc["output"]["gcode_sha256"] == job["output_sha256"]
    topics = [r[0] for r in admin_conn.execute("SELECT topic FROM outbox").fetchall()]
    assert topics == ["slice_job.succeeded"]
    assert not any(Path(get_settings().worker_workdir).glob(f"{job_id}*"))  # workdir removed


def test_bambu_job_produces_a_sliced_3mf(client, auth_header):
    job_id = submit(client, auth_header, job_body("bambu_3mf", with_sidecar=False))
    runner = FakeRunner()
    make_worker(runner).run_once()
    job = state(job_id)
    assert job["status"] == "succeeded"
    assert runner.calls[0][runner.calls[0].index("--export-3mf") + 1] == "result.gcode.3mf"
    doc = json.loads(stored(job["slicer_variables_sha256"]))
    jsonschema.validate(doc, SCHEMA)
    assert "generator_output" not in doc
    assert doc["output"]["media_type"] == "model/3mf" and doc["output"]["filename"] == "plate_1.gcode.3mf"
    assert doc["estimates"]["print_time_s"] == 1941 and doc["estimates"]["source"] == "slice_info.config+gcode"
    assert doc["warnings"][0]["msg"] == "bed_temperature_too_high_than_filament"
    assert doc["output"]["gcode_sha256"] != doc["output"]["sha256"]


@pytest.mark.parametrize(
    ("files", "code"),
    [
        (
            {BASE + "part.stl": b"other bytes", BASE + "part.stl.variables.json": sidecar_for(STL)},
            "input_digest_mismatch",
        ),
        ({BASE + "part.stl": 404}, "input_gone"),
        ({BASE + "part.stl": 302}, "input_redirect"),
        ({BASE + "part.stl": 403}, "input_refused"),
    ],
)
def test_permanent_input_failures(client, auth_header, admin_conn, files, code):
    job_id = submit(client, auth_header)
    make_worker(files=files).run_once()
    job = state(job_id)
    assert job["status"] == "failed" and job["error_code"] == code
    assert admin_conn.execute("SELECT topic FROM outbox").fetchone()[0] == "slice_job.failed"


def test_sidecar_must_describe_the_input(client, auth_header):
    bad_side = sidecar_for(STL, list_geometry=False)
    body = job_body()
    body["input"]["variables"]["sha256"] = sha(bad_side)
    job_id = submit(client, auth_header, body)
    make_worker(files={BASE + "part.stl": STL, BASE + "part.stl.variables.json": bad_side}).run_once()
    assert state(job_id)["error_code"] == "sidecar_mismatch"


def test_sidecar_with_wrong_digests_is_invalid(client, auth_header):
    doc = json.loads(sidecar_for(STL))
    doc["variables"][0]["value"] = 31  # contents no longer match variables_sha256
    side = json.dumps(doc).encode()
    body = job_body()
    body["input"]["variables"]["sha256"] = sha(side)
    job_id = submit(client, auth_header, body)
    make_worker(files={BASE + "part.stl": STL, BASE + "part.stl.variables.json": side}).run_once()
    assert state(job_id)["error_code"] == "sidecar_invalid"
    side = b'{"format": "something-else"}'
    body["input"]["variables"]["sha256"] = sha(side)
    job2 = submit(client, auth_header, body)
    make_worker(files={BASE + "part.stl": STL, BASE + "part.stl.variables.json": side}).run_once()
    assert state(job2)["error_code"] == "sidecar_invalid"


def test_transient_input_failure_retries_then_dead_letters(client, auth_header, admin_conn):
    job_id = submit(client, auth_header)
    w = make_worker(files={BASE + "part.stl": 503})
    for attempt in (1, 2):
        w.run_once()
        job = state(job_id)
        assert job["status"] == "queued" and job["attempts"] == attempt and job["error_code"] == "input_unavailable"
    w.run_once()
    job = state(job_id)
    assert job["status"] == "dead_lettered" and job["attempts"] == 3
    assert admin_conn.execute("SELECT topic FROM outbox").fetchone()[0] == "slice_job.dead_lettered"


@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("timeout", "queued", "slicer_timeout"),
        ("signal", "queued", "slicer_failed"),
        ("reject", "failed", "slicer_rejected"),
        ("no_output", "failed", "slicer_no_output"),
        ("abort", "queued", "lease_lost"),
    ],
)
def test_slicer_failure_classes(client, auth_header, mode, status, code):
    job_id = submit(client, auth_header)
    make_worker(FakeRunner(mode)).run_once()
    job = state(job_id)
    assert (job["status"], job["error_code"]) == (status, code)
    if mode == "reject":
        assert "process not compatible" in job["error_message"]


def test_corrupt_3mf_and_version_mismatch(client, auth_header):
    a = submit(client, auth_header, job_body("bambu_3mf"))
    make_worker(FakeRunner("bad_md5")).run_once()
    assert state(a)["error_code"] == "slicer_output_corrupt"
    b = submit(client, auth_header)
    make_worker(FakeRunner(version="2.5.0")).run_once()
    assert state(b)["error_code"] == "slicer_version_mismatch"


def test_requirements_rechecked_against_the_slicers_own_record(client, auth_header):
    # The A1 process does not set default_jerk, so only the slicer's config dump can prove it.
    body = job_body("bambu_3mf", requirements={"process_parameters": {"default_jerk": {"max": 10}}})
    job_id = submit(client, auth_header, body)
    make_worker(FakeRunner(dump_overrides={"default_jerk": "20"})).run_once()
    job = state(job_id)
    assert job["status"] == "failed" and job["error_code"] == "requirements_not_met"
    assert "default_jerk=20.0 (slicer)" in job["error_message"]


def test_profile_digest_drift_is_refused(client, auth_header, admin_conn):
    job_id = submit(client, auth_header)
    admin_conn.execute("SELECT set_config('fabrication_prep.worker', 'on', false)")
    admin_conn.execute(
        "UPDATE slice_jobs SET request = "
        "jsonb_set(request, '{resolved_profiles,process,sha256}', to_jsonb(repeat('0', 64)))"
    )
    make_worker().run_once()
    assert state(job_id)["error_code"] == "profile_unavailable"


def test_result_discarded_when_the_lease_moved(client, auth_header, admin_conn):
    job_id = submit(client, auth_header)
    real = FakeRunner()

    def steal(cmd, cwd, timeout, abort):
        result = real(cmd, cwd, timeout, abort)
        admin_conn.execute("SELECT set_config('fabrication_prep.worker', 'on', false)")
        admin_conn.execute("UPDATE slice_jobs SET lease_owner = 'thief'")
        return result

    make_worker(steal).run_once()
    job = state(job_id)
    assert job["status"] == "running" and job["lease_owner"] == "thief" and job["output_sha256"] is None


def test_unexpected_error_is_retried(client, auth_header):
    class Broken(FsArtifactStore):
        def put_file(self, *a, **k):
            raise RuntimeError("disk on fire")

    job_id = submit(client, auth_header)
    make_worker(store=Broken(get_settings().artifact_fs_root)).run_once()
    job = state(job_id)
    assert job["status"] == "queued" and job["error_code"] == "internal_error"
    assert "disk on fire" not in job["error_message"]


def test_idle_worker_and_stop(clean_db):
    w = make_worker()
    assert w.run_once() is False
    t = threading.Thread(target=w.run_forever, kwargs={"install_signals": False})
    t.start()
    time.sleep(0.3)
    w.stop()
    t.join(timeout=10)
    assert not t.is_alive()
    assert Path(get_settings().worker_heartbeat_file).exists()


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "orca"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_probe_version_and_startup_check(tmp_path):
    good = _script(tmp_path, 'echo "OrcaSlicer-2.4.2:"\necho "Usage: orca-slicer"\n')
    assert probe_version(good) == "2.4.2"
    s = get_settings().model_copy(update={"orcaslicer_bin": good})
    assert check_slicer(s, get_catalog()) == "2.4.2"
    newer = _script(tmp_path, 'echo "OrcaSlicer-2.5.0:"\n')
    with pytest.raises(ProfileIntegrityError):
        check_slicer(get_settings().model_copy(update={"orcaslicer_bin": newer}), get_catalog())
    with pytest.raises(SlicerError):
        probe_version(str(tmp_path / "missing"))
    with pytest.raises(SlicerError):
        probe_version(_script(tmp_path, "echo nothing\n"))


def test_subprocess_runner_success_timeout_and_abort(tmp_path):
    ok = subprocess_runner(["/bin/sh", "-c", "echo hello; exit 3"], tmp_path, 10, lambda: False)
    assert ok.returncode == 3 and ok.log_tail == ["hello"] and not ok.timed_out
    slow = subprocess_runner(["/bin/sh", "-c", "sleep 30"], tmp_path, 1, lambda: False)
    assert slow.timed_out
    started = time.monotonic()
    aborted = subprocess_runner(["/bin/sh", "-c", "sleep 30"], tmp_path, 60, lambda: time.monotonic() - started > 1)
    assert aborted.aborted and time.monotonic() - started < 10
    env_dump = subprocess_runner(["/bin/sh", "-c", "env"], tmp_path, 10, lambda: False)
    names = {line.split("=", 1)[0] for line in env_dump.log_tail}
    assert names <= {"PATH", "HOME", "LANG", "TMPDIR", "PWD", "SHLVL", "_", "OLDPWD"}
    assert "FABRICATION_PREP_TEST_APP_URL" not in names and os.environ.get("ARTIFACT_URL_KEYS")
