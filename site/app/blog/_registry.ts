import type { ComponentType } from "react";
import WhyIBuiltSchemaBrain, {
  meta as whyIBuiltSchemaBrainMeta,
} from "./_posts/why-i-built-schemabrain";

/** Metadata for a single blog post — drives the index card and the article header. */
export interface BlogPostMeta {
  /** URL segment: /blog/<slug>. */
  slug: string;
  title: string;
  /** One-paragraph deck shown under the title and used as the meta description. */
  dek: string;
  /** ISO date (yyyy-mm-dd) — sorted on, formatted deterministically for display. */
  date: string;
  readingMinutes: number;
  tags: string[];
  author: string;
}

/** A registered post: its metadata plus the React component that renders its body. */
export interface BlogPost {
  meta: BlogPostMeta;
  Body: ComponentType;
}

// Add new posts here. The body lives in ./_posts/<slug>.tsx as a default-export
// component; its `meta` is the named export.
const POSTS: BlogPost[] = [
  { meta: whyIBuiltSchemaBrainMeta, Body: WhyIBuiltSchemaBrain },
];

/** All posts, newest first. */
export function getAllPosts(): BlogPost[] {
  return [...POSTS].sort((a, b) => (a.meta.date < b.meta.date ? 1 : -1));
}

export function getPostBySlug(slug: string): BlogPost | undefined {
  return POSTS.find((post) => post.meta.slug === slug);
}

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/**
 * Formats an ISO date (yyyy-mm-dd) as "Mon D, YYYY" without touching the
 * locale-dependent Date APIs — keeps server and client output identical so
 * there is no hydration mismatch.
 */
export function formatPostDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  return `${MONTHS[month - 1]} ${day}, ${year}`;
}
