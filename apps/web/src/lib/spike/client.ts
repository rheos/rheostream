import type { paths } from "@/generated/api-types";
import {
  INTERNAL_OPERATIONS_PATH,
  INTERNAL_SECRET_HEADER,
  internalApiCredentials,
  type OperationPathKey,
} from "@/lib/routing/load";
import { HOST_HEADER, SESSION_HEADER } from "@/lib/session";

/**
 * The typed operations client (0v, C4) — deleted at 0c0's branch cut.
 *
 * One function, `runOperation`, typed entirely from
 * `src/generated/api-types.ts`, which `openapi-typescript` derives from
 * `src/generated/openapi.json`, which `rheo openapi` derives from the operation
 * registry. Nothing here restates an input or output shape: add a field to
 * `NoteAddInput` in `modules/spike` and this file's callers stop compiling, which
 * is the whole point of shape 5.
 *
 * **The path keys it is typed by are not the path it posts to, and that is
 * deliberate.** The generated document keys one path per operation under the
 * bearer `api` surface's own prefix (`/api/v1/operations/<name>`), while the web
 * tier reaches `dispatch()` only through the **internal** listener
 * (`/internal/v1/operations/<name>`). Both listeners serve the same operation
 * contract over the same `dispatch()` and differ only in how the caller
 * authenticates, so the schemas are right — but they are right by agreement
 * rather than by construction. That is run 0v's finding F20, and it is held here
 * rather than papered over: the types come from `paths[...]`, the request goes to
 * `INTERNAL_OPERATIONS_PATH`.
 *
 * **Fail-closed, mirroring `lib/session.ts` rather than `lib/core-health.ts`.**
 * A 200 whose body fails the guard below is not a success. An unparseable body, a
 * body of the wrong shape, an unset credential, a rejected fetch and the
 * internal listener's own shared-secret 401 all resolve to `unavailable`, and
 * none of them can produce a `succeeded`.
 *
 * **It reads the envelope on a non-2xx too, and that is the one place it
 * deliberately departs from `session.ts`.** `session.ts` treats any non-ok as
 * `unavailable` because a failing status there says nothing about the session.
 * Here the status is *derived from* the state we need: `outcome_status` maps
 * `input_invalid` to 422, `role_not_permitted` to 403, `not_found` to 404. Reading
 * only 200s would collapse every refusal into `unavailable`, and the spike's
 * route handler would redirect `?add=unavailable` for an operation that refused
 * for a nameable reason — which is exactly the outcome-discarding failure the
 * response contract exists to prevent.
 */

/**
 * One registered operation name, extracted from the generated path keys.
 *
 * The template itself is `OperationPathKey`, imported from `lib/routing/load.ts`:
 * a type-level route literal is still a route literal, and this file is outside
 * the `routing/` package that `scripts/check_routing_literals.py` allowlists.
 */
type NameOf<Key> = Key extends OperationPathKey<infer Name> ? Name : never;

export type OperationName = NameOf<keyof paths>;

type Post<Name extends OperationName> = paths[OperationPathKey<Name>]["post"];

/** The operation's own input model, as the document declares it. */
export type OperationInput<Name extends OperationName> =
  Post<Name>["requestBody"]["content"]["application/json"];

/** The operation envelope: `{state, operation_id: null, result | error}`. */
type OperationEnvelope<Name extends OperationName> =
  Post<Name>["responses"][200]["content"]["application/json"];

/** The operation's own output model, as the document declares it. */
export type OperationResult<Name extends OperationName> = NonNullable<
  OperationEnvelope<Name>["result"]
>;

export interface OperationError {
  error_code: string;
  error_text: string;
}

export type OperationOutcome<Name extends OperationName> =
  | { state: "succeeded"; result: OperationResult<Name> }
  | { state: string; error: OperationError | null }
  | { state: "unavailable" };

/** The one success state the dispatcher reports; every other state is a refusal. */
export const SUCCEEDED = "succeeded";

/**
 * Whether an outcome is the success case, as a type predicate.
 *
 * **The union above cannot be narrowed by `state` alone, and that is a property
 * of the shape rather than of this code.** The refusal member's `state` is
 * `string` — it has to be: the dispatcher's refusal states are open
 * (`input_invalid`, `module_disabled`, `role_not_permitted`, `failed`, whatever
 * 0c adds) and are not enumerable here. `string` includes the literal
 * `"succeeded"`, so TypeScript cannot use `state` as a discriminant, and
 * `outcome.state !== "succeeded"` narrows nothing. Widening `state` to a closed
 * literal union would be worse: it would either restate the dispatcher's refusal
 * vocabulary in the web tier, which is exactly what the generated types exist to
 * avoid, or silently drop a state the dispatcher added.
 *
 * So the discriminant is the presence of `result`, and this function is where
 * that is said once rather than at every call site. It re-checks `state` too, so
 * a body carrying a `result` under a refusal state could never be read as a
 * success — `runOperation` already refuses to build one, and this is the second
 * lock on the same door.
 */
export function succeeded<Name extends OperationName>(
  outcome: OperationOutcome<Name>,
): outcome is { state: typeof SUCCEEDED; result: OperationResult<Name> } {
  return outcome.state === SUCCEEDED && "result" in outcome;
}

/** The sentinel for "this never reached the dispatcher at all". */
export const UNAVAILABLE = "unavailable";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readError(value: unknown): OperationError | null {
  if (
    !isRecord(value) ||
    typeof value.error_code !== "string" ||
    typeof value.error_text !== "string"
  ) {
    return null;
  }
  return { error_code: value.error_code, error_text: value.error_text };
}

/**
 * Narrow a parsed body to the envelope both listeners answer with.
 *
 * `operation_id` is required and must be `null`: the generated type says exactly
 * that (`operation_id: null`), because no operation record is minted before 0c.
 * A body without it is not this contract — most likely FastAPI's own
 * `{"detail": ...}` from the shared-secret dependency, which must never be read
 * as an operation outcome.
 */
function isEnvelope(
  value: unknown,
): value is { state: string; operation_id: null; result?: unknown; error?: unknown } {
  return (
    isRecord(value) &&
    typeof value.state === "string" &&
    "operation_id" in value &&
    value.operation_id === null
  );
}

/**
 * Run one registered operation for the session behind `sessionSecret`/`host`.
 *
 * The three headers are the internal listener's own and are not interchangeable:
 * `X-Rheo-Internal` is the shared secret the listener gates every route on,
 * `X-Rheo-Session` carries the session secret the browser's cookie holds, and
 * `X-Rheo-Host` is the host the browser actually asked for — which is what decides
 * which session applies, because the cookie is host-only. **No workspace
 * identifier is sent**, in the path, the query or the body: the workspace comes
 * from the session row and nowhere else (FR 5).
 */
export async function runOperation<Name extends OperationName>(options: {
  name: Name;
  input: OperationInput<Name>;
  sessionSecret: string | undefined;
  host: string | undefined;
}): Promise<OperationOutcome<Name>> {
  const credentials = internalApiCredentials();
  if (credentials === null) {
    return { state: UNAVAILABLE };
  }
  if (!options.sessionSecret || !options.host) {
    // Knowable without a round trip, and the same refusal the listener would
    // return for the same inputs. Not a success by any path.
    return { state: UNAVAILABLE };
  }
  let body: unknown;
  try {
    const response = await fetch(
      `${credentials.baseUrl}${INTERNAL_OPERATIONS_PATH}/${encodeURIComponent(options.name)}`,
      {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          [INTERNAL_SECRET_HEADER]: credentials.secret,
          [SESSION_HEADER]: options.sessionSecret,
          [HOST_HEADER]: options.host,
        },
        body: JSON.stringify(options.input),
      },
    );
    body = await response.json();
  } catch {
    // A rejected fetch and an unparseable body are the same thing to a caller:
    // no outcome was reported, so none may be invented.
    return { state: UNAVAILABLE };
  }
  if (!isEnvelope(body)) {
    return { state: UNAVAILABLE };
  }
  if (body.state !== SUCCEEDED) {
    return { state: body.state, error: readError(body.error) };
  }
  if (!isRecord(body.result)) {
    // A body claiming `succeeded` that carries no result object is a contract
    // break, not a success with unknown details — reporting it as the latter is
    // precisely the fail-open this guard exists to prevent.
    return { state: UNAVAILABLE };
  }
  return {
    state: SUCCEEDED,
    result: body.result as OperationResult<Name>,
  };
}
