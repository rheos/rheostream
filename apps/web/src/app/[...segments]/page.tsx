import { headers } from "next/headers";

import { requestHost } from "@/lib/operations";
import { firstValues, pathFromSegments, renderSurface } from "@/shell/render-surface";

// Request-time for the same reason as the home page: every screen reads core.
export const dynamic = "force-dynamic";

/**
 * Every path below the bare host: a module surface's screens in path mode, and a
 * module's deeper routes on its own host in subdomain mode. `renderSurface` decides
 * which, and answers `notFound()` for anything it does not serve (the segment's
 * `layout.tsx` has already made that call before streaming, for a real 404).
 */
export default async function ModuleSurfacePage(props: {
  params: Promise<{ segments: string[] }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [{ segments }, search, headerList] = await Promise.all([
    props.params,
    props.searchParams,
    headers(),
  ]);
  return renderSurface({
    host: requestHost(headerList) ?? "",
    pathname: pathFromSegments(segments),
    query: firstValues(search),
  });
}
