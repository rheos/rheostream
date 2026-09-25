"use client";

import panel from "@/shell/panel.module.css";

/**
 * Next's error boundary for the app's route segments: a render or runtime crash
 * below the root layout renders this, themed, instead of a blank screen.
 *
 * It shows no error message. In production a server error's message is replaced
 * by a generic one anyway, and a client error's message can carry details that do
 * not belong on screen. The digest is the identifier that matches the server log.
 */
export default function ErrorBoundary({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  return (
    <main className={panel.page}>
      <section className={panel.panel} role="alert">
        <h1 className={panel.heading}>Something went wrong</h1>
        <p className={panel.line}>This page could not be shown.</p>
        {error.digest ? (
          <p className={panel.detail}>
            Reference: <code>{error.digest}</code>
          </p>
        ) : null}
        <button className={panel.action} type="button" onClick={() => retry()}>
          Try again
        </button>
      </section>
    </main>
  );
}
