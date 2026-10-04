"""Request validation against the profile catalog and the fabrication vocabularies.

Every refusal is a ``Problem`` with a JSON-pointer ``path``; range violations name the key, the bound and the
value (``details``), so the caller can show exactly why a job was refused (HTTP 422).

Requirement bounds follow the RequirementProfile shape (hyperobjects-spec ``processParameterBound``):
``min`` and/or ``max`` (inclusive) for a numeric setting, or ``value`` for an exact one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .effective import apply_overrides, effective_values, merged_profile_source
from .errors import Problem
from .profiles import Catalog, Profile, ProfileLookupError
from .vocab import ORCA_ENUMS, Vocabulary

OVERRIDABLE_PRESETS = ("print", "filament")
TARGET_FOR_PRINTER = ("klipper_gcode", "bambu_3mf")
# Service guard rails by unit (MADFAM authoring, not OrcaSlicer limits): a value outside these is a
# mistake whatever the requirement says.
SANITY_BOUNDS: dict[str, tuple[float, float]] = {
    "percent": (0, 100),
    "count": (0, 100),
    "mm": (0.01, 20),
    "mm/s": (0, 1000),
    "degC": (0, 500),
}


@dataclass(frozen=True)
class ResolvedProfiles:
    printer: Profile
    process: Profile
    filament: Profile

    def docs(self) -> dict[str, dict[str, Any]]:
        return {"printer": self.printer.content, "process": self.process.content, "filament": self.filament.content}


def _typed_ok(value: Any, value_type: str) -> bool:
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
    return isinstance(value, str) and 0 < len(value) <= 200


def describe_bound(bound: dict[str, Any], unit: str) -> str:
    if "value" in bound:
        return f"exactly {bound['value']!r}"
    parts = []
    if "min" in bound:
        parts.append(f"min {bound['min']:g}")
    if "max" in bound:
        parts.append(f"max {bound['max']:g}")
    return f"[{', '.join(parts)}] {unit}".rstrip()


def violates(value: Any, bound: dict[str, Any]) -> bool:
    if "value" in bound:
        expected = bound["value"]
        if isinstance(expected, bool) or isinstance(value, bool):
            return value is not expected
        if isinstance(expected, int | float) and isinstance(value, int | float):
            return not math.isclose(float(value), float(expected), rel_tol=0, abs_tol=1e-9)
        return value != expected
    if not isinstance(value, int | float) or isinstance(value, bool):
        return True
    return ("min" in bound and value < bound["min"]) or ("max" in bound and value > bound["max"])


def resolve_profiles(catalog: Catalog, request: dict[str, Any]) -> tuple[ResolvedProfiles | None, list[Problem]]:
    problems: list[Problem] = []
    found: dict[str, Profile] = {}
    for kind in ("printer", "process", "filament"):
        field = f"{kind}_profile"
        try:
            found[kind] = catalog.resolve(kind, request[field])
        except ProfileLookupError as exc:
            problems.append(Problem("unknown_profile", str(exc), f"/{field}"))
    if problems:
        return None, problems
    printer, process, filament = found["printer"], found["process"], found["filament"]
    if printer.target != request["target"]:
        problems.append(
            Problem(
                "target_mismatch",
                f"printer profile {printer.ref} produces '{printer.target}', not '{request['target']}'",
                "/target",
            )
        )
    if printer.id not in process.printers:
        problems.append(
            Problem(
                "incompatible_profiles", f"process {process.ref} is not for printer {printer.ref}", "/process_profile"
            )
        )
    if printer.id not in filament.printers:
        problems.append(
            Problem(
                "incompatible_profiles",
                f"filament {filament.ref} is not for printer {printer.ref}",
                "/filament_profile",
            )
        )
    if filament.requires_process_tag and filament.requires_process_tag not in process.tags:
        problems.append(
            Problem(
                "incompatible_profiles",
                f"filament {filament.ref} requires a "
                f"'{filament.requires_process_tag}' process; {process.ref} is not one",
                "/process_profile",
            )
        )
    return ResolvedProfiles(printer, process, filament), problems


def validate_overrides(overrides: dict[str, Any], vocab: Vocabulary) -> list[Problem]:
    problems: list[Problem] = []
    for key, value in overrides.items():
        path = f"/overrides/{key}"
        param = vocab.process_parameters.get(key)
        if param is None:
            problems.append(Problem("unknown_parameter", f"'{key}' is not a process-parameters key", path))
            continue
        if param.preset not in OVERRIDABLE_PRESETS:
            reason = (
                "a printer setting: choose a printer profile instead"
                if param.preset == "printer"
                else "not a stored OrcaSlicer setting; override the active plate's temperature key"
            )
            problems.append(Problem("not_overridable", f"'{key}' cannot be overridden ({reason})", path))
            continue
        if not _typed_ok(value, param.value_type):
            problems.append(Problem("invalid_value", f"'{key}' must be a {param.value_type}", path))
            continue
        if key in ORCA_ENUMS and value not in ORCA_ENUMS[key]:
            problems.append(Problem("invalid_value", f"'{value}' is not an OrcaSlicer value for '{key}'", path))
            continue
        sanity = SANITY_BOUNDS.get(param.unit)
        if sanity and isinstance(value, int | float) and not isinstance(value, bool):
            low, high = sanity
            if not low <= value <= high:
                problems.append(
                    Problem(
                        "override_out_of_bounds",
                        f"{key}={value:g} is outside the service's guard rail [{low:g}, {high:g}] {param.unit}",
                        path,
                        {"key": key, "value": value, "min": low, "max": high, "unit": param.unit},
                    )
                )
    return problems


def effective_requirements(requirements: dict[str, Any] | None, part: str | None) -> dict[str, Any]:
    """Top-level requirements with the named part's entries layered over them (key by key for
    process_parameters; materials and process replaced when the part states them)."""
    if not requirements:
        return {}
    out = {k: v for k, v in requirements.items() if k in ("process", "materials", "process_parameters")}
    part_req = (requirements.get("parts") or {}).get(part) if part else None
    if part_req:
        for key in ("process", "materials"):
            if key in part_req:
                out[key] = part_req[key]
        if "process_parameters" in part_req:
            out["process_parameters"] = {**out.get("process_parameters", {}), **part_req["process_parameters"]}
    return out


def validate_requirements(req: dict[str, Any], vocab: Vocabulary) -> list[Problem]:
    problems: list[Problem] = []
    for proc in req.get("process", []) or []:
        if proc not in vocab.processes:
            problems.append(Problem("unknown_process", f"'{proc}' is not a processes key", "/requirements/process"))
    for side in ("any_of", "none_of"):
        for cls in (req.get("materials") or {}).get(side, []) or []:
            if cls not in vocab.material_classes:
                problems.append(
                    Problem(
                        "unknown_material_class",
                        f"'{cls}' is not a material-classes key",
                        f"/requirements/materials/{side}",
                    )
                )
    for key, bound in (req.get("process_parameters") or {}).items():
        path = f"/requirements/process_parameters/{key}"
        param = vocab.process_parameters.get(key)
        if param is None:
            problems.append(Problem("unknown_parameter", f"'{key}' is not a process-parameters key", path))
            continue
        if not isinstance(bound, dict) or not ({"min", "max", "value"} & set(bound)):
            problems.append(Problem("invalid_bound", f"'{key}' needs min, max or value", path))
            continue
        if bound.get("unit") not in (None, param.unit):
            problems.append(Problem("unit_mismatch", f"'{key}' is measured in {param.unit}, not {bound['unit']}", path))
        if "min" in bound and "max" in bound and bound["min"] > bound["max"]:
            problems.append(Problem("invalid_bound", f"'{key}': min is greater than max", path))
    return problems


def check_against_requirements(
    values: dict[str, Any], req: dict[str, Any], vocab: Vocabulary, source: str, path_prefix: str
) -> list[Problem]:
    """``values`` violating ``req['process_parameters']``. ``source`` says where the values come from."""
    problems: list[Problem] = []
    for key, bound in (req.get("process_parameters") or {}).items():
        if key not in values or key not in vocab.process_parameters:
            continue
        unit = vocab.process_parameters[key].unit
        if violates(values[key], bound):
            rng = describe_bound(bound, unit)
            problems.append(
                Problem(
                    "override_out_of_range" if source == "override" else "requirement_not_met",
                    f"{key}={values[key]!r} ({source}) is outside the required range {rng}",
                    f"{path_prefix}/{key}",
                    {
                        "key": key,
                        "value": values[key],
                        "unit": unit,
                        "source": source,
                        **{k: bound[k] for k in ("min", "max", "value") if k in bound},
                    },
                )
            )
    return problems


def check_material(filament: Profile, req: dict[str, Any]) -> list[Problem]:
    materials = req.get("materials") or {}
    cls = filament.material_class
    if materials.get("any_of") and cls not in materials["any_of"]:
        return [
            Problem(
                "material_not_allowed",
                f"filament {filament.ref} is '{cls}'; the requirements accept {sorted(materials['any_of'])}",
                "/filament_profile",
            )
        ]
    if cls in (materials.get("none_of") or []):
        return [
            Problem(
                "material_not_allowed",
                f"filament {filament.ref} is '{cls}', which the requirements refuse",
                "/filament_profile",
            )
        ]
    return []


def validate_job_request(request: dict[str, Any], catalog: Catalog, vocab: Vocabulary):
    """(ResolvedProfiles, effective requirements) or a list of problems (HTTP 422)."""
    resolved, problems = resolve_profiles(catalog, request)
    overrides = request.get("overrides") or {}
    problems += validate_overrides(overrides, vocab)
    req = effective_requirements(request.get("requirements"), request.get("part"))
    problems += validate_requirements(req, vocab)
    if req.get("process") and "fff" not in req["process"]:
        problems.append(
            Problem(
                "process_not_supported",
                "the requirements do not allow FFF, the only process this service slices for",
                "/requirements/process",
            )
        )
    if problems or resolved is None:
        return None, None, problems
    problems += check_material(resolved.filament, req)
    problems += check_against_requirements(overrides, req, vocab, "override", "/overrides")
    planned = effective_values(merged_profile_source(apply_overrides(resolved.docs(), overrides, vocab)), vocab)
    profile_only = {k: v for k, v in planned.items() if k not in overrides}
    problems += check_against_requirements(profile_only, req, vocab, "profile", "/requirements/process_parameters")
    return (None, None, problems) if problems else (resolved, req, [])
