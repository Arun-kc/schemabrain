---
title: "Data dictionary"
description: "A generated reference for the bundled SaaS demo — every entity, column, type, PII classification, semantic join, and metric, produced by the schemabrain docs command."
---

Generated from the local SchemaBrain store (schema version 16). Every indexed table, column, type, PII classification, semantic join, and metric.

## api_key

A programmatic API credential issued to a workspace.

- **Table:** `public.api_keys`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `key_prefix` | `text` | no | no | no | Public | — | — |
| `api_key_hash` | `text` | no | no | no | PII | `Credential` (catastrophic) | — |
| `scope` | `text` | no | no | no | Public | — | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |
| `revoked_at` | `timestamptz` | yes | no | no | Public | — | — |

## billing_profile

A workspace's legal / tax identity used for invoicing.

- **Table:** `public.billing_profiles`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `legal_entity` | `text` | no | no | no | Public | — | — |
| `tax_id` | `text` | no | no | no | PII | `Government ID` (catastrophic) | — |
| `national_id` | `text` | yes | no | no | PII | `Government ID` (catastrophic) | — |
| `country_code` | `text` | no | no | no | PII | `Contact` | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |

## invoice

A billing document issued to a workspace.

- **Table:** `public.invoices`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `subscription_id` | `bigint` | yes | no | no | Public | — | — |
| `invoice_number` | `text` | no | no | no | Public | — | — |
| `status` | `text` | no | no | no | Public | — | — |
| `invoice_total_cents` | `integer` | no | no | no | Public | — | — |
| `issued_at` | `timestamptz` | no | no | no | Public | — | — |
| `paid_at` | `timestamptz` | yes | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `invoice_subscription` | `"invoice"."subscription_id" = "subscription"."id"` | many_to_one | Operator-authored | Links each invoice to the subscription it covers. |
| `invoice_workspace` | `"invoice"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each invoice to the workspace it bills. |

### Metrics

| Metric | Aggregation | Measure | Time dimension | Grains | Description |
| --- | --- | --- | --- | --- | --- |
| `total_revenue` | sum | `invoice_total_cents` | `invoice.issued_at` | day, week, month | Sum of invoice totals (cents). One row per requested grain. |

## payment_method

A card on file for a workspace (tokenized last-4, brand, expiry).

- **Table:** `public.payment_methods`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `card_number_last4` | `text` | no | no | no | PII | `Payment Card` (catastrophic) | — |
| `card_brand` | `text` | no | no | no | Public | — | — |
| `card_exp_month` | `integer` | no | no | no | Public | — | — |
| `card_exp_year` | `integer` | no | no | no | Public | — | — |
| `is_default` | `boolean` | no | no | no | Public | — | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `workspace_payment_methods` | `"payment_method"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each payment method on file to its workspace. |

## plan

A subscription plan in the catalog (tier, price, included seats, SLA).

- **Table:** `public.plans`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `plan_code` | `text` | no | no | no | Public | — | — |
| `title` | `text` | no | no | no | Public | — | — |
| `monthly_price_cents` | `integer` | no | no | no | Public | — | — |
| `included_seats` | `integer` | no | no | no | Public | — | — |
| `sla_tier` | `text` | no | no | no | Public | — | — |
| `is_active` | `boolean` | no | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `subscription_plan` | `"subscription"."plan_id" = "plan"."id"` | many_to_one | Operator-authored | Links each subscription to the plan it is on (leaderboard dimension). |

## session

An authenticated login session for a user.

- **Table:** `public.sessions`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `user_id` | `bigint` | no | no | no | Public | — | — |
| `session_token` | `text` | no | no | no | PII | `Credential` (catastrophic) | — |
| `last_login_ip` | `text` | yes | no | no | PII | `Online Identifier` | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |
| `expires_at` | `timestamptz` | no | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `user_sessions` | `"session"."user_id" = "user"."id"` | many_to_one | Operator-authored | Links each login session to its user. |

## subscription

A workspace's binding to a plan, with its lifecycle timestamps.

- **Table:** `public.subscriptions`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `plan_id` | `bigint` | no | no | no | Public | — | — |
| `status` | `text` | no | no | no | Public | — | — |
| `mrr_cents` | `integer` | no | no | no | Public | — | — |
| `started_at` | `timestamptz` | no | no | no | Public | — | — |
| `canceled_at` | `timestamptz` | yes | no | no | Public | — | — |
| `trial_ends_at` | `timestamptz` | yes | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `invoice_subscription` | `"invoice"."subscription_id" = "subscription"."id"` | many_to_one | Operator-authored | Links each invoice to the subscription it covers. |
| `subscription_items_subscription` | `"subscription_item"."subscription_id" = "subscription"."id"` | many_to_one | Operator-authored | Links each subscription line item to its parent subscription. |
| `subscription_plan` | `"subscription"."plan_id" = "plan"."id"` | many_to_one | Operator-authored | Links each subscription to the plan it is on (leaderboard dimension). |
| `workspace_subscriptions` | `"subscription"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each subscription to its workspace. |

### Metrics

| Metric | Aggregation | Measure | Time dimension | Grains | Description |
| --- | --- | --- | --- | --- | --- |
| `active_subscriptions` | count | `id` | `subscription.started_at` | day, week, month | Number of subscriptions per requested grain. |

## subscription_item

A per-seat line item on a subscription (line-level revenue grain).

- **Table:** `public.subscription_items`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `subscription_id` | `bigint` | no | no | no | Public | — | — |
| `plan_id` | `bigint` | no | no | no | Public | — | — |
| `seats` | `integer` | no | no | no | Public | — | — |
| `unit_price_cents` | `integer` | no | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `subscription_items_subscription` | `"subscription_item"."subscription_id" = "subscription"."id"` | many_to_one | Operator-authored | Links each subscription line item to its parent subscription. |

### Metrics

| Metric | Aggregation | Measure | Time dimension | Grains | Description |
| --- | --- | --- | --- | --- | --- |
| `total_revenue_real` | sum | `unit_price_cents * seats` | — | — | SUM of line-item subscription revenue (unit_price_cents x seats, in cents). Computed per subscription_item, so multi-line subscriptions roll up correctly. Use instead of `total_revenue` for per-plan/per-seat rollups via `group_by`.  |

## support_ticket

A customer-filed support request (free-text body).

- **Table:** `public.support_tickets`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `user_id` | `bigint` | yes | no | no | Public | — | — |
| `subject` | `text` | no | no | no | Public | — | — |
| `body` | `text` | yes | no | no | Public | — | — |
| `status` | `text` | no | no | no | Public | — | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |

## usage_event

A metered telemetry event (api_call / login / export).

- **Table:** `public.usage_events`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `event_type` | `text` | no | no | no | Public | — | — |
| `quantity` | `integer` | no | no | no | Public | — | — |
| `occurred_at` | `timestamptz` | no | no | no | Public | — | — |
| `recorded_at` | `timestamptz` | no | no | no | Public | — | — |

### Metrics

| Metric | Aggregation | Measure | Time dimension | Grains | Description |
| --- | --- | --- | --- | --- | --- |
| `usage_volume` | sum | `quantity` | `usage_event.occurred_at` | day, week, month | Sum of usage-event quantity per requested grain. |

## user

A workspace member account (login credential + contact details).

- **Table:** `public.users`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `workspace_id` | `bigint` | no | no | no | Public | — | — |
| `email` | `text` | no | no | no | PII | `Contact` | — |
| `full_name` | `text` | no | no | no | PII | `Contact` | — |
| `password_hash` | `text` | no | no | no | PII | `Credential` (catastrophic) | — |
| `role` | `text` | no | no | no | Public | — | — |
| `bio` | `text` | yes | no | no | Public | — | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `user_sessions` | `"session"."user_id" = "user"."id"` | many_to_one | Operator-authored | Links each login session to its user. |
| `workspace_users` | `"user"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each user to the workspace they belong to. |

### Metrics

| Metric | Aggregation | Measure | Time dimension | Grains | Description |
| --- | --- | --- | --- | --- | --- |
| `user_count` | count | `id` | — | — | Number of user accounts. Demonstrates PII-aware refusal: grouping by a credential column is refused, while grouping by a public column like `role` is allowed.  |

## workspace

A tenant account — the org boundary that owns users, subscriptions, and billing.

- **Table:** `public.workspaces`
- **Identity:** `id`
- **Group:** other

### Columns

| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `id` | `bigint` | no | yes | yes | Public | — | — |
| `name` | `text` | no | no | no | PII | `Contact` | — |
| `slug` | `text` | no | no | no | Public | — | — |
| `region` | `text` | no | no | no | Public | — | — |
| `plan_tier` | `text` | no | no | no | Public | — | — |
| `seat_count` | `integer` | no | no | no | Public | — | — |
| `created_at` | `timestamptz` | no | no | no | Public | — | — |

### Joins

| Join | On | Cardinality | Provenance | Description |
| --- | --- | --- | --- | --- |
| `invoice_workspace` | `"invoice"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each invoice to the workspace it bills. |
| `workspace_payment_methods` | `"payment_method"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each payment method on file to its workspace. |
| `workspace_subscriptions` | `"subscription"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each subscription to its workspace. |
| `workspace_users` | `"user"."workspace_id" = "workspace"."id"` | many_to_one | Operator-authored | Links each user to the workspace they belong to. |
