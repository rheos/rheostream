/**
 * Every operation these screens call, by its full registered name. This is the only
 * file in the package that spells an operation name: screens and loaders import the
 * constant, so the set of operations the screens can reach is exactly this file's
 * exports. All seven are read-class; `operations.test.ts` and the module's pytest
 * `test_web_operations.py` hold that on both sides.
 */

export const RECALL = "recallatron.memory.recall";
export const READ = "recallatron.memory.read";
export const GET = "recallatron.memory.get";
export const ENTITY_LIST = "recallatron.entity.list";
export const ENTITY_GET = "recallatron.entity.get";
export const DEDUP_CANDIDATES = "recallatron.memory.dedup_candidates";
export const EMBEDDING_COVERAGE = "recallatron.embedding.coverage";
