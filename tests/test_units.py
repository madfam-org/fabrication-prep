"""Pure units: canonical JSON, GOC-1 helpers, value conversion, G-code and slice_info parsing, settings."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from fabrication_prep import canonical, effective, slicer
from fabrication_prep.settings import Settings
from fabrication_prep.vocab import get_vocabulary

FIX = Path(__file__).parent / "fixtures"


def test_canonical_json_matches_goc1_rules():
    assert canonical.canonical_json({"b": 12.0, "a": [-0.0, 0.5, True]}) == b'{"a":[0,0.5,true],"b":12}'
    assert canonical.canonical_json({"é": "ñ"}) == '{"é":"ñ"}'.encode()
    with pytest.raises(ValueError):
        canonical.canonical_json({"x": float("nan")})


def test_goc1_digests_match_a_real_yantra4d_sidecar():
    # From the P5-FAB proof render of tslot-corner/corner_3way (yantra4d #203 path, complete=true).
    variables = [
        {"id": "bolt_dia", "value": 0},
        {"id": "fillet_r", "value": 3},
        {"id": "leg_len", "value": 30},
        {"id": "series", "value": "2020"},
        {"id": "thickness", "value": 6},
        {"id": "width", "value": 20},
    ]
    vsha = canonical.goc1_variables_sha256(variables)
    assert vsha == "49367d70f9dc32220ee0642cdc939b9dffdddedd3c33894f41459159514c52a6"


def test_parse_and_format_orca_values():
    v = get_vocabulary().process_parameters
    assert effective.parse_orca_value("15%", v["sparse_infill_density"]) == 15.0
    assert effective.parse_orca_value(["220"], v["nozzle_temperature"]) == 220
    assert effective.parse_orca_value("200,200", v["outer_wall_speed"]) == 200.0
    assert effective.parse_orca_value("1", v["enable_support"]) is True
    assert effective.parse_orca_value("crosshatch", v["sparse_infill_pattern"]) == "crosshatch"
    with pytest.raises(effective.ValueParseError):
        effective.parse_orca_value("x", v["wall_loops"])
    with pytest.raises(effective.ValueParseError):
        effective.parse_orca_value("maybe", v["enable_support"])
    with pytest.raises(effective.ValueParseError):
        effective.parse_orca_value([], v["wall_loops"])
    assert effective.format_orca_value(40.0, v["sparse_infill_density"], "15%") == "40%"
    assert effective.format_orca_value(25, v["outer_wall_speed"], ["200"]) == ["25"]
    assert effective.format_orca_value(0.15, v["layer_height"], None) == "0.15"
    assert effective.format_orca_value(True, v["enable_support"], "0") == "1"
    assert effective.format_orca_value(30, v["outer_wall_speed"], None) == ["30"]  # coFloats default shape


def test_effective_values_from_a_real_config_dump():
    dump = effective.parse_config_block((FIX / "orca-2.4.2-klipper-excerpt.gcode").read_text())
    values = effective.effective_values(dump, get_vocabulary())
    assert values["wall_loops"] == 3 and values["outer_wall_speed"] == 25.0
    assert values["sparse_infill_density"] == 15.0 and values["nozzle_diameter"] == 0.4
    assert values["bed_temperature"] == 35  # textured_plate_temp via curr_bed_type
    assert values["enable_support"] is False
    assert effective.parse_config_block("no block") == {}


def test_bed_temperature_absent_for_unknown_plate():
    values = effective.effective_values({"curr_bed_type": "Glass", "cool_plate_temp": "30"}, get_vocabulary())
    assert "bed_temperature" not in values


def test_gcode_estimates_klipper_and_bambu_real_excerpts():
    k = slicer.parse_gcode_estimates((FIX / "orca-2.4.2-klipper-excerpt.gcode").read_text())
    assert k == {"print_time_s": 2314, "filament_g": 5.22, "filament_mm": 1779.21, "filament_cm3": 4.28}
    b = slicer.parse_gcode_estimates((FIX / "orca-2.4.2-bambu-excerpt.gcode").read_text())
    assert b["print_time_s"] == 1941 and b["filament_g"] == 4.86


def test_durations():
    assert slicer.parse_duration("1d 2h 3m 4s") == 93784
    assert slicer.parse_duration(" 0.95s") == 1
    assert slicer.parse_duration("soon") is None


def test_slice_info_real_fixture():
    est, warnings = slicer.parse_slice_info((FIX / "orca-2.4.2-slice_info.config").read_text())
    assert est == {"print_time_s": 1941, "filament_g": 4.86}
    assert warnings == [{"msg": "bed_temperature_too_high_than_filament", "level": "3", "error_code": "1000C001"}]
    est, warnings = slicer.parse_slice_info("<config><plate><metadata key='weight' value=''/></plate></config>")
    assert est == {} and warnings == []


def test_build_command_targets(tmp_path):
    k = slicer.build_command("/bin/orca", tmp_path, tmp_path / "model.stl", "klipper_gcode")
    assert "--export-3mf" not in k and k[-1].endswith("model.stl")
    assert k[k.index("--slice") + 1] == "0" and k[k.index("--orient") + 1] == "0"
    b = slicer.build_command("/bin/orca", tmp_path, tmp_path / "model.stl", "bambu_3mf")
    assert b[b.index("--export-3mf") + 1] == "result.gcode.3mf"  # relative to --outputdir (proven)


def _settings(**kw) -> Settings:
    base = {
        "fabrication_prep_env": "production",
        "jwks_path": "",
        "public_base_url": "https://api.example",
        "artifact_url_keys": "a:" + base64.b64encode(b"x" * 32).decode(),
    }
    return Settings(**{**base, **kw})


def test_settings_validation_rules():
    _settings().validate_runtime()
    for bad in (
        {"jwks_path": "/x"},
        {"db_pool_max": 11},
        {"db_pool_min": 5, "db_pool_max": 2},
        {"artifact_backend": "ftp"},
        {"artifact_url_ttl_seconds": 7200},
        {"public_base_url": "http://x"},
        {"worker_lease_seconds": 10},
        {"job_max_attempts": 0},
    ):
        with pytest.raises(RuntimeError):
            _settings(**bad).validate_runtime()


def test_url_signing_keys_fail_closed():
    assert _settings().url_signing_keys()[0][0] == "a"
    for bad in ("", "nokey", "a:!!!", "a:" + base64.b64encode(b"short").decode(), "bad kid:" + "eA=="):
        with pytest.raises(RuntimeError):
            _settings(artifact_url_keys=bad).url_signing_keys()


def test_allowed_hosts_parsing():
    assert _settings(input_allowed_hosts=" A.test, b.test ,").allowed_input_hosts == {"a.test", "b.test"}
