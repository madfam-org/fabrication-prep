"""The shipped profile catalog: digests, integrity refusal, references, provenance, compatibility."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from fabrication_prep.canonical import canonical_sha256
from fabrication_prep.profiles import ROOT, ProfileIntegrityError, ProfileLookupError, get_catalog, load_catalog
from fabrication_prep.vocab import get_vocabulary

RECIPES = json.loads((Path(__file__).parent.parent / "scripts" / "profile-recipes.json").read_text())


def test_catalog_digests_and_inventory():
    catalog = get_catalog()
    assert catalog.orcaslicer["version"] == "2.4.2"
    assert len(catalog.profiles) == 12
    for p in catalog.profiles:
        assert canonical_sha256(json.loads((ROOT / p.file).read_text())) == p.sha256
        assert p.content["name"] == p.ref
        assert p.content["from"] == "system"
        assert "inherits" not in p.content
    kinds = {k: len(catalog.by_kind(k)) for k in ("printer", "filament", "process")}
    assert kinds == {"printer": 2, "filament": 6, "process": 4}
    assert {p.material_class for p in catalog.by_kind("filament")} == {"pla", "petg", "tpu-95a"}


def test_catalog_matches_the_recipes():
    catalog = get_catalog()
    assert {(r["id"], r["version"]) for r in RECIPES["profiles"]} == {(p.id, p.version) for p in catalog.profiles}


def test_tampered_profile_is_refused(tmp_path):
    copy = tmp_path / "profiles"
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    path = copy / "process" / "standard-0.20-klipper@1.json"
    doc = json.loads(path.read_text())
    doc["wall_loops"] = "9"
    path.write_text(json.dumps(doc))
    with pytest.raises(ProfileIntegrityError):
        load_catalog(copy)


def test_duplicate_or_renamed_profiles_are_refused(tmp_path):
    copy = tmp_path / "profiles"
    shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    cat = json.loads((copy / "catalog.json").read_text())
    cat["profiles"].append(cat["profiles"][0])
    (copy / "catalog.json").write_text(json.dumps(cat))
    with pytest.raises(ProfileIntegrityError):
        load_catalog(copy)


def test_resolve_references():
    catalog = get_catalog()
    assert catalog.resolve("printer", "bambu-a1-0.4").ref == "bambu-a1-0.4@1"
    assert catalog.resolve("printer", "bambu-a1-0.4@1").ref == "bambu-a1-0.4@1"
    for bad in ("bambu-a1-0.4@2", "nope", "Bad Ref", "", "x@0"):
        with pytest.raises(ProfileLookupError):
            catalog.resolve("printer", bad)
    with pytest.raises(ProfileLookupError):
        catalog.resolve("filament", "bambu-a1-0.4")  # right id, wrong kind
    assert catalog.get("printer", "bambu-a1-0.4", 1) is not None
    assert catalog.get("printer", "bambu-a1-0.4", 9) is None


def test_every_value_has_provenance():
    catalog = get_catalog()
    for p in catalog.profiles:
        prov = json.loads((ROOT / "provenance" / f"{p.ref}.json").read_text())
        assert set(prov) == set(p.content), p.ref
        for key, source in prov.items():
            assert source.startswith(("orcaslicer:resources/profiles/", "madfam:")), (p.ref, key, source)
    tpu = catalog.resolve("process", "tpu-safe-0.20-klipper")
    prov = json.loads((ROOT / "provenance" / f"{tpu.ref}.json").read_text())
    assert prov["outer_wall_speed"] == "madfam:tpu_safe"
    assert tpu.content["outer_wall_speed"] == "25"


def test_authored_values_are_declared_with_reasons():
    catalog_doc = json.loads((ROOT / "catalog.json").read_text())
    groups = catalog_doc["authored_groups"]
    for entry in catalog_doc["profiles"]:
        for spec in entry["authored"].values():
            assert spec["group"] in groups and len(groups[spec["group"]]["reason"]) > 40
            assert entry["base"]["chain"], entry["id"]
    for entry in catalog_doc["profiles"]:
        for link in entry["base"]["chain"]:
            assert len(link["blob"]) == 40 and link["path"].startswith("resources/profiles/")


def test_compatibility_rules_are_consistent():
    catalog = get_catalog()
    printers = {p.id: p for p in catalog.by_kind("printer")}
    assert {p.target for p in printers.values()} == {"klipper_gcode", "bambu_3mf"}
    for proc in catalog.by_kind("process"):
        # The 2.4.2 CLI requires the process to name the printer (proven in test_orcaslicer).
        assert proc.content["compatible_printers"] == [printers[i].ref for i in proc.printers]
    for fil in catalog.by_kind("filament"):
        assert set(fil.printers) <= set(printers)
        if fil.material_class == "tpu-95a":
            assert fil.requires_process_tag == "tpu-safe"
    for printer in printers.values():
        assert printer.content["curr_bed_type"] == "Textured PEI Plate"


def test_material_classes_and_print_keys_exist_in_the_vocabulary():
    vocab = get_vocabulary()
    for fil in get_catalog().by_kind("filament"):
        assert fil.material_class in vocab.material_classes
    standard = get_catalog().resolve("process", "standard-0.20-klipper").content
    for key in (
        "wall_loops",
        "top_shell_layers",
        "bottom_shell_layers",
        "sparse_infill_density",
        "layer_height",
        "enable_support",
        "outer_wall_speed",
    ):
        assert key in standard


def test_summary_shape():
    s = get_catalog().resolve("filament", "tpu-95a-bambu-a1").summary()
    assert s["ref"] == "tpu-95a-bambu-a1@1" and s["requires_process_tag"] == "tpu-safe"
    assert s["material_class"] == "tpu-95a" and s["printers"] == ["bambu-a1-0.4"]
