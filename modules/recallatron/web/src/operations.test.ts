import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import * as operations from "./operations";

/**
 * The read-only operation allowlist: the seven operations these screens may call.
 * The module's pytest `test_web_operations.py` asserts each one is registered as a
 * read-class operation; this file asserts the package cannot name any other.
 */
const MODULE = "recallatron";
const READ_ONLY_ALLOWLIST = [
  `${MODULE}.memory.recall`,
  `${MODULE}.memory.read`,
  `${MODULE}.memory.get`,
  `${MODULE}.entity.list`,
  `${MODULE}.entity.get`,
  `${MODULE}.memory.dedup_candidates`,
  `${MODULE}.embedding.coverage`,
];

const PACKAGE_ROOT = fileURLToPath(new URL("..", import.meta.url));
const OPERATIONS_FILE = join("src", "operations.ts");
const SKIP_DIRS = new Set(["node_modules", "fixtures", ".turbo"]);
const SOURCE = /\.(?:ts|tsx|js|jsx|mjs|cjs|css|json)$/;

function packageFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return SKIP_DIRS.has(entry.name) ? [] : packageFiles(path);
    return SOURCE.test(entry.name) ? [path] : [];
  });
}

// A quote (any of the three) directly before the module prefix. Built from parts so
// this file does not match its own pattern.
const OPERATION_LITERAL = new RegExp(`["'\`]${MODULE}\\.`);

describe("operations", () => {
  it("exports only read-only operations, and all of them", () => {
    const exported = Object.values(operations);
    for (const value of exported) {
      expect(READ_ONLY_ALLOWLIST).toContain(value);
    }
    expect([...exported].sort()).toEqual([...READ_ONLY_ALLOWLIST].sort());
  });

  it("is the only file in the package that spells an operation name", () => {
    const files = packageFiles(PACKAGE_ROOT);
    expect(files.map((file) => relative(PACKAGE_ROOT, file))).toContain(OPERATIONS_FILE);
    const offenders = files
      .filter((file) => relative(PACKAGE_ROOT, file) !== OPERATIONS_FILE)
      .filter((file) => OPERATION_LITERAL.test(readFileSync(file, "utf8")))
      .map((file) => relative(PACKAGE_ROOT, file));
    expect(offenders).toEqual([]);
  });

  it("scans with a pattern that catches a spelled-out name", () => {
    expect(OPERATION_LITERAL.test(`call("${MODULE}.memory.recall")`)).toBe(true);
    expect(OPERATION_LITERAL.test(readFileSync(join(PACKAGE_ROOT, OPERATIONS_FILE), "utf8"))).toBe(
      true,
    );
  });
});
