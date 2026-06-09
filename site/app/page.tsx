"use client";

import { useEffect, useState, type ReactNode } from "react";
import { BrainMark } from "@/components/BrainMark";
import { Icon } from "@/components/Icon";
import { INSTALL_PRIMARY, LICENSE_LABEL, TAGLINE } from "@/lib/positioning";

/* Faithful port of the design handoff (landing/landing-app.jsx). Honest
 * adaptations vs. the prototype:
 *  - license reads LICENSE_LABEL (Apache-2.0), not the prototype's "MIT"
 *  - the fabricated "4.2k" GitHub star count is dropped
 *  - dashboard CTAs (the prototype linked ../app/index.html) point at the repo
 *    and docs — the dashboard is a local sidecar, not a sibling static page
 *  - the hero H1 / install command come from the canonical positioning strings
 *    (single source, guarded by the TS↔Python parity test)
 */

const GITHUB_URL = "https://github.com/Arun-kc/schemabrain";
const DOCS_URL = "https://schemabrain.mintlify.app/";

// Split the canonical tagline so the middle phrase can be accent-colored
// without duplicating the string.
const HERO_PHRASE = "trust and intelligence layer";
const [HERO_PRE, HERO_POST] = TAGLINE.split(HERO_PHRASE);

type Theme = "dark" | "light";

function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>("dark");
  useEffect(() => {
    setTheme(
      document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light",
    );
  }, []);
  const toggle = () => {
    setTheme((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      if (next === "dark") document.documentElement.setAttribute("data-theme", "dark");
      else document.documentElement.removeAttribute("data-theme");
      try {
        localStorage.setItem("schemabrain.theme", next);
      } catch {
        /* private mode / storage disabled — theme just won't persist */
      }
      return next;
    });
  };
  return { theme, toggle };
}

function Nav({ theme, toggle }: { theme: Theme; toggle: () => void }) {
  return (
    <nav className="ld-nav">
      <div className="ld-wrap ld-nav-in">
        <a className="ld-brand" href="#top">
          <BrainMark size={24} />
          <span className="wm">schemabrain</span>
        </a>
        <div className="ld-links">
          <a href="#intelligence">Intelligence</a>
          <a href="#how">How it works</a>
          <a href="#safety">Safety</a>
          <a href={DOCS_URL}>Docs</a>
        </div>
        <div className="ld-nav-right">
          <button
            className="sb-iconbtn"
            type="button"
            title="Toggle theme"
            aria-label="Toggle color theme"
            onClick={toggle}
          >
            <Icon name={theme === "dark" ? "sun" : "moon"} size={16} />
          </button>
          <a className="sb-btn" href={GITHUB_URL} target="_blank" rel="noreferrer">
            <Icon name="github" size={14} /> GitHub
          </a>
          <a className="sb-btn primary" href="#install">
            Get started <Icon name="arrow-right" size={14} />
          </a>
        </div>
      </div>
    </nav>
  );
}

type Stage = { name: string; icon: string; color: string; desc: string };

const STAGES: Stage[] = [
  { name: "inspection", icon: "scan-search", color: "var(--cyan)", desc: "classify PII · resolve schema" },
  { name: "trust", icon: "shield-check", color: "var(--green)", desc: "policy · refusal · audit" },
  { name: "execution", icon: "terminal", color: "var(--green)", desc: "compile read-only SQL" },
  { name: "intelligence", icon: "sparkles", color: "var(--violet)", desc: "semantic · graph · RAG" },
];

function MentalModel() {
  return (
    <div className="ld-mental">
      <div className="ld-mm">
        <div className="ld-mm-node">
          <span className="ld-mm-ep">
            <Icon name="bot" size={22} />
          </span>
          <span className="lab">AI AGENT</span>
          <span className="sub">claude · cursor · zed</span>
        </div>

        <div className="ld-mm-arrow a1">
          <div className="t">
            MCP tools
            <br />
            (read-only)
          </div>
          <div className="ln">
            <span className="flow" />
            <span className="tip">▶</span>
          </div>
        </div>

        <div className="ld-mm-core">
          <div className="chead">
            <BrainMark size={22} />
            <span className="wm">schemabrain</span>
            <span className="bnd">
              <span className="live" />
              TRUST BOUNDARY
            </span>
          </div>
          <div className="ld-mm-pipe">
            {STAGES.map((stage, i) => (
              <div className="ld-mm-stage" key={stage.name}>
                <span
                  className="si"
                  style={{
                    color: stage.color,
                    borderColor: `color-mix(in oklch, ${stage.color} 40%, transparent)`,
                  }}
                >
                  <Icon name={stage.icon} size={15} />
                </span>
                <span className="stx">
                  <span className="sname">{stage.name}</span>
                  <span className="sdesc">{stage.desc}</span>
                </span>
                <span className="sn">0{i + 1}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="ld-mm-arrow a2">
          <div className="t">
            compiled
            <br />
            read-only SQL
          </div>
          <div className="ln">
            <span className="flow" />
            <span className="tip">▶</span>
          </div>
        </div>

        <div className="ld-mm-node">
          <span className="ld-mm-ep">
            <Icon name="database" size={22} />
          </span>
          <span className="lab">YOUR DATABASE</span>
          <span className="sub">postgres · sqlite</span>
          <span className="pin">read_only = ON</span>
        </div>
      </div>
    </div>
  );
}

function Hero() {
  return (
    <header className="ld-hero" id="top">
      <div className="ld-hero-grid" />
      <div className="ld-hero-glow" />
      <div className="ld-wrap ld-hero-in">
        <span className="ld-eyebrow">
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: "var(--green)",
              display: "inline-block",
            }}
          />{" "}
          open source · {LICENSE_LABEL} · MCP-native
        </span>
        <h1>
          {HERO_PRE}
          <span className="g">{HERO_PHRASE}</span>
          {HERO_POST}
        </h1>
        <p className="lede">
          Like Cloudflare sits between the internet and your servers, SchemaBrain sits between
          AI agents and your database — making your schema understandable, keeping access safe,
          and keeping every query trustworthy.
        </p>
        <div className="cta">
          <a className="sb-btn primary ld-btn-lg" href="#install">
            <Icon name="terminal" size={15} /> {INSTALL_PRIMARY}
          </a>
          <a className="sb-btn ld-btn-lg" href={DOCS_URL}>
            Read the docs <Icon name="arrow-right" size={15} />
          </a>
        </div>
        <div className="tags">
          <span>12 read-only tools</span>
          <span>·</span>
          <span>local vector embedder</span>
          <span>·</span>
          <span>SHA-256 audit</span>
        </div>
        <MentalModel />
      </div>
    </header>
  );
}

type Layer = {
  n: string;
  icon: string;
  color: string;
  title: string;
  body: string;
  ex: ReactNode;
};

const LAYERS: Layer[] = [
  {
    n: "01",
    icon: "scan-search",
    color: "var(--cyan)",
    title: "Semantic understanding",
    body: "Every column gets an AI-generated meaning, and real business entities are identified out of raw tables — each with a rationale and a confidence band.",
    ex: (
      <span>
        <span className="g">user</span> · bound entity · <span className="c">conf high</span> —
        “join hub for orders, sessions, billing”
      </span>
    ),
  },
  {
    n: "02",
    icon: "git-merge",
    color: "var(--violet)",
    title: "Relationship intelligence",
    body: "Declared foreign keys plus join paths mined from real query logs — surfacing relationships the schema never declared, with cardinality.",
    ex: (
      <span>
        <span className="c">session → user</span> · log-mined · 1.8M joins observed
      </span>
    ),
  },
  {
    n: "03",
    icon: "waypoints",
    color: "var(--green)",
    title: "The knowledge graph",
    body: "A real, traversable graph with multi-hop join paths and cardinality. Ask for a rollup and get the canonical path back.",
    ex: (
      <span>
        <span className="g">order_item → order → user → tenant</span> · 3 hops
      </span>
    ),
  },
  {
    n: "04",
    icon: "radar",
    color: "var(--cyan)",
    title: "Retrieval (RAG)",
    body: "Local semantic search returns what's relevant and why it matched — all process-local, nothing leaves the box.",
    ex: (
      <span>
        q=“monthly revenue” → <span className="g">revenue 0.91</span> ·{" "}
        <span className="c">sales 0.84</span>
      </span>
    ),
  },
];

function Intelligence() {
  return (
    <section className="ld-section" id="intelligence">
      <div className="ld-wrap">
        <div className="ld-shead">
          <div className="ek">● The intelligence</div>
          <h2>It understands your database deeply — so it can guard it.</h2>
          <p>
            Safety is downstream of understanding. Four layers turn a raw schema into something
            an agent can reason over, and an operator can trust.
          </p>
        </div>
        <div className="ld-layers">
          {LAYERS.map((layer) => (
            <div className="ld-layer" key={layer.n}>
              <div className="num">LAYER {layer.n}</div>
              <div
                className="ic"
                style={{
                  color: layer.color,
                  borderColor: `color-mix(in oklch, ${layer.color} 35%, transparent)`,
                  background: `color-mix(in oklch, ${layer.color} 10%, transparent)`,
                }}
              >
                <Icon name={layer.icon} size={21} />
              </div>
              <h3>{layer.title}</h3>
              <p>{layer.body}</p>
              <div className="ex">{layer.ex}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

const STEPS: [string, string][] = [
  ["discover", "Agent calls one of 12 read-only MCP tools over stdio. SchemaBrain resolves intent against the semantic layer."],
  ["describe", "It classifies and refuses PII, looks up cosine-similar entities, and resolves the canonical join path."],
  ["compute", "It compiles strictly parameterized read-only SQL, runs it, and writes a hash-chained audit row."],
];

function How() {
  return (
    <section className="ld-section alt" id="how">
      <div className="ld-wrap">
        <div className="ld-shead center">
          <div className="ek">● How it works</div>
          <h2>discover → describe → compute</h2>
          <p>The agent never writes SQL. SchemaBrain compiles it from definitions you control.</p>
        </div>
        <div className="ld-flow">
          {STEPS.map(([title, body], i) => (
            <div className="ld-step" key={title}>
              <div className="n">{i + 1}</div>
              {i < 2 && (
                <span className="conn">
                  <Icon name="arrow-right" size={18} />
                </span>
              )}
              <h4>{title}</h4>
              <p>{body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

type Proof = { icon: string; title: string; detail: string; alarm: boolean };

const PROOF: Proof[] = [
  { icon: "shield", title: "PII classification", detail: "Twelve sensitivity categories, scored per column with a confidence band.", alarm: false },
  { icon: "lock", title: "Catastrophic floor", detail: "Credentials, government IDs, and card data are hard-blocked, regardless of policy.", alarm: true },
  { icon: "scale", title: "Editable policy", detail: "Tag or clear any column; preview the diff before it applies.", alarm: false },
  { icon: "fingerprint", title: "Tamper-evident audit", detail: "Every query writes a SHA-256 hash-chained row. Verify the chain in ms.", alarm: false },
  { icon: "file-code", title: "Def-driven compilation", detail: "Agents never write SQL — it's compiled from definitions you own.", alarm: false },
  { icon: "flame", title: "SQL firewall", detail: "Read-only connections, statement timeouts, row caps — one guarantee among many.", alarm: false },
];

function Safety() {
  return (
    <section className="ld-section" id="safety">
      <div className="ld-wrap">
        <div className="ld-shead">
          <div className="ek">● The safety, as proof-points</div>
          <h2>Trust you can show your security team.</h2>
          <p>
            Because SchemaBrain understands the data, every safeguard is precise — not a blunt
            allowlist. The firewall is the floor, not the headline.
          </p>
        </div>
        <div className="ld-proof">
          {PROOF.map((pp) => (
            <div className={"ld-pp" + (pp.alarm ? " alarm" : "")} key={pp.title}>
              <span className="pi">
                <Icon name={pp.icon} size={19} />
              </span>
              <div>
                <h4>{pp.title}</h4>
                <p>{pp.detail}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Install() {
  return (
    <section className="ld-section alt" id="install">
      <div className="ld-wrap ld-install-in">
        <div>
          <div className="ld-shead">
            <div className="ek">● Get started</div>
            <h2>Running in one command.</h2>
            <p>
              Point SchemaBrain at a Postgres URL, add it to your MCP client, and the agent
              talks to a schema it finally understands — safely.
            </p>
          </div>
          <div className="cta" style={{ display: "flex", gap: 12, marginTop: 26, flexWrap: "wrap" }}>
            <a className="sb-btn primary ld-btn-lg" href={GITHUB_URL} target="_blank" rel="noreferrer">
              <Icon name="github" size={14} /> View on GitHub
            </a>
            <a className="sb-btn ld-btn-lg" href={DOCS_URL}>
              <Icon name="book-open" size={14} /> Read the docs
            </a>
          </div>
        </div>
        <div className="ld-term">
          <div className="ld-term-bar">
            <span className="ld-tl" style={{ background: "#FF5F57" }} />
            <span className="ld-tl" style={{ background: "#FEBC2E" }} />
            <span className="ld-tl" style={{ background: "#28C840" }} />
            <span className="ld-term-title">~/acme-saas</span>
          </div>
          <div className="ld-term-body">
            <div>
              <span className="pr">$</span> {INSTALL_PRIMARY}
            </div>
            <div className="cm"># indexing schema · 12 tables · mining joins</div>
            <div>
              <span className="cy">→</span> 38 entities · 94 joins · 26 metrics · 4,612 vecs
            </div>
            <div className="ok">✓ mcp stdio server live · 12 read-only tools</div>
            <div className="cm"># transaction_read_only = ON</div>
          </div>
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="ld-footer">
      <div className="ld-wrap ld-footer-in">
        <a className="ld-brand" href="#top">
          <BrainMark size={22} />
          <span
            className="wm"
            style={{
              fontFamily: "var(--f-mono)",
              fontWeight: 800,
              fontSize: 16,
              letterSpacing: "-0.03em",
            }}
          >
            schemabrain
          </span>
        </a>
        <span className="base">
          — schemabrain · the trust boundary between AI agents and production data · {LICENSE_LABEL}
        </span>
        <span style={{ display: "inline-flex", gap: 16, color: "var(--ink-2)" }}>
          <a href={GITHUB_URL} target="_blank" rel="noreferrer" aria-label="GitHub">
            <Icon name="github" size={17} />
          </a>
        </span>
      </div>
    </footer>
  );
}

export default function LandingPage() {
  const { theme, toggle } = useTheme();
  return (
    <div className="sb-app ld">
      <Nav theme={theme} toggle={toggle} />
      <Hero />
      <Intelligence />
      <How />
      <Safety />
      <Install />
      <Footer />
    </div>
  );
}
