"""PII-001 — i18n column names bypass the PII classifier.

Background:

    Spanish/French/German/Portuguese column names ALL classify as
    ``('public', frozenset())``: ``correo_electronico``,
    ``numero_seguridad_social``, ``cpf``, ``cnpj``,
    ``kreditkartennummer``, ``sozialversicherungsnummer``,
    ``telefonnummer``.

Expected SECURE behaviour:

    The classifier is the firewall's single source of PII truth at
    index time. The rule table at ``schemabrain/pii/classifier.py`` is
    English-only — so any non-English-named column carrying real PII
    (email, SSN-equivalents, credit cards) is classified ``public`` and
    flows through the redaction floor untouched. ``describe_table`` and
    ``find_relevant_tables`` will then surface the raw column name to
    the agent.

The fix is either (a) translate the rule table into the supported
locales, or (b) ship a YAML overlay layer so operators can add
language-specific column-name rules without a code change. Until then
this test FAILS, locking the regression in.
"""

from __future__ import annotations

import pytest

from schemabrain.pii import classify_column

pytestmark = [pytest.mark.firewall_bypass]


# ``locale`` and ``equivalent_en`` are recorded so a future remediation
# PR has the context to map the regex addition correctly.
_I18N_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("correo_electronico", "es", "email"),
    ("numero_seguridad_social", "es", "ssn"),
    ("cpf", "pt-BR", "national_id"),
    ("cnpj", "pt-BR", "tax_id"),
    ("kreditkartennummer", "de", "card_number"),
    ("sozialversicherungsnummer", "de", "ssn"),
    ("telefonnummer", "de", "phone"),
)


@pytest.mark.parametrize(("column", "locale", "equivalent_en"), _I18N_COLUMNS)
def test_i18n_column_classifies_as_pii(column: str, locale: str, equivalent_en: str) -> None:
    """The classifier MUST tag this column as PII.

    Today this fails — the column comes back as ``('public', frozenset())``.
    When the classifier is hardened (rule additions OR overlay), flip
    the assertion so it locks the secure behaviour permanently.
    """
    sensitivity, categories = classify_column(column)

    # SECURE behaviour we'd like to see — every entry in `_I18N_COLUMNS`
    # is a textbook PII column in some language. The assertion below is
    # how this test should READ once the fix lands.
    assert sensitivity == "pii", (
        f"BYPASS: i18n column {column!r} (locale={locale}, "
        f"english={equivalent_en!r}) classified as {sensitivity!r} "
        f"with categories={sorted(categories)}; expected PII. "
        f"The agent will see the raw column name through "
        f"describe_table/find_relevant_tables."
    )
    assert categories, (
        f"BYPASS: i18n column {column!r} produced empty category set; "
        f"expected at least one PII category."
    )
