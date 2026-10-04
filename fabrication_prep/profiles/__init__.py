"""Versioned OrcaSlicer profiles shipped with the service (see catalog.json and scripts/build_profiles.py).

Every profile is a flattened OrcaSlicer JSON document. Its digest is the GOC-1 canonical-JSON sha256 of that
document; the catalog records it, and ``load_catalog`` refuses to start when any file does not match.
Profiles are immutable once released: a change is a new version, never an edit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..canonical import canonical_sha256

ROOT = Path(__file__).resolve().parent
KINDS = ("printer", "filament", "process")
REF_PATTERN = re.compile(r"^(?P<id>[a-z0-9][a-z0-9.-]{0,79})(?:@(?P<version>[1-9][0-9]{0,5}))?$")


class ProfileIntegrityError(RuntimeError):
    """A shipped profile file does not match its catalog digest."""


class ProfileLookupError(LookupError):
    """A profile reference does not resolve."""


@dataclass(frozen=True)
class Profile:
    id: str
    version: int
    kind: str
    sha256: str
    file: str
    label: dict[str, str]
    content: dict[str, Any] = field(repr=False, compare=False, hash=False)
    target: str | None = None
    material_class: str | None = None
    printers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    requires_process_tag: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "version": self.version,
            "ref": self.ref,
            "kind": self.kind,
            "sha256": self.sha256,
            "label": self.label,
        }
        for key in ("target", "material_class", "requires_process_tag"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.printers:
            out["printers"] = list(self.printers)
        if self.tags:
            out["tags"] = list(self.tags)
        return out


@dataclass(frozen=True)
class Catalog:
    orcaslicer: dict[str, Any]
    profiles: tuple[Profile, ...]

    def by_kind(self, kind: str) -> list[Profile]:
        return [p for p in self.profiles if p.kind == kind]

    def resolve(self, kind: str, reference: str) -> Profile:
        """``id`` (the newest version) or ``id@version``."""
        match = REF_PATTERN.fullmatch(reference or "")
        if match is None:
            raise ProfileLookupError(f"'{reference}' is not a profile reference (id or id@version)")
        candidates = [p for p in self.profiles if p.kind == kind and p.id == match["id"]]
        if not candidates:
            raise ProfileLookupError(f"no {kind} profile '{match['id']}'")
        if match["version"] is None:
            return max(candidates, key=lambda p: p.version)
        version = int(match["version"])
        for p in candidates:
            if p.version == version:
                return p
        raise ProfileLookupError(f"{kind} profile '{match['id']}' has no version {version}")

    def get(self, kind: str, profile_id: str, version: int) -> Profile | None:
        for p in self.profiles:
            if p.kind == kind and p.id == profile_id and p.version == version:
                return p
        return None


def load_catalog(root: Path = ROOT) -> Catalog:
    doc = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    if doc.get("format") != "madfam.fabrication-prep.profile-catalog":
        raise ProfileIntegrityError("catalog.json has an unexpected format")
    profiles = []
    seen: set[tuple[str, str, int]] = set()
    for entry in doc["profiles"]:
        if entry["kind"] not in KINDS:
            raise ProfileIntegrityError(f"{entry['id']}: unknown kind {entry['kind']!r}")
        key = (entry["kind"], entry["id"], int(entry["version"]))
        if key in seen:
            raise ProfileIntegrityError(f"{entry['id']}@{entry['version']}: listed twice")
        seen.add(key)
        path = (root / entry["file"]).resolve()
        if root.resolve() not in path.parents:
            raise ProfileIntegrityError(f"{entry['id']}: file outside the profile directory")
        content = json.loads(path.read_text(encoding="utf-8"))
        digest = canonical_sha256(content)
        if digest != entry["sha256"]:
            raise ProfileIntegrityError(f"{entry['id']}@{entry['version']}: content digest does not match the catalog")
        if content.get("name") != f"{entry['id']}@{entry['version']}":
            raise ProfileIntegrityError(f"{entry['id']}@{entry['version']}: the file's name is not its reference")
        profiles.append(
            Profile(
                id=entry["id"],
                version=int(entry["version"]),
                kind=entry["kind"],
                sha256=digest,
                file=entry["file"],
                label=entry["label"],
                content=content,
                target=entry.get("target"),
                material_class=entry.get("material_class"),
                printers=tuple(entry.get("printers", ())),
                tags=tuple(entry.get("tags", ())),
                requires_process_tag=entry.get("requires_process_tag"),
            )
        )
    return Catalog(orcaslicer=doc["orcaslicer"], profiles=tuple(profiles))


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    return load_catalog()
