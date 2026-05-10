"""LLM enrichment pipeline.

Generates per-column semantic descriptions using Anthropic Claude. The
prompt templates live in `prompts.py` with an explicit `PROMPT_VERSION`
that is part of the column semantic fingerprint — bumping the version
invalidates every cached description, so the rule is "any prompt change
must bump the version."
"""

from schemabrain.enrichment.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    column_description_user_prompt,
)

__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT", "column_description_user_prompt"]
