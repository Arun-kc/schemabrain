import type { Metadata } from "next";
import Link from "next/link";
import { Icon } from "@/components/Icon";
import { formatPostDate, getAllPosts } from "./_registry";

export const metadata: Metadata = {
  title: "Blog — SchemaBrain",
  description:
    "Writing on safe, semantic database access for AI agents — from the maker of the trust and intelligence layer between agents and Postgres.",
};

export default function BlogIndexPage() {
  const posts = getAllPosts();
  return (
    <main className="bl-index">
      <header className="ld-wrap bl-index-head">
        <div className="ek">● Writing</div>
        <h1>Notes from the trust boundary</h1>
        <p>
          Essays on giving AI agents safe, semantic access to a real database — the design decisions,
          the refusals, and the things that broke.
        </p>
      </header>

      <div className="ld-wrap bl-list">
        {posts.map((post) => (
          <Link className="bl-card" key={post.meta.slug} href={`/blog/${post.meta.slug}`}>
            <div className="bl-card-tags">
              {post.meta.tags.map((tag) => (
                <span className="bl-tag" key={tag}>
                  {tag}
                </span>
              ))}
            </div>
            <h2>{post.meta.title}</h2>
            <p className="bl-card-dek">{post.meta.dek}</p>
            <div className="bl-card-meta">
              <span>{formatPostDate(post.meta.date)}</span>
              <span className="dot">·</span>
              <span>{post.meta.readingMinutes} min read</span>
              <span className="bl-card-arrow">
                <Icon name="arrow-right" size={15} />
              </span>
            </div>
          </Link>
        ))}
      </div>
    </main>
  );
}
