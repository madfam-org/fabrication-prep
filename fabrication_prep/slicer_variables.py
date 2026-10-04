"""The ``slicer-variables.json`` document: what was sliced, with what, by which slicer, and the estimate.

GOC-1 style: canonical JSON (§3.1), digests everywhere, no timestamps (the same slice of the same inputs
produces the same document except for the output digest, which changes because OrcaSlicer stamps the
G-code with its generation time). Schema: ``fabrication_prep/schemas/slicer-variables.schema.json``.

``effective`` holds every process-parameters key as the slicer reported using it (its own config dump in
the G-code); ``effective_sha256`` is the canonical digest of that object, which the passport records.
"""

from __future__ import annotations

from typing import Any

from .canonical import canonical_sha256
from .inputs import GeneratorInstance
from .validation import ResolvedProfiles

FORMAT = "madfam.fabrication-prep.slicer-variables"
FORMAT_VERSION = "1.0.0"


def build(
    *,
    job_id: str,
    request: dict[str, Any],
    profiles: ResolvedProfiles,
    input_bytes: int,
    instance: GeneratorInstance | None,
    effective: dict[str, Any],
    requirements: dict[str, Any],
    orcaslicer_version: str,
    output: dict[str, Any],
    estimates: dict[str, Any],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "job_id": job_id,
        "target": request["target"],
        "input": {
            "sha256": request["input"]["sha256"],
            "media_type": request["input"]["media_type"],
            "bytes": input_bytes,
        },
        "profiles": {
            kind: {"id": p.id, "version": p.version, "sha256": p.sha256}
            for kind, p in (
                ("printer", profiles.printer),
                ("process", profiles.process),
                ("filament", profiles.filament),
            )
        },
        "material_class": profiles.filament.material_class,
        "overrides": request.get("overrides") or {},
        "requirements_sha256": canonical_sha256(requirements),
        "effective": effective,
        "effective_sha256": canonical_sha256(effective),
        "slicer": {"name": "OrcaSlicer", "version": orcaslicer_version},
        "output": output,
        "estimates": estimates,
        "warnings": warnings,
    }
    if instance is not None:
        doc["generator_output"] = {
            "instance_id": instance.instance_id,
            "variables_sha256": instance.variables_sha256,
            "sidecar_sha256": instance.sidecar_sha256,
            "cartridge": instance.cartridge,
            "mode": instance.mode,
            "part": instance.part,
            "complete": instance.complete,
        }
    return doc
