import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { OperationOutcome } from "@rheo-stream/web-contract/screen";

import { isRefusalEnvelope } from "../guards";

/**
 * The shared synthetic fixtures in `modules/recallatron/web/fixtures/`, read from disk
 * for tests. A result fixture is an operation's `result`; a refusal fixture is a whole
 * envelope, turned here into the `refused` outcome the shell's operation client would
 * hand a screen.
 */

export const FIXTURE_DIR = fileURLToPath(new URL("../../fixtures/", import.meta.url));

export function fixtureNames(): string[] {
  return readdirSync(FIXTURE_DIR)
    .filter((name) => name.endsWith(".json"))
    .sort();
}

export function fixture(name: string): unknown {
  return JSON.parse(readFileSync(`${FIXTURE_DIR}${name}`, "utf8")) as unknown;
}

export function ok(name: string): OperationOutcome {
  return { state: "ok", result: fixture(name) };
}

export function refused(name: string): OperationOutcome {
  const envelope = fixture(name);
  if (!isRefusalEnvelope(envelope)) {
    throw new Error(`${name} is not a refusal envelope`);
  }
  return {
    state: "refused",
    code: envelope.error.error_code,
    text: envelope.error.error_text,
  };
}

/** A refusal with no fixture of its own, e.g. `not_found`. */
export function refusal(code: string): OperationOutcome {
  return { state: "refused", code, text: code };
}

/** A string field of a fixture object, for building queries from fixture refs. */
export function field(name: string, ...path: (string | number)[]): string {
  let value: unknown = fixture(name);
  for (const key of path) {
    value = (value as Record<string | number, unknown>)[key];
  }
  if (typeof value !== "string") {
    throw new Error(`${name} ${path.join(".")} is not a string`);
  }
  return value;
}
