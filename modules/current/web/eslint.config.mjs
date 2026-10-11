import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";
const config = [{ ignores: ["node_modules/**"] }, ...nextCoreWebVitals, ...nextTypescript,
{ settings: { react: { version: "19" } }, rules: { "@next/next/no-html-link-for-pages": "off" } }];

export default config;
