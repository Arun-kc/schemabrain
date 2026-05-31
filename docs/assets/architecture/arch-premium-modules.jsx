/* SchemaBrain · Architecture Premium · Module components
   Each module is positioned absolutely inside the stage canvas. */

const { useState, useEffect, useMemo } = React;

// ────────────────────────────────────────────────────────────────────
// AGENT card (Column 1)
// ────────────────────────────────────────────────────────────────────
function AgentCard({ mode, beat }) {
  const lit = mode !== "indexing" && (beat === 0 || beat === 5 || beat === 6);
  const callLabel = useMemo(() => {
    if (mode === "pii") return beat >= 5 ? "← refused {pii_blocked}" : "list_entities()";
    if (mode === "audit") return "list_metrics()";
    return "get_metric('revenue_30d')";
  }, [mode, beat]);

  return (
    <div
      className={`glass ${lit ? "lit-mint" : ""} ${mode === "indexing" ? "dimmed" : ""}`}
      style={{ left: 0, top: 160, width: 260, height: 320 }}
    >
      <div className="glass-header">
        <span className="ttl"><span className="sym">◆</span>MCP Client Host</span>
        <span className="tag-pill mint">LLM AGENT</span>
      </div>
      <div className="glass-body" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <ClientRow icon="C" name="Claude Desktop" active={lit} />
        <ClientRow icon="cc" name="Claude Code" active={false} />
        <ClientRow icon="C" name="Cursor" active={false} />
        <ClientRow icon="Z" name="Zed" active={false} />
        <ClientRow icon="L" name="LangGraph loop" active={false} />

        <div className="stdio-pipe" style={{ marginTop: 12 }}>
          <span className="pipe-dot"></span>
          stdio · jsonrpc · 2.0
          <span className="pipe-tag">▶ {callLabel}</span>
        </div>
      </div>
    </div>
  );
}

function ClientRow({ icon, name, active }) {
  return (
    <div className={`client-row ${active ? "active" : ""}`}>
      <span className="lhs">
        <span className="icon">{icon}</span>
        {name}
      </span>
      <span className="pulse-dot"></span>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// CORE TRUST BOUNDARY (Column 2 wrapper)
// ────────────────────────────────────────────────────────────────────
function CoreBoundary({ mode }) {
  return (
    <div
      className="trust-boundary"
      style={{ left: 304, top: 0, width: 720, height: 660 }}
    >
      <span className="trust-corner tl"></span>
      <span className="trust-corner tr"></span>
      <span className="trust-corner bl"></span>
      <span className="trust-corner br"></span>
      <div className="trust-label">
        <span className="pip"></span>
        SCHEMABRAIN · LOCAL CORE
        <span className="right">SQL FIREWALL · SEMANTIC LAYER · process-local</span>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// MCP STDIO SERVER (12 Read-Only Tools)
// ────────────────────────────────────────────────────────────────────
const PHYSICAL_TOOLS = [
  "find_relevant_tables", "describe_table", "describe_column",
  "suggest_joins", "get_example_queries"
];
const SEMANTIC_TOOLS = [
  "list_entities", "list_metrics", "list_joins",
  "find_relevant_entities", "describe_entity", "resolve_join", "get_metric"
];

function MCPServerModule({ mode, beat }) {
  const lit = mode === "mcp" && (beat === 1 || beat === 4) ||
              mode === "pii"  && beat === 1 ||
              mode === "audit" && beat === 1;
  const dimmed = mode === "indexing";

  const activeTool = useMemo(() => {
    if (mode === "mcp") return beat === 4 ? "get_metric" : "find_relevant_entities";
    if (mode === "pii") return "list_entities";
    if (mode === "audit") return "list_metrics";
    return null;
  }, [mode, beat]);

  return (
    <div
      className={`module ${lit ? "lit-mint" : ""} ${dimmed ? "dimmed" : ""}`}
      style={{ position: "absolute", left: 324, top: 24, width: 680, height: 152 }}
    >
      <div className="module-header">
        <span className="label"><span className="idot"></span>MCP STDIO SERVER · 12 READ-ONLY TOOLS</span>
        <span className="badge">stdio · jsonrpc 2.0</span>
      </div>
      <div className="module-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div>
          <div className="tool-group-label">▸ Physical Layer · 5</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {PHYSICAL_TOOLS.map(t => (
              <span key={t} className={`tool-chip mint ${activeTool === t ? "active" : ""}`}>{t}</span>
            ))}
          </div>
        </div>
        <div>
          <div className="tool-group-label">▸ Semantic Layer · 7</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {SEMANTIC_TOOLS.map(t => (
              <span key={t} className={`tool-chip cyan ${activeTool === t ? "active cyan" : ""}`}>{t}</span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// PII CLASSIFIER & REFUSAL ENGINE
// ────────────────────────────────────────────────────────────────────
const PII_CATS = [
  { code: "CCPA",  hit: false }, { code: "GDPR",  hit: true },
  { code: "HIPAA", hit: false }, { code: "PCI",   hit: false },
  { code: "contact", hit: true }, { code: "health",  hit: false },
  { code: "financial", hit: false }, { code: "name", hit: true }
];

function PIIModule({ mode, beat }) {
  const lit = mode === "pii" && beat >= 2 && beat <= 5;
  const dimmed = mode === "indexing" || mode === "audit";
  return (
    <div
      className={`module ${lit ? "lit-red" : ""} ${dimmed ? "dimmed" : ""}`}
      style={{ position: "absolute", left: 324, top: 192, width: 332, height: 180 }}
    >
      <div className="module-header">
        <span className="label">
          <span className={`idot ${lit ? "red" : ""}`}></span>
          PII Classifier · Refusal Engine
        </span>
        <span className="badge">firewall</span>
      </div>
      <div className="module-body" style={{ paddingTop: 8 }}>
        <div className="pii-grid">
          {PII_CATS.map((c, i) => (
            <span key={c.code} className={`pii-cell ${lit && c.hit ? "hit" : ""}`}>{c.code}</span>
          ))}
        </div>
        <pre className="refusal-envelope" style={{ marginTop: 10, opacity: mode === "pii" && beat >= 4 ? 1 : 0.45, margin: "10px 0 0" }}>
{'{ '}<span className="k">status</span>: <span className="s">'refused'</span>,{'\n  '}<span className="k">kind</span>: <span className="s">'pii_blocked'</span>,{'\n  '}<span className="k">recovery</span>: <span className="c">{'{ … }'}</span>{'\n}'}
        </pre>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// VECTOR EMBEDDER & SIMILARITY ENGINE
// ────────────────────────────────────────────────────────────────────
function EmbedderModule({ mode, beat }) {
  const lit = (mode === "mcp" && beat === 2) ||
              (mode === "indexing" && beat === 2);
  const dimmed = mode === "audit";

  const [bars] = useState(() => Array.from({ length: 28 }, () => 0.3 + Math.random() * 0.7));

  const sims = [
    { lbl: "revenue · sales", val: 0.91 },
    { lbl: "user · customer", val: 0.84 },
    { lbl: "order · purchase", val: 0.79 }
  ];

  return (
    <div
      className={`module ${lit ? "lit-cyan" : ""} ${dimmed ? "dimmed" : ""}`}
      style={{ position: "absolute", left: 672, top: 192, width: 332, height: 180 }}
    >
      <div className="module-header">
        <span className="label">
          <span className="idot cyan"></span>
          Local Vector Embedder
        </span>
        <span className="badge">fastembed · 67MB · ONNX</span>
      </div>
      <div className="module-body" style={{ paddingTop: 6 }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 9.5, color: "var(--paper-faint)", letterSpacing: "0.06em", marginBottom: 4 }}>
          BAAI/bge-small-en-v1.5 · 384-dim
        </div>
        <div className="embed-strip">
          {bars.map((b, i) => (
            <span key={i} className="bar" style={{
              transform: `scaleY(${lit ? (0.4 + Math.sin((Date.now()/180 + i*0.5))*0.3 + b*0.5) : b})`,
              opacity: lit ? 0.95 : 0.6
            }}></span>
          ))}
        </div>
        <div className="sim-bar">
          {sims.map(s => (
            <div className="sim-row" key={s.lbl}>
              <span className="lbl">{s.lbl}</span>
              <span className="meter"><span className="fill" style={{ width: (s.val * 100) + "%" }}></span></span>
              <span className="val">{s.val.toFixed(2)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// SHA-256 AUDIT LOG (Cryptographic chain)
// ────────────────────────────────────────────────────────────────────
const AUDIT_ROWS = [
  { id: 14, hash: "a1f2 b9c3 4d7e 8f01" },
  { id: 15, hash: "c8d4 e29a 117b 3fa6" },
  { id: 16, hash: "9b34 7e1d a4c8 5f02" },
  { id: 17, hash: "ff90 2d18 c4ab 6e7c" },
  { id: 18, hash: "3a4c 7b81 e9d2 0f15" }
];

function AuditModule({ mode, beat }) {
  const lit = (mode === "mcp"   && beat === 5) ||
              (mode === "pii"   && beat === 5) ||
              (mode === "audit" && beat >= 1);
  const dimmed = mode === "indexing";

  // Which row is currently "being hashed" in audit mode
  const activeRow = mode === "audit" ? Math.min(AUDIT_ROWS.length - 1, beat - 1) : -1;
  const verifyDone = mode === "audit" && beat >= AUDIT_ROWS.length + 1;

  return (
    <div
      className={`module ${lit ? "lit-mint" : ""} ${dimmed ? "dimmed" : ""}`}
      style={{ position: "absolute", left: 324, top: 388, width: 332, height: 270 }}
    >
      <div className="module-header">
        <span className="label"><span className="idot"></span>SHA-256 Audit Chain</span>
        <span className="badge">append-only · tamper-evident</span>
      </div>
      <div className="module-body" style={{ paddingTop: 6 }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 9, color: "var(--paper-faint)", letterSpacing: "0.04em", marginBottom: 6 }}>
          H(row<sub>N</sub>) = SHA256( data ‖ H(row<sub>N-1</sub>) )
        </div>
        <div className="audit-chain">
          {AUDIT_ROWS.map((r, i) => (
            <div key={r.id} className={`audit-row ${activeRow >= i || (mode !== "audit" && lit) ? "lit" : ""}`}>
              <span className="blk">{r.id}</span>
              <span className="hash">0x{r.hash}</span>
              <span className="check">{(activeRow >= i || (mode !== "audit" && lit)) ? "✓" : "·"}</span>
            </div>
          ))}
        </div>
        <div className="audit-verify" style={{ opacity: verifyDone || (mode !== "audit" && lit) ? 1 : 0.35 }}>
          <span>$ schemabrain audit verify</span>
          <span className="check">✓</span>
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// LOCAL REGISTRY (SQLite)
// ────────────────────────────────────────────────────────────────────
const REGISTRY_ITEMS = [
  { name: "Table Schemas",       count: "142 tables",   tone: "mint" },
  { name: "Semantic Entities",   count: "38 entities",  tone: "cyan" },
  { name: "Canonical Joins",     count: "94 joins",     tone: "mint" },
  { name: "Custom Metrics",      count: "26 metrics",   tone: "mint" },
  { name: "Vector Embeddings",   count: "4 612 vecs",   tone: "cyan" }
];

function RegistryModule({ mode, beat }) {
  const lit = (mode === "mcp" && (beat === 2 || beat === 3)) ||
              (mode === "indexing" && beat >= 3);
  const dimmed = mode === "audit";

  const activeIdx = mode === "indexing" ? Math.min(REGISTRY_ITEMS.length - 1, beat - 3) : -1;

  return (
    <div
      className={`module ${lit ? (mode === "indexing" ? "lit-cyan" : "lit-mint") : ""} ${dimmed ? "dimmed" : ""}`}
      style={{ position: "absolute", left: 672, top: 388, width: 332, height: 270 }}
    >
      <div className="module-header">
        <span className="label">
          <span className={`idot ${mode === "indexing" ? "cyan" : ""}`}></span>
          Local Registry Store
        </span>
        <span className="badge">sqlite · ./schemabrain.db</span>
      </div>
      <div className="module-body" style={{ paddingTop: 8 }}>
        <div className="registry-list">
          {REGISTRY_ITEMS.map((r, i) => (
            <div key={r.name} className="registry-row" style={{
              borderColor: activeIdx === i ? "rgba(6,182,212,0.55)" : undefined,
              background:  activeIdx === i ? "rgba(6,182,212,0.08)" : undefined,
            }}>
              <span className="lhs">
                <span className={`swatch ${r.tone}`}></span>
                {r.name}
              </span>
              <span className="count">{r.count}</span>
            </div>
          ))}
        </div>
        {mode === "indexing" && (
          <div style={{
            marginTop: 8,
            fontFamily: "var(--mono)", fontSize: 9.5,
            color: "var(--cyan-bright)", letterSpacing: "0.06em",
            display: "flex", justifyContent: "space-between", alignItems: "center",
            padding: "5px 8px",
            background: "rgba(6, 182, 212, 0.06)",
            border: "1px dashed rgba(6, 182, 212, 0.4)",
            borderRadius: 4
          }}>
            <span>$ schemabrain index</span>
            <span style={{ color: "var(--paper-faint)" }}>cron · 04:00 UTC</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────
// DATABASE (Column 3)
// ────────────────────────────────────────────────────────────────────
function DatabaseCard({ mode, beat }) {
  const lit = mode === "mcp" && (beat === 4 || beat === 5);
  const dimmed = mode === "pii" || mode === "audit" || mode === "indexing";

  const sqlLine = useMemo(() => {
    if (mode === "pii") return "— blocked at firewall —";
    if (mode === "indexing") return "SELECT col FROM information_schema";
    return "SELECT sum(amount) FROM orders\nWHERE created_at > $1";
  }, [mode]);

  return (
    <div
      className={`glass db-card ${lit ? "lit" : ""} ${dimmed ? "dimmed" : ""}`}
      style={{ left: 1068, top: 160, width: 260, height: 320 }}
    >
      <div className="glass-header">
        <span className="ttl"><span className="sym">◆</span>Production Database</span>
        <span className="tag-pill mint">PG / SQLITE</span>
      </div>
      <div className="glass-body" style={{ textAlign: "center", paddingTop: 4 }}>
        <div className="db-cylinder">
          <div className="disk top"></div>
          <div className="body"></div>
          <div className="disk mid"></div>
          <div className="disk bot"></div>
        </div>

        <div className="read-only-badge">
          <span className="lock">⚿</span>
          default_transaction_read_only = ON
        </div>

        <div style={{
          marginTop: 12,
          padding: "8px 10px",
          background: "rgba(0,0,0,0.35)",
          border: "1px dashed rgba(61, 205, 139, 0.3)",
          borderRadius: 5,
          fontFamily: "var(--mono)",
          fontSize: 9.5,
          color: lit ? "var(--mint)" : "var(--paper-dim)",
          textAlign: "left",
          letterSpacing: "0.01em",
          lineHeight: 1.45,
          whiteSpace: "pre-line",
          minHeight: 38
        }}>
          {sqlLine}
        </div>
      </div>
    </div>
  );
}

Object.assign(window, {
  AgentCard, CoreBoundary,
  MCPServerModule, PIIModule, EmbedderModule, AuditModule, RegistryModule,
  DatabaseCard
});
