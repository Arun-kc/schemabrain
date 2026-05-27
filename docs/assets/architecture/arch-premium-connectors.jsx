/* SchemaBrain · Architecture Premium · Connector / flow layer
   SVG paths between modules + animated particles. */

// All paths use the stage-canvas coordinate system (1328 × 660).
const SEGMENTS = {
  // ── happy path
  agentToMcp:        { d: "M 260 420 C 290 420, 290 100, 324 100", color: "mint", label: "list_entities()" },
  mcpToDb:           { d: "M 1004 100 C 1040 100, 1040 220, 1068 220", color: "mint", label: "parameterized SQL" },
  dbToAudit:         { d: "M 1068 360 C 1020 360, 1014 380, 1014 440 C 1014 500, 1000 500, 940 500 L 656 500", color: "mint", label: "exec metadata" },
  auditToAgent:      { d: "M 324 500 C 270 500, 280 470, 280 420 C 280 410, 270 420, 260 420", color: "mint", label: "{ ok: true, data: […] }" },

  // ── internal (always-visible faint, lit in specific modes)
  mcpToPii:          { d: "M 490 176 L 490 192", color: "mint", label: "" },
  mcpToEmbedder:     { d: "M 840 176 L 840 192", color: "cyan", label: "resolve" },
  embedderToRegistry:{ d: "M 840 372 L 840 388", color: "cyan", label: "" },
  piiToAudit:        { d: "M 490 372 L 490 388", color: "red",  label: "" },

  // ── pii refusal exit
  piiToAgent:        { d: "M 324 282 C 280 282, 280 282, 280 360 C 280 420, 270 420, 260 420", color: "red",  label: "{ pii_blocked }" },

  // ── indexing pipeline (offline)
  dbToEmbedder:      { d: "M 1068 320 C 1040 320, 1014 282, 1004 282", color: "cyan", label: "read schemas" },
};

// What segments are active for a given (mode, beat) pair.
const FLOW_SCRIPT = {
  // (M)CP stdio happy path
  mcp: [
    /* 0 */ ["agentToMcp"],
    /* 1 */ [],
    /* 2 */ ["mcpToEmbedder"],
    /* 3 */ ["embedderToRegistry"],
    /* 4 */ ["mcpToDb"],
    /* 5 */ ["dbToAudit"],
    /* 6 */ ["auditToAgent"],
  ],
  // (P)II refusal
  pii: [
    /* 0 */ ["agentToMcp"],
    /* 1 */ [],
    /* 2 */ ["mcpToPii"],
    /* 3 */ [],
    /* 4 */ [],
    /* 5 */ ["piiToAudit"],
    /* 6 */ ["piiToAgent"],
  ],
  // (I)ndexing pipeline (offline cron)
  indexing: [
    /* 0 */ ["dbToEmbedder"],
    /* 1 */ [],
    /* 2 */ ["embedderToRegistry"],
    /* 3 */ ["embedderToRegistry"],
    /* 4 */ [],
    /* 5 */ [],
    /* 6 */ [],
  ],
  // (A)udit chain
  audit: [
    /* 0 */ [],
    /* 1 */ ["dbToAudit"],
    /* 2 */ [],
    /* 3 */ [],
    /* 4 */ [],
    /* 5 */ [],
    /* 6 */ ["auditToAgent"],
  ],
};

function getActive(mode, beat) {
  const script = FLOW_SCRIPT[mode] || FLOW_SCRIPT.mcp;
  return script[beat] || [];
}

const COLOR_HEX = {
  mint: "#3DCD8B",
  cyan: "#22D3EE",
  red:  "#EF4444",
};
const COLOR_HEX_DIM = {
  mint: "rgba(61, 205, 139, 0.38)",
  cyan: "rgba(34, 211, 238, 0.38)",
  red:  "rgba(239, 68, 68, 0.38)",
};

function Segment({ id, d, color, label, active }) {
  const hex = COLOR_HEX[color];
  const dim = COLOR_HEX_DIM[color];
  const markerId = `arrow-${color}${active ? "-on" : ""}`;
  return (
    <g>
      {/* base faint line, always visible */}
      <path
        d={d}
        stroke={active ? hex : dim}
        strokeWidth={active ? 1.5 : 1.2}
        fill="none"
        strokeDasharray={active ? "0" : "4 5"}
        markerEnd={`url(#${markerId})`}
        style={{ transition: "stroke 240ms, stroke-width 240ms" }}
      />
      {active && (
        <>
          {/* flowing dashes over the path */}
          <path
            d={d}
            stroke={hex}
            strokeWidth={2.5}
            fill="none"
            strokeDasharray="6 10"
            strokeLinecap="round"
            style={{
              filter: `drop-shadow(0 0 6px ${hex})`,
              animation: "flowDash 1.4s linear infinite",
            }}
          />
          {/* traveling particle */}
          <circle r={4.5} fill={hex} style={{ filter: `drop-shadow(0 0 8px ${hex})` }}>
            <animateMotion dur="1.6s" repeatCount="indefinite" path={d} rotate="auto" />
          </circle>
          <circle r={2.5} fill="#fff" style={{ filter: `drop-shadow(0 0 4px ${hex})` }}>
            <animateMotion dur="1.6s" repeatCount="indefinite" path={d} rotate="auto" />
          </circle>
        </>
      )}
    </g>
  );
}

function ArrowMarkers() {
  // pre-defined arrow heads for each color (on + dim)
  const make = (id, color, opacity) => (
    <marker key={id} id={id} viewBox="0 0 10 10" refX="8" refY="5"
            markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 Z" fill={color} opacity={opacity}/>
    </marker>
  );
  return (
    <defs>
      {Object.entries(COLOR_HEX).map(([k, v]) => make(`arrow-${k}-on`, v, 1))}
      {Object.entries(COLOR_HEX).map(([k, v]) => make(`arrow-${k}`,    v, 0.55))}
    </defs>
  );
}

function PortDots({ mode, beat }) {
  const active = getActive(mode, beat);
  // little dots at boundary crossings — emphasize the trust boundary
  const dots = [
    { x: 304, y: 100, color: "mint", show: active.includes("agentToMcp") },
    { x: 1024, y: 220, color: "mint", show: active.includes("mcpToDb") },
    { x: 1024, y: 360, color: "mint", show: active.includes("dbToAudit") },
    { x: 304, y: 500, color: "mint", show: active.includes("auditToAgent") },
    { x: 304, y: 282, color: "red",  show: active.includes("piiToAgent") },
    { x: 1024, y: 282, color: "cyan", show: active.includes("dbToEmbedder") },
  ];
  return (
    <g>
      {dots.map((dot, i) => (
        <g key={i}>
          <circle cx={dot.x} cy={dot.y} r={dot.show ? 6 : 3}
                  fill={COLOR_HEX[dot.color]}
                  opacity={dot.show ? 1 : 0.35}
                  style={{
                    filter: dot.show ? `drop-shadow(0 0 8px ${COLOR_HEX[dot.color]})` : "none",
                    transition: "all 240ms"
                  }}/>
          {dot.show && (
            <circle cx={dot.x} cy={dot.y} r={10}
                    fill="none" stroke={COLOR_HEX[dot.color]} strokeWidth="1" opacity="0.7">
              <animate attributeName="r" from="6" to="18" dur="1.2s" repeatCount="indefinite"/>
              <animate attributeName="opacity" from="0.7" to="0" dur="1.2s" repeatCount="indefinite"/>
            </circle>
          )}
        </g>
      ))}
    </g>
  );
}

function ConnectorLayer({ mode, beat }) {
  const active = getActive(mode, beat);
  return (
    <svg viewBox="0 0 1328 660" className="connectors" preserveAspectRatio="none">
      <ArrowMarkers />
      {Object.entries(SEGMENTS).map(([id, seg]) => (
        <Segment
          key={id}
          id={id}
          d={seg.d}
          color={seg.color}
          label={seg.label}
          active={active.includes(id)}
        />
      ))}
      <PortDots mode={mode} beat={beat} />
    </svg>
  );
}

Object.assign(window, { ConnectorLayer, FLOW_SCRIPT, SEGMENTS });
