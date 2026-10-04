"""selftest-slice (the image build's Linux proof) and the adapter's handling of a missing CLI."""

from __future__ import annotations

import json
import os
import stat

import pytest

from fabrication_prep import cli, selftest, slicer
from fabrication_prep.slicer import SlicerError, run_slicer
from tests.slicer_fakes import FakeRunner


def test_cube_is_a_closed_ascii_stl():
    text = selftest.cube_stl()
    assert text.startswith("solid cube") and text.count("facet normal") == 12 and text.count("vertex") == 36


def test_missing_binary_is_a_transient_slicer_error(tmp_path):
    with pytest.raises(SlicerError) as exc:
        run_slicer(
            str(tmp_path / "missing" / "orca"), tmp_path / "w", tmp_path / "m.stl", "klipper_gcode", 10, lambda: False
        )
    assert exc.value.code == "slicer_unavailable" and exc.value.transient


@pytest.mark.parametrize("target", ["klipper_gcode", "bambu_3mf"])
def test_selftest_with_the_fake_cli(tmp_path, monkeypatch, capsys, target):
    fake = tmp_path / "orca"
    fake.write_text('#!/bin/sh\necho "OrcaSlicer-2.4.2:"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    real_run = slicer.run_slicer
    monkeypatch.setattr(selftest, "run_slicer", lambda *a, **k: real_run(*a, runner=FakeRunner(), **k))
    assert cli.main(["selftest-slice", "--target", target, "--workdir", str(tmp_path / "w"), "--bin", str(fake)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["gcode_reports"] == "2.4.2" and report["estimates"]["print_time_s"] > 0
    assert report["effective"]["layer_height"] == 0.2


def test_selftest_refuses_another_slicer_version(tmp_path):
    fake = tmp_path / "orca"
    fake.write_text('#!/bin/sh\necho "OrcaSlicer-9.9.9:"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(RuntimeError):
        selftest.selftest(str(fake), "klipper_gcode", tmp_path / "w")


@pytest.mark.orcaslicer
@pytest.mark.parametrize("target", ["klipper_gcode", "bambu_3mf"])
def test_selftest_with_the_real_cli(tmp_path, target):
    binary = os.environ.get("ORCASLICER_BIN")
    if not binary:
        raise RuntimeError("ORCASLICER_BIN is not set; the real-slicer proof cannot run (it is not skipped)")
    report = selftest.selftest(binary, target, tmp_path / "w")
    assert report["gcode_reports"] == "2.4.2" and report["estimates"]["filament_g"] > 0
