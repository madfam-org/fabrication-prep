"""HTTP API ``/v1`` (MES-1 §4).

| Method + path | Auth | Purpose |
|---|---|---|
| ``POST /v1/slice-jobs`` | slice scope | validate, enqueue (202; ``Idempotency-Key`` replays return 200) |
| ``GET /v1/slice-jobs/{id}`` | slice scope, owner | status, error, output + slicer-variables with fresh signed URLs |
| ``GET /v1/profiles`` | slice scope | the profile catalog (ids, versions, digests, compatibility) |
| ``GET /v1/profiles/{kind}/{id}/{version}`` | slice scope | one profile's OrcaSlicer JSON |
| ``GET /v1/artifacts/{sha256}`` | signed URL | the bytes (no token: the URL is the capability); 410 once expired |
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.concurrency import run_in_threadpool

from . import queue
from .artifacts import ArtifactNotFound, store_from_settings
from .auth import Principal, require_slice_principal
from .canonical import canonical_sha256
from .errors import ApiError, Problem, conflict, forbidden, gone, not_found, unprocessable
from .inputs import check_url
from .profiles import get_catalog
from .settings import get_settings
from .signing import SignatureInvalid, sign, verify
from .validation import validate_job_request
from .vocab import get_vocabulary

router = APIRouter(prefix="/v1")

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Url = Annotated[str, StringConstraints(min_length=8, max_length=2048)]
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VariablesRef(Strict):
    url: Url
    sha256: Sha256


class InputRef(Strict):
    url: Url
    sha256: Sha256
    media_type: Literal["model/stl", "model/3mf"]
    variables: VariablesRef | None = Field(None, description="The GOC-1 variables.json sidecar of this geometry")


class Bound(Strict):
    min: float | None = None
    max: float | None = None
    value: float | int | bool | str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if self.min is None and self.max is None and self.value is None:
            raise ValueError("a bound needs min, max or value")
        return self


class Materials(Strict):
    any_of: list[str] | None = None
    none_of: list[str] | None = None


class RequirementSet(Strict):
    process: list[str] | None = None
    materials: Materials | None = None
    process_parameters: dict[str, Bound] | None = None


class Requirements(RequirementSet):
    rationale: dict[str, str] | None = None
    parts: dict[str, RequirementSet] | None = None


class SliceJobRequest(Strict):
    input: InputRef
    printer_profile: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    filament_profile: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    process_profile: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    overrides: dict[str, float | int | bool | str] = Field(default_factory=dict, max_length=64)
    requirements: Requirements | None = None
    part: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    target: Literal["klipper_gcode", "bambu_3mf"]


def _signed(sha256: str, extra: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    signed = sign(s.public_base_url, sha256, s.url_signing_keys(), s.artifact_url_ttl_seconds)
    expires = datetime.fromtimestamp(signed.expires_at, UTC).isoformat().replace("+00:00", "Z")
    return {**extra, "sha256": sha256, "url": signed.url, "expires_at": expires}


def job_view(job: dict) -> dict[str, Any]:
    request = queue.job_request(job)
    view: dict[str, Any] = {
        "id": str(job["id"]),
        "status": job["status"],
        "target": job["target"],
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "created_at": job["created_at"].isoformat(),
        "updated_at": job["updated_at"].isoformat(),
        "finished_at": job["finished_at"].isoformat() if job["finished_at"] else None,
        "input": {k: request["input"][k] for k in ("sha256", "media_type")},
        "profiles": request["resolved_profiles"],
        "error": {"code": job["error_code"], "message": job["error_message"]} if job["error_code"] else None,
        "output": None,
        "slicer_variables": None,
    }
    if job["status"] == "succeeded":
        out = queue.get_artifact(job["output_sha256"])
        doc = queue.get_artifact(job["slicer_variables_sha256"])
        expired = [a["expired_at"] for a in (out, doc) if a["expired_at"] is not None]
        if expired:
            # Retention deleted the bytes (fabrication_prep.retention): no URL is issued, so a consumer fails visibly
            # instead of handing out a link that cannot be served. The digests stay in the job's result.
            view["artifacts_expired_at"] = _iso(min(expired))
        else:
            view["output"] = _signed(
                job["output_sha256"],
                {"media_type": out["media_type"], "bytes": out["bytes"], "filename": out["filename"]},
            )
            view["slicer_variables"] = _signed(
                job["slicer_variables_sha256"], {"media_type": doc["media_type"], "bytes": doc["bytes"]}
            )
        view["estimates"] = (job["result"] or {}).get("estimates")
    return view


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.post("/slice-jobs", status_code=202)
def create_slice_job(
    body: SliceJobRequest,
    request: Request,
    principal: Principal = Depends(require_slice_principal),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    s = get_settings()
    if idempotency_key is not None and not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise ApiError.one(400, "invalid_idempotency_key", "Idempotency-Key must be 1-200 of [A-Za-z0-9._:-]")
    raw = body.model_dump(exclude_none=True)
    problems: list[Problem] = []
    for path, url in [("/input/url", body.input.url)] + (
        [("/input/variables/url", body.input.variables.url)] if body.input.variables else []
    ):
        reason = check_url(url, s)
        if reason:
            problems.append(Problem("input_url_not_allowed", reason, path))
    resolved, requirements, more = validate_job_request(raw, get_catalog(), get_vocabulary())
    problems += more
    if problems:
        raise unprocessable(problems)
    stored = {
        **raw,
        "resolved_profiles": {
            kind: {"id": p.id, "version": p.version, "sha256": p.sha256}
            for kind, p in (
                ("printer", resolved.printer),
                ("process", resolved.process),
                ("filament", resolved.filament),
            )
        },
    }
    try:
        job, created = queue.enqueue(
            owner=principal.sub,
            tenant_id=principal.tenant_id,
            idempotency_key=idempotency_key,
            request=stored,
            request_sha256=canonical_sha256(stored),
            target=body.target,
            max_attempts=s.job_max_attempts,
        )
    except queue.IdempotencyConflict:
        raise conflict("idempotency_conflict", "This Idempotency-Key was used with a different request") from None
    view = job_view(job)
    return JSONResponse(view, status_code=202 if created else 200, headers={"Location": f"/v1/slice-jobs/{view['id']}"})


@router.get("/slice-jobs/{job_id}")
def read_slice_job(job_id: uuid.UUID, principal: Principal = Depends(require_slice_principal)):
    job = queue.get_job(principal.sub, job_id)
    if job is None:
        raise not_found()
    return job_view(job)


@router.get("/profiles")
def list_profiles(principal: Principal = Depends(require_slice_principal)):
    catalog = get_catalog()
    return {"orcaslicer": catalog.orcaslicer, "profiles": [p.summary() for p in catalog.profiles]}


@router.get("/profiles/{kind}/{profile_id}/{version}")
def read_profile(kind: str, profile_id: str, version: int, principal: Principal = Depends(require_slice_principal)):
    profile = get_catalog().get(kind, profile_id, version)
    if profile is None:
        raise not_found()
    return {**profile.summary(), "content": profile.content}


@router.get("/artifacts/{sha256}")
async def download_artifact(
    sha256: str,
    exp: str | None = Query(None, max_length=12),
    kid: str | None = Query(None, max_length=64),
    sig: str | None = Query(None, max_length=128),
):
    s = get_settings()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise not_found()
    try:
        verify(sha256, exp, kid, sig, s.url_signing_keys(), s.artifact_url_ttl_seconds)
    except SignatureInvalid as exc:
        raise forbidden(exc.code, exc.message) from None
    meta = await run_in_threadpool(queue.get_artifact, sha256)
    if meta is None:
        raise not_found()
    if meta["expired_at"] is not None:
        raise gone("artifact_expired", "The artifact's bytes passed the retention period and were deleted")
    store = store_from_settings(s)
    try:
        stream = await run_in_threadpool(store.open_stream, sha256)
    except ArtifactNotFound:
        raise not_found("The artifact's bytes are no longer stored") from None
    headers = {
        "Content-Length": str(meta["bytes"]),
        "Content-Disposition": f'attachment; filename="{meta["filename"]}"',
        "ETag": f'"{sha256}"',
        "Cache-Control": "private, max-age=0, no-store",
        "X-Content-SHA256": sha256,
    }
    return StreamingResponse(stream, media_type=meta["media_type"], headers=headers)
