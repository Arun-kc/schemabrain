# Schema Brain stress harness

Headless integration harness that exercises the full MCP surface area
end-to-end against a real Postgres + indexed store. Used to validate
correctness across the categories an agent actually drives:

| Category   | Coverage                                                           |
| ---------- | ------------------------------------------------------------------ |
| DISCOVERY  | list / describe / find tools                                       |
| AGGREGATE  | single-row sums / counts / averages                                |
| RANKING    | top-N via `order_by` + `limit` (PR-6h.2 marquee)                   |
| JOIN       | multi-hop chains + via= disambiguation (PR-6h.1 / PR-6h.1.1)       |
| COLUMN_VAL | compile-time `group_by` / `filter` column validation (PR-6h.3)     |
| FILTER     | eq / in / not_null / unary-op-with-value rejection                 |
| DEGRADE    | `fan_out_join` / `missing_order_by_with_limit` precedence          |
| ERROR      | unknown_metric / unknown_name / malformed_name / invalid_time_grain|
| TIME       | grain bucketing by day / week / month                              |
| PII        | category propagation through joined columns                        |
| VOLUME     | large-limit slicing + deterministic top-row                        |

## Setup

```bash
# 1. Boot the bundled demo Postgres + load the fixture
docker run -d --name sb-demo-pg -p 5433:5432 \
    -e POSTGRES_PASSWORD=local postgres:16
docker exec -i sb-demo-pg psql -U postgres -d postgres \
    < schemabrain/eval/fixtures/ecommerce.sql

# 2. Build a fresh store with enrichment
DATABASE_URL=postgresql://postgres:local@localhost:5433/postgres \
    uv run schemabrain index --url-env DATABASE_URL \
    --store-path /tmp/stress-store.db --no-enrich --quiet

DATABASE_URL=postgresql://postgres:local@localhost:5433/postgres \
    uv run schemabrain entities suggest --url-env DATABASE_URL \
    --store-path /tmp/stress-store.db --apply
DATABASE_URL=postgresql://postgres:local@localhost:5433/postgres \
    uv run schemabrain joins suggest --url-env DATABASE_URL \
    --store-path /tmp/stress-store.db --apply
DATABASE_URL=postgresql://postgres:local@localhost:5433/postgres \
    uv run schemabrain metrics suggest --url-env DATABASE_URL \
    --store-path /tmp/stress-store.db --apply

# 3. Run the harness
uv run python scripts/stress_test/comprehensive.py
```

## What gets validated

Each scenario reports `PASS` / `FAIL` / `UNEXPECTED`. Expected outcome:

```
======================================================================
  AGGREGATE      7/7  PASS
  COLUMN_VAL     6/6  PASS
  DEGRADE        4/4  PASS
  DISCOVERY      8/8  PASS
  ERROR          6/6  PASS
  FILTER         6/6  PASS
  JOIN           7/7  PASS
  PII            3/3  PASS
  RANKING        7/7  PASS
  TIME           3/3  PASS
  VOLUME         3/3  PASS
======================================================================
TOTAL: 60/60 PASS
```

## When to extend

Add a scenario whenever:

- A new MCP tool ships (give it a discovery-category check)
- A new compiler error class is added (envelope mapping check)
- A new degradation kind ships (precedence + recovery hint check)
- A bug-report or smoke surfaces an agent-UX gap not covered above

Scenarios are defined in `comprehensive.py` under one of the
`*_scenarios()` helpers. Add to the right category, keep the scoring
predicate small enough to read in one breath.

## Layer-A vs Layer-B coverage

This harness is **Layer A**: drives MCP tools directly with hand-picked
arguments. It validates the substrate's correctness end-to-end.

**Layer B** — agent-loop accuracy — requires running Claude (via the
Anthropic SDK or live Claude Desktop) against the same store and
checking that the natural-language plan picks the right tools in the
right sequence with the right args. Not in this harness today; the
right next step is to wire the Anthropic SDK + the same MCP server
together and replay 10-20 representative natural-language questions.
