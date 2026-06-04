"""TypeScript ↔ Python charter sync gate.

Asserts that every value in the Python-side Literal types has a
matching union member in the corresponding TypeScript file. A
contributor adding a PIICategory on the Python side without
mirroring it in `web/lib/types/pii.ts` fails CI here.

Cheap regex check rather than a full codegen pipeline — the v0.4
surface is small enough that the parity matrix fits in one file.
A future contributor extending the type surface bumps the
DASHBOARD_SCHEMA_VERSION constant + updates both sides + this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from schemabrain.audit.fingerprint import CostClass, RefusalReason
from schemabrain.core.entity import BindConfidence
from schemabrain.mcp.envelope import (
    Confidence,
    DegradationReason,
    ErrorKind,
    InferenceMethod,
    ProvenanceSource,
    Status,
    ValidationState,
)
from schemabrain.pii.categories import (
    CATASTROPHIC_LEAK_CATEGORIES,
    PII_CATEGORIES,
    PIICategory,
    PiiConfidenceBand,
    Sensitivity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TS_TYPES_DIR = REPO_ROOT / "web" / "lib" / "types"


def _literal_values(literal_type: object) -> set[str]:
    """Extract the closed string set from a `typing.Literal[...]`."""
    from typing import get_args

    return {str(v) for v in get_args(literal_type)}


def _ts_union_values(ts_file: Path, type_name: str) -> set[str]:
    """Pull the string union members off a `export type Name = "a" | "b";` line.

    Regex-only — no TS parser needed for the closed-grammar shapes
    we're checking. Matches across multi-line unions; leading `|`
    pipes are stripped.
    """
    text = ts_file.read_text()
    match = re.search(
        rf"export\s+type\s+{re.escape(type_name)}\s*=([^;]+);",
        text,
        re.MULTILINE,
    )
    if not match:
        raise AssertionError(f"type {type_name!r} not declared in {ts_file.relative_to(REPO_ROOT)}")
    body = match.group(1)
    values = re.findall(r'"([^"]+)"', body)
    return set(values)


def _ts_array_values(ts_file: Path, const_name: str) -> set[str]:
    """Pull string members out of a readonly array const."""
    text = ts_file.read_text()
    match = re.search(
        rf"export\s+const\s+{re.escape(const_name)}[^=]*=\s*\[([^\]]+)\]",
        text,
        re.MULTILINE,
    )
    if not match:
        raise AssertionError(
            f"const {const_name!r} not declared in {ts_file.relative_to(REPO_ROOT)}"
        )
    return set(re.findall(r'"([^"]+)"', match.group(1)))


@pytest.mark.parametrize(
    ("python_literal", "ts_file_name", "ts_type"),
    [
        (Status, "envelope.ts", "Status"),
        (Confidence, "envelope.ts", "Confidence"),
        (InferenceMethod, "envelope.ts", "InferenceMethod"),
        (ValidationState, "envelope.ts", "ValidationState"),
        (BindConfidence, "meta.ts", "BindConfidence"),
        (ProvenanceSource, "envelope.ts", "ProvenanceSource"),
        (ErrorKind, "envelope.ts", "ErrorKind"),
        (DegradationReason, "envelope.ts", "DegradationReason"),
        (PIICategory, "pii.ts", "PIICategory"),
        (Sensitivity, "pii.ts", "Sensitivity"),
        (PiiConfidenceBand, "pii.ts", "PiiConfidenceBand"),
        (CostClass, "audit.ts", "CostClass"),
        (RefusalReason, "audit.ts", "RefusalReason"),
    ],
)
def test_python_literal_matches_ts_union(
    python_literal: object, ts_file_name: str, ts_type: str
) -> None:
    python_values = _literal_values(python_literal)
    ts_values = _ts_union_values(TS_TYPES_DIR / ts_file_name, ts_type)
    assert python_values == ts_values, (
        f"drift detected for {ts_type}:\n"
        f"  only in Python: {sorted(python_values - ts_values)}\n"
        f"  only in TS:     {sorted(ts_values - python_values)}"
    )


def test_pii_categories_array_matches_python() -> None:
    ts_values = _ts_array_values(TS_TYPES_DIR / "pii.ts", "PII_CATEGORIES")
    assert ts_values == set(PII_CATEGORIES)


def test_catastrophic_leak_categories_array_matches_python() -> None:
    ts_values = _ts_array_values(TS_TYPES_DIR / "pii.ts", "CATASTROPHIC_LEAK_CATEGORIES")
    assert ts_values == set(CATASTROPHIC_LEAK_CATEGORIES)


def test_ts_types_directory_layout() -> None:
    """The four mirrored files + barrel must exist."""
    for name in ("envelope.ts", "pii.ts", "audit.ts", "meta.ts", "index.ts"):
        assert (TS_TYPES_DIR / name).exists(), f"missing {name}"
