import { cookies } from "next/headers";
import type { Metadata } from "next";
import type { ReactNode } from "react";

import { BUILT_IN_THEMES, themeId } from "@/theme/builtins";
import { builtInThemeCss } from "@/theme/built-in-css";
import { brandFont, novadiemFont } from "@/theme/font";

import "./globals.css";

export const metadata: Metadata = {
  title: "rheoStream",
  description: "rheoStream application shell.",
};

const themeStyles = Object.fromEntries(Object.entries(BUILT_IN_THEMES).map(([id, raw]) => [id, builtInThemeCss(raw)]));

export default async function RootLayout({ children }: { children: ReactNode }) {
  const theme = themeId((await cookies()).get("rheo_theme")?.value);
  const themeCss = themeStyles[theme];
  return (
    <html lang="en" className={theme.startsWith("novadiem") ? novadiemFont.variable : brandFont.variable} data-theme={theme}>
      <head>
        {/* Not escaped: every value was grammar-checked by validateTheme and
            again by compileTheme, and no token grammar admits `<`, `;`, `{` or `}`. */}
        <style dangerouslySetInnerHTML={{ __html: themeCss }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
