"""OrcaSlicer values <-> typed values, overrides, and the effective process-parameter values.

OrcaSlicer's JSON stores every value as a string, and per-extruder options as a list of strings (some
system profiles also write those as a bare string). This module converts in both directions for the
process-parameters vocabulary keys, applies validated overrides to copies of the profiles, and reads the
slicer's own record of the values it used: the ``; key = value`` lines between ``CONFIG_BLOCK_START`` and
``CONFIG_BLOCK_END`` in the G-code it wrote. That record is authoritative; profile-derived values are only
used before slicing, to reject impossible requests early.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from .vocab import PLATE_TEMPERATURE_KEYS, ProcessParameter, Vocabulary

PRESET_DOCS = {"print": "process", "filament": "filament", "printer": "printer"}


class ValueParseError(ValueError):
    pass


def _first(raw: Any) -> Any:
    if isinstance(raw, list):
        if not raw:
            raise ValueParseError("empty vector")
        return raw[0]
    return raw


def parse_orca_value(raw: Any, param: ProcessParameter) -> Any:
    """The first (single-extruder) value of an OrcaSlicer option, typed per the vocabulary."""
    text = _first(raw)
    if isinstance(text, str) and "," in text and param.option_type.endswith("s"):
        text = text.split(",")[0]  # the G-code dump joins vectors with commas
    text = str(text).strip()
    try:
        if param.value_type == "boolean":
            if text.lower() in ("1", "true"):
                return True
            if text.lower() in ("0", "false"):
                return False
            raise ValueParseError(f"not a boolean: {text!r}")
        if param.value_type == "integer":
            return int(text)
        if param.value_type == "number":
            value = float(text[:-1] if text.endswith("%") else text)
            if not math.isfinite(value):
                raise ValueParseError("non-finite number")
            return value
        return text
    except ValueError as exc:
        raise ValueParseError(f"{param.key}: cannot read {text!r} as {param.value_type}") from exc


def format_orca_value(value: Any, param: ProcessParameter, existing: Any) -> Any:
    """A typed value in the shape OrcaSlicer reads, following the existing value's shape."""
    if isinstance(value, bool):
        text = "1" if value else "0"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = format(value, ".10g")
    else:
        text = str(value)
    if param.option_type in ("coPercent", "coPercents"):
        text = f"{text}%"
    vector = isinstance(existing, list) if existing is not None else param.option_type.endswith("s")
    return [text] if vector else text


def apply_overrides(
    docs: dict[str, dict[str, Any]], overrides: dict[str, Any], vocab: Vocabulary
) -> dict[str, dict[str, Any]]:
    """Copies of {"printer", "process", "filament"} with validated overrides applied (print/filament keys)."""
    out = {kind: copy.deepcopy(doc) for kind, doc in docs.items()}
    for key, value in overrides.items():
        param = vocab.process_parameters[key]
        kind = PRESET_DOCS[param.preset]
        target = out[kind]
        target[param.orcaslicer_key] = format_orca_value(value, param, target.get(param.orcaslicer_key))
    return out


def _bed_temperature(lookup, curr_bed_type: str | None) -> Any:
    key = PLATE_TEMPERATURE_KEYS.get(curr_bed_type or "")
    return None if key is None else lookup(key)


def effective_values(source: dict[str, Any], vocab: Vocabulary) -> dict[str, Any]:
    """Typed values of every vocabulary key found in a flat {orca_key: raw} mapping (a merged profile set or
    the G-code dump). ``bed_temperature`` resolves through ``curr_bed_type``; absent keys are omitted."""
    out: dict[str, Any] = {}
    for key, param in vocab.process_parameters.items():
        if param.preset == "placeholder":
            continue
        raw = source.get(param.orcaslicer_key)
        if raw is None:
            continue
        out[key] = parse_orca_value(raw, param)
    bed = vocab.process_parameters.get("bed_temperature")
    if bed is not None:
        raw = _bed_temperature(source.get, _first(source.get("curr_bed_type", "")) or None)
        if raw is not None:
            out["bed_temperature"] = parse_orca_value(raw, bed)
    return out


def merged_profile_source(docs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One flat mapping in OrcaSlicer's load order (printer, then process, then filament)."""
    merged: dict[str, Any] = {}
    for kind in ("printer", "process", "filament"):
        merged.update(docs[kind])
    return merged


def parse_config_block(gcode: str) -> dict[str, str]:
    """The ``; key = value`` lines between CONFIG_BLOCK_START and CONFIG_BLOCK_END."""
    values: dict[str, str] = {}
    inside = False
    for line in gcode.splitlines():
        stripped = line.strip()
        if stripped == "; CONFIG_BLOCK_START":
            inside = True
            continue
        if stripped == "; CONFIG_BLOCK_END":
            break
        if inside and stripped.startswith("; ") and " = " in stripped:
            key, _, value = stripped[2:].partition(" = ")
            values[key.strip()] = value
    return values
