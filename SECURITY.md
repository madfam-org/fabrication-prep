# Security

Report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) on
`madfam-org/fabrication-prep`. Please do not open a public issue for an unfixed vulnerability.

Scope notes:

* Every `/v1` route requires a Janua RS256 service token (audience `fabrication-prep-api`, scope
  `fabrication-prep:slice`); artifact downloads require a short-lived signed URL.
* The worker fetches inputs only from allow-listed hosts, without following redirects, with size and time limits,
  and verifies sha256 digests.
* OrcaSlicer runs as a non-root user with a read-only root filesystem, a minimal environment and a time limit.
