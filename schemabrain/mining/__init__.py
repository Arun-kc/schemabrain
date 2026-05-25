"""Query log mining for SchemaBrain.

Reads observed SQL from a source database's `pg_stat_statements` view,
parses each statement with `sqlglot` to identify which indexed tables
it touches, and writes the result into the `example_queries` store
table so tool #5 `get_example_queries` can surface real usage
patterns.

v0.5 entry point:
  - `schemabrain mine-queries --source <URL>` (CLI subcommand)
  - `schemabrain.mining.pipeline.mine_queries` (programmatic)

PII fields on the written rows stay at the safe defaults
(`sensitivity='public'`, `pii_categories=frozenset()`) at this
layer; once full PII classification integration lands on mined
queries, a subsequent re-mining of the same rows will reclassify
them via the UPSERT.
"""
