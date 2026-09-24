/**
 * Type-narrowing guards for every response shape the screens read.
 *
 * These stand in for generated types, which cannot serve here: `make codegen` runs with
 * no module installed, so no Recallatron operation appears in the shell's generated
 * OpenAPI client. The guards are proven against the shared JSON fixtures in
 * `../fixtures/`, which the module's pytest `test_web_fixtures.py` validates against
 * the Python output models, so the two languages meet at those files.
 *
 * A guard checks every field a screen could read, with its type. It does not refuse an
 * unknown extra key: the Python models forbid extras, and the fixture test already
 * names a key mismatch, so a stricter guard here would only turn an additive server
 * change into a blank screen.
 */

type Json = Readonly<Record<string, unknown>>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function isNullableInteger(value: unknown): value is number | null {
  return value === null || isInteger(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isArrayOf<T>(value: unknown, guard: (item: unknown) => item is T): value is T[] {
  return Array.isArray(value) && value.every((item) => guard(item));
}

// --- memory items -----------------------------------------------------------------

export interface MemoryLinkHead {
  ref: string;
  relation: string;
  supersession_lineage: boolean;
  display: string | null;
}

export interface MemoryItem {
  ref: string;
  kind: string;
  title: string;
  body: string;
  occurred_at: string | null;
  recorded_at: string;
  revision: number;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  superseded_by: string | null;
  links: MemoryLinkHead[];
}

export interface RecallItem extends MemoryItem {
  score: number;
  strategy: string;
}

export function isMemoryLinkHead(value: unknown): value is MemoryLinkHead {
  return (
    isRecord(value) &&
    isString(value.ref) &&
    isString(value.relation) &&
    typeof value.supersession_lineage === "boolean" &&
    isNullableString(value.display)
  );
}

export function isMemoryItem(value: unknown): value is MemoryItem {
  return (
    isRecord(value) &&
    isString(value.ref) &&
    isString(value.kind) &&
    isString(value.title) &&
    isString(value.body) &&
    isNullableString(value.occurred_at) &&
    isString(value.recorded_at) &&
    isInteger(value.revision) &&
    isNullableString(value.invalidated_at) &&
    isNullableString(value.invalidation_reason) &&
    isNullableString(value.superseded_by) &&
    isArrayOf(value.links, isMemoryLinkHead)
  );
}

export function isRecallItem(value: unknown): value is RecallItem {
  return (
    isRecord(value) &&
    isFiniteNumber(value.score) &&
    isString(value.strategy) &&
    isMemoryItem(value)
  );
}

// --- recall -----------------------------------------------------------------------

export interface RecallProvenance {
  strategy: string;
  arms: { lexical: number; dense: number };
  dense_available: boolean;
}

export interface RecallResult {
  items: RecallItem[];
  provenance: RecallProvenance;
}

function isRecallProvenance(value: unknown): value is RecallProvenance {
  return (
    isRecord(value) &&
    isString(value.strategy) &&
    isRecord(value.arms) &&
    isInteger(value.arms.lexical) &&
    isInteger(value.arms.dense) &&
    typeof value.dense_available === "boolean"
  );
}

export function isRecallResult(value: unknown): value is RecallResult {
  return (
    isRecord(value) &&
    isArrayOf(value.items, isRecallItem) &&
    isRecallProvenance(value.provenance)
  );
}

// --- read -------------------------------------------------------------------------

export interface ReadWindow {
  items: MemoryItem[];
  target_position: number | null;
  window_start: number;
  window_end: number;
  total: number;
  has_more: boolean;
}

export function isReadWindow(value: unknown): value is ReadWindow {
  return (
    isRecord(value) &&
    isArrayOf(value.items, isMemoryItem) &&
    isNullableInteger(value.target_position) &&
    isInteger(value.window_start) &&
    isInteger(value.window_end) &&
    isInteger(value.total) &&
    typeof value.has_more === "boolean"
  );
}

// --- entities ---------------------------------------------------------------------

/** Exactly the five fields the entity operations answer with, and no others. */
export interface EntityItem {
  ref: string;
  kind: string;
  name: string;
  backing_ref: string | null;
  mention_count: number;
}

export interface EntityList {
  items: EntityItem[];
}

export function isEntityItem(value: unknown): value is EntityItem {
  return (
    isRecord(value) &&
    isString(value.ref) &&
    isString(value.kind) &&
    isString(value.name) &&
    isNullableString(value.backing_ref) &&
    isInteger(value.mention_count)
  );
}

export function isEntityList(value: unknown): value is EntityList {
  return isRecord(value) && isArrayOf(value.items, isEntityItem);
}

// --- duplicates -------------------------------------------------------------------

export interface DedupPair {
  ref_a: string;
  ref_b: string;
  score: number;
}

export interface DedupCandidates {
  pairs: DedupPair[];
}

function isDedupPair(value: unknown): value is DedupPair {
  return (
    isRecord(value) &&
    isString(value.ref_a) &&
    isString(value.ref_b) &&
    isFiniteNumber(value.score)
  );
}

export function isDedupCandidates(value: unknown): value is DedupCandidates {
  return isRecord(value) && isArrayOf(value.pairs, isDedupPair);
}

// --- embedding coverage -----------------------------------------------------------

export interface EmbeddingCoverageReport {
  model_id: string | null;
  live: number | null;
  embedded: number | null;
}

export function isEmbeddingCoverageReport(value: unknown): value is EmbeddingCoverageReport {
  return (
    isRecord(value) &&
    isNullableString(value.model_id) &&
    isNullableInteger(value.live) &&
    isNullableInteger(value.embedded)
  );
}

// --- refusals ---------------------------------------------------------------------

/** The refusal codes a screen names a state for. Any other code is the generic error. */
export const NOT_FOUND = "not_found";
export const REFERENCE_SCAN_LIMIT = "reference_scan_limit";
export const WINDOW_SCAN_LIMIT = "window_scan_limit";

/**
 * A refused operation's whole envelope, as the core answers it:
 * `{state, operation_id, error: {error_code, error_text}}`. The shell's operation
 * client narrows this into `OperationOutcome`'s `refused` arm before a screen sees it;
 * the guard exists so the refusal fixtures are held to the same shape.
 */
export interface RefusalEnvelope {
  state: string;
  operation_id: string | null;
  error: { error_code: string; error_text: string };
}

export function isRefusalEnvelope(value: unknown): value is RefusalEnvelope {
  return (
    isRecord(value) &&
    isString(value.state) &&
    isNullableString(value.operation_id) &&
    isRecord(value.error) &&
    isString(value.error.error_code) &&
    isString(value.error.error_text)
  );
}

function isRefusalWithCode(value: unknown, code: string): value is RefusalEnvelope {
  return isRefusalEnvelope(value) && value.error.error_code === code;
}

/** `entity.list` over its reference budget (fixture `entity-list-refused-budget.json`). */
export function isEntityListRefusedBudget(value: unknown): value is RefusalEnvelope {
  return isRefusalWithCode(value, REFERENCE_SCAN_LIMIT);
}

/** `read` past its window scan bound (fixture `read-refused-window.json`). */
export function isReadRefusedWindow(value: unknown): value is RefusalEnvelope {
  return isRefusalWithCode(value, WINDOW_SCAN_LIMIT);
}

/** `read` over its reference budget (fixture `read-refused-budget.json`). */
export function isReadRefusedBudget(value: unknown): value is RefusalEnvelope {
  return isRefusalWithCode(value, REFERENCE_SCAN_LIMIT);
}
