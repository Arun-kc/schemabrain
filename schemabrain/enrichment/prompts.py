"""Versioned LLM prompt templates for column enrichment.

`PROMPT_VERSION` is part of `column_semantic_fingerprint` so any prompt
change invalidates the cache. The discipline is:

  Change the prompt → bump PROMPT_VERSION (in the same commit).

Tests in `tests/test_enrichment_prompts.py` lock the version string AND
the rendered template format so a change to one without the other fails
CI loudly.

The user prompt is rendered DETERMINISTICALLY from structured inputs.
Two calls with identical (table, column, stats, fk_targets) produce
byte-identical output — same invariant the fingerprint cache depends
on. Pre-redacted sample values from the profiler are passed through
verbatim; this module never re-applies PII redaction (and never has the
raw text to redact in the first place).
"""

from __future__ import annotations

from schemabrain.core.models import Column, Table
from schemabrain.pii.classifier import classify_column
from schemabrain.profiler.stats import ColumnStats

# Bump on ANY change that should re-enrich every cached column.
# Format: `YYYY-MM-DD-N` where N is the sequence within that day. The
# date-stamp lets a future debugger reconstruct "what prompt was in use
# when these cached fingerprints were stamped" without checking out an
# old commit. Tests treat this as a load-bearing constant.
PROMPT_VERSION = "2026-06-08-1"

SYSTEM_PROMPT = """\
You are a data engineer documenting columns in a relational database for an AI agent.

Given a column's metadata, type, sample values, and surrounding context, write a concise,
factual one-sentence description of what the column represents and how it's used.

Rules:
- Output one sentence, max 30 words, no period at the end.
- Describe the column's MEANING (what real-world thing it stores), not its TYPE.
- If the column appears to store PII, describe the field generically. Never repeat
  redacted sample values (`<EMAIL>`, `<SSN>`, `<CC>`) back in the description.
- If the column is a foreign key, mention what entity it joins to.
- If the column is in a junction (M:N association) table, additionally note that
  joining through this table multiplies result rows; downstream aggregates that
  sum or count across this association will double-count.
- Avoid hedging language ("probably", "might be"). State your best guess as fact.
- Output the description ONLY. No preamble, no explanation, no JSON wrapper.\
"""

# Caps the prompt's token cost; a typical wide table (8-15 columns) is
# adequately disambiguated by 8 sibling names without inflating the
# prompt for very wide tables (50+ columns).
_SIBLING_LIMIT = 8


def column_description_user_prompt(
    *,
    table: Table,
    column: Column,
    stats: ColumnStats | None,
    fk_targets: tuple[str, ...],
) -> str:
    """Render the user message asking the LLM to describe `column`.

    Order of fields is deliberate and stable: identity first, then
    schema constraints, then surrounding context (siblings, FKs), then
    profiler-derived signal. Stats lines are suppressed entirely when
    `stats` is `None` or when the relevant tuple is empty (avoids
    emitting empty `Sample values: ` lines that confuse the model).

    **TRUST BOUNDARY:** profiled-value-derived lines — both
    `stats.sample_values` and `stats.shape_patterns` — are emitted ONLY
    for columns the name-based PII classifier marks `public`. The
    profiler's value-level redaction covers email/SSN/CC, but names,
    addresses, phone numbers, and non-US national IDs slip past it, so any
    column `classify_column` flags as `pii` has BOTH lines dropped here,
    before the prompt is sent. (Shape signatures are masked but pass
    non-ASCII characters through verbatim, so they are withheld for PII
    columns too rather than relied on as content-free.) The classifier is
    name-driven, so the gate needs no profiled value to make its call.
    This closes the gap for columns whose NAME signals PII; a free-text
    column with a generic name (`bio`, `notes`) is a residual gap — the
    v1 classifier is name-based by design.
    """
    siblings = [c for c in table.columns if c.name != column.name][:_SIBLING_LIMIT]

    parts: list[str] = [
        f"Table: {table.qualified_name}",
        f"Column: {column.name}",
        f"Type: {column.data_type}",
        f"Nullable: {column.nullable}",
        f"Primary key: {column.is_primary_key}",
    ]
    if column.default is not None:
        parts.append(f"Default: {column.default}")
    if fk_targets:
        parts.append(f"Foreign key references: {', '.join(fk_targets)}")
    junction_targets = table.junction_target_tables()
    if junction_targets:
        parts.append(f"Junction table associating: {', '.join(junction_targets)}")
    if siblings:
        sibling_summary = ", ".join(f"{c.name} ({c.data_type})" for c in siblings)
        parts.append(f"Sibling columns: {sibling_summary}")
    if stats is not None:
        parts.append(f"Total rows: {stats.total_rows}")
        parts.append(f"Null %: {stats.null_pct:.1%}")
        parts.append(f"Distinct values: {stats.distinct_count}")
        # PII gate (see TRUST BOUNDARY above). `classify_column` decides
        # from the column NAME alone, so no profiled value is needed to
        # withhold one. Both profiled-value-derived lines — the raw samples
        # AND their shape signatures (which pass non-ASCII characters
        # through verbatim) — are emitted ONLY for columns the classifier
        # marks `public`. The `== "public"` allowlist (not `!= "pii"`)
        # means a future `internal`/`confidential` sensitivity withholds
        # rather than leaks.
        sensitivity, _categories = classify_column(
            column.name,
            column.data_type,
            is_primary_key=column.is_primary_key,
            table_name=table.name,
        )
        column_is_public = sensitivity == "public"
        if stats.sample_values and column_is_public:
            parts.append(f"Sample values: {' | '.join(stats.sample_values)}")
        if stats.shape_patterns and column_is_public:
            parts.append(f"Shape patterns: {', '.join(stats.shape_patterns)}")
    return "\n".join(parts)
