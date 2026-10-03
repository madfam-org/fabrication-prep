"""The real OrcaSlicer CLI through the real worker path (local proof; deselected in CI with -m).

Needs ``ORCASLICER_BIN`` (the CLI binary) and ``ORCA_PROOF_BUNDLE_DIR`` (a GOC-1 render bundle: one geometry
file and its ``<file>.variables.json``). Without them these tests ERROR rather than skip. When
``ORCA_PROOF_OUT`` is set, the evidence (commands, digests, estimates, effective values) is written there.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import jsonschema
import pytest

from fabrication_prep.artifacts import FsArtifactStore
from fabrication_prep.effective import parse_config_block
from fabrication_prep.profiles import get_catalog
from fabrication_prep.settings import get_settings
from fabrication_prep.slicer import probe_version
from fabrication_prep.vocab import get_vocabulary
from fabrication_prep.worker import Worker
from tests.slicer_fakes import transport

pytestmark = pytest.mark.orcaslicer
BASE = "https://bundles.test/proof/"
SCHEMA = json.loads(
    (Path(__file__).parent.parent / "fabrication_prep" / "schemas" / "slicer-variables.schema.json").read_text()
)
EVIDENCE: dict = {}


def _need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; the real-slicer proof cannot run (it is not skipped)")
    return value


@pytest.fixture(scope="module")
def bundle():
    folder = Path(_need("ORCA_PROOF_BUNDLE_DIR"))
    geometry = next(p for p in sorted(folder.iterdir()) if p.suffix in (".stl", ".3mf"))
    sidecar = folder / (geometry.name + ".variables.json")
    return geometry, sidecar


def recording_runner(log: list):
    from fabrication_prep.slicer import subprocess_runner

    def run(cmd, cwd, timeout, abort):
        log.append(cmd)
        return subprocess_runner(cmd, cwd, timeout, abort)

    return run


@pytest.fixture(scope="module", autouse=True)
def write_evidence():
    yield
    if os.environ.get("ORCA_PROOF_OUT"):
        Path(os.environ["ORCA_PROOF_OUT"]).write_text(json.dumps(EVIDENCE, indent=2, sort_keys=True))


CASES = [
    ("klipper_gcode", "klipper-corexy-350-0.4", "standard-0.20-klipper", "pla-generic-klipper", {}),
    ("klipper_gcode", "klipper-corexy-350-0.4", "tpu-safe-0.20-klipper", "tpu-95a-klipper", {"wall_loops": 4}),
    ("bambu_3mf", "bambu-a1-0.4", "standard-0.20-bambu-a1", "petg-generic-bambu-a1", {}),
]


@pytest.mark.parametrize(("target", "printer", "process", "filament", "overrides"), CASES)
def test_real_slice(client, auth_header, bundle, target, printer, process, filament, overrides):
    geometry, sidecar = bundle
    s = get_settings().model_copy(update={"orcaslicer_bin": _need("ORCASLICER_BIN"), "slice_timeout_seconds": 600})
    media = "model/stl" if geometry.suffix == ".stl" else "model/3mf"
    body = {
        "input": {
            "url": BASE + geometry.name,
            "sha256": hashlib.sha256(geometry.read_bytes()).hexdigest(),
            "media_type": media,
            "variables": {"url": BASE + sidecar.name, "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()},
        },
        "printer_profile": printer,
        "process_profile": process,
        "filament_profile": filament,
        "overrides": overrides,
        "target": target,
        "requirements": {"process": ["fff"], "process_parameters": {"wall_loops": {"min": 2}}},
    }
    r = client.post("/v1/slice-jobs", json=body, headers=auth_header())
    assert r.status_code == 202, r.text
    commands: list = []
    files = {BASE + geometry.name: geometry.read_bytes(), BASE + sidecar.name: sidecar.read_bytes()}
    worker = Worker(
        s,
        get_catalog(),
        get_vocabulary(),
        FsArtifactStore(s.artifact_fs_root),
        http=transport(files),
        runner=recording_runner(commands),
        worker_id="proof",
    )
    assert worker.run_once()
    view = client.get(f"/v1/slice-jobs/{r.json()['id']}", headers=auth_header()).json()
    assert view["status"] == "succeeded", view["error"]
    root = Path(s.artifact_fs_root)
    output = (root / view["output"]["sha256"][:2] / view["output"]["sha256"]).read_bytes()
    doc = json.loads((root / view["slicer_variables"]["sha256"][:2] / view["slicer_variables"]["sha256"]).read_bytes())
    jsonschema.validate(doc, SCHEMA)
    if target == "bambu_3mf":
        with zipfile.ZipFile(root / view["output"]["sha256"][:2] / view["output"]["sha256"]) as zf:
            names = set(zf.namelist())
            gcode = zf.read("Metadata/plate_1.gcode").decode()
        assert {"Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5", "Metadata/slice_info.config"} <= names
    else:
        gcode = output.decode()
        assert gcode.startswith("; HEADER_BLOCK_START")
    dump = parse_config_block(gcode)
    vocab = get_vocabulary()
    missing = [
        p.orcaslicer_key
        for p in vocab.process_parameters.values()
        if p.preset != "placeholder" and p.orcaslicer_key not in dump
    ]
    assert missing == [], f"vocabulary keys absent from the 2.4.2 config dump: {missing}"
    for key, value in overrides.items():
        assert doc["effective"][key] == value
    assert dump["printer_settings_id"] == f"{printer}@1" and dump["print_settings_id"] == f"{process}@1"
    assert doc["estimates"]["print_time_s"] > 0 and doc["estimates"]["filament_g"] > 0
    assert doc["slicer"]["version"] == probe_version(s.orcaslicer_bin) == "2.4.2"
    EVIDENCE[f"{target}:{process}:{filament}"] = {
        "command": [
            c if not c.startswith(str(Path(s.worker_workdir))) else "<workdir>" + c[len(s.worker_workdir) :]
            for c in commands[0]
        ],
        "output": view["output"] | {"url": "<signed>"},
        "slicer_variables_sha256": view["slicer_variables"]["sha256"],
        "slicer_variables": doc,
        "zip_entries": sorted(names) if target == "bambu_3mf" else None,
    }
