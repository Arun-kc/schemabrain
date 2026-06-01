"""MCP tool implementation: describe_entity.

Surfaces the full shape of one confirmed entity — its YAML-grammar
fields (name, description, binding, identity, origin) plus the
columns of the bound table with their LLM-enriched descriptions and
column-granular PII classifications.

Charter v1.2 column-granular firewall: each column carries its real
`pii_sensitivity` from the `column_pii_tags` store layer plus the
specific `pii_categories` it was classified as. Columns whose
categories intersect the server's `--pii-block` set are marked
`redacted=True` with the LLM-enriched semantic description cleared.
The agent still sees the column exists (name, data_type, nullable)
but cannot read the model-generated semantics — moving refusal from
"whole entity blocked" to "specific columns redacted".

Catastrophic-leak floor: columns tagged with `credential`,
`payment_card`, or `government_id` are redacted unconditionally
regardless of the operator's `--pii-block` policy. The intent here
is minimum decency — even an operator who explicitly disabled PII
enforcement (`--pii-block ''`) should not let the agent read
"password hash" or "social security number" as part of the column
description string. The agent still sees the column exists.

The bound-table lookup relies on the store-side FK invariant: an
entity can only reference an indexed table, so a non-None entity
implies a non-None bound table. Defensive None-handling would be
unreachable code under the v8 schema contract.
"""

from __future__ import annotations

from schemabrain.core.store_protocol import Store
from schemabrain.mcp._helpers import _validate_ident, _with_token_estimate
from schemabrain.mcp.shapes import (
    EntityColumn,
    EntityDetail,
    EntityNotFoundError,
)
from schemabrain.pii import CATASTROPHIC_LEAK_CATEGORIES, PIICategory


def describe_entity_impl(
    *,
    store: Store,
    source_connection_id: str,
    name: str,
    pii_block: frozenset[PIICategory] = frozenset(),
) -> EntityDetail:
    """Return the full `EntityDetail` for `name`.

    Raises `ValueError` for a malformed name (empty, too long, non-
    identifier shape) and `EntityNotFoundError` when no entity by
    that name exists for `source_connection_id`. The two error
    surfaces are distinct so the MCP wrapper can route them to
    `malformed_name` vs `unknown_name` envelopes respectively.

    Every column of the bound table is exposed (no YAML allowlist
    at this release). Per-column descriptions come from the same
    store layer that backs `describe_table`, so an entity inherits
    whatever enrichment its bound table has received.

    `pii_block` is the server-policy frozenset of categories the
    operator forbade. Each column's `pii_categories` is checked for
    intersection — when non-empty, the column is marked redacted and
    its description is cleared so the agent cannot read the model-
    generated semantics for a policy-blocked column.
    """
    _validate_ident(name, role="entity")

    entity = store.get_entity(name, source_connection_id=source_connection_id)
    if entity is None:
        raise EntityNotFoundError(
            f"No entity named {name!r} for this source. Call `list_entities` to see what's defined."
        )

    # Bound-table lookup. The v8 schema's FK constraint guarantees
    # this is non-None when the entity is non-None — but we raise
    # a real exception (not an `assert`, which `python -O` strips)
    # so a corrupt store still surfaces a diagnostic message through
    # the server's `_wrap_internal_error` catch-all rather than a
    # bare AttributeError on the next line.
    schema, table_name = entity.qualified_table.split(".", 1)
    table = store.get_table(schema, table_name, source_connection_id=source_connection_id)
    if table is None:
        raise RuntimeError(
            f"FK invariant violated: entity {name!r} references "
            f"missing table {entity.qualified_table!r}"
        )

    descriptions = store.get_table_descriptions(
        schema_name=schema,
        name=table_name,
        source_connection_id=source_connection_id,
    )

    # Column-granular PII: bulk fetch tags for every column on the
    # bound table. The store returns one row per (qualified_table,
    # column_name) tuple that was classified; columns with no row
    # default to `("public", frozenset())` per the propagation
    # helper's empty-input contract.
    pii_tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=entity.qualified_table,
        columns=[col.name for col in table.columns],
    )

    columns: list[EntityColumn] = []
    redacted_names: list[str] = []
    # Effective redaction floor: the operator's `--pii-block` set
    # union the catastrophic-leak categories that are always redacted
    # even when the operator opted out of enforcement. This is the
    # minimum-decency rule — an `--pii-block ''` operator still
    # should not let the agent read the description of a `password`
    # or `ssn` column. See module docstring.
    effective_block: frozenset[PIICategory] = pii_block | CATASTROPHIC_LEAK_CATEGORIES
    # per-category counter for deterministic placeholder
    # naming (``<redacted_<category>_column_N>``) — matches the scheme
    # used by ``describe_table`` so an agent that calls both surfaces
    # sees consistent placeholder numbering per entity.
    category_counters: dict[str, int] = {}
    for col in table.columns:
        sensitivity, categories = pii_tags.get(col.name, ("public", frozenset()))
        sorted_categories = tuple(sorted(categories))
        # A column is redacted when ANY of its tagged PII categories
        # appears in the effective block set (operator policy OR the
        # catastrophic-leak floor). Even one matching category
        # warrants redaction because the operator's intent is "do not
        # expose this kind of data to the agent regardless of column".
        blocked = categories & effective_block
        is_redacted = bool(blocked)
        if is_redacted:
            # hide the real name behind a placeholder. The
            # end-to-end smoke run caught Claude reading
            # ``card_number_last4`` from describe_entity (with
            # ``redacted=True`` but the real name preserved) and
            # emitting raw SQL referencing it. Replacing the name
            # closes the loophole — the agent learns "a payment_card
            # column slot exists" without learning what it's called.
            cat = sorted(blocked)[0]
            category_counters[cat] = category_counters.get(cat, 0) + 1
            display_name = f"<redacted_{cat}_column_{category_counters[cat]}>"
            # Record the PLACEHOLDER, never the real name — the
            # ``redacted_columns`` summary field below is agent-facing,
            # and listing the real name re-discloses exactly what the
            # placeholder just hid. See the firewall name-disclosure
            # regression in tests/test_saas_firewall_name_disclosure.py.
            redacted_names.append(display_name)
        else:
            display_name = col.name
        description = (
            "" if is_redacted else (descriptions[col.name].text if col.name in descriptions else "")
        )
        # When a column is redacted, scrub ``pii_categories`` too. The
        # placeholder name already hides the column's identity; shipping
        # the real category tuple alongside (`('credential',)`) would
        # be a probe oracle — the agent learns "the redacted column at
        # ordinal N is a credential field of type T" and can deduce the
        # real name from data_type + ordinal position. Treat the
        # category field as part of the redacted shape.
        columns.append(
            EntityColumn(
                name=display_name,
                data_type=col.data_type,
                nullable=col.nullable,
                description=description,
                pii_sensitivity=sensitivity,
                pii_categories=() if is_redacted else sorted_categories,
                redacted=is_redacted,
            )
        )

    partial = EntityDetail(
        name=entity.name,
        description=entity.description,
        qualified_table=entity.qualified_table,
        identity=entity.identity,
        origin=entity.origin,
        columns=columns,
        token_estimate=0,  # placeholder; `_with_token_estimate` rebuilds
        inference_method=entity.inference_method,
        validation_state=entity.validation_state,
        redacted_columns=tuple(redacted_names),
    )
    return _with_token_estimate(partial)
