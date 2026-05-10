"""Stored representation of one LLM-generated column description.

Lives in `core` because both the enrichment pipeline (producer) and the
store (persister) need to depend on it. Keeping it out of
`core/models.py` so that file stays focused on database-introspection
models (Column, Table, ForeignKey).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnDescription:
    """One LLM-generated description for one column.

    Records both the text and the provenance (model, prompt version,
    token counts, cost) so a future debugger can reconstruct exactly
    what produced this description.
    """

    text: str
    model: str
    prompt_version: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: float
