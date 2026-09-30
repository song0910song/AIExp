import type { NextConfig } from "next";

const backend = process.env.LIGHTING_API_URL ?? "http://127.0.0.1:8000";
const allowedDevOrigins = (
  process.env.NEXT_ALLOWED_DEV_ORIGINS ?? "*.trycloudflare.com"
)
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  // Allow isolated browser smoke tests without touching a running dev build.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  allowedDevOrigins,
  experimental: {
    // Multi-page external OCR can outlive the usual short rewrite timeout.
    proxyTimeout: Number(process.env.LIGHTING_UPLOAD_PROXY_TIMEOUT_MS) || 900_000,
  },
  async rewrites() {
    return [
      {
        source: "/backend/:path*",
        destination: `${backend}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
