"""Regenerate `schemabrain/eval/fixtures/saas.sql` — the bundled v0.5.0
B2B SaaS control-plane demo database.

Why this exists: `saas` is the v0.5.0 default demo pack (e-commerce stays
bundled as the fallback). It tells a sharper firewall story than
e-commerce — it exercises ALL THREE catastrophic-leak legs
(credential / payment_card / government_id) instead of two, and stages
every demo beat S1-S7 incl. the §2.4 named-metric-over-a-catastrophic-
column refusal and the S7 Willison-Mirror injection beat.

Schema is FROZEN per `docs/internal/v0.5.0_saas_demo_db_design_spec.md`
(operator-signed 2026-05-31): 12 tables / 84 columns. The free-text
columns `users.bio` and `support_tickets.body` SHIP EMPTY (NULL) — the
Willison injection payload is applied at RECORD-TIME only, never in the
wheel (locked decision #8).

`saas.sql` targets Postgres ONLY — it is loaded via `docker run psql`
into the demo container (the `public.` schema prefix and `setval()` are
Postgres-only). SchemaBrain's SQLite surface is the metadata/audit
store, never a target for this seed.

Determinism comes from `random.Random(_SEED)`; re-running the script
produces byte-identical output.

Usage:
    python scripts/generate_saas_fixture.py > \
        schemabrain/eval/fixtures/saas.sql
"""

from __future__ import annotations

import datetime
import random
import sys

_SEED = 42

# Fixture version sentinel — bump when the schema changes so the wizard
# detects a stale demo container. Embedded in the SQL header comment.
_FIXTURE_VERSION = 1

# Time window for generated timestamps — 12 months ending 2026-05-15.
_ANCHOR = datetime.datetime(2025, 5, 16, tzinfo=datetime.UTC)
_DATE_RANGE_DAYS = 365

# ---------------------------------------------------------------------------
# Volume targets (per OQ-5 — lean bundled seed, < 90 KB). Pareto-skewed
# so leaderboard metrics (revenue by plan, users by workspace) rank
# clearly instead of returning flat trivia.
# ---------------------------------------------------------------------------
_N_WORKSPACES = 12
_SESSIONS_PER_USER = (0, 3)  # inclusive range; some users never logged in
_USAGE_EVENTS = 300
_SUPPORT_TICKETS = 30

# bcrypt's custom base64 alphabet — realistic-shaped credential fakes
# (RNG noise, never real secrets) so the `credential` PII tag looks
# natural in the demo.
_BCRYPT_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_TOKEN_ALPHABET = "0123456789abcdef"

# Recognizably-fake card tails (Stripe test values) + the four networks,
# so the benign `card_brand` companion column has visible variety.
_DEMO_CARD_LAST4 = ("4242", "4444", "0000", "0341", "1881", "0259", "6011", "0005")
_DEMO_CARD_BRANDS = ("visa", "mastercard", "amex", "discover")

_PLAN_TIERS = ("free", "pro", "enterprise")
_SLA_TIERS = ("standard", "priority", "dedicated")
_USER_ROLES = ("owner", "admin", "member", "member", "member")  # skewed to member
_API_SCOPES = ("read", "read", "write", "admin")
_EVENT_TYPES = ("api_call", "api_call", "api_call", "login", "export")
_SUBSCRIPTION_STATUSES = ("active", "active", "active", "trialing", "canceled")
_INVOICE_STATUSES = ("paid", "paid", "paid", "open", "void")
_TICKET_STATUSES = ("open", "pending", "closed", "closed")
_REGIONS = ("us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1")

# Preserved, stable demo identities (mirrors the ecommerce generator's
# Alice/Bob/Cara convention) so docs + smoke tests that reference these
# by name stay valid across regenerations.
_PRESERVED_WORKSPACES = [
    # (name, slug, region, plan_tier)
    ("Acme Corp", "acme", "us-east-1", "enterprise"),
    ("Globex", "globex", "eu-west-1", "pro"),
    ("Initech", "initech", "us-west-2", "pro"),
]

_WORKSPACE_NAMES = [
    ("Hooli", "hooli"),
    ("Stark Industries", "stark"),
    ("Wayne Enterprises", "wayne"),
    ("Soylent", "soylent"),
    ("Umbrella", "umbrella"),
    ("Cyberdyne", "cyberdyne"),
    ("Massive Dynamic", "massive-dynamic"),
    ("Wonka", "wonka"),
    ("Vandelay", "vandelay"),
]

# Plan catalog — 4 plans. (code, title, monthly_price_cents, included_seats, sla_tier)
_PLANS = [
    ("free", "Free", 0, 3, "standard"),
    ("pro", "Pro", 4900, 10, "priority"),
    ("scale", "Scale", 19900, 50, "priority"),
    ("enterprise", "Enterprise", 99900, 250, "dedicated"),
]

_FIRST_NAMES = [
    "Maya",
    "Liam",
    "Priya",
    "Noah",
    "Aisha",
    "Diego",
    "Yuki",
    "Ravi",
    "Zara",
    "Omar",
    "Mei",
    "Kai",
    "Sofia",
    "Hugo",
    "Anya",
    "Lars",
    "Imani",
    "Cleo",
    "Theo",
    "Ines",
    "Jonas",
    "Lila",
    "Marco",
    "Naomi",
    "Pablo",
    "Rin",
    "Sami",
    "Tara",
    "Uri",
    "Vera",
]
_LAST_NAMES = [
    "Singh",
    "Garcia",
    "Tanaka",
    "Murphy",
    "Okonkwo",
    "Hassan",
    "Petrov",
    "Wong",
    "Sato",
    "Khan",
    "Diallo",
    "Romano",
    "Kowalski",
    "Ahmed",
    "Schmidt",
    "Andersen",
    "Lopez",
    "Park",
]
_TICKET_SUBJECTS = [
    "Cannot reset my password",
    "Billing question about last invoice",
    "API rate limit increase",
    "SSO configuration help",
    "Export is timing out",
    "Add seats to our plan",
    "Webhook deliveries failing",
    "Downgrade to Pro",
    "Question about data retention",
    "Need a signed BAA",
]


def _fake_bcrypt(rng: random.Random) -> str:
    return "$2b$12$" + "".join(rng.choices(_BCRYPT_ALPHABET, k=53))


def _fake_token(rng: random.Random, prefix: str, length: int) -> str:
    return prefix + "".join(rng.choices(_TOKEN_ALPHABET, k=length))


def _fake_tax_id(rng: random.Random) -> str:
    # EIN-shaped fake (NN-NNNNNNN); recognizably synthetic.
    return f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}"


def _fake_national_id(rng: random.Random) -> str:
    # SSN-shaped fake (NNN-NN-NNNN).
    return f"{rng.randint(100, 899)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"


def _ts(rng: random.Random, *, start_day: int = 0) -> str:
    """Random TIMESTAMPTZ literal inside the trailing window."""
    days = rng.randint(start_day, _DATE_RANGE_DAYS)
    when = _ANCHOR + datetime.timedelta(
        days=days, hours=rng.randint(0, 23), minutes=rng.randint(0, 59), seconds=rng.randint(0, 59)
    )
    return when.strftime("%Y-%m-%d %H:%M:%S+00")


def _ts_after(base_iso: str, rng: random.Random, max_days: int) -> str:
    base = datetime.datetime.strptime(base_iso, "%Y-%m-%d %H:%M:%S+00").replace(tzinfo=datetime.UTC)
    when = base + datetime.timedelta(days=rng.randint(1, max_days), hours=rng.randint(0, 23))
    return when.strftime("%Y-%m-%d %H:%M:%S+00")


# ---------------------------------------------------------------------------
# Row generators — each returns a list of tuples in CREATE-TABLE column
# order. The generic emitter formats them.
# ---------------------------------------------------------------------------
def _gen_workspaces(rng: random.Random) -> list[tuple]:
    rows: list[tuple] = []
    pool = list(_PRESERVED_WORKSPACES) + [
        (name, slug, rng.choice(_REGIONS), rng.choice(_PLAN_TIERS))
        for name, slug in _WORKSPACE_NAMES
    ]
    for wid, (name, slug, region, tier) in enumerate(pool[:_N_WORKSPACES], start=1):
        seats = {
            "free": rng.randint(1, 3),
            "pro": rng.randint(4, 12),
            "enterprise": rng.randint(40, 200),
        }[tier]
        rows.append((wid, name, slug, region, tier, seats, _ts(rng)))
    return rows


def _gen_plans() -> list[tuple]:
    rows: list[tuple] = []
    for pid, (code, title, price, seats, sla) in enumerate(_PLANS, start=1):
        is_active = code != "scale" or True  # all active in the seed
        rows.append((pid, code, title, price, seats, sla, is_active))
    return rows


def _gen_users(rng: random.Random, workspaces: list[tuple]) -> list[tuple]:
    """~5 users per workspace, Pareto-skewed (enterprise workspaces larger)."""
    rows: list[tuple] = []
    used_emails: set[str] = set()
    uid = 1
    for ws in workspaces:
        wid, _name, slug, _region, tier, _seats, _created = ws
        n_users = {
            "free": rng.randint(1, 3),
            "pro": rng.randint(3, 7),
            "enterprise": rng.randint(8, 14),
        }[tier]
        for i in range(n_users):
            first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
            email = f"{first.lower()}.{last.lower()}{uid}@{slug}.example.com"
            if email in used_emails:
                continue
            used_emails.add(email)
            role = "owner" if i == 0 else rng.choice(_USER_ROLES)
            # bio SHIPS NULL — S7 record-time vector (locked #8).
            rows.append(
                (uid, wid, email, f"{first} {last}", _fake_bcrypt(rng), role, None, _ts(rng))
            )
            uid += 1
    return rows


def _gen_subscriptions(
    rng: random.Random, workspaces: list[tuple], plans: list[tuple]
) -> list[tuple]:
    """One current subscription per workspace + some historical/canceled."""
    plan_by_code = {p[1]: p for p in plans}
    rows: list[tuple] = []
    sid = 1
    for ws in workspaces:
        wid, _n, _s, _r, tier, seats, _c = ws
        plan = plan_by_code.get(
            "enterprise" if tier == "enterprise" else ("pro" if tier == "pro" else "free")
        )
        mrr = plan[3] * max(1, seats // max(1, plan[4]) + 1)
        started = _ts(rng, start_day=0)
        status = rng.choice(_SUBSCRIPTION_STATUSES)
        canceled = _ts_after(started, rng, 200) if status == "canceled" else None
        trial_ends = _ts_after(started, rng, 30) if status == "trialing" else None
        rows.append((sid, wid, plan[0], status, mrr, started, canceled, trial_ends))
        sid += 1
        # ~1/3 of workspaces have a prior (canceled) subscription.
        if rng.random() < 0.34:
            prior_started = _ts(rng, start_day=0)
            rows.append(
                (
                    sid,
                    wid,
                    plan_by_code["free"][0],
                    "canceled",
                    0,
                    prior_started,
                    _ts_after(prior_started, rng, 120),
                    None,
                )
            )
            sid += 1
    return rows


def _gen_subscription_items(
    rng: random.Random, subscriptions: list[tuple], plans: list[tuple]
) -> list[tuple]:
    plan_by_id = {p[0]: p for p in plans}
    rows: list[tuple] = []
    iid = 1
    for sub in subscriptions:
        sid, _wid, plan_id, status, _mrr, _st, _ca, _te = sub
        n_items = 1 if status == "canceled" else rng.randint(1, 3)
        plan = plan_by_id[plan_id]
        for _ in range(n_items):
            seats = rng.randint(1, max(2, plan[4] // 2))
            unit_price = plan[3] if plan[3] > 0 else rng.choice((0, 0, 900))
            rows.append((iid, sid, plan_id, seats, unit_price))
            iid += 1
    return rows


def _gen_payment_methods(rng: random.Random, workspaces: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    pid = 1
    for ws in workspaces:
        wid, _n, _s, _r, tier, _seats, _c = ws
        if tier == "free" and rng.random() < 0.5:
            continue  # some free workspaces have no card
        rows.append(
            (
                pid,
                wid,
                rng.choice(_DEMO_CARD_LAST4),
                rng.choice(_DEMO_CARD_BRANDS),
                rng.randint(1, 12),
                rng.randint(2026, 2031),
                True,
                _ts(rng),
            )
        )
        pid += 1
        if rng.random() < 0.35:  # second card on file
            rows.append(
                (
                    pid,
                    wid,
                    rng.choice(_DEMO_CARD_LAST4),
                    rng.choice(_DEMO_CARD_BRANDS),
                    rng.randint(1, 12),
                    rng.randint(2026, 2031),
                    False,
                    _ts(rng),
                )
            )
            pid += 1
    return rows


def _gen_invoices(
    rng: random.Random, workspaces: list[tuple], subscriptions: list[tuple]
) -> list[tuple]:
    """~12 months of invoices per active workspace — the revenue leaderboard source."""
    subs_by_ws: dict[int, list[tuple]] = {}
    for sub in subscriptions:
        subs_by_ws.setdefault(sub[1], []).append(sub)
    rows: list[tuple] = []
    inv_id = 1
    for ws in workspaces:
        wid, _n, _s, _r, tier, _seats, _c = ws
        ws_subs = subs_by_ws.get(wid, [])
        if not ws_subs:
            continue
        n_invoices = {
            "free": rng.randint(0, 3),
            "pro": rng.randint(8, 14),
            "enterprise": rng.randint(12, 20),
        }[tier]
        for _ in range(n_invoices):
            sub = rng.choice(ws_subs)
            mrr = sub[4]
            amount = max(0, mrr + rng.randint(-500, 5000))
            status = rng.choice(_INVOICE_STATUSES)
            issued = _ts(rng)
            paid = _ts_after(issued, rng, 20) if status == "paid" else None
            rows.append((inv_id, wid, sub[0], f"INV-{inv_id:05d}", status, amount, issued, paid))
            inv_id += 1
    return rows


def _gen_api_keys(rng: random.Random, workspaces: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    kid = 1
    for ws in workspaces:
        wid, _n, _s, _r, tier, _seats, _c = ws
        n_keys = {
            "free": rng.randint(0, 1),
            "pro": rng.randint(1, 3),
            "enterprise": rng.randint(2, 5),
        }[tier]
        for _ in range(n_keys):
            created = _ts(rng)
            revoked = _ts_after(created, rng, 100) if rng.random() < 0.15 else None
            rows.append(
                (
                    kid,
                    wid,
                    _fake_token(rng, "sk_live_", 8),
                    _fake_bcrypt(rng),
                    rng.choice(_API_SCOPES),
                    created,
                    revoked,
                )
            )
            kid += 1
    return rows


def _gen_sessions(rng: random.Random, users: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    sess_id = 1
    for user in users:
        uid = user[0]
        for _ in range(rng.randint(*_SESSIONS_PER_USER)):
            created = _ts(rng)
            ip = f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
            rows.append(
                (
                    sess_id,
                    uid,
                    _fake_token(rng, "sess_", 32),
                    ip,
                    created,
                    _ts_after(created, rng, 14),
                )
            )
            sess_id += 1
    return rows


def _gen_usage_events(rng: random.Random, workspaces: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    wids = [w[0] for w in workspaces]
    for ev_id in range(1, _USAGE_EVENTS + 1):
        occurred = _ts(rng)
        rows.append(
            (
                ev_id,
                rng.choice(wids),
                rng.choice(_EVENT_TYPES),
                rng.randint(1, 500),
                occurred,
                _ts_after(occurred, rng, 1),
            )
        )
    return rows


def _gen_support_tickets(
    rng: random.Random, workspaces: list[tuple], users: list[tuple]
) -> list[tuple]:
    users_by_ws: dict[int, list[int]] = {}
    for u in users:
        users_by_ws.setdefault(u[1], []).append(u[0])
    rows: list[tuple] = []
    wids = [w[0] for w in workspaces]
    for tid in range(1, _SUPPORT_TICKETS + 1):
        wid = rng.choice(wids)
        ws_users = users_by_ws.get(wid) or [None]
        uid = rng.choice(ws_users)
        # body SHIPS NULL — S7 record-time vector (locked #8).
        rows.append(
            (
                tid,
                wid,
                uid,
                rng.choice(_TICKET_SUBJECTS),
                None,
                rng.choice(_TICKET_STATUSES),
                _ts(rng),
            )
        )
    return rows


def _gen_billing_profiles(rng: random.Random, workspaces: list[tuple]) -> list[tuple]:
    rows: list[tuple] = []
    for bid, ws in enumerate(workspaces, start=1):
        wid, name, _s, region, _tier, _seats, _c = ws
        country = {
            "us-east-1": "US",
            "us-west-2": "US",
            "eu-west-1": "DE",
            "ap-southeast-1": "SG",
        }.get(region, "US")
        national = (
            _fake_national_id(rng) if rng.random() < 0.4 else None
        )  # sole-proprietor accounts
        rows.append((bid, wid, f"{name}, Inc.", _fake_tax_id(rng), national, country, _ts(rng)))
    return rows


# ---------------------------------------------------------------------------
# Generic emitter
# ---------------------------------------------------------------------------
def _sql_value(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def _emit(table: str, columns: tuple[str, ...], rows: list[tuple], *, id_seq: bool = True) -> str:
    if not rows:
        return f"-- (no rows for public.{table})"
    cols = ", ".join(columns)
    lines = [f"INSERT INTO public.{table} ({cols}) VALUES"]
    lines.append(",\n".join("    (" + ", ".join(_sql_value(v) for v in row) + ")" for row in rows))
    lines.append("ON CONFLICT DO NOTHING;")
    if id_seq:
        lines.append(f"SELECT setval('public.{table}_id_seq', {max(r[0] for r in rows)}, true);")
    return "\n".join(lines)


_HEADER = f"""\
-- Bundled SchemaBrain v0.5.0 DEFAULT demo database: a B2B SaaS control plane.
--
-- SaaS was chosen as the default bundled domain because its tables
-- (workspaces / users / subscriptions / invoices / api_keys) are
-- universally legible AND exercise all three catastrophic-leak PII legs
-- (credential, payment_card, government_id). SchemaBrain is a generic
-- semantic layer + SQL firewall for ANY production database; e-commerce
-- stays bundled as the fallback pack (`fixture-path ecommerce.sql`).
--
-- SCHEMABRAIN_FIXTURE_VERSION: {_FIXTURE_VERSION}
-- ^^ Wizard reads this from a running container to detect drift. Bump
-- when the schema changes so existing `sb-demo-pg` containers prompt
-- the user to recreate. See `schemabrain/setup/setup_stage.py`.
--
-- Loads via Postgres ONLY (the `public.` schema prefix + `setval()` are
-- Postgres-only):
--   psql "$DATABASE_URL" -f schemabrain/eval/fixtures/saas.sql
--   schemabrain index "$DATABASE_URL" --store-path ./schemabrain.db
--   schemabrain eval --source "$DATABASE_URL" --store-path ./schemabrain.db
--
-- Catastrophic-leak columns exist so the default `--pii-block` policy
-- fires on a natural agent query on the first interaction:
--   credential    : users.password_hash, api_keys.api_key_hash, sessions.session_token
--   payment_card  : payment_methods.card_number_last4
--   government_id : billing_profiles.tax_id, billing_profiles.national_id
-- Values are RNG noise (bcrypt-shaped hashes, recognizably-fake card
-- tails + EIN/SSN-shaped ids). NO `cvv`/`cvc` is stored (PCI-correct).
--
-- The free-text columns `users.bio` and `support_tickets.body` SHIP
-- EMPTY (NULL). The S7 Willison-Mirror injection payload is applied at
-- RECORD-TIME only and never appears in this seed.
--
-- Seed rows are generated by `scripts/generate_saas_fixture.py` with a
-- fixed RNG seed so reruns are byte-identical. If you change a table or
-- column name here, regenerate the matching golden set in
-- `schemabrain/eval/golden_sets/saas.json` and re-pin the firewall corpus.

CREATE TABLE IF NOT EXISTS public.workspaces (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    region TEXT NOT NULL,
    plan_tier TEXT NOT NULL DEFAULT 'free',
    seat_count INTEGER NOT NULL CHECK (seat_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.plans (
    id BIGSERIAL PRIMARY KEY,
    plan_code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    monthly_price_cents INTEGER NOT NULL CHECK (monthly_price_cents >= 0),
    included_seats INTEGER NOT NULL CHECK (included_seats >= 0),
    sla_tier TEXT NOT NULL DEFAULT 'standard',
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- `users.password_hash` is a catastrophic-leak (credential) column. The
-- ADD COLUMN IF NOT EXISTS guard is belt-and-suspenders for direct
-- `psql -f` reruns against a pre-existing container. `bio` ships NULL
-- (S7 record-time injection vector).
CREATE TABLE IF NOT EXISTS public.users (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    email TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    bio TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT '$2b$12$placeholder';
ALTER TABLE public.users
    ALTER COLUMN password_hash DROP DEFAULT;

-- Dual-timestamp lifecycle (S5 ambiguous-time beat): started_at is
-- intentionally the FIRST timestamp column so the firewall lists it
-- first in recovery.suggested_args.
CREATE TABLE IF NOT EXISTS public.subscriptions (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    plan_id BIGINT NOT NULL REFERENCES public.plans(id),
    status TEXT NOT NULL DEFAULT 'trialing',
    mrr_cents INTEGER NOT NULL CHECK (mrr_cents >= 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    canceled_at TIMESTAMPTZ,
    trial_ends_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.subscription_items (
    id BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT NOT NULL REFERENCES public.subscriptions(id),
    plan_id BIGINT NOT NULL REFERENCES public.plans(id),
    seats INTEGER NOT NULL CHECK (seats > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);

-- `card_number_last4` is a catastrophic-leak (payment_card) column.
-- Tokenized last4 + brand + exp only — NO cvv/cvc (PCI anti-pattern).
CREATE TABLE IF NOT EXISTS public.payment_methods (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    card_number_last4 TEXT NOT NULL CHECK (length(card_number_last4) = 4),
    card_brand TEXT NOT NULL,
    card_exp_month INTEGER NOT NULL CHECK (card_exp_month BETWEEN 1 AND 12),
    card_exp_year INTEGER NOT NULL CHECK (card_exp_year BETWEEN 2025 AND 2099),
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.invoices (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    subscription_id BIGINT REFERENCES public.subscriptions(id),
    invoice_number TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'open',
    invoice_total_cents INTEGER NOT NULL CHECK (invoice_total_cents >= 0),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at TIMESTAMPTZ
);

-- `api_key_hash` is a catastrophic-leak (credential) column. `key_prefix`
-- is the benign displayable companion (classifies public).
CREATE TABLE IF NOT EXISTS public.api_keys (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    key_prefix TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'read',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

-- `sessions.user_id` is the canonical incoming-FK-to-catastrophic case
-- (parent `users` holds password_hash). `session_token` is itself a
-- catastrophic-leak (credential) column.
CREATE TABLE IF NOT EXISTS public.sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id),
    session_token TEXT NOT NULL,
    last_login_ip TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.usage_events (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    event_type TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `support_tickets.body` ships NULL (S7 record-time injection vector).
CREATE TABLE IF NOT EXISTS public.support_tickets (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL REFERENCES public.workspaces(id),
    user_id BIGINT REFERENCES public.users(id),
    subject TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `tax_id` + `national_id` are catastrophic-leak (government_id) columns.
-- `legal_entity` is the benign company string (classifies public).
CREATE TABLE IF NOT EXISTS public.billing_profiles (
    id BIGSERIAL PRIMARY KEY,
    workspace_id BIGINT NOT NULL UNIQUE REFERENCES public.workspaces(id),
    legal_entity TEXT NOT NULL,
    tax_id TEXT NOT NULL,
    national_id TEXT,
    country_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def main() -> int:
    rng = random.Random(_SEED)
    workspaces = _gen_workspaces(rng)
    plans = _gen_plans()
    users = _gen_users(rng, workspaces)
    subscriptions = _gen_subscriptions(rng, workspaces, plans)
    subscription_items = _gen_subscription_items(rng, subscriptions, plans)
    payment_methods = _gen_payment_methods(rng, workspaces)
    invoices = _gen_invoices(rng, workspaces, subscriptions)
    api_keys = _gen_api_keys(rng, workspaces)
    sessions = _gen_sessions(rng, users)
    usage_events = _gen_usage_events(rng, workspaces)
    support_tickets = _gen_support_tickets(rng, workspaces, users)
    billing_profiles = _gen_billing_profiles(rng, workspaces)

    sections = [
        _HEADER,
        f"-- Seeded by scripts/generate_saas_fixture.py with random.Random({_SEED});"
        " reruns are byte-identical.",
        "",
        _emit(
            "workspaces",
            ("id", "name", "slug", "region", "plan_tier", "seat_count", "created_at"),
            workspaces,
        ),
        "",
        _emit(
            "plans",
            (
                "id",
                "plan_code",
                "title",
                "monthly_price_cents",
                "included_seats",
                "sla_tier",
                "is_active",
            ),
            plans,
        ),
        "",
        _emit(
            "users",
            (
                "id",
                "workspace_id",
                "email",
                "full_name",
                "password_hash",
                "role",
                "bio",
                "created_at",
            ),
            users,
        ),
        "",
        _emit(
            "subscriptions",
            (
                "id",
                "workspace_id",
                "plan_id",
                "status",
                "mrr_cents",
                "started_at",
                "canceled_at",
                "trial_ends_at",
            ),
            subscriptions,
        ),
        "",
        _emit(
            "subscription_items",
            ("id", "subscription_id", "plan_id", "seats", "unit_price_cents"),
            subscription_items,
        ),
        "",
        _emit(
            "payment_methods",
            (
                "id",
                "workspace_id",
                "card_number_last4",
                "card_brand",
                "card_exp_month",
                "card_exp_year",
                "is_default",
                "created_at",
            ),
            payment_methods,
        ),
        "",
        _emit(
            "invoices",
            (
                "id",
                "workspace_id",
                "subscription_id",
                "invoice_number",
                "status",
                "invoice_total_cents",
                "issued_at",
                "paid_at",
            ),
            invoices,
        ),
        "",
        _emit(
            "api_keys",
            (
                "id",
                "workspace_id",
                "key_prefix",
                "api_key_hash",
                "scope",
                "created_at",
                "revoked_at",
            ),
            api_keys,
        ),
        "",
        _emit(
            "sessions",
            ("id", "user_id", "session_token", "last_login_ip", "created_at", "expires_at"),
            sessions,
        ),
        "",
        _emit(
            "usage_events",
            ("id", "workspace_id", "event_type", "quantity", "occurred_at", "recorded_at"),
            usage_events,
        ),
        "",
        _emit(
            "support_tickets",
            ("id", "workspace_id", "user_id", "subject", "body", "status", "created_at"),
            support_tickets,
        ),
        "",
        _emit(
            "billing_profiles",
            (
                "id",
                "workspace_id",
                "legal_entity",
                "tax_id",
                "national_id",
                "country_code",
                "created_at",
            ),
            billing_profiles,
        ),
        "",
    ]
    sys.stdout.write("\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
