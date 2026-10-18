import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = __dirname;
const sources = readdirSync(DIR).filter(
  (name) => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name),
);
/** The directive may follow comments, but nothing else. */
const isClient = (name: string) => {
  const text = readFileSync(join(DIR, name), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  return /^\s*["']use client["']/.test(text);
};
const clientModules = new Set(
  sources.filter(isClient).map((name) => name.replace(/\.tsx?$/, "")),
);

/** Every value binding a file takes from a sibling module, by import or re-export.
 * A namespace import or a re-export of everything is reported as "*", which is
 * never a component. */
function valueBindings(
  source: string,
): { module: string; names: string[] }[] {
  const found: { module: string; names: string[] }[] = [];
  const pattern =
    /\b(import|export)\s+(?!type\b)([^;]*?)\s*from\s+["']\.\/([^"']+)["']/g;
  for (const match of source.matchAll(pattern)) {
    const clause = match[2] ?? "";
    const names: string[] = [];
    const braces = /\{([^}]*)\}/.exec(clause);
    for (const part of (braces?.[1] ?? "").split(",")) {
      const item = part.trim();
      if (item && !item.startsWith("type ")) {
        names.push((item.split(/\s+as\s+/)[0] ?? "").trim());
      }
    }
    const outside = clause.replace(/\{[^}]*\}/, "").replace(/,/g, " ").trim();
    if (/\*/.test(outside)) names.push("*");
    else if (match[1] === "import" && outside) names.push(outside);
    found.push({ module: match[3] ?? "", names });
  }
  return found;
}

const COMPONENT = /^[A-Z][a-z][A-Za-z0-9]*$/;

describe("the server/client boundary", () => {
  it("finds every client module", () => {
    expect([...clientModules].sort()).toEqual([
      "action-form",
      "finish-connecting",
      "job-paste",
      "paste-capture",
      "proposal-workflow",
      "queue-view",
      "review-view",
    ]);
  });
  it("reads every import and re-export form", () => {
    const planted = [
      'import { JobPaste,\n  pipelineRef } from "./job-paste";',
      'export { pipelineRef } from "./job-paste";',
      'import * as paste from "./job-paste";',
      'import Paste, { PAGE_LIMIT } from "./job-paste";',
      'import { type PasteKind, JobPaste } from "./job-paste";',
    ].join("\n");
    expect(valueBindings(planted).map((b) => b.names)).toEqual([
      ["JobPaste", "pipelineRef"],
      ["pipelineRef"],
      ["*"],
      ["PAGE_LIMIT", "Paste"],
      ["JobPaste"],
    ]);
  });
  it("server modules take only components from client modules", () => {
    // A "use client" module's exports are client references on the server: a
    // server screen may render them, never call or read them.
    const offenders: string[] = [];
    for (const name of sources.filter((file) => !isClient(file))) {
      const source = readFileSync(join(DIR, name), "utf8");
      for (const { module, names } of valueBindings(source)) {
        if (!clientModules.has(module)) continue;
        for (const bound of names) {
          if (!COMPONENT.test(bound)) offenders.push(`${name}: ${bound}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
