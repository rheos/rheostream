import type { NextConfig } from "next";

import packageJson from "./package.json";
import { workspaceTranspilePackages } from "./src/lib/transpile-packages";

const nextConfig: NextConfig = {
  // Workspace packages ship TypeScript source. Derived from this app's own
  // package.json, so a new @rheo-stream/* dependency needs no edit here.
  transpilePackages: workspaceTranspilePackages(packageJson),
};

export default nextConfig;
