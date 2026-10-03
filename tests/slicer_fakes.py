"""Test doubles: a GOC-1 render bundle, an HTTP transport serving it, and a fake OrcaSlicer runner.

``FakeRunner`` honours the CLI contract the adapter relies on (proven against the real 2.4.2 CLI in
tests/test_orcaslicer.py): it reads the profile files named by ``--load-settings``/``--load-filaments``,
writes ``plate_1.gcode`` into ``--outputdir`` with a header, estimate lines and a CONFIG_BLOCK carrying the
merged values, and for ``--export-3mf`` a zip with ``Metadata/plate_1.gcode`` (+ md5) and slice_info.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import httpx

from fabrication_prep.canonical import goc1_instance_id, goc1_variables_sha256
from fabrication_prep.slicer import RunResult

STL = (
    b"solid cube\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 0\n"
    b"endloop\nendfacet\nendsolid cube\n"
)
BASE = "https://bundles.test/r/"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sidecar_for(geometry: bytes, *, part: str | None = "corner_3way", list_geometry: bool = True) -> bytes:
    variables = [{"id": "leg_len", "value": 30, "type": "number", "source": "manifest_default"}]
    vsha = goc1_variables_sha256(variables)
    tree = "a" * 64
    doc = {
        "format": "hyperobjects.generator-output",
        "format_version": "1.0.0",
        "kind": "solid",
        "generator": {
            "platform": "yantra4d",
            "cartridge": "tslot-corner",
            "mode": "corner_3way",
            "part": part,
            "engine": "cadquery",
            "source": {"tree_sha256": tree},
        },
        "variables": variables,
        "variables_sha256": vsha,
        "complete": True,
        "geometry": [
            {
                "path": "x.stl",
                "media_type": "model/stl",
                "bytes": len(geometry),
                "sha256": sha(geometry) if list_geometry else "b" * 64,
                "role": "primary",
            }
        ],
        "instance_id": goc1_instance_id("tslot-corner", "corner_3way", part, tree, vsha),
    }
    return json.dumps(doc).encode()


def transport(files: dict[str, bytes | int]) -> httpx.Client:
    """URL -> bytes (200) or an int status code."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        if isinstance(body, int):
            return httpx.Response(body, headers={"location": BASE + "elsewhere"} if 300 <= body < 400 else None)
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def job_body(target: str = "klipper_gcode", with_sidecar: bool = True, **extra) -> dict:
    printer, process, filament = {
        "klipper_gcode": ("klipper-corexy-350-0.4", "standard-0.20-klipper", "pla-generic-klipper"),
        "bambu_3mf": ("bambu-a1-0.4", "standard-0.20-bambu-a1", "pla-generic-bambu-a1"),
    }[target]
    body = {
        "input": {"url": BASE + "part.stl", "sha256": sha(STL), "media_type": "model/stl"},
        "printer_profile": printer,
        "process_profile": process,
        "filament_profile": filament,
        "target": target,
    }
    if with_sidecar:
        body["input"]["variables"] = {"url": BASE + "part.stl.variables.json", "sha256": sha(sidecar_for(STL))}
    body.update(extra)
    return body


def bundle_files() -> dict[str, bytes]:
    return {BASE + "part.stl": STL, BASE + "part.stl.variables.json": sidecar_for(STL)}


class FakeRunner:
    def __init__(self, mode: str = "ok", version: str = "2.4.2", dump_overrides: dict | None = None):
        self.mode = mode
        self.version = version
        self.dump_overrides = dump_overrides or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd: Path, timeout, should_abort) -> RunResult:
        self.calls.append(cmd)
        if self.mode == "timeout":
            return RunResult(-9, ["killed"], timed_out=True)
        if self.mode == "abort":
            return RunResult(-9, ["killed"], aborted=True)
        if self.mode == "reject":
            return RunResult(239, ["run 2652: process not compatible with printer.", "run found error, exit"])
        if self.mode == "signal":
            return RunResult(-9, ["Killed"])
        out = Path(cmd[cmd.index("--outputdir") + 1])
        if self.mode == "no_output":
            return RunResult(0, ["done"])
        settings = cmd[cmd.index("--load-settings") + 1].split(";")
        merged: dict = {}
        for path in [*settings, cmd[cmd.index("--load-filaments") + 1]]:
            merged.update(json.loads(Path(path).read_text()))
        merged.update(self.dump_overrides)

        def dump(v) -> str:
            return ",".join(v) if isinstance(v, list) else str(v)

        config = "\n".join(f"; {k} = {dump(v)}" for k, v in sorted(merged.items()) if "\n" not in dump(v))
        gcode = (
            f"; HEADER_BLOCK_START\n; generated by OrcaSlicer {self.version} on 2026-10-03 at 12:00:00\n"
            "; HEADER_BLOCK_END\nG28\nG1 X10 Y10\n; filament used [mm] = 1779.21\n; filament used [cm3] = 4.28\n"
            "; filament used [g] = 5.22\n; total filament used [g] = 5.22\n"
            "; estimated printing time (normal mode) = 38m 34s\n"
            f"; CONFIG_BLOCK_START\n{config}\n; CONFIG_BLOCK_END\n"
        )
        (out / "plate_1.gcode").write_text(gcode)
        if "--export-3mf" in cmd:
            data = gcode.encode()
            info = Path(__file__).parent / "fixtures" / "orca-2.4.2-slice_info.config"
            with zipfile.ZipFile(out / cmd[cmd.index("--export-3mf") + 1], "w") as zf:
                zf.writestr("Metadata/plate_1.gcode", data)
                md5 = hashlib.md5(data, usedforsecurity=False).hexdigest().upper()
                zf.writestr("Metadata/plate_1.gcode.md5", "BAD" if self.mode == "bad_md5" else md5)
                zf.writestr("Metadata/slice_info.config", info.read_text())
        return RunResult(0, ["done"])
