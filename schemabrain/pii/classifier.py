"""Heuristic PII classifier — regex on column name → category set.

The classifier produces `(sensitivity, categories)` for one column based
on its name, its declared data type (for the integer-FK guard), and (a
hook for future use) its shape patterns.

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

## Two refinements on top of the raw regex pass

Two systematic false-positive shapes that pure column-name regex
cannot disambiguate:

  - `<noun>_name` in non-PII contexts (e.g. `product_name` in a catalog
    table) matched the bare `name` rule and tagged catalog columns as
    `pii (contact)`. Handled here by `_NON_PII_NAME_RE` — when a
    column matches `^<denylist>_name$`, the name rule is skipped in
    the loop. The bare-`name` case (a column literally named `name`
    in a lookup table) is a known remaining limitation — there is no
    column-level signal that distinguishes it from `customers.name`,
    and we accept that over-tagging per the docstring posture.

  - `<token>_id` integer FKs whose name contains a category keyword
    (e.g. `address_id BIGINT` matching `address` → `contact`) tagged
    the FK as PII when the actual PII lives on the referenced table.
    Handled here by an integer-FK guard: when the column's declared
    type is integer-like AND the name matches the `<word>_id` shape
    (alphanumeric segments separated by underscores, ending in `_id`)
    AND the column is not a primary key, the matched category set
    is intersected with `_FK_SAFE_CATEGORIES` — the categories where
    the FK itself remains PII even as an integer reference (e.g.
    `patient_id` is HIPAA-sensitive, `cookie_id` is an online
    identifier, `national_id` is a government ID, `clickstream_id`
    is a behavioral identifier under GDPR Recital 30). The PK
    exemption avoids stripping the canonical row-ID's tags on the
    table that OWNS the row (e.g. a `health_records.health_record_id`
    PK is the row's identifier, not a reference).
"""

from __future__ import annotations

import re
from typing import Final

from schemabrain.pii.categories import PIICategory, Sensitivity


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
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alts})(?![A-Za-z0-9])", re.IGNORECASE)


# The "name" rule is referenced separately by `classify_column` so it
# can be suppressed for `<denylist>_name` shapes (S1 guard). Holding it
# as a named module constant keeps the lookup branch-free (`is` check
# against the canonical pattern object) rather than relying on a label
# field threaded through every other rule's row.
_NAME_RULE_PATTERN: Final[re.Pattern[str]] = _kw(
    "name",
    "user_name",
    "customer_name",
    "display_name",
    "legal_name",
    "contact_name",
)
_NAME_RULE_CATS: Final[frozenset[PIICategory]] = frozenset({"contact"})

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
    # Bare name variants — common ecommerce/CRM shapes (`user_name`,
    # `customer_name`, `display_name`, `legal_name`). The `name`
    # alternative covers the bare column too. This rule is held as
    # `_NAME_RULE_PATTERN` above so the S1 denylist can skip it on
    # `<noun>_name` shapes (`product_name`, `category_name`, etc.).
    (_NAME_RULE_PATTERN, _NAME_RULE_CATS),
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
    (
        _kw("account_number", "bank_account", "routing_number", "sort_code"),
        frozenset({"payment_card"}),
    ),
    # LB-3 (firewall E2E audit 2026-05-30): concatenated + abbreviated
    # card-number shapes the `card_number` rule missed — `_kw` boundaries
    # don't fire mid-word (`creditcard`), and several of the most common
    # names had no rule at all (`credit_card`, `cc_num`). SWIFT/BIC bank
    # identifier codes join here as catastrophic financial identifiers.
    # These classified to `public` with no operator misconfig pre-fix.
    (
        _kw(
            "credit_card",
            "creditcard",
            "credit_card_no",
            "debit_card",
            "debitcard",
            "cardnumber",
            "card_no",
            "cardno",
            "cc_num",
            "ccnum",
            "cc_number",
            "ccnumber",
            "swift_code",
            "swift_bic",
            "bic",
        ),
        frozenset({"payment_card"}),
    ),
    # ---- health ----
    (_kw("diagnosis", "medication", "treatment"), frozenset({"health"})),
    (_kw("icd9", "icd_9", "icd10", "icd_10", "cpt_code"), frozenset({"health"})),
    (_kw("phi", "mrn", "medical_record"), frozenset({"health"})),
    (
        _kw(
            "allergy",
            "blood_type",
            "prescription",
            "lab_result",
            "symptom",
            "bmi",
            "vital_sign",
        ),
        frozenset({"health"}),
    ),
    # `patient` matches `patient_id`, `patient_name`, `patient_age`,
    # etc. `insurance_id` and `health_record` cover HIPAA insurance
    # identifiers + medical record references that the existing `mrn`
    # rule misses. `patient` deliberately matches as a substring per
    # the breadth-over-precision posture — `patient_satisfaction_score`
    # will over-tag, which is consistent with the design.
    (
        _kw("patient", "insurance_id", "health_record"),
        frozenset({"health"}),
    ),
    # Encounter-level health identifiers — visit/admission/discharge/
    # encounter IDs reveal that a person had a clinical interaction,
    # which is HIPAA-sensitive even without the clinical detail. These
    # are commonly integer FKs whose category should survive the S2
    # guard (`health` is in `_FK_SAFE_CATEGORIES`).
    (
        _kw("encounter_id", "visit_id", "admission_id", "discharge_id"),
        frozenset({"health"}),
    ),
    # Health-insurance member identifiers. `subscriber_id` alone is
    # ambiguous (newsletter contexts), so we require the `insurance_`
    # prefix to keep the rule unambiguous.
    (
        _kw("insurance_member_id", "insurance_subscriber_id"),
        frozenset({"health"}),
    ),
    # ---- genetic ----
    (_kw("genome", "genotype", "dna_seq", "rsid"), frozenset({"genetic"})),
    # ---- biometric ----
    # `face_embedding`/`face_print`/`face_vector` are common shapes for
    # the BYTEA / vector storage of facial biometrics; the existing
    # `face_id` / `face_hash` rule misses them.
    (
        _kw(
            "fingerprint",
            "face_id",
            "face_hash",
            "face_embedding",
            "face_print",
            "face_vector",
            "voiceprint",
            "iris",
            "biometric",
        ),
        frozenset({"biometric"}),
    ),
    # ---- behavioral ----
    (
        _kw("purchase_history", "browse_history", "clickstream", "click_stream", "event_log"),
        frozenset({"behavioral"}),
    ),
    # ---- online_identifier ----
    # ECJ ruling treats IP addresses as personal data under GDPR;
    # bare `ip`, `client_ip`, `remote_ip`, `src_ip`, and `ip_addr` are
    # all common web-app column shapes that must classify.
    (
        _kw("ip", "ip_addr", "ip_address", "client_ip", "remote_ip", "src_ip"),
        frozenset({"online_identifier"}),
    ),
    (
        _kw("cookie_id", "device_id", "session_id", "idfa", "gaid"),
        frozenset({"online_identifier"}),
    ),
    (
        _kw("aid", "google_aid", "fb_aid", "advertising_aid"),
        frozenset({"online_identifier"}),
    ),
    # Blockchain wallet addresses are pseudonymous on-chain identifiers.
    # The bare `address` rule above also matches `wallet_address`, so
    # this rule contributes `online_identifier` in union with `contact`
    # — operators can downgrade at the column-overlay layer if needed.
    # Transaction hashes themselves are public on-chain data and are
    # NOT tagged here (over-tagging public ledger content would obscure
    # genuine PII signal in the audit trail).
    (
        _kw("wallet_address", "blockchain_address", "crypto_address"),
        frozenset({"online_identifier"}),
    ),
    # ---- credential ----
    (_kw("password", "passwd", "pwd"), frozenset({"credential"})),
    (_kw("api_key", "secret"), frozenset({"credential"})),
    (
        _kw("token", "refresh_token", "access_token", "session_id"),
        frozenset({"credential"}),
    ),
    (_kw("pass_hash", "pw_hash"), frozenset({"credential"})),
    # Crypto private keys and seed phrases are key material whose
    # disclosure is catastrophic — treated as credentials alongside
    # API keys and passwords. Joining the credential bucket also means
    # they participate in the FK-safe set if someone (oddly) stores them
    # as an integer FK reference.
    (
        _kw("private_key", "seed_phrase", "mnemonic_phrase", "mnemonic"),
        frozenset({"credential"}),
    ),
    # Knowledge-based auth factors: PINs, recovery codes, and the
    # classic "security question" answer. Disclosure of any of these
    # is equivalent to disclosing a password — they're typed in lieu
    # of one during second-factor flows.
    (
        _kw("pin", "pin_code", "recovery_code", "security_answer"),
        frozenset({"credential"}),
    ),
    # Cryptographic and cloud-credential key material. SSH/KMS/signing
    # keys decrypt at-rest data or forge identity; an `aws_access_key`
    # is the public half of a credential pair whose disclosure narrows
    # the brute-force surface to the matching secret.
    (
        _kw(
            "ssh_key",
            "kms_key",
            "encryption_key",
            "signing_key",
            "aws_access_key",
        ),
        frozenset({"credential"}),
    ),
    # Passwordless and 2FA secrets. Magic-link tokens, WebAuthn
    # credential blobs, and TOTP shared seeds all let an attacker
    # authenticate without the user's password.
    (
        _kw("magic_link", "webauthn_credential", "totp_seed"),
        frozenset({"credential"}),
    ),
    # LB-3 (firewall E2E audit 2026-05-30): concatenated credential
    # shapes the `_kw` boundary missed — `password` does not match
    # `passwordhash`, `api_key` does not match `apikey`, `access_token`
    # does not match `accesstoken`. Disclosure of any is catastrophic;
    # these classified to `public` with no operator misconfig pre-fix.
    (
        _kw(
            "passwordhash",
            "pwhash",
            "passhash",
            "pwdhash",
            "apikey",
            "apisecret",
            "apitoken",
            "accesstoken",
            "refreshtoken",
            "authtoken",
            "bearertoken",
            "sessiontoken",
            "privatekey",
            "secretkey",
            "accesskey",
            "signingkey",
            "encryptionkey",
        ),
        frozenset({"credential"}),
    ),
    # ---- government_id ----
    (_kw("ssn", "tin", "nino"), frozenset({"government_id"})),
    (_kw("passport"), frozenset({"government_id"})),
    # Plural `drivers_license` / `drivers_license_number` join the
    # singular variants — both shapes are common.
    (
        _kw(
            "driver_license",
            "driver_licence",
            "drivers_license",
            "drivers_licence",
            "dl_number",
            "dl_no",
            "driver_license_number",
            "drivers_license_number",
        ),
        frozenset({"government_id"}),
    ),
    (_kw("tax_id"), frozenset({"government_id"})),
    (
        _kw("national_id", "aadhar", "aadhaar", "voter_id", "ein"),
        frozenset({"government_id"}),
    ),
    # NPI (US National Provider Identifier, HHS-issued) and DEA numbers
    # are government-issued professional identifiers. They live in the
    # government_id bucket rather than `health` because the *identifier*
    # is the regulator's, not the patient's. A column named `provider_npi`
    # on a claims table will tag here.
    (
        _kw("npi", "provider_npi", "dea_number"),
        frozenset({"government_id"}),
    ),
    # ---- location ----
    (_kw("lat", "latitude", "lon", "lng", "longitude"), frozenset({"location"})),
    (
        _kw("location", "geolocation", "gps", "coords", "coord"),
        frozenset({"location"}),
    ),
    # ---- demographic_protected ----
    # DOB and birthdate variants live here, NOT in `contact`. DOB is
    # a HIPAA Safe Harbor identifier and a GDPR Article 9 attribute;
    # tagging it `contact` (as v1 did) understates the sensitivity
    # bucket the audit pipeline should group it under.
    (
        _kw("dob", "date_of_birth", "birth_date", "birthdate"),
        frozenset({"demographic_protected"}),
    ),
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
    # `age` is a HIPAA Safe Harbor identifier (ages > 89 specifically,
    # but the column itself is the demographic attribute) and a GDPR
    # demographic attribute. Joins the same demographic_protected
    # group as gender / nationality.
    (
        _kw(
            "age",
            "gender",
            "sex",
            "disability",
            "marital_status",
            "nationality",
            "veteran_status",
        ),
        frozenset({"demographic_protected"}),
    ),
    # ---- i18n ----
    # Non-English column-name shapes for the same underlying PII. The
    # English rule table is the canonical surface; this block extends
    # coverage to ES, FR, DE, and PT-BR for the regulatory categories
    # most likely to appear in localised schemas. Tokens are listed
    # under the English category they semantically map to so the
    # category-coverage invariant continues to hold.
    #
    # `email_adresse` (DE) is intentionally absent — the existing
    # `e_?mail` rule already matches its `email` substring through the
    # non-alphanumeric boundary semantics.
    (
        # ES: correo electrónico; FR: courriel.
        _kw("correo_electronico", "courriel"),
        frozenset({"contact"}),
    ),
    (
        # DE: Telefonnummer; ES: número de teléfono; FR: numéro de
        # téléphone.
        _kw("telefonnummer", "numero_telefono", "numero_telephone"),
        frozenset({"contact"}),
    ),
    (
        # DE: Kreditkartennummer; FR: numéro de carte de crédit; ES:
        # número de tarjeta de crédito.
        _kw(
            "kreditkartennummer",
            "numero_carte_credit",
            "numero_tarjeta_credito",
        ),
        frozenset({"payment_card"}),
    ),
    (
        # DE: Sozialversicherungsnummer; ES: número de seguridad social;
        # FR: numéro de sécurité sociale.
        _kw(
            "sozialversicherungsnummer",
            "numero_seguridad_social",
            "numero_securite_sociale",
        ),
        frozenset({"government_id"}),
    ),
    # PT-BR: CPF is the individual tax/national ID; CNPJ is the
    # corporate equivalent. Both are sensitive enough that disclosure
    # enables identity-bound queries against Brazilian government
    # systems.
    (_kw("cpf"), frozenset({"government_id"})),
    (_kw("cnpj"), frozenset({"government_id"})),
    # LB-3 (firewall E2E audit 2026-05-30): non-English national / tax
    # identifiers that classified to `public` under the default floor —
    # ES (DNI / NIF / NIE), IT (codice fiscale), MX (CURP), SG (NRIC),
    # NL (BSN), PL (PESEL). Disclosure enables identity-bound queries
    # against the issuing government's systems, so they belong in the
    # always-on catastrophic floor alongside SSN.
    (
        _kw(
            "dni",
            "nif",
            "nie",
            "codice_fiscale",
            "curp",
            "nric",
            "bsn",
            "pesel",
        ),
        frozenset({"government_id"}),
    ),
)

# Public count constant — pinned by CI so adding a rule is a deliberate
# action that requires updating the test alongside.
RULE_COUNT: Final[int] = len(_RULES)


# ----- S1: `<noun>_name` denylist ----------------------------------
# Common thing-noun prefixes that should NOT classify when paired
# with `_name`. The classifier suppresses the `_NAME_RULE_PATTERN`
# match for columns of shape `^<prefix>_name$`. Other rule matches
# on the same column still fire — e.g. `country_name` still matches
# the `country` rule (contact). The fix targets the specific
# false-positive shape (`product_name` in catalog tables), not the
# broader question of table-level context.
# Deliberately CONSERVATIVE — only prefixes that are unambiguously
# non-person attributes across HR / CRM / SaaS schemas. Words that
# can legitimately denote a person's attribute in some context have
# been excluded:
#
#   - `company`, `organization` — can be a person's employer in
#     CRM contact rows (legal-entity names paired with a natural
#     person are GDPR Art. 4(1) personal data).
#   - `team`, `department` — workforce schemas often use
#     `team_name` for a person's team affiliation.
#   - `region`, `zone` — can be a person's geographic region in
#     HR / payroll tables.
#
# Per the docstring posture (over-tag is safer than under-tag), the
# excluded shapes keep their `contact` tag. Adding to this set is
# a deliberate action — the test class
# `TestS1NonPiiNameDenylist::test_pii_named_shapes_still_classify`
# locks the people-name shapes that must continue to classify.
_NON_PII_NAME_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "product",
        "brand",
        "category",
        "language",
        "currency",
        "tag",
        "event",
        "feature",
        "setting",
        "field",
        "column",
        "table",
        "database",
        "service",
        "process",
        "task",
        "job",
        "file",
        "directory",
        "folder",
        "bucket",
        "device",
        "app",
        "module",
        "plugin",
        "report",
        "dashboard",
        "widget",
        "channel",
        "topic",
        "queue",
        "host",
        "cluster",
    }
)
# Build the regex with longest-first alternation order so that
# multi-segment prefixes (if added later) don't get shadowed by a
# shorter prefix matching first. `fullmatch` already enforces the
# `_name$` anchor so the risk is small today, but ordering by length
# is the future-safe construction.
_NON_PII_NAME_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?:{'|'.join(sorted(_NON_PII_NAME_PREFIXES, key=len, reverse=True))})_name$",
    re.IGNORECASE,
)


# Table-level extension of the denylist. The fix above suppresses
# the name rule for `<noun>_name` shapes — but bare `name` columns on
# tables NAMED after a denylist noun (e.g. `products.name`,
# `categories.name`) were still slipping through and getting tagged as
# `contact`. A query grouped by `product.name` would come back with
# `pii_categories=['contact']` even though no contact info was touched
# anywhere in the SQL.
#
# We accept both singular and plural forms because real schemas use
# both (`product` vs `products`, `category` vs `categories`). English
# pluralisation rules are messy enough that explicit enumeration is
# clearer than a singularisation helper. Add new entries here when
# new failure modes surface — same posture as `_NON_PII_NAME_PREFIXES`.
_NON_PII_NAME_TABLE_NAMES: Final[frozenset[str]] = frozenset(
    {prefix for prefix in _NON_PII_NAME_PREFIXES}
    | {
        # Plural forms — English `-s` / `-es` / `-ies` endings.
        "products",
        "brands",
        "categories",
        "languages",
        "currencies",
        "tags",
        "events",
        "features",
        "settings",
        "fields",
        "columns",
        "tables",
        "databases",
        "services",
        "processes",
        "tasks",
        "jobs",
        "files",
        "directories",
        "folders",
        "buckets",
        "devices",
        "apps",
        "modules",
        "plugins",
        "reports",
        "dashboards",
        "widgets",
        "channels",
        "topics",
        "queues",
        "hosts",
        "clusters",
    }
)


# ----- S2: integer-FK guard ----------------------------------------
# Categories where the FK itself remains PII even as an integer
# reference. Examples:
#   - `patient_id BIGINT` is a HIPAA identifier even though the actual
#     name/DOB live on the patients table.
#   - `cookie_id BIGINT` is an online identifier.
#   - `national_id BIGINT` is a government ID.
#   - `clickstream_id BIGINT` is a behavioral identifier (GDPR
#     Recital 30 covers clickstream identifiers as personal data).
#   - `location_id BIGINT` is a location identifier (a tracking row's
#     pointer to a precise GPS sample).
# Categories outside this set (`contact`, `financial`, `payment_card`,
# `biometric`, `genetic`) are inherited only through the referenced
# row; the integer FK itself doesn't leak that content, so the guard
# strips them.
_FK_SAFE_CATEGORIES: Final[frozenset[PIICategory]] = frozenset(
    {
        "credential",
        "online_identifier",
        "government_id",
        "health",
        "demographic_protected",
        "behavioral",
        "location",
    }
)
# Integer-like data type prefixes (lowercased). Postgres, MySQL,
# SQLite, SQLAlchemy default reflection — all covered. The matcher
# strips `(N)` parameterisation suffixes, `[]` array suffixes, and
# trailing modifiers like `UNSIGNED` / `ZEROFILL` before comparison.
#
# NUMERIC / NUMBER are deliberately EXCLUDED: parameterised
# `NUMERIC(10,2)` and Oracle `NUMBER(5,2)` are decimal types whose
# FK semantics differ from integer FKs. A column named `rate_id
# NUMERIC(10,2)` should not have its categories silently stripped.
# Operators using NUMERIC-as-integer (rare) will get the over-tag
# fallback, consistent with the breadth-over-precision posture.
_INTEGER_TYPE_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "int",
        "integer",
        "bigint",
        "smallint",
        "tinyint",
        "mediumint",
        "int2",
        "int4",
        "int8",
        "serial",
        "bigserial",
        "smallserial",
    }
)
# Column-name shape for the FK guard: `<word>_id`, alphanumeric
# segments separated by underscores. Note that this can also match
# PK columns named `<table>_id` (rare but real — some schemas name
# the PK after the table). The classifier accepts an explicit
# `is_primary_key` parameter so the indexer can skip the guard on
# PKs; callers that don't pass it default to `False` (worst-case
# guard fires), which is the safe direction for a single misidentified
# column compared to under-tagging an FK.
_FK_SHAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*_id$",
    re.IGNORECASE,
)


def _is_integer_type(column_type: str) -> bool:
    """Return True when `column_type` is an integer-like SQL type.

    Strips `(N[, M])` parameter suffixes, `[]` array suffixes, and
    trailing modifier tokens (`UNSIGNED`, `ZEROFILL`, etc.) so
    `BIGINT(20)`, `INTEGER(11)`, `int4[]`, `INT UNSIGNED`, and
    `TINYINT UNSIGNED ZEROFILL` all classify as integer.
    """
    # Split off `(N[, M])` and `[]` suffixes, then take the first
    # whitespace-separated token so MySQL-style modifiers like
    # `UNSIGNED` / `ZEROFILL` don't poison the lookup.
    pruned = column_type.split("(", 1)[0].split("[", 1)[0]
    tokens = pruned.split()
    if not tokens:
        return False
    return tokens[0].lower() in _INTEGER_TYPE_PREFIXES


def classify_column(
    column_name: str,
    column_type: str | None = None,
    is_primary_key: bool = False,
    table_name: str | None = None,
    shape_patterns: tuple[str, ...] = (),  # reserved for future heuristics
) -> tuple[Sensitivity, frozenset[PIICategory]]:
    """Classify a single column by name (and optionally declared type).

    Returns `(sensitivity, categories)`:
      - `sensitivity` is `"pii"` if any category matched, else `"public"`.
      - `categories` is the union of all matched rules' category sets,
        subject to two refinements:

          * S1 guard — when `column_name` is a `<noun>_name` shape in
            the non-PII-noun denylist OR when `column_name == "name"`
            AND `table_name` is in the non-PII table-name denylist,
            the bare-name / `<x>_name` contact rule is skipped. Other
            rule matches still fire. Catches `products.name` and
            `categories.name` getting tagged `contact` despite zero
            contact information in the column.

          * S2 guard — when `column_type` is integer-like AND
            `column_name` matches the `<token>_id` FK shape AND
            `is_primary_key` is `False`, the matched category set
            is intersected with `_FK_SAFE_CATEGORIES`. The remaining
            categories are those where the FK itself remains PII
            even as an integer ref. PKs are exempt because a PK
            named `<table>_id` is the canonical row identifier, not
            a reference to a row elsewhere — its tag content should
            stay intact.

    `column_type`, `is_primary_key`, and `table_name` are optional;
    passing the defaults (`None`, `False`, `None`) skips the S2
    guard's type+PK refinements and the S1 guard's table-context
    extension (safe direction — categories are kept, the over-tag
    posture is preserved). Callers that have the source-of-truth
    column metadata (the indexer) should pass them through.

    `shape_patterns` is in the signature so a future heuristic refinement
    can read it without changing the contract. v1 ignores it.
    """
    # S1: detect `<denylist>_name` shape up front so the name rule
    # can be skipped in the loop. Set membership check via fullmatch
    # so suffix collisions don't slip through.
    skip_name_rule = _NON_PII_NAME_RE.fullmatch(column_name) is not None
    # S1 table-context extension: bare `name` on a non-PII-table
    # (products / categories / brands / etc.) also skips the name
    # rule. Without this, `products.name` returns `contact` despite
    # being a SKU label, not a person's name.
    if (
        not skip_name_rule
        and table_name is not None
        and column_name.lower() == "name"
        and table_name.lower() in _NON_PII_NAME_TABLE_NAMES
    ):
        skip_name_rule = True

    matched: set[PIICategory] = set()
    for pattern, cats in _RULES:
        if skip_name_rule and pattern is _NAME_RULE_PATTERN:
            continue
        if pattern.search(column_name):
            matched.update(cats)

    # S2: integer-FK guard. Apply AFTER all rules so we can intersect
    # the union with the FK-safe allowlist rather than per-rule logic.
    # Skips PKs — a PK named `<table>_id` is the canonical row ID, not
    # a reference, so the inherited categories belong on the row itself.
    if (
        column_type is not None
        and not is_primary_key
        and _is_integer_type(column_type)
        and _FK_SHAPE_RE.fullmatch(column_name)
    ):
        matched &= _FK_SAFE_CATEGORIES

    if not matched:
        return ("public", frozenset())
    return ("pii", frozenset(matched))
