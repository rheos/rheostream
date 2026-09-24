import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

// The same flat-config shape as apps/web/eslint.config.mjs, so every web
// package lints under one rule set. This package ships no Next.js app, so there
// is no build output to ignore beyond dependencies, no `pages/` directory for the
// link rule to look for, and no installed `react` for the React plugin to detect
// (the package uses React's types only, through @types/react).
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
