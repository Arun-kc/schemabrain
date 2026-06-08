/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export — the resulting `out/` is copied into
  // `schemabrain/dashboard/static/` at wheel-build time and served
  // by the FastAPI sidecar at `/`. No Node runtime ever runs in
  // production; the sidecar serves bytes.
  output: "export",
  // The sidecar serves `index.html` for `/` and falls back to it
  // for unknown routes via the StaticFiles `html=True` flag, so
  // Next.js trailingSlash behaviour stays default.
  trailingSlash: false,
  reactStrictMode: true,
  // The sidecar serves at the root, no asset prefix needed.
  // Images: we ship a tiny set of inline SVGs (trust badges, logos);
  // no <Image /> optimization needed. Disable the optimizer so the
  // export step does not require sharp at build time.
  images: {
    unoptimized: true,
  },
  // Dev only: proxy /api/* to the running FastAPI sidecar so
  // contributors can run both servers side-by-side.
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:7878/api/:path*",
      },
    ];
  },
  // Deterministic build id (issue #185). A static export has no server
  // runtime to mint per-request CSP nonces, so the sidecar hash-pins
  // script-src instead (see web/scripts/inject-csp-hashes.mjs +
  // schemabrain/dashboard/sidecar.py build_content_security_policy). The
  // ONLY inline-script bytes that vary build-to-build are Next's random
  // build id, which surfaces in the RSC-flight payload as `"b":"<id>"`.
  // Pinning it to a fixed value makes every inline <script> byte-stable,
  // so the same source produces the same hashes across builds. (style-src
  // still allows 'unsafe-inline' — React inline style="" attrs fall under
  // style-src-attr, which hashes can't cover; out of scope for #185.)
  generateBuildId: () => "schemabrain",
};

export default nextConfig;
