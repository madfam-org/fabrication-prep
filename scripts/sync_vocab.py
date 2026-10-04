#!/usr/bin/env python3
"""Derive fabrication_prep/vocab/fabrication.json from hyperobjects-spec's fabrication vocabularies.

    python scripts/sync_vocab.py --spec <hyperobjects-spec checkout> [--rev origin/main] [--check]

Only the facts this service enforces are kept: every process-parameters key with its unit, value type and
OrcaSlicer preset/option type, and the keys of material-classes and processes. The source commit and the git
blob id of each vocabulary file are recorded so drift is detectable; ``--check`` exits non-zero when the
committed file differs from what the given revision produces. hyperobjects-spec is Apache-2.0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fabrication_prep" / "vocab" / "fabrication.json"
BASE = "src/hyperobjects_lexicon/vocabularies/fabrication"


def git(spec: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(spec), *args], check=True, capture_output=True, text=True).stdout  # noqa: S603,S607


def derive(spec: Path, rev: str) -> dict:
    commit = git(spec, "rev-parse", rev).strip()
    out: dict = {
        "source": {
            "repository": "https://github.com/madfam-org/hyperobjects-spec",
            "commit": commit,
            "licence": "Apache-2.0",
            "files": {},
        }
    }
    for name in ("process-parameters", "material-classes", "processes"):
        path = f"{BASE}/{name}.json"
        out["source"]["files"][name] = {"path": path, "blob": git(spec, "rev-parse", f"{commit}:{path}").strip()}
        doc = json.loads(git(spec, "show", f"{commit}:{path}"))
        if name == "process-parameters":
            out["process_parameters"] = {
                e["key"]: {
                    "unit": e["unit"],
                    "value_type": e["value_type"],
                    "preset": e["orcaslicer"]["preset"],
                    "option_type": e["orcaslicer"]["option_type"],
                    "orcaslicer_key": e["orcaslicer"]["key"],
                }
                for e in doc["entries"]
            }
        elif name == "material-classes":
            out["material_classes"] = {
                e["key"]: {"kind": e["kind"], "processes": e.get("processes", [])} for e in doc["entries"]
            }
        else:
            out["processes"] = sorted(e["key"] for e in doc["entries"])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--rev", default="origin/main")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = json.dumps(derive(args.spec, args.rev), indent=1, ensure_ascii=False) + "\n"
    if args.check:
        if OUT.read_text(encoding="utf-8") != text:
            print("vocab/fabrication.json differs from the vocabularies at", args.rev)
            return 1
        print("vocab/fabrication.json is current")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print("wrote", OUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
