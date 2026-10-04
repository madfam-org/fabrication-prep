"""The production manifests satisfy the Kyverno-derived rules (scripts/check_manifests.py), the rules catch
violations, and enclii.yaml names the ruled hostnames. CI also runs the script on a real `kustomize build`."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "infra" / "k8s" / "production"

spec = importlib.util.spec_from_file_location("check_manifests", ROOT / "scripts" / "check_manifests.py")
check_manifests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_manifests)


def render() -> list[dict]:
    """A minimal stand-in for `kustomize build`: the resources plus the images: digest pins."""
    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    pins = {i["name"]: f"{i['newName']}@{i['digest']}" for i in kustomization["images"]}
    docs: list[dict] = []
    for resource in kustomization["resources"]:
        docs += [d for d in yaml.safe_load_all((OVERLAY / resource).read_text()) if d]
    for doc in docs:
        if doc.get("kind") == "Deployment":
            pod = doc["spec"]["template"]["spec"]
            for c in pod.get("containers", []) + pod.get("initContainers", []):
                c["image"] = pins[c["image"]]
    return docs


def test_overlay_passes_the_rules():
    assert check_manifests.check(render()) == []


def test_enclii_manifest_names_the_ruled_domains():
    docs = list(yaml.safe_load_all((ROOT / "enclii.yaml").read_text()))
    domains = {d["name"] for doc in docs for d in doc.get("spec", {}).get("domains", [])}
    assert domains == {"fabrication-prep.madfam.io", "fabrication-prep-api.madfam.io"}
    services = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Service"}
    assert services == {"fabrication-prep", "fabrication-prep-worker", "fabrication-prep-web"}


def test_worker_takes_no_traffic_and_holds_no_owner_credentials():
    docs = render()
    worker = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "fabrication-prep-worker")
    container = worker["spec"]["template"]["spec"]["containers"][0]
    assert "ports" not in container
    keys = {e["valueFrom"]["secretKeyRef"]["key"] for e in container["env"] if "valueFrom" in e}
    assert "DATABASE_URL" not in keys and "ARTIFACT_URL_KEYS" not in keys
    api = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "fabrication-prep-api")
    api_keys = {
        e["valueFrom"]["secretKeyRef"]["key"]
        for e in api["spec"]["template"]["spec"]["containers"][0]["env"]
        if "valueFrom" in e
    }
    assert "DATABASE_URL" not in api_keys  # only the migrate init container reads the owner URL


def _mutated(path: list, value, name="fabrication-prep-api") -> list[dict]:
    docs = copy.deepcopy(render())
    node = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == name)
    for key in path[:-1]:
        node = node[key]
    if value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    return docs


CONTAINER = ["spec", "template", "spec", "containers", 0]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (CONTAINER + ["image"], "ghcr.io/madfam-org/fabrication-prep/fabrication-prep-api:latest"),
        (CONTAINER + ["image"], "docker.io/library/python@sha256:" + "1" * 64),
        (CONTAINER + ["securityContext", "readOnlyRootFilesystem"], False),
        (CONTAINER + ["securityContext", "allowPrivilegeEscalation"], True),
        (CONTAINER + ["securityContext", "capabilities"], {"drop": []}),
        (CONTAINER + ["resources"], {}),
        (CONTAINER + ["readinessProbe"], None),
        (["spec", "template", "spec", "securityContext", "runAsNonRoot"], False),
    ],
)
def test_rules_catch_violations(path, value):
    assert check_manifests.check(_mutated(path, value))


def test_rules_catch_forbidden_objects_and_a_worker_service():
    docs = render()
    assert check_manifests.check(docs + [{"kind": "Secret", "metadata": {"name": "x"}}])
    svc = {
        "kind": "Service",
        "metadata": {"name": "w"},
        "spec": {"selector": {"app.kubernetes.io/name": "fabrication-prep-worker"}},
    }
    assert check_manifests.check(docs + [svc])


# Keys Enclii writes into fabrication-prep-credentials: the generated owner and app-role URLs and the
# generated URL-signing key (`enclii onboard --generate-db-password --app-role ... --generate-secret
# ARTIFACT_URL_KEYS`), and the bucket credentials (`enclii buckets create`, provisioning/r2.go).
ENCLII_WRITTEN_KEYS = {
    "DATABASE_URL",
    "APP_DATABASE_URL",
    "ARTIFACT_URL_KEYS",
    "R2_ENDPOINT_URL",
    "R2_BUCKET_NAME",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
}


def _secret_refs(docs: list[dict]) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        spec = d["spec"]["template"]["spec"]
        for c in spec.get("initContainers", []) + spec["containers"]:
            for e in c.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    assert ref["name"] == "fabrication-prep-credentials"
                    refs.setdefault(e["name"], set()).add(ref["key"])
    return refs


def test_deployments_read_only_keys_enclii_writes():
    refs = _secret_refs(render())
    read = set().union(*refs.values())
    assert read <= ENCLII_WRITTEN_KEYS, read - ENCLII_WRITTEN_KEYS
    assert refs["S3_ACCESS_KEY_ID"] == {"R2_ACCESS_KEY_ID"}
    assert refs["S3_SECRET_ACCESS_KEY"] == {"R2_SECRET_ACCESS_KEY"}
    assert refs["S3_ENDPOINT_URL"] == {"R2_ENDPOINT_URL"}
    assert refs["S3_BUCKET"] == {"R2_BUCKET_NAME"}


def test_input_allowed_hosts_is_plain_config_not_a_secret():
    for d in render():
        if d.get("kind") != "Deployment" or d["metadata"]["name"] == "fabrication-prep-web":
            continue
        env = {e["name"]: e for e in d["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["INPUT_ALLOWED_HOSTS"].get("value"), d["metadata"]["name"]
        assert "valueFrom" not in env["INPUT_ALLOWED_HOSTS"]
