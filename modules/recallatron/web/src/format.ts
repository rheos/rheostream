/**
 * Display formatting shared by the screens. Pure, and server-side only: a
 * timestamp renders in the viewer's zone, which the shell learns from the browser
 * (#465), and in UTC, labelled, when the shell gives none.
 */

/** `2026-03-02T10:00:00Z` renders as `2026-03-02 10:00 UTC`, or as
 * `2026-03-02 02:00` in `America/Vancouver`; an unparseable value renders as given. */
export function formatMoment(iso: string, zone = "UTC"): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return zone === "UTC" ? `${iso} UTC` : iso;
  }
  if (zone === "UTC") {
    const text = date.toISOString();
    return `${text.slice(0, 10)} ${text.slice(11, 16)} UTC`;
  }
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZone: zone,
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
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
