import Link from "next/link";
import { BrainMark } from "@/components/BrainMark";
import { Icon } from "@/components/Icon";
import { LICENSE_LABEL } from "@/lib/positioning";

const GITHUB_URL = "https://github.com/Arun-kc/schemabrain";

/** Site-wide footer, mirroring the landing footer chrome for sub-pages. */
export function SiteFooter() {
  return (
    <footer className="ld-footer">
      <div className="ld-wrap ld-footer-in">
        <Link className="ld-brand" href="/">
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
        </Link>
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
