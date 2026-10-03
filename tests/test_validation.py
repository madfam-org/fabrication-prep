"""Request validation: profiles, compatibility, overrides vs vocabulary and requirement ranges (422 detail)."""

from __future__ import annotations

from fabrication_prep.profiles import get_catalog
from fabrication_prep.validation import effective_requirements, validate_job_request, violates
from fabrication_prep.vocab import get_vocabulary
from tests.slicer_fakes import job_body


def run(body):
    return validate_job_request(body, get_catalog(), get_vocabulary())


def codes(problems):
    return [p.code for p in problems]


def test_valid_requests_resolve():
    for target in ("klipper_gcode", "bambu_3mf"):
        resolved, req, problems = run(job_body(target))
        assert problems == [] and resolved.printer.target == target and req == {}


def test_unknown_profile_and_target_mismatch():
    _, _, p = run(job_body(printer_profile="nope"))
    assert codes(p) == ["unknown_profile"] and p[0].path == "/printer_profile"
    _, _, p = run(
        job_body(
            target="bambu_3mf",
            printer_profile="klipper-corexy-350-0.4",
            process_profile="standard-0.20-klipper",
            filament_profile="pla-generic-klipper",
        )
    )
    assert codes(p) == ["target_mismatch"]


def test_incompatible_profiles():
    _, _, p = run(job_body(process_profile="standard-0.20-bambu-a1", filament_profile="pla-generic-bambu-a1"))
    assert codes(p) == ["incompatible_profiles", "incompatible_profiles"]


def test_tpu_requires_the_tpu_safe_process():
    _, _, p = run(job_body(filament_profile="tpu-95a-klipper"))
    assert codes(p) == ["incompatible_profiles"] and "tpu-safe" in p[0].message
    _, _, p = run(job_body(filament_profile="tpu-95a-klipper", process_profile="tpu-safe-0.20-klipper"))
    assert p == []


def test_override_vocabulary_checks():
    _, _, p = run(job_body(overrides={"brim_width": 5}))
    assert codes(p) == ["unknown_parameter"] and p[0].path == "/overrides/brim_width"
    _, _, p = run(job_body(overrides={"nozzle_diameter": 0.6, "bed_temperature": 60}))
    assert codes(p) == ["not_overridable", "not_overridable"]
    _, _, p = run(job_body(overrides={"wall_loops": 2.5, "enable_support": 1, "sparse_infill_pattern": "spiral"}))
    assert codes(p) == ["invalid_value"] * 3
    _, _, p = run(job_body(overrides={"sparse_infill_density": 140}))
    assert codes(p) == ["override_out_of_bounds"] and p[0].details["max"] == 100


def test_override_outside_requirement_range_names_key_and_range():
    body = job_body(
        overrides={"wall_loops": 2}, requirements={"process_parameters": {"wall_loops": {"min": 3, "max": 6}}}
    )
    _, _, p = run(body)
    assert codes(p) == ["override_out_of_range"]
    assert p[0].path == "/overrides/wall_loops"
    assert p[0].details == {"key": "wall_loops", "value": 2, "unit": "count", "source": "override", "min": 3, "max": 6}
    assert "wall_loops=2" in p[0].message and "min 3, max 6" in p[0].message
    _, _, p = run(
        job_body(overrides={"wall_loops": 4}, requirements={"process_parameters": {"wall_loops": {"min": 3, "max": 6}}})
    )
    assert p == []


def test_profile_value_outside_requirement_is_refused_before_slicing():
    # standard-0.20-klipper has 15 % infill; the product needs at least 40 %.
    body = job_body(requirements={"process_parameters": {"sparse_infill_density": {"min": 40, "unit": "percent"}}})
    _, _, p = run(body)
    assert codes(p) == ["requirement_not_met"] and p[0].details["source"] == "profile"
    _, _, p = run({**body, "overrides": {"sparse_infill_density": 45}})
    assert p == []


def test_bed_temperature_requirement_uses_the_active_plate():
    body = job_body(requirements={"process_parameters": {"bed_temperature": {"max": 50}}})
    _, _, p = run(body)  # Generic PLA on textured PEI = 55 degC
    assert codes(p) == ["requirement_not_met"] and p[0].details["value"] == 55


def test_requirement_shape_and_vocabulary():
    _, _, p = run(
        job_body(
            requirements={
                "process": ["fff", "warp"],
                "materials": {"any_of": ["unobtainium"]},
                "process_parameters": {
                    "brim": {"min": 1},
                    "wall_loops": {"min": 4, "max": 3},
                    "layer_height": {"max": 0.3, "unit": "inch"},
                    "top_shell_layers": {},
                },
            }
        )
    )
    assert sorted(codes(p)) == sorted(
        [
            "unknown_process",
            "unknown_material_class",
            "unknown_parameter",
            "invalid_bound",
            "unit_mismatch",
            "invalid_bound",
        ]
    )
    _, _, p = run(job_body(requirements={"process": ["sla"]}))
    assert codes(p) == ["process_not_supported"]


def test_material_requirements():
    _, _, p = run(job_body(requirements={"materials": {"any_of": ["tpu-95a"]}}))
    assert codes(p) == ["material_not_allowed"]
    _, _, p = run(job_body(requirements={"materials": {"none_of": ["pla"]}}))
    assert codes(p) == ["material_not_allowed"]
    _, _, p = run(job_body(requirements={"materials": {"any_of": ["pla", "petg"]}}))
    assert p == []


def test_part_requirements_layer_over_the_top_level():
    req = {
        "process_parameters": {"wall_loops": {"min": 2}, "layer_height": {"max": 0.3}},
        "materials": {"any_of": ["pla"]},
        "parts": {"hinge": {"materials": {"any_of": ["tpu-95a"]}, "process_parameters": {"wall_loops": {"min": 4}}}},
    }
    eff = effective_requirements(req, "hinge")
    assert eff["materials"] == {"any_of": ["tpu-95a"]}
    assert eff["process_parameters"] == {"wall_loops": {"min": 4}, "layer_height": {"max": 0.3}}
    assert effective_requirements(req, None)["materials"] == {"any_of": ["pla"]}
    assert effective_requirements(None, "x") == {}


def test_exact_value_bounds():
    assert not violates(False, {"value": False}) and violates(True, {"value": False})
    assert not violates(0.2, {"value": 0.2}) and violates(0.25, {"value": 0.2})
    assert violates("grid", {"value": "gyroid"}) and violates("x", {"min": 1})
    _, _, p = run(job_body(requirements={"process_parameters": {"enable_support": {"value": True}}}))
    assert codes(p) == ["requirement_not_met"]
