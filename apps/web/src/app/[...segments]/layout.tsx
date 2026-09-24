import { headers } from "next/headers";
import type { ReactNode } from "react";

import { requestHost } from "@/lib/operations";
import { ensureServed, pathFromSegments } from "@/shell/render-surface";

/**
 * Decides "not served here" before the segment's `loading.tsx` boundary starts
 * streaming, so an unknown path or a module this workspace has not enabled answers
 * a real 404 rather than a not-found page on a 200 (see `ensureServed`). It renders
 * nothing of its own.
 */
export default async function ModuleSurfaceLayout(props: {
  children: ReactNode;
  params: Promise<{ segments: string[] }>;
}) {
  const [{ segments }, headerList] = await Promise.all([props.params, headers()]);
  await ensureServed({
    host: requestHost(headerList) ?? "",
    pathname: pathFromSegments(segments),
  });
  return props.children;
}
