import type { CSSProperties } from "react";
import {
  ArrowRight,
  Bot,
  BookOpen,
  Database,
  FileCode,
  Fingerprint,
  Flame,
  Github,
  GitMerge,
  Lock,
  Menu,
  Moon,
  Radar,
  ScanSearch,
  Scale,
  Shield,
  ShieldCheck,
  Sparkles,
  Sun,
  Terminal,
  Waypoints,
  X,
  type LucideIcon,
} from "lucide-react";

/**
 * Thin wrapper over `lucide-react` mapping the design handoff's kebab-case
 * icon names to tree-shaken named imports. The handoff loaded lucide from a CDN
 * global (`window.lucide`); here we bundle only the icons the landing uses so
 * the JS payload stays small and there is no external script origin.
 *
 * Icons inherit `currentColor`, so callers set color via the parent / `style`.
 */
const ICONS: Record<string, LucideIcon> = {
  "arrow-right": ArrowRight,
  bot: Bot,
  "book-open": BookOpen,
  database: Database,
  "file-code": FileCode,
  fingerprint: Fingerprint,
  flame: Flame,
  github: Github,
  "git-merge": GitMerge,
  lock: Lock,
  menu: Menu,
  moon: Moon,
  radar: Radar,
  "scan-search": ScanSearch,
  scale: Scale,
  shield: Shield,
  "shield-check": ShieldCheck,
  sparkles: Sparkles,
  sun: Sun,
  terminal: Terminal,
  waypoints: Waypoints,
  x: X,
};

interface IconProps {
  name: string;
  size?: number;
  stroke?: number;
  style?: CSSProperties;
}

export function Icon({ name, size = 16, stroke = 2, style }: IconProps) {
  const Glyph = ICONS[name];
  if (!Glyph) return null;
  return <Glyph size={size} strokeWidth={stroke} style={style} aria-hidden />;
}
