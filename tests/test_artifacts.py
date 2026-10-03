"""Artifact stores: content addressing, digest verification, streaming; the S3 backend against moto."""

from __future__ import annotations

import hashlib

import boto3
import pytest
from moto import mock_aws

from fabrication_prep.artifacts import (
    ArtifactDigestMismatch,
    ArtifactNotFound,
    FsArtifactStore,
    S3ArtifactStore,
    store_from_settings,
)
from fabrication_prep.settings import Settings

DATA = b"G28\nG1 X10\n" * 1000
SHA = hashlib.sha256(DATA).hexdigest()


def exercise(store, tmp_path):
    src = tmp_path / "out.gcode"
    src.write_bytes(DATA)
    assert not store.exists(SHA)
    with pytest.raises(ArtifactNotFound):
        store.size(SHA)
    with pytest.raises(ArtifactNotFound):
        b"".join(store.open_stream(SHA))
    with pytest.raises(ArtifactDigestMismatch):
        store.put_file(src, "0" * 64, "text/x-gcode")
    store.put_file(src, SHA, "text/x-gcode")
    store.put_file(src, SHA, "text/x-gcode")  # idempotent
    assert store.exists(SHA) and store.size(SHA) == len(DATA)
    assert b"".join(store.open_stream(SHA)) == DATA
    with pytest.raises(ValueError):
        store.exists("../etc/passwd")


def test_fs_store(tmp_path):
    store = FsArtifactStore(tmp_path / "root")
    exercise(store, tmp_path)
    assert (tmp_path / "root" / SHA[:2] / SHA).is_file()
    assert not list((tmp_path / "root" / SHA[:2]).glob(".incoming-*"))


@mock_aws
def test_s3_store(tmp_path):
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="fabrication-prep")
    store = S3ArtifactStore(client, "fabrication-prep", "artifacts/")
    exercise(store, tmp_path)
    head = client.head_object(Bucket="fabrication-prep", Key=f"artifacts/{SHA[:2]}/{SHA}")
    assert head["ContentType"] == "text/x-gcode"


@mock_aws
def test_s3_store_from_settings_and_errors(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    s = Settings(
        artifact_backend="s3", s3_bucket="b1", s3_region="us-east-1", jwks_path="", fabrication_prep_env="test"
    )
    store = store_from_settings(s)
    assert isinstance(store, S3ArtifactStore)
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):  # bucket missing: a real error, not "not found"
        store.exists(SHA)
    with pytest.raises(RuntimeError):
        store_from_settings(Settings(artifact_backend="s3", s3_bucket="", jwks_path=""))
    assert isinstance(store_from_settings(Settings(artifact_backend="fs", jwks_path="")), FsArtifactStore)
