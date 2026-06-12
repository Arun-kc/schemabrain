"""Integration proof of the PII sample gate against a real Postgres.

The unit tests in `test_enrichment_prompts.py` exercise the gate with
hand-built `ColumnStats`. This test closes the loop on the real path:
profile a seeded table with genuine PII (emails, names, phones), build the
actual enrichment prompt per column, and assert that no profiled value
from a PII column reaches the prompt while a public column's samples do.
"""

from __future__ import annotations

import pytest

from schemabrain.core.models import Column, Table
from schemabrain.enrichment.prompts import column_description_user_prompt
from schemabrain.profiler.postgres import PostgresProfiler

pytestmark = pytest.mark.integration


def _users_profile() -> Table:
    specs = [
        ("id", "BIGINT", True),
        ("email", "TEXT", False),
        ("middle_name", "TEXT", False),
        ("phone", "TEXT", False),
        ("bio", "TEXT", False),
    ]
    return Table(
        name="users_profile",
        schema_name="profiling",
        columns=tuple(
            Column(
                name=name,
                table_name="users_profile",
                schema_name="profiling",
                data_type=dtype,
                nullable=True,
                ordinal_position=i + 1,
                is_primary_key=pk,
            )
            for i, (name, dtype, pk) in enumerate(specs)
        ),
    )


def test_pii_columns_withheld_public_columns_kept(profiling_pg_url: str) -> None:
    table = _users_profile()
    with PostgresProfiler(profiling_pg_url) as profiler:
        stats = profiler.profile_table(table)
    by_name = {c.name: c for c in table.columns}

    # email / middle_name / phone classify as contact PII: neither the
    # sample-value line nor the shape line is emitted, and no actual
    # profiled value appears anywhere in the rendered prompt.
    for name in ("email", "middle_name", "phone"):
        rendered = column_description_user_prompt(
            table=table, column=by_name[name], stats=stats[name], fk_targets=()
        )
        assert "Sample values:" not in rendered
        assert "Shape patterns:" not in rendered
        for sample in stats[name].sample_values:
            assert sample not in rendered

    # `id` matches no PII rule -> public -> its samples still flow.
    rendered_id = column_description_user_prompt(
        table=table, column=by_name["id"], stats=stats["id"], fk_targets=()
    )
    assert "Sample values:" in rendered_id
