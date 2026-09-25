/**
 * Display formatting shared by the screens. Pure, and server-side only: every
 * timestamp renders in UTC and says so, because there is no client script to learn
 * the reader's zone.
 */

/** `2026-03-02T10:00:00Z` renders as `2026-03-02 10:00 UTC`; an unparseable value renders as given. */
export function formatUtc(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return `${iso} UTC`;
  }
  const text = date.toISOString();
  return `${text.slice(0, 10)} ${text.slice(11, 16)} UTC`;
}

/** The first `limit` characters (code points, not UTF-16 units), marked when cut. */
export function excerpt(body: string, limit: number): string {
  const characters = Array.from(body);
  return characters.length <= limit ? body : `${characters.slice(0, limit).join("")}…`;
}

/** Length in code points, the unit the operation's own input bound counts in. */
export function characterCount(text: string): number {
  return Array.from(text).length;
}
