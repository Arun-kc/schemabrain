"""Tests for the versioned LLM prompt templates.

PROMPT_VERSION is part of the column semantic fingerprint, so any
prompt change that doesn't bump the version would silently keep stale
cached descriptions. The golden tests in this file lock in BOTH the
version string and the rendered prompt text — change one without the
other and CI fails.
"""

from __future__ import annotations

from schemabrain.core.models import Column, Table
from schemabrain.enrichment.prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    column_description_user_prompt,
)
from schemabrain.profiler.stats import ColumnStats


def _column(
    name: str,
    *,
    table_name: str = "users",
    schema_name: str = "public",
    data_type: str = "TEXT",
    nullable: bool = True,
    ordinal_position: int = 1,
    default: str | None = None,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
        default=default,
        is_primary_key=is_primary_key,
    )


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            _column(
                "id",
                data_type="BIGINT",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "email",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
            ),
            _column(
                "created_at",
                data_type="TIMESTAMP",
                nullable=False,
                ordinal_position=3,
                default="now()",
            ),
        ),
    )


class TestPromptVersion:
    def test_is_non_empty_string(self) -> None:
        assert isinstance(PROMPT_VERSION, str)
        assert PROMPT_VERSION

    def test_locked_value(self) -> None:
        # Changing this string is a deliberate cache-invalidation event.
        # Bumping it must be paired with a prompt change (or vice versa).
        assert PROMPT_VERSION == "2026-05-10-2"

    def test_format_is_date_stamped(self) -> None:
        # Format: `YYYY-MM-DD-N`. Locks the convention so future bumps
        # follow the same date-stamped scheme.
        import re

        assert re.match(r"^\d{4}-\d{2}-\d{2}-\d+$", PROMPT_VERSION), (
            f"PROMPT_VERSION {PROMPT_VERSION!r} should match YYYY-MM-DD-N"
        )


class TestSystemPrompt:
    def test_mentions_one_sentence_rule(self) -> None:
        assert "one sentence" in SYSTEM_PROMPT.lower() or "one-line" in SYSTEM_PROMPT.lower()

    def test_mentions_pii_rule(self) -> None:
        assert "pii" in SYSTEM_PROMPT.lower()

    def test_no_trailing_whitespace_per_line(self) -> None:
        # Token-cost discipline: trailing spaces are pure waste in the
        # cached prompt prefix.
        for line in SYSTEM_PROMPT.splitlines():
            assert line == line.rstrip(), f"trailing whitespace on: {line!r}"


class TestColumnDescriptionUserPrompt:
    def test_includes_column_and_table_identity(self) -> None:
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("email", data_type="TEXT", nullable=False, ordinal_position=2),
            stats=None,
            fk_targets=(),
        )
        assert "email" in rendered
        assert "TEXT" in rendered
        assert "public.users" in rendered

    def test_omits_default_when_none(self) -> None:
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("email", default=None),
            stats=None,
            fk_targets=(),
        )
        assert "Default:" not in rendered

    def test_includes_default_when_present(self) -> None:
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("created_at", default="now()"),
            stats=None,
            fk_targets=(),
        )
        assert "now()" in rendered

    def test_includes_fk_targets(self) -> None:
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("user_id"),
            stats=None,
            fk_targets=("public.users.id",),
        )
        assert "public.users.id" in rendered

    def test_omits_fk_section_when_no_targets(self) -> None:
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("name"),
            stats=None,
            fk_targets=(),
        )
        assert "Foreign key" not in rendered

    def test_includes_sibling_columns(self) -> None:
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("email", table_name="users", data_type="TEXT", ordinal_position=2),
            stats=None,
            fk_targets=(),
        )
        # Sibling columns should appear with their types so the LLM can
        # disambiguate `email` from `email_verified` etc.
        assert "id" in rendered
        assert "created_at" in rendered

    def test_omits_sibling_section_when_no_siblings(self) -> None:
        # Single-column table — there are no siblings to list, and
        # emitting `Sibling columns: ` with an empty value would be noise.
        single_col = Column(
            name="id",
            table_name="solo",
            schema_name="public",
            data_type="BIGINT",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        )
        solo_table = Table(name="solo", schema_name="public", columns=(single_col,))
        rendered = column_description_user_prompt(
            table=solo_table,
            column=single_col,
            stats=None,
            fk_targets=(),
        )
        assert "Sibling columns" not in rendered

    def test_excludes_self_from_sibling_list(self) -> None:
        # Render the prompt for `email`. The Sibling line must not list
        # `email` itself — that's noise.
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("email", table_name="users", data_type="TEXT", ordinal_position=2),
            stats=None,
            fk_targets=(),
        )
        sibling_line = next(line for line in rendered.splitlines() if line.startswith("Sibling"))
        # The sibling line should mention `id` and `created_at` but not start
        # the email entry mid-line.
        assert "email (" not in sibling_line

    def test_includes_stats_when_provided(self) -> None:
        stats = ColumnStats(
            column_name="email",
            total_rows=100,
            null_count=10,
            distinct_count=85,
            sample_values=("<EMAIL>",),
            shape_patterns=("aaa@aaaa.aa",),
        )
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("email"),
            stats=stats,
            fk_targets=(),
        )
        assert "100" in rendered
        assert "85" in rendered
        assert "<EMAIL>" in rendered
        assert "aaa@aaaa.aa" in rendered

    def test_omits_sample_lines_when_no_samples(self) -> None:
        # A column profiled but with zero non-null rows produces empty
        # sample tuples. Don't emit "Sample values:" with nothing after it.
        stats = ColumnStats(
            column_name="middle_name",
            total_rows=10,
            null_count=10,
            distinct_count=0,
            sample_values=(),
            shape_patterns=(),
        )
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("middle_name"),
            stats=stats,
            fk_targets=(),
        )
        assert "Sample values" not in rendered
        assert "Shape patterns" not in rendered

    def test_does_not_re_redact_already_redacted_samples(self) -> None:
        # Profiler already redacts; this function must trust the input
        # and pass redacted samples through verbatim.
        stats = ColumnStats(
            column_name="ssn",
            total_rows=5,
            null_count=0,
            distinct_count=5,
            sample_values=("<SSN>", "<SSN>"),
        )
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=_column("ssn"),
            stats=stats,
            fk_targets=(),
        )
        assert "<SSN>" in rendered

    def test_is_deterministic(self) -> None:
        # Two calls with structurally identical inputs must produce
        # identical output. Same invariant the cache fingerprint depends on.
        args = dict(
            table=_users_table(),
            column=_column("email", data_type="TEXT", ordinal_position=2),
            stats=ColumnStats(
                column_name="email",
                total_rows=100,
                null_count=10,
                distinct_count=85,
                sample_values=("<EMAIL>",),
                shape_patterns=("aaa@aaaa.aa",),
            ),
            fk_targets=("public.contacts.email",),
        )
        first = column_description_user_prompt(**args)
        second = column_description_user_prompt(**args)
        assert first == second

    def test_golden_locks_full_format(self) -> None:
        # Locks the exact rendered prompt for a known input. If the
        # template changes, this fails — and PROMPT_VERSION must be
        # bumped in the same change.
        rendered = column_description_user_prompt(
            table=_users_table(),
            column=Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="TEXT",
                nullable=False,
                ordinal_position=2,
                default=None,
                is_primary_key=False,
            ),
            stats=ColumnStats(
                column_name="email",
                total_rows=100,
                null_count=10,
                distinct_count=85,
                sample_values=("<EMAIL>",),
                shape_patterns=("aaa@aaaa.aa",),
            ),
            fk_targets=("public.contacts.email",),
        )
        expected = (
            "Table: public.users\n"
            "Column: email\n"
            "Type: TEXT\n"
            "Nullable: False\n"
            "Primary key: False\n"
            "Foreign key references: public.contacts.email\n"
            "Sibling columns: id (BIGINT), created_at (TIMESTAMP)\n"
            "Total rows: 100\n"
            "Null %: 10.0%\n"
            "Distinct values: 85\n"
            "Sample values: <EMAIL>\n"
            "Shape patterns: aaa@aaaa.aa"
        )
        assert rendered == expected
