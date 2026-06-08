import type { LucideIcon } from "lucide-react";
import {
  ArrowRight,
  ArrowUpRight,
  Ban,
  BookOpen,
  Bot,
  Boxes,
  Check,
  ChevronDown,
  CircleCheckBig,
  Copy,
  Database,
  EyeOff,
  FileCode,
  Frame,
  GitCompare,
  GitMerge,
  GitPullRequest,
  Github,
  Grid3x3,
  LayoutDashboard,
  Link,
  Loader,
  Lock,
  Moon,
  Radar,
  RotateCw,
  Route,
  Scale,
  ScrollText,
  Search,
  Shield,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Sun,
  Table2,
  Terminal,
  Twitter,
  User,
  Waypoints,
  X,
} from "lucide-react";

/**
 * The bundled icon set.
 *
 * The design prototype pulled icons from the Lucide CDN global and injected
 * them with dangerouslySetInnerHTML. Here we import the React components from
 * `lucide-react` (tree-shaken, no CDN, no innerHTML). Every kebab name the
 * handoff referenced resolves; `viewport` (a graph fit-to-view affordance with
 * no Lucide equivalent) maps to `Frame`.
 */
const ICONS = {
  "arrow-right": ArrowRight,
  "arrow-up-right": ArrowUpRight,
  ban: Ban,
  "book-open": BookOpen,
  bot: Bot,
  boxes: Boxes,
  check: Check,
  "chevron-down": ChevronDown,
  "circle-check-big": CircleCheckBig,
  copy: Copy,
  database: Database,
  "eye-off": EyeOff,
  "file-code": FileCode,
  "git-compare": GitCompare,
  "git-merge": GitMerge,
  "git-pull-request": GitPullRequest,
  github: Github,
  "grid-3x3": Grid3x3,
  "layout-dashboard": LayoutDashboard,
  link: Link,
  loader: Loader,
  lock: Lock,
  moon: Moon,
  radar: Radar,
  "rotate-cw": RotateCw,
  route: Route,
  scale: Scale,
  "scroll-text": ScrollText,
  search: Search,
  shield: Shield,
  "shield-check": ShieldCheck,
  "sliders-horizontal": SlidersHorizontal,
  sparkles: Sparkles,
  sun: Sun,
  "table-2": Table2,
  terminal: Terminal,
  twitter: Twitter,
  user: User,
  viewport: Frame,
  waypoints: Waypoints,
  x: X,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof ICONS;

/** Stable, sorted list of every available icon name (used by tests/tooling). */
export const ICON_NAMES = Object.keys(ICONS) as IconName[];

interface IconProps {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
  /**
   * Accessible name. When provided the glyph is exposed to assistive tech as
   * an image; otherwise it is marked decorative (aria-hidden) so it never
   * adds noise to a labelled control.
   */
  label?: string;
}

export function Icon({ name, size = 16, strokeWidth = 2, className, label }: IconProps) {
  const Glyph = ICONS[name];
  return (
    <Glyph
      size={size}
      strokeWidth={strokeWidth}
      className={className}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}
