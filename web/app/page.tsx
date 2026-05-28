"use client";

import Link from "next/link";
import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export default function HomePage() {
  const [theme, setTheme] = useState<"light" | "dark">("light");

  // Read theme from document attributes (synced with the layout script)
  useEffect(() => {
    const observer = new MutationObserver(() => {
      const isDark = document.documentElement.getAttribute("data-theme") === "dark";
      setTheme(isDark ? "dark" : "light");
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    
    const initialDark = document.documentElement.getAttribute("data-theme") === "dark";
    setTheme(initialDark ? "dark" : "light");

    return () => observer.disconnect();
  }, []);

  // Fetch live stats from the active sidecar
  const matrixQuery = useQuery({
    queryKey: ["pii-matrix"],
    queryFn: () => api.piiMatrix(),
  });

  const refusalsQuery = useQuery({
    queryKey: ["audit-refusals"],
    queryFn: () => api.auditRefusals(),
  });

  const auditQuery = useQuery({
    queryKey: ["audit-verify"],
    queryFn: () => api.auditVerify(),
  });

  // While loading, render `—` (set by the renderers below) instead of
  // hardcoded placeholder numbers. The prior fallbacks (2, 2) made a
  // fresh install look like fake demo data was already in the store.
  const catastrophicCount = matrixQuery.data?.totals.catastrophic_columns ?? null;
  const refusalsCount = refusalsQuery.data?.total ?? null;
  const isChainIntact = auditQuery.data?.status === "intact";

  return (
    <main className="relative min-h-screen bg-(--surface-base) px-rhythm-wide py-section overflow-hidden">
      {/* Visual background grid backdrop */}
      <div className="absolute inset-0 grid-bg pointer-events-none" />

      {/* Decorative background radial glows */}
      <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-[#3ECF8E]/8 rounded-full blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-[#3ECF8E]/4 rounded-full blur-[120px] pointer-events-none" />

      <div className="relative mx-auto max-w-5xl">
        {/* Header brand strip */}
        <header className="mb-section">
          <div className="flex items-center justify-between flex-wrap gap-4 border-b border-(--border-subtle) pb-8 mb-12">
            <Logo theme={theme} />
            <div className="flex items-center gap-3">
              <span className="font-mono text-xs uppercase tracking-widest text-(--text-muted) bg-(--surface-raised) border border-(--border-subtle) px-3 py-1 rounded">
                v0.4.0 · STABLE
              </span>
              {/* Simple theme switch trigger for visual completeness */}
              <button 
                onClick={() => {
                  const nextTheme = theme === "dark" ? "light" : "dark";
                  document.documentElement.setAttribute("data-theme", nextTheme);
                  localStorage.setItem("schemabrain.theme", nextTheme);
                  setTheme(nextTheme);
                }}
                className="p-2 rounded border border-(--border-subtle) bg-(--surface-raised) hover:bg-(--surface-base) transition-colors text-sm cursor-pointer"
                aria-label="Toggle theme"
              >
                {theme === "dark" ? "☀" : "☾"}
              </button>
            </div>
          </div>

          <div className="max-w-3xl mt-8">
            <p className="font-mono text-xs uppercase tracking-widest text-[#3ECF8E] font-semibold mb-3">
              // SECURING THE INTERFACE BETWEEN LLMS & SQL
            </p>
            <h1 className="font-display text-4xl sm:text-6xl font-semibold leading-[1.05] tracking-tight text-(--text-primary)">
              What the SQL firewall sees.
            </h1>
            <p className="mt-rhythm-base max-w-2xl text-lg text-(--text-secondary) leading-relaxed">
              Read-only visual telemetry into categorized database entities, refused tool calls, 
              and the cryptographic audit ledger that anchors every database operation.
            </p>
          </div>
        </header>

        {/* Animated SQL Firewall Pipeline Section */}
        <section className="mb-16 border border-(--border-subtle) bg-(--surface-raised)/40 backdrop-blur-md p-8 rounded-lg relative overflow-hidden glass-panel">
          <div className="absolute top-0 left-0 w-full h-[3px] bg-gradient-to-r from-transparent via-[#3ECF8E]/30 to-transparent" />
          <h2 className="font-mono text-xs uppercase tracking-wider text-(--text-muted) mb-6 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-[#3ECF8E] animate-pulse" />
            ACTIVE PIPELINE TELEMETRY
          </h2>
          <PipelineDiagram refusalsCount={refusalsCount} />
        </section>

        {/* Bento Grid cards for the surfaces */}
        <section className="grid grid-cols-1 gap-rhythm-wide md:grid-cols-3">
          <BentoCard
            href="/pii"
            eyebrow="SURFACE 01"
            title="PII Visualization"
            description="Which columns carry catastrophic-leak categories — and which database entities will trigger automatic tool refusals."
            metric={`${catastrophicCount ?? "—"} Catastrophic Cols`}
            status="ACTIVE"
            icon={<TableIcon />}
          />
          <BentoCard
            href="/refusals"
            eyebrow="SURFACE 02"
            title="Refusal Experience"
            description="Live feed of refused database queries, showcasing the detailed recovery envelopes LLMs use to self-correct."
            metric={`${refusalsCount ?? "—"} Refused Queries`}
            status="ACTIVE"
            disabled={false}
            icon={<WarningIcon />}
          />
          <BentoCard
            href="/audit"
            eyebrow="SURFACE 03"
            title="Audit Viewer"
            description="A cryptographic ledger chain of all executed database operations, verifying tamper-evidence via SHA-256 links."
            metric={isChainIntact ? "Ledger Verified Intact" : "Ledger Verification"}
            status="ACTIVE"
            disabled={false}
            icon={<ShieldIcon />}
          />
        </section>

        <footer className="mt-20 border-t border-(--border-subtle) pt-6 flex items-center justify-between text-xs font-mono text-(--text-muted)">
          <span>schemabrain · localhost only</span>
          <span>localhost connection only</span>
        </footer>
      </div>
    </main>
  );
}

/* ─────────── PREMIUM INLINE SVG LOGO ─────────── */

function Logo({ theme }: { theme: "light" | "dark" }) {
  const isDark = theme === "dark";
  const strokeColor = isDark ? "#FAFAF7" : "#0C0C0C";
  const innerDotColor = isDark ? "#0B0F14" : "#FAFAF7";

  return (
    <div className="flex items-center gap-3 select-none">
      <svg width="40" height="40" viewBox="0 0 64 64" overflow="visible" className="shrink-0">
        <defs>
          <clipPath id="clip-logo">
            <path d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z"></path>
          </clipPath>
        </defs>
        
        {/* Glowing aura under the logo */}
        <path 
          d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z" 
          fill="#3ECF8E" 
          opacity="0.15" 
          className="animate-pulse"
        />

        {/* Half Green Fill */}
        <g clipPath="url(#clip-logo)">
          <rect x="32" y="0" width="32" height="64" fill="#3ECF8E" />
        </g>

        {/* Logo Outlines */}
        <path 
          d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z" 
          stroke={strokeColor} 
          strokeWidth="2.2" 
          strokeLinejoin="round" 
          fill="none"
        />
        <line x1="32" y1="10" x2="32" y2="52" stroke={strokeColor} strokeWidth="1.6" strokeLinecap="round" />
        
        {/* Wavy lines on left (database rows) */}
        <path d="M14 22 C 20 19, 26 22, 30 21" stroke={strokeColor} strokeWidth="1.4" strokeLinecap="round" fill="none" />
        <path d="M12 32 C 18 28, 26 33, 30 31" stroke={strokeColor} strokeWidth="1.4" strokeLinecap="round" fill="none" />
        <path d="M14 42 C 20 39, 26 43, 30 41" stroke={strokeColor} strokeWidth="1.4" strokeLinecap="round" fill="none" />
        
        {/* Dots on green side (AI nodes) */}
        <g clipPath="url(#clip-logo)">
          <circle cx="36" cy="22" r="1.4" fill={innerDotColor} />
          <line x1="38.5" y1="22" x2="50" y2="22" stroke={innerDotColor} strokeWidth="1.6" strokeLinecap="round" />
          <circle cx="36" cy="32" r="1.4" fill={innerDotColor} />
          <line x1="38.5" y1="32" x2="52" y2="32" stroke={innerDotColor} strokeWidth="1.6" strokeLinecap="round" />
          <circle cx="36" cy="42" r="1.4" fill={innerDotColor} />
          <line x1="38.5" y1="42" x2="50" y2="42" stroke={innerDotColor} strokeWidth="1.6" strokeLinecap="round" />
        </g>
      </svg>
      <span className="font-mono text-xl font-bold tracking-[-0.04em] text-(--text-primary) select-none">
        schemabrain
      </span>
    </div>
  );
}

/* ─────────── DYNAMIC ANIMATED PIPELINE DIAGRAM ─────────── */

function PipelineDiagram({ refusalsCount }: { refusalsCount: number | null }) {
  return (
    <div className="w-full overflow-x-auto select-none py-4">
      <div className="min-w-[640px] flex items-center justify-between px-4 relative">
        
        {/* AI Agent Node */}
        <div className="flex flex-col items-center gap-2 z-10">
          <div className="h-16 w-16 rounded-lg bg-(--surface-base) border border-(--border-subtle) flex items-center justify-center relative shadow-sm">
            <svg className="w-8 h-8 text-(--text-secondary)" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
            </svg>
            <div className="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-[#3ECF8E]" />
          </div>
          <span className="font-mono text-xs text-(--text-secondary) font-medium">AI AGENT</span>
          <span className="font-mono text-[10px] text-(--text-muted)">Generates SQL</span>
        </div>

        {/* Animated Connecting Line 1 */}
        <div className="flex-1 h-[2px] mx-4 relative overflow-hidden bg-(--border-subtle)">
          <div 
            className="absolute inset-0 h-full w-[200%] bg-gradient-to-r from-transparent via-[#3ECF8E] to-transparent"
            style={{
              backgroundImage: "linear-gradient(90deg, transparent, #3ECF8E, transparent)",
              animation: "flowRight 2.5s infinite linear"
            }}
          />
        </div>

        {/* SchemaBrain SQL Firewall Node */}
        <div className="flex flex-col items-center gap-2 z-10 group/firewall relative">
          <div className="h-20 w-20 rounded-full bg-(--surface-base) border-2 border-[#3ECF8E] flex items-center justify-center relative shadow-lg shadow-[#3ECF8E]/10 transition-transform group-hover/firewall:scale-105 duration-300">
            <div className="absolute inset-2 rounded-full border border-[#3ECF8E]/40 animate-ping opacity-60" />
            <svg className="w-10 h-10 text-[#3ECF8E] animate-pulse" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            <div className="absolute -bottom-1.5 font-mono text-[9px] bg-[#3ECF8E] text-[#0B0F14] px-2 py-0.5 rounded font-bold uppercase tracking-widest shadow-sm">
              FIREWALL
            </div>
          </div>
          <span className="font-mono text-xs text-(--text-primary) font-semibold">SCHEMABRAIN</span>
          <span className="font-mono text-[10px] text-[#3ECF8E] font-semibold animate-pulse">
            {refusalsCount !== null && refusalsCount > 0
              ? `// ${refusalsCount} REFUSALS BLOCKED`
              : "// ACTIVE GUARD"}
          </span>
        </div>

        {/* Animated Connecting Line 2 */}
        <div className="flex-1 h-[2px] mx-4 relative overflow-hidden bg-(--border-subtle)">
          <div 
            className="absolute inset-0 h-full w-[200%] bg-gradient-to-r from-transparent via-[#3ECF8E] to-transparent"
            style={{
              backgroundImage: "linear-gradient(90deg, transparent, #3ECF8E, transparent)",
              animation: "flowRight 2.5s infinite linear",
              animationDelay: "1.25s"
            }}
          />
        </div>

        {/* Database Node */}
        <div className="flex flex-col items-center gap-2 z-10">
          <div className="h-16 w-16 rounded-lg bg-(--surface-base) border border-(--border-subtle) flex items-center justify-center relative shadow-sm">
            <svg className="w-8 h-8 text-(--text-secondary)" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
            </svg>
            <div className="absolute -bottom-1 -right-1 w-2.5 h-2.5 rounded-full bg-[#3ECF8E]" />
          </div>
          <span className="font-mono text-xs text-(--text-secondary) font-medium">DATABASE</span>
          <span className="font-mono text-[10px] text-(--text-muted)">Postgres / SQLite</span>
        </div>

      </div>

      <style dangerouslySetInnerHTML={{ __html: `
        @keyframes flowRight {
          0% { transform: translateX(-100%); }
          100% { transform: translateX(50%); }
        }
      `}} />
    </div>
  );
}

/* ─────────── BENTO-GRID SURFACE CARD ─────────── */

interface BentoCardProps {
  href: string;
  eyebrow: string;
  title: string;
  description: string;
  metric: string;
  status: string;
  disabled?: boolean;
  icon: React.ReactNode;
}

function BentoCard({
  href,
  eyebrow,
  title,
  description,
  metric,
  status,
  disabled = false,
  icon,
}: BentoCardProps) {
  const cardContent = (
    <>
      {/* Decorative vertical colored stripe for active surface */}
      {!disabled && (
        <div className="absolute left-0 top-0 bottom-0 w-[3px] bg-[#3ECF8E] scale-y-0 group-hover:scale-y-100 origin-bottom transition-transform duration-normal ease-out-expo" />
      )}

      {/* Grid Pattern in Card background */}
      <div className="absolute inset-0 grid-bg opacity-5 pointer-events-none" />

      <div className="flex items-start justify-between mb-8">
        <div>
          <span className="font-mono text-[10px] uppercase tracking-widest text-(--text-muted)">
            {eyebrow}
          </span>
          <h3 className="mt-1 font-display text-2xl font-semibold text-(--text-primary) tracking-tight group-hover:text-[#3ECF8E] transition-colors">
            {title}
          </h3>
        </div>
        <div className={`p-2 rounded bg-(--surface-base) border border-(--border-subtle) ${
          !disabled && "group-hover:border-[#3ECF8E]/40"
        } transition-colors`}>
          {icon}
        </div>
      </div>

      <p className="text-sm text-(--text-secondary) leading-relaxed min-h-[72px]">
        {description}
      </p>

      <div className="mt-6 pt-4 border-t border-(--border-subtle) flex items-center justify-between">
        <span className="font-mono text-xs text-(--text-muted) flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${disabled ? "bg-amber-500/60" : "bg-[#3ECF8E] animate-pulse"}`} />
          {status}
        </span>
        <span className="font-mono text-xs text-(--text-primary) font-semibold">
          {metric}
        </span>
      </div>

      {!disabled && (
        <div className="absolute bottom-4 right-4 flex items-center gap-1 text-[#3ECF8E] opacity-0 group-hover:opacity-100 group-hover:translate-x-1 transition-all duration-normal ease-out-expo font-mono text-xs">
          OPEN →
        </div>
      )}
    </>
  );

  if (disabled) {
    return (
      <div className="group block relative p-6 rounded-lg glass-panel hover-glow overflow-hidden transition-all duration-normal ease-out-expo opacity-75 cursor-not-allowed border-(--border-subtle)">
        {cardContent}
      </div>
    );
  }

  return (
    <Link
      href={href}
      className="group block relative p-6 rounded-lg glass-panel hover-glow overflow-hidden transition-all duration-normal ease-out-expo cursor-pointer"
    >
      {cardContent}
    </Link>
  );
}

/* ─────────── UTILITY SVG ICONS ─────────── */

function TableIcon() {
  return (
    <svg className="w-5 h-5 text-[#3ECF8E]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 10h18M3 14h18m-9-4v8m-7 0h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
    </svg>
  );
}

function WarningIcon() {
  return (
    <svg className="w-5 h-5 text-amber-500/70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
    </svg>
  );
}

function ShieldIcon() {
  return (
    <svg className="w-5 h-5 text-indigo-500/70" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
    </svg>
  );
}
