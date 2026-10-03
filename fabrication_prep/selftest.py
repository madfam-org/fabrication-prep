"""``fabrication-prep selftest-slice``: slice a built-in 20 mm cube with the shipped profiles, no database.

Used by the image build in CI (the Linux proof of the CLI contract) and by operators after an upgrade. It goes
through the same adapter as the worker (command line, output collection, estimates, config dump) and checks
that the slicer reports the version the profiles are pinned to.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .canonical import canonical_json
from .effective import effective_values
from .profiles import get_catalog
from .slicer import probe_version, run_slicer
from .vocab import get_vocabulary

DEFAULTS = {
    "klipper_gcode": ("klipper-corexy-350-0.4", "standard-0.20-klipper", "pla-generic-klipper"),
    "bambu_3mf": ("bambu-a1-0.4", "standard-0.20-bambu-a1", "pla-generic-bambu-a1"),
}


def cube_stl(size: float = 20.0) -> str:
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    lines = ["solid cube"]
    for a, b, c in faces:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += [f"vertex {v[i][0]} {v[i][1]} {v[i][2]}" for i in (a, b, c)]
        lines += ["endloop", "endfacet"]
    lines.append("endsolid cube")
    return "\n".join(lines) + "\n"


def selftest(binary: str, target: str, workdir: Path, timeout: float = 600) -> dict:
    catalog = get_catalog()
    version = probe_version(binary)
    if version != catalog.orcaslicer["version"]:
        raise RuntimeError(f"OrcaSlicer {version} installed; profiles pinned to {catalog.orcaslicer['version']}")
    printer, process, filament = (
        catalog.resolve(k, r) for k, r in zip(("printer", "process", "filament"), DEFAULTS[target], strict=True)
    )
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    for kind, profile in (("printer", printer), ("process", process), ("filament", filament)):
        (workdir / f"{kind}.json").write_bytes(canonical_json(profile.content))
    model = workdir / "cube.stl"
    model.write_text(cube_stl())
    out = run_slicer(binary, workdir, model, target, timeout, lambda: False)
    return {
        "target": target,
        "orcaslicer": version,
        "gcode_reports": out.orcaslicer_version,
        "profiles": [printer.ref, process.ref, filament.ref],
        "output": {"file": out.path.name, "bytes": out.path.stat().st_size, "media_type": out.media_type},
        "estimates": out.estimates,
        "warnings": out.warnings,
        "effective": effective_values(out.config, get_vocabulary()),
    }


def main(binary: str, target: str, workdir: str) -> int:
    report = selftest(binary, target, Path(workdir))
    print(json.dumps(report, indent=2, sort_keys=True))
    ok = report["gcode_reports"] == report["orcaslicer"] and report["estimates"].get("print_time_s", 0) > 0
    return 0 if ok else 1
