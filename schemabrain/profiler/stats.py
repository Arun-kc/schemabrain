"""Pure profiling primitives.

`ColumnStats` is the immutable per-column profile that profilers produce and
the store will persist. The PII redaction and shape-detection helpers are
intentionally pure functions so they are trivially testable and reusable
outside the Postgres path (e.g. a future SQLite or Snowflake profiler).

PII redaction runs *before* sample values reach the cache. Customer emails,
SSNs, and credit-card numbers must never be persisted or sent to an LLM.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

SAMPLE_TRUNCATE_AT = 50  # public — also imported by the Postgres profiler.

# All three patterns are ASCII-only on purpose. Python's default `\w` and `\d`
# match Unicode letters/digits, which would let crafted Unicode strings (e.g.
# Arabic-Indic numerals) trigger or evade redaction unpredictably. We accept
# the trade-off of not redacting full-width digits in customer data — those
# would surface in samples as raw text, which is honest about what we did.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+", re.ASCII)
# SSN and CC use digit-only lookarounds rather than `\b`. `\b` is a word
# boundary, so a CC embedded in `id4111111111111111tail` has *no* boundary
# at the digit transition (both sides are word chars) and would not be
# redacted. Digit lookarounds catch the run regardless of neighbouring
# letters/punctuation while still preventing matches inside a longer digit
# sequence (which would otherwise produce partial false positives).
_SSN_RE = re.compile(r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])", re.ASCII)
# Credit-card-like runs: 13-19 digits, optionally separated by single spaces
# or dashes. The 13-digit lower bound matches Visa's minimum length, which
# means we deliberately over-redact: any 13-19 digit run (long order IDs,
# EAN-13 barcodes, all-numeric UUIDs with dashes) shows up as `<CC>` in
# samples. Test coverage locks this in as an explicit choice — over-redaction
# is acceptable for a profiler whose purpose is to keep customer data out of
# the LLM context. Under-redaction is not.
_CC_RE = re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])", re.ASCII)


class ColumnStats(BaseModel):
    """Per-column profile produced by a profiler.

    Invariants enforced on construction:
    - All counts are non-negative.
    - `null_count` cannot exceed `total_rows`.
    - `distinct_count` cannot exceed the number of non-null rows
      (`total_rows - null_count`).
    """

    model_config = ConfigDict(frozen=True)

    column_name: NonEmptyStr
    total_rows: int = Field(..., ge=0)
    null_count: int = Field(..., ge=0)
    distinct_count: int = Field(..., ge=0)
    sample_values: tuple[str, ...] = ()
    shape_patterns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_counts(self) -> ColumnStats:
        if self.null_count > self.total_rows:
            raise ValueError(
                f"null_count ({self.null_count}) cannot exceed total_rows ({self.total_rows})"
            )
        non_null = self.total_rows - self.null_count
        if self.distinct_count > non_null:
            raise ValueError(
                f"distinct_count ({self.distinct_count}) cannot exceed non-null rows ({non_null})"
            )
        return self

    @property
    def null_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return self.null_count / self.total_rows


def redact_pii(value: str) -> str:
    """Replace email, SSN, and credit-card-like substrings with sentinels.

    The replacement preserves position so a downstream shape detector still
    sees that *something* of that approximate length lived there, but the
    actual PII content never leaves this function.
    """
    value = _EMAIL_RE.sub("<EMAIL>", value)
    value = _SSN_RE.sub("<SSN>", value)
    value = _CC_RE.sub("<CC>", value)
    return value


def shape_of(value: str) -> str:
    """Return a structural signature of `value`.

    Digits become `9`, lowercase ASCII letters `a`, uppercase ASCII letters
    `A`. All other characters are kept literally so separators (`-`, `@`,
    `.`) survive into the signature. Values longer than 50 chars are
    truncated first to bound the signature size.
    """
    value = value[:SAMPLE_TRUNCATE_AT]
    chars: list[str] = []
    for c in value:
        # ASCII-only checks throughout — `str.isdigit()` would map
        # Arabic-Indic and other Unicode numerals to `9`, which is
        # inconsistent with the ASCII-only PII regex and would give a
        # misleading "all numeric" shape for non-ASCII data.
        if "0" <= c <= "9":
            chars.append("9")
        elif "a" <= c <= "z":
            chars.append("a")
        elif "A" <= c <= "Z":
            chars.append("A")
        else:
            chars.append(c)
    return "".join(chars)


def top_shapes(values: Iterable[str], limit: int = 3) -> tuple[str, ...]:
    """Return up to `limit` most-common shape signatures across `values`.

    Ties on count are broken lexicographically so the result is fully
    deterministic regardless of the input iteration order or the underlying
    `Counter.most_common` tie-handling (CPython preserves insertion order
    for ties, but that is not part of the language spec).
    """
    counter: Counter[str] = Counter(shape_of(v) for v in values)
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(shape for shape, _ in ordered[:limit])
