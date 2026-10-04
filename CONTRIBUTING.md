# Contributing

1. Read `AGENTS.md` (invariants and gates).
2. Branch from `main`, Conventional Commits, one concern per PR, open it as a draft.
3. Run the gates listed in `AGENTS.md`; paste the commands and counts in the PR body, and say which tests did not
   run (for example the `orcaslicer` marker) and why.
4. Profile changes: edit `scripts/profile-recipes.json` (new version, reason for every authored value), rebuild with
   `scripts/build_profiles.py` against the pinned OrcaSlicer release, and include the real-slicer test output.

By contributing you agree that your contribution is licensed under AGPL-3.0-only.
