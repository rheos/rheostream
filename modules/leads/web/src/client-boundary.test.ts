import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = __dirname;
const sources = readdirSync(DIR).filter(
  (name) => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name),
);
const isClient = (name: string) =>
  /^\s*["']use client["']/.test(readFileSync(join(DIR, name), "utf8"));
const clientModules = new Set(
  sources.filter(isClient).map((name) => name.replace(/\.tsx?$/, "")),
);

/** Value (not type) names a file imports from each sibling module. */
function valueImports(source: string): { module: string; names: string[] }[] {
  const found: { module: string; names: string[] }[] = [];
  const pattern = /import\s+(?!type\b)\{([^}]*)\}\s+from\s+["']\.\/([^"']+)["']/g;
  for (const match of source.matchAll(pattern)) {
    const names = (match[1] ?? "")
      .split(",")
      .map((part) => part.trim())
      .filter((part) => part && !part.startsWith("type "))
      .map((part) => (part.split(/\s+as\s+/)[0] ?? "").trim());
    found.push({ module: match[2] ?? "", names });
  }
  return found;
}

describe("the server/client boundary", () => {
  it("finds the client modules", () => {
    expect(clientModules.has("job-paste")).toBe(true);
    expect(clientModules.has("queue-view")).toBe(true);
  });
  it("server modules import only components from client modules", () => {
    // A "use client" module's exports are client references on the server: a
    // server screen may render them, never call them.
    const offenders: string[] = [];
    for (const name of sources.filter((file) => !isClient(file))) {
      const source = readFileSync(join(DIR, name), "utf8");
      for (const { module, names } of valueImports(source)) {
        if (!clientModules.has(module)) continue;
        for (const imported of names) {
          if (!/^[A-Z][a-z][A-Za-z0-9]*$/.test(imported)) offenders.push(`${name}: ${imported}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
