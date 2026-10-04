"""Canonical JSON and digests, identical to GOC-1 §3.1 (hyperobjects-spec ``canonical_json``).

Numbers are normalised first — every finite float with an integral value (and |x| < 2^53) becomes an int,
recursively — then ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)``
encoded as UTF-8. Profile digests, the effective-values digest and the slicer-variables document all use it,
so a consumer can recompute every digest with the keystone library.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

_INT_LIMIT = 2**53


def normalise_numbers(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not allowed in canonical JSON")
        if value.is_integer() and abs(value) < _INT_LIMIT:
            return int(value)
        return value
    if isinstance(value, dict):
        return {k: normalise_numbers(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalise_numbers(v) for v in value]
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        normalise_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def goc1_variables_sha256(variables: list[dict[str, Any]]) -> str:
    """GOC-1 §3.2: sha256 of the canonical JSON of [[id, value], ...] sorted by id (bytewise)."""
    pairs = sorted(([v["id"], v["value"]] for v in variables), key=lambda p: p[0].encode("utf-8"))
    return canonical_sha256(pairs)


def goc1_instance_id(cartridge: str, mode: str, part: str | None, tree_sha256: str, variables_sha256: str) -> str:
    """GOC-1 §3.4."""
    return canonical_sha256(
        {
            "cartridge": cartridge,
            "mode": mode,
            "part": part,
            "tree_sha256": tree_sha256,
            "variables_sha256": variables_sha256,
        }
    )
