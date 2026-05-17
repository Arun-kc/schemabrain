"""Heuristic PII classifier — regex on column name → category set.

The classifier produces `(sensitivity, categories)` for one column based
on its name and (a hook for future use) its shape patterns. v1 only
reads the column name; shape signal is in the signature so a future
heuristic refinement does not change callers.

Rules are module-level constants: a tuple of `(compiled_regex,
frozenset[PIICategory])` pairs. A column matching multiple rules carries
the *union* of all matched categories — set semantics throughout, no
precedence ordering.

Sensitivity is derived: any non-empty category set → `"pii"`; empty set
→ `"public"`. `"internal"` and `"confidential"` are reserved for
operator-asserted classification (a future PR) — the heuristic
classifier only ever produces `"public"` or `"pii"`. Over-tagging is
the safer default at the heuristic layer; operators can downgrade later
when they know a column is genuinely aggregate / non-personal.

The rule table is *closed* at v1: when an operator hits a column name
our rules miss, the fix is to add a rule to this module. YAML-overlay
extensibility is deferred to first real demand. CI tests pin (a) the
rule count and (b) that every `PIICategory` has at least one rule
producing it — drift fails CI before merge.

## Why custom boundary semantics

The standard `\b` regex boundary treats `_` (underscore) as a word
character, so `\bemail\b` does NOT match `email` inside `email_address`
— the boundary doesn't fire at the underscore. SQL column names are
snake_case overwhelmingly, so `_` must act as a segment boundary for
keyword matching. The `_kw()` helper wraps each keyword in
`(?<![A-Za-z0-9])…(?![A-Za-z0-9])` lookarounds so any non-alphanumeric
character (including `_`, `-`, `.`, start, end) terminates the keyword.
"""

from __future__ import annotations

import re
from typing import Final

from schemabrain.pii.categories import PIICategory, Sensitivity

# Matching is case-insensitive against the column name.
_CASE_INSENSITIVE: Final[int] = re.IGNORECASE


def _kw(*alternatives: str) -> re.Pattern[str]:
    """Compile a keyword pattern with non-alphanumeric boundaries.

    Each alternative is wrapped so any non-alphanumeric character
    (including `_`) acts as a boundary. This makes `email` match inside
    `email_address` and `user_email` but not inside `emailish`.

    Patterns inside alternatives may include `_?` for optional
    separators (e.g. `e_?mail` to match both `email` and `e_mail`) —
    underscores INSIDE an alternative are kept verbatim.
    """
    alts = "|".join(f"(?:{a})" for a in alternatives)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", _CASE_INSENSITIVE)


# Rule table — `(compiled_regex, frozenset[PIICategory])`. Order is
# stable for deterministic iteration but does NOT imply precedence:
# every matching rule contributes its categories via set union.
#
# The patterns deliberately favour breadth over precision at the
# column-name layer. False positives at v1 ("address" matched on
# `network_address` → contact) are accepted because the cost is
# over-tagging an audit row, while false negatives would silently
# under-report PII in the audit trail.
_RULES: Final[tuple[tuple[re.Pattern[str], frozenset[PIICategory]], ...]] = (
    # ---- contact ----
    (_kw("email", "e_?mail"), frozenset({"contact"})),
    (_kw("phone", "mobile", "fax"), frozenset({"contact"})),
    (_kw("address", "address_line", "address_street", "street"), frozenset({"contact"})),
    (
        _kw("first_name", "last_name", "middle_name", "full_name", "given_name", "family_name"),
        frozenset({"contact"}),
    ),
    (_kw("zip", "zip_code", "postal_code"), frozenset({"contact"})),
    (_kw("city", "state", "country"), frozenset({"contact"})),
    # ---- financial ----
    (_kw("salary", "wage", "income"), frozenset({"financial"})),
    (_kw("balance", "revenue"), frozenset({"financial"})),
    (
        _kw("amount", "transaction_amount", "transaction_value"),
        frozenset({"financial"}),
    ),
    # ---- payment_card ----
    (
        _kw("card_number", "card_num", "card_pan", "pan"),
        frozenset({"payment_card"}),
    ),
    (_kw("cvv", "cvc"), frozenset({"payment_card"})),
    (_kw("iban"), frozenset({"payment_card"})),
    # ---- health ----
    (_kw("diagnosis", "medication", "treatment"), frozenset({"health"})),
    (_kw("icd9", "icd_9", "icd10", "icd_10", "cpt_code"), frozenset({"health"})),
    (_kw("phi", "mrn", "medical_record"), frozenset({"health"})),
    # ---- genetic ----
    (_kw("genome", "genotype", "dna_seq", "rsid"), frozenset({"genetic"})),
    # ---- biometric ----
    (
        _kw("fingerprint", "face_id", "face_hash", "voiceprint", "iris", "biometric"),
        frozenset({"biometric"}),
    ),
    # ---- behavioral ----
    (
        _kw("purchase_history", "browse_history", "clickstream", "click_stream", "event_log"),
        frozenset({"behavioral"}),
    ),
    # ---- online_identifier ----
    (_kw("ip_address"), frozenset({"online_identifier"})),
    (
        _kw("cookie_id", "device_id", "idfa", "gaid"),
        frozenset({"online_identifier"}),
    ),
    (
        _kw("aid", "google_aid", "fb_aid", "advertising_aid"),
        frozenset({"online_identifier"}),
    ),
    # ---- credential ----
    (_kw("password", "passwd", "pwd"), frozenset({"credential"})),
    (_kw("api_key", "secret"), frozenset({"credential"})),
    (
        _kw("token", "refresh_token", "access_token", "session_id"),
        frozenset({"credential"}),
    ),
    # ---- government_id ----
    (_kw("ssn", "tin", "nino"), frozenset({"government_id"})),
    (_kw("passport"), frozenset({"government_id"})),
    (
        _kw("driver_license", "dl_number", "dl_no", "driver_license_number"),
        frozenset({"government_id"}),
    ),
    (_kw("tax_id"), frozenset({"government_id"})),
    # ---- location ----
    (_kw("lat", "latitude", "lon", "lng", "longitude"), frozenset({"location"})),
    (_kw("geolocation", "gps", "coords", "coord"), frozenset({"location"})),
    # ---- demographic_protected ----
    (
        _kw("race", "ethnicity", "religion"),
        frozenset({"demographic_protected"}),
    ),
    (
        _kw("orientation", "sexual_orientation", "gender_orientation"),
        frozenset({"demographic_protected"}),
    ),
    (
        _kw(
            "political_party",
            "political_view",
            "political_affiliation",
            "trade_union",
        ),
        frozenset({"demographic_protected"}),
    ),
)

# Public count constant — pinned by CI so adding a rule is a deliberate
# action that requires updating the test alongside.
RULE_COUNT: Final[int] = len(_RULES)


def classify_column(
    column_name: str,
    shape_patterns: tuple[str, ...] = (),  # reserved for future heuristics
) -> tuple[Sensitivity, frozenset[PIICategory]]:
    """Classify a single column by name.

    Returns `(sensitivity, categories)`:
      - `sensitivity` is `"pii"` if any category matched, else `"public"`.
      - `categories` is the union of all matched rules' category sets.

    `shape_patterns` is in the signature so a future heuristic refinement
    can read it without changing the contract. v1 ignores it.
    """
    matched: set[PIICategory] = set()
    for pattern, cats in _RULES:
        if pattern.search(column_name):
            matched.update(cats)
    if not matched:
        return ("public", frozenset())
    return ("pii", frozenset(matched))
