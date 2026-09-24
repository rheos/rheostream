import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// The same flat-config shape as packages/web-contract/eslint.config.mjs, so every
// web package lints under one rule set. This package ships screens, not a Next.js
// app, so there is no build output to ignore and no `pages/` directory for the
// link rule to look for.
const eslintConfig = [
  { ignores: ["node_modules/**"] },
  ...nextCoreWebVitals,
  ...nextTypescript,
  {
    settings: { react: { version: "19" } },
    rules: { "@next/next/no-html-link-for-pages": "off" },
  },
];

export default eslintConfig;
