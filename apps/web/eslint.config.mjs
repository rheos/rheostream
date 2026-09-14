import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const eslintConfig = [
  // eslint-config-next 16 exports native flat configs, so FlatCompat is gone;
  // feeding these through it is what threw "Converting circular structure to
  // JSON". The ignores stay explicit: `next lint` applied them implicitly.
  { ignores: [".next/**", "out/**", "build/**", "next-env.d.ts"] },
  ...nextCoreWebVitals,
  ...nextTypescript,
];

export default eslintConfig;
