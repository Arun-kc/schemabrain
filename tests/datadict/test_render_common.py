"""Pure-renderer contract: ON-clause grammar, PII labels, catastrophic flag.

`render_common` is the sole owner of the data-dictionary's display
contract (the canonical `PIICategory`/`Sensitivity` -> label maps and
the `render_on_clause` SQL grammar). These tests pin that contract so a
silent edit can't drift the byte-exact golden (`test_dictionary_golden`).
"""

from __future__ import annotations

from schemabrain.datadict.render_common import (
    CATEGORY_LABELS,
    SENSITIVITY_LABELS,
    category_label,
    is_catastrophic_category,
    render_on_clause,
    sensitivity_label,
)
from schemabrain.pii import (
    CATASTROPHIC_LEAK_CATEGORIES,
    PII_CATEGORIES,
    SENSITIVITIES,
)

# ----- label maps: total coverage of the closed enums ------------------


def test_category_labels_cover_every_pii_category() -> None:
    # A new PIICategory must get a human label here, or the dictionary
    # would render the raw enum token. Pin total coverage.
    assert set(CATEGORY_LABELS) == PII_CATEGORIES


def test_sensitivity_labels_cover_every_sensitivity() -> None:
    assert set(SENSITIVITY_LABELS) == set(SENSITIVITIES)


def test_pii_sensitivity_label_is_uppercase_acronym() -> None:
    # "pii" -> "PII", not the title-cased "Pii".
    assert SENSITIVITY_LABELS["pii"] == "PII"
    assert sensitivity_label("pii") == "PII"


def test_category_label_falls_back_to_raw_token_on_unknown() -> None:
    # Defensive: an unmapped value renders verbatim rather than raising,
    # so a taxonomy addition degrades gracefully instead of crashing the
    # CLI before the label map is updated.
    assert category_label("contact") == "Contact"
    assert category_label("not_a_real_category") == "not_a_real_category"  # type: ignore[arg-type]


# ----- catastrophic membership ----------------------------------------


def test_is_catastrophic_category_true_for_each_catastrophic_leg() -> None:
    for cat in CATASTROPHIC_LEAK_CATEGORIES:
        assert is_catastrophic_category(cat) is True


def test_is_catastrophic_category_false_for_benign_categories() -> None:
    assert is_catastrophic_category("contact") is False
    assert is_catastrophic_category("location") is False


# ----- render_on_clause: quoted entity-alias grammar -------------------


def test_render_on_clause_single_pair() -> None:
    out = render_on_clause(
        (("id", "workspace_id"),),
        source_alias="workspace",
        target_alias="user",
    )
    assert out == '"workspace"."id" = "user"."workspace_id"'


def test_render_on_clause_composite_pairs_joined_with_and() -> None:
    out = render_on_clause(
        (
            ("tenant_id", "tenant_id"),
            ("id", "order_id"),
        ),
        source_alias="order",
        target_alias="order_item",
    )
    assert out == (
        '"order"."tenant_id" = "order_item"."tenant_id" AND "order"."id" = "order_item"."order_id"'
    )


def test_render_on_clause_empty_pairs_returns_empty_string() -> None:
    # Defensive: a join with no column pairs renders an empty clause
    # rather than raising. The aggregator never constructs this, but the
    # pure helper stays total.
    assert render_on_clause((), source_alias="a", target_alias="b") == ""


def test_render_on_clause_passes_redaction_placeholder_through() -> None:
    # A catastrophic FK column is masked to <redacted_column> BEFORE
    # rendering; the placeholder (not identifier-shaped) renders quoted.
    out = render_on_clause(
        (("<redacted_column>", "id"),),
        source_alias="billing_profile",
        target_alias="workspace",
    )
    assert out == '"billing_profile"."<redacted_column>" = "workspace"."id"'


def test_render_on_clause_contains_no_table_breaking_pipe() -> None:
    # The clause renders inside a single Markdown table cell (backtick
    # span). It must never contain a raw `|`.
    out = render_on_clause(
        (("id", "user_id"),),
        source_alias="user",
        target_alias="session",
    )
    assert "|" not in out
