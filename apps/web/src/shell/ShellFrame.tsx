import type { ReactNode } from "react";

import { LogoutForm } from "@/components/logout-form";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";
import type { CoreHealthResult } from "@/lib/core-health";
import type { Membership } from "@/lib/session";

import styles from "./ShellFrame.module.css";

/**
 * What the header needs to offer a signed-in account its controls. The caller
 * passes it only on the signed-in path, after its own routing and session guards,
 * so this component never decides whether anyone is signed in.
 */
export interface ShellAccount {
  memberships: Membership[];
  activeWorkspaceId: string;
  /** Same-host path from `switcherAction`. */
  switcherAction: string;
  /** Same-host path from `logoutAction`. */
  logoutAction: string;
}

/**
 * The frame every shell page renders inside: a header with the product name, the
 * navigation slot and (signed in only) the compact workspace switcher and logout,
 * then the page, then the core-status line.
 *
 * The status line keeps the literal text `contract v`: `make demo`'s health check
 * greps the rendered home page for exactly that substring.
 *
 * Every color, font and spacing value in `ShellFrame.module.css` is a `--rs-*`
 * theme token. There is no search input here, and there must never be one: the
 * only search input in the application belongs to the memory module's own Search
 * screen.
 */
export function ShellFrame({
  health,
  account,
  children,
}: {
  health: CoreHealthResult;
  account?: ShellAccount | null;
  children: ReactNode;
}) {
  return (
    <div className={styles.frame}>
      <header className={styles.header}>
        <p className={styles.brand}>rheoStream</p>
        {/* Navigation slot. Entries come from the module manifests composed at build
            time and filtered per workspace at runtime (decision A18), rendered here
            as a <nav> once the shell composition runtime supplies them. Empty until
            then, and never hard-wired to any one module. */}
        {account ? (
          <div className={styles.controls}>
            <WorkspaceSwitcher
              action={account.switcherAction}
              memberships={account.memberships}
              activeWorkspaceId={account.activeWorkspaceId}
            />
            <LogoutForm action={account.logoutAction} />
          </div>
        ) : null}
      </header>
      <main className={styles.main}>{children}</main>
      <footer className={styles.footer}>
        {health.status === "ok" ? (
          <p className={styles.statusOk}>core: ok (contract v{health.contractVersion})</p>
        ) : (
          <p className={styles.statusDown}>core: unavailable</p>
        )}
      </footer>
    </div>
  );
}
