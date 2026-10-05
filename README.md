# fabrication-prep

> Boundary: this repository is MADFAM's public fabrication-prep service (slicing and printer, filament and
> process profiles). It holds no secrets, hostnames of internal nodes, or private runbooks. Identity is Janua,
> orchestration is pravara-mes; this service never moves a machine.

fabrication-prep turns a generator render bundle (GOC-1: geometry plus `variables.json`) into printer-ready
output with the [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) command line and versioned,
digest-pinned printer, filament and process profiles:

| Target | Printer class | Output | Media type |
|---|---|---|---|
| `klipper_gcode` | Klipper (Moonraker), e.g. Voron 2.4 class CoreXY | plain G-code `plate_1.gcode` | `text/x-gcode` |
| `bambu_3mf` | Bambu Lab A1 class (LAN mode) | sliced 3MF `plate_1.gcode.3mf` | `model/3mf` |

Every job also produces `slicer-variables.json`: the input and GOC-1 instance digests, the profile digests, the
overrides, the effective process values as OrcaSlicer reported them, the OrcaSlicer version, the output digest,
the slicer's estimates (print time, filament) and its warnings. pravara-mes records these in the product
passport (MES-1 §7).

Status: **v1, in development, not deployed.** Licence: AGPL-3.0-only (see `LICENSE`).

## Where it sits

```
yantra4d (render bundle: STL/3MF + variables.json)
      │  HTTPS, allow-listed hosts, sha256-verified
      ▼
pravara-mes dispatcher ──POST /v1/slice-jobs──▶ fabrication-prep API ──▶ Postgres queue ──▶ worker (OrcaSlicer CLI)
      ▲                                                                                        │
      └──GET /v1/slice-jobs/{id}: status + signed URLs (output, slicer-variables.json) ◀───────┘
pravara edge node: downloads the signed URL, verifies the sha256, uploads to the printer (MES-1 §2).
```

Separation of concerns: Janua issues identity; pravara-mes orchestrates and dispatches; yantra4d generates
geometry; this service only slices. It never contacts a printer.

## API (`/v1`, OpenAPI at `/openapi.json`)

| Method + path | Auth | Result |
|---|---|---|
| `POST /v1/slice-jobs` | Janua token, scope `fabrication-prep:slice` | `202` + job (`Location` header); `Idempotency-Key` replay `200`; reuse with another body `409`; invalid `422` |
| `GET /v1/slice-jobs/{id}` | same, owner only (`404` for others) | status, attempts, error, and when succeeded `output` + `slicer_variables` with fresh signed URLs and `estimates` |
| `GET /v1/profiles` | same | the profile catalog (ids, versions, digests, compatibility) |
| `GET /v1/profiles/{kind}/{id}/{version}` | same | one profile's OrcaSlicer JSON |
| `GET /v1/artifacts/{sha256}?exp&kid&sig` | signed URL only | the bytes; `403` when the signature is missing, wrong, expired or for another artifact |
| `GET /health`, `GET /ready` | none | liveness; readiness = DB round trip at the expected schema revision |

Request body (MES-1 §4):

```json
{
  "input": {"url": "https://<render host>/…/part.stl", "sha256": "<64 hex>", "media_type": "model/stl",
            "variables": {"url": "https://<render host>/…/part.stl.variables.json", "sha256": "<64 hex>"}},
  "printer_profile": "klipper-corexy-350-0.4",
  "process_profile": "standard-0.20-klipper",
  "filament_profile": "petg-generic-klipper@1",
  "overrides": {"wall_loops": 4, "sparse_infill_density": 40},
  "requirements": {"process": ["fff"], "materials": {"any_of": ["petg", "pla"]},
                   "process_parameters": {"wall_loops": {"min": 3}, "sparse_infill_density": {"min": 30, "unit": "percent"}}},
  "part": "corner_3way",
  "target": "klipper_gcode"
}
```

* `input.variables` (optional) is the GOC-1 sidecar. When given, the worker verifies its digests (GOC-1 §3.2,
  §3.4) and that it lists the input's sha256, and copies `instance_id`/`variables_sha256` into
  `slicer-variables.json`.
* Profile references are `id` (newest version) or `id@version`; the job records the resolved version and digest.
* `overrides` keys must be `process-parameters` vocabulary keys whose OrcaSlicer preset is `print` or
  `filament`. Printer keys (`nozzle_diameter`) and the `bed_temperature` placeholder are refused: choose another
  printer profile, or override the active plate's key.
* `requirements` is a RequirementProfile (`min`/`max` inclusive, or `value`), with optional `parts.<part>`
  layered over the top level for the named `part`. A violation is a `422` naming the key, the range and the
  value: `{"code": "override_out_of_range", "path": "/overrides/wall_loops", "details": {"key": "wall_loops",
  "value": 2, "min": 3, "unit": "count", "source": "override"}}`. Profile values are checked before slicing;
  after slicing the worker re-checks against OrcaSlicer's own record of the values it used and fails the job
  (`requirements_not_met`) if any is out of range.

Job states: `queued` → `running` → `succeeded` | `failed` (permanent: digest mismatch, slicer refused the input,
requirements not met) | `dead_lettered` (transient failures exhausted `JOB_MAX_ATTEMPTS`). Terminal transitions
emit outbox events `slice_job.succeeded|failed|dead_lettered`.

## Slicing — what is proven

Measured with the official OrcaSlicer **2.4.2** macOS universal release on 2026-10-03, slicing a real yantra4d
render (tslot-corner `corner_3way`, GOC-1 `complete: true`) through the full API → queue → worker path:

* `--slice 0 --outputdir D` writes plain G-code to `D/plate_1.gcode`. That is the Klipper output; no 3MF step.
* `--export-3mf NAME` writes a sliced 3MF, and `NAME` is resolved **relative to `--outputdir`**. Its
  `Metadata/plate_1.gcode`, `plate_1.gcode.md5` and `slice_info.config` (`prediction` s, `weight` g) are checked
  before the 3MF is accepted. That is the Bambu output.
* Loose profile files must be **flattened**: the CLI does not resolve `inherits` for them, and the process must
  name the printer in `compatible_printers` with the printer's `from` set to `system`. The shipped profiles do both.
* The CLI defaults the plate to *Cool Plate*; the printer profiles pin `curr_bed_type`.
* Headless runs skip thumbnails (no OpenGL); slicing is unaffected.
* The Linux image runs `fabrication-prep selftest-slice` for both targets in CI. The Linux path is proven only by
  that job: images were not built on the development host.

Not proven yet: printing either output on physical hardware (the edge bridge lane, MES-1 §2). The Bambu
3MF's `printer_model_id` metadata is empty, because the 2.4.2 CLI looks for `profiles/BBL/machine_full/`,
which the release does not ship. Whether the A1 firmware accepts that file is unverified.

## Profiles

`fabrication_prep/profiles/`: 12 flattened OrcaSlicer JSON profiles, `catalog.json` (ids, versions, sha256 of the
canonical JSON, compatibility, base chain with git blob ids verified against the v2.4.2 tag) and
`provenance/<ref>.json` (the OrcaSlicer file behind every value, or the MADFAM authoring group and its reason).
Rebuild: `python scripts/build_profiles.py --orca-profiles <OrcaSlicer resources/profiles> --tree-json <tree>`.
Profiles are immutable: a change is a new version. The worker refuses to start when the installed CLI is not
the version the profiles are pinned to.

| Profile | Base (OrcaSlicer 2.4.2 system profile) | MADFAM-authored values |
|---|---|---|
| printer `klipper-corexy-350-0.4@1` | Voron / `Voron 2.4 350 0.4 nozzle` | `curr_bed_type` = Textured PEI Plate |
| printer `bambu-a1-0.4@1` | BBL / `Bambu Lab A1 0.4 nozzle` | `curr_bed_type` = Textured PEI Plate (Orca's own A1 `default_bed_type`) |
| filament `pla-generic-klipper@1` / `petg-generic-klipper@1` | OrcaFilamentLibrary / `Generic PLA @System`, `Generic PETG @System` | none |
| filament `tpu-95a-klipper@1` | OrcaFilamentLibrary / `Bambu TPU 95A @System` | none |
| filament `pla-generic-bambu-a1@1` / `petg-generic-bambu-a1@1` | BBL / `Generic PLA @BBL A1`, `Generic PETG @BBL A1` | none |
| filament `tpu-95a-bambu-a1@1` | BBL / `Bambu TPU 95A @BBL A1` | none |
| process `standard-0.20-klipper@1` | Voron / `0.20mm Standard @Voron` | none |
| process `standard-0.20-bambu-a1@1` | BBL / `0.20mm Standard @BBL A1` | none |
| process `tpu-safe-0.20-klipper@1`, `tpu-safe-0.20-bambu-a1@1` | the standard process of that printer | 16 speed/acceleration values (below), not yet validated on a printer |

For every profile, the name, `from` and compatibility keys are rewritten so that our ids appear in the G-code
(`printer_settings_id = klipper-corexy-350-0.4@1`). TPU 95A filaments require a `tpu-safe` process.

TPU-safe values (mm/s, mm/s²): `outer_wall_speed` 25, `inner_wall_speed` 30, `sparse_infill_speed` 30, `internal_solid_infill_speed` 30, `top_surface_speed` 25, `gap_infill_speed` 25, `initial_layer_speed` 15, `initial_layer_infill_speed` 20, `bridge_speed` 20, `support_speed` 30, `support_interface_speed` 20, `default_acceleration` 2000, `outer_wall_acceleration` 1000, `inner_wall_acceleration` 2000, `top_surface_acceleration` 1000, `travel_acceleration` 3000. Reason: MADFAM authoring, not an OrcaSlicer value. The TPU 95A filament profiles cap volumetric flow at 3.2-3.6 mm3/s; at 0.2 mm layers and ~0.45 mm lines that is ~35-40 mm/s. Line speeds are set below that so the cap never silently rescales them, and accelerations are reduced for a soft filament in a direct-drive extruder. Not yet validated on a physical printer.

## Run locally

```bash
python3.13 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
# throwaway Postgres (TCP only), then the roles:
psql -h 127.0.0.1 -p <port> -U postgres -f scripts/test-db-setup.sql
export FABRICATION_PREP_TEST_ADMIN_URL=postgresql://fabrication_prep_owner@127.0.0.1:<port>/fabrication_prep_test
export FABRICATION_PREP_TEST_APP_URL=postgresql://fabrication_prep_app@127.0.0.1:<port>/fabrication_prep_test
pytest -m "not orcaslicer" --cov=fabrication_prep          # what CI runs
ORCASLICER_BIN=<path to the CLI> ORCA_PROOF_BUNDLE_DIR=<dir with part.stl + part.stl.variables.json> \
  pytest -m orcaslicer                                      # the real-slicer proof
fabrication-prep selftest-slice --target bambu_3mf --bin <path to the CLI> --workdir /tmp/st
```

Configuration is environment-only (`fabrication_prep/settings.py` documents each key and where it comes from).

## Documents

* `docs/STATUS.md` — where the service stands (as of 2026-10-05): what landed, open PRs in merge order, next steps.
* `AGENTS.md` — rules for agents and contributors working in this repository.
* `docs/operator-setup.md` — the owner's steps: push, Janua client, one `enclii onboard` (database, runtime role and
  generated keys), `enclii buckets create`, deploy wiring, smoke. No step handles a credential.
* `fabrication_prep/schemas/slicer-variables.schema.json` — the output document's JSON Schema.
* MES-1 contract (MADFAM internal) §4 — this service's contract.
