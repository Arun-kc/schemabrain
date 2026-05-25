"""Setup-time machinery for `schemabrain init` and `schemabrain doctor`.

This package owns the activation gate — the surface a stranger first
touches when wiring SchemaBrain to an MCP host. Three modules will
land here as the gate matures:

  - `checks`       — `Check` DTO + sequential runner used by `doctor`.
  - `hosts`        — per-host config-path detection + snippet generation.
  - `config_io`    — atomic read-merge-write of host configs with backup.
  - `init_flow`    — interactive + non-interactive `init` orchestration.
  - `doctor_flow`  — `doctor` orchestration + JSON output mode.

Each module is pure where possible — IO concentrates in `config_io`
and the per-host snippet-write paths, so the rest stay easy to test
against constructed fixtures.
"""

from __future__ import annotations
