"""API behaviour against PostgreSQL: create, read, ownership, idempotency, 422 detail, profiles, downloads."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from fabrication_prep.worker import Worker
from tests.slicer_fakes import FakeRunner, bundle_files, job_body, transport


def create(client, auth_header, body=None, **kw):
    return client.post("/v1/slice-jobs", json=body or job_body(), headers=auth_header(**kw))


def test_create_and_read(client, auth_header):
    r = create(client, auth_header)
    assert r.status_code == 202, r.text
    job = r.json()
    assert r.headers["location"] == f"/v1/slice-jobs/{job['id']}"
    assert job["status"] == "queued" and job["attempts"] == 0 and job["output"] is None
    assert job["profiles"]["printer"]["id"] == "klipper-corexy-350-0.4" and job["profiles"]["printer"]["version"] == 1
    assert len(job["profiles"]["process"]["sha256"]) == 64
    again = client.get(f"/v1/slice-jobs/{job['id']}", headers=auth_header())
    assert again.status_code == 200 and again.json()["id"] == job["id"]


def test_jobs_are_visible_only_to_their_owner(client, auth_header):
    job = create(client, auth_header, sub="service-account:a").json()
    other = client.get(f"/v1/slice-jobs/{job['id']}", headers=auth_header(sub="service-account:b"))
    assert other.status_code == 404
    missing = client.get(f"/v1/slice-jobs/{uuid.uuid4()}", headers=auth_header(sub="service-account:a"))
    assert missing.status_code == 404
    assert client.get("/v1/slice-jobs/not-a-uuid", headers=auth_header()).status_code == 422


def test_idempotency_key(client, auth_header):
    h = {**auth_header(), "Idempotency-Key": "order-42:item-1"}
    first = client.post("/v1/slice-jobs", json=job_body(), headers=h)
    replay = client.post("/v1/slice-jobs", json=job_body(), headers=h)
    assert first.status_code == 202 and replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    changed = client.post("/v1/slice-jobs", json=job_body(overrides={"wall_loops": 4}), headers=h)
    assert changed.status_code == 409 and changed.json()["errors"][0]["code"] == "idempotency_conflict"
    other_owner = client.post(
        "/v1/slice-jobs",
        json=job_body(),
        headers={**auth_header(sub="service-account:other"), "Idempotency-Key": "order-42:item-1"},
    )
    assert other_owner.status_code == 202 and other_owner.json()["id"] != first.json()["id"]
    bad = client.post("/v1/slice-jobs", json=job_body(), headers={**auth_header(), "Idempotency-Key": "a b"})
    assert bad.status_code == 400


def test_422_names_the_key_and_the_range(client, auth_header):
    body = job_body(
        overrides={"outer_wall_speed": 300},
        requirements={"process_parameters": {"outer_wall_speed": {"min": 20, "max": 150, "unit": "mm/s"}}},
    )
    r = create(client, auth_header, body)
    assert r.status_code == 422
    err = r.json()["errors"][0]
    assert err["code"] == "override_out_of_range" and err["path"] == "/overrides/outer_wall_speed"
    assert err["details"] == {
        "key": "outer_wall_speed",
        "value": 300,
        "unit": "mm/s",
        "source": "override",
        "min": 20,
        "max": 150,
    }
    assert "outer_wall_speed=300" in err["message"] and "[min 20, max 150] mm/s" in err["message"]


def test_schema_errors_are_422_with_paths(client, auth_header):
    body = job_body()
    body["input"]["sha256"] = "XYZ"
    body["surprise"] = 1
    r = create(client, auth_header, body)
    assert r.status_code == 422
    paths = {e["path"] for e in r.json()["errors"]}
    assert "/input/sha256" in paths and "/surprise" in paths
    r = create(client, auth_header, job_body(requirements={"process_parameters": {"wall_loops": {}}}))
    assert r.status_code == 422


def test_input_host_allowlist(client, auth_header):
    body = job_body()
    body["input"]["url"] = "https://elsewhere.test/part.stl"
    body["input"]["variables"]["url"] = "ftp://bundles.test/x.json"
    r = create(client, auth_header, body)
    assert r.status_code == 422
    assert {e["path"] for e in r.json()["errors"]} == {"/input/url", "/input/variables/url"}


def test_profiles_endpoints(client, auth_header):
    listing = client.get("/v1/profiles", headers=auth_header()).json()
    assert listing["orcaslicer"]["version"] == "2.4.2" and len(listing["profiles"]) == 12
    one = client.get("/v1/profiles/process/tpu-safe-0.20-bambu-a1/1", headers=auth_header())
    assert one.status_code == 200 and one.json()["content"]["name"] == "tpu-safe-0.20-bambu-a1@1"
    assert client.get("/v1/profiles/process/nope/1", headers=auth_header()).status_code == 404


def run_worker(target="klipper_gcode"):
    from fabrication_prep.artifacts import FsArtifactStore
    from fabrication_prep.profiles import get_catalog
    from fabrication_prep.settings import get_settings
    from fabrication_prep.vocab import get_vocabulary

    s = get_settings()
    w = Worker(
        s,
        get_catalog(),
        get_vocabulary(),
        FsArtifactStore(s.artifact_fs_root),
        http=transport(bundle_files()),
        runner=FakeRunner(),
        worker_id="test-worker",
    )
    assert w.run_once()


def test_succeeded_job_has_signed_downloads(client, auth_header):
    job = create(client, auth_header, job_body("bambu_3mf")).json()
    run_worker()
    view = client.get(f"/v1/slice-jobs/{job['id']}", headers=auth_header()).json()
    assert view["status"] == "succeeded", view
    out, doc = view["output"], view["slicer_variables"]
    assert out["media_type"] == "model/3mf" and out["filename"] == "plate_1.gcode.3mf"
    assert view["estimates"]["print_time_s"] == 1941
    url = urlsplit(out["url"])
    assert url.scheme == "http" and url.netloc == "testserver" and "sig=" in url.query
    got = client.get(f"{url.path}?{url.query}")
    assert got.status_code == 200 and got.headers["x-content-sha256"] == out["sha256"]
    import hashlib

    assert hashlib.sha256(got.content).hexdigest() == out["sha256"]
    assert got.headers["content-disposition"] == 'attachment; filename="plate_1.gcode.3mf"'
    d = urlsplit(doc["url"])
    assert client.get(f"{d.path}?{d.query}").json()["format"] == "madfam.fabrication-prep.slicer-variables"
    # The signature is bound to the artifact: the output's signature cannot fetch the document.
    assert client.get(f"/v1/artifacts/{doc['sha256']}?{url.query}").status_code == 403
    tampered = url.query.replace("sig=", "sig=0")
    assert client.get(f"{url.path}?{tampered}").status_code == 403
    assert client.get(url.path).json()["errors"][0]["code"] == "signature_missing"
    assert client.get("/v1/artifacts/NOTASHA").status_code == 404


def test_signed_url_for_unknown_artifact_is_404(client):
    from fabrication_prep.settings import get_settings
    from fabrication_prep.signing import sign

    s = get_settings()
    url = urlsplit(sign("http://testserver", "c" * 64, s.url_signing_keys(), 300).url)
    assert client.get(f"{url.path}?{url.query}").status_code == 404


def test_health_ready_root_openapi(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert client.get("/").json()["api"] == "/v1"
    spec = client.get("/openapi.json").json()
    assert "/v1/slice-jobs" in spec["paths"] and "/v1/artifacts/{sha256}" in spec["paths"]


def test_oversized_body_is_413(client, auth_header):
    r = client.post(
        "/v1/slice-jobs", content=b"x" * 300_000, headers={**auth_header(), "content-type": "application/json"}
    )
    assert r.status_code == 413
