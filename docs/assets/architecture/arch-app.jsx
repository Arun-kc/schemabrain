/* SchemaBrain · Architecture Premium · Main app
   Tab controller, beat timeline, captions, control bar. */

const { useState, useEffect, useRef } = React;

const MODES = [
  { id: "mcp",      num: "01", label: "12-Tool MCP Stdio Flow",        kind: "mint",
    caption: { ttl: "Stdio JSON-RPC over a 12-tool surface",
               desc: "Agent emits an MCP call. SchemaBrain resolves it against the semantic layer, looks up cosine-similar entities in the local vector store, compiles a strictly parameterized read-only SQL, executes it against Postgres, then writes a hash-chained audit row and returns the structured response." } },
  { id: "pii",      num: "02", label: "PII Refusal & Firewall Path",   kind: "pii",
    caption: { ttl: "The database is never touched",
               desc: "Same MCP call. The classifier matches the requested columns against CCPA · GDPR · HIPAA · PCI categories — a hit on contact data triggers an immediate refusal. SchemaBrain returns a structured recovery envelope {status: 'refused', kind: 'pii_blocked', recovery: …} and logs the attempt." } },
  { id: "indexing", num: "03", label: "Indexing & Enrichment Pipeline", kind: "cyan",
    caption: { ttl: "Local cron · DB Connector → Profiler → Embedder → SQLite",
               desc: "A scheduled job reads schemas through a read-only connection, a regex-based profiler classifies columns, fastembed BAAI/bge-small-en-v1.5 (~67 MB ONNX) generates 384-dim embeddings locally, and everything is written to the single SQLite registry." } },
  { id: "audit",    num: "04", label: "Cryptographic Audit Chain",     kind: "mint",
    caption: { ttl: "Tamper-evident SHA-256 chain · append-only",
               desc: "Every tool call is appended as a ledger row hashed against the previous: H(rowₙ) = SHA256(data ‖ H(rowₙ₋₁)). schemabrain audit verify walks the chain and surfaces any tampered row in milliseconds." } },
];

const BEATS_PER_CYCLE = 7;
const BEAT_MS = 1200; // 1.2s per beat → ~8.4s loop

function App() {
  const [mode, setMode] = useState("mcp");
  const [beat, setBeat] = useState(0);
  const [playing, setPlaying] = useState(true);
  const beatRef = useRef(beat);
  beatRef.current = beat;

  // Beat timer
  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      setBeat(b => (b + 1) % BEATS_PER_CYCLE);
    }, BEAT_MS);
    return () => clearInterval(id);
  }, [playing, mode]);

  // Reset beat when mode changes
  useEffect(() => { setBeat(0); }, [mode]);

  const activeMode = MODES.find(m => m.id === mode) || MODES[0];

  return (
    <>
      <TabBarPortal mode={mode} setMode={setMode} />
      <StageContents mode={mode} beat={beat} />
      <FlowCaption mode={mode} active={activeMode} beat={beat} />
      <ControlsPortal playing={playing} setPlaying={setPlaying} mode={mode} beat={beat} />
    </>
  );
}

function TabBarPortal({ mode, setMode }) {
  const bar = document.getElementById("tab-bar");
  if (!bar) return null;
  return ReactDOM.createPortal(
    <>
      {MODES.map(m => (
        <button
          key={m.id}
          className={`tab ${m.kind} ${mode === m.id ? "active" : ""}`}
          onClick={() => setMode(m.id)}
        >
          <span className="num">{m.num}</span>
          {m.label}
        </button>
      ))}
    </>,
    bar
  );
}

function ControlsPortal({ playing, setPlaying, mode, beat }) {
  const el = document.getElementById("controls-mini");
  if (!el) return null;
  return ReactDOM.createPortal(
    <>
      <span style={{ marginRight: 6 }}>BEAT {String(beat + 1).padStart(2, "0")}/{String(BEATS_PER_CYCLE).padStart(2, "0")}</span>
      <button className={`ctl ${playing ? "on" : ""}`} onClick={() => setPlaying(p => !p)}>
        {playing ? "⏸ pause" : "▶ play"}
      </button>
    </>,
    el
  );
}

function StageContents({ mode, beat }) {
  const root = document.getElementById("stage-canvas");
  if (!root) return null;
  return ReactDOM.createPortal(
    <>
      <ConnectorLayer mode={mode} beat={beat} />
      <CoreBoundary mode={mode} />
      <AgentCard mode={mode} beat={beat} />
      <MCPServerModule mode={mode} beat={beat} />
      <PIIModule mode={mode} beat={beat} />
      <EmbedderModule mode={mode} beat={beat} />
      <AuditModule mode={mode} beat={beat} />
      <RegistryModule mode={mode} beat={beat} />
      <DatabaseCard mode={mode} beat={beat} />
    </>,
    root
  );
}

// Numbered step badges floating along the active connector
function StepLabels({ mode, beat }) {
  const labels = STEP_LABELS[mode] || [];
  const current = labels[beat];
  if (!current) return null;
  return (
    <div style={{
      position: "absolute",
      left: current.x, top: current.y,
      transform: "translate(-50%, -50%)",
      display: "flex", alignItems: "center", gap: 8,
      background: "rgba(7, 8, 11, 0.92)",
      border: `1px solid ${current.color}`,
      borderRadius: 6,
      padding: "5px 10px 5px 5px",
      fontFamily: "var(--mono)",
      fontSize: 10,
      color: "var(--paper)",
      letterSpacing: "0.04em",
      boxShadow: `0 0 18px ${current.color}66`,
      whiteSpace: "nowrap",
      zIndex: 5,
      animation: "fadeStep 240ms ease-out"
    }}>
      <span style={{
        width: 18, height: 18, borderRadius: 4,
        background: current.color, color: "#0A0B10",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 10, fontWeight: 700
      }}>{current.n}</span>
      {current.txt}
    </div>
  );
}

const C_MINT = "#3DCD8B";
const C_CYAN = "#22D3EE";
const C_RED  = "#EF4444";

const STEP_LABELS = {
  mcp: [
    { n: "1", txt: "Discovery · stdio jsonrpc", x: 295, y: 240, color: C_MINT },
    { n: "2", txt: "MCP routes to semantic tool", x: 700, y: 60,  color: C_MINT },
    { n: "3", txt: "Cosine lookup · local vectors", x: 840, y: 184, color: C_CYAN },
    { n: "3", txt: "Resolve canonical join", x: 840, y: 380, color: C_CYAN },
    { n: "4", txt: "Compile parameterized SQL", x: 1040, y: 180, color: C_MINT },
    { n: "5", txt: "Log SHA-256 chain row", x: 870, y: 500, color: C_MINT },
    { n: "6", txt: "Return { ok, data }", x: 280, y: 460, color: C_MINT },
  ],
  pii: [
    { n: "1", txt: "Same MCP call inbound", x: 295, y: 240, color: C_MINT },
    { n: "2", txt: "Tool inspects requested columns", x: 660, y: 60, color: C_MINT },
    { n: "3", txt: "Match · contact + GDPR", x: 490, y: 264, color: C_RED },
    { n: "4", txt: "BLOCKED · DB never touched", x: 800, y: 282, color: C_RED },
    { n: "4", txt: "Build recovery envelope", x: 490, y: 320, color: C_RED },
    { n: "5", txt: "Audit the refusal too", x: 490, y: 380, color: C_RED },
    { n: "6", txt: "Return { pii_blocked }", x: 280, y: 360, color: C_RED },
  ],
  indexing: [
    { n: "1", txt: "Cron 04:00 · read schemas", x: 1040, y: 300, color: C_CYAN },
    { n: "2", txt: "Profiler classifies columns", x: 840, y: 282, color: C_CYAN },
    { n: "3", txt: "fastembed → 384-dim vec", x: 840, y: 380, color: C_CYAN },
    { n: "4", txt: "Write to SQLite registry", x: 840, y: 460, color: C_CYAN },
    { n: "4", txt: "Index 26 metrics",        x: 840, y: 500, color: C_CYAN },
    { n: "4", txt: "Index 4 612 embeddings",  x: 840, y: 540, color: C_CYAN },
    { n: "✓", txt: "Index complete",          x: 840, y: 460, color: C_CYAN },
  ],
  audit: [
    { n: "✓", txt: "Audit ledger · 5 rows shown", x: 490, y: 360, color: C_MINT },
    { n: "1", txt: "Row 14 hashed", x: 490, y: 460, color: C_MINT },
    { n: "2", txt: "Row 15 chained · prev hash mixed in", x: 490, y: 478, color: C_MINT },
    { n: "3", txt: "Row 16 chained", x: 490, y: 496, color: C_MINT },
    { n: "4", txt: "Row 17 chained", x: 490, y: 514, color: C_MINT },
    { n: "5", txt: "Row 18 chained · tip of chain", x: 490, y: 532, color: C_MINT },
    { n: "✓", txt: "schemabrain audit verify · 18/18", x: 490, y: 600, color: C_MINT },
  ],
};

const STEP_TEXT = {
  mcp: [
    "Discovery · stdio jsonrpc · agent calls find_relevant_entities()",
    "MCP router resolves the call against semantic-layer tool surface",
    "Cosine-similarity lookup against 4 612 local vectors",
    "Resolve canonical join · user → order → order_item",
    "Compile strictly-parameterized read-only SQL for Postgres",
    "Append SHA-256 audit row · inputs, metadata, timestamp",
    "Return { ok: true, data } envelope to the agent",
  ],
  pii: [
    "Same MCP call inbound · agent requests list_entities()",
    "MCP router inspects the requested columns",
    "PII classifier flags contact / GDPR match",
    "REFUSED · database is never touched",
    "Build structured recovery envelope · { kind: 'pii_blocked' }",
    "Audit the refusal too · tamper-evident chain entry",
    "Return { pii_blocked, recovery } back to the agent",
  ],
  indexing: [
    "Cron 04:00 UTC · read-only connection opens to Postgres",
    "Regex-based profiler classifies columns by content shape",
    "fastembed BAAI/bge-small-en-v1.5 → 384-dim ONNX vectors",
    "Write 142 table schemas to ~/.schemabrain.db",
    "Write 38 semantic entities + 26 metric definitions",
    "Write 4 612 vector embeddings · ready for cosine search",
    "Index complete · agent traffic resumes against fresh registry",
  ],
  audit: [
    "Audit ledger snapshot · last 5 rows of the chain",
    "Row 14 hashed · SHA-256 over data + prev hash",
    "Row 15 chained · previous hash mixes into next",
    "Row 16 chained · any tampering will cascade",
    "Row 17 chained · still verifying",
    "Row 18 chained · tip of the chain",
    "$ schemabrain audit verify · 18/18 ✓ chain intact",
  ],
};

function FlowCaption({ mode, active, beat }) {
  const stage = document.getElementById("stage-canvas");
  if (!stage) return null;
  const stepTxt = (STEP_TEXT[mode] || [])[beat] || "";
  return ReactDOM.createPortal(
    <div className={`flow-caption ${active.kind === "pii" ? "red" : active.kind === "cyan" ? "cyan" : ""}`}>
      <span className="num">{active.num}</span>
      <span className="txt">
        <span className="ttl">{active.caption.ttl}</span>
        <span className="desc">{active.caption.desc}</span>
      </span>
      <span className="step-block">
        <span className="step-num">STEP {String(beat + 1).padStart(2, "0")}/{String(BEATS_PER_CYCLE).padStart(2, "0")}</span>
        <span className="step-text">{stepTxt}</span>
      </span>
    </div>,
    stage
  );
}

ReactDOM.createRoot(document.getElementById("react-mount")).render(<App />);
