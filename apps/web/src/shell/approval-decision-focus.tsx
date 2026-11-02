"use client";

import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";

const noSubscription = () => () => {};
const onClient = () => true;
const onServer = () => false;

/**
 * A durable decision replaces the form during refresh, so the result heading
 * mounts in a client render and takes focus. A plain page load hydrates the
 * same heading from server HTML, where the server snapshot applies; capturing
 * that first value keeps the shell's skip link first after hydration.
 */
export function ApprovalOutcomeHeading({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const target = useRef<HTMLHeadingElement | null>(null);
  const mountedOnClient = useSyncExternalStore(noSubscription, onClient, onServer);
  const [fromDecision] = useState(mountedOnClient);

  useEffect(() => {
    if (fromDecision) {
      target.current?.focus();
    }
  }, [fromDecision]);

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
