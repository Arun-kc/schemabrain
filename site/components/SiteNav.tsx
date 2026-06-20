"use client";

import Link from "next/link";
import { BrainMark } from "@/components/BrainMark";
import { Icon } from "@/components/Icon";
import { useTheme } from "@/lib/use-theme";

const GITHUB_URL = "https://github.com/Arun-kc/schemabrain";
const DOCS_URL = "https://schemabrain.mintlify.app/";

/**
 * Site-wide nav for pages other than the landing (e.g. /blog).
 *
 * Reuses the landing's `.ld-nav` chrome and the shared theme toggle, but its
 * section links are absolute ("/#…") so they resolve from any route, the brand
 * returns home, and a "Blog" entry is added. Internal routes use next/link;
 * the external Docs/GitHub links stay as plain anchors. The landing keeps its
 * own inline <Nav> (its anchors are same-page) so its smoke test is untouched.
 */
export function SiteNav() {
  const { theme, toggle } = useTheme();
  return (
    <nav className="ld-nav">
      <div className="ld-wrap ld-nav-in">
        <Link className="ld-brand" href="/">
          <BrainMark size={24} />
          <span className="wm">schemabrain</span>
        </Link>
        <div className="ld-links">
          <Link href="/#intelligence">Intelligence</Link>
          <Link href="/#how">How it works</Link>
          <Link href="/#safety">Safety</Link>
          <Link href="/blog">Blog</Link>
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
          <Link className="sb-btn primary" href="/#install">
            Get started <Icon name="arrow-right" size={14} />
          </Link>
        </div>
      </div>
    </nav>
  );
}
