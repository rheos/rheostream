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
  // The read-only guarantee: `ShellApi.call` takes any string, so the package reaches
  // it only through `src/call.ts`'s `callChecked`, whose operation parameter is the
  // `Operation` union from `src/operations.ts`. Any other `shell.call` (or
  // `props.shell.call`) outside that file and the test helpers is a lint error.
  {
    files: ["src/**/*.ts", "src/**/*.tsx"],
    ignores: ["src/call.ts", "src/testing/**", "src/**/*.test.ts", "src/**/*.test.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "MemberExpression[property.name='call'][object.name='shell'], MemberExpression[property.name='call'][object.property.name='shell']",
          message: "Call operations through callChecked in src/call.ts, never shell.call directly.",
        },
      ],
    },
  },
];

export default eslintConfig;
