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

-- `addresses` exists to demonstrate the wk-13 multi-canonical-per-pair
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

-- Minimal seed data so the profiler has sample values to feed the LLM.
INSERT INTO public.users (email, full_name) VALUES
    ('alice@example.com', 'Alice Patel'),
    ('bob@example.com', 'Bob Mendez'),
    ('cara@example.com', 'Cara Liu')
ON CONFLICT DO NOTHING;

INSERT INTO public.categories (id, name, parent_id) VALUES
    (1, 'Apparel', NULL),
    (2, 'Shoes', 1),
    (3, 'Electronics', NULL)
ON CONFLICT DO NOTHING;

INSERT INTO public.products (sku, name, description, price_cents) VALUES
    ('SKU-001', 'Running Shoes', 'Lightweight trail runners', 8999),
    ('SKU-002', 'Wireless Headphones', 'Over-ear bluetooth', 14999)
ON CONFLICT DO NOTHING;

INSERT INTO public.addresses (id, line1, city, country) VALUES
    (1, '101 First Street', 'Brooklyn', 'US'),
    (2, '42 Second Avenue', 'Brooklyn', 'US')
ON CONFLICT DO NOTHING;

-- Junction rows so the M:N path between products and categories isn't
-- silently empty. SKU-001 (Running Shoes) → Shoes (cat 2);
-- SKU-002 (Wireless Headphones) → Electronics (cat 3).
INSERT INTO public.product_categories (product_id, category_id) VALUES
    (1, 2),
    (2, 3)
ON CONFLICT DO NOTHING;

-- Transactional rows so the marquee metrics (`total_revenue`,
-- `order_count`, `distinct_ordering_customers`, `average_order_value`,
-- `total_units_sold`) return non-null numbers a user can sanity-check
-- against `examples/ecommerce/`'s walkthrough Step 6. Three orders,
-- four line items, all three users represented. Sums:
--   order 1: 2 × 8999 + 1 × 14999 = 32997
--   order 2: 1 × 8999                = 8999
--   order 3: 3 × 14999               = 44997
-- total_revenue across all orders = 86993 cents = $869.93
INSERT INTO public.orders (id, user_id, billing_address_id, shipping_address_id, status, total_cents, placed_at) VALUES
    (1, 1, 1, 1, 'fulfilled', 32997, '2026-04-15 10:30:00+00'),
    (2, 2, 2, 2, 'fulfilled', 8999,  '2026-04-22 14:05:00+00'),
    (3, 3, 1, 1, 'pending',   44997, '2026-05-03 09:12:00+00')
ON CONFLICT DO NOTHING;

INSERT INTO public.order_items (id, order_id, product_id, quantity, unit_price_cents) VALUES
    (1, 1, 1, 2, 8999),
    (2, 1, 2, 1, 14999),
    (3, 2, 1, 1, 8999),
    (4, 3, 2, 3, 14999)
ON CONFLICT DO NOTHING;
