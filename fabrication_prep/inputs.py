"""Fetching a render bundle: the geometry file and, optionally, its GOC-1 ``variables.json`` sidecar.

* URLs must be https (http only in local/test) and their host must be on ``INPUT_ALLOWED_HOSTS``; no
  credentials in URLs, no redirects (a redirect is an error, never followed), bounded size and time.
* The geometry's sha256 must equal the request's; a mismatch is permanent (the bundle changed or the URL
  points elsewhere).
* A sidecar is verified the same way, then checked as a GOC-1 document: its recomputed
  ``variables_sha256`` and ``instance_id`` must match, and its ``geometry`` list must contain the input's
  sha256 — so the slice is provably of that generator instance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .canonical import goc1_instance_id, goc1_variables_sha256
from .settings import Settings

SIDECAR_MAX_BYTES = 2 * 1024 * 1024


class InputError(Exception):
    def __init__(self, code: str, message: str, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient


@dataclass(frozen=True)
class GeneratorInstance:
    instance_id: str
    variables_sha256: str
    cartridge: str
    mode: str
    part: str | None
    complete: bool
    sidecar_sha256: str
    geometry_role: str | None


def check_url(url: str, s: Settings) -> str | None:
    """None when acceptable, else the reason (also used by the API for an early 422)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "not a URL"
    allowed_schemes = ("https", "http") if s.is_local else ("https",)
    if parts.scheme not in allowed_schemes:
        return f"scheme must be {' or '.join(allowed_schemes)}"
    if parts.username or parts.password:
        return "credentials in URLs are not accepted"
    host = (parts.hostname or "").lower()
    if not host:
        return "the URL has no host"
    allowed = s.allowed_input_hosts
    if not allowed and not s.is_local:
        return "no input hosts are allowed (INPUT_ALLOWED_HOSTS is empty)"
    if allowed and host not in allowed:
        return f"host '{host}' is not an allowed input host"
    return None


def fetch(client: httpx.Client, url: str, dest: Path, expected_sha256: str, max_bytes: int, s: Settings) -> int:
    reason = check_url(url, s)
    if reason:
        raise InputError("input_url_not_allowed", reason)
    h = hashlib.sha256()
    size = 0
    try:
        with client.stream("GET", url, follow_redirects=False) as resp:
            if resp.status_code in (404, 410):
                raise InputError("input_gone", f"the input URL answered {resp.status_code}")
            if 300 <= resp.status_code < 400:
                raise InputError("input_redirect", "the input URL redirects; redirects are not followed")
            if resp.status_code >= 500 or resp.status_code == 429:
                raise InputError("input_unavailable", f"the input URL answered {resp.status_code}", transient=True)
            if resp.status_code != 200:
                raise InputError("input_refused", f"the input URL answered {resp.status_code}")
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise InputError("input_too_large", f"the input exceeds {max_bytes} bytes")
                    h.update(chunk)
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        raise InputError("input_unavailable", f"fetching the input failed ({type(exc).__name__})", True) from None
    if h.hexdigest() != expected_sha256:
        raise InputError("input_digest_mismatch", "the fetched bytes do not match the declared sha256")
    return size


def read_sidecar(path: Path, sha256: str, input_sha256: str) -> GeneratorInstance:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("format") != "hyperobjects.generator-output":
            raise InputError("sidecar_invalid", "the sidecar is not a GOC-1 generator-output document")
        gen = doc["generator"]
        variables_sha = goc1_variables_sha256(doc["variables"])
        instance = goc1_instance_id(
            gen["cartridge"], gen["mode"], gen.get("part"), gen["source"]["tree_sha256"], variables_sha
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError(
            "sidecar_invalid", f"the sidecar is not a valid GOC-1 document ({type(exc).__name__})"
        ) from None
    if variables_sha != doc.get("variables_sha256") or instance != doc.get("instance_id"):
        raise InputError("sidecar_invalid", "the sidecar's digests do not match its contents (GOC-1 §3.2/§3.4)")
    roles = [g.get("role") for g in doc.get("geometry", []) if g.get("sha256") == input_sha256]
    if not roles:
        raise InputError("sidecar_mismatch", "the sidecar does not list the input geometry's sha256")
    return GeneratorInstance(
        instance_id=instance,
        variables_sha256=variables_sha,
        cartridge=gen["cartridge"],
        mode=gen["mode"],
        part=gen.get("part"),
        complete=bool(doc.get("complete")),
        sidecar_sha256=sha256,
        geometry_role=roles[0],
    )


def fetch_bundle(
    client: httpx.Client, request: dict, workdir: Path, s: Settings
) -> tuple[Path, int, GeneratorInstance | None]:
    from .slicer import MODEL_SUFFIX

    spec = request["input"]
    model = workdir / f"model{MODEL_SUFFIX[spec['media_type']]}"
    size = fetch(client, spec["url"], model, spec["sha256"], s.input_max_bytes, s)
    instance = None
    if spec.get("variables"):
        side = workdir / "variables.json"
        fetch(client, spec["variables"]["url"], side, spec["variables"]["sha256"], SIDECAR_MAX_BYTES, s)
        instance = read_sidecar(side, spec["variables"]["sha256"], spec["sha256"])
    return model, size, instance
