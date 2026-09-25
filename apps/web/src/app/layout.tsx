import type { Metadata } from "next";
import type { ReactNode } from "react";

import { BUILT_IN_THEMES } from "@/theme/builtins";
import { builtInThemeCss } from "@/theme/built-in-css";
import { brandFont } from "@/theme/font";

import "./globals.css";

export const metadata: Metadata = {
  title: "rheoStream",
  description: "rheoStream application shell.",
};

// Compiled once at module load. A seed theme that fails validation throws here,
// which fails the build or the server's boot instead of rendering unstyled.
const themeCss = builtInThemeCss(BUILT_IN_THEMES["seed-dark"]);

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={brandFont.variable}>
      <head>
        {/* Not escaped: every value was grammar-checked by validateTheme and
            again by compileTheme, and no token grammar admits `<`, `;`, `{` or `}`. */}
        <style dangerouslySetInnerHTML={{ __html: themeCss }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
