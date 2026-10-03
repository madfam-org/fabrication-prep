# AGENTS.md — fabrication-prep

Rules for humans and agents changing this repository. Public repo, AGPL-3.0-only.

## What this service is (and is not)

* It **slices**: GOC-1 render bundle in, printer-ready file plus `slicer-variables.json` out (MES-1 §4).
* It **owns** the printer/filament/process profiles (`fabrication_prep/profiles/`).
* It does **not** move machines, contact printers, dispatch jobs or hold printer credentials. pravara-mes
  dispatches; its edge node uploads to printers. Nothing in this repository may open a connection to a printer.
* It does **not** compute geometry or change designs (yantra4d / fashion-cabinet do), issue identity (Janua) or
  quote (Cotiza).

## Invariants — never break these

1. **Fail closed on identity.** RS256 only, `kid` required, audience `fabrication-prep-api`, scope
   `fabrication-prep:slice`. No anonymous `/v1` route except the signed artifact download.
2. **No public bucket URLs.** Bytes leave only through `/v1/artifacts/{sha256}` with a short-lived HMAC signature
   bound to the sha256 (ADR-014). Keep the bucket private.
3. **Digests everywhere.** Input bytes, sidecars, profiles and outputs are verified against sha256; a mismatch is
   a permanent job failure, never a warning.
4. **Profiles are immutable.** Change a profile = new version (recipes → `scripts/build_profiles.py`), never an
   edit of a shipped file. Every value keeps its provenance (an OrcaSlicer file, or a MADFAM authoring group with
   a reason). Do not invent values without saying so in `scripts/profile-recipes.json`.
5. **Profiles are pinned to one OrcaSlicer version** (`catalog.json` → `orcaslicer.version`, and the
   `Dockerfile.worker` version + sha256). Upgrading the slicer means rebuilding the profiles from that release, the
   real-slicer tests, and a version bump of every profile whose content changed.
6. **Non-owner runtime role.** API and worker connect as `fabrication_prep_app` (NOSUPERUSER, NOBYPASSRLS, no
   table ownership); `check_role_posture` refuses anything else. Only the migrate init container sees the owner URL.
7. **Database errors never reach logs or responses** with detail (`describe_db_error`, `DbErrorScrubFilter`).
8. **The vocabulary is derived, not edited.** `fabrication_prep/vocab/fabrication.json` comes from
   hyperobjects-spec via `scripts/sync_vocab.py`; CI re-derives it at the recorded commit.

## Gates (all must pass before a PR leaves draft)

```bash
ruff check . && ruff format --check .
pytest -m "not orcaslicer" --cov=fabrication_prep --cov-fail-under=85     # needs the two test DB URLs
fabrication-prep check-profiles
python scripts/check_manifests.py <(kubectl kustomize infra/k8s/production)
PYTHON=<runtime-only venv python> scripts/check-licenses.sh
```

Tests marked `orcaslicer` slice with the real CLI (`ORCASLICER_BIN`, `ORCA_PROOF_BUNDLE_DIR`). They **error**,
never skip, when run without those; CI deselects them and instead runs `selftest-slice` inside the worker image.
A skipped or deselected test is not a pass: say which ran.

## Conventions

* Python 3.13, FastAPI, psycopg 3 (no ORM at runtime; Alembic migrations are plain SQL). English identifiers,
  Conventional Commits, files ≤ 600 lines.
* Errors: `{"errors": [{"code", "message", "path"?, "details"?}], "request_id"}`.
* Public text (README, landing, PRs): no secrets, internal hostnames, node names or IPs, and no claims that are not
  proven (no "production-ready", no conformance claims). The landing is Spanish first.
* GitHub-hosted CI on `ubuntu-24.04` with `timeout-minutes`; nothing in CI pushes images or deploys.
