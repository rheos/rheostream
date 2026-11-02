"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * A durable decision replaces the form during refresh. Keep the result an
 * ordinary server-rendered heading, then move focus once hydration can do so.
 */
export function ApprovalOutcomeHeading({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const target = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    target.current?.focus();
  }, []);

  return (
    <h1 className={className} ref={target} tabIndex={-1}>
      {children}
    </h1>
  );
}

/** A refused/stale action stays on the form, so focus its live error summary. */
export function ApprovalActionError({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const target = useRef<HTMLParagraphElement | null>(null);

  useEffect(() => {
    target.current?.focus();
  }, [children]);

  return (
    <p className={className} ref={target} role="alert" tabIndex={-1}>
      {children}
    </p>
  );
}
