import { headers } from "next/headers";

import { requestHost } from "@/lib/operations";
import { firstValues, renderSurface } from "@/shell/render-surface";

// Never prerender this route: reading `core`'s /healthz is request-time work, and
// this line — not the fetchCoreHealth try/catch — is what keeps `next build` green
// with no `core` process running (the page body never executes at build time).
export const dynamic = "force-dynamic";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

/**
 * The bare host: the shell's home in path mode, and in subdomain mode either the
 * shell's home or, on a module's own host, that module's `/` route.
 *
 * Which of those it is, and the three seams behind the home panel (core health,
 * routing, session, each with its own failure state), belong to `renderSurface`;
 * this file only hands it the request. `app/[...segments]/page.tsx` does the same
 * for every deeper path, so neither route file holds any resolution logic.
 */
export default async function Page(props: { searchParams?: SearchParams } = {}) {
  const [headerList, search] = await Promise.all([headers(), props.searchParams]);
  return renderSurface({
    host: requestHost(headerList) ?? "",
    pathname: "/",
    query: firstValues(search ?? {}),
  });
}
