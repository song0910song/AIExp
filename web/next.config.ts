import type { NextConfig } from "next";

const backend = process.env.LIGHTING_API_URL ?? "http://127.0.0.1:8000";
const allowedDevOrigins = (
  process.env.NEXT_ALLOWED_DEV_ORIGINS ?? "*.trycloudflare.com"
)
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  allowedDevOrigins,
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
