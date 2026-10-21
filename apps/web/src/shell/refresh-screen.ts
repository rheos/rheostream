"use server";

import { refresh } from "next/cache";

/** Renders the calling page's screen again in place (#443): a screen following
 * running work uses this instead of reloading the page. It reads nothing and
 * changes nothing; the re-render runs every session and module check again. */
export async function refreshScreen(): Promise<void> {
  refresh();
}
