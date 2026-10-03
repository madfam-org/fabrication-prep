# fabrication-prep

> Boundary: this repository is MADFAM's public fabrication-prep service (slicing and printer, filament and
> process profiles). It holds no secrets, hostnames of internal nodes, or private runbooks. Identity is Janua,
> orchestration is pravara-mes; this service never moves a machine.

fabrication-prep turns a generator render bundle (GOC-1: geometry plus `variables.json`) into printer-ready
output — plain G-code for Klipper printers, a sliced `.gcode.3mf` for Bambu printers — using the OrcaSlicer
command line, with versioned, digest-pinned printer, filament and process profiles.

Licence: AGPL-3.0-only (see `LICENSE`).
