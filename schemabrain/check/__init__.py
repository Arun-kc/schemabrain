"""`schemabrain check` — drift detection over the semantic-layer definitions.

This package compares the entities, metrics, and canonical joins
persisted in the local SQLite store against the live source database
and reports each mismatch as a `Drift` record.

The package is split into two modules:

  - `engine` — pure logic: traversal + drift classification. No I/O
    beyond the supplied `DataSource` and `Store`. Returns a frozen
    `CheckReport`.
  - `render` — rich-rendered + JSON renderers consumed by the CLI.
    Render code is kept out of `engine` so the engine remains
    testable without `rich` and so future surfaces (e.g. a hosted
    control-plane report) can render the same report shape
    differently.
"""

from __future__ import annotations

from schemabrain.check.engine import (
    CheckReport,
    DefKind,
    Drift,
    DriftKind,
    check_drift,
)
from schemabrain.check.render import render_json, render_report

__all__ = [
    "CheckReport",
    "DefKind",
    "Drift",
    "DriftKind",
    "check_drift",
    "render_json",
    "render_report",
]
