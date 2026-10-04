#!/usr/bin/env python3
"""Build the shipped OrcaSlicer profiles from OrcaSlicer's own system profiles (maintainers only).

    python scripts/build_profiles.py --orca-profiles <OrcaSlicer resources/profiles dir> \
        [--tree-json <GitHub git/trees/<tag>?recursive=1 response>]

For every recipe in ``scripts/profile-recipes.json`` it resolves the ``inherits`` chain of the named system
profile inside the vendor folder, merges it (child wins), removes the chain markers, applies the recipe's
authored values (each with a recorded reason) and writes:

* ``fabrication_prep/profiles/<kind>/<id>@<version>.json`` — the flattened OrcaSlicer JSON the worker loads;
* ``fabrication_prep/profiles/provenance/<id>@<version>.json`` — for every key, the OrcaSlicer file that
  supplied its value, or the authoring group that set it;
* ``fabrication_prep/profiles/catalog.json`` — ids, versions, compatibility, content digests, base chain
  with git blob ids (verified against the tag's tree when ``--tree-json`` is given).

Why flatten: the OrcaSlicer 2.4.2 CLI does not resolve ``inherits`` for loose files (measured: the BBL A1
system files loaded directly produced no filament weight and Cool Plate temperatures), and a digest of the
flattened file covers every value the slicer receives from the profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fabrication_prep.canonical import canonical_sha256  # noqa: E402

RECIPES = ROOT / "scripts" / "profile-recipes.json"
OUT = ROOT / "fabrication_prep" / "profiles"
CHAIN_MARKERS = ("inherits", "instantiation", "setting_id", "renamed_from", "from")
# Compatibility is enforced by the catalog (printer ids), not by OrcaSlicer preset names, because the
# shipped profiles carry our ids as names. Processes then list our printer refs (see below).
CLEARED_FOR_CATALOG = (
    "compatible_printers",
    "compatible_printers_condition",
    "compatible_prints",
    "compatible_prints_condition",
)


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def index_vendor(profiles: Path, vendor: str) -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for path in sorted((profiles / vendor).rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("name"), str):
            idx.setdefault(doc["name"], path)
    return idx


def resolve_chain(profiles: Path, vendor: str, name: str) -> list[tuple[Path, dict]]:
    idx = index_vendor(profiles, vendor)
    chain: list[tuple[Path, dict]] = []
    current: str | None = name
    while current:
        if current not in idx:
            raise SystemExit(f"{vendor}: profile {current!r} not found (chain of {name!r})")
        path = idx[current]
        doc = json.loads(path.read_text(encoding="utf-8"))
        chain.append((path, doc))
        current = doc.get("inherits") or None
        if len(chain) > 12:
            raise SystemExit(f"inherits chain too deep for {name!r}")
    return chain


def as_orca_value(existing, value):
    """An authored value in OrcaSlicer's JSON shape: strings, and a one-element list where the base uses a
    per-extruder vector."""
    text = ("1" if value else "0") if isinstance(value, bool) else str(value)
    return [text] if isinstance(existing, list) else text


def build(profiles: Path, tree: dict[str, str] | None) -> None:
    recipes = json.loads(RECIPES.read_text(encoding="utf-8"))
    orca = recipes["orcaslicer"]
    groups = recipes["authored_groups"]
    catalog_entries = []
    printer_refs = {r["id"]: f"{r['id']}@{r['version']}" for r in recipes["profiles"] if r["kind"] == "printer"}
    for recipe in recipes["profiles"]:
        ref = f"{recipe['id']}@{recipe['version']}"
        chain = resolve_chain(profiles, recipe["base"]["vendor"], recipe["base"]["name"])
        merged: dict = {}
        provenance: dict[str, str] = {}
        chain_meta = []
        for path, doc in reversed(chain):
            rel = path.relative_to(profiles).as_posix()
            data = path.read_bytes()
            blob = git_blob_sha(data)
            repo_path = f"{orca['profiles_path']}/{rel}"
            if tree is not None and tree.get(repo_path) != blob:
                raise SystemExit(f"{repo_path}: blob {blob} does not match the {orca['tag']} tree")
            chain_meta.append({"path": repo_path, "blob": blob})
            for key, value in doc.items():
                merged[key] = value
                provenance[key] = f"orcaslicer:{repo_path}"
        for key in CHAIN_MARKERS:
            merged.pop(key, None)
            provenance.pop(key, None)
        if recipe["kind"] in ("process", "filament"):
            for key in CLEARED_FOR_CATALOG:
                if key in merged:
                    merged[key] = [] if isinstance(merged[key], list) else ""
                    provenance[key] = "madfam:catalog-compatibility"
        if recipe["kind"] == "process":
            # The 2.4.2 CLI refuses a process whose compatible_printers does not name the loaded printer
            # (src/OrcaSlicer.cpp, "process not compatible with printer"); name our printer profiles.
            merged["compatible_printers"] = [printer_refs[p] for p in recipe["printers"]]
            provenance["compatible_printers"] = "madfam:catalog-compatibility"
        authored = {}
        for key, spec in recipe.get("set", {}).items():
            value, group = (spec["value"], spec["group"]) if isinstance(spec, dict) else (spec, recipe["set_group"])
            merged[key] = as_orca_value(merged.get(key), value)
            provenance[key] = f"madfam:{group}"
            authored[key] = {"value": merged[key], "group": group}
        merged["name"] = ref
        provenance["name"] = "madfam:profile-id"
        # "system": the 2.4.2 CLI takes a printer's own name as its system name only when from == "system"
        # (src/OrcaSlicer.cpp); otherwise it uses `inherits`, which flattened profiles no longer have.
        merged["from"] = "system"
        provenance["from"] = "madfam:profile-id"
        if recipe["kind"] == "printer":
            # The machine preset's own type tag; OrcaSlicer reads it from the file.
            merged["type"] = "machine"
        rel_file = f"{recipe['kind']}/{ref}.json"
        (OUT / recipe["kind"]).mkdir(parents=True, exist_ok=True)
        (OUT / rel_file).write_text(
            json.dumps(merged, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (OUT / "provenance").mkdir(parents=True, exist_ok=True)
        (OUT / "provenance" / f"{ref}.json").write_text(
            json.dumps(provenance, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        entry = {
            "id": recipe["id"],
            "version": recipe["version"],
            "kind": recipe["kind"],
            "label": recipe["label"],
            "file": rel_file,
            "sha256": canonical_sha256(merged),
            "base": {"vendor": recipe["base"]["vendor"], "name": recipe["base"]["name"], "chain": chain_meta},
            "authored": {k: {"value": v["value"], "group": v["group"]} for k, v in authored.items()},
        }
        for key in ("target", "material_class", "printers", "requires_process_tag", "tags"):
            if key in recipe:
                entry[key] = recipe[key]
        catalog_entries.append(entry)
        print(f"{ref:32s} {entry['sha256']}  chain={len(chain)} authored={len(authored)}")
    catalog = {
        "format": "madfam.fabrication-prep.profile-catalog",
        "format_version": "1.0.0",
        "orcaslicer": {**orca, "blob_url": f"{orca['repository']}/blob/{orca['tag']}/<path>"},
        "authored_groups": groups,
        "profiles": catalog_entries,
    }
    (OUT / "catalog.json").write_text(dump_catalog(catalog), encoding="utf-8")


def dump_catalog(catalog: dict) -> str:
    """Readable and short: one line per top-level key, and one line per key inside each profile entry."""

    def compact(value) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))

    lines = ["{"]
    keys = [k for k in catalog if k != "profiles"]
    for key in keys:
        lines.append(f"  {compact(key)}: {compact(catalog[key])},")
    lines.append('  "profiles": [')
    for i, entry in enumerate(catalog["profiles"]):
        lines.append("    {")
        items = list(entry.items())
        for j, (key, value) in enumerate(items):
            lines.append(f"      {compact(key)}: {compact(value)}{',' if j < len(items) - 1 else ''}")
        lines.append("    }" + ("," if i < len(catalog["profiles"]) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--orca-profiles", required=True, type=Path)
    parser.add_argument("--tree-json", type=Path)
    args = parser.parse_args()
    tree = None
    if args.tree_json:
        raw = json.loads(args.tree_json.read_text(encoding="utf-8"))
        if raw.get("truncated"):
            raise SystemExit("the tree listing is truncated; cannot verify blobs")
        tree = {e["path"]: e["sha"] for e in raw["tree"] if e["type"] == "blob"}
    build(args.orca_profiles, tree)
    return 0


if __name__ == "__main__":
    sys.exit(main())
