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
 * One entry of the composed navigation, ready to render: `renderSurface` builds the
 * `href` with `urlFor` and marks the entry for the page being shown as `current`.
 */
export interface ShellNavigationItem {
  key: string;
  label: string;
  href: string;
  current: boolean;
}

/** The skip link's target. A fragment on the current page, not a route. */
const MAIN_ID = "rheo-main";

/**
 * The frame the shell's signed-in-capable pages render inside, the home page and
 * every module screen: a header with the product name, the composed navigation and
 * (signed in only) the compact workspace switcher and logout, then the page, then
 * the core-status line. Sign-in, not-found and the error boundary do not use it;
 * they render a bare panel with no account or health context.
 *
 * The navigation is whatever `composeNavigation` let through for this workspace
 * and role (decision A18). The frame names no module and renders no `<nav>` when
 * the list is empty.
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
  navigation = [],
  children,
}: {
  health: CoreHealthResult;
  account?: ShellAccount | null;
  navigation?: readonly ShellNavigationItem[];
  children: ReactNode;
}) {
  return (
    <div className={styles.frame}>
      {/* The first stop for a keyboard: past the header's links and controls to the
          page itself. `main` has tabIndex -1 so following the link moves focus there
          without adding `main` to the Tab order. */}
      <a className={styles.skip} href={`#${MAIN_ID}`}>
        Skip to content
      </a>
      <header className={styles.header}>
        <p className={styles.brand}>rheoStream</p>
        {navigation.length > 0 ? (
          <nav className={styles.nav} aria-label="Workspace">
            <ul className={styles.navList}>
              {navigation.map((item) => (
                <li key={item.key}>
                  <a
                    className={styles.navLink}
                    href={item.href}
                    aria-current={item.current ? "page" : undefined}
                  >
                    {item.label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
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
      <main id={MAIN_ID} tabIndex={-1} className={styles.main}>
        {children}
      </main>
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
