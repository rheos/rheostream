"use client";

import { useState, type FormEvent } from "react";

import type { Membership } from "@/lib/session";

import styles from "./workspace-switcher.module.css";

/**
 * The active-workspace switcher (C9, B3).
 *
 * A client component rather than a plain form because `POST /auth/session/workspace`
 * reads a **JSON** body (`{"target_workspace_id": ...}`), which an
 * `application/x-www-form-urlencoded` form submission cannot produce. `fetch` also
 * sends `Origin` and same-origin cookies by default, which is what the route's
 * CSRF guard and the host-only session cookie need.
 *
 * No workspace identifier appears in any URL: `action` is the same-host path from
 * `switcherAction`, and the target travels in the body (B3).
 */
export function WorkspaceSwitcher({
  action,
  memberships,
  activeWorkspaceId,
}: {
  action: string;
  memberships: Membership[];
  activeWorkspaceId: string;
}) {
  const [target, setTarget] = useState(activeWorkspaceId);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setRefusal(null);
    try {
      const response = await fetch(action, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_workspace_id: target }),
      });
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const state =
          typeof body === "object" &&
          body !== null &&
          typeof (body as Record<string, unknown>).state === "string"
            ? ((body as Record<string, unknown>).state as string)
            : "refused";
        setRefusal(state);
        return;
      }
      // The next request routes to the new workspace by session alone, so the
      // whole page is re-read rather than patched in place.
      window.location.reload();
    } catch {
      setRefusal("unavailable");
    } finally {
      setPending(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={onSubmit}>
      <label className={styles.label} htmlFor="rheo-workspace">
        Active workspace
      </label>
      <select
        id="rheo-workspace"
        name="target_workspace_id"
        value={target}
        onChange={(event) => setTarget(event.target.value)}
      >
        {memberships.map((membership) => (
          <option key={membership.workspace_id} value={membership.workspace_id}>
            {membership.workspace_id} ({membership.role})
          </option>
        ))}
      </select>
      <button type="submit" disabled={pending}>
        Switch
      </button>
      {refusal === null ? null : (
        <p className={styles.refusal} role="status">
          switch refused: {refusal}
        </p>
      )}
    </form>
  );
}
