/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export — the marketing site is plain static bytes. On Vercel this
  // deploys as a static build (no Node runtime); the output is also portable
  // to any static host (Pages/Cloudflare) by adding a basePath if ever moved
  // off a root domain. It is intentionally NOT bundled into the pip wheel —
  // the wheel ships only the dashboard (see
  // docs/internal/landing_site_separation_plan_2026_06_09.md).
  output: "export",
  trailingSlash: false,
  reactStrictMode: true,
  // The landing ships a small set of inline SVGs (BrainMark, lucide icons);
  // no <Image /> optimization, so the export needs no sharp at build time.
  images: {
    unoptimized: true,
  },
};

export default nextConfig;
