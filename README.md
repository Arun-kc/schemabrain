# Schema Brain

> An MCP server that gives AI agents deep semantic understanding of any production database.

**Status:** Pre-alpha. Under active development. Not yet usable.

Schema Brain introspects your database schema, profiles your data, mines your query logs, and uses an LLM to generate semantic descriptions of every table and column. It then exposes that knowledge through a stable set of MCP (Model Context Protocol) tools that AI agents can call to answer questions like:

- *"Which tables hold customer payment data?"*
- *"How do I join `orders` to `users` correctly?"*
- *"What does the `acct_dim_v3` table actually represent?"*

## Why this exists

AI agents fail when querying real production databases because:

1. Schemas are large (hundreds of tables) and don't fit in LLM context windows
2. Column names are cryptic (`acct_dim_v3`, `pmt_fct_h`, `cust_id_v2_legacy`)
3. Business logic isn't in the schema (which join is correct? what defines "churn"?)
4. Data has weird shapes (NULLs, deprecated columns, test data mixed with prod)

Schema Brain is the layer that fixes this — without forcing you to adopt a semantic-layer product or a data catalog UI.

## License

MIT License. See [LICENSE](LICENSE).
