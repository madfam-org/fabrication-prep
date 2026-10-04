"""The fabrication vocabularies this service enforces (derived by scripts/sync_vocab.py from
hyperobjects-spec, Apache-2.0; the source commit and blob ids are inside the JSON file)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

PATH = Path(__file__).resolve().parent / "fabrication.json"

# OrcaSlicer's enum strings for the one enum key in process-parameters, at the slicer version the worker
# runs (src/libslic3r/PrintConfig.cpp v2.4.2, lines 3017-3047). The vocabulary deliberately does not
# re-list them; this list must follow the bundled slicer, not the vocabulary.
ORCA_ENUMS: dict[str, frozenset[str]] = {
    "sparse_infill_pattern": frozenset(
        "rectilinear alignedrectilinear zigzag crosszag lockedzag line grid triangles tri-hexagon cubic "
        "adaptivecubic quartercubic supportcubic lightning honeycomb 3dhoneycomb lateral-honeycomb "
        "lateral-lattice crosshatch tpmsd tpmsfk gyroid concentric hilbertcurve archimedeanchords "
        "octagramspiral".split()
    )
}

# OrcaSlicer bed types (PrintConfig.cpp v2.4.2 lines 471-476) and the filament key that holds each plate's
# bed temperature: the vocabulary's `bed_temperature` resolves through the active plate.
PLATE_TEMPERATURE_KEYS: dict[str, str] = {
    "Supertack Plate": "supertack_plate_temp",
    "Cool Plate": "cool_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Textured Cool Plate": "textured_cool_plate_temp",
}


@dataclass(frozen=True)
class ProcessParameter:
    key: str
    unit: str
    value_type: str  # integer | number | string | boolean
    preset: str  # print | filament | printer | placeholder
    option_type: str
    orcaslicer_key: str


@dataclass(frozen=True)
class Vocabulary:
    source: dict[str, Any]
    process_parameters: dict[str, ProcessParameter]
    material_classes: frozenset[str]
    processes: frozenset[str]


def load_vocabulary(path: Path = PATH) -> Vocabulary:
    doc = json.loads(path.read_text(encoding="utf-8"))
    params = {k: ProcessParameter(key=k, **v) for k, v in doc["process_parameters"].items()}
    return Vocabulary(
        source=doc["source"],
        process_parameters=params,
        material_classes=frozenset(doc["material_classes"]),
        processes=frozenset(doc["processes"]),
    )


@lru_cache(maxsize=1)
def get_vocabulary() -> Vocabulary:
    return load_vocabulary()
