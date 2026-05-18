-- Synthetic PII regression fixture for Schema Brain's heuristic classifier.
--
-- Designed to exercise EVERY category in `schemabrain/pii/categories.py`
-- (12 PIICategory values) plus the four bug shapes the 2026-05-18
-- production-DB smoke surfaced:
--
--   S1 — `<noun>_name` columns in non-PII tables (`product_name` in
--        `non_pii_things`) tagged `pii (contact)`.
--   S2 — `<token>_id` INTEGER FKs tagged via the referenced table's
--        keyword (`address_id BIGINT` → `pii (contact)`).
--   S3 — `date_of_birth` / `birthdate` routed to `contact` instead of
--        `demographic_protected`.
--   S4 — false negatives: `drivers_license`, `face_embedding`,
--        `insurance_id`, `age`, `patient_id`.
--
-- The companion regression test `tests/pii/test_pii_mockup_snapshot.py`
-- parses this file's column declarations and pins the desired
-- `(column → (sensitivity, categories))` mapping for every column,
-- so any future rule change that regresses against the smoke's
-- findings fails CI before merge.
--
-- The fixture is intentionally Postgres-flavoured but uses only
-- portable types (BIGINT, TEXT, DATE, INTEGER, BYTEA, REAL,
-- TIMESTAMPTZ, BOOLEAN) so it can also load against SQLite for
-- store-level tests if needed.

-- =====================================================================
-- Table 1 — `users`: contact, demographic, government_id, biometric.
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.users (
    id BIGSERIAL PRIMARY KEY,

    -- contact
    email TEXT NOT NULL UNIQUE,
    phone TEXT,
    full_name TEXT NOT NULL,
    home_address TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    zip_code TEXT,

    -- demographic_protected (post-fix; today tagged `contact` — S3)
    date_of_birth DATE,
    age INTEGER,                          -- S4: today `public`, should be demographic_protected
    gender TEXT,
    nationality TEXT,

    -- government_id
    ssn TEXT,
    tax_id TEXT,
    passport_number TEXT,
    drivers_license TEXT,                 -- S4: today `public` (rule has singular `driver_license` only)

    -- biometric
    fingerprint_hash TEXT,
    face_embedding BYTEA,                 -- S4: today `public`, should be biometric

    -- location
    last_known_latitude REAL,
    last_known_longitude REAL,

    -- non-PII operational columns
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- =====================================================================
-- Table 2 — `payments`: financial, payment_card, integer-FK back to users.
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.payments (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id),

    -- financial
    amount_cents BIGINT NOT NULL,

    -- payment_card
    credit_card_number TEXT,
    iban TEXT,
    bank_account_number TEXT,
    routing_number TEXT,

    -- S2 trigger: BIGINT FK whose name contains a contact keyword.
    -- `address` substring → today flagged `pii (contact)`. After fix,
    -- the integer-FK guard suppresses `contact` (FK-safe categories
    -- only).
    address_id BIGINT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =====================================================================
-- Table 3 — `health_records`: health, demographic, S4 health misses,
--                              integer-FK that DOES survive suppression.
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.health_records (
    id BIGSERIAL PRIMARY KEY,

    -- S4: today `public`; both should be `health`. Composite shape:
    -- `patient_id` is an integer FK but the FK shape carries PII even
    -- after the S2 integer-FK guard because `health` is FK-safe.
    patient_id BIGINT NOT NULL,
    insurance_id TEXT,

    -- health (already classified today)
    diagnosis_code TEXT,
    medication_list TEXT,
    blood_type TEXT,
    allergy TEXT,

    -- genetic
    genome_seq TEXT,

    -- behavioral (browsing in a health context — wearable telemetry)
    purchase_history TEXT,

    -- intentional false-positive-by-design (per docstring posture):
    -- `patient_satisfaction_score` matches `patient` → health after S4.
    -- The snapshot test pins this explicitly so the over-tagging is
    -- documented, not silent.
    patient_satisfaction_score INTEGER,

    visit_date DATE NOT NULL
);

-- =====================================================================
-- Table 4 — `auth_sessions`: credential, online_identifier.
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.auth_sessions (
    id BIGSERIAL PRIMARY KEY,

    -- S2 path that DOES survive integer-FK suppression: `user_id` has
    -- no PII keyword match today (no `user` rule), so it stays public
    -- with or without the guard.
    user_id BIGINT NOT NULL,

    -- credential
    session_token TEXT NOT NULL UNIQUE,
    api_key TEXT,
    refresh_token TEXT,
    password_hash TEXT,

    -- online_identifier — session_id IS FK-safe even when integer
    session_id BIGINT,
    cookie_id TEXT,
    device_id TEXT,
    ip_address TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

-- =====================================================================
-- Table 5 — `non_pii_things`: catalog table that should classify ENTIRELY
-- as public. This is the S1 repro surface: a TEXT column named
-- `product_name` (and friends) in a non-PII context.
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.non_pii_things (
    id BIGSERIAL PRIMARY KEY,

    -- S1 surfaces: today all flagged `pii (contact)` via the `_name`
    -- rule. After fix, suppressed by the `<denylist>_name` rule.
    product_name TEXT NOT NULL,
    brand_name TEXT,
    category_name TEXT,
    language_name TEXT,

    -- Non-PII columns the catalog typically carries.
    sku TEXT,
    quantity INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
