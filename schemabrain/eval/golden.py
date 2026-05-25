"""Golden-set loader for the SchemaBrain eval harness.

A golden set is a JSON file with a list of natural-language questions,
each paired with the qualified table name(s) a correct retriever should
return. The runner scores top-K recall against this expected answer set.

The harness is **schema-agnostic**: any user-authored golden set against
any indexed database works the same way. The package bundles one starter
golden set in `golden_sets/ecommerce.json` (paired with the synthetic
fixture in `fixtures/ecommerce.sql`) so the CLI works out of the box,
but production usage is "author your own `golden_sets/<your-schema>.json`
for your real schema, then `schemabrain eval --golden ./that-file.json`."

Authoring convention: every `expected_tables` entry must be a qualified
name in `schema.table` form (e.g., `"public.users"`). Unqualified names
silently miss against the retriever (which keys on `(schema, name)`)
and create false negatives in the score, so we reject them at load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# Default starter golden set bundled with the package. E-commerce is the
# example domain only because its tables (users/orders/products/order_items)
# are universally legible across industries — it is NOT what SchemaBrain
# is "for". The harness is generic; bring your own golden file via
# `schemabrain eval --golden /path/to/your-schema.json`. Future bundled
# sets (HR, analytics, etc.) drop into the same `golden_sets/` directory
# without restructuring.
DEFAULT_GOLDEN_PATH = Path(__file__).parent / "golden_sets" / "ecommerce.json"


class GoldenQuestion(BaseModel):
    """One natural-language question with its expected qualified tables."""

    model_config = ConfigDict(frozen=True)

    id: NonEmptyStr
    question: NonEmptyStr
    expected_tables: tuple[NonEmptyStr, ...] = Field(min_length=1)
    notes: str = ""

    @field_validator("expected_tables")
    @classmethod
    def _validate_qualified_and_unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for t in v:
            if "." not in t:
                raise ValueError(f"expected_table {t!r} must be qualified (schema.table)")
        if len(set(v)) != len(v):
            raise ValueError(f"duplicate expected_tables entries: {v}")
        return v


class GoldenSet(BaseModel):
    """A bundle of golden questions tied to a specific schema."""

    model_config = ConfigDict(frozen=True)

    version: NonEmptyStr
    schema_description: NonEmptyStr
    questions: tuple[GoldenQuestion, ...] = Field(min_length=1)


def load_golden(path: Path | str) -> GoldenSet:
    """Read + validate a golden set from disk.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file is not valid JSON, or if validation
            (Pydantic constraints + uniqueness of question IDs) fails.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"failed to parse {p} as JSON: {e}") from e

    gs = GoldenSet.model_validate(raw)

    ids = [q.id for q in gs.questions]
    if len(set(ids)) != len(ids):
        # Duplicate IDs would silently merge in any per-question report
        # downstream; reject at load so the authoring slip surfaces immediately.
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate question IDs in golden set: {dupes}")

    return gs


__all__ = [
    "DEFAULT_GOLDEN_PATH",
    "GoldenQuestion",
    "GoldenSet",
    "load_golden",
]
