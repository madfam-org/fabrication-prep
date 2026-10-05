# fabrication-prep: status as of 2026-10-05

A dated snapshot for someone resuming from a fresh clone. **The
[open-PR list](https://github.com/madfam-org/fabrication-prep/pulls) is
authoritative**; when this file and GitHub disagree, GitHub wins. Operator
runbooks are kept privately; the public operator steps are
[`operator-setup.md`](operator-setup.md).

## Where it stands

- `main` is `a0ab24e3`: the v1 slicing service.
- **Not deployed.** `main` has no build-and-deploy workflow; #2 adds one that
  runs only on manual dispatch.
- There has been no physical print yet. The slicer outputs are proven headless
  in CI (the Linux `selftest-slice` proof for both targets), not on a printer.

## Landed recently

| PR | What it did |
|---|---|
| [#1](https://github.com/madfam-org/fabrication-prep/pull/1) | v1: the OrcaSlicer 2.4.2 CLI behind a `/v1` API, a Postgres `SKIP LOCKED` job queue, versioned printer, filament and process profiles, signed artifact URLs, Klipper G-code and Bambu `.gcode.3mf` targets |

## Open PRs, in merge order

| PR | Purpose | Precondition | Deploys |
|---|---|---|---|
| [#2](https://github.com/madfam-org/fabrication-prep/pull/2) | Dispatch-only build-and-deploy workflow; 30-day artifact retention (migration `0002`, a sweep, `410 Gone` for expired artifacts); drops the unused Postgres addon from `enclii.yaml`; onboarding with generated secrets | CI green | No (the workflow runs only when dispatched) |

## Next steps

1. Merge #2.
2. Operator onboarding with generated secrets (database, runtime role, keys and
   the artifact bucket), then the first dispatched deploy and a smoke test.
   Nobody types or stores a credential; see [`operator-setup.md`](operator-setup.md).
3. pravara-mes' dispatcher becomes the first caller of `POST /v1/slice-jobs`
   (pravara-mes#55, off by default there).
4. The first physical print, from the programme's MVP: one order, one real
   printer, G-code, and a passport event on the part's twin.

Open questions that only a printer answers:
- whether a Bambu A1 accepts the 2.4.2 `.gcode.3mf`, whose `printer_model_id` is
  empty because the release lacks the file it reads it from;
- the two TPU-safe processes are our own authoring and have not been validated
  on a printer.

## Cross-repo contracts

| Contract | Defined in | What this service relies on |
|---|---|---|
| The render bundle it slices (GOC-1 `variables.json`) | yantra4d [`docs/reference/generator-output.md`](https://github.com/madfam-org/yantra4d/blob/main/docs/reference/generator-output.md) | the input files and their parameter record |
| The caller: fabrication dispatch | pravara-mes [`ROADMAP.md`](https://github.com/madfam-org/pravara-mes/blob/main/ROADMAP.md) (*Pending work and roadmap ahead*) and pravara-mes#55 | the dispatcher posts slice jobs and hands the signed G-code URL to the edge node |
| Edge delivery to the printer | pravara-mes [`packages/sparkplug/README.md`](https://github.com/madfam-org/pravara-mes/blob/main/packages/sparkplug/README.md) | the edge node downloads the signed URL and checks its sha256; this service never contacts a printer |
| The part's twin and passport | asset-shells [`README.md`](https://github.com/madfam-org/asset-shells/blob/main/README.md) (*Publish API*) | slicer estimates reach the passport through pravara-mes, not directly |
