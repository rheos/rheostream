import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Workspace packages ship TypeScript source. Literal for now; Prompt 9 derives
  // this list from package.json when a second @rheo-stream/* package arrives.
  transpilePackages: ["@rheo-stream/web-contract"],
};

export default nextConfig;
