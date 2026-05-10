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

CREATE TABLE IF NOT EXISTS public.orders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id),
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
