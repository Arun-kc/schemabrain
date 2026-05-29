"""PII-013 — auth secrets and recovery keys bypass the classifier.

Background:

    These all classify as ``('public', frozenset())``: ``pin``,
    ``pin_code``, ``recovery_code``, ``ssh_key``, ``kms_key``,
    ``encryption_key``, ``signing_key``, ``aws_access_key``,
    ``security_answer``, ``magic_link``, ``webauthn_credential``,
    ``totp_seed``.

Expected SECURE behaviour:

    All of these are credentials by intent — knowing one lets an
    attacker authenticate as the user or read encrypted data. The
    classifier already covers ``password``, ``api_key``, ``secret``,
    ``token`` — these are simply additional credential shapes that
    were never enumerated.

The classifier's rule table is "closed at v1" per its own docstring;
adding new credentials is a deliberate code change. This test fails
until that change ships.
"""

from __future__ import annotations

import pytest

from schemabrain.pii import classify_column

pytestmark = [pytest.mark.firewall_bypass]


# Each row: (column_name, why_it's_a_credential). The second tuple
# element appears in the assertion failure message so a future
# remediation PR has context for the rule-table addition.
_AUTH_SECRET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("pin", "second-factor PIN; knowing it lets an attacker auth"),
    ("pin_code", "second-factor PIN code variant"),
    ("recovery_code", "MFA recovery codes — equivalent to a password"),
    ("ssh_key", "private SSH key material"),
    ("kms_key", "KMS key material — decrypts at-rest data"),
    ("encryption_key", "symmetric/asymmetric key material"),
    ("signing_key", "private signing key; forges signatures"),
    ("aws_access_key", "AWS credential pair half"),
    ("security_answer", "knowledge-based auth answer"),
    ("magic_link", "one-time-use auth token in URL form"),
    ("webauthn_credential", "WebAuthn private credential blob"),
    ("totp_seed", "TOTP shared secret; clones the second factor"),
)


@pytest.mark.parametrize(("column", "reason"), _AUTH_SECRET_COLUMNS)
def test_auth_secret_column_classifies_as_credential(column: str, reason: str) -> None:
    """The classifier MUST tag this column as ``credential``.

    ``credential`` is one of the catastrophic-leak categories — every
    higher layer (``describe_table``, ``describe_entity``, ``get_metric``)
    redacts unconditionally when this tag is present. Misclassifying
    the column as ``public`` skips that protection entirely.
    """
    sensitivity, categories = classify_column(column)

    assert sensitivity == "pii", (
        f"BYPASS: auth-secret column {column!r} ({reason}) classified "
        f"as {sensitivity!r}; expected 'pii'."
    )
    assert "credential" in categories, (
        f"BYPASS: auth-secret column {column!r} ({reason}) produced "
        f"categories={sorted(categories)}; expected 'credential' to be "
        f"present so the catastrophic-leak floor redacts it."
    )
