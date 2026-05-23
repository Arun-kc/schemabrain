"""Regenerate `schemabrain/eval/fixtures/ecommerce.sql` with a demoable
row volume + skewed purchase distribution.

Why this exists: the original fixture had 3 users / 3 orders, which made
queries like `get_metric(name="total_items_sold", group_by=["user.email"],
limit=5)` return only 3 rows — too few to demonstrate ranking. PR-6h.3
(Gap #4 from the 2026-05-21 smoke) widened it to ~80 users with a
Pareto-shaped order distribution so "find users who bought the most
products" returns a clear, interpretable leaderboard.

Schema is UNCHANGED — only the seeded data volume + shape changes.
Determinism comes from `random.Random(_SEED)`; re-running the script
produces byte-identical output.

Usage:
    python scripts/generate_ecommerce_fixture.py > \
        schemabrain/eval/fixtures/ecommerce.sql

The first three users (Alice / Bob / Cara) and the two original
products (Running Shoes / Wireless Headphones) are preserved at the
same IDs so downstream tests + docs that reference them by name stay
valid.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass

_SEED = 42

# Date range for `placed_at` — 12 months ending 2026-05-15.
_DATE_RANGE_DAYS = 365
_DATE_END_ISO = "2026-05-15T23:59:59+00:00"

# User-count tiers. Sum is the total user-count target (~80 users).
_TIER_POWER_USERS = 5  # 8-15 orders each
_TIER_REGULAR_USERS = 15  # 3-6 orders each
_TIER_OCCASIONAL_USERS = 30  # 1-2 orders each
_TIER_DORMANT_USERS = 30  # 0 orders

# Order-volume range per tier (inclusive).
_ORDERS_POWER = (8, 15)
_ORDERS_REGULAR = (3, 6)
_ORDERS_OCCASIONAL = (1, 2)

# Items per order — Pareto-like skew toward small baskets.
_ITEMS_PER_ORDER_WEIGHTS = [(1, 0.40), (2, 0.30), (3, 0.18), (4, 0.08), (5, 0.04)]


@dataclass(frozen=True)
class _Product:
    id: int
    sku: str
    name: str
    description: str
    price_cents: int


@dataclass(frozen=True)
class _Category:
    id: int
    name: str
    parent_id: int | None


# Preserved from the original fixture so docstring + redactor + smoke
# tests that reference these names by string still pass.
_PRESERVED_USERS: list[tuple[int, str, str]] = [
    (1, "alice@example.com", "Alice Patel"),
    (2, "bob@example.com", "Bob Mendez"),
    (3, "cara@example.com", "Cara Liu"),
]

# Preserved from the original fixture (same IDs).
_PRESERVED_PRODUCTS: list[_Product] = [
    _Product(1, "SKU-001", "Running Shoes", "Lightweight trail runners", 8999),
    _Product(2, "SKU-002", "Wireless Headphones", "Over-ear bluetooth", 14999),
]

_CATEGORIES: list[_Category] = [
    _Category(1, "Apparel", None),
    _Category(2, "Shoes", 1),
    _Category(3, "Electronics", None),
    _Category(4, "Audio", 3),
    _Category(5, "Home", None),
    _Category(6, "Kitchen", 5),
]

# Additional products beyond the two preserved ones. Category mapping
# below uses the same IDs.
_NEW_PRODUCTS: list[tuple[_Product, list[int]]] = [
    (_Product(3, "SKU-003", "Trail Sneakers", "Grip-soled trail shoes", 11999), [2]),
    (_Product(4, "SKU-004", "Studio Headphones", "Wired studio reference", 19999), [4, 3]),
    (_Product(5, "SKU-005", "Yoga Pants", "Four-way stretch", 5999), [1]),
    (_Product(6, "SKU-006", "Rain Jacket", "Packable waterproof shell", 12999), [1]),
    (_Product(7, "SKU-007", "Smart Watch", "Health + fitness tracker", 24999), [3]),
    (_Product(8, "SKU-008", "Bluetooth Speaker", "Outdoor portable", 7999), [4, 3]),
    (_Product(9, "SKU-009", "Coffee Grinder", "Burr conical", 8499), [6, 5]),
    (_Product(10, "SKU-010", "Cast Iron Skillet", "12-inch pre-seasoned", 4999), [6, 5]),
    (_Product(11, "SKU-011", "Throw Blanket", "Cable knit wool blend", 6499), [5]),
    (_Product(12, "SKU-012", "Desk Lamp", "Adjustable LED", 4499), [5]),
    (_Product(13, "SKU-013", "Hiking Boots", "Mid-cut leather", 17999), [2]),
    (_Product(14, "SKU-014", "Wool Socks", "Merino crew (3-pack)", 2499), [1]),
    (_Product(15, "SKU-015", "Running Belt", "Reflective phone holder", 1999), [1, 2]),
    (_Product(16, "SKU-016", "Earbuds Pro", "Active noise cancelling", 17999), [4, 3]),
    (_Product(17, "SKU-017", "Espresso Cups", "Set of 6 ceramic", 3499), [6, 5]),
    (_Product(18, "SKU-018", "Chef Knife", "8-inch Damascus steel", 13999), [6, 5]),
]

# Plausible-looking name pool for generated users — keep the pool
# small + reused; the point is volume, not identity diversity.
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
    "Wren",
    "Xander",
    "Yara",
    "Zane",
    "Asha",
    "Bram",
    "Chen",
    "Dara",
    "Emil",
    "Fariha",
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
    "Iyer",
    "Bauer",
    "Nakamura",
    "Marquez",
    "Brennan",
    "Tran",
    "Fernandes",
    "Kapoor",
]

# Plausible address lines for generated addresses — keep small,
# duplicates across orders are intentional.
_STREETS = [
    "12 Maple Street",
    "88 Birch Lane",
    "44 Cedar Ave",
    "201 Oak Drive",
    "67 Pine Road",
    "5 Spruce Place",
    "129 Elm Court",
    "333 Willow Way",
    "9 Magnolia Blvd",
    "274 Sycamore St",
]

_CITIES_COUNTRIES = [
    ("Brooklyn", "US"),
    ("Austin", "US"),
    ("Toronto", "CA"),
    ("London", "GB"),
    ("Berlin", "DE"),
    ("Mumbai", "IN"),
    ("Tokyo", "JP"),
    ("Lagos", "NG"),
]


def _generate_users(rng: random.Random) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = list(_PRESERVED_USERS)
    total = _TIER_POWER_USERS + _TIER_REGULAR_USERS + _TIER_OCCASIONAL_USERS + _TIER_DORMANT_USERS
    next_id = len(_PRESERVED_USERS) + 1
    used_emails: set[str] = {row[1] for row in rows}
    while next_id <= total + len(_PRESERVED_USERS) - len(_PRESERVED_USERS):
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        email = f"{first.lower()}.{last.lower()}{next_id}@example.com"
        if email in used_emails:
            continue
        used_emails.add(email)
        rows.append((next_id, email, f"{first} {last}"))
        next_id += 1
    return rows


def _generate_addresses(rng: random.Random) -> list[tuple[int, str, str, str]]:
    # 30 addresses; first 2 preserved for backwards-compat-ish ordering.
    preserved = [
        (1, "101 First Street", "Brooklyn", "US"),
        (2, "42 Second Avenue", "Brooklyn", "US"),
    ]
    rows: list[tuple[int, str, str, str]] = list(preserved)
    for idx in range(3, 31):
        street = rng.choice(_STREETS)
        city, country = rng.choice(_CITIES_COUNTRIES)
        rows.append((idx, street, city, country))
    return rows


def _all_products() -> list[_Product]:
    return list(_PRESERVED_PRODUCTS) + [p for p, _ in _NEW_PRODUCTS]


def _product_categories() -> list[tuple[int, int]]:
    """Each product belongs to 1+ categories. Preserved mapping:
    SKU-001→Shoes (2), SKU-002→Electronics (3). Then NEW_PRODUCTS' own
    mapping from the data table.
    """
    pairs: list[tuple[int, int]] = [(1, 2), (2, 3)]
    for product, category_ids in _NEW_PRODUCTS:
        for cat_id in category_ids:
            pairs.append((product.id, cat_id))
    return pairs


def _weighted_choice(rng: random.Random, weights: list[tuple[int, float]]) -> int:
    r = rng.random()
    cumulative = 0.0
    for value, weight in weights:
        cumulative += weight
        if r < cumulative:
            return value
    return weights[-1][0]


def _format_timestamp(rng: random.Random) -> str:
    """Random ISO timestamp inside the trailing 12 months.

    Returned as a SQL literal Postgres can interpret as TIMESTAMPTZ.
    """
    # 2025-05-16 00:00:00 UTC + uniform random seconds within range.
    days = rng.randint(0, _DATE_RANGE_DAYS)
    hours = rng.randint(0, 23)
    minutes = rng.randint(0, 59)
    seconds = rng.randint(0, 59)
    # Anchor: 2025-05-16 ≈ day 0; emit using Postgres-friendly literal.
    import datetime

    anchor = datetime.datetime(2025, 5, 16, tzinfo=datetime.UTC)
    when = anchor + datetime.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return when.strftime("%Y-%m-%d %H:%M:%S+00")


def _generate_orders_and_items(
    rng: random.Random,
    users: list[tuple[int, str, str]],
    products: list[_Product],
    address_count: int,
) -> tuple[list[tuple], list[tuple]]:
    """Return (orders, order_items) row tuples.

    Distribution:
      - 5 users x 8-15 orders   = power users (most-ranked)
      - 15 users x 3-6 orders   = regular
      - 30 users x 1-2 orders   = occasional
      - 30 users x 0 orders     = dormant (forces COUNT(DISTINCT) skew)

    Alice (id=1) is the top power user by construction so the README's
    deterministic-walkthrough example has a stable winner.
    """
    user_ids = [u[0] for u in users]
    # First 3 preserved users land in the power tier so Alice/Bob/Cara
    # are still the visible winners; remaining tier seats filled by
    # round-robin from the generated pool.
    preserved_ids = [u[0] for u in users if u[0] <= len(_PRESERVED_USERS)]
    generated_ids = [u_id for u_id in user_ids if u_id not in preserved_ids]
    rng.shuffle(generated_ids)

    def _pop_n(n: int) -> list[int]:
        out = generated_ids[:n]
        del generated_ids[:n]
        return out

    power_remaining = _TIER_POWER_USERS - len(preserved_ids)
    power_users = preserved_ids + _pop_n(max(0, power_remaining))
    regular_users = _pop_n(_TIER_REGULAR_USERS)
    occasional_users = _pop_n(_TIER_OCCASIONAL_USERS)
    # Remainder are dormant — they have an account but never order.

    orders: list[tuple] = []
    items: list[tuple] = []
    order_id = 1
    item_id = 1
    statuses = ("fulfilled", "fulfilled", "fulfilled", "fulfilled", "shipped", "pending")

    def _emit_orders_for(user_id: int, count: int) -> None:
        nonlocal order_id, item_id
        # Make the FIRST power user (Alice, id=1) the top buyer
        # deterministically — push her order count to the high end.
        if user_id == 1:
            count = _ORDERS_POWER[1]
        for _ in range(count):
            billing_addr = rng.randint(1, address_count)
            shipping_addr = rng.randint(1, address_count)
            placed_at = _format_timestamp(rng)
            n_items = _weighted_choice(rng, _ITEMS_PER_ORDER_WEIGHTS)
            chosen_products = rng.sample(products, k=min(n_items, len(products)))
            order_total = 0
            line_rows: list[tuple] = []
            for product in chosen_products:
                quantity = _weighted_choice(rng, _ITEMS_PER_ORDER_WEIGHTS)
                unit_price = product.price_cents
                line_rows.append((item_id, order_id, product.id, quantity, unit_price))
                order_total += quantity * unit_price
                item_id += 1
            status = rng.choice(statuses)
            orders.append(
                (
                    order_id,
                    user_id,
                    billing_addr,
                    shipping_addr,
                    status,
                    order_total,
                    placed_at,
                )
            )
            items.extend(line_rows)
            order_id += 1

    for u in power_users:
        _emit_orders_for(u, rng.randint(*_ORDERS_POWER))
    for u in regular_users:
        _emit_orders_for(u, rng.randint(*_ORDERS_REGULAR))
    for u in occasional_users:
        _emit_orders_for(u, rng.randint(*_ORDERS_OCCASIONAL))

    return orders, items


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _emit_users(rows: list[tuple[int, str, str]]) -> str:
    lines = ["INSERT INTO public.users (id, email, full_name) VALUES"]
    formatted = [f"    ({r[0]}, '{_sql_escape(r[1])}', '{_sql_escape(r[2])}')" for r in rows]
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    lines.append(f"SELECT setval('public.users_id_seq', {max(r[0] for r in rows)}, true);")
    return "\n".join(lines)


def _emit_categories() -> str:
    lines = ["INSERT INTO public.categories (id, name, parent_id) VALUES"]
    formatted: list[str] = []
    for cat in _CATEGORIES:
        parent = "NULL" if cat.parent_id is None else str(cat.parent_id)
        formatted.append(f"    ({cat.id}, '{_sql_escape(cat.name)}', {parent})")
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    lines.append(
        f"SELECT setval('public.categories_id_seq', {max(c.id for c in _CATEGORIES)}, true);"
    )
    return "\n".join(lines)


def _emit_products(products: list[_Product]) -> str:
    lines = ["INSERT INTO public.products (id, sku, name, description, price_cents) VALUES"]
    formatted = [
        f"    ({p.id}, '{_sql_escape(p.sku)}', '{_sql_escape(p.name)}',"
        f" '{_sql_escape(p.description)}', {p.price_cents})"
        for p in products
    ]
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    lines.append(f"SELECT setval('public.products_id_seq', {max(p.id for p in products)}, true);")
    return "\n".join(lines)


def _emit_product_categories(pairs: list[tuple[int, int]]) -> str:
    lines = ["INSERT INTO public.product_categories (product_id, category_id) VALUES"]
    formatted = [f"    ({pair[0]}, {pair[1]})" for pair in pairs]
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    return "\n".join(lines)


def _emit_addresses(rows: list[tuple[int, str, str, str]]) -> str:
    lines = ["INSERT INTO public.addresses (id, line1, city, country) VALUES"]
    formatted = [
        f"    ({r[0]}, '{_sql_escape(r[1])}', '{_sql_escape(r[2])}', '{_sql_escape(r[3])}')"
        for r in rows
    ]
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    lines.append(f"SELECT setval('public.addresses_id_seq', {max(r[0] for r in rows)}, true);")
    return "\n".join(lines)


def _emit_orders(rows: list[tuple]) -> str:
    lines = [
        "INSERT INTO public.orders (id, user_id, billing_address_id,"
        " shipping_address_id, status, total_cents, placed_at) VALUES"
    ]
    formatted = [
        f"    ({r[0]}, {r[1]}, {r[2]}, {r[3]}, '{_sql_escape(r[4])}', {r[5]}, '{r[6]}')"
        for r in rows
    ]
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    lines.append(f"SELECT setval('public.orders_id_seq', {max(r[0] for r in rows)}, true);")
    return "\n".join(lines)


def _emit_order_items(rows: list[tuple]) -> str:
    lines = [
        "INSERT INTO public.order_items (id, order_id, product_id, quantity,"
        " unit_price_cents) VALUES"
    ]
    formatted = [f"    ({r[0]}, {r[1]}, {r[2]}, {r[3]}, {r[4]})" for r in rows]
    lines.append(",\n".join(formatted))
    lines.append("ON CONFLICT DO NOTHING;")
    lines.append(f"SELECT setval('public.order_items_id_seq', {max(r[0] for r in rows)}, true);")
    return "\n".join(lines)


_HEADER = """\
-- ONE example fixture for the bundled Schema Brain eval set.
--
-- E-commerce was chosen as the bundled example domain only because its
-- tables (users/orders/products/order_items) are universally legible —
-- NOT because Schema Brain targets e-commerce. Schema Brain is a
-- generic semantic layer for ANY production database. Future bundled
-- fixtures (HR, analytics, healthcare-light) drop into this same
-- `fixtures/` directory; users author their own `.sql` + matching
-- `golden_sets/*.json` for real schemas.
--
-- To reproduce the bundled `golden_sets/ecommerce.json` scores:
--   psql "$DATABASE_URL" -f schemabrain/eval/fixtures/ecommerce.sql
--   schemabrain index "$DATABASE_URL" --store-path ./schemabrain.db
--   schemabrain eval --source "$DATABASE_URL" --store-path ./schemabrain.db
--
-- If you change table or column names here, regenerate the matching
-- golden set in `schemabrain/eval/golden_sets/ecommerce.json`.
--
-- The seed rows are generated by `scripts/generate_ecommerce_fixture.py`
-- with a fixed RNG seed so reruns are deterministic. ~80 users with a
-- Pareto-shaped order distribution let queries like
-- `get_metric(name="total_items_sold", group_by=["user.email"], limit=5)`
-- return a clear leaderboard instead of a 3-row trivia answer.

CREATE TABLE IF NOT EXISTS public.users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.categories (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id BIGINT REFERENCES public.categories(id)
);

CREATE TABLE IF NOT EXISTS public.products (
    id BIGSERIAL PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE IF NOT EXISTS public.product_categories (
    product_id BIGINT NOT NULL REFERENCES public.products(id),
    category_id BIGINT NOT NULL REFERENCES public.categories(id),
    PRIMARY KEY (product_id, category_id)
);

-- `addresses` exists to demonstrate the multi-canonical-per-pair
-- case in the bundled fixture: `orders` carries TWO FKs to addresses
-- (billing + shipping), which produce two distinct canonical joins
-- with the same `(order, address)` entity pair, disambiguated by name.
CREATE TABLE IF NOT EXISTS public.addresses (
    id BIGSERIAL PRIMARY KEY,
    line1 TEXT NOT NULL,
    city TEXT NOT NULL,
    country TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS public.orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id),
    billing_address_id BIGINT REFERENCES public.addresses(id),
    shipping_address_id BIGINT REFERENCES public.addresses(id),
    status TEXT NOT NULL DEFAULT 'pending',
    total_cents INTEGER NOT NULL,
    placed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.order_items (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES public.orders(id),
    product_id BIGINT NOT NULL REFERENCES public.products(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0)
);
"""


def main() -> int:
    rng = random.Random(_SEED)
    users = _generate_users(rng)
    addresses = _generate_addresses(rng)
    products = _all_products()
    orders, items = _generate_orders_and_items(rng, users, products, len(addresses))

    sections = [
        _HEADER,
        "-- Seeded by scripts/generate_ecommerce_fixture.py with"
        f" random.Random({_SEED}); reruns are deterministic.",
        "",
        _emit_users(users),
        "",
        _emit_categories(),
        "",
        _emit_products(products),
        "",
        _emit_addresses(addresses),
        "",
        _emit_product_categories(_product_categories()),
        "",
        _emit_orders(orders),
        "",
        _emit_order_items(items),
        "",
    ]
    sys.stdout.write("\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
