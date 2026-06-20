import type { BlogPostMeta } from "../_registry";

export const meta: BlogPostMeta = {
  slug: "why-i-built-schemabrain",
  title: "Why I built SchemaBrain",
  dek: "I didn’t want to give an AI agent raw SQL access to production Postgres — so I built a different boundary. One that compiles intent to safe SQL, refuses PII before the query runs, and keeps a tamper-evident audit.",
  date: "2026-06-20",
  readingMinutes: 6,
  tags: ["AI agents", "Postgres", "MCP"],
  author: "Arun K C",
};

/**
 * Body for /blog/why-i-built-schemabrain.
 *
 * Ported from docs/internal/distribution/content/founder-blog-post.md with three
 * corrections applied so the published essay matches the (PR #256-corrected)
 * README rather than the draft’s stale claims:
 *   1. retrieval is on-device cosine — NOT "BM25 / hybrid retrieval"
 *   2. indexing an 84-col schema "runs about $0.03" — "directly measured" is
 *      reserved for the Pagila run ($0.0299); the draft’s "measured ~$0.034" is gone
 *   3. the absolute "no telemetry / no data egress" is reframed to disclose the
 *      one opt-in egress (the cost-capped enrichment call), which the draft’s own
 *      enrichment section already implied
 *
 * Biographical detail (career stack: Postgres / RDS / Oracle / dbt; tribal
 * knowledge in heads, stale Confluence pages, Teams/Slack threads; the "understand
 * the data without handing over total control" framing) reflects the author's own
 * account. The seven-value status column stays as illustrative color.
 */
export default function WhyIBuiltSchemaBrain() {
  return (
    <>
      <p>
        I’ve spent my career as a data engineer — Postgres, RDS, Oracle, dbt. Most of it went to a
        quiet, unglamorous truth: the schema is never the whole story. Column names lie. The business
        logic that explains them lives in someone’s head, in a Confluence page nobody has updated in
        two years, in a Teams or Slack thread. A <code>status</code> column has seven values and only
        three of them mean what you’d guess. I’ve watched this firsthand, more than once.
      </p>
      <p>
        Here’s what that job teaches you: anything can be data, but what’s actually useful is the part
        that’s curated — the part you can infer something real from. Turning raw tables into meaning
        you can trust is the entire job of data engineering.
      </p>
      <p>
        So when AI agents started getting database access, I watched them hit the same wall humans do —
        except faster, and more confidently wrong. They had the schema. They didn’t have the meaning.
      </p>

      <h2>The real problem</h2>
      <p>
        Point a capable model at a production Postgres database and three things go wrong almost
        immediately.
      </p>
      <p>
        <strong>The schema doesn’t fit the context window, and it wouldn’t help if it did.</strong> A
        real database is hundreds of tables and thousands of columns. Even if you dump all of it into
        the prompt, the agent still doesn’t know that <code>unit_price_cents</code> is an integer in
        cents, or that “revenue” means contracted subscription line-items and not collected invoices.
        The schema tells you the shape. It does not tell you the meaning.
      </p>
      <p>
        <strong>Cryptic columns.</strong> <code>dt_ref</code>, <code>flg_2</code>, <code>amt</code>.
        The model guesses. Sometimes it guesses right. The dangerous case is when it guesses plausibly
        and you don’t catch it.
      </p>
      <p>
        <strong>The genuine danger of raw SQL.</strong> This is the one that kept me from shipping the
        obvious thing. The standard pattern — give the agent a tool that runs SQL — means the agent can
        run <em>any</em> SQL. A confused agent writes a 40-million-row cross join. A jailbroken one
        reads your <code>users</code> table, including the column nobody remembered was storing
        plaintext API keys. And you find out from the audit log, if you have one.
      </p>
      <p>
        The usual answer is “give it read-only credentials and hope.” Read-only stops the writes. It
        does nothing about the cross join, and nothing about an agent happily <code>SELECT</code>-ing
        every email address in the database because a prompt asked it to.
      </p>

      <h2>The insight</h2>
      <p>The fix isn’t a better prompt or a smarter model. It’s a different boundary.</p>
      <p>
        I didn’t want to give the agent total control over the data just so it could understand the
        data — those are two different things, and conflating them is the whole problem. So I stopped
        thinking of this as “give the agent database access” and started thinking of it as a{" "}
        <strong>trust and intelligence layer</strong> that sits between the agent and Postgres. A
        layer with one strong opinion: the agent never writes SQL, and the layer never executes SQL
        the agent wrote.
      </p>
      <p>
        Instead, the agent expresses <em>intent</em> — an entity, a metric, a grain (“contracted
        revenue, by plan tier”). SchemaBrain compiles that intent to parameterized SQL on its own side
        and runs it. The agent gets rows back, and it gets the exact SQL that ran, but there is no path
        from an agent prompt to an arbitrary statement at your database. The guarantee is structural,
        not a config flag the agent can talk its way around.
      </p>
      <p>Once you have that compile step, you can put real checks at the boundary:</p>
      <ul>
        <li>
          <strong>Refuse PII before the query runs</strong>, not after it’s already in the response.
        </li>
        <li>
          <strong>Refuse to fabricate</strong> — if the join doesn’t exist, say so, and hand back a
          machine-readable way to recover instead of a paragraph the agent has to parse.
        </li>
        <li>
          <strong>Record every call in a tamper-evident log</strong>, so “what did the agent actually
          do” has a cryptographic answer.
        </li>
      </ul>

      <h2>What SchemaBrain actually is</h2>
      <p>
        It’s an open-source MCP server (Apache-2.0) that gives AI agents safe, semantic access to a
        Postgres database. It’s local-first: the server runs entirely on your machine, and an agent’s
        queries never leave it. The one thing that can leave the box is the optional
        semantic-enrichment pass at index time — a cost-capped LLM call with redacted samples that you
        opt into — and the bundled demo skips even that. That restraint isn’t marketing I bolted on;
        it’s the whole point. The trust pitch only works if the trust layer itself isn’t quietly
        shipping your schema somewhere.
      </p>
      <p>Concretely, four stages:</p>
      <p>
        <strong>1. Index.</strong> SchemaBrain reads your Postgres schema into a local SQLite store.
        Tables, columns, types, declared foreign keys.
      </p>
      <p>
        <strong>2. Semantic enrichment.</strong> This is where the intelligence comes from. A
        cost-capped LLM pass writes a plain-language description for each column, proposes entities
        (the <code>users</code> table is a <em>User</em>), and mines joins from three sources: declared
        foreign keys, your query log, and dbt <code>relationships</code> tests. It builds a canonical
        join graph with multi-hop BFS, so the layer knows how to get from <em>User</em> to{" "}
        <em>Invoice</em> even when it’s three hops through a junction table. On-device embeddings
        (BAAI/bge-small, ONNX) power semantic cosine retrieval, so the agent can find the right tables
        by meaning, not just by name. You review and apply the suggestions explicitly — nothing
        auto-commits to your semantic layer.
      </p>
      <p>
        <strong>3. MCP tools.</strong> The server exposes twelve read-only tools —{" "}
        <code>find_relevant_tables</code>, <code>describe_entity</code>, <code>list_metrics</code>,{" "}
        <code>get_metric</code>, <code>resolve_join</code>, and so on. None of them can write. There is
        no <code>execute()</code>, no <code>query()</code>. <code>serve</code> also pins{" "}
        <code>default_transaction_read_only=on</code> at the database as belt-and-suspenders, but the
        real guarantee is that the write tool simply does not exist in the binary.
      </p>
      <p>
        <strong>4. Firewall + audit on every call.</strong> PII tags propagate from the physical
        column through joins and metrics. If a <code>get_metric</code> call touches a blocked category,
        the compiled SQL never runs — the call comes back refused, and the refusal lands in the audit
        log. Every call, every refusal, every recovery writes one row to an append-only{" "}
        <code>mcp_audit</code> table, SHA-256 hash-chained, with browser-verifiable RFC-6962 Merkle
        inclusion proofs. <code>audit verify</code> re-walks the chain and exits non-zero if any past
        row was rewritten.
      </p>
      <p>
        The refusal is the part I’m proudest of, because of <em>how</em> it refuses. When the agent
        asks for something the schema can’t answer — say, usage volume by plan tier, when there’s no{" "}
        <code>plan_id</code> on usage events — SchemaBrain doesn’t hallucinate a join. It returns a
        structured envelope:
      </p>
      <pre>
        <code>{`{ "kind": "unreachable_entity", "recovery": { "suggested_tool": "resolve_join" } }`}</code>
      </pre>
      <p>
        A refused agent gets an actionable, machine-readable path forward, not just “denied.” In my
        live tests the agent reads that contract and pivots — “I can’t fake that join, here’s
        contracted revenue by plan tier instead” — programmatically, without making something up.
      </p>

      <h2>What it deliberately does NOT do</h2>
      <p>I’d rather you know the edges than discover them.</p>
      <ul>
        <li>
          <strong>It does not run agent-authored SQL.</strong> That’s the design, not a limitation I’m
          apologizing for — but if your use case is “let the agent write arbitrary analytical SQL,”
          this is the wrong tool today. Inspecting agent-emitted SQL is a possible later opt-in lane,
          not the direction the product is pivoting to.
        </li>
        <li>
          <strong>It does not do content-aware PII classification yet.</strong> PII detection today is
          column-name pattern matching — 60 rules across 12 GDPR / CCPA / HIPAA / PCI categories, with
          a catastrophic-leak floor (<code>credential</code>, <code>payment_card</code>,{" "}
          <code>government_id</code>) that hard-refuses regardless of your policy. A PII value hiding in
          a generically named free-text column won’t be caught by name alone. Content-aware
          classification is on the roadmap.
        </li>
        <li>
          <strong>It does not connect to anything but Postgres.</strong> The local store is SQLite, but
          there is no SQLite, Snowflake, BigQuery, or MySQL <em>source</em> connector today. Those are
          roadmap. I’d rather ship one connector that works than five that half-work.
        </li>
        <li>
          <strong>The dbt import is partial.</strong> It binds dbt models to entities and dbt{" "}
          <code>relationships</code> tests to joins. It does not import your full metric/semantic
          layer. If you have a rich dbt semantic layer, treat the import as a head start, not a
          migration.
        </li>
        <li>
          <strong>The dashboard is a viewer.</strong> The optional local dashboard binds{" "}
          <code>127.0.0.1</code> only, is read-only, and has no <code>--host</code> flag by design. No
          SQL pad, no settings, no write path.
        </li>
      </ul>

      <h2>The numbers</h2>
      <p>
        I keep the cost honest because the cost is the objection. Semantic enrichment runs on Claude
        Haiku 4.5 at roughly $0.0004 per column. Indexing an 84-column schema with full LLM column
        descriptions runs about $0.03. The Pagila DVD-rental sample (87 columns) is the directly
        measured reference — $0.0299 in 105 seconds. Re-indexing an unchanged schema costs $0 —
        content-addressable fingerprinting skips the LLM call entirely, so you only pay for what
        actually changed. Cryptic-name columns can opt into Sonnet 4.6 routing when Haiku isn’t enough.
      </p>
      <p>
        The bundled demo runs for $0 with no API key at all — it ships with a pre-curated semantic
        pack, so you can watch the whole refuse-then-recover loop before spending a cent.
      </p>

      <h2>If you’re pointing agents at a real Postgres</h2>
      <p>
        <code>pip install schemabrain</code> (or <code>uvx schemabrain init</code>) walks you to a
        wired-up MCP host in one command. Everything is open source, Apache-2.0. The code is at{" "}
        <a href="https://github.com/Arun-kc/schemabrain" target="_blank" rel="noreferrer">
          github.com/Arun-kc/schemabrain
        </a>
        , docs at{" "}
        <a href="https://schemabrain.mintlify.app" target="_blank" rel="noreferrer">
          schemabrain.mintlify.app
        </a>
        .
      </p>
      <p>
        I’m building this part-time, solo, and the most useful thing in the world to me right now is
        watching it meet a real schema that isn’t mine. The demo pack is curated; your production
        Postgres is not. If you’re actually pointing agents at a real database, I’d genuinely love to
        hear how it goes — what it gets right, where it falls down on your column names, which refusal
        you wished it had made. I’m happy to help you get it set up. Reach me through the repo, or just
        open an issue.
      </p>
      <p>The schema was never the whole story. I’d like the agent to know that too.</p>
    </>
  );
}
