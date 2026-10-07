import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

// next/font/local is a build-time transform with no runtime export, so vitest
// gets a stand-in that records the options it was called with.
const fontCalls = vi.hoisted((): Array<Record<string, unknown>> => []);
vi.mock("next/font/local", () => ({
  default: (options: Record<string, unknown>) => {
    fontCalls.push(options);
    return { className: "brand-font-class", variable: "brand-font-variable", style: {} };
  },
}));

vi.mock("next/headers", () => ({ cookies: async () => ({ get: () => undefined }) }));

const { default: RootLayout } = await import("./layout");

async function render(): Promise<string> {
  return renderToStaticMarkup(await RootLayout({children: <main>page body</main>}));
}

describe("RootLayout", () => {
  it("injects the compiled seed theme as a style element in head", async () => {
    const html = await render();
    const style = /<head><style>([\s\S]*?)<\/style>/.exec(html);
    expect(style).not.toBeNull();
    expect(style?.[1]).toContain("--rs-color-ground: #06181b;");
    expect(style?.[1]).toContain("color-scheme: dark;");
  });

  it("puts the brand font's variable class on html and keeps lang", async () => {
    expect(await render()).toMatch(/^<html lang="en" class="brand-font-variable" data-theme="greenstream-dark">/);
  });

  it("loads the vendored font file as --rs-font-brand", () => {
    expect(fontCalls).toHaveLength(2);
    const [options] = fontCalls;
    expect(options.variable).toBe("--rs-font-brand");
    expect(typeof options.src).toBe("string");
    const fontsDir = resolve(import.meta.dirname, "../theme");
    expect(existsSync(resolve(fontsDir, String(options.src)))).toBe(true);
    expect(existsSync(resolve(fontsDir, "fonts/OFL.txt"))).toBe(true);
  });

  it("renders its children in body", async () => {
    expect(await render()).toContain("<body><main>page body</main></body>");
  });
});
