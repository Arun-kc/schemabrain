import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { Icon } from "@/components/Icon";
import { INSTALL_PRIMARY } from "@/lib/positioning";
import { formatPostDate, getAllPosts, getPostBySlug } from "../_registry";

const GITHUB_URL = "https://github.com/Arun-kc/schemabrain";
const DOCS_URL = "https://schemabrain.mintlify.app/";

interface PageProps {
  params: Promise<{ slug: string }>;
}

export function generateStaticParams() {
  return getAllPosts().map((post) => ({ slug: post.meta.slug }));
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { slug } = await params;
  const post = getPostBySlug(slug);
  if (!post) return { title: "Not found — SchemaBrain" };
  return {
    title: `${post.meta.title} — SchemaBrain`,
    description: post.meta.dek,
  };
}

export default async function BlogPostPage({ params }: PageProps) {
  const { slug } = await params;
  const post = getPostBySlug(slug);
  if (!post) notFound();

  const { meta, Body } = post;
  return (
    <main className="bl-article">
      <div className="bl-article-grid" aria-hidden />
      <article className="ld-wrap bl-article-in">
        <Link className="bl-back" href="/blog">
          <Icon name="arrow-right" size={14} style={{ transform: "rotate(180deg)" }} /> All writing
        </Link>

        <div className="bl-art-tags">
          {meta.tags.map((tag) => (
            <span className="bl-tag" key={tag}>
              {tag}
            </span>
          ))}
        </div>
        <h1 className="bl-title">{meta.title}</h1>
        <p className="bl-dek">{meta.dek}</p>
        <div className="bl-byline">
          <span>{meta.author}</span>
          <span className="dot">·</span>
          <span>{formatPostDate(meta.date)}</span>
          <span className="dot">·</span>
          <span>{meta.readingMinutes} min read</span>
        </div>

        <hr className="bl-rule" />

        <div className="bl-prose">
          <Body />
        </div>

        <aside className="bl-cta">
          <div className="bl-cta-text">
            <h3>Pointing agents at a real Postgres?</h3>
            <p>Watch the whole refuse-then-recover loop in one command — $0, no API key.</p>
          </div>
          <div className="bl-cta-actions">
            <code className="bl-cta-cmd">{INSTALL_PRIMARY}</code>
            <div className="bl-cta-btns">
              <a className="sb-btn primary" href={GITHUB_URL} target="_blank" rel="noreferrer">
                <Icon name="github" size={14} /> GitHub
              </a>
              <a className="sb-btn" href={DOCS_URL} target="_blank" rel="noreferrer">
                <Icon name="book-open" size={14} /> Docs
              </a>
            </div>
          </div>
        </aside>
      </article>
    </main>
  );
}
