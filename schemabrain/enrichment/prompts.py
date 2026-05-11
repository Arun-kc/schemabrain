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
from schemabrain.profiler.stats import ColumnStats

# Bump on ANY change that should re-enrich every cached column.
# Format: `YYYY-MM-DD-N` where N is the sequence within that day. The
# date-stamp lets a future debugger reconstruct "what prompt was in use
# when these cached fingerprints were stamped" without checking out an
# old commit. Tests treat this as a load-bearing constant.
PROMPT_VERSION = "2026-05-11-1"

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

    **TRUST BOUNDARY:** `stats.sample_values` and `stats.shape_patterns`
    are sent to the LLM verbatim. The contract is that the profiler
    (`schemabrain.profiler.postgres.PostgresProfiler`) has already run
    PII redaction (email/SSN/CC) before populating `ColumnStats`. If a
    custom profiler ever returns un-redacted PII in `sample_values`, it
    will reach Anthropic. Do not change the profiler contract without
    re-evaluating this assumption.
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
        if stats.sample_values:
            parts.append(f"Sample values: {' | '.join(stats.sample_values)}")
        if stats.shape_patterns:
            parts.append(f"Shape patterns: {', '.join(stats.shape_patterns)}")
    return "\n".join(parts)
